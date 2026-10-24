import json
import os
import re
import subprocess
from pathlib import Path

from . import PASS_TIMEOUT, Adapter, AgentError, parse_json_answer

# Passes only read: files and the PR on GitHub. Only the interactive session you open
# yourself edits files. git log/diff/show are left out because --output writes a file;
# `gh pr diff` and `gh pr view` cover what a pass needs.
READ_ONLY_TOOLS = [
    "Read", "Grep", "Glob", "Task",
    "Bash(git status:*)", "Bash(git blame:*)",
    "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr checks:*)", "Bash(gh run view:*)",
]
WRITE_TOOLS = ["Edit", "Write", "NotebookEdit", "MultiEdit"]
# Set when the companion itself was started from inside a Claude Code session; passed
# through, they make the headless run think it is that session (or a child of it).
PARENT_SESSION_VARS = {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                       "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_PID"}


class ClaudeCode(Adapter):
    key = "claude-code"
    process_name = "claude"
    can_fork = True

    def project_dirs(self, path):
        # Claude Code keeps each session as ~/.claude/projects/<cwd, non-alphanumerics
        # replaced by "-">/<session id>.jsonl.
        projects = Path.home() / ".claude" / "projects"
        return {projects / re.sub(r"[^A-Za-z0-9]", "-", p) for p in (path, os.path.realpath(path))}

    def session_files(self, path):
        return [(f.stem, f) for d in self.project_dirs(path) for f in d.glob("*.jsonl")]

    def user_messages(self, text):
        out = []
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") != "user" or record.get("isMeta") or record.get("isSidechain"):
                continue
            content = (record.get("message") or {}).get("content")
            if isinstance(content, list):  # tool results ride in "user" records too
                if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                    continue
                # Desktop sends a <system-reminder> part ahead of the text you typed.
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict)
                                   and not c.get("text", "").lstrip().startswith("<")).strip()
            if isinstance(content, str) and content and not content.startswith("<"):
                out.append(content)
        return out

    def command(self, prompt, schema, mode, session_id, new_session_id, untrusted=False):
        argv = ["claude", "-p", prompt, "--output-format", "json",
                "--json-schema", json.dumps(schema),
                "--allowedTools", *READ_ONLY_TOOLS, "--disallowedTools", *WRITE_TOOLS]
        if untrusted:
            # Someone else's PR may ship .claude/settings.json hooks or allow rules;
            # load only your user settings.
            argv += ["--setting-sources", "user"]
        if mode in ("resume", "fork"):
            argv += ["--resume", session_id]
            if mode == "fork":
                argv.append("--fork-session")
        elif new_session_id:
            argv += ["--session-id", new_session_id]
        return argv

    def run(self, cwd, prompt, schema, mode="fresh", session_id=None, new_session_id=None, untrusted=False):
        argv = self.command(prompt, schema, mode, session_id, new_session_id, untrusted)
        try:
            env = {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}
            r = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env,
                               timeout=PASS_TIMEOUT, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as e:
            raise AgentError(f"claude timed out after {e.timeout} seconds") from e
        except OSError as e:
            raise AgentError(f"could not run claude: {e}") from e
        try:
            out = json.loads(r.stdout)
        except ValueError:
            raise AgentError((r.stderr or r.stdout or f"claude exited {r.returncode}").strip()[:500])
        if out.get("is_error") or r.returncode != 0:
            raise AgentError(str(out.get("result") or r.stderr or "claude reported an error")[:500])
        answer = out.get("structured_output")
        if not isinstance(answer, dict):
            answer = parse_json_answer(out.get("result"))
        return {"session_id": out.get("session_id") or new_session_id or session_id, "answer": answer}
