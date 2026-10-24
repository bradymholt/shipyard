"""Named events from the difference between two snapshots of one PR.

derive(prev, cur) is pure: prev is None the first time a PR is seen, and the first
sighting reports conditions that already hold (changes requested, CI red, a review
request) so a PR you open the board on is handled like one that just changed. The
caller dedupes by (PR, event, head SHA), so a re-poll never fires the same event twice.
"""

FAILING = {"FAILURE", "ERROR"}


FEEDBACK = {"CHANGES_REQUESTED", "COMMENTED"}


def _feedback_ids(snap):
    return {r["id"] for r in snap.get("reviews") or [] if r.get("state") in FEEDBACK}


def requesters(snap):
    """Who a changes-requested PR is waiting to hear back from. GitHub keeps the decision
    until that reviewer approves, but a later comment-only review replaces theirs in
    latestReviews; then it's whoever hasn't approved."""
    reviews = snap.get("reviews") or []
    asked = [r["author"] for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
    if asked or snap.get("decision") != "CHANGES_REQUESTED":
        return asked
    return [r["author"] for r in reviews if r.get("state") in FEEDBACK]


def derive(prev, cur):
    events = []

    def add(name, key=None, **data):
        # The head SHA is the dedupe key; `key` refines it when the same head can earn
        # the event again (a second reviewer requesting changes on the same commit).
        events.append({"name": name, "head": f"{cur.get('head')}{key or ''}", "data": data})

    first = prev is None
    if cur["state"] == "MERGED":
        if first or prev.get("state") != "MERGED":
            add("merged")
        return events
    if cur["state"] != "OPEN":
        if first or prev.get("state") == "OPEN":
            add("closed")
        return events

    if cur["mine"]:
        if first:
            add("pr_opened")
        changes = cur.get("decision") == "CHANGES_REQUESTED"
        # A reviewer's comment-only review (each inline reply makes one) is new feedback too.
        new_feedback = _feedback_ids(cur) - (set() if first else _feedback_ids(prev))
        if changes and (first or prev.get("decision") != "CHANGES_REQUESTED" or new_feedback):
            ids = sorted(str(i) for i in _feedback_ids(cur) if i is not None)
            add("changes_requested", key=f":{','.join(ids)}" if ids else None, reviewers=requesters(cur))
        if (not first and prev.get("head") != cur.get("head") and
                prev.get("decision") == "CHANGES_REQUESTED"):
            pending = set(cur.get("requested") or [])
            reviewers = [r for r in requesters(cur) if r not in pending]
            if reviewers:
                add("pushed_after_changes_requested", reviewers=reviewers)
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
