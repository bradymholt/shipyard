"""Run the real Shipyard server against fake GitHub and agent data, for the browser
smoke test (tests/ui/smoke.mjs) or to look at the sidebar without touching GitHub:

    python3 tests/ui/demo_server.py 4399
"""
import copy
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
from test_orchestrator import FakeAgent, FakeGitHub  # noqa: E402

from shipyard.agents.claude_code import ClaudeCode  # noqa: E402
from shipyard.orchestrator import Orchestrator  # noqa: E402
from shipyard.config import REPO_DIR, validate_config  # noqa: E402
from shipyard.server import Handler  # noqa: E402


class DemoGitHub(FakeGitHub):
    """Applies the merges and review requests the smoke test makes, since the server
    re-reads a PR right after an action, and answers the merged-history and timeline reads."""

    def __init__(self):
        super().__init__()
        self.merged = {"author:@me": [], "reviewed-by:@me": []}

    def graphql(self, query, variables=None):
        q = (variables or {}).get("q", "")
        if "is:merged" in q:
            nodes = self.merged["reviewed-by:@me" if "reviewed-by:@me" in q else "author:@me"]
            return {"search": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": copy.deepcopy(nodes)}}
        if "node(id:" in query:
            return {"node": {"timelineItems": {"nodes": [
                {"__typename": "PullRequestCommit", "commit": {"messageHeadline": "Retry failed deliveries",
                                                                "committedDate": ago(hours=5)}},
                {"__typename": "PullRequestReview", "state": "CHANGES_REQUESTED", "submittedAt": ago(hours=2),
                 "author": {"login": "rev"}}]}}}
        if "mergePullRequest" in query:
            node = self.nodes[variables["id"]]
            node.update(state="MERGED", mergedAt=ago(minutes=0))
            self.put(node)
        return super().graphql(query, variables)

    def rest(self, method, path, body=None):
        if path.endswith("/requested_reviewers"):
            number = int(path.split("/")[-2])
            node = next(n for n in self.nodes.values() if n["number"] == number)
            node["reviewRequests"]["nodes"] += [{"requestedReviewer": {"__typename": "User", "login": r}}
                                                for r in (body or {}).get("reviewers") or []]
        return super().rest(method, path, body)


def ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")


def approved(at, oid):
    return {"databaseId": 12, "state": "APPROVED", "submittedAt": at, "commit": {"oid": oid}, "onBehalfOf": {"nodes": []}}


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4399
    tmp = Path(tempfile.mkdtemp(prefix="shipyard-demo-"))
    checkout = tmp / "dev" / "r" / ".claude" / "worktrees" / "feature"
    checkout.mkdir(parents=True)
    merged_wt = tmp / "dev" / "r" / ".claude" / "worktrees" / "old-feature"
    merged_wt.mkdir(parents=True)

    gh = DemoGitHub()
    gh.put(pr_node(101, head="h101", branch="me/webhook-retry", decision="CHANGES_REQUESTED",
                   title="Add retry to the webhook sender",
                   reviews=[{"state": "CHANGES_REQUESTED", "author": "rev", "body": "A couple of things."}],
                   threads=[{"body": "Why retry 5 times? GitHub already retries.", "comment_id": 501,
                             "path": "webhooks.py", "line": 12,
                             "hunk": "@@ -10,2 +10,3 @@\n def send(event):\n-    retries = 3\n+    retries = 5"}],
                   updated=ago(hours=1)), "author:@me")
    gh.put(pr_node(102, head="h102", branch="me/ci-cache", ci="FAILURE",
                   contexts=[check_run("rspec", "FAILURE", job=7, run=8)], requested=["jm"],
                   requested_at={"jm": ago(hours=3)}, updated=ago(hours=2)), "author:@me")
    gh.put(pr_node(103, head="h103", branch="me/parser", decision="APPROVED",
                   reviews=[{"state": "APPROVED", "author": "jm", "body": ""}]), "author:@me")
    gh.put(pr_node(109, head="h109", branch="me/lookups", decision="APPROVED",
                   reviews=[{"state": "APPROVED", "author": "jm", "body": "Approving, though I'd prefer a Map here."}]),
           "author:@me")
    gh.put(pr_node(110, head="h110", branch="me/schema", decision="APPROVED", mergeable="CONFLICTING",
                   reviews=[{"state": "APPROVED", "author": "jm", "body": ""}]), "author:@me")
    lookups = pr_node(98, head="h98", branch="me/merge-on-approval", requested=["jm"])
    gh.put(lookups, "author:@me")
    gh.put(pr_node(104, head="h104", branch="me/onboarding", draft=True, committed=ago(days=5),
                   updated=ago(days=5)), "author:@me")
    gh.put(pr_node(105, head="h105", branch="me/auth", requested=["jm"], requested_at={"jm": ago(hours=30)},
                   updated=ago(hours=30)), "author:@me")
    gh.put(pr_node(106, head="h106", branch="me/readme", decision="CHANGES_REQUESTED",
                   reviews=[{"state": "CHANGES_REQUESTED", "author": "rev"}], threads=[{"body": "Typo", "author": "me"}]),
           "author:@me")
    gh.put(pr_node(107, head="h107", branch="me/typo", decision="CHANGES_REQUESTED",
                   reviews=[{"state": "COMMENTED", "author": "rev"}],
                   threads=[{"body": "Done?", "author": "me"}, {"body": "Not yet", "author": "rev"}]), "author:@me")
    gh.put(pr_node(108, head="h108", branch="me/flag", decision="CHANGES_REQUESTED", requested=["rev"]), "author:@me")
    gh.put(pr_node(201, author="alice", head="h201", branch="alice/search", requested=["me"],
                   updated=ago(hours=4)), "review-requested:@me")
    gh.put(pr_node(202, author="bob", head="h202", branch="bob/export", requested=["me"],
                   my_review={"databaseId": 9, "state": "CHANGES_REQUESTED", "submittedAt": ago(days=1),
                              "commit": {"oid": "old"}},
                   threads=[{"body": "Handle empty export", "author": "me"}], updated=ago(hours=5)),
           "review-requested:@me", "reviewed-by:@me")
    gh.put(pr_node(203, author="cy", head="h203", branch="cy/follow-up", standing=[("CHANGES_REQUESTED", "me")],
                   threads=[{"body": "Rename this", "author": "me", "replies": ["cy"], "at": ago(hours=2)},
                            {"body": "And this", "author": "me"},
                            {"body": "Old one", "author": "me", "replies": ["cy"], "at": ago(days=2)}],
                   my_review={"databaseId": 10, "state": "COMMENTED", "submittedAt": ago(days=1),
                              "commit": {"oid": "h203"}, "onBehalfOf": {"nodes": []}}), "reviewed-by:@me")
    gh.put(pr_node(204, author="di", head="h204", branch="di/drive-by",
                   my_review={"databaseId": 11, "state": "COMMENTED", "submittedAt": ago(days=1),
                              "commit": {"oid": "h204"}, "onBehalfOf": {"nodes": []}}), "reviewed-by:@me")
    gh.merged["author:@me"] = [pr_node(99, head="h99", branch="me/old-feature", state="MERGED", merged_at=ago(hours=20))]
    gh.merged["reviewed-by:@me"] = [
        pr_node(97, author="cy", head="h97", state="MERGED", merged_at=ago(hours=5),
                threads=[{"body": "Why?", "author": "me", "replies": ["cy"], "at": ago(hours=5)}],
                my_review=approved(ago(hours=6), "h97")),
        pr_node(96, author="cy", head="h96", state="MERGED", merged_at=ago(hours=1),
                comments=[{"author": "cy", "at": ago(hours=1)}], my_review=approved(ago(hours=2), "h96")),
        pr_node(95, author="cy", head="h95", state="MERGED", merged_at=ago(hours=1),
                comments=[{"author": "cy", "at": ago(hours=3)}, {"author": "me", "at": ago(hours=2)}],
                my_review=approved(ago(hours=4), "h95")),
    ]

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

        def title(self, text):
            return ClaudeCode().title(text)

    agent = DemoAgent(str(checkout))
    agent.answers = {
        "reviewer requested changes": {
            "summary": "Rev questions the retry count and wants the backoff capped. Plan: drop `MAX_RETRIES` "
                       "to 3 with jitter, and reply that GitHub doesn't retry failed deliveries.",
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
                           "payload": {"event": "REQUEST_CHANGES", "body": "One issue with accents below.",
                                       "comments": [{"path": "search/tokenize.py", "line": 12,
                                                     "body": "This strips accents before matching exact names."}]}}],
            "auto_approve": {"on": True, "why": "the one fix is small and checkable from the diff"}},
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
    for target in ("shipyard.orchestrator.adapter_for", "shipyard.server.worktrees.discover",
                   "shipyard.orchestrator.worktrees.discover", "shipyard.orchestrator.worktrees.review_worktree"):
        value = {"shipyard.orchestrator.adapter_for": agent,
                 "shipyard.orchestrator.worktrees.review_worktree": {"ok": True, "path": str(checkout)}}.get(target, wts)
        mock.patch(target, return_value=value).start()

    comp = Orchestrator(validate_config({"rules": {"passes": {"feedback": "auto"}}, "auto": {"rerun_checks": False}}), [str(tmp / "dev")], tmp / "demo.db", github=gh,
                     config_path=tmp / "shipyard.config.json")
    comp.poll_once()
    lookups.update(state="MERGED", reviewDecision="APPROVED", updatedAt=ago(minutes=10), mergedAt=ago(minutes=10),
                   latestReviews={"nodes": [{"databaseId": 98, "state": "APPROVED", "submittedAt": ago(minutes=10),
                                             "body": "Approving, though I'd prefer a Map here.",
                                             "author": {"login": "jm"}, "commit": {"oid": "h98"}}]})
    gh.put(lookups)
    comp.poll_once()
    Handler.config = comp.config
    Handler.session_token = "demo-token"
    Handler.orchestrator = comp
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                            functools.partial(Handler, directory=str(REPO_DIR)))
    httpd.daemon_threads = True
    print(f"demo server on http://localhost:{port}", flush=True)
    comp.drain_queue()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
