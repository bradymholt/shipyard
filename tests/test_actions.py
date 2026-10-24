import unittest

from shipyard import actions

from fixtures import check_run, snap


class RecordingGitHub:
    def __init__(self, fail_inline=False):
        self.calls = []
        self.fail_inline = fail_inline

    def rest(self, method, path, body=None):
        if self.fail_inline and body and body.get("comments"):
            raise RuntimeError("422 line must be part of the diff")
        self.calls.append((method, path, body))

    def graphql(self, query, variables):
        self.calls.append(("graphql", query, variables))


class Actions(unittest.TestCase):
    def test_review_with_a_bad_line_folds_comments_into_the_body(self):
        gh = RecordingGitHub(fail_inline=True)
        payload = {"event": "COMMENT", "body": "Summary.", "comments": [{"path": "a.py", "line": 9, "body": "Hmm."}]}
        self.assertEqual(actions.run(gh, "submit_review", snap(author="alice"), payload), "submitted a review")
        method, path, body = gh.calls[-1]
        self.assertEqual(path, "/repos/o/r/pulls/1/reviews")
        self.assertNotIn("comments", body)
        self.assertIn("`a.py:9` Hmm.", body["body"])

    def test_non_approval_review_needs_text(self):
        with self.assertRaises(actions.ActionError):
            actions.run(RecordingGitHub(), "submit_review", snap(author="alice"), {"event": "REQUEST_CHANGES"})
        with self.assertRaises(actions.ActionError):
            actions.run(RecordingGitHub(), "post_reply", snap(), {"body": "  "})

    def test_rerequest_splits_users_and_teams(self):
        gh = RecordingGitHub()
        actions.run(gh, "rerequest_review", snap(), {"reviewers": ["jm", "team:web"]})
        self.assertEqual(gh.calls[-1][2], {"reviewers": ["jm"], "team_reviewers": ["web"]})

    def test_rerun_only_failed_actions_runs(self):
        gh = RecordingGitHub()
        s = snap(ci="FAILURE", contexts=[check_run("a", "FAILURE", run=3), check_run("b", "FAILURE", job=12, run=3)])
        self.assertIn("1 workflow run", actions.run(gh, "rerun_checks", s, {}))
        with self.assertRaises(actions.ActionError):
            actions.run(gh, "rerun_checks", snap(), {})

    def test_unknown_action(self):
        with self.assertRaises(actions.ActionError):
            actions.run(RecordingGitHub(), "force_push", snap(), {})


if __name__ == "__main__":
    unittest.main()
