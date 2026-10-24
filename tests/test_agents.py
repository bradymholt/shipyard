import json
import os
import stat
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from shipyard.agents import AgentError, parse_json_answer
from shipyard.agents.claude_code import ClaudeCode
from shipyard.agents.codex import Codex, _meta


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def tool_result():
    return {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}}


class HomeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        patcher = mock.patch.object(Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.checkout = str(self.home / "dev" / "repo")
        os.makedirs(self.checkout)

    def fake_cli(self, name, script):
        bin_dir = self.home / "bin"
        bin_dir.mkdir(exist_ok=True)
        exe = bin_dir / name
        exe.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(script))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)


class ClaudeSessions(HomeCase):
    def project(self):
        import re
        return self.home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(self.checkout))

    def test_pin_prefers_the_session_that_ran_gh_pr_create_for_this_pr(self):
        url = "https://github.com/o/r/pull/7"
        write_jsonl(self.project() / "parent.jsonl", [user("plan the feature"), user("gh pr create for other")])
        write_jsonl(self.project() / "child.jsonl", [user("build it"), user(f"ran gh pr create → {url}")])
        write_jsonl(self.project() / "latest.jsonl", [user("unrelated, newest")])
        claude = ClaudeCode()
        self.assertEqual(claude.find_pr_session(self.checkout, "feature", url), {"id": "child", "how": "pr-url"})
        # no transcript mentions the PR: fall back to the newest session in the checkout
        os.utime(self.project() / "latest.jsonl", (time.time() + 5, time.time() + 5))
        found = claude.find_pr_session(self.checkout, "nomatch", "https://github.com/o/r/pull/99")
        self.assertEqual(found, {"id": "latest", "how": "latest"})

    def test_user_messages_skip_tool_results(self):
        text = "".join(json.dumps(r) + "\n" for r in [user("one"), tool_result(), user("two"),
                                                       {"type": "assistant", "message": {}}])
        self.assertEqual(ClaudeCode().user_messages(text), ["one", "two"])

    def test_liveness_from_recent_writes(self):
        write_jsonl(self.project() / "s.jsonl", [user("hi")])
        claude = ClaudeCode()
        self.assertTrue(claude.is_live(self.checkout, "s"))
        old = time.time() - 3600
        os.utime(self.project() / "s.jsonl", (old, old))
        with mock.patch("shipyard.agents.processes_in", return_value=False):
            self.assertFalse(claude.is_live(self.checkout, "s"))

    def test_fork_command(self):
        argv = ClaudeCode().command("hi", {"type": "object"}, "fork", "abc", None)
        self.assertEqual(argv[argv.index("--resume") + 1], "abc")
        self.assertIn("--fork-session", argv)
        self.assertIn("--json-schema", argv)
        self.assertNotIn("Edit", argv[argv.index("--allowedTools"):argv.index("--disallowedTools")])
        fresh = ClaudeCode().command("hi", {}, "fresh", None, "new-id")
        self.assertEqual(fresh[fresh.index("--session-id") + 1], "new-id")

    def test_run_parses_structured_output(self):
        self.fake_cli("claude", """
            import json, sys
            args = sys.argv[1:]
            json.dump({"type": "result", "is_error": False, "session_id": "s2", "result": "",
                       "structured_output": {"summary": "flaky", "proposals": []},
                       "args": args}, sys.stdout)
        """)
        out = ClaudeCode().run(self.checkout, "why?", {}, "fork", "s1")
        self.assertEqual(out, {"session_id": "s2", "answer": {"summary": "flaky", "proposals": []}})

    def test_run_falls_back_to_json_in_text_and_reports_errors(self):
        self.fake_cli("claude", """
            import json, sys
            json.dump({"is_error": False, "session_id": "s", "result": "Here:\\n```json\\n{\\"summary\\": \\"x\\", \\"proposals\\": []}\\n```"}, sys.stdout)
        """)
        self.assertEqual(ClaudeCode().run(self.checkout, "q", {})["answer"]["summary"], "x")
        self.fake_cli("claude", """
            import json, sys
            json.dump({"is_error": True, "result": "not logged in"}, sys.stdout)
        """)
        with self.assertRaisesRegex(AgentError, "not logged in"):
            ClaudeCode().run(self.checkout, "q", {})


class CodexSessions(HomeCase):
    def setUp(self):
        super().setUp()
        _meta.clear()

    def rollout(self, sid, cwd, messages, day="2026/10/24"):
        f = self.home / ".codex" / "sessions" / day / f"rollout-{sid}.jsonl"
        write_jsonl(f, [{"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}] + [
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text", "text": m}]}}
            for m in messages])
        return f

    def test_sessions_and_messages(self):
        self.rollout("c1", self.checkout, ["<environment_context>…</environment_context>", "fix it"])
        codex = Codex()
        self.assertEqual(codex.sessions([self.checkout])[self.checkout]["id"], "c1")
        f = codex.session_file(self.checkout, "c1")
        self.assertEqual(codex.user_messages(f.read_text()), ["fix it"])
        self.assertFalse(codex.can_fork)

    def test_run_reads_thread_and_last_message(self):
        self.fake_cli("codex", """
            import json, sys
            args = sys.argv[1:]
            out = args[args.index("--output-last-message") + 1]
            assert "--output-schema" in args and "read-only" in args
            open(out, "w").write(json.dumps({"summary": "ok", "proposals": [], "resume": "resume" in args}))
            print(json.dumps({"type": "thread.started", "thread_id": "t9"}))
        """)
        out = Codex().run(self.checkout, "q", {"type": "object"}, mode="fork", session_id="c1")
        self.assertEqual(out["session_id"], "t9")
        self.assertFalse(out["answer"]["resume"])  # no headless fork: a fresh session instead
        out = Codex().run(self.checkout, "q", {}, mode="resume", session_id="c1")
        self.assertTrue(out["answer"]["resume"])


class ParseAnswer(unittest.TestCase):
    def test_variants(self):
        self.assertEqual(parse_json_answer('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json_answer('Sure! {"a": 2} hope that helps'), {"a": 2})
        self.assertEqual(parse_json_answer({"a": 3}), {"a": 3})
        with self.assertRaises(AgentError):
            parse_json_answer("no json here")


if __name__ == "__main__":
    unittest.main()
