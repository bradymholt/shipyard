"""Named events from the difference between two snapshots of one PR.

derive(prev, cur) is pure: prev is None the first time a PR is seen, and the first
sighting reports conditions that already hold (changes requested, CI red, a review
request) so a PR you open the board on is handled like one that just changed. The
caller dedupes by (PR, event, head SHA), so a re-poll never fires the same event twice.
"""

import re

FAILING = {"FAILURE", "ERROR"}

FEEDBACK = {"CHANGES_REQUESTED", "COMMENTED"}


# An approval whose body is only one of these is a plain sign-off, not feedback to answer.
SIGN_OFF = re.compile(r"^(lgtm|looks good( to me)?|ship ?it|:shipit:|approved?|nice( work| one)?|"
                      r"great( work)?|thanks?( you)?|ty)?[\s!.,:;)\U0001F300-\U0001FAFF\u2600-\u27BF]*$", re.I)
IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img\b[^>]*>", re.I)


def approval_feedback(snap, image_sign_off=True):
    """Approving reviews with something still to answer: a thread the approver opened that is
    unresolved without your reply last, or a review body beyond a sign-off that you haven't
    pushed or commented after."""
    viewer = snap.get("viewer")
    out = []
    for r in snap.get("reviews") or []:
        if r.get("state") != "APPROVED":
            continue
        threads = [t for t in snap.get("threads") or [] if not t.get("resolved") and t.get("comments")
                   and t["comments"][0].get("author") == r["author"]]
        if any(t["comments"][-1].get("author") != viewer for t in threads):
            out.append(r)
            continue
        body = r.get("body") or ""
        if SIGN_OFF.match((IMAGE.sub("", body) if image_sign_off else body).strip()):
            continue
        answered = (r.get("commit") and r["commit"] != snap.get("head")) or any(
            c.get("author") == viewer and (c.get("created") or "") > (r.get("submitted") or "")
            for c in (snap.get("comments") or []) + [c for t in snap.get("threads") or []
                                                     for c in t.get("comments") or []])
        if not answered:
            out.append(r)
    return out


def _feedback_ids(snap, image_sign_off=True):
    return ({r["id"] for r in snap.get("reviews") or [] if r.get("state") in FEEDBACK} |
            {r["id"] for r in approval_feedback(snap, image_sign_off)})


def requesters(snap):
    """Who a PR with feedback is waiting to hear back from. GitHub keeps the decision
    until that reviewer approves, but a later comment-only review replaces theirs in
    latestReviews; then it's whoever hasn't approved. Without a changes-requested
    decision, a comment-only review counts while its threads are open."""
    reviews = snap.get("reviews") or []
    if snap.get("decision") != "CHANGES_REQUESTED":
        if not any(not t.get("resolved") for t in snap.get("threads") or []):
            return []
        return [r["author"] for r in reviews if r.get("state") == "COMMENTED"]
    asked = [r["author"] for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
    return asked or [r["author"] for r in reviews if r.get("state") in FEEDBACK]


def needs_changes(snap, image_sign_off=True):
    return (snap.get("decision") == "CHANGES_REQUESTED" or
            bool(requesters(snap) or approval_feedback(snap, image_sign_off)))


def derive(prev, cur, image_sign_off=True):
    events = []

    def add(name, key=None, **data):
        # The head SHA is the dedupe key; `key` refines it when the same head can earn
        # the event again (a second reviewer requesting changes on the same commit).
        events.append({"name": name, "head": f"{cur.get('head')}{key or ''}", "data": data})

    first = prev is None
    if cur["state"] == "MERGED":
        if first or prev.get("state") != "MERGED":
            add("merged")
            # Auto-merge can land on the approval itself, before its comments were seen open.
            approvals = approval_feedback(cur, image_sign_off) if cur["mine"] else []
            if approvals:
                ids = sorted(str(r["id"]) for r in approvals if r.get("id") is not None)
                add("feedback", key=f":{','.join(ids)}" if ids else None,
                    reviewers=[r["author"] for r in approvals])
        return events
    if cur["state"] != "OPEN":
        if first or prev.get("state") == "OPEN":
            add("closed")
        return events

    if cur["mine"]:
        if first:
            add("pr_opened")
        changes = needs_changes(cur, image_sign_off)
        # A reviewer's comment-only review (each inline reply makes one) is new feedback too.
        new_feedback = _feedback_ids(cur, image_sign_off) - (set() if first else _feedback_ids(prev, image_sign_off))
        if changes and (first or not needs_changes(prev, image_sign_off) or new_feedback):
            ids = sorted(str(i) for i in _feedback_ids(cur, image_sign_off) if i is not None)
            reviewers = requesters(cur)
            reviewers += [r["author"] for r in approval_feedback(cur, image_sign_off) if r["author"] not in reviewers]
            add("feedback", key=f":{','.join(ids)}" if ids else None, reviewers=reviewers)
        if (not first and prev.get("head") != cur.get("head") and
                needs_changes(prev, image_sign_off)):
            pending = set(cur.get("requested") or [])
            reviewers = [r for r in requesters(cur) if r not in pending]
            if reviewers:
                add("pushed_after_feedback", reviewers=reviewers)
        failing = cur.get("ci") in FAILING
        was_failing = not first and prev.get("ci") in FAILING and prev.get("head") == cur.get("head")
        if failing and not was_failing:
            add("ci_failed", checks=[c["name"] for c in cur.get("failing") or []])
        if not first and prev.get("ci") in FAILING and cur.get("ci") == "SUCCESS":
            add("ci_recovered")
        if cur.get("decision") == "APPROVED" and (first or prev.get("decision") != "APPROVED"):
            add("approved")
        return events

    mine = cur.get("my_review")
    was_requested = not first and prev.get("requested_me")
    if cur.get("requested_me") and not was_requested:
        add("review_rerequested" if mine else "review_requested_of_me")
    if (mine and mine.get("state") == "DISMISSED" and
            (first or (prev.get("my_review") or {}).get("state") != "DISMISSED")):
        add("review_dismissed")
    if (not first and mine and prev.get("head") != cur.get("head") and
            mine.get("commit") != cur.get("head")):
        add("new_commits_since_my_review")
    return events
