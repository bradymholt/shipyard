import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import PASS_TIMEOUT, Adapter, AgentError, parse_json_answer

_meta = {}  # rollout file -> (cwd, session id); a rollout's first line never changes


class Codex(Adapter):
    key = "codex"
    process_name = "codex"
    # `codex exec resume` appends to the session; there is no headless fork that
    # leaves the original untouched, so question passes run as fresh sessions.
    can_fork = False

    def rollouts(self):
        # Codex writes ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl, whose first line is
        # a session_meta record carrying the session id and working directory.
        for f in (Path.home() / ".codex" / "sessions").glob("*/*/*/rollout-*.jsonl"):
            if f not in _meta:
                try:
                    with f.open() as fh:
                        payload = json.loads(fh.readline()).get("payload") or {}
                except (OSError, ValueError, AttributeError):
                    continue
                _meta[f] = (payload.get("cwd"), payload.get("id"))
            cwd, session = _meta[f]
            if cwd and session:
                yield f, cwd, session

    def session_files(self, path):
        want = os.path.realpath(path)
        return [(sid, f) for f, cwd, sid in self.rollouts() if os.path.realpath(cwd) == want]

    def sessions(self, paths):
        # One walk of the rollouts for every checkout rather than one per path.
        wanted = {os.path.realpath(p): p for p in paths}
        by_path = {}
        for f, cwd, sid in self.rollouts():
            path = wanted.get(os.path.realpath(cwd))
            if path:
                by_path.setdefault(path, {})[f] = sid
        found = {}
        for path, files in by_path.items():
            best = max(((os.path.getmtime(f), f) for f in files if f.exists()), default=None)
            if best:
                found[path] = {"id": files[best[1]], "updated": int(best[0])}
        return found

    def user_messages(self, text):
        out = []
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            payload = record.get("payload") or {}
            if record.get("type") != "response_item" or payload.get("role") != "user":
                continue
            content = " ".join(c.get("text", "") for c in payload.get("content") or []
                               if isinstance(c, dict))
            if content and not content.lstrip().startswith("<"):
                out.append(content)
        return out

    def command(self, prompt, schema_file, last_file, mode, session_id):
        opts = ["--json", "--sandbox", "read-only", "--skip-git-repo-check",
                "--output-schema", schema_file, "--output-last-message", last_file]
        if mode == "resume":
            return ["codex", "exec", *opts, "resume", session_id, prompt]
        return ["codex", "exec", *opts, prompt]

    def run(self, cwd, prompt, schema, mode="fresh", session_id=None, new_session_id=None, untrusted=False):
        # The read-only sandbox has no hooks or network, so untrusted code changes nothing here.
        if mode == "fork":
            mode = "fresh"
        with tempfile.TemporaryDirectory() as tmp:
            schema_file = os.path.join(tmp, "schema.json")
            last_file = os.path.join(tmp, "last.txt")
            Path(schema_file).write_text(json.dumps(schema))
            argv = self.command(prompt, schema_file, last_file, mode, session_id)
            try:
                r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                                   timeout=PASS_TIMEOUT, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired as e:
                raise AgentError(f"codex timed out after {e.timeout} seconds") from e
            except OSError as e:
                raise AgentError(f"could not run codex: {e}") from e
            if r.returncode != 0:
                raise AgentError((r.stderr or r.stdout or f"codex exited {r.returncode}").strip()[-500:])
            thread = session_id if mode == "resume" else None
            for line in r.stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    thread = event["thread_id"]
            try:
                last = Path(last_file).read_text()
            except OSError:
                last = ""
        return {"session_id": thread, "answer": parse_json_answer(last)}
