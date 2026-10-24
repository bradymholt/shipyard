"""Run the real companion server against fake GitHub and agent data, for the browser
smoke test (tests/ui/smoke.mjs) or to look at the sidebar without touching GitHub:

    python3 tests/ui/demo_server.py 4399

The page's own GitHub calls still go to api.github.com; smoke.mjs answers them with
matching fixtures."""
import functools
import http.server
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent.parent), str(HERE.parent)]

from fixtures import check_run, pr_node  # noqa: E402
from test_companion import FakeAgent, FakeGitHub  # noqa: E402

from shipyard.agents.claude_code import ClaudeCode  # noqa: E402
from shipyard.companion import Companion  # noqa: E402
from shipyard.config import REPO_DIR, validate_config  # noqa: E402
from shipyard.server import Handler  # noqa: E402


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4399
    tmp = Path(tempfile.mkdtemp(prefix="shipyard-demo-"))
    checkout = tmp / "dev" / "r" / ".claude" / "worktrees" / "feature"
    checkout.mkdir(parents=True)
    merged_wt = tmp / "dev" / "r" / ".claude" / "worktrees" / "old-feature"
    merged_wt.mkdir(parents=True)

    gh = FakeGitHub()
    gh.put(pr_node(101, head="h101", branch="me/webhook-retry", decision="CHANGES_REQUESTED",
                   reviews=[{"state": "CHANGES_REQUESTED", "author": "rev", "body": "A couple of things."}],
                   threads=[{"body": "Why retry 5 times? GitHub already retries.", "comment_id": 501}],
                   updated=ago(hours=1)), "author:@me")
    gh.put(pr_node(102, head="h102", branch="me/ci-cache", ci="FAILURE",
                   contexts=[check_run("rspec", "FAILURE", job=7, run=8)], requested=["jm"],
                   requested_at={"jm": ago(hours=3)}, updated=ago(hours=2)), "author:@me")
    gh.put(pr_node(104, head="h104", branch="me/onboarding", draft=True, committed=ago(days=5),
                   updated=ago(days=5)), "author:@me")
    gh.put(pr_node(105, head="h105", branch="me/auth", requested=["jm"], requested_at={"jm": ago(hours=30)},
                   updated=ago(hours=30)), "author:@me")
    gh.put(pr_node(201, author="alice", head="h201", branch="alice/search", requested=["me"],
                   updated=ago(hours=4)), "review-requested:@me")
    gh.put(pr_node(202, author="bob", head="h202", branch="bob/export", requested=["me"],
                   my_review={"databaseId": 9, "state": "CHANGES_REQUESTED", "submittedAt": ago(days=1),
                              "commit": {"oid": "old"}},
                   threads=[{"body": "Handle empty export", "author": "me"}], updated=ago(hours=5)),
           "review-requested:@me")

    transcript = tmp / "s-open.jsonl"
    transcript.write_text("".join(json.dumps({"type": "user", "message": {"role": "user", "content": t}}) + "\n"
                                  for t in ["Add retries to the webhook sender", "make the backoff exponential",
                                            "open the PR"]))
    os.utime(transcript, (time.time() - 7200, time.time() - 7200))

    class DemoAgent(FakeAgent):
        def session_file(self, path, sid):
            return transcript if sid == "s-open" else None

        def user_messages(self, text):
            return ClaudeCode().user_messages(text)

    agent = DemoAgent(str(checkout))
    agent.answers = {
        "reviewer requested changes": {
            "summary": "Rev questions the retry count and wants the backoff capped. Plan: drop to 3 "
                       "retries with jitter, and reply that GitHub doesn't retry failed deliveries.",
            "proposals": [{"action": "continue", "confidence": "high", "reason": "the backoff change needs code"},
                          {"action": "post_reply", "confidence": "high", "reason": "the question has a factual answer",
                           "payload": {"comment_id": 501, "body": "GitHub doesn't retry failed deliveries, "
                                       "so we do. I'll drop it to 3 with jitter."}}]},
        "CI failure": {
            "summary": "rspec timed out reaching the npm registry; the diff doesn't touch that path.",
            "proposals": [{"action": "rerun_checks", "confidence": "high", "reason": "network timeout, looks flaky"},
                          {"action": "apply_fix", "confidence": "low", "reason": "pin the registry mirror",
                           "payload": {"diff": "--- a/.npmrc\n+++ b/.npmrc\n@@ -0,0 +1 @@\n+registry=https://mirror.example/\n"}}]},
        "first-pass code review": {
            "summary": "Adds fuzzy search to the index. Solid overall; the tokenizer drops accents, "
                       "which breaks exact matches for non-ASCII names.",
            "proposals": [{"action": "submit_review", "confidence": "medium", "reason": "one real bug, otherwise fine",
                           "payload": {"event": "COMMENT", "body": "Nice. One issue with accents below.",
                                       "comments": [{"path": "search/tokenize.py", "line": 12,
                                                     "body": "This strips accents before matching exact names."}]}}]},
        "re-review": {
            "summary": "2 of 3 threads addressed; empty exports still crash.", "addressed": 2, "total": 3,
            "proposals": [{"action": "comment_unresolved", "confidence": "high", "reason": "one thread is open",
                           "payload": {"body": "Thanks! Empty exports still crash (export.py:40)."}}]},
    }
    wts = [{"repo": "o/r", "branch": "me/webhook-retry", "path": str(checkout), "main": False,
            "workspace": None, "dirty": False, "activity": 0, "session": {"id": "s-open", "updated": 0}},
           {"repo": "o/r", "branch": "me/ci-cache", "path": str(checkout), "main": False,
            "workspace": None, "dirty": False, "activity": 0, "session": {"id": "s-open", "updated": 0}},
           {"repo": "o/r", "branch": "me/old-feature", "path": str(merged_wt), "main": False,
            "workspace": None, "dirty": False, "activity": 0, "session": None}]
    for target in ("shipyard.companion.adapter_for", "shipyard.server.worktrees.discover",
                   "shipyard.companion.worktrees.discover", "shipyard.companion.worktrees.review_worktree"):
        value = {"shipyard.companion.adapter_for": agent,
                 "shipyard.companion.worktrees.review_worktree": {"ok": True, "path": str(checkout)}}.get(target, wts)
        mock.patch(target, return_value=value).start()
    mock.patch("shipyard.companion.update_config",
               side_effect=lambda section, name, value: validate_config({section: {name: value}}
                                                                        if "." not in name else {})).start()
    mock.patch("shipyard.companion.worktrees.cleanup_worktree",
               return_value={"ok": True, "removed": str(merged_wt), "branchDeleted": True}).start()

    comp = Companion(validate_config({}), [str(tmp / "dev")], tmp / "demo.db", github=gh)
    comp.poll_once()
    Handler.roots = [str(tmp / "dev")]
    Handler.config = comp.config
    Handler.session_token = "demo-token"
    Handler.companion = comp
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                            functools.partial(Handler, directory=str(REPO_DIR)))
    httpd.daemon_threads = True
    print(f"demo companion on http://localhost:{port}", flush=True)
    comp.drain_queue()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
