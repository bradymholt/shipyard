"""Actions: the GitHub calls (and the one git push) the server makes for a card.

Each runs only when you press its button, or, for an action type you trusted in
"auto", when the server presses it for you. Anything that posts text under your
name shows that text on the card first; post_reply is never automatic.
"""
import os
import tempfile

from . import worktrees
from .github import GitHubError

# Trust flag behind each action type; actions without one are always manual.
TRUST_FLAG = {
    "rerun_checks": "rerun_checks",
    "approve": "approve_rereview",
    "post_reply": "post_reply",
    "apply_fix": "apply_fix",
}
# What Undo does for an automatic action, where there is a sensible inverse.
UNDOABLE = {"enable_auto_merge"}
MERGE_METHODS = ("SQUASH", "MERGE", "REBASE")


class ActionError(RuntimeError):
    pass


def _pulls(snap):
    return f"/repos/{snap['repo']}/pulls/{snap['number']}"


def rerun_checks(gh, snap, payload, ctx):
    runs = sorted({c["run_id"] for c in snap.get("failing") or [] if c.get("run_id")})
    if not runs:
        raise ActionError("No failed GitHub Actions runs to re-run (other CI has to be re-run there).")
    for run in runs:
        gh.rest("POST", f"/repos/{snap['repo']}/actions/runs/{run}/rerun-failed-jobs")
    return f"re-ran failed jobs in {len(runs)} workflow run{'s' if len(runs) != 1 else ''}"


def _reviewer_lists(reviewers):
    users = [r for r in reviewers if not r.startswith("team:")]
    teams = [r[5:] for r in reviewers if r.startswith("team:")]
    return {"reviewers": users, "team_reviewers": teams}


def rerequest_review(gh, snap, payload, ctx):
    reviewers = payload.get("reviewers") or []
    if not reviewers:
        raise ActionError("No reviewers to re-request.")
    gh.rest("POST", f"{_pulls(snap)}/requested_reviewers", _reviewer_lists(reviewers))
    return f"re-requested review from {', '.join(reviewers)}"


def _method_not_allowed(error):
    # GitHub: "Merge method squash merging is not allowed on this repository"
    text = str(error).lower()
    return "merge method" in text and "not allowed" in text


def _with_merge_method(run):
    """Try merge methods in order, moving on only when the repo disallows one; any
    other failure (branch protection, a network error) stops there."""
    for method in MERGE_METHODS:
        try:
            run(method)
            return method
        except Exception as e:
            if not _method_not_allowed(e):
                raise ActionError(str(e)) from e
    raise ActionError("The repository allows none of squash, merge, or rebase.")


def _auto_merge(gh, snap, enable):
    if not enable:
        gh.graphql("mutation($id: ID!) { disablePullRequestAutoMerge(input: {pullRequestId: $id}) "
                   "{ clientMutationId } }", {"id": snap["id"]})
        return
    _with_merge_method(lambda m: gh.graphql(
        "mutation($id: ID!, $m: PullRequestMergeMethod!, $head: GitObjectID) { enablePullRequestAutoMerge("
        "input: {pullRequestId: $id, mergeMethod: $m, expectedHeadOid: $head}) { clientMutationId } }",
        {"id": snap["id"], "m": m, "head": snap.get("head")}))


def enable_auto_merge(gh, snap, payload, ctx):
    _auto_merge(gh, snap, True)
    return "enabled auto-merge"


def undo_enable_auto_merge(gh, snap, payload, ctx):
    _auto_merge(gh, snap, False)
    return "disabled auto-merge"


def merge(gh, snap, payload, ctx):
    # expectedHeadOid: commits pushed after you looked are not merged with it.
    method = _with_merge_method(lambda m: gh.graphql(
        "mutation($id: ID!, $m: PullRequestMergeMethod!, $head: GitObjectID) { mergePullRequest("
        "input: {pullRequestId: $id, mergeMethod: $m, expectedHeadOid: $head}) { clientMutationId } }",
        {"id": snap["id"], "m": m, "head": snap.get("head")}))
    return f"merged ({method.lower()})"


def mark_ready(gh, snap, payload, ctx):
    gh.graphql("mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) "
               "{ clientMutationId } }", {"id": snap["id"]})
    return "marked ready for review"


def _text(payload):
    body = (payload.get("body") or "").strip()
    if not body:
        raise ActionError("There is no text to post.")
    return body


def post_reply(gh, snap, payload, ctx):
    body = _text(payload)
    comment = payload.get("comment_id")
    if comment:
        gh.rest("POST", f"{_pulls(snap)}/comments/{int(comment)}/replies", {"body": body})
        return "posted a reply on a review thread"
    gh.rest("POST", f"/repos/{snap['repo']}/issues/{snap['number']}/comments", {"body": body})
    return "posted a comment"


NUDGED = "nudged the reviewers"
NUDGED_AUTHOR = "nudged the author"


def nudge(gh, snap, payload, ctx):
    gh.rest("POST", f"/repos/{snap['repo']}/issues/{snap['number']}/comments", {"body": _text(payload)})
    return NUDGED if snap.get("mine") else NUDGED_AUTHOR


