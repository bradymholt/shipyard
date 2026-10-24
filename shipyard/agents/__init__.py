"""Coding-agent adapters.

Everything that talks to a coding agent goes through one small interface, so the rest
of the companion (rules, passes, the page) never knows which CLI is behind it:

    sessions(paths)            latest session per checkout   {path: {id, updated}}
    list_sessions(path)        every session in a checkout, newest first
    find_pr_session(...)       the session that opened a PR, for pinning
    user_messages(text)        the messages typed into a session, for its title and turns
    title(text)                the session's title in the agent's app, if recorded
    is_live(path, id)          whether the session is being written to right now
    discard(path, id)          delete a throwaway session Shipyard made for a question
    run(...)                   one headless turn: fresh, resume, or fork

Adapters are looked up by the config's agent "sessions" key.
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

# A session written to within this window is treated as open in the agent's app.
LIVE_WINDOW = 120
PASS_TIMEOUT = 900
# How every pass prompt Shipyard sends on your own PRs begins (shipyard/passes/*.md).
PASS_PROMPT_START = "Shipyard (the PR dashboard)"


class AgentError(RuntimeError):
    pass


def newest_session(files):
    """(session file, mtime) of the most recently written transcript, or None."""
    best = None
    for f in files:
        try:
            mtime = os.path.getmtime(f)
        except OSError:
            continue
        if best is None or mtime > best[1]:
            best = (f, mtime)
    return best


def read_text(path, limit=8_000_000):
    try:
        with open(path, errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def processes_in(name, cwd):
    """True when a process whose command starts with `name` has `cwd` as its directory."""
    want = os.path.realpath(cwd)
    if shutil.which("lsof"):
        try:
            r = subprocess.run(["lsof", "-a", "-d", "cwd", "-c", name, "-Fn"],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return any(line[1:] and os.path.realpath(line[1:]) == want
                   for line in r.stdout.splitlines() if line.startswith("n"))
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for pid in proc.iterdir():
        if not pid.name.isdigit():
            continue
        try:
            if not (pid / "comm").read_text().strip().startswith(name):
                continue
            if os.path.realpath(pid / "cwd") == want:
                return True
        except OSError:
            continue
    return False


def parse_json_answer(text):
    """The JSON object in an agent's final answer, tolerating code fences or prose."""
    if isinstance(text, dict):
        return text
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise AgentError("the agent's answer was not a JSON object")


class Adapter:
    key = ""
    process_name = ""
    can_fork = False

    def session_files(self, path):
        """[(session id, file)] for every session whose working directory is `path`."""
        raise NotImplementedError

    def sessions(self, paths):
        found = {}
        for path in paths:
            files = dict((f, sid) for sid, f in self.session_files(path))
            best = newest_session(files)
            if best:
                found[path] = {"id": files[best[0]], "updated": int(best[1])}
        return found

    def session_file(self, path, session_id):
        return next((f for sid, f in self.session_files(path) if sid == session_id), None)

    def list_sessions(self, path):
        out = []
        for sid, f in self.session_files(path):
            try:
                st = os.stat(f)
            except OSError:
                continue
            out.append({"id": sid, "updated": int(st.st_mtime)})
        return sorted(out, key=lambda s: -s["updated"])

    def user_messages(self, text):
        """The text of each message a person (or Shipyard) typed, oldest first."""
        raise NotImplementedError

    def title(self, text):
        """The session's title as the agent's own app shows it, when the transcript has one."""
        return None

    def find_pr_session(self, path, branch, pr_url, latest=True):
        """The session that opened a PR: the newest one whose transcript ran `gh pr create`
        and mentions the PR's URL (best) or its branch. Newest wins because a parent
        session that fans out into children is older than the child holding this PR."""
        best = {}
        newest = None
        for sid, f in self.session_files(path):
            text = read_text(f)
            if PASS_PROMPT_START in text and \
                    next(iter(self.user_messages(text)), "").startswith(PASS_PROMPT_START):
                continue
            try:
                mtime = os.path.getmtime(f)
            except OSError:
                continue
            if not newest or mtime > newest["mtime"]:
                newest = {"id": sid, "mtime": mtime}
            if "gh pr create" not in text:
                continue
            how = "pr-url" if pr_url and pr_url in text else "branch" if branch and branch in text else None
            if not how:
                continue
            rank = 2 if how == "pr-url" else 1
            if not best or (rank, mtime) > (best["rank"], best["mtime"]):
                best = {"id": sid, "how": how, "rank": rank, "mtime": mtime}
        if best:
            return {"id": best["id"], "how": best["how"]}
        return {"id": newest["id"], "how": "latest"} if newest and latest else None

    def discard(self, path, session_id):
        """Delete a session Shipyard created only to ask a question (a fork, or a fresh
        run nobody will continue), so it never shows up as your latest session or in
        the agent's session list. Returns True when a file was removed."""
        f = self.session_file(path, session_id)
        if not f:
            return False
        try:
            os.remove(f)
            return True
        except OSError:
            return False

    def is_live(self, path, session_id):
        f = self.session_file(path, session_id)
        try:
            if f and time.time() - os.path.getmtime(f) < LIVE_WINDOW:
                return True
        except OSError:
            pass
        return processes_in(self.process_name, path)

    def run(self, cwd, prompt, schema, mode="fresh", session_id=None, new_session_id=None, untrusted=False,
            name=None):
        """One headless turn. mode is "fresh", "resume" or "fork"; untrusted is set for
        someone else's PR, whose checkout may carry agent settings; name titles a session
        Shipyard keeps, where the agent supports it. Returns
        {"session_id": id of the session that holds the turn, "answer": dict}."""
        raise NotImplementedError

    def available(self):
        return bool(shutil.which(self.process_name))


from .claude_code import ClaudeCode  # noqa: E402
from .codex import Codex  # noqa: E402

ADAPTERS = {"claude-code": ClaudeCode(), "codex": Codex()}


def adapter_for(agent):
    return ADAPTERS.get(agent["sessions"]) if agent else None


def latest_sessions(paths, agent):
    adapter = adapter_for(agent)
    if not adapter or not paths:
        return {}
    try:
        return adapter.sessions(paths)
    except OSError as e:
        print(f"Warning: could not look up {agent['name']} sessions: {e}")
        return {}
