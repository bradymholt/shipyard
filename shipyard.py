#!/usr/bin/env python3
"""Local companion for Shipyard.

Serves the dashboard AND a /worktrees.json endpoint so the page can offer
links that open branches in your configured app or IDE. It
reports, for every git worktree under the given root(s):

    { "repo": "owner/name", "branch": "...", "path": "/abs/path", "workspace": "…|null" }

Run it from the dashboard directory and open the printed URL:

    python3 shipyard.py                 # scan the folders set in the config file
    python3 shipyard.py ~/dev ~/work    # or override the roots on the command line

Binds to localhost only. Discovery is read-only; branch actions you choose can
change local Git state.

shipyard.config.json (next to this file) sets the folders to scan, the app
launcher, and the prefix used for new branch names:

    {
      "roots": ["~/dev"],      // folders to scan for git clones (required,
                               // unless roots are passed on the command line)
      "launcher": {
        "name": "VS Code",
        "mode": "url",
        "target": "workspace",
        "url": "vscode://file/{path}",
        "command": ["code", "{path}"]
      },
      "agent": {               // resume a coding agent's latest session in a
        "name": "Claude Code", // checkout; set to null to turn it off
        "sessions": "claude-code",   // or "codex"
        "mode": "url",
        "url": "claude://resume?session={session}",
        "command": ["open", "claude://resume?session={session}"]
      }
    }
"""
import argparse
import concurrent.futures
import functools
import hashlib
import http.server
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 4321
CONFIG_FILE = "shipyard.config.json"
DEFAULT_LAUNCHER = {
    "name": "VS Code",
    "mode": "url",
    "target": "workspace",
    "url": "vscode://file/{path}",
    "command": ["code", "{path}"],
}
DEFAULT_AGENT = {
    "name": "Claude Code",
    "sessions": "claude-code",
    "mode": "url",
    "url": "claude://resume?session={session}",
    "command": ["open", "claude://resume?session={session}"],
}
DEFAULT_CONFIG = {"roots": [], "launcher": DEFAULT_LAUNCHER, "agent": DEFAULT_AGENT}


def validate_open_settings(label, settings, placeholder):
    if not isinstance(settings["name"], str) or not settings["name"].strip():
        raise ValueError(f'{label} "name" must be a non-empty string')
    if settings["mode"] not in ("url", "command"):
        raise ValueError(f'{label} "mode" must be either "url" or "command"')
    if not isinstance(settings["url"], str):
        raise ValueError(f'{label} "url" must be a string')
    if (not isinstance(settings["command"], list) or not settings["command"] or
            any(not isinstance(arg, str) for arg in settings["command"])):
        raise ValueError(f'{label} "command" must be a non-empty array of strings')
    if settings["mode"] == "url":
        has_placeholder = placeholder in settings["url"]
        scheme = urlparse(settings["url"].replace(placeholder, "x")).scheme.lower()
        if not scheme or scheme in ("data", "javascript"):
            raise ValueError(f'{label} "url" must use a safe URL scheme')
    else:
        has_placeholder = any(placeholder in arg for arg in settings["command"])
    if not has_placeholder:
        raise ValueError(f'{label} {settings["mode"]!r} must include a "{placeholder}" placeholder')


def validate_agent(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError('"agent" must be an object or null')
    unknown = sorted(set(raw) - set(DEFAULT_AGENT))
    if unknown:
        raise ValueError(f"unknown agent setting(s): {', '.join(unknown)}")
    agent = {**DEFAULT_AGENT, **raw}
    if agent["sessions"] not in SESSION_FINDERS:
        raise ValueError(f'agent "sessions" must be one of: {", ".join(SESSION_FINDERS)}')
    validate_open_settings("agent", agent, "{session}")
    return agent


def validate_config(raw):
    if not isinstance(raw, dict):
        raise ValueError("the top-level value must be a JSON object")
    unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")

    roots = raw.get("roots", DEFAULT_CONFIG["roots"])
    if (not isinstance(roots, list) or
            any(not isinstance(root, str) or not root.strip() for root in roots)):
        raise ValueError('"roots" must be an array of non-empty strings')
    launcher_raw = raw.get("launcher", {})
    if not isinstance(launcher_raw, dict):
        raise ValueError('"launcher" must be an object')
    launcher_unknown = sorted(set(launcher_raw) - set(DEFAULT_LAUNCHER))
    if launcher_unknown:
        raise ValueError(f"unknown launcher setting(s): {', '.join(launcher_unknown)}")
    launcher = {**DEFAULT_LAUNCHER, **launcher_raw}
    if launcher["target"] not in ("folder", "workspace"):
        raise ValueError('launcher "target" must be either "folder" or "workspace"')
    validate_open_settings("launcher", launcher, "{path}")

    agent = validate_agent(raw.get("agent", DEFAULT_AGENT))
    return {"roots": roots, "launcher": launcher, "agent": agent}


def load_config():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILE)
    try:
        with open(path) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raw = {}
    except (OSError, json.JSONDecodeError) as e:
        sys.exit(f"Could not read {CONFIG_FILE}: {e}")
    try:
        cfg = validate_config(raw)
    except ValueError as e:
        sys.exit(f"Invalid {CONFIG_FILE}: {e}")
    for label in ("launcher", "agent"):
        settings = cfg[label]
        if settings and settings["mode"] == "command" and not shutil.which(settings["command"][0]):
            print(f"Warning: {label} command {settings['command'][0]!r} is not on PATH.")
    return cfg


