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
      }
    }
"""
import argparse
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
DEFAULT_CONFIG = {"roots": [], "launcher": DEFAULT_LAUNCHER}


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

    if not isinstance(launcher["name"], str) or not launcher["name"].strip():
        raise ValueError('launcher "name" must be a non-empty string')
    if launcher["mode"] not in ("url", "command"):
        raise ValueError('launcher "mode" must be either "url" or "command"')
    if launcher["target"] not in ("folder", "workspace"):
        raise ValueError('launcher "target" must be either "folder" or "workspace"')
    if not isinstance(launcher["url"], str):
        raise ValueError('launcher "url" must be a string')
    if (not isinstance(launcher["command"], list) or not launcher["command"] or
            any(not isinstance(arg, str) for arg in launcher["command"])):
        raise ValueError('launcher "command" must be a non-empty array of strings')
    if launcher["mode"] == "url":
        has_path = "{path}" in launcher["url"]
        scheme = urlparse(launcher["url"].replace("{path}", "path")).scheme.lower()
        if not scheme or scheme in ("data", "javascript"):
            raise ValueError('launcher "url" must use a safe URL scheme')
    else:
        has_path = any("{path}" in arg for arg in launcher["command"])
    if not has_path:
        raise ValueError(f'launcher {launcher["mode"]!r} must include a "{{path}}" placeholder')

    return {"roots": roots, "launcher": launcher}


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
    launcher = cfg["launcher"]
    if launcher["mode"] == "command" and not shutil.which(launcher["command"][0]):
        print(f"Warning: launcher command {launcher['command'][0]!r} is not on PATH.")
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
            # Dirty state gates the move/open actions: a dirty main clone can't switch
            # branches, and a dirty linked worktree can't be removed to relocate its
            # branch. "dirty" = uncommitted edits to tracked files; untracked files
            # (node_modules, build output) carry across a switch, so they don't count.
            try:
                dirty = has_tracked_changes(cur["path"])
            except GitError as e:
                print(f"Warning: could not verify status for {cur['path']}: {e}")
                dirty = True
            out.append({"repo": repo, "branch": cur["branch"], "path": cur["path"],
                        "workspace": workspace_in(cur["path"]), "main": is_main, "dirty": dirty})
            cur = {}
    return out


def clone_dirs(roots):
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            repo_dir = os.path.join(root, entry)
            # Only main clones (a linked worktree has a .git *file*, not dir).
            if os.path.isdir(os.path.join(repo_dir, ".git")):
                yield repo_dir


def discover(roots):
    out, seen = [], set()
    for repo_dir in clone_dirs(roots):
        repo_dir = os.path.realpath(repo_dir)
        if repo_dir in seen:
            continue
        seen.add(repo_dir)
        out.extend(worktrees_for_repo(repo_dir))
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


def launch_app(target, roots, config):
    real = os.path.realpath(target)
    def is_within(root):
        try:
            return os.path.commonpath((real, root)) == root
        except ValueError:
            return False
    if not any(is_within(root) for root in roots):
        return {"ok": False, "error": "Path is outside the configured roots."}
    if not os.path.exists(real):
        return {"ok": False, "error": f"Path does not exist: {real}"}
    launcher = config["launcher"]
    if launcher["mode"] != "command":
        return {"ok": False, "error": "This launcher opens through a browser URL."}
    command = [arg.replace("{path}", real) for arg in launcher["command"]]
    try:
        r = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or f"{launcher['name']} failed to open").strip()}
    return {"ok": True}


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
            return self._json(discover(self.roots))
        if path == "/config.json":
            return self._json({"launcher": self.config["launcher"],
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
                               "/open-default-main", "/open-app"}:
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
        if parsed.path == "/open-default-main":
            repo = (q.get("repo") or [""])[0]
            result = open_default_in_main(repo, self.roots)
            return self._json(result, 200 if result.get("ok") else 500)
        if parsed.path == "/open-app":
            target = (q.get("path") or [""])[0]
            result = launch_app(target, self.roots, self.config)
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
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
