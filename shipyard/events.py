"""Named events from the difference between two snapshots of one PR.

derive(prev, cur) is pure: prev is None the first time a PR is seen, and the first
sighting reports conditions that already hold (changes requested, CI red, a review
request) so a PR you open the board on is handled like one that just changed. The
caller dedupes by (PR, event, head SHA), so a re-poll never fires the same event twice.
"""

FAILING = {"FAILURE", "ERROR"}


def _review_ids(snap, state):
    return {r["id"] for r in snap.get("reviews") or [] if r.get("state") == state}


def derive(prev, cur):
    events = []

    def add(name, **data):
        events.append({"name": name, "head": cur.get("head"), "data": data})

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
        new_change_reviews = _review_ids(cur, "CHANGES_REQUESTED") - (
            set() if first else _review_ids(prev, "CHANGES_REQUESTED"))
        if changes and (first or prev.get("decision") != "CHANGES_REQUESTED" or new_change_reviews):
            requesters = [r["author"] for r in cur["reviews"] if r["state"] == "CHANGES_REQUESTED"]
            add("changes_requested", reviewers=requesters)
        if (not first and prev.get("head") != cur.get("head") and
                prev.get("decision") == "CHANGES_REQUESTED"):
            pending = set(cur.get("requested") or [])
            reviewers = [r["author"] for r in cur["reviews"]
                         if r["state"] == "CHANGES_REQUESTED" and r["author"] not in pending]
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
