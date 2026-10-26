// The PR lists for the current view, merged history, and how fresh they are.
import { $, state } from "./base.js";
import { githubAge } from "./html.js";
import { companionCall, companionGet, getCurrentRepo } from "./server.js";

export function resetData() {
  state.mergedHistory = { mine: [], review: [] };
  state.mergedCursor = undefined;
  state.reviewedMergedCursor = undefined;
  state.mergedExtra = 0;
  applyServerPrs();
}

// Past the auto-refresh interval, a refresh was missed and what's on screen is old.
const STALE_AFTER = 5 * 60 * 1000;

export const lastPollAt = () => Date.parse(state.companionState?.status?.lastPoll || "") || 0;

// Why the server's last poll of GitHub failed, if it did; lastPoll stays at the last success.
export function githubProblem() {
  const github = state.companionState?.status?.github;
  return github && github !== "ok" && github !== "starting" ? github : null;
}

export function updateGitHubStatus() {
  const el = $("#last-updated");
  const updatedAt = lastPollAt();
  const problem = githubProblem();
  const stale = !problem && !!updatedAt && Date.now() - updatedAt > STALE_AFTER;
  const status = $("#github-status");
  status.classList.toggle("hidden", state.refreshing || (!updatedAt && !problem));
  status.classList.toggle("failed", !!problem);
  status.classList.toggle("stale", stale);
  $("#load-bar").classList.toggle("hidden", !state.refreshing);
  el.textContent = problem
    ? updatedAt ? `Refresh failed · ${githubAge(updatedAt)}` : "Refresh failed"
    : updatedAt ? `Updated ${githubAge(updatedAt)}` : "";
  el.title = problem || "";
  renderStaleBanner();
}

// A failed poll on top of earlier data would otherwise look like live data:
// the board renders normally and only the sub-bar age hints that nothing updated.
function renderStaleBanner() {
  const banner = $("#stale-banner");
  const problem = githubProblem();
  const show = !!problem && state.targetedLoaded;
  banner.classList.toggle("hidden", !show);
  if (!show) return;
  $("#stale-headline").textContent = /\(401\)/.test(problem)
    ? "Not updating — GitHub rejected Shipyard's gh token"
    : "Not updating — the last GitHub refresh failed";
  const updatedAt = lastPollAt();
  $("#stale-detail").textContent = `${updatedAt ? `Showing data from ${githubAge(updatedAt)}. ` : ""}${problem}`;
}

export const ownerType = (login) => state.ownerTypes[login];

// Merged history has no date cutoff: a few show at first, and each "Show more"
// reveals another batch, paging the server's search further back whenever the local list runs out.
export const MERGED_INITIAL = 3;

export const MERGED_STEP = 10;

// A page can be spent entirely on merges that get filtered out, so walk a few.
export const MERGED_MAX_PAGES_PER_CLICK = 4;


const byMergedAt = (prs) => prs.filter((pr) => pr.mergedAt)
  .sort((a, b) => new Date(b.mergedAt) - new Date(a.mergedAt));

export const mergeMergedLists = (...lists) => {
  const seen = new Set();
  const out = [];
  for (const pr of lists.flat()) {
    const key = prKey(pr);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(pr);
  }
  return byMergedAt(out);
};

function requireTarget() {
  const target = getCurrentRepo();
  if (!target) throw new Error("No repo, org, or user selected. Open settings (⚙) and add one.");
  return target;
}

// A view is one or more comma-separated targets (repos, orgs, or users).
export const viewTargets = (view) => view.split(",").map((t) => t.trim()).filter(Boolean);

// Each merged feed's cursor, so "Show more" can resume the server's search.
export const MERGED_SEARCHES = {
  mine: { cursorKey: "mergedCursor" },
  review: { cursorKey: "reviewedMergedCursor" },
};

export async function fetchMergedPage(kind, after) {
  const params = new URLSearchParams({ view: requireTarget(), kind });
  if (after) params.set("after", after);
  const data = await companionGet(`companion/merged?${params}`);
  Object.assign(state.ownerTypes, data.owners);
  return data;
}

// Sort the server's PRs into the page's lists. The server polls every PR you're
// involved in, so a view only narrows them.
export function applyServerPrs() {
  const cs = state.companionState;
  state.targetedLoaded = !!cs?.status?.lastPoll;
  if (!state.targetedLoaded) return;
  state.viewer = cs.viewer;
  $("#viewer-info").textContent = state.viewer ? `Signed in as ${state.viewer}` : "";
  const prs = [], reviewed = [], merged = [], reviewedMerged = [];
  for (const [key, cp] of Object.entries(cs.prs || {})) {
    if (!cp.pr || !inScope(key)) continue;
    const pr = { ...cp.pr };
    const lists = cp.lists || [];
    if (pr.state === "OPEN") {
      if (lists.includes("mine") || lists.includes("requested")) prs.push(pr);
      if (lists.includes("reviewed")) reviewed.push(pr);
    } else if (pr.mergedAt) {
      if (pr.author?.login === state.viewer) merged.push(pr);
      else if (pr.viewerLatestReview) reviewedMerged.push(pr);
    }
  }
  const byUpdated = (a, b) => (b.updatedAt || "").localeCompare(a.updatedAt || "");
  state.prs = prs.sort(byUpdated);
  state.reviewedPrs = reviewed.sort(byUpdated);
  const inView = (prs) => prs.filter((pr) => inScope(prKey(pr)));
  state.merged = mergeMergedLists(merged, inView(state.recentMerged.mine), state.mergedHistory.mine);
  state.reviewedMerged = mergeMergedLists(reviewedMerged, inView(state.recentMerged.review), state.mergedHistory.review);
}

export const prKey = (pr) => `${pr.repository?.nameWithOwner || ""}#${pr.number}`;

function inScope(key) {
  const repo = key.split("#")[0].toLowerCase();
  return viewTargets(getCurrentRepo() || "").some((t) =>
    t.includes("/") ? t.toLowerCase() === repo : repo.split("/")[0] === t.toLowerCase());
}

// The server polls GitHub and searches merged history on its own; this asks it to do both now.
export function refreshNow() {
  if (!getCurrentRepo()) return;
  state.refreshing = true;
  updateGitHubStatus();
  companionCall("companion/check-now", { merged: true }).catch(() => {
    state.refreshing = false;
    updateGitHubStatus();
  });
}
