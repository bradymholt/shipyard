import unittest
from datetime import datetime, timezone

from shipyard import rules
from shipyard.config import validate_rules
from shipyard.events import derive

from fixtures import check_run, snap

RULES = validate_rules({})
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def names(events):
    return [e["name"] for e in events]


def review(state, author="rev", **kw):
    return {"state": state, "author": author, **kw}


def mine_review(state, commit="aaa", at="2026-10-20T10:00:00Z"):
    return {"databaseId": 9, "state": state, "submittedAt": at, "commit": {"oid": commit}}


class DeriveMine(unittest.TestCase):
    def test_first_sight_reports_conditions_that_already_hold(self):
        cur = snap(decision="CHANGES_REQUESTED", reviews=[review("CHANGES_REQUESTED")], ci="FAILURE",
                   contexts=[check_run("rspec", "FAILURE")])
        self.assertEqual(names(derive(None, cur)), ["pr_opened", "changes_requested", "ci_failed"])

    def test_nothing_new_means_no_events(self):
        cur = snap(decision="CHANGES_REQUESTED", reviews=[review("CHANGES_REQUESTED")], ci="FAILURE")
        self.assertEqual(derive(cur, cur), [])

    def test_a_second_reviewer_requesting_changes_fires_again(self):
        prev = snap(decision="CHANGES_REQUESTED", reviews=[review("CHANGES_REQUESTED", id=1)])
        cur = snap(decision="CHANGES_REQUESTED",
                   reviews=[review("CHANGES_REQUESTED", id=1), review("CHANGES_REQUESTED", "jm", id=2)])
        events = derive(prev, cur)
        self.assertEqual(names(events), ["changes_requested"])
        self.assertEqual(events[0]["data"]["reviewers"], ["rev", "jm"])
        # same commit, new set of reviews: a distinct dedupe key, so it isn't swallowed
        self.assertNotEqual(events[0]["head"], derive(None, prev)[1]["head"])

    def test_push_after_changes_requested_proposes_re_request(self):
        prev = snap(decision="CHANGES_REQUESTED", reviews=[review("CHANGES_REQUESTED", commit="aaa")])
        cur = snap(head="bbb", decision="CHANGES_REQUESTED",
                   reviews=[review("CHANGES_REQUESTED", commit="aaa")])
        events = derive(prev, cur)
        self.assertEqual(names(events), ["pushed_after_changes_requested"])
        self.assertEqual(events[0]["data"]["reviewers"], ["rev"])

    def test_no_re_request_proposal_when_reviewer_already_requested(self):
        prev = snap(decision="CHANGES_REQUESTED", reviews=[review("CHANGES_REQUESTED")])
        cur = snap(head="bbb", decision="CHANGES_REQUESTED", requested=["rev"],
                   reviews=[review("CHANGES_REQUESTED", commit="aaa")])
        self.assertEqual(derive(prev, cur), [])

    def test_ci_fails_once_per_head_and_recovers(self):
        green = snap(ci="SUCCESS")
        red = snap(ci="FAILURE")
        self.assertEqual(names(derive(green, red)), ["ci_failed"])
        self.assertEqual(derive(red, red), [])
        self.assertEqual(names(derive(red, snap(ci="SUCCESS"))), ["ci_recovered"])
        # a new push that fails again is a new failure
        self.assertEqual(names(derive(red, snap(head="bbb", ci="FAILURE"))), ["ci_failed"])

    def test_approved_and_merged(self):
        self.assertEqual(names(derive(snap(), snap(decision="APPROVED"))), ["approved"])
        self.assertEqual(names(derive(snap(), snap(state="MERGED"))), ["merged"])
        self.assertEqual(derive(snap(state="MERGED"), snap(state="MERGED")), [])
        self.assertEqual(names(derive(snap(), snap(state="CLOSED"))), ["closed"])


class DeriveReviews(unittest.TestCase):
    def theirs(self, **kw):
        return snap(author="alice", **kw)

    def test_first_review_request(self):
        self.assertEqual(names(derive(None, self.theirs(requested=["me"]))), ["review_requested_of_me"])

    def test_re_request_after_my_review(self):
        prev = self.theirs(my_review=mine_review("CHANGES_REQUESTED"))
        cur = self.theirs(head="bbb", requested=["me"], my_review=mine_review("CHANGES_REQUESTED"))
        self.assertEqual(names(derive(prev, cur)), ["review_rerequested", "new_commits_since_my_review"])

    def test_dismissed(self):
        prev = self.theirs(my_review=mine_review("APPROVED"))
        cur = self.theirs(my_review=mine_review("DISMISSED"))
        self.assertEqual(names(derive(prev, cur)), ["review_dismissed"])
        self.assertEqual(derive(cur, cur), [])


