import json
import os
import tempfile
import unittest

from shipyard.config import validate_auto, validate_config, validate_rules
from shipyard import passes, settings
from shipyard.poll import Poller, signature, snapshot

from fixtures import VIEWER, check_run, pr_node


class Config(unittest.TestCase):
    def test_defaults(self):
        cfg = validate_config({})
        self.assertEqual(cfg["rules"]["nudge"], {"after_hours": 24})
        self.assertEqual(cfg["rules"]["passes"]["ci_failed"], "auto")
        self.assertEqual({k for k, v in cfg["auto"].items() if v}, {"approve_rereview", "rerun_checks"})
        self.assertIsNone(validate_config({"auto": {"approve_rereview": False}})["auto"]["approve_rereview"])
        self.assertEqual(cfg["approve_replies"][:2], ["LGTM", "Looks great!"])
        with self.assertRaises(ValueError):
            validate_config({"approve_replies": ["LGTM", " "]})

    def test_rules_validation(self):
        self.assertEqual(validate_rules({"instructions": {"ci_failed": "Flaky specs live in spec/system."}})["instructions"],
                         {"ci_failed": "Flaky specs live in spec/system."})
        self.assertIs(validate_rules({"nudge": False})["nudge"], False)
        self.assertEqual(validate_rules({"nudge": True})["nudge"], {"after_hours": 24})
        self.assertEqual(validate_rules({"passes": {"pre_review": "ask"}})["passes"]["pre_review"], "ask")
        self.assertEqual(validate_rules({"address_feedback": {"prompt": "/respond {url}"}})["address_feedback"],
                         {"prompt": "/respond {url}"})
        for bad in ({"address_feedback": {"prompt": ""}}, {"nudge": {"after_days": 1}}, {"nudge": {"after_hours": 0}}, {"merge": "yes"},
                    {"passes": {"pre_review": "sometimes"}}, {"passes": {"lint": "auto"}}, {"unknown": 1},
                    {"instructions": {"lint": "x"}}, {"instructions": {"ci_failed": 3}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_rules(bad)

    def test_pass_instructions_go_before_the_answer_format(self):
        prompt = passes.render("ci_failed", {}, "Flaky specs live in spec/system.")
        self.assertLess(prompt.index("Flaky specs live in spec/system."), prompt.index("Answer with one JSON object"))
        self.assertNotIn("instructions from the user", passes.render("ci_failed", {}, "  "))

    def test_auto_validation(self):
        auto = validate_auto({"rerun_checks": {"when": "confidence >= medium", "max_per_pr": 3},
                              "enable_auto_merge": True})
        self.assertEqual(auto["rerun_checks"], {"min_confidence": "medium", "max_per_pr": 3})
        self.assertNotIn("enable_auto_merge", auto)
        self.assertEqual(validate_auto({"approve_rereview": {"max_lines": 10}})["approve_rereview"]["max_lines"], 10)
        self.assertEqual(validate_auto({"approve_rereview": True})["approve_rereview"]["max_lines"], 30)
        self.assertEqual(validate_auto({"approve_rereview": True})["approve_rereview"]["min_wait_minutes"], 3)
        self.assertEqual(validate_auto({"approve_rereview": {"min_wait_minutes": 0}})["approve_rereview"]["min_wait_minutes"], 0)
        self.assertEqual(validate_auto({"apply_fix": True})["apply_fix"]["max_lines"], 20)
        for bad in ({"post_reply": True}, {"rerequest_review": True}, {"rerun_checks": {"when": "sometimes"}},
                    {"rerun_checks": {"max_per_pr": 0}}, {"merge_everything": True},
                    {"approve_rereview": {"max_lines": -1}}, {"approve_rereview": {"min_wait_minutes": -1}}, {"rerun_checks": {"max_lines": 5}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_auto(bad)

    def test_existing_settings_still_validate(self):
        with self.assertRaises(ValueError):
            validate_config({"agent": {"sessions": "copilot"}})
        self.assertIsNone(validate_config({"agent": None})["agent"])

    def test_each_kind_of_session_brings_its_own_defaults(self):
        self.assertIn("{prompt}", validate_config({})["agent"]["prompt_url"])
        codex = validate_config({"agent": {"sessions": "codex"}})["agent"]
        self.assertEqual((codex["name"], codex["mode"], codex["prompt_url"], codex["command"][-2:]),
                         ("Codex", "command", None, ["{path}", "{session}"]))
        self.assertEqual(validate_config({"agent": {"sessions": "codex", "name": "Mine"}})["agent"]["name"], "Mine")
        cursor = validate_config({"launcher": {"app": "cursor"}})["launcher"]
        self.assertEqual((cursor["name"], cursor["url"], cursor["command"]), ("Cursor", "cursor://file/{path}", ["cursor", "{path}"]))
        self.assertEqual(validate_config({"launcher": {"mode": "command"}})["launcher"]["app"], "vscode")
        with self.assertRaises(ValueError):
            validate_config({"launcher": {"app": "notepad"}})
        fields = {tuple(f["path"]): f for f in settings.fields({"agent": {"sessions": "codex"}})}
        self.assertEqual((fields[("agent", "name")]["value"], fields[("agent", "name")]["set"]), ("Codex", False))
        with self.assertRaises(ValueError):
            validate_config({"agent": {"prompt_url": "claude://code/new"}})



class Snapshot(unittest.TestCase):
    def test_flattens_what_rules_need(self):
        node = pr_node(ci="FAILURE", contexts=[check_run("rspec", "FAILURE", job=5, run=6),
                                               check_run("lint", "SUCCESS")],
                       requested=["jm"], requested_at={"jm": "2026-10-20T00:00:00Z"},
                       reviews=[{"state": "CHANGES_REQUESTED", "author": "rev"},
                                {"state": "COMMENTED", "author": VIEWER}],
                       threads=[{"body": "why?", "comment_id": 77}])
        s = snapshot(node, VIEWER)
        self.assertEqual(s["key"], "o/r#1")
        self.assertTrue(s["mine"])
        self.assertEqual(s["failing"], [{"name": "rspec", "url": node["commits"]["nodes"][0]["commit"][
            "statusCheckRollup"]["contexts"]["nodes"][0]["detailsUrl"], "job_id": 5, "run_id": 6}])
        self.assertEqual(s["requested"], ["jm"])
        self.assertEqual(s["requested_at"], {"jm": "2026-10-20T00:00:00Z"})
        # the author's own review comments don't count as reviews of their PR
        self.assertEqual([r["author"] for r in s["reviews"]], ["rev"])
        self.assertEqual(s["threads"][0]["comments"][0]["id"], 77)

    def test_a_rerun_replaces_the_failed_run(self):
        failed, passed = check_run("labels", "FAILURE", job=5), check_run("labels", "SUCCESS", job=9)
        s = snapshot(pr_node(ci="FAILURE", contexts=[failed, passed]), VIEWER)
        self.assertEqual((s["ci"], s["failing"]), ("SUCCESS", []))
        self.assertEqual(s["page"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"], "SUCCESS")
        other = check_run("rspec", "FAILURE", job=3)
        self.assertEqual(snapshot(pr_node(ci="FAILURE", contexts=[failed, passed, other]), VIEWER)["ci"], "FAILURE")

    def test_team_requests(self):
        node = pr_node()
        node["reviewRequests"]["nodes"] = [{"requestedReviewer": {"__typename": "Team", "slug": "web"}}]
        self.assertEqual(snapshot(node, VIEWER)["requested"], ["team:web"])

    def test_page_shape(self):
        node = pr_node(contexts=[check_run("lint", "SUCCESS"), check_run("rspec", "FAILURE")], comments=[{"author": "rev"}],
                       reviews=[{"state": "APPROVED", "author": "rev", "body": "lgtm"}],
                       threads=[{"body": "why?", "author": VIEWER, "replies": ["you", "me2"]}])
        node["comments"]["totalCount"] = 4
        node["reviewThreads"]["nodes"][0]["latest"] = {"nodes": [{"author": {"login": "me2"}}]}
        page = snapshot(node, VIEWER)["page"]
        self.assertEqual(page["comments"], {"totalCount": 4})
        self.assertEqual(page["recentComments"]["nodes"][0]["author"], {"login": "rev"})
        self.assertEqual(page["latestReviews"]["nodes"], [{"state": "APPROVED", "author": {"login": "rev"},
                                                          "commit": {"oid": "aaa"}}])
        self.assertEqual(page["reviewThreads"]["nodes"], [{"isResolved": False,
                                                          "opener": {"nodes": [{"author": {"login": VIEWER}}]},
                                                          "comments": {"nodes": [{"author": {"login": "me2"}}]}}])
        [ctx] = page["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"]
        self.assertEqual((ctx["name"], set(ctx)), ("rspec", {"__typename", "name", "conclusion", "detailsUrl"}))
        self.assertIsNone(snapshot(pr_node(ci=None), VIEWER)["page"]["commits"]["nodes"][0]["commit"]["statusCheckRollup"])


class FakeGitHub:
    def __init__(self, lists, details):
        self.lists = lists      # search string -> nodes
        self.details = details  # node id -> full node
        self.detail_calls = []

    def graphql(self, query, variables):
        if "search(" in query:
            nodes = next((v for k, v in self.lists.items() if k in variables["q"]), [])
            return {"viewer": {"login": VIEWER},
                    "search": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}}
        self.detail_calls.append(list(variables["ids"]))
        return {"nodes": [self.details[i] for i in variables["ids"]]}


class MergedHistory(unittest.TestCase):
    def test_scopes_the_search_to_the_view_and_looks_up_owners_once(self):
        calls = []

        class GitHub:
            def graphql(self, query, variables):
                calls.append(variables)
                if "repositoryOwner" in query:
                    return {"repositoryOwner": {"__typename": "Organization" if variables["login"] == "acme" else "User"}}
                return {"search": {"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                                   "nodes": [pr_node(state="MERGED"), {}]}}

        poller = Poller(GitHub())
        out = poller.merged("acme, o/r,someone", "review")
        self.assertEqual(calls[-1]["q"], "is:pr archived:false org:acme repo:o/r user:someone "
                                         "is:merged reviewed-by:@me -author:@me sort:updated-desc")
        self.assertEqual(([p["number"] for p in out["prs"]], out["after"]), ([1], "c1"))
        poller.merged("acme", "mine", "c1")
        poller.merged("", "mine")
        self.assertEqual(calls[-1]["q"], "is:pr archived:false is:merged author:@me sort:updated-desc")
        self.assertEqual(sum("login" in c for c in calls), 2)
        with self.assertRaises(ValueError):
            poller.merged("acme is:open", "mine")


class PollerTest(unittest.TestCase):
    def test_details_only_for_moved_prs_and_vanished_ones(self):
        a, b = pr_node(1), pr_node(2)
        gh = FakeGitHub({"author:@me": [a, b]}, {a["id"]: a, b["id"]: b})
        poller = Poller(gh)
        viewer, snaps, open_ids = poller.poll(set())
        self.assertEqual(viewer, VIEWER)
        self.assertEqual(sorted(s["number"] for s in snaps), [1, 2])
        # nothing moved: no detail query at all
        _, snaps, _ = poller.poll(open_ids)
        self.assertEqual(snaps, [])
        self.assertEqual(len(gh.detail_calls), 1)
        # a check result changes the signature even though updatedAt didn't move
        a["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "FAILURE"
        _, snaps, _ = poller.poll(open_ids)
        self.assertEqual([s["number"] for s in snaps], [1])
        # b drops out of every list (merged): it is re-read so the merge is seen
        gh.lists["author:@me"] = [a]
        b["state"] = "MERGED"
        _, snaps, open_ids = poller.poll(open_ids)
        self.assertEqual([(s["number"], s["state"]) for s in snaps], [(2, "MERGED")])
        self.assertEqual(open_ids, {a["id"]})

    def test_signature_includes_checks(self):
        a = pr_node()
        before = signature(a)
        a["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "PENDING"
        self.assertNotEqual(before, signature(a))


if __name__ == "__main__":
    unittest.main()
