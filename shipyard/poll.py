"""GitHub polling: one search per list, then a detail query for each PR whose light
signature (updatedAt, head, checks, review decision, draft) moved. Check results don't
bump a PR's updatedAt, which is why the signature carries them too.

Each PR becomes a plain snapshot dict; events.py diffs two of them."""
import concurrent.futures
import hashlib
from pathlib import Path

from .config import view_targets

# A change to how PRs are read or shaped must re-read them all, so saved memory carries this.
VERSION = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]

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
      id updatedAt isDraft reviewDecision headRefOid mergeable
      commits(last: 1) { nodes { commit { statusCheckRollup { state contexts {
        checkRunCountsByState { state count } statusContextCountsByState { state count } } } } } }
    } }
  }
}"""

DETAIL_FIELDS = """
  id number title url state isDraft createdAt updatedAt mergedAt closedAt body
  headRefName headRefOid baseRefName additions deletions changedFiles
  repository { nameWithOwner }
  author { __typename login avatarUrl }
  reviewDecision mergeable
  labels(first: 10) { nodes { name color } }
  viewerCanEnableAutoMerge viewerCanDisableAutoMerge
  comments(last: 10) { totalCount nodes { createdAt author { login } } }
  autoMergeRequest { enabledAt }
  viewerLatestReview { databaseId state submittedAt commit { oid } onBehalfOf(first: 1) { nodes { slug } } }
  reviewRequests(first: 20) { nodes { requestedReviewer {
    __typename ... on User { login avatarUrl } ... on Team { slug name } } } }
  latestReviews(first: 20) { nodes { databaseId state submittedAt body author { login avatarUrl } commit { oid } } }
  latestOpinionatedReviews(first: 20) { nodes { state author { login } } }
  pendingReviews: reviews(first: 10, states: [PENDING]) { nodes { author { login } } }
  reviewThreads(first: 50) { nodes {
    id isResolved isOutdated path line
    comments(first: 20) { nodes { databaseId body createdAt diffHunk author { login } } }
    latest: comments(last: 1) { nodes { createdAt author { login } } }
  } }
  commits(last: 1) { nodes { commit {
    oid committedDate
    statusCheckRollup { state contexts(first: 50) {
      totalCount checkRunCountsByState { state count } statusContextCountsByState { state count }
      nodes {
      __typename
      ... on CheckRun { name conclusion status detailsUrl databaseId
                        checkSuite { workflowRun { databaseId workflow { name } } } }
      ... on StatusContext { context state targetUrl }
    } } }
  } } }
  timelineItems(last: 40, itemTypes: [REVIEW_REQUESTED_EVENT, READY_FOR_REVIEW_EVENT, REVIEW_DISMISSED_EVENT]) { nodes {
    __typename
    ... on ReviewRequestedEvent { createdAt requestedReviewer {
      __typename ... on User { login } ... on Team { slug } } }
    ... on ReadyForReviewEvent { createdAt }
    ... on ReviewDismissedEvent { previousReviewState review { databaseId } }
  } }
"""

DETAIL_QUERY = f"""
query($ids: [ID!]!) {{
  nodes(ids: $ids) {{ ... on PullRequest {{ {DETAIL_FIELDS} }} }}
}}"""

# Merged history is per view, so the page asks for it rather than the poll keeping it.
MERGED_SEARCHES = {
    "mine": "is:merged author:@me",
    "review": "is:merged reviewed-by:@me -author:@me",
}
MERGED_PAGE_SIZE = 10  # a view's Show more reveals 10 at a time
RECENT_MERGED = 25  # your latest merges across every repo, which the page narrows to a view

MERGED_QUERY = """
query($q: String!, $after: String, $first: Int!) {
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest {
      id number title url state isDraft createdAt updatedAt mergedAt
      headRefName baseRefName headRefOid additions deletions changedFiles
      autoMergeRequest { enabledAt }
      labels(first: 10) { nodes { name color } }
      viewerCanEnableAutoMerge viewerCanDisableAutoMerge
      repository { nameWithOwner }
      author { login avatarUrl }
      reviewDecision
      comments(last: 10) { totalCount nodes { createdAt author { login } } }
      viewerLatestReview { state submittedAt commit { oid } onBehalfOf(first: 1) { nodes { slug } } }
      reviewRequests(first: 10) { nodes { requestedReviewer {
        __typename ... on User { login avatarUrl } ... on Team { slug name } } } }
      latestReviews(first: 10) { nodes { state author { login avatarUrl } commit { oid } } }
      latestOpinionatedReviews(first: 20) { nodes { state author { login } } }
      pendingReviews: reviews(first: 10, states: [PENDING]) { nodes { author { login } } }
      reviewThreads(first: 20) { nodes { isResolved
        comments(first: 1) { nodes { author { login } } }
        latest: comments(last: 1) { nodes { createdAt author { login } } } } }
      commits(last: 1) { nodes { commit { oid statusCheckRollup { state contexts(first: 30) { nodes {
        __typename
        ... on CheckRun { name conclusion detailsUrl }
        ... on StatusContext { context state targetUrl }
      } } } } } }
    } }
  }
}"""

# Merges for the Insights charts: only the dates, so a few pages cover months.
INSIGHTS_WEEKS = 12
INSIGHTS_MAX_PAGES = 5
INSIGHTS_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { createdAt mergedAt } }
  }
}"""

