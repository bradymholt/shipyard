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


def session_id_for(path):
    # Claude stores per-project sessions under a dir name that is the abs path
    # with every "/" and "." flattened to "-"; newest .jsonl is the live session.
    enc = re.sub(r"[/.]", "-", path)
    proj = Path.home() / ".claude" / "projects" / enc
    if not proj.is_dir():
        return None
    sessions = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return sessions[0].stem if sessions else None


def worktrees_for_repo(repo_dir):
    repo = parse_origin(git(repo_dir, "config", "--get", "remote.origin.url"))
    out, cur = [], {}
    for line in git(repo_dir, "worktree", "list", "--porcelain").splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {"path": line[9:]}
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
        elif line == "" and cur.get("path") and cur.get("branch"):
            out.append({"repo": repo, "branch": cur["branch"], "path": cur["path"],
                        "sessionId": session_id_for(cur["path"])})
            cur = {}
    return out


def discover(roots):
    out, seen = [], set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.listdir(root)):
            repo_dir = os.path.join(root, entry)
            # Only main clones (a linked worktree has a .git *file*, not dir);
            # `git worktree list` from the clone already enumerates them all.
            if repo_dir in seen or not os.path.isdir(os.path.join(repo_dir, ".git")):
                continue
            seen.add(repo_dir)
            out.extend(worktrees_for_repo(repo_dir))
    return out


class Handler(http.server.SimpleHTTPRequestHandler):
    roots = []

    def do_GET(self):
        if self.path.split("?")[0] == "/worktrees.json":
            body = json.dumps(discover(self.roots)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

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
