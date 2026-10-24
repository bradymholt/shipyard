"""GitHub polling: one search per list, then a detail query for each PR whose light
signature (updatedAt, head, checks, review decision, draft) moved. Check results don't
bump a PR's updatedAt, which is why the signature carries them too.

Each PR becomes a plain snapshot dict; events.py diffs two of them."""

SEARCHES = {
    "mine": "is:pr is:open archived:false author:@me",
    "requested": "is:pr is:open archived:false review-requested:@me",
    "reviewed": "is:pr is:open archived:false reviewed-by:@me -author:@me",
}

SEARCH_QUERY = """
query($q: String!, $after: String) {
  viewer { login }
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      id updatedAt isDraft reviewDecision headRefOid
      commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
    } }
  }
}"""

DETAIL_FIELDS = """
  id number title url state isDraft createdAt updatedAt mergedAt closedAt body
  headRefName headRefOid baseRefName additions deletions changedFiles
  repository { nameWithOwner }
  author { login }
  reviewDecision mergeable
  autoMergeRequest { enabledAt }
  viewerLatestReview { databaseId state submittedAt commit { oid } }
  reviewRequests(first: 20) { nodes { requestedReviewer {
    __typename ... on User { login } ... on Team { slug } } } }
  latestReviews(first: 20) { nodes { databaseId state submittedAt body author { login } commit { oid } } }
  reviewThreads(first: 50) { nodes {
    id isResolved isOutdated path line
    comments(first: 20) { nodes { databaseId body createdAt diffHunk author { login } } }
  } }
  commits(last: 1) { nodes { commit {
    oid committedDate
    statusCheckRollup { state contexts(first: 50) { nodes {
      __typename
      ... on CheckRun { name conclusion status detailsUrl databaseId
                        checkSuite { workflowRun { databaseId } } }
      ... on StatusContext { context state targetUrl }
    } } }
  } } }
  timelineItems(last: 40, itemTypes: [REVIEW_REQUESTED_EVENT, READY_FOR_REVIEW_EVENT]) { nodes {
    __typename
    ... on ReviewRequestedEvent { createdAt requestedReviewer {
      __typename ... on User { login } ... on Team { slug } } }
    ... on ReadyForReviewEvent { createdAt }
  } }
"""

DETAIL_QUERY = f"""
query($ids: [ID!]!) {{
  nodes(ids: $ids) {{ ... on PullRequest {{ {DETAIL_FIELDS} }} }}
}}"""

FAILING = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
DETAIL_BATCH = 10
HUNK_LINES = 6


def hunk_tail(hunk):
    """The last few lines of a review comment's diff hunk, which ends at the commented line."""
    lines = [l for l in (hunk or "").splitlines() if not l.startswith("@@")]
    return "\n".join(lines[-HUNK_LINES:]) or None


