import json
import os
import tempfile
import unittest

from shipyard.config import update_config, validate_auto, validate_config, validate_rules
from shipyard.poll import Poller, signature, snapshot

from fixtures import VIEWER, check_run, pr_node


class Config(unittest.TestCase):
    def test_defaults(self):
        cfg = validate_config({})
        self.assertEqual(cfg["rules"]["nudge"], {"after_hours": 24})
        self.assertEqual(cfg["rules"]["passes"]["ci_failed"], "auto")
        self.assertTrue(all(v is None for v in cfg["auto"].values()))

    def test_rules_validation(self):
        self.assertIs(validate_rules({"nudge": False})["nudge"], False)
        self.assertEqual(validate_rules({"nudge": True})["nudge"], {"after_hours": 24})
        self.assertEqual(validate_rules({"passes": {"pre_review": "ask"}})["passes"]["pre_review"], "ask")
        for bad in ({"nudge": {"after_days": 1}}, {"nudge": {"after_hours": 0}}, {"merge": "yes"},
                    {"passes": {"pre_review": "sometimes"}}, {"passes": {"lint": "auto"}}, {"unknown": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_rules(bad)

    def test_auto_validation(self):
        auto = validate_auto({"rerun_checks": {"when": "confidence >= medium", "max_per_pr": 3},
                              "cleanup_review_wt": True})
        self.assertEqual(auto["rerun_checks"], {"min_confidence": "medium", "max_per_pr": 3})
        self.assertEqual(auto["cleanup_review_wt"]["min_confidence"], "high")
        for bad in ({"post_reply": True}, {"rerun_checks": {"when": "sometimes"}},
                    {"rerun_checks": {"max_per_pr": 0}}, {"merge_everything": True}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_auto(bad)

    def test_existing_settings_still_validate(self):
        with self.assertRaises(ValueError):
            validate_config({"agent": {"sessions": "copilot"}})
        self.assertIsNone(validate_config({"agent": None})["agent"])

    def test_update_config_keeps_other_settings_and_refuses_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "shipyard.config.json")
            with open(path, "w") as fh:
                json.dump({"roots": ["~/dev"]}, fh)
            cfg = update_config("auto", "rerun_checks", {"when": "confidence >= high"}, path)
            self.assertEqual(cfg["auto"]["rerun_checks"]["min_confidence"], "high")
            update_config("rules", "passes.ci_failed", "ask", path)
            with open(path) as fh:
                saved = json.load(fh)
            self.assertEqual(saved["roots"], ["~/dev"])
            self.assertEqual(saved["rules"], {"passes": {"ci_failed": "ask"}})
            with self.assertRaises(ValueError):
                update_config("auto", "post_reply", True, path)
            with open(path) as fh:
                self.assertNotIn("post_reply", json.load(fh)["auto"])


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
