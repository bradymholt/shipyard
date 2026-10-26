// What each PR needs from you: review state, next steps, lanes, and sort order.
import { state } from "./base.js";
import { mergeMergedLists, prKey } from "./data.js";

export function reviewRequestedForViewer(pr) {
  return (pr.reviewRequests?.nodes || []).some((r) => r.requestedReviewer?.login === state.viewer);
}

// GitHub has no diff comment outside a review: "Add single comment" still creates a
// review with state COMMENTED, so reviewed-by:@me matches PRs you merely chimed in
// on. A COMMENTED review only counts when onBehalfOf names a team, which GitHub sets
// just for a review submitted to fulfill that team's request, or when an earlier
// approval or changes request of yours still stands.
function viewerGaveVerdict(pr) {
  const verdict = pr.viewerLatestReview?.state;
  if (verdict === "COMMENTED")
    return (pr.viewerLatestReview?.onBehalfOf?.nodes || []).length > 0 ||
      (pr.latestOpinionatedReviews?.nodes || []).some((r) => r.author?.login === state.viewer);
  return verdict === "APPROVED" || verdict === "CHANGES_REQUESTED" || verdict === "DISMISSED";
}

export function reviewItem(pr) {
  if (pr.author?.login === state.viewer) return false;
  if (reviewRequestedForViewer(pr)) return true;
  // viewerLatestReview only comes back on the reviewed-by fetch, so check that copy.
  const reviewed = state.reviewedPrs.find((r) => prKey(r) === prKey(pr));
  return !!reviewed && viewerGaveVerdict(reviewed);
}

export const reviewedMergedItems = () => state.reviewedMerged.filter((pr) =>
  pr.author?.login !== state.viewer && viewerGaveVerdict(pr)
);

// Repos with a "dismiss stale reviews" ruleset void the review on a push; without one
// the review still stands, so new commits alone don't put the PR back on the reviewer.
function viewerReviewDismissed(pr) {
  return pr.viewerLatestReview?.state === "DISMISSED";
}

export function hasNewCommitsSinceReview(pr) {
  const reviewedCommit = pr.viewerLatestReview?.commit?.oid;
  const currentCommit = pr.commits?.nodes?.[0]?.commit?.oid;
  return !!(reviewedCommit && currentCommit && reviewedCommit !== currentCommit);
}

export function matchesFilter(pr) {
  if (state.filter === "mine" && pr.author?.login !== state.viewer) return false;
  if (state.filter === "review" && !reviewItem(pr)) return false;
  return true;
}

export function ciOk(pr) {
  const ci = pr.commits?.nodes?.[0]?.commit?.statusCheckRollup?.state;
  return !ci || ci === "SUCCESS";
}

// Ready to merge first, then actionable-by-author, then unassigned, then waiting on a reviewer.
function sortRank(pr) {
  if (pr.reviewDecision === "APPROVED") return ciOk(pr) ? 0 : 1;
  if (pr.reviewDecision === "CHANGES_REQUESTED") return 1;
  return (pr.reviewRequests?.nodes || []).length > 0 ? 3 : 2;
}

function reviewSortRank(pr) {
  if (hasNewCommitsSinceReview(pr)) return 0;
  if (reviewRequestedForViewer(pr)) return 1;
  return 2;
}

export function sortComparator() {
  if (state.sort === "priority")
    return (a, b) => {
      const rank = state.filter === "review" ? reviewSortRank : sortRank;
      return rank(a) - rank(b) || new Date(b.updatedAt) - new Date(a.updatedAt);
    };
  if (state.sort === "created")
    return (a, b) => new Date(b.createdAt) - new Date(a.createdAt);
  return (a, b) => new Date(b.updatedAt) - new Date(a.updatedAt);
}

export function buildStacks(prs) {
  const branchKey = (pr, ref) => `${pr.repository?.nameWithOwner || ""}#${ref}`;
  const byHead = new Map();
  for (const pr of prs) if (!byHead.has(branchKey(pr, pr.headRefName))) byHead.set(branchKey(pr, pr.headRefName), pr);
  const children = new Map();
  const parents = new Map();
  const roots = [];
  for (const pr of prs) {
    const parent = byHead.get(branchKey(pr, pr.baseRefName));
    if (parent && parent !== pr) {
      if (!children.has(parent.number)) children.set(parent.number, []);
      children.get(parent.number).push(pr);
      parents.set(pr.number, parent);
    } else {
      roots.push(pr);
    }
  }
  return { roots, children, parents };
}

// Board lanes: 0 Working (incl. drafts), 1 Waiting (on reviewer), 2 Ready (approved).
export function laneOf(pr) {
  if (pr.isDraft) return 0;
  if (pr.reviewDecision === "APPROVED") return 2;
  if ((pr.reviewRequests?.nodes || []).length > 0) return 1;
  return 0;
}

export function reviewLaneOf(pr) {
  return reviewRequestedForViewer(pr) || viewerReviewDismissed(pr) ? 0 : 1;
}