def signature(node):
    commit = ((node.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}
    rollup = (commit.get("statusCheckRollup") or {}).get("state")
    return "|".join(str(v) for v in (node.get("updatedAt"), node.get("headRefOid"), rollup,
                                     node.get("reviewDecision"), node.get("isDraft")))


def reviewer_name(rr):
    rr = rr or {}
    return rr.get("login") or (f"team:{rr['slug']}" if rr.get("slug") else None)


def snapshot(node, viewer):
    """Flatten a PullRequest node into what events and rules need."""
    repo = node["repository"]["nameWithOwner"]
    commit = ((node.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}
    rollup = commit.get("statusCheckRollup") or {}
    failing = []
    for ctx in (rollup.get("contexts") or {}).get("nodes") or []:
        if ctx.get("__typename") == "CheckRun" and ctx.get("conclusion") in FAILING:
            failing.append({
                "name": ctx.get("name"), "url": ctx.get("detailsUrl"), "job_id": ctx.get("databaseId"),
                "run_id": (((ctx.get("checkSuite") or {}).get("workflowRun")) or {}).get("databaseId"),
            })
        elif ctx.get("__typename") == "StatusContext" and ctx.get("state") in ("FAILURE", "ERROR"):
            failing.append({"name": ctx.get("context"), "url": ctx.get("targetUrl"),
                            "job_id": None, "run_id": None})
    requested = [n for n in (reviewer_name(r.get("requestedReviewer"))
                             for r in (node.get("reviewRequests") or {}).get("nodes") or []) if n]
    requested_at, ready_at = {}, None
    for item in (node.get("timelineItems") or {}).get("nodes") or []:
        if item.get("__typename") == "ReviewRequestedEvent":
            name = reviewer_name(item.get("requestedReviewer"))
            if name:
                requested_at[name] = max(requested_at.get(name, ""), item.get("createdAt") or "")
        elif item.get("__typename") == "ReadyForReviewEvent":
            ready_at = item.get("createdAt")
    author = (node.get("author") or {}).get("login")
    reviews = []
    for r in (node.get("latestReviews") or {}).get("nodes") or []:
        login = (r.get("author") or {}).get("login")
        if login and login != author:
            reviews.append({"id": r.get("databaseId"), "state": r.get("state"), "author": login,
                            "submitted": r.get("submittedAt"), "body": (r.get("body") or "")[:2000],
                            "commit": (r.get("commit") or {}).get("oid")})
    mine = node.get("viewerLatestReview")
    if mine and mine.get("state") == "PENDING":
        mine = None
    threads = []
    for t in (node.get("reviewThreads") or {}).get("nodes") or []:
        threads.append({
            "id": t.get("id"), "resolved": bool(t.get("isResolved")), "outdated": bool(t.get("isOutdated")),
            "path": t.get("path"), "line": t.get("line"),
            "hunk": hunk_tail(((t.get("comments") or {}).get("nodes") or [{}])[0].get("diffHunk")),
            "comments": [{"id": c.get("databaseId"), "author": (c.get("author") or {}).get("login"),
                          "body": (c.get("body") or "")[:2000], "created": c.get("createdAt")}
                         for c in (t.get("comments") or {}).get("nodes") or []],
        })
    return {
        "key": f"{repo}#{node['number']}",
        "id": node["id"],
        "repo": repo,
        "number": node["number"],
        "title": node.get("title") or "",
        "url": node.get("url"),
        "state": node.get("state"),
        "draft": bool(node.get("isDraft")),
        "author": author,
        "viewer": viewer,
        "mine": author == viewer,
        "branch": node.get("headRefName"),
        "base": node.get("baseRefName"),
        "head": node.get("headRefOid"),
        "created": node.get("createdAt"),
        "updated": node.get("updatedAt"),
        "merged_at": node.get("mergedAt"),
        "body": (node.get("body") or "")[:4000],
        "additions": node.get("additions"),
        "deletions": node.get("deletions"),
        "changed_files": node.get("changedFiles"),
        "decision": node.get("reviewDecision"),
        "auto_merge": bool(node.get("autoMergeRequest")),
        "mergeable": node.get("mergeable"),
        "requested": requested,
        "requested_me": viewer in requested,
        "requested_at": requested_at,
        "ready_at": ready_at,
        "reviews": reviews,
        "my_review": ({"id": mine.get("databaseId"), "state": mine.get("state"),
                       "submitted": mine.get("submittedAt"),
                       "commit": (mine.get("commit") or {}).get("oid")} if mine else None),
        "threads": threads,
        "ci": rollup.get("state"),
        "failing": failing,
        "last_commit_at": commit.get("committedDate"),
    }


class Poller:
    def __init__(self, github):
        self.github = github
        self.signatures = {}  # PR node id -> last light signature

    def search(self, q):
        nodes, viewer, after = [], None, None
        for _ in range(4):  # 200 PRs is plenty for one person's lists
            data = self.github.graphql(SEARCH_QUERY, {"q": q, "after": after})
            viewer = data["viewer"]["login"]
            nodes += [n for n in data["search"]["nodes"] if n and n.get("id")]
            page = data["search"]["pageInfo"]
            if not page["hasNextPage"]:
                break
            after = page["endCursor"]
        return viewer, nodes

    def details(self, ids, viewer):
        out = []
        ids = list(ids)
        for i in range(0, len(ids), DETAIL_BATCH):
            data = self.github.graphql(DETAIL_QUERY, {"ids": ids[i:i + DETAIL_BATCH]})
            out += [snapshot(n, viewer) for n in data["nodes"] if n and n.get("number")]
        return out

    def poll(self, known_open):
        """(viewer, changed snapshots, ids still open). `known_open` is the set of node
        ids the caller holds as open; any that dropped out of every list are re-read so a
        merge or close is seen."""
        viewer, light = None, {}
        for q in SEARCHES.values():
            viewer, nodes = self.search(q)
            for n in nodes:
                light[n["id"]] = n
        changed = [i for i, n in light.items() if self.signatures.get(i) != signature(n)]
        gone = [i for i in known_open if i not in light]
        snaps = self.details(changed + gone, viewer) if viewer else []
        for i in changed:
            self.signatures[i] = signature(light[i])
        for i in gone:
            self.signatures.pop(i, None)
        return viewer, snaps, set(light)
