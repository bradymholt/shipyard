"""Builders for GitHub GraphQL PR nodes and snapshots used across the tests."""
import copy

from shipyard.poll import snapshot

VIEWER = "me"


def check_run(name, conclusion, job=11, run=22):
    return {"__typename": "CheckRun", "name": name, "conclusion": conclusion, "status": "COMPLETED",
            "detailsUrl": f"https://github.com/o/r/actions/runs/{run}/job/{job}", "databaseId": job,
            "checkSuite": {"workflowRun": {"databaseId": run}}}


def pr_node(number=1, author=VIEWER, head="aaa", state="OPEN", decision=None, ci="SUCCESS",
            contexts=None, requested=(), reviews=(), threads=(), draft=False, my_review=None,
            updated="2026-10-20T10:00:00Z", requested_at=None, committed="2026-10-20T09:00:00Z",
            created="2026-10-19T09:00:00Z", repo="o/r", branch="feature", comments=(), running=0):
    """A PullRequest node shaped like the companion's detail query returns."""
    requested_at = requested_at or {}
    return copy.deepcopy({
        "id": f"PR_{repo}_{number}", "number": number, "title": f"PR {number}",
        "url": f"https://github.com/{repo}/pull/{number}", "state": state, "isDraft": draft,
        "createdAt": created, "updatedAt": updated, "mergedAt": None, "closedAt": None,
        "body": "Does a thing.", "headRefName": branch, "headRefOid": head, "baseRefName": "main",
        "additions": 10, "deletions": 2, "changedFiles": 3,
        "repository": {"nameWithOwner": repo}, "author": {"login": author},
        "reviewDecision": decision, "mergeable": "MERGEABLE", "autoMergeRequest": None,
        "viewerLatestReview": my_review,
        "reviewRequests": {"nodes": [{"requestedReviewer": {"__typename": "User", "login": r}}
                                     for r in requested]},
        "latestReviews": {"nodes": [
            {"databaseId": r.get("id", i + 100), "state": r["state"], "submittedAt": r.get("at", updated),
             "body": r.get("body", ""), "author": {"login": r["author"]}, "commit": {"oid": r.get("commit", head)}}
            for i, r in enumerate(reviews)]},
        "reviewThreads": {"nodes": [
            {"id": f"T{i}", "isResolved": t.get("resolved", False), "isOutdated": False,
             "path": t.get("path", "app.py"), "line": t.get("line", 3),
             "comments": {"nodes": [{"databaseId": t.get("comment_id", 500 + i), "body": t["body"],
                                     "createdAt": updated, "diffHunk": t.get("hunk"),
                                     "author": {"login": t.get("author", "rev")}}] +
                                   [{"databaseId": 900 + i, "body": "reply", "createdAt": updated,
                                     "author": {"login": who}} for who in t.get("replies", ())]}}
            for i, t in enumerate(threads)]},
        "comments": {"nodes": [{"createdAt": c.get("at", updated), "author": {"login": c["author"]}}
                               for c in comments]},
        "commits": {"nodes": [{"commit": {
            "oid": head, "committedDate": committed,
            "statusCheckRollup": {"state": ci, "contexts": {
                "nodes": contexts or [], "checkRunCountsByState": [{"state": "IN_PROGRESS", "count": running}]}}
            if ci else None}}]},
        "timelineItems": {"nodes": [
            {"__typename": "ReviewRequestedEvent", "createdAt": at,
             "requestedReviewer": {"__typename": "User", "login": who}}
            for who, at in requested_at.items()]},
    })


def snap(**kw):
    return snapshot(pr_node(**kw), VIEWER)