// A card gets one chip, only when there is a next step. The companion decides every
// one, so a page without it shows none.
const FAILING_CONCLUSIONS = new Set(["FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"]);

export const companionPr = (pr) => state.companionState?.prs?.[prKey(pr)] || null;

export const agentName = () => state.agent?.name || "your agent";

export const rollupState = (pr) => pr.commits?.nodes?.[0]?.commit?.statusCheckRollup?.state;

export function failingChecks(pr) {
  const nodes = pr.commits?.nodes?.[0]?.commit?.statusCheckRollup?.contexts?.nodes || [];
  return nodes
    .filter((c) => c.__typename === "CheckRun" ? FAILING_CONCLUSIONS.has(c.conclusion) : c.state === "FAILURE" || c.state === "ERROR")
    .map((c) => ({ name: c.name || c.context || "check", url: c.detailsUrl || c.targetUrl }));
}

// The Reviews tab's copy of a PR carries viewerLatestReview; the targeted copy doesn't.
const reviewedCopy = (pr) => state.reviewedPrs.find((r) => prKey(r) === prKey(pr)) || pr;

const reviewRequested = (pr, login) => (pr.reviewRequests?.nodes || []).some((n) => n.requestedReviewer?.login === login);

export const REVIEW_STATES = { approve: "APPROVED", APPROVE: "APPROVED", comment_unresolved: "COMMENTED",
  COMMENT: "COMMENTED", REQUEST_CHANGES: "CHANGES_REQUESTED" };

// Show a re-request right away, so the card moves to Waiting before GitHub's data catches up.
export function markRequested(pr, reviewers) {
  const added = (reviewers || []).filter((r) => !reviewRequested(pr, r)).map((r) => ({ requestedReviewer:
    r.startsWith("team:") ? { __typename: "Team", name: r.slice(5) } : { __typename: "User", login: r } }));
  pr.reviewRequests = { nodes: [...(pr.reviewRequests?.nodes || []), ...added] };
}

// Likewise for a review you just submitted, so the card moves to Review submitted.
export function markReviewed(pr, reviewState) {
  const key = prKey(pr);
  const review = { state: reviewState, submittedAt: new Date().toISOString(),
    commit: { oid: pr.headRefOid }, onBehalfOf: { nodes: [] } };
  let reviewed = state.reviewedPrs.find((p) => prKey(p) === key);
  if (!reviewed) state.reviewedPrs = [...state.reviewedPrs, (reviewed = { ...pr })];
  for (const copy of new Set([pr, reviewed])) {
    copy.reviewRequests = { nodes: (copy.reviewRequests?.nodes || [])
      .filter((n) => n.requestedReviewer?.login !== state.viewer) };
    copy.viewerLatestReview = review;
  }
}

// Likewise for a merge, so the card drops to Recently merged without waiting for a sync.
export function markMerged(pr) {
  const key = prKey(pr);
  const mergedAt = new Date().toISOString();
  const mine = state.prs.find((p) => prKey(p) === key);
  const reviewed = state.reviewedPrs.find((p) => prKey(p) === key);
  state.prs = state.prs.filter((p) => p !== mine);
  state.reviewedPrs = state.reviewedPrs.filter((p) => p !== reviewed);
  if (mine) state.merged = mergeMergedLists([{ ...mine, mergedAt }], state.merged);
  if (reviewed) state.reviewedMerged = mergeMergedLists([{ ...reviewed, mergedAt }], state.reviewedMerged);
}

// The companion works out every next step; a merged PR keeps only an unanswered approval.
export function companionSteps(pr) {
  const steps = companionPr(pr)?.steps || [];
  return pr.mergedAt ? steps.filter((s) => s.approval) : steps;
}

export function nextChip(pr) {
  const cp = companionPr(pr);
  if (cp?.chip?.kind === "snoozed") return { ...cp.chip, tone: "muted" };
  const chip = companionSteps(pr)[0];
  if (chip) return chip;
  if (cp?.chip?.kind === "nudge") return { ...cp.chip, tone: "gold", optional: true };
  if (cp?.chip) return { ...cp.chip, tone: "muted" };
  return null;
}

export const needsYou = (pr) => {
  const chip = nextChip(pr);
  return !!chip && chip.tone !== "muted" && !chip.optional;
};

const allPrs = () => [...state.prs, ...state.reviewedPrs, ...state.merged, ...state.reviewedMerged];

export const findPr = (key) => allPrs().find((pr) => prKey(pr) === key);

export const isReviewPr = (pr) => pr.author?.login !== state.viewer;

export function stageOf(pr) {
  if (pr.mergedAt) return "Merged";
  if (isReviewPr(pr)) return reviewLaneOf(pr) === 0 ? "Waiting on you" : "Review submitted";
  return pr.isDraft ? "Draft" : ["Working", "Waiting", "Ready"][laneOf(pr)];
}