def parse_args(argv):
    def port_number(value):
        try:
            port = int(value)
        except ValueError as e:
            raise argparse.ArgumentTypeError("must be an integer") from e
        if not 1 <= port <= 65535:
            raise argparse.ArgumentTypeError("must be between 1 and 65535")
        return port

    parser = argparse.ArgumentParser(
        description="Serve Shipyard and discover Git worktrees under one or more folders.")
    parser.add_argument("roots", nargs="*", metavar="ROOT",
                        help=f"folder containing Git clones (overrides {CONFIG_FILE})")
    parser.add_argument("-p", "--port", type=port_number, default=DEFAULT_PORT,
                        help=f"localhost port (default: {DEFAULT_PORT})")
    args = parser.parse_args(argv)
    return args.port, args.roots


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


def newest_session(files):
    """(session id, mtime) of the most recently written transcript, or None."""
    best = None
    for f in files:
        try:
            mtime = os.path.getmtime(f)
        except OSError:
            continue
        if best is None or mtime > best[1]:
            best = (f, mtime)
    return best


def claude_code_sessions(paths):
    # Claude Code keeps each session as ~/.claude/projects/<cwd, non-alphanumerics
    # replaced by "-">/<session id>.jsonl.
    projects = Path.home() / ".claude" / "projects"
    found = {}
    for path in paths:
        dirs = {projects / re.sub(r"[^A-Za-z0-9]", "-", p) for p in (path, os.path.realpath(path))}
        best = newest_session(f for d in dirs for f in d.glob("*.jsonl"))
        if best:
            found[path] = {"id": Path(best[0]).stem, "updated": int(best[1])}
    return found


_codex_meta = {}  # rollout file -> (cwd, session id); a rollout's first line never changes


def codex_sessions(paths):
    # Codex writes ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl, whose first line is
    # a session_meta record carrying the session id and working directory.
    wanted = {os.path.realpath(p): p for p in paths}
    by_path = {}
    for f in (Path.home() / ".codex" / "sessions").glob("*/*/*/rollout-*.jsonl"):
        if f not in _codex_meta:
            try:
                with f.open() as fh:
                    payload = json.loads(fh.readline()).get("payload") or {}
            except (OSError, ValueError, AttributeError):
                continue
            _codex_meta[f] = (payload.get("cwd"), payload.get("id"))
        cwd, session = _codex_meta[f]
        path = wanted.get(os.path.realpath(cwd)) if cwd and session else None
        if path:
            by_path.setdefault(path, []).append((f, session))
    found = {}
    for path, entries in by_path.items():
        best = newest_session(f for f, _ in entries)
        if best:
            session = next(s for f, s in entries if f == best[0])
            found[path] = {"id": session, "updated": int(best[1])}
    return found


SESSION_FINDERS = {"claude-code": claude_code_sessions, "codex": codex_sessions}


def latest_sessions(paths, agent):
    if not agent or not paths:
        return {}
    try:
        return SESSION_FINDERS[agent["sessions"]](paths)
    except OSError as e:
        print(f"Warning: could not look up {agent['name']} sessions: {e}")
        return {}


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


