"""Git worktree discovery and the branch actions the dashboard can ask for.

Discovery is read-only. Actions change local Git state only when the dashboard asks,
and refuse to switch or remove a checkout with uncommitted work.
"""
import concurrent.futures
import hashlib
import os
import re
import subprocess
from pathlib import Path

from .agents import latest_sessions


def normalize_roots(roots):
    normalized = []
    seen = set()
    for root in roots:
        canonical = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
        if canonical not in seen:
            seen.add(canonical)
            normalized.append(canonical)
    return normalized


def warn_unavailable_roots(roots):
    for root in roots:
        if not os.path.exists(root):
            print(f"Warning: scan root does not exist and will be skipped: {root}")
        elif not os.path.isdir(root):
            print(f"Warning: scan root is not a directory and will be skipped: {root}")


class GitError(RuntimeError):
    pass


def run_git(repo, *args, timeout=10):
    try:
        return subprocess.run(["git", "-C", repo, *args],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git {' '.join(args)} timed out after {e.timeout} seconds") from e
    except OSError as e:
        raise GitError(f"could not run git {' '.join(args)}: {e}") from e


def command_error(result):
    return (result.stderr or result.stdout or
            f"git exited with status {result.returncode}").strip()


def git(repo, *args, timeout=10):
    r = run_git(repo, *args, timeout=timeout)
    if r.returncode != 0:
        raise GitError(command_error(r))
    return r.stdout


def try_git(repo, *args, timeout=10):
    try:
        return git(repo, *args, timeout=timeout)
    except GitError:
        return ""


def git_ref_exists(repo, ref):
    r = run_git(repo, "show-ref", "--verify", "--quiet", ref)
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    raise GitError(command_error(r))


def mutate_git(repo, *args, timeout):
    try:
        r = run_git(repo, *args, timeout=timeout)
    except GitError as e:
        return str(e)
    return command_error(r) if r.returncode != 0 else None


def parse_origin(url):
    m = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?/?$", url.strip())
    return m.group(1) if m else None


def validate_branch(branch):
    branch = branch.strip()
    if not branch:
        return None, "Branch name is required."
    try:
        result = subprocess.run(["git", "check-ref-format", "--branch", branch],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"Could not validate branch name: {e}"
    if result.returncode != 0:
        return None, (result.stderr or f"Invalid branch name: {branch}").strip()
    return branch, None


def worktree_path(clone, branch):
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", branch).strip(".-") or "branch"
    digest = hashlib.sha256(branch.encode()).hexdigest()[:10]
    return os.path.join(clone, ".claude", "worktrees", f"{slug[:80]}-{digest}")


def workspace_in(path):
    files = sorted(Path(path).glob("*.code-workspace"))
    return str(files[0]) if files else None


def git_dir_for(work_dir):
    dot = os.path.join(work_dir, ".git")
    if os.path.isdir(dot):
        return dot
    try:  # a linked worktree's .git is a file pointing at the real git dir
        line = Path(dot).read_text().strip()
    except OSError:
        return None
    return line[7:].strip() if line.startswith("gitdir:") else None


def last_activity(work_dir):
    """When HEAD last moved here (commit, checkout, merge, reset) — one stat, no git call.

    The index would also catch staging, but our own read-only status checks refresh
    its mtime, which would make every clone look freshly used. The HEAD reflog only
    moves when the user does something.
    """
    git_dir = git_dir_for(work_dir)
    if not git_dir:
        return 0
    try:
        return int(os.path.getmtime(os.path.join(git_dir, "logs", "HEAD")))
    except OSError:
        return 0


def is_dirty(path):
    # Dirty state gates the move/open actions: a dirty main clone can't switch
    # branches, and a dirty linked worktree can't be removed to relocate its
    # branch. "dirty" = uncommitted edits to tracked files; untracked files
    # (node_modules, build output) carry across a switch, so they don't count.
    try:
        return has_tracked_changes(path)
    except GitError as e:
        print(f"Warning: could not verify status for {path}: {e}")
        return True


def worktrees_for_repo(repo_dir):
    repo = parse_origin(try_git(repo_dir, "config", "--get", "remote.origin.url"))
    out, cur = [], {}
    try:
        worktree_list = git(repo_dir, "worktree", "list", "--porcelain")
    except GitError as e:
        print(f"Warning: could not inspect worktrees for {repo_dir}: {e}")
        return []
    for line in worktree_list.splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
        elif line == "" and cur.get("path") and cur.get("branch"):
            is_main = os.path.realpath(cur["path"]) == os.path.realpath(repo_dir)
            out.append({"repo": repo, "branch": cur["branch"], "path": cur["path"],
                        "workspace": workspace_in(cur["path"]), "main": is_main,
                        "activity": last_activity(cur["path"])})
            cur = {}
    return out


def with_dirty_state(worktrees):
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        for w, dirty in zip(worktrees, pool.map(is_dirty, [w["path"] for w in worktrees])):
            w["dirty"] = dirty
    return worktrees


def clone_dirs(roots):
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            repo_dir = os.path.join(root, entry)
            # Only main clones (a linked worktree has a .git *file*, not dir).
            if os.path.isdir(os.path.join(repo_dir, ".git")):
                yield repo_dir


def discover(roots, agent=None):
    repo_dirs = list(dict.fromkeys(os.path.realpath(d) for d in clone_dirs(roots)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        out = [w for worktrees in pool.map(worktrees_for_repo, repo_dirs) for w in worktrees]
    with_dirty_state(out)
    sessions = latest_sessions([w["path"] for w in out], agent)
    for w in out:
        w["session"] = sessions.get(w["path"])
    return out


def find_clone(repo, roots):
    for repo_dir in clone_dirs(roots):
        if parse_origin(try_git(repo_dir, "config", "--get", "remote.origin.url")) == repo:
            return repo_dir
    return None


def default_branch(clone):
    ref = try_git(clone, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    prefix = "refs/remotes/origin/"
    if ref.startswith(prefix):
        return ref[len(prefix):]
    for b in ("main", "master"):
        if try_git(clone, "rev-parse", "--verify", f"origin/{b}").strip():
            return b
    return "main"


def new_task_worktree(repo, branch, roots):
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    branch, error = validate_branch(branch)
    if error:
        return {"ok": False, "error": error}
    # Idempotent: if the branch is already checked out somewhere, open that.
    existing = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
    if existing:
        return {"ok": True, "path": existing["path"], "workspace": existing.get("workspace"), "created": False}
    base = default_branch(clone)
    try_git(clone, "fetch", "origin", base)  # best effort: branch off a fresh base
    path = worktree_path(clone, branch)
    try:
        local_exists = git_ref_exists(clone, f"refs/heads/{branch}")
        if not local_exists and not git_ref_exists(clone, f"refs/remotes/origin/{base}"):
            return {"ok": False, "error": f"Default branch origin/{base} was not found locally."}
    except GitError as e:
        return {"ok": False, "error": f"Could not inspect local branches: {e}"}
    args = ("worktree", "add", path, branch) if local_exists else (
        "worktree", "add", "-b", branch, path, f"origin/{base}")
    error = mutate_git(clone, *args, timeout=120)
    if error:
        # A checkout or post-checkout hook may have completed before a timeout/error.
        existing = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
        if existing:
            return {"ok": True, "path": existing["path"], "workspace": existing.get("workspace"),
                    "created": os.path.realpath(existing["path"]) == os.path.realpath(path)}
        return {"ok": False, "error": error}
    return {"ok": True, "path": path, "workspace": workspace_in(path), "created": True, "branch": branch}


def checked_path(target, roots):
    """(real path, error) for a path the dashboard asked to open."""
    real = os.path.realpath(target)
    def is_within(root):
        try:
            return os.path.commonpath((real, root)) == root
        except ValueError:
            return False
    if not any(is_within(root) for root in roots):
        return None, "Path is outside the configured roots."
    if not os.path.exists(real):
        return None, f"Path does not exist: {real}"
    return real, None


def run_open_command(name, command):
    try:
        r = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or f"{name} failed to open").strip()}
    return {"ok": True}


def launch_app(target, roots, config):
    real, error = checked_path(target, roots)
    if error:
        return {"ok": False, "error": error}
    launcher = config["launcher"]
    if launcher["mode"] != "command":
        return {"ok": False, "error": "This launcher opens through a browser URL."}
    return run_open_command(launcher["name"],
                            [arg.replace("{path}", real) for arg in launcher["command"]])


def launch_agent(target, roots, config, session_id=None):
    agent = config["agent"]
    if not agent or agent["mode"] != "command":
        return {"ok": False, "error": "No agent command is configured."}
    real, error = checked_path(target, roots)
    if error:
        return {"ok": False, "error": error}
    if not session_id:  # the pinned session when the sidebar names one, else the latest
        session = latest_sessions([target], agent).get(target)
        if not session:
            return {"ok": False, "error": f"No {agent['name']} session found for {real}."}
        session_id = session["id"]
    if not re.fullmatch(r"[A-Za-z0-9._-]+", session_id):
        return {"ok": False, "error": "Invalid session id."}
    return run_open_command(agent["name"], [
        arg.replace("{session}", session_id).replace("{path}", real)
        for arg in agent["command"]])


def has_tracked_changes(work_dir):
    # Uncommitted edits to tracked files — what actually blocks a branch switch.
    # Untracked files (node_modules, build output, .claude/) are carried across a
    # switch, so they don't count here.
    return bool(git(work_dir, "status", "--porcelain", "--untracked-files=no").strip())


def open_in_main(repo, branch, roots):
    """Check the branch out in the main clone (for heavy dev / running the server
    where build caches and node_modules live) instead of an isolated worktree.
    Guarded: won't switch a main clone with uncommitted work. If the branch is
    already checked out in a linked worktree, open that checkout instead."""
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    branch, error = validate_branch(branch)
    if error:
        return {"ok": False, "error": error}
    here = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
    if here:  # already checked out somewhere — open it instead of moving/removing it
        return {"ok": True, "path": here["path"], "workspace": here.get("workspace"), "moved": False}
    try:
        main_dirty = has_tracked_changes(clone)
    except GitError as e:
        return {"ok": False, "error": f"Could not verify the main clone's status: {e}"}
    if main_dirty:
        return {"ok": False, "error": "Your main clone has uncommitted changes. Commit or stash them first."}
    try_git(clone, "fetch", "origin", branch)  # best effort so the ref is present
    try:
        local_exists = git_ref_exists(clone, f"refs/heads/{branch}")
        remote_exists = git_ref_exists(clone, f"refs/remotes/origin/{branch}")
    except GitError as e:
        return {"ok": False, "error": f"Could not inspect local branches: {e}"}
    if local_exists:
        args = ("switch", branch)
    elif remote_exists:
        args = ("switch", "-c", branch, "--track", f"origin/{branch}")
    else:
        return {"ok": False, "error": f"Branch {branch} was not found locally or on origin."}
    error = mutate_git(clone, *args, timeout=60)
    if error and try_git(clone, "branch", "--show-current").strip() != branch:
        return {"ok": False, "error": error}
    return {"ok": True, "path": clone, "workspace": workspace_in(clone), "moved": True}


def open_default_in_main(repo, roots):
    """Open the main clone on its default branch (develop/main) for a blank slate —
    no named branch yet. Resolves the default branch, then reuses open_in_main to
    switch and open it (a no-op switch when the clone is already on it)."""
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    return open_in_main(repo, default_branch(clone), roots)


def move_to_main(repo, branch, roots):
    """Move a branch out of its linked worktree and into the main clone: remove the
    worktree, then switch the main clone to the branch. Guarded on both ends — won't
    switch a dirty main clone, and lets `git worktree remove` refuse a dirty worktree
    (its uncommitted/untracked work would otherwise be lost) rather than forcing it."""
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    branch, error = validate_branch(branch)
    if error:
        return {"ok": False, "error": error}
    here = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
    if not here:
        return {"ok": False, "error": f"Branch {branch} isn't checked out in a worktree."}
    if here["main"]:  # already the main clone's checkout — nothing to move
        return {"ok": True, "path": here["path"], "workspace": here.get("workspace"), "moved": False}
    try:
        if has_tracked_changes(clone):
            return {"ok": False, "error": "Your main clone has uncommitted changes. Commit or stash them first."}
    except GitError as e:
        return {"ok": False, "error": f"Could not verify the main clone's status: {e}"}
    error = mutate_git(clone, "worktree", "remove", here["path"], timeout=60)
    if error:
        return {"ok": False, "error": "Couldn't remove the worktree — commit or stash its "
                f"changes first, then retry.\n{error}"}
    error = mutate_git(clone, "switch", branch, timeout=60)
    if error and try_git(clone, "branch", "--show-current").strip() != branch:
        return {"ok": False, "error": error}
    return {"ok": True, "path": clone, "workspace": workspace_in(clone), "moved": True}


def move_to_worktree(repo, branch, roots):
    """Create a worktree for the branch and open it — the opposite of move_to_main.
    If the branch is currently the main clone's checkout, switch the main clone to the
    default branch first (guarded on uncommitted work) so the worktree can claim it."""
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    branch, error = validate_branch(branch)
    if error:
        return {"ok": False, "error": error}
    here = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
    if here and not here["main"]:  # already in a linked worktree → just open it
        return {"ok": True, "path": here["path"], "workspace": here.get("workspace"), "created": False}
    if here and here["main"]:  # occupying the main clone — free it before the worktree can take it
        base = default_branch(clone)
        if base == branch:
            return {"ok": False, "error": f"{branch} is your default branch — it can't move to a worktree."}
        try:
            if has_tracked_changes(clone):
                return {"ok": False, "error": "Your main clone has uncommitted changes. Commit or stash them first."}
        except GitError as e:
            return {"ok": False, "error": f"Could not verify the main clone's status: {e}"}
        try_git(clone, "fetch", "origin", base)
        error = mutate_git(clone, "switch", base, timeout=60)
        if error and try_git(clone, "branch", "--show-current").strip() != base:
            return {"ok": False, "error": error}
    try_git(clone, "fetch", "origin", branch)  # best effort so the ref is present
    try:
        local_exists = git_ref_exists(clone, f"refs/heads/{branch}")
        remote_exists = git_ref_exists(clone, f"refs/remotes/origin/{branch}")
    except GitError as e:
        return {"ok": False, "error": f"Could not inspect branches: {e}"}
    if not local_exists and not remote_exists:
        return {"ok": False, "error": f"Branch {branch} was not found locally or on origin."}
    path = worktree_path(clone, branch)
    args = ("worktree", "add", path, branch) if local_exists else (
        "worktree", "add", "--track", "-b", branch, path, f"origin/{branch}")
    error = mutate_git(clone, *args, timeout=120)
    if error:
        existing = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
        if existing and not existing["main"]:
            return {"ok": True, "path": existing["path"], "workspace": existing.get("workspace"), "created": True}
        return {"ok": False, "error": error}
    return {"ok": True, "path": path, "workspace": workspace_in(path), "created": True}


def new_branch_in_main(repo, branch, roots):
    """Create a new branch off the default branch, checked out in the main clone
    (instead of a worktree). Guarded: won't switch a main clone with uncommitted work."""
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    branch, error = validate_branch(branch)
    if error:
        return {"ok": False, "error": error}
    existing = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
    if existing:  # already checked out somewhere → just open it
        return {"ok": True, "path": existing["path"], "workspace": existing.get("workspace"), "created": False}
    try:
        main_dirty = has_tracked_changes(clone)
    except GitError as e:
        return {"ok": False, "error": f"Could not verify the main clone's status: {e}"}
    if main_dirty:
        return {"ok": False, "error": "Your main clone has uncommitted changes. Commit or stash them first."}
    base = default_branch(clone)
    try_git(clone, "fetch", "origin", base)  # best effort: branch off a fresh base
    try:
        local_exists = git_ref_exists(clone, f"refs/heads/{branch}")
        if not local_exists and not git_ref_exists(clone, f"refs/remotes/origin/{base}"):
            return {"ok": False, "error": f"Default branch origin/{base} was not found locally."}
    except GitError as e:
        return {"ok": False, "error": f"Could not inspect local branches: {e}"}
    args = ("switch", branch) if local_exists else ("switch", "-c", branch, f"origin/{base}")
    error = mutate_git(clone, *args, timeout=60)
    if error and try_git(clone, "branch", "--show-current").strip() != branch:
        return {"ok": False, "error": error}
    return {"ok": True, "path": clone, "workspace": workspace_in(clone), "created": not local_exists}


def managed_worktree_dir(clone):
    return os.path.join(clone, ".claude", "worktrees")


def is_managed(clone, path):
    try:
        root = os.path.realpath(managed_worktree_dir(clone))
        return os.path.commonpath((os.path.realpath(path), root)) == root
    except ValueError:
        return False


def cleanup_worktree(repo, branch, roots, merged_head=None, path=None):
    """Remove a merged PR's worktree under .claude/worktrees/ and prune its local branch.

    `git worktree remove` without --force refuses a worktree with modified or untracked
    files, so nothing uncommitted is lost. The branch is deleted only when Git agrees
    it is merged, or its tip is the head GitHub merged (a squash merge leaves the
    branch looking unmerged). Session files are left alone."""
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    if not path:
        here = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
        if not here:
            return {"ok": False, "error": f"Branch {branch} isn't checked out in a worktree."}
        if here["main"]:
            return {"ok": False, "error": "That branch is your main clone's checkout; Shipyard only "
                    "cleans up worktrees under .claude/worktrees/."}
        path = here["path"]
    if not is_managed(clone, path):
        return {"ok": False, "error": "Shipyard only cleans up worktrees under .claude/worktrees/."}
    review = re.fullmatch(r"review-(\d+)", os.path.basename(path))
    if review and has_review_commits(clone, path, review.group(1)):
        return {"ok": False, "error": "The review worktree has commits of its own; move them to a branch first."}
    error = mutate_git(clone, "worktree", "remove", path, timeout=60)
    if error:
        return {"ok": False, "error": "Couldn't remove the worktree — it has uncommitted or "
                f"untracked files. Commit, stash, or delete them first.\n{error}"}
    if review:
        mutate_git(clone, "update-ref", "-d", review_ref(review.group(1)), timeout=30)
    deleted = False
    if branch:
        try:
            exists = git_ref_exists(clone, f"refs/heads/{branch}")
        except GitError:
            exists = False
        if exists:
            if not mutate_git(clone, "branch", "-d", branch, timeout=30):
                deleted = True
            elif merged_head and try_git(clone, "rev-parse", f"refs/heads/{branch}").strip() == merged_head:
                deleted = not mutate_git(clone, "branch", "-D", branch, timeout=30)
    return {"ok": True, "removed": path, "branchDeleted": deleted}


def review_ref(number):
    return f"refs/shipyard/review/{int(number)}"


def has_review_commits(clone, path, number):
    """Commits made in a review worktree that aren't part of the PR as last fetched.
    A detached checkout would leave them reachable only through the reflog."""
    known = try_git(clone, "rev-parse", "--verify", "--quiet", review_ref(number)).strip()
    here = try_git(path, "rev-parse", "HEAD").strip()
    if not known or not here or here == known:
        return False
    return run_git(clone, "merge-base", "--is-ancestor", here, known).returncode != 0


def review_worktree(repo, number, head, roots):
    """A detached worktree at a PR's head, for reviewing someone else's PR:
    <clone>/.claude/worktrees/review-<number>. Reused (and moved to the new head) when
    it already exists; refuses to move one with local edits or commits of its own."""
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    path = os.path.join(managed_worktree_dir(clone), f"review-{int(number)}")
    if os.path.isdir(path):
        try:
            if has_tracked_changes(path):
                return {"ok": False, "error": f"The review worktree {path} has local edits."}
        except GitError as e:
            return {"ok": False, "error": str(e)}
        if has_review_commits(clone, path, number):
            return {"ok": False, "error": f"The review worktree {path} has commits of its own; "
                    "move them to a branch first."}
    error = mutate_git(clone, "fetch", "origin", f"+refs/pull/{int(number)}/head:{review_ref(number)}", timeout=120)
    if error:
        return {"ok": False, "error": f"Couldn't fetch the PR: {error}"}
    target = head or try_git(clone, "rev-parse", review_ref(number)).strip()
    if os.path.isdir(path):
        if try_git(path, "rev-parse", "HEAD").strip() == target:
            return {"ok": True, "path": path}
        error = mutate_git(path, "checkout", "--detach", target, timeout=60)
    else:
        error = mutate_git(clone, "worktree", "add", "--detach", path, target, timeout=120)
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "path": path}
