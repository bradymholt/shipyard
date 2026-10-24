import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from shipyard.companion import Companion
from shipyard.config import validate_config

from fixtures import VIEWER, check_run, pr_node


class FakeGitHub:
    def __init__(self):
        self.lists = {"author:@me": [], "review-requested:@me": [], "reviewed-by:@me": []}
        self.nodes = {}
        self.calls = []

    def put(self, node, *lists):
        self.nodes[node["id"]] = node
        for name in self.lists:
            self.lists[name] = [n for n in self.lists[name] if n["id"] != node["id"]]
        for name in lists:
            self.lists[name].append(node)

    def graphql(self, query, variables=None):
        if "search(" in query:
            nodes = next(v for k, v in self.lists.items() if k in variables["q"])
            return {"viewer": {"login": VIEWER},
                    "search": {"pageInfo": {"hasNextPage": False}, "nodes": copy.deepcopy(nodes)}}
        if "nodes(ids" in query:
            return {"nodes": [copy.deepcopy(self.nodes[i]) for i in variables["ids"]]}
        self.calls.append(("graphql", query.split("(")[0].split()[-1], variables))
        return {}

    def rest(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {}

    def job_log_tail(self, repo, job_id, lines=150):
        return "Error: connect ETIMEDOUT registry.npmjs.org"


class FakeAgent:
    key = "fake"
    process_name = "fake"
    can_fork = True

    def __init__(self, checkout):
        self.checkout = checkout
        self.runs = []
        self.live = False
        self.answers = {}
        self.discarded = []

    def available(self):
        return True

    def find_pr_session(self, path, branch, url):
        return {"id": "s-open", "how": "pr-url"}

    def list_sessions(self, path):
        return [{"id": "s-open", "updated": 1}, {"id": "s-other", "updated": 0}]

    def session_file(self, path, sid):
        return None

    def user_messages(self, text):
        return []

    def is_live(self, path, sid):
        return self.live

    def discard(self, path, sid):
        self.discarded.append(sid)
        return True

    def run(self, cwd, prompt, schema, mode="fresh", session_id=None, new_session_id=None):
        self.runs.append({"cwd": cwd, "prompt": prompt, "mode": mode, "session": session_id})
        produced = new_session_id or {"fork": "fork-1", "fresh": "fresh-1"}.get(mode, session_id)
        answer = next((a for marker, a in self.answers.items() if marker in prompt),
                      {"summary": "ok", "proposals": []})
        return {"session_id": produced, "answer": answer}


class CompanionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.checkout = str(base / "checkout")
        Path(self.checkout).mkdir()
        self.config_path = base / "shipyard.config.json"
        self.gh = FakeGitHub()
        self.agent = FakeAgent(self.checkout)
        patches = [
            mock.patch("shipyard.companion.adapter_for", return_value=self.agent),
            mock.patch("shipyard.companion.worktrees.discover", return_value=[
                {"repo": "o/r", "branch": "feature", "path": self.checkout, "main": False}]),
            mock.patch("shipyard.companion.worktrees.review_worktree",
                       return_value={"ok": True, "path": self.checkout}),
            mock.patch("shipyard.companion.update_config",
                       side_effect=lambda section, name, value: self.reconfigure(section, name, value)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.raw = {}
        self.comp = Companion(validate_config({}), [str(base)], base / "shipyard.db", github=self.gh)
        self.addCleanup(self.comp.db.close)
        self.addCleanup(self.comp.stop)

    def reconfigure(self, section, name, value):
        self.raw.setdefault(section, {})[name] = value
        return validate_config(self.raw)

    def poll(self):
        self.comp.poll_once()
        self.idle()
        return self.comp.page_state()["prs"]

    def idle(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            busy = [p for s in self.comp.db.prs() for p in self.comp.db.passes(s["key"])
                    if p["status"] in ("queued", "running")]
            if not busy and not self.comp.running:
                return
            time.sleep(0.02)
        self.fail("passes did not finish")


class ChangesRequested(CompanionCase):
    def test_pass_resumes_pinned_session_and_card_posts_the_edited_reply(self):
        self.agent.answers["reviewer requested changes"] = {
            "summary": "Rev wants a guard and asks why.",
            "proposals": [{"action": "continue", "confidence": "high", "reason": "needs code"},
                          {"action": "post_reply", "confidence": "high", "reason": "a reply answers it",
                           "payload": {"comment_id": 500, "body": "Because the cache is per-process."}},
                          {"action": "delete_repo", "confidence": "high", "reason": "nope"}]}
        self.gh.put(pr_node(decision="CHANGES_REQUESTED",
                            reviews=[{"state": "CHANGES_REQUESTED", "author": "rev"}],
                            threads=[{"body": "why per-process?"}]), "author:@me")
        prs = self.poll()
        pr = prs["o/r#1"]
        self.assertEqual(self.agent.runs[0]["mode"], "resume")
        self.assertEqual(self.agent.runs[0]["session"], "s-open")
        self.assertIn("comment_id=500", self.agent.runs[0]["prompt"])
        self.assertTrue(pr["planReady"])
        card = pr["cards"][0]
        self.assertEqual(card["kind"], "pass")
        self.assertEqual([p["action"] for p in card["proposals"]], ["continue", "post_reply"])
        self.assertIn("asked the PR session for a plan", [a["text"] for a in pr["activity"]])

        # the same state polled again runs nothing new
        self.poll()
        self.assertEqual(len(self.agent.runs), 1)

        self.comp.card_action(card["id"], 1, {"body": "Edited reply."})
        self.assertIn(("POST", "/repos/o/r/pulls/1/comments/500/replies", {"body": "Edited reply."}),
                      self.gh.calls)
        self.assertEqual(self.comp.page_state()["prs"]["o/r#1"]["cards"], [])

    def test_live_session_gets_the_raw_event_instead_of_a_turn(self):
        self.agent.live = True
        self.gh.put(pr_node(decision="CHANGES_REQUESTED",
                            reviews=[{"state": "CHANGES_REQUESTED", "author": "rev"}]), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertEqual(self.agent.runs, [])
        self.assertEqual(card["kind"], "raw")
        self.assertIn("Changes were requested", card["raw"])

    def test_card_goes_away_once_feedback_is_addressed(self):
        node = pr_node(decision="CHANGES_REQUESTED", reviews=[{"state": "CHANGES_REQUESTED", "author": "rev"}])
        self.gh.put(node, "author:@me")
        self.assertEqual(len(self.poll()["o/r#1"]["cards"]), 1)
        node["reviewDecision"] = "APPROVED"
        node["updatedAt"] = "2026-10-21T00:00:00Z"
        self.assertEqual(self.poll()["o/r#1"]["cards"], [])


class CiFailed(CompanionCase):
    def setUp(self):
        super().setUp()
        self.agent.answers["CI failure"] = {
            "summary": "npm registry timed out; unrelated to the diff.",
            "proposals": [{"action": "rerun_checks", "confidence": "high", "reason": "looked flaky"}]}

    def red(self, head="aaa"):
        return pr_node(head=head, ci="FAILURE", contexts=[check_run("rspec", "FAILURE", job=11, run=22)])

    def test_fork_and_log_tail_in_prompt(self):
        self.gh.put(self.red(), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertEqual(self.agent.runs[0]["mode"], "fork")
        self.assertIn("ETIMEDOUT", self.agent.runs[0]["prompt"])
        self.assertEqual(self.agent.discarded, ["fork-1"])  # the fork's transcript is not kept
        self.assertEqual(card["proposals"][0]["action"], "rerun_checks")
        self.assertFalse(any(c[0] == "POST" for c in self.gh.calls))  # manual until trusted

    def test_trusted_rerun_fires_itself_and_lands_in_handled(self):
        self.comp.set_trust("rerun_checks", True)
        self.gh.put(self.red(), "author:@me")
        pr = self.poll()["o/r#1"]
        self.assertIn(("POST", "/repos/o/r/actions/runs/22/rerun-failed-jobs", None), self.gh.calls)
        self.assertEqual(pr["cards"], [])
        self.assertEqual(pr["handled"][0]["handled"]["reason"], "looked flaky")
        self.assertFalse(pr["handled"][0]["handled"]["undoable"])

    def test_max_per_pr_caps_automatic_reruns(self):
        self.comp.set_trust("rerun_checks", True)
        for head in ("a1", "a2", "a3"):
            node = self.red(head)
            node["updatedAt"] = f"2026-10-2{head[-1]}T00:00:00Z"
            self.gh.put(node, "author:@me")
            self.poll()
        reruns = [c for c in self.gh.calls if c[0] == "POST" and "rerun" in c[1]]
        self.assertEqual(len(reruns), 2)

    def test_trust_offer_after_three_manual_clicks(self):
        self.gh.put(self.red(), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertEqual(card["trustOffers"], [])
        for _ in range(3):
            self.comp.do_action("o/r#1", "rerun_checks", {})
        self.comp.db.update_card(card["id"], status="open")
        card = self.comp.page_state()["prs"]["o/r#1"]["cards"][0]
        self.assertEqual(card["trustOffers"], ["rerun_checks"])

    def test_ask_mode_offers_instead_of_running(self):
        self.raw = {"rules": {"passes": {"ci_failed": "ask"}}}
        self.comp.config = validate_config(self.raw)
        self.gh.put(self.red(), "author:@me")
        card = self.poll()["o/r#1"]["cards"][0]
        self.assertEqual(card["kind"], "offer")
        self.assertEqual(self.agent.runs, [])
        self.comp.run_pass_now("o/r#1", "ci_failed")
        self.idle()
        self.assertEqual(self.comp.page_state()["prs"]["o/r#1"]["cards"][0]["kind"], "pass")


class Reviews(CompanionCase):
    def test_pre_review_runs_fresh_in_review_worktree_and_pins_it(self):
        self.agent.answers["first-pass code review"] = {
            "summary": "Solid; one edge case.",
            "proposals": [{"action": "submit_review", "confidence": "medium", "reason": "minor",
                           "payload": {"event": "COMMENT", "body": "Looks good; one question.",
                                       "comments": [{"path": "app.py", "line": 3, "body": "None here?"}]}}]}
        self.gh.put(pr_node(number=5, author="alice", requested=[VIEWER]), "review-requested:@me")
        pr = self.poll()["o/r#5"]
        run = self.agent.runs[0]
        self.assertEqual((run["mode"], run["cwd"]), ("fresh", self.checkout))
        self.assertEqual(self.comp.db.pin("o/r#5")["how"], "shipyard")
        self.assertEqual(self.agent.discarded, [])  # the pinned review session stays
        card = pr["cards"][0]
        self.comp.card_action(card["id"], 0, None)
        method, path, body = self.gh.calls[-1]
        self.assertEqual((method, path), ("POST", "/repos/o/r/pulls/5/reviews"))
        self.assertEqual(body["comments"][0]["side"], "RIGHT")


    def test_re_review_without_a_review_worktree_makes_one(self):
        from shipyard.companion import worktrees
        review = {"databaseId": 9, "state": "CHANGES_REQUESTED", "submittedAt": "2026-10-19T00:00:00Z",
                  "commit": {"oid": "old"}}
        self.gh.put(pr_node(number=6, author="bob", requested=[VIEWER], my_review=review), "review-requested:@me")
        pr = self.poll()["o/r#6"]
        worktrees.review_worktree.assert_called()
        self.assertEqual(self.agent.runs[0]["cwd"], self.checkout)
        self.assertEqual(pr["cards"][0]["kind"], "pass")


class Proposals(CompanionCase):
    def test_push_after_changes_requested_proposes_re_request(self):
        node = pr_node(decision="CHANGES_REQUESTED", reviews=[{"state": "CHANGES_REQUESTED", "author": "jm"}])
        self.gh.put(node, "author:@me")
        self.poll()
        node["headRefOid"] = "bbb"
        node["commits"]["nodes"][0]["commit"]["oid"] = "bbb"
        self.gh.put(node, "author:@me")
        cards = self.poll()["o/r#1"]["cards"]
        proposal = next(c for c in cards if c["kind"] == "proposal")
        self.assertEqual(proposal["title"], "Re-request jm's review")
        self.comp.card_action(proposal["id"], 0, None)
        self.assertIn(("POST", "/repos/o/r/pulls/1/requested_reviewers",
                       {"reviewers": ["jm"], "team_reviewers": []}), self.gh.calls)

    def test_trusted_re_request_can_be_undone(self):
        self.comp.set_trust("rerequest_review", True)
        node = pr_node(decision="CHANGES_REQUESTED", reviews=[{"state": "CHANGES_REQUESTED", "author": "jm"}])
        self.gh.put(node, "author:@me")
        self.poll()
        node["headRefOid"] = "bbb"
        self.gh.put(node, "author:@me")
        handled = self.poll()["o/r#1"]["handled"][0]
        self.assertTrue(handled["handled"]["undoable"])
        self.comp.undo(handled["id"])
        self.assertEqual(self.gh.calls[-1][0], "DELETE")


class PageRequests(CompanionCase):
    def test_snooze_chip_and_repin(self):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        self.comp.snooze("o/r#1", 24)
        pr = self.comp.page_state()["prs"]["o/r#1"]
        self.assertEqual(pr["chip"]["kind"], "snoozed")
        self.comp.snooze("o/r#1", None)
        self.assertIsNone(self.comp.page_state()["prs"]["o/r#1"]["chip"])
        self.comp.repin("o/r#1", "s-other")
        self.assertEqual(self.comp.db.pin("o/r#1")["session_id"], "s-other")
        with self.assertRaises(LookupError):
            self.comp.repin("o/r#1", "not-in-this-checkout")

    def test_ask_runs_a_fork_with_the_question(self):
        self.gh.put(pr_node(), "author:@me")
        self.poll()
        self.comp.ask("o/r#1", "  what did we decide about caching?  ")
        self.idle()
        self.assertEqual(self.agent.runs[-1]["mode"], "fork")
        self.assertIn("what did we decide about caching?", self.agent.runs[-1]["prompt"])
        self.assertEqual(self.comp.page_state()["prs"]["o/r#1"]["cards"][0]["kind"], "answer")
        with self.assertRaises(ValueError):
            self.comp.ask("o/r#1", "   ")

    def test_post_reply_cannot_be_trusted(self):
        with self.assertRaises(ValueError):
            self.comp.set_trust("post_reply", True)

    def test_unknown_pr(self):
        with self.assertRaises(LookupError):
            self.comp.do_action("o/r#404", "merge", {})


if __name__ == "__main__":
    unittest.main()
