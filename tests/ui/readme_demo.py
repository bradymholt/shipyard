"""The demo server with realistic-looking data, for the README screenshots
(tests/ui/readme_shots.mjs):

    python3 tests/ui/readme_demo.py 4398
"""
import functools
import http.server
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent.parent), str(HERE.parent), str(HERE)]

import fixtures  # noqa: E402
import test_orchestrator  # noqa: E402
from demo_server import DemoGitHub, ago, approved  # noqa: E402
from fixtures import check_run, pr_node  # noqa: E402
from test_orchestrator import FakeAgent  # noqa: E402

from shipyard.agents.claude_code import ClaudeCode  # noqa: E402
from shipyard.config import REPO_DIR, validate_config  # noqa: E402
from shipyard.orchestrator import Orchestrator  # noqa: E402
from shipyard.server import Handler  # noqa: E402

ME = "bradymholt"
REPO = "acme/storefront"
fixtures.VIEWER = test_orchestrator.VIEWER = ME


def pr(number, title, **kw):
    kw.setdefault("author", ME)
    return pr_node(number, title=title, repo=REPO, head=f"h{number}", **kw)


class ReadmeGitHub(DemoGitHub):
    timelines = {}

    def graphql(self, query, variables=None):
        if "node(id:" in query:
            return {"node": {"timelineItems": {"nodes": self.timelines.get((variables or {}).get("id"), [])}}}
        return super().graphql(query, variables)


def commit(headline, **kw):
    return {"__typename": "PullRequestCommit", "commit": {"messageHeadline": headline, "committedDate": ago(**kw)}}