# What the page's sidebar lists under Activity, read when it opens.
TIMELINE_QUERY = """
query($id: ID!) { node(id: $id) { ... on PullRequest {
  timelineItems(last: 30, itemTypes: [PULL_REQUEST_COMMIT, PULL_REQUEST_REVIEW, REVIEW_REQUESTED_EVENT,
      ISSUE_COMMENT, READY_FOR_REVIEW_EVENT, CONVERT_TO_DRAFT_EVENT, MERGED_EVENT,
      HEAD_REF_FORCE_PUSHED_EVENT, REVIEW_DISMISSED_EVENT]) { nodes {
    __typename
    ... on PullRequestCommit { commit { messageHeadline committedDate author { user { login } name } } }
    ... on PullRequestReview { state submittedAt author { login } }
    ... on ReviewRequestedEvent { createdAt actor { login } requestedReviewer { ... on User { login } ... on Team { name } } }
    ... on IssueComment { createdAt author { login } }
    ... on ReadyForReviewEvent { createdAt actor { login } }
    ... on ConvertToDraftEvent { createdAt actor { login } }
    ... on MergedEvent { createdAt actor { login } }
    ... on HeadRefForcePushedEvent { createdAt actor { login } }
    ... on ReviewDismissedEvent { createdAt actor { login } }
  } }
} } }"""

OWNER_QUERY = "query($login: String!) { repositoryOwner(login: $login) { __typename } }"

FAILING = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
RUNNING = {"IN_PROGRESS", "QUEUED", "PENDING", "WAITING", "EXPECTED"}
DETAIL_BATCH = 10
HUNK_LINES = 6


def hunk_tail(hunk):
    """The last few lines of a review comment's diff hunk, which ends at the commented line."""
    lines = [l for l in (hunk or "").splitlines() if not l.startswith("@@")]
    return "\n".join(lines[-HUNK_LINES:]) or None


def checks_running(rollup):
    """How many checks are still queued or running; the rollup reads FAILURE once one fails."""
    contexts = (rollup or {}).get("contexts") or {}
    counts = (contexts.get("checkRunCountsByState") or []) + (contexts.get("statusContextCountsByState") or [])
    return sum(c.get("count") or 0 for c in counts if c.get("state") in RUNNING)


