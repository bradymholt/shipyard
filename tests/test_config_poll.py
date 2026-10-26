import json
import os
import tempfile
import unittest

from shipyard.config import validate_auto, validate_config, validate_rules
from shipyard.poll import Poller, signature, snapshot

from fixtures import VIEWER, check_run, pr_node


class Config(unittest.TestCase):
    def test_defaults(self):
        cfg = validate_config({})
        self.assertEqual(cfg["rules"]["nudge"], {"after_hours": 24})
        self.assertEqual(cfg["rules"]["passes"]["ci_failed"], "auto")
        self.assertEqual({k for k, v in cfg["auto"].items() if v}, {"approve_rereview", "rerun_checks"})
        self.assertIsNone(validate_config({"auto": {"approve_rereview": False}})["auto"]["approve_rereview"])

    def test_rules_validation(self):
        self.assertIs(validate_rules({"nudge": False})["nudge"], False)
        self.assertEqual(validate_rules({"nudge": True})["nudge"], {"after_hours": 24})
        self.assertEqual(validate_rules({"passes": {"pre_review": "ask"}})["passes"]["pre_review"], "ask")
        self.assertEqual(validate_rules({"address_feedback": {"prompt": "/respond {url}"}})["address_feedback"],
                         {"prompt": "/respond {url}"})
        for bad in ({"address_feedback": {"prompt": ""}}, {"nudge": {"after_days": 1}}, {"nudge": {"after_hours": 0}}, {"merge": "yes"},
                    {"passes": {"pre_review": "sometimes"}}, {"passes": {"lint": "auto"}}, {"unknown": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_rules(bad)

    def test_auto_validation(self):
        auto = validate_auto({"rerun_checks": {"when": "confidence >= medium", "max_per_pr": 3},
                              "enable_auto_merge": True})
        self.assertEqual(auto["rerun_checks"], {"min_confidence": "medium", "max_per_pr": 3})
        self.assertEqual(auto["enable_auto_merge"]["min_confidence"], "high")
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

    def test_prompt_url_defaults_to_claude_code_only(self):
        self.assertIn("{prompt}", validate_config({})["agent"]["prompt_url"])
        self.assertIsNone(validate_config({"agent": {"sessions": "codex"}})["agent"]["prompt_url"])
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

    def test_team_requests(self):
        node = pr_node()
        node["reviewRequests"]["nodes"] = [{"requestedReviewer": {"__typename": "Team", "slug": "web"}}]
        self.assertEqual(snapshot(node, VIEWER)["requested"], ["team:web"])

    def test_page_shape(self):
        node = pr_node(contexts=[check_run("lint", "SUCCESS")], comments=[{"author": "rev"}],
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
        ctx = page["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["contexts"]["nodes"][0]
        self.assertEqual(set(ctx), {"__typename", "name", "conclusion", "detailsUrl"})
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