def _review(gh, snap, event, body, comments=()):
    review = {"commit_id": snap.get("head")}
    if event:
        review["event"] = event
    if body:
        review["body"] = body
    inline = [{"path": c["path"], "line": int(c["line"]), "side": "RIGHT", "body": c["body"]}
              for c in comments if c.get("path") and c.get("line") and c.get("body")]
    if inline:
        review["comments"] = inline
    try:
        gh.rest("POST", f"{_pulls(snap)}/reviews", review)
    except GitHubError as e:
        if "one pending review" in str(e):
            raise ActionError("You already have a pending review on this PR; finish it on GitHub.") from e
        if not inline or e.status != 422:
            raise
        # A line outside the diff rejects the whole review; fold the comments into the body.
        folded = "\n".join(f"- `{c['path']}:{c['line']}` {c['body']}" for c in inline)
        review.pop("comments")
        review["body"] = f"{body}\n\n{folded}".strip()
        gh.rest("POST", f"{_pulls(snap)}/reviews", review)


def approve(gh, snap, payload, ctx):
    _review(gh, snap, "APPROVE", (payload.get("body") or "").strip())
    return "approved"


def comment_unresolved(gh, snap, payload, ctx):
    _review(gh, snap, "COMMENT", _text(payload))
    return "commented on what is still open"


def submit_review(gh, snap, payload, ctx):
    event = payload.get("event") if payload.get("event") in ("APPROVE", "COMMENT", "REQUEST_CHANGES") else "COMMENT"
    body = (payload.get("body") or "").strip()
    if event != "APPROVE" and not body:
        raise ActionError("A review that isn't an approval needs a summary.")
    _review(gh, snap, event, body, payload.get("comments") or [])
    return {"APPROVE": "approved", "COMMENT": "submitted a review",
            "REQUEST_CHANGES": "requested changes"}[event]


def apply_fix(gh, snap, payload, ctx):
    """Apply the shown diff in the PR's checkout, commit, and push. Refuses unless the
    checkout is on the PR's branch, clean, and at the PR's head."""
    diff = payload.get("diff") or ""
    if not diff.strip():
        raise ActionError("There is no diff to apply.")
    checkout = ctx.get("worktree")
    if not checkout and snap.get("mine") and ctx.get("roots"):
        checkout = worktrees.branch_worktree(snap["repo"], snap["branch"], ctx["roots"])
        if not checkout["ok"]:
            raise ActionError(f"Couldn't check out {snap['branch']} to apply the fix: {checkout['error']}")
    if not checkout:
        raise ActionError("The branch isn't checked out locally, so there is nowhere to apply the fix.")
    path = checkout["path"]
    if worktrees.has_tracked_changes(path):
        raise ActionError("The checkout has uncommitted changes. Commit or stash them first.")
    if worktrees.try_git(path, "symbolic-ref", "--short", "HEAD").strip() != snap["branch"]:
        raise ActionError(f"The checkout isn't on {snap['branch']}.")
    if worktrees.try_git(path, "rev-parse", "HEAD").strip() != snap.get("head"):
        raise ActionError("The checkout isn't at the PR's head; pull first so the fix applies to what reviewers see.")
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False) as fh:
        fh.write(diff if diff.endswith("\n") else diff + "\n")
        patch = fh.name
    try:
        # --index stages what the diff touches, new files included, and nothing else.
        for args in (("apply", "--check", "--index", patch), ("apply", "--index", patch)):
            error = worktrees.mutate_git(path, *args, timeout=60)
            if error:
                raise ActionError(f"The diff doesn't apply cleanly: {error}")
    finally:
        os.unlink(patch)
    message = payload.get("message") or "Fix the failing CI checks"
    for args in (("commit", "-m", message), ("push", "origin", f"HEAD:{snap['branch']}")):
        error = worktrees.mutate_git(path, *args, timeout=120)
        if error:
            raise ActionError(error)
    return "applied the fix and pushed"


def draft_review(gh, snap, payload, ctx):
    """Leave the review pending on GitHub, visible only to you until you submit it there."""
    _review(gh, snap, None, (payload.get("body") or "").strip(), payload.get("comments") or [])
    return "drafted a pending review on GitHub"


RUN = {
    "rerun_checks": rerun_checks, "rerequest_review": rerequest_review,
    "enable_auto_merge": enable_auto_merge, "disable_auto_merge": undo_enable_auto_merge,
    "merge": merge, "mark_ready": mark_ready,
    "post_reply": post_reply, "nudge": nudge, "approve": approve,
    "comment_unresolved": comment_unresolved, "submit_review": submit_review, "apply_fix": apply_fix,
    "draft_review": draft_review,
}
UNDO = {"enable_auto_merge": undo_enable_auto_merge}


def run(gh, action, snap, payload, ctx=None):
    fn = RUN.get(action)
    if not fn:
        raise ActionError(f"Unknown action: {action}")
    return fn(gh, snap, payload or {}, ctx or {})


def undo(gh, action, snap, payload, ctx=None):
    fn = UNDO.get(action)
    if not fn:
        raise ActionError(f"{action} can't be undone from here.")
    return fn(gh, snap, payload or {}, ctx or {})