def review(state, login, **kw):
    return {"__typename": "PullRequestReview", "state": state, "submittedAt": ago(**kw), "author": {"login": login}}


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 4398
    tmp = Path(tempfile.mkdtemp(prefix="shipyard-readme-"))
    worktrees = tmp / "dev" / "storefront" / ".claude" / "worktrees"

    gh = ReadmeGitHub()
    webhook = pr(412, "Retry failed webhook deliveries with backoff", branch="webhook-retry",
                 decision="CHANGES_REQUESTED", updated=ago(hours=1),
                 reviews=[{"state": "CHANGES_REQUESTED", "author": "kfisher", "body": "A couple of questions."}],
                 threads=[{"body": "Why 5 retries? Doesn't GitHub already retry failed deliveries?",
                           "author": "kfisher", "comment_id": 501, "path": "app/services/webhook_sender.rb",
                           "line": 14, "hunk": "@@ -12,3 +12,3 @@ class WebhookSender\n   def deliver(event)\n"
                                                "-    retries = 3\n+    retries = 5"}])
    gh.put(webhook, "author:@me")
    ci = pr(415, "Cache bundle install in CI", branch="ci-bundle-cache", ci="FAILURE", updated=ago(hours=2),
            contexts=[check_run("rspec", "FAILURE", job=7, run=8)], requested=["priya"],
            requested_at={"priya": ago(hours=3)})
    gh.put(ci, "author:@me")
    gh.put(pr(409, "Add rate limiting to the checkout API", branch="checkout-rate-limit", decision="APPROVED",
              updated=ago(hours=5), reviews=[{"state": "APPROVED", "author": "kfisher", "body": ""}]), "author:@me")
    notify = pr(418, "Extract notification service", branch="notification-service", requested=["priya"],
                requested_at={"priya": ago(hours=30)}, updated=ago(hours=30))
    gh.put(notify, "author:@me")
    email = pr(419, "Add email channel to notification service", branch="notification-email",
               updated=ago(hours=9))
    email["baseRefName"] = "notification-service"
    gh.put(email, "author:@me")
    gh.put(pr(420, "WIP: experiment with view transitions", branch="view-transitions", draft=True,
              committed=ago(hours=2), updated=ago(hours=2)), "author:@me")

    search = pr(421, "Add fuzzy search to the product catalog", author="dchen", branch="dchen/fuzzy-search",
                requested=[ME], updated=ago(hours=4))
    gh.put(search, "review-requested:@me")
    export = pr(416, "Export orders as CSV", author="mlopez", branch="mlopez/order-export", requested=[ME],
                my_review={"databaseId": 9, "state": "CHANGES_REQUESTED", "submittedAt": ago(days=1),
                           "commit": {"oid": "old"}},
                threads=[{"body": "Handle an empty export", "author": ME}], updated=ago(hours=5))
    gh.put(export, "review-requested:@me", "reviewed-by:@me")
    gh.put(pr(414, "Move image uploads to direct S3 uploads", author="priya", branch="priya/direct-uploads",
              standing=[("CHANGES_REQUESTED", ME)],
              threads=[{"body": "Can this use the existing presigner?", "author": ME, "replies": ["priya"]},
                       {"body": "Missing a content-type check", "author": ME}],
              my_review={"databaseId": 10, "state": "COMMENTED", "submittedAt": ago(days=1),
                         "commit": {"oid": "h414"}, "onBehalfOf": {"nodes": []}}, updated=ago(hours=20)),
           "reviewed-by:@me")

    gh.merged["author:@me"] = [
        pr(410, "Add Slack notifications for failed deploys", branch="deploy-slack", state="MERGED",
           merged_at=ago(hours=20)),
        pr(405, "Migrate settings page to the new design system", branch="settings-redesign", state="MERGED",
           merged_at=ago(days=2)),
    ]
    gh.merged["reviewed-by:@me"] = [
        pr(411, "Speed up the cart totals query", author="kfisher", state="MERGED", merged_at=ago(hours=3),
           my_review=approved(ago(hours=4), "h411")),
        pr(407, "Add gift card balance lookup", author="dchen", state="MERGED", merged_at=ago(hours=26),
           my_review=approved(ago(hours=28), "h407")),
    ]
    gh.timelines = {
        webhook["id"]: [commit("Retry failed deliveries with exponential backoff", hours=5),
                        review("CHANGES_REQUESTED", "kfisher", hours=2)],
        ci["id"]: [commit("Cache vendor/bundle between CI runs", hours=3)],
        search["id"]: [commit("Add trigram index for product names", hours=6),
                       commit("Rank fuzzy matches by similarity", hours=4)],
        export["id"]: [review("CHANGES_REQUESTED", ME, days=1), commit("Handle an empty export", hours=5)],
    }

    sessions = {"webhook-retry": ("s-webhook", ["Retry failed webhook deliveries", "make the backoff exponential",
                                                "open the PR"]),
                "ci-bundle-cache": ("s-ci", ["Cache bundle install in CI", "open the PR"])}
    files = {}
    for branch, (sid, turns) in sessions.items():
        f = tmp / f"{sid}.jsonl"
        f.write_text("".join(json.dumps({"type": "user", "message": {"role": "user", "content": t}}) + "\n"
                             for t in turns))
        os.utime(f, (time.time() - 7200, time.time() - 7200))
        files[sid] = f

    class ReadmeAgent(FakeAgent):
        def find_pr_session(self, path, branch, url, latest=True):
            return {"id": sessions[branch][0], "how": "pr-url"} if branch in sessions else None

        def list_sessions(self, path):
            return [{"id": sid, "updated": 1} for sid, _ in sessions.values()]

        def session_file(self, path, sid):
            return files.get(sid)

        def user_messages(self, text):
            return ClaudeCode().user_messages(text)

        def title(self, text):
            return ClaudeCode().title(text)

    agent = ReadmeAgent(str(worktrees / "webhook-retry"))
    agent.answers = {
        "reviewer requested changes": {
            "summary": "Kim questions the retry count. GitHub doesn't redeliver failed webhooks on its own, "
                       "so the retries stay; plan: drop to 3 with jitter and cap the backoff at 30s.",
            "proposals": [{"action": "continue", "confidence": "high", "reason": "the backoff change needs code"},
                          {"action": "post_reply", "confidence": "high", "reason": "the question has a factual answer",
                           "payload": {"comment_id": 501, "body": "GitHub doesn't redeliver failed webhooks "
                                       "automatically, so we retry ourselves. I'll drop it to 3 with jitter."}}]},
        "CI failure": {
            "summary": "rspec timed out fetching gems from rubygems.org; this PR only changes the cache key, "
                       "so the failure is the network, not the diff.",
            "proposals": [{"action": "rerun_checks", "confidence": "high", "reason": "network timeout, looks flaky"},
                          {"action": "apply_fix", "confidence": "low", "reason": "retry the gem fetch",
                           "payload": {"diff": "--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n"
                                               "@@ -20,1 +20,1 @@\n-      run: bundle install\n"
                                               "+      run: bundle install --retry 3\n"}}]},
        "first-pass code review": {
            "summary": "Adds trigram fuzzy search to the catalog. Solid overall; the normalizer strips accents "
                       "before exact matching, so \"Café Bistro Mug\" no longer matches itself.",
            "proposals": [{"action": "submit_review", "confidence": "medium", "reason": "one real bug, otherwise good",
                           "payload": {"event": "REQUEST_CHANGES", "body": "One issue with accented names below.",
                                       "comments": [{"path": "app/models/product_search.rb", "line": 12,
                                                     "body": "This strips accents before exact matching."}]}}],
            "auto_approve": {"on": True, "why": "the one fix is small and checkable from the diff"}},
        "re-review": {
            "summary": "2 of 3 threads addressed; an empty export still raises in OrderExporter#rows.",
            "addressed": 2, "total": 3,
            "proposals": [{"action": "comment_unresolved", "confidence": "high", "reason": "one thread is open",
                           "payload": {"body": "Thanks! An empty export still raises (order_exporter.rb:40)."}}]},
    }
    wts = []
    for branch in ("webhook-retry", "ci-bundle-cache", "checkout-rate-limit", "notification-service",
                   "notification-email", "view-transitions"):
        (worktrees / branch).mkdir(parents=True)
        wts.append({"repo": REPO, "branch": branch, "path": str(worktrees / branch), "main": False,
                    "workspace": None, "dirty": False, "activity": 0,
                    "session": {"id": sessions[branch][0], "updated": 0} if branch in sessions else None})
    for target in ("shipyard.server.worktrees.discover", "shipyard.orchestrator.worktrees.discover"):
        mock.patch(target, return_value=wts).start()
    mock.patch("shipyard.orchestrator.adapter_for", return_value=agent).start()
    mock.patch("shipyard.orchestrator.worktrees.review_worktree",
               return_value={"ok": True, "path": str(worktrees / "review-421")}).start()

    comp = Orchestrator(validate_config({"views": [REPO],
                                         "rules": {"passes": {"feedback": "auto"}}, "auto": {"rerun_checks": False}}),
                        [str(tmp / "dev")], tmp / "demo.db", github=gh, config_path=tmp / "shipyard.config.json")
    comp.poll_once()
    Handler.config = comp.config
    Handler.session_token = "demo-token"
    Handler.orchestrator = comp
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                            functools.partial(Handler, directory=str(REPO_DIR)))
    httpd.daemon_threads = True
    print(f"readme demo on http://localhost:{port}", flush=True)
    comp.drain_queue()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    os._exit(0)


if __name__ == "__main__":
    main()