def launch_agent(target, roots, config):
    agent = config["agent"]
    if not agent or agent["mode"] != "command":
        return {"ok": False, "error": "No agent command is configured."}
    real, error = checked_path(target, roots)
    if error:
        return {"ok": False, "error": error}
    session = latest_sessions([target], agent).get(target)
    if not session:
        return {"ok": False, "error": f"No {agent['name']} session found for {real}."}
    return run_open_command(agent["name"], [
        arg.replace("{session}", session["id"]).replace("{path}", real)
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


class Handler(http.server.SimpleHTTPRequestHandler):
    roots = []
    config = DEFAULT_CONFIG
    session_token = ""
    STATIC_PATHS = {"/", "/index.html", "/favicon.svg", "/docs/list-view.png", "/docs/board-view.png"}

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _allowed_hosts(self):
        port = self.server.server_port
        hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
        if port == 80:
            hosts.update({"localhost", "127.0.0.1"})
        return hosts

    def _host_is_allowed(self):
        return self.headers.get("Host", "").lower() in self._allowed_hosts()

    def _mutation_is_authorized(self):
        host = self.headers.get("Host", "").lower()
        if not self._host_is_allowed():
            return False
        if self.headers.get("Origin", "").lower() != f"http://{host}":
            return False
        supplied = self.headers.get("X-Shipyard-Token", "")
        return bool(supplied and secrets.compare_digest(supplied, self.session_token))

    def _not_found(self):
        self.send_error(404)

    def do_GET(self):
        if not self._host_is_allowed():
            return self._json({"ok": False, "error": "Invalid Host header."}, 403)
        path = urlparse(self.path).path
        if path == "/worktrees.json":
            return self._json(discover(self.roots, self.config["agent"]))
        if path == "/config.json":
            return self._json({"launcher": self.config["launcher"],
                               "agent": self.config["agent"],
                               "companionToken": self.session_token})
        if path in self.STATIC_PATHS:
            return super().do_GET()
        return self._not_found()

    def do_HEAD(self):
        if not self._host_is_allowed():
            return self.send_error(403, "Invalid Host header")
        if urlparse(self.path).path in self.STATIC_PATHS:
            return super().do_HEAD()
        return self._not_found()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in {"/new-task", "/new-branch-main", "/open-in-main", "/move-to-main",
                               "/move-to-worktree", "/open-default-main", "/open-app", "/open-agent"}:
            return self._not_found()
        if not self._mutation_is_authorized():
            return self._json({"ok": False, "error": "Companion request was not authorized."}, 403)
        q = parse_qs(parsed.query)
        if parsed.path == "/new-task":
            repo = (q.get("repo") or [""])[0]
            branch = (q.get("branch") or [""])[0]
            result = new_task_worktree(repo, branch, self.roots)
            return self._json(result, 200 if result.get("ok") else 500)
        if parsed.path == "/new-branch-main":
            repo = (q.get("repo") or [""])[0]
            branch = (q.get("branch") or [""])[0]
            result = new_branch_in_main(repo, branch, self.roots)
            return self._json(result, 200 if result.get("ok") else 500)
        if parsed.path == "/open-in-main":
            repo = (q.get("repo") or [""])[0]
            branch = (q.get("branch") or [""])[0]
            result = open_in_main(repo, branch, self.roots)
            return self._json(result, 200 if result.get("ok") else 500)
        if parsed.path == "/move-to-main":
            repo = (q.get("repo") or [""])[0]
            branch = (q.get("branch") or [""])[0]
            result = move_to_main(repo, branch, self.roots)
            return self._json(result, 200 if result.get("ok") else 500)
        if parsed.path == "/move-to-worktree":
            repo = (q.get("repo") or [""])[0]
            branch = (q.get("branch") or [""])[0]
            result = move_to_worktree(repo, branch, self.roots)
            return self._json(result, 200 if result.get("ok") else 500)
        if parsed.path == "/open-default-main":
            repo = (q.get("repo") or [""])[0]
            result = open_default_in_main(repo, self.roots)
            return self._json(result, 200 if result.get("ok") else 500)
        if parsed.path == "/open-app":
            target = (q.get("path") or [""])[0]
            result = launch_app(target, self.roots, self.config)
            return self._json(result, 200 if result.get("ok") else 500)
        if parsed.path == "/open-agent":
            target = (q.get("path") or [""])[0]
            result = launch_agent(target, self.roots, self.config)
            return self._json(result, 200 if result.get("ok") else 500)
        return self._not_found()

    def log_message(self, *args):
        pass


def main():
    port, cli_roots = parse_args(sys.argv[1:])
    config = load_config()
    # Roots come from the command line if given, otherwise the "roots" config key.
    cfg_roots = config.get("roots") or []
    roots = normalize_roots(cli_roots or cfg_roots)
    if not roots:
        sys.exit(f'No folders to scan. Set "roots" in {CONFIG_FILE} (e.g. ["~/dev"]) '
                 f'or pass them on the command line: python3 shipyard.py ~/dev')
    warn_unavailable_roots(roots)
    here = os.path.dirname(os.path.abspath(__file__))
    Handler.roots = roots
    Handler.config = config
    Handler.session_token = secrets.token_urlsafe(32)
    httpd = http.server.HTTPServer(("127.0.0.1", port),
                                   functools.partial(Handler, directory=here))
    print(f"Shipyard companion → http://localhost:{port}")
    print(f"Scanning worktrees under: {', '.join(roots)}")
    launcher = Handler.config["launcher"]
    print(f"Opens in: {launcher['name']} via {launcher['mode']}")
    agent = Handler.config["agent"]
    if agent:
        print(f"Resumes sessions in: {agent['name']} via {agent['mode']}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