class MemoryChips(unittest.TestCase):
    def test_nudge_after_threshold(self):
        s = snap(requested=["rev"], requested_at={"rev": "2026-10-23T10:00:00Z"})
        chip = rules.memory_chip(s, RULES, NOW)
        self.assertEqual(chip["kind"], "nudge")
        self.assertEqual(chip["label"], "Nudge · 26h")

    def test_nudge_restarts_the_clock(self):
        s = snap(requested=["rev"], requested_at={"rev": "2026-10-20T10:00:00Z"})
        self.assertIsNone(rules.memory_chip(s, RULES, NOW, nudged_at="2026-10-24T02:00:00Z"))
        self.assertEqual(rules.memory_chip(s, RULES, NOW, nudged_at="2026-10-23T10:00:00Z")["label"], "Nudge · 26h")

    def test_no_nudge_before_threshold_or_when_off(self):
        s = snap(requested=["rev"], requested_at={"rev": "2026-10-24T02:00:00Z"})
        self.assertIsNone(rules.memory_chip(s, RULES, NOW))
        old = snap(requested=["rev"], requested_at={"rev": "2026-10-20T02:00:00Z"})
        self.assertIsNone(rules.memory_chip(old, validate_rules({"nudge": False}), NOW))
        self.assertIsNotNone(rules.memory_chip(old, validate_rules({"nudge": {"after_hours": 48}}), NOW))

    def test_idle_draft_gets_no_chip(self):
        self.assertIsNone(rules.memory_chip(snap(draft=True, committed="2026-10-01T08:00:00Z"), RULES, NOW))

    def test_snooze_wins_until_it_expires(self):
        s = snap(requested=["rev"], requested_at={"rev": "2026-10-20T10:00:00Z"})
        chip = rules.memory_chip(s, RULES, NOW, "2026-10-26T00:00:00Z")
        self.assertEqual(chip["kind"], "snoozed")
        self.assertEqual(rules.memory_chip(s, RULES, NOW, "2026-10-23T00:00:00Z")["kind"], "nudge")

    def test_others_prs_get_no_memory_chip(self):
        s = snap(author="alice", draft=True, committed="2026-10-01T08:00:00Z")
        self.assertIsNone(rules.memory_chip(s, RULES, NOW))


class Reactions(unittest.TestCase):
    def ev(self, name, **data):
        return {"name": name, "head": "aaa", "data": data}

    def test_changes_requested_runs_a_pass(self):
        out = rules.reactions(self.ev("changes_requested"), snap(), RULES)
        self.assertEqual(out, [{"do": "pass", "pass": "changes_requested", "mode": "auto"}])

    def test_pass_mode_ask_and_off(self):
        ask = validate_rules({"passes": {"ci_failed": "ask"}})
        self.assertEqual(rules.reactions(self.ev("ci_failed"), snap(), ask)[0]["mode"], "ask")
        off = validate_rules({"fix_ci": False})
        self.assertEqual(rules.reactions(self.ev("ci_failed"), snap(), off), [])
        never = validate_rules({"passes": {"ci_failed": False}})
        self.assertEqual(rules.reactions(self.ev("ci_failed"), snap(), never), [])

    def test_review_request_makes_worktree_and_pre_review(self):
        out = rules.reactions(self.ev("review_requested_of_me"), snap(author="alice"), RULES)
        self.assertEqual([o["do"] for o in out], ["review_worktree", "pass"])
        self.assertEqual(out[1]["pass"], "pre_review")

    def test_re_request_proposal(self):
        out = rules.reactions(self.ev("pushed_after_changes_requested", reviewers=["jm"]), snap(), RULES)
        self.assertEqual(out[0]["action"], "rerequest_review")
        self.assertEqual(out[0]["payload"], {"reviewers": ["jm"]})
        self.assertEqual(rules.reactions(self.ev("pushed_after_changes_requested", reviewers=["jm"]),
                                         snap(), validate_rules({"rerequest_review": False})), [])

    def test_merged_closes_cards(self):
        out = rules.reactions(self.ev("merged"), snap(state="MERGED"), RULES)
        self.assertEqual([o["do"] for o in out], ["close_cards"])


class StaleCards(unittest.TestCase):
    def test_changes_card_goes_when_decision_changes(self):
        card = {"pass": "changes_requested", "head": "aaa"}
        self.assertFalse(rules.card_is_stale(card, snap(decision="CHANGES_REQUESTED")))
        self.assertTrue(rules.card_is_stale(card, snap(decision="APPROVED")))

    def test_ci_card_goes_on_green_or_new_head(self):
        card = {"pass": "ci_failed", "head": "aaa"}
        self.assertFalse(rules.card_is_stale(card, snap(ci="FAILURE")))
        self.assertTrue(rules.card_is_stale(card, snap(ci="SUCCESS")))
        self.assertTrue(rules.card_is_stale(card, snap(head="bbb", ci="FAILURE")))

    def test_rerequest_card_goes_once_requested(self):
        card = {"action": "rerequest_review", "payload": {"reviewers": ["jm"]}}
        self.assertFalse(rules.card_is_stale(card, snap()))
        self.assertTrue(rules.card_is_stale(card, snap(requested=["jm"])))


if __name__ == "__main__":
    unittest.main()
