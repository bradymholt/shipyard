#!/usr/bin/env python3
"""Local companion for the PR Review Dashboard.

Serves the dashboard AND a /worktrees.json endpoint so the page can offer
"Open in VS Code" / "Resume in Claude" links for branches you have checked
out locally. It reports, for every git worktree under the given root(s):

    { "repo": "owner/name", "branch": "...", "path": "/abs/path", "sessionId": "uuid|null" }

Run it from the dashboard directory and open the printed URL:

    python3 companion.py ~/dev
    python3 companion.py ~/dev ~/work --port 4321

Binds to localhost only. Nothing it reads or serves is written to the repo.
"""
import functools
import http.server
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 4321


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
    return port, roots or [os.path.expanduser("~/dev")]


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


def first_cwd(f):
    # Match sessions by the working dir recorded *inside* the transcript, not by
    # the project-dir name: cloud/desktop worktree sessions get filed under the
    # main repo's dir but their cwd still points at the worktree. cwd appears
    # within the first few entries, so stop as soon as we find it.
    try:
        with f.open() as fh:
            for _ in range(50):
                line = fh.readline()
                if not line:
                    break
                if '"cwd"' in line:
                    try:
                        cwd = json.loads(line).get("cwd")
                        if cwd:
                            return cwd
                    except Exception:
                        pass
    except Exception:
        pass
    return None


def session_index():
    root = Path.home() / ".claude" / "projects"
    idx = []
    if root.is_dir():
        for f in root.glob("*/*.jsonl"):
            cwd = first_cwd(f)
            if cwd:
                idx.append((cwd, f.stat().st_mtime, f.stem))
    return idx


def attach_sessions(worktrees):
    idx = session_index()
    # Assign each session to the worktree whose path is the longest prefix of the
    # session's cwd, so a worktree wins over the main checkout it lives inside.
    paths = sorted({w["path"] for w in worktrees}, key=len, reverse=True)
    best = {}  # worktree path -> (mtime, sessionId)
    for cwd, mtime, sid in idx:
        for p in paths:
            if cwd == p or cwd.startswith(p + "/"):
                if p not in best or mtime > best[p][0]:
                    best[p] = (mtime, sid)
                break
    for w in worktrees:
        w["sessionId"] = best.get(w["path"], (None, None))[1]
    return worktrees


def workspace_in(path):
    files = sorted(Path(path).glob("*.code-workspace"))
    return str(files[0]) if files else None


def worktrees_for_repo(repo_dir):
    repo = parse_origin(git(repo_dir, "config", "--get", "remote.origin.url"))
    main = os.path.realpath(repo_dir)
    out, cur = [], {}
    for line in git(repo_dir, "worktree", "list", "--porcelain").splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
        elif line == "" and cur.get("path") and cur.get("branch"):
            out.append({"repo": repo, "branch": cur["branch"], "path": cur["path"],
                        "linked": os.path.realpath(cur["path"]) != main,
                        "workspace": workspace_in(cur["path"])})
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
    return attach_sessions(out)


def find_clone(repo, roots):
    for repo_dir in clone_dirs(roots):
        if parse_origin(git(repo_dir, "config", "--get", "remote.origin.url")) == repo:
            return repo_dir
    return None


def create_worktree(repo, branch, roots):
    clone = find_clone(repo, roots)
    if not clone:
        return {"ok": False, "error": f"No local clone of {repo} found under the configured roots."}
    # Idempotent: if the branch is already checked out somewhere (a worktree, or
    # the main clone itself), open that instead of making a duplicate.
    existing = next((w for w in worktrees_for_repo(clone) if w["branch"] == branch), None)
    if existing:
        return {"ok": True, "path": existing["path"], "workspace": existing.get("workspace"), "created": False}
    path = os.path.join(clone, ".claude", "worktrees", re.sub(r"[^\w.-]", "-", branch))
    if os.path.isdir(path):
        return {"ok": True, "path": path, "workspace": workspace_in(path), "created": False}
    git(clone, "fetch", "origin", branch)  # best effort so the ref is present
    add = lambda *a: subprocess.run(["git", "-C", clone, "worktree", "add", *a],
                                    capture_output=True, text=True, timeout=120)
    r = add(path, branch)
    if r.returncode != 0:  # no local branch yet → create one tracking the remote
        r = add("--track", "-b", branch, path, f"origin/{branch}")
    if r.returncode != 0:
        return {"ok": False, "error": (r.stderr or "git worktree add failed").strip()}
    return {"ok": True, "path": path, "workspace": workspace_in(path), "created": True}


class Handler(http.server.SimpleHTTPRequestHandler):
    roots = []

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlparse(self.path).path == "/worktrees.json":
            return self._json(discover(self.roots))
        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/create-worktree":
            q = parse_qs(parsed.query)
            repo = (q.get("repo") or [""])[0]
            branch = (q.get("branch") or [""])[0]
            result = create_worktree(repo, branch, self.roots)
            return self._json(result, 200 if result.get("ok") else 500)
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


def main():
    port, roots = parse_args(sys.argv[1:])
    here = os.path.dirname(os.path.abspath(__file__))
    Handler.roots = roots
    httpd = http.server.HTTPServer(("127.0.0.1", port),
                                   functools.partial(Handler, directory=here))
    print(f"PR Review Dashboard companion → http://localhost:{port}")
    print(f"Scanning worktrees under: {', '.join(roots)}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