def signature(node):
    commit = ((node.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}
    rollup = commit.get("statusCheckRollup") or {}
    return "|".join(str(v) for v in (node.get("updatedAt"), node.get("headRefOid"), rollup.get("state"),
                                     checks_running(rollup), node.get("reviewDecision"), node.get("isDraft"),
                                     node.get("mergeable")))


def reviewer_name(rr):
    rr = rr or {}
    return rr.get("login") or (f"team:{rr['slug']}" if rr.get("slug") else None)


PAGE_FIELDS = ("id", "number", "title", "url", "state", "isDraft", "createdAt", "updatedAt", "mergedAt",
               "headRefName", "baseRefName", "headRefOid", "additions", "deletions", "changedFiles",
               "autoMergeRequest", "labels", "viewerCanEnableAutoMerge", "viewerCanDisableAutoMerge",
               "repository", "author", "reviewDecision", "reviewRequests", "viewerLatestReview",
               "latestOpinionatedReviews", "pendingReviews")
PAGE_CONTEXT_FIELDS = ("__typename", "name", "conclusion", "detailsUrl", "context", "state", "targetUrl")


def is_failing(ctx):
    if ctx.get("__typename") == "CheckRun":
        return ctx.get("conclusion") in FAILING
    return ctx.get("state") in ("FAILURE", "ERROR")


def current_checks(rollup):
    """The rollup's checks less any run a later run of the same workflow job replaced."""
    nodes = ((rollup or {}).get("contexts") or {}).get("nodes") or []

    def key(ctx):
        if ctx.get("__typename") != "CheckRun" or not ctx.get("databaseId"):
            return None
        workflow = (((ctx.get("checkSuite") or {}).get("workflowRun") or {}).get("workflow") or {}).get("name")
        return workflow, ctx.get("name")

    latest = {}
    for ctx in nodes:
        k = key(ctx)
        if k and (k not in latest or ctx["databaseId"] > latest[k]["databaseId"]):
            latest[k] = ctx
    return [c for c in nodes if not key(c) or latest[key(c)] is c]


def ci_state(rollup):
    """GitHub's rollup state, except that it keeps failing on a run a later one replaced, as when
    adding a label reruns a label check."""
    rollup = rollup or {}
    state, contexts = rollup.get("state"), rollup.get("contexts") or {}
    if state not in ("FAILURE", "ERROR") or len(contexts.get("nodes") or []) < (contexts.get("totalCount") or 0):
        return state
    current = current_checks(rollup)
    replaced = [c for c in contexts.get("nodes") or [] if not any(c is k for k in current)]
    if any(is_failing(c) for c in current) or not any(is_failing(c) for c in replaced):
        return state
    running = any((c.get("status") if c.get("__typename") == "CheckRun" else c.get("state")) in RUNNING
                  for c in current)
    return "PENDING" if running else "SUCCESS"


def first_author(conn):
    return [{"author": n.get("author")} for n in ((conn or {}).get("nodes") or [])[:1]]


def page_pr(node):
    """The PR in the shape the page's own GitHub queries return, so the page can draw it as is."""
    commit = ((node.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}
    rollup = commit.get("statusCheckRollup")
    comments = node.get("comments") or {}
    return {
        **{k: node.get(k) for k in PAGE_FIELDS},
        "latestReviews": {"nodes": [{"state": r.get("state"), "author": r.get("author"), "commit": r.get("commit")}
                                    for r in (node.get("latestReviews") or {}).get("nodes") or []]},
        "comments": {"totalCount": comments.get("totalCount") or 0},
        "recentComments": {"nodes": comments.get("nodes") or []},
        "reviewThreads": {"nodes": [{"isResolved": bool(t.get("isResolved")),
                                     "opener": {"nodes": first_author(t.get("comments"))},
                                     "comments": {"nodes": [{"author": n.get("author"), "createdAt": n.get("createdAt")}
                                                            for n in ((t.get("latest") or {}).get("nodes") or [])[:1]]}}
                                    for t in (node.get("reviewThreads") or {}).get("nodes") or []]},
        "commits": {"nodes": [{"commit": {"oid": commit.get("oid"), "statusCheckRollup": rollup and {
            "state": ci_state(rollup),
            # The page shows the overall state and names only the failing checks.
            "contexts": {"nodes": [{k: c[k] for k in PAGE_CONTEXT_FIELDS if k in c}
                                   for c in current_checks(rollup) if is_failing(c)]}}}}]},
    }


def snapshot(node, viewer):
    """Flatten a PullRequest node into what events and rules need."""
    repo = node["repository"]["nameWithOwner"]
    commit = ((node.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}
    rollup = commit.get("statusCheckRollup") or {}
    failing = []
    for ctx in current_checks(rollup):
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
    requested_at, ready_at, dismissed_from = {}, None, {}
    for item in (node.get("timelineItems") or {}).get("nodes") or []:
        if item.get("__typename") == "ReviewRequestedEvent":
            name = reviewer_name(item.get("requestedReviewer"))
            if name:
                requested_at[name] = max(requested_at.get(name, ""), item.get("createdAt") or "")
        elif item.get("__typename") == "ReadyForReviewEvent":
            ready_at = item.get("createdAt")
        elif item.get("__typename") == "ReviewDismissedEvent":
            dismissed_from[(item.get("review") or {}).get("databaseId")] = item.get("previousReviewState")
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
        "author_bot": (node.get("author") or {}).get("__typename") == "Bot",
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
                       "dismissed_from": dismissed_from.get(mine.get("databaseId")),
                       "submitted": mine.get("submittedAt"),
                       "commit": (mine.get("commit") or {}).get("oid")} if mine else None),
        "threads": threads,
        "comments": [{"author": (c.get("author") or {}).get("login"), "created": c.get("createdAt")}
                     for c in (node.get("comments") or {}).get("nodes") or []],
        "ci": ci_state(rollup),
        "failing": failing,
        "checks_running": checks_running(rollup),
        "last_commit_at": commit.get("committedDate"),
        "page": page_pr(node),
    }


