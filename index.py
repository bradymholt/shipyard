#!/usr/bin/env python3
"""Local companion for Shipyard.

Serves the dashboard AND a /worktrees.json endpoint so the page can offer
"Open in VS Code" links for branches you have checked out locally. It
reports, for every git worktree under the given root(s):

    { "repo": "owner/name", "branch": "...", "path": "/abs/path", "workspace": "…|null" }

Run it from the dashboard directory and open the printed URL:

    python3 index.py                 # scan the folders set in the config file
    python3 index.py ~/dev ~/work    # or override the roots on the command line

Binds to localhost only. Nothing it reads or serves is written to the repo.

shipyard.config.json (next to this file) sets the folders to scan and
controls how the page opens VS Code / prefills New task branch names:

    {
      "roots": ["~/dev"],      // folders to scan for git clones (required,
                               // unless roots are passed on the command line)
      "vscodeOpen": "cli",     // "cli" runs the code CLI; "scheme" (default)
                               // uses vscode://file links
      "codeCliArgs": ["--disable-extension", "github.copilot-chat"],
      "branchPrefix": "brady/" // New task branch-name prefix; empty (default)
                               // falls back to your signed-in GitHub username
    }

"cli" falls back to "scheme" when the code CLI is not on PATH.
"""
import functools
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 4321
CONFIG_FILE = "shipyard.config.json"
DEFAULT_CONFIG = {"roots": [], "vscodeOpen": "scheme", "codeCliArgs": [], "branchPrefix": ""}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILE)
    try:
        with open(path) as fh:
            cfg.update(json.load(fh))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"Warning: ignoring unreadable {CONFIG_FILE}: {e}")
    if cfg["vscodeOpen"] == "cli" and not shutil.which("code"):
        print("Warning: vscodeOpen is 'cli' but the code CLI is not on PATH; using vscode:// links.")
        cfg["vscodeOpen"] = "scheme"
    return cfg


def parse_args(argv):
    port, roots = DEFAULT_PORT, []
    it = iter(argv)
    for a in it:
        if a in ("--port", "-p"):
            port = int(next(it))
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])
        else:
            roots.append(os.path.abspath(os.path.expanduser(a)))
    return port, roots


def git(repo, *args):
    try:
        r = subprocess.run(["git", "-C", repo, *args],
                           capture_output=True, text=True, timeout=10)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def parse_origin(url):
    m = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?/?$", url.strip())
    return m.group(1) if m else None


def workspace_in(path):
    files = sorted(Path(path).glob("*.code-workspace"))
    return str(files[0]) if files else None


def worktrees_for_repo(repo_dir):
    repo = parse_origin(git(repo_dir, "config", "--get", "remote.origin.url"))
    out, cur = [], {}
    for line in git(repo_dir, "worktree", "list", "--porcelain").splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
        elif line == "" and cur.get("path") and cur.get("branch"):
            is_main = os.path.realpath(cur["path"]) == os.path.realpath(repo_dir)
            # Only the main clone's dirty state is used (to block "open in main" when
            # it can't switch), so skip a `git status` per linked worktree — that keeps
            # polling cheap. "dirty" = uncommitted edits to tracked files; untracked
            # files (node_modules, build output) carry across a switch, so they don't count.
            dirty = has_tracked_changes(cur["path"]) if is_main else False
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
        if repo_dir in seen:
            continue
        seen.add(repo_dir)
        out.extend(worktrees_for_repo(repo_dir))
    return out


def find_clone(repo, roots):
    for repo_dir in clone_dirs(roots):
        if parse_origin(git(repo_dir, "config", "--get", "remote.origin.url")) == repo:
            return repo_dir
    return None


def default_branch(clone):
    ref = git(clone, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
    prefix = "refs/remotes/origin/"
    if ref.startswith(prefix):
        return ref[len(prefix):]
    for b in ("main", "master"):
        if git(clone, "rev-parse", "--verify", f"origin/{b}").strip():
            return b
    return "main"


def new_task_worktree(repo, branch, roots):
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    branch = branch.strip()
    if not branch:
        return {"ok": False, "error": "Branch name is required."}
    # Idempotent: if the branch is already checked out somewhere, open that.
    existing = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
    if existing:
        return {"ok": True, "path": existing["path"], "workspace": existing.get("workspace"), "created": False}
    base = default_branch(clone)
    git(clone, "fetch", "origin", base)  # best effort: branch off a fresh base
    path = os.path.join(clone, ".claude", "worktrees", re.sub(r"[^\w.-]", "-", branch))
    add = lambda *a: subprocess.run(["git", "-C", clone, "worktree", "add", *a],
                                    capture_output=True, text=True, timeout=120)
    r = add("-b", branch, path, f"origin/{base}")
    if r.returncode != 0:  # branch may already exist locally → check it out instead
        r = add(path, branch)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "git worktree add failed").strip()}
    return {"ok": True, "path": path, "workspace": workspace_in(path), "created": True, "branch": branch}


def open_vscode(target, roots, config):
    real = os.path.realpath(target)
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        return {"ok": False, "error": "Path is outside the configured roots."}
    if not os.path.exists(real):
        return {"ok": False, "error": f"Path does not exist: {real}"}
    try:
        r = subprocess.run(["code", *config.get("codeCliArgs", []), real],
                           capture_output=True, text=True, timeout=30)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "code CLI failed").strip()}
    return {"ok": True}


def has_tracked_changes(work_dir):
    # Uncommitted edits to tracked files — what actually blocks a branch switch.
    # Untracked files (node_modules, build output, .claude/) are carried across a
    # switch, so they don't count here.
    return bool(git(work_dir, "status", "--porcelain", "--untracked-files=no").strip())


