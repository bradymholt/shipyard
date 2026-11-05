import unittest

from shipyard import actions
from shipyard.github import GitHubError

from fixtures import check_run, snap


class RecordingGitHub:
    def __init__(self, fail_inline=None, graphql_errors=()):
        self.calls = []
        self.fail_inline = fail_inline
        self.graphql_errors = list(graphql_errors)

    def rest(self, method, path, body=None):
        if self.fail_inline and body and body.get("comments"):
            raise self.fail_inline
        self.calls.append((method, path, body))

    def graphql(self, query, variables):
        self.calls.append(("graphql", query, variables))
        if self.graphql_errors:
            raise self.graphql_errors.pop(0)


class Actions(unittest.TestCase):
    def test_review_with_a_bad_line_folds_comments_into_the_body(self):
        gh = RecordingGitHub(fail_inline=GitHubError("line must be part of the diff", 422))
        payload = {"event": "COMMENT", "body": "Summary.", "comments": [{"path": "a.py", "line": 9, "body": "Hmm."}]}
        self.assertEqual(actions.run(gh, "submit_review", snap(author="alice"), payload), "submitted a review")
        method, path, body = gh.calls[-1]
        self.assertEqual(path, "/repos/o/r/pulls/1/reviews")
        self.assertNotIn("comments", body)
        self.assertIn("`a.py:9` Hmm.", body["body"])

    def test_review_is_not_reposted_after_a_timeout(self):
        gh = RecordingGitHub(fail_inline=GitHubError("network error reaching GitHub"))
        payload = {"event": "COMMENT", "body": "x", "comments": [{"path": "a.py", "line": 9, "body": "y"}]}
        with self.assertRaises(GitHubError):
            actions.run(gh, "submit_review", snap(author="alice"), payload)
        self.assertEqual(gh.calls, [])

    def test_merge_keeps_its_method_unless_the_repo_disallows_it(self):
        gh = RecordingGitHub(graphql_errors=[GitHubError("Merge method squash merging is not allowed on this repository")])
        self.assertEqual(actions.run(gh, "merge", snap(), {}), "merged (merge)")
        self.assertEqual(gh.calls[-1][2]["head"], "aaa")  # pinned to the head you saw
        gh = RecordingGitHub(graphql_errors=[GitHubError("Required status check \"ci\" is expected")])
        with self.assertRaises(actions.ActionError):
            actions.run(gh, "merge", snap(), {})
        self.assertEqual(len(gh.calls), 1)

    def test_stacked_pr_merges_through_the_async_api(self):
        gh = RecordingGitHub(graphql_errors=[GitHubError(
            "This pull request is part of a stack and must be merged using the asynchronous merge REST API.")])
        self.assertEqual(actions.run(gh, "merge", snap(), {}), "merging (squash)")
        self.assertEqual(gh.calls[-1], ("PUT", "/repos/o/r/pulls/1/merge-async", {"merge_method": "squash", "sha": "aaa"}))

    def test_approve_can_turn_auto_merge_on_or_merge_when_nothing_is_pending(self):
        gh = RecordingGitHub()
        payload = {"merge": True}
        self.assertEqual(actions.run(gh, "approve", snap(author="bob"), payload), "approved and enabled auto-merge")
        self.assertEqual(gh.calls[0][1], "/repos/o/r/pulls/1/reviews")
        self.assertIn("enablePullRequestAutoMerge", gh.calls[1][1])
        self.assertEqual(payload["merged"], "auto_merge")
        gh = RecordingGitHub(graphql_errors=[GitHubError("Pull request is in clean status")])
        payload = {"merge": True}
        self.assertEqual(actions.run(gh, "approve", snap(author="bob"), payload), "approved and merged (squash)")
        self.assertIn("mergePullRequest", gh.calls[-1][1])
        self.assertEqual(payload["merged"], "merged")
        gh = RecordingGitHub(graphql_errors=[GitHubError("Required status check \"ci\" is expected")])
        payload = {"merge": True}
        self.assertTrue(actions.run(gh, "approve", snap(author="bob"), payload).startswith("approved, but merging failed"))
        self.assertNotIn("merged", payload)
        payload = {"merge": True}
        self.assertEqual(actions.run(RecordingGitHub(), "approve", {**snap(author="bob"), "auto_merge": True}, payload),
                         "approved and auto-merge was already on")
        self.assertEqual(payload["merged"], "auto_merge")
        gh = RecordingGitHub()
        self.assertEqual(actions.run(gh, "approve", snap(author="bob"), {}), "approved")
        self.assertEqual(len(gh.calls), 1)

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