class Poller:
    def __init__(self, github):
        self.github = github
        self.signatures = {}  # PR node id -> last light signature
        self.owner_types = {}  # login -> "org" | "user"
        self.lists = {}  # PR node id -> names of the SEARCHES it was on in the last poll

    def memory(self):
        return {"version": VERSION, "signatures": self.signatures, "lists": self.lists}

    def restore(self, memory):
        """Pick up where the last process left off, unless this code reads PRs differently."""
        if memory and memory.get("version") == VERSION:
            self.signatures = dict(memory.get("signatures") or {})
            self.lists = dict(memory.get("lists") or {})

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

    def owner_qualifier(self, login):
        """Search needs org: or user: depending on the account type."""
        if login not in self.owner_types:
            owner = self.github.graphql(OWNER_QUERY, {"login": login}).get("repositoryOwner")
            if not owner:
                raise LookupError(f'"{login}" is not a GitHub user or organization, or the token lacks access.')
            self.owner_types[login] = "org" if owner["__typename"] == "Organization" else "user"
        return self.owner_types[login]

    def scope(self, view):
        """Search qualifiers for a view: comma-separated repos, orgs, or users, which GitHub ORs."""
        return " ".join(f"repo:{t}" if "/" in t else f"{self.owner_qualifier(t)}:{t}" for t in view_targets(view))

    def recent_merged(self, kind):
        q = f"is:pr archived:false {MERGED_SEARCHES[kind]} sort:updated-desc"
        search = self.github.graphql(MERGED_QUERY, {"q": q, "after": None, "first": RECENT_MERGED})["search"]
        return [page_pr(n) for n in search["nodes"] if n and n.get("number")]

    def merged(self, view, kind, after=None):
        """One page of a view's merged history (all of yours with no view), newest first."""
        if kind not in MERGED_SEARCHES:
            raise ValueError(f'Unknown merged list "{kind}".')
        scope = f"{self.scope(view)} " if view else ""
        q = f"is:pr archived:false {scope}{MERGED_SEARCHES[kind]} sort:updated-desc"
        search = self.github.graphql(MERGED_QUERY, {"q": q, "after": after or None, "first": MERGED_PAGE_SIZE})["search"]
        page = search["pageInfo"]
        return {"prs": [page_pr(n) for n in search["nodes"] if n and n.get("number")],
                "after": page["endCursor"] if page["hasNextPage"] else None}

    def insights(self, view, since):
        """Your merges and the merges you reviewed since a date, as (created, merged) pairs."""
        scope = f"{self.scope(view)} " if view else ""
        out = {}
        for kind, search in MERGED_SEARCHES.items():
            q = f"is:pr archived:false {scope}{search} merged:>={since}"
            rows, after = [], None
            for _ in range(INSIGHTS_MAX_PAGES):
                found = self.github.graphql(INSIGHTS_QUERY, {"q": q, "after": after})["search"]
                rows += [{"created": n["createdAt"], "merged": n["mergedAt"]}
                         for n in found["nodes"] if n and n.get("mergedAt")]
                if not found["pageInfo"]["hasNextPage"]:
                    break
                after = found["pageInfo"]["endCursor"]
            out[kind] = rows
        return out

    def timeline(self, node_id):
        if not node_id:
            raise ValueError("Which PR?")
        node = self.github.graphql(TIMELINE_QUERY, {"id": node_id}).get("node")
        if not node:
            raise LookupError("GitHub has no such PR, or the token can't see it.")
        return node

    def details(self, ids, viewer):
        ids = list(ids)
        batches = [ids[i:i + DETAIL_BATCH] for i in range(0, len(ids), DETAIL_BATCH)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            pages = list(pool.map(lambda b: self.github.graphql(DETAIL_QUERY, {"ids": b}), batches))
        return [snapshot(n, viewer) for data in pages for n in data["nodes"] if n and n.get("number")]

    def poll(self, known_open):
        """(viewer, changed snapshots, ids still open). `known_open` is the set of node
        ids the caller holds as open; any that dropped out of every list are re-read so a
        merge or close is seen."""
        viewer, light, lists = None, {}, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(SEARCHES)) as pool:
            found = list(pool.map(self.search, SEARCHES.values()))
        for name, (viewer, nodes) in zip(SEARCHES, found):
            for n in nodes:
                light[n["id"]] = n
                lists.setdefault(n["id"], []).append(name)
        self.lists = lists
        changed = [i for i, n in light.items() if self.signatures.get(i) != signature(n)]
        gone = [i for i in known_open if i not in light]
        snaps = self.details(changed + gone, viewer) if viewer else []
        for i in changed:
            self.signatures[i] = signature(light[i])
        for i in gone:
            self.signatures.pop(i, None)
        return viewer, snaps, set(light)