def has_local_work(work_dir):
    # Tracked edits OR new (non-ignored) untracked files — work that'd be lost if
    # this checkout is discarded. Ignored files (node_modules, build) don't count.
    return bool(git(work_dir, "status", "--porcelain").strip())


def open_in_main(repo, branch, roots):
    """Check the branch out in the main clone (for heavy dev / running the server
    where build caches and node_modules live) instead of an isolated worktree.
    Guarded: won't switch a main clone with uncommitted work, and won't discard a
    worktree that has uncommitted work."""
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    branch = branch.strip()
    if not branch:
        return {"ok": False, "error": "Branch name is required."}
    here = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
    if here and here.get("main"):  # already checked out in the main clone
        return {"ok": True, "path": clone, "workspace": workspace_in(clone), "moved": False}
    if has_tracked_changes(clone):
        return {"ok": False, "error": "Your main clone has uncommitted changes. Commit or stash them first."}
    if here:  # branch lives in a linked worktree
        if has_local_work(here["path"]):
            return {"ok": False, "error": f"The worktree for {branch} has uncommitted changes. Commit or stash them first."}
        # --force clears ignored build artifacts (node_modules etc.); the branch's commits are kept.
        r = subprocess.run(["git", "-C", clone, "worktree", "remove", "--force", here["path"]],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or "Couldn't remove the worktree.").strip()}
    git(clone, "fetch", "origin", branch)  # best effort so the ref is present
    sw = lambda *a: subprocess.run(["git", "-C", clone, "switch", *a], capture_output=True, text=True, timeout=60)
    r = sw(branch)
    if r.returncode != 0:  # no local branch yet → create one tracking the remote
        r = sw("-c", branch, "--track", f"origin/{branch}")
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "git switch failed").strip()}
    return {"ok": True, "path": clone, "workspace": workspace_in(clone), "moved": True}


def new_branch_in_main(repo, branch, roots):
    """Create a new branch off the default branch, checked out in the main clone
    (instead of a worktree). Guarded: won't switch a main clone with uncommitted work."""
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    branch = branch.strip()
    if not branch:
        return {"ok": False, "error": "Branch name is required."}
    existing = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
    if existing:  # already checked out somewhere → just open it
        return {"ok": True, "path": existing["path"], "workspace": existing.get("workspace"), "created": False}
    if has_tracked_changes(clone):
        return {"ok": False, "error": "Your main clone has uncommitted changes. Commit or stash them first."}
    base = default_branch(clone)
    git(clone, "fetch", "origin", base)  # best effort: branch off a fresh base
    sw = lambda *a: subprocess.run(["git", "-C", clone, "switch", *a], capture_output=True, text=True, timeout=60)
    r = sw("-c", branch, f"origin/{base}")
    if r.returncode != 0:  # branch may already exist locally → check it out instead
        r = sw(branch)
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "git switch failed").strip()}
    return {"ok": True, "path": clone, "workspace": workspace_in(clone), "created": True}


class Handler(http.server.SimpleHTTPRequestHandler):
    roots = []
    config = DEFAULT_CONFIG

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/worktrees.json":
            return self._json(discover(self.roots))
        if path == "/config.json":
            return self._json({"vscodeOpen": self.config["vscodeOpen"],
                               "branchPrefix": self.config["branchPrefix"]})
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
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
        if parsed.path == "/open-vscode":
            target = (q.get("path") or [""])[0]
            result = open_vscode(target, self.roots, self.config)
            return self._json(result, 200 if result.get("ok") else 500)
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_dev_reloader(paths):
    """With SHIPYARD_DEV_RELOAD set, exit when a watched file changes so a
    process supervisor (e.g. a KeepAlive LaunchAgent) restarts with new code.
    No-op otherwise, so plain `python3 index.py` is unaffected."""
    if not os.environ.get("SHIPYARD_DEV_RELOAD"):
        return
    mtimes = {p: os.path.getmtime(p) for p in paths if os.path.exists(p)}

    def watch():
        while True:
            time.sleep(1)
            for p in paths:
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if mtimes.get(p) != m:
                    print(f"{os.path.basename(p)} changed — exiting for reload.", flush=True)
                    os._exit(0)

    threading.Thread(target=watch, daemon=True).start()


def main():
    port, cli_roots = parse_args(sys.argv[1:])
    config = load_config()
    # Roots come from the command line if given, otherwise the "roots" config key.
    cfg_roots = config.get("roots") or []
    if isinstance(cfg_roots, str):
        cfg_roots = [cfg_roots]
    roots = cli_roots or [os.path.abspath(os.path.expanduser(r)) for r in cfg_roots]
    if not roots:
        sys.exit(f'No folders to scan. Set "roots" in {CONFIG_FILE} (e.g. ["~/dev"]) '
                 f'or pass them on the command line: python3 index.py ~/dev')
    here = os.path.dirname(os.path.abspath(__file__))
    start_dev_reloader([os.path.abspath(__file__), os.path.join(here, CONFIG_FILE)])
    Handler.roots = roots
    Handler.config = config
    httpd = http.server.HTTPServer(("127.0.0.1", port),
                                   functools.partial(Handler, directory=here))
    print(f"Shipyard companion → http://localhost:{port}")
    print(f"Scanning worktrees under: {', '.join(roots)}")
    print(f"VS Code opens via: {'code CLI' if Handler.config['vscodeOpen'] == 'cli' else 'vscode:// links'}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
