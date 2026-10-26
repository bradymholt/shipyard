// The board: lanes, cards, and Recently merged.
import { $, listBody, LS, narrowViewport, state } from "./base.js";
import { AGENT_ICONS, agoSpan, BRANCH_ICON, DEFAULT_AGENT_ICON, EMPTY_ICON, esc, fmtTitle, GITHUB_ICON, ICONS, labelColors, LANE_CHEVRON, LAUNCH_ICON, showError, timeAgo } from "./html.js";
import { companionCall, companionPost, companionStatusHtml, getCurrentRepo, worktreeFor } from "./server.js";
import { applyServerPrs, fetchMergedPage, githubProblem, load, MERGED_INITIAL, MERGED_MAX_PAGES_PER_CLICK, MERGED_SEARCHES, MERGED_STEP, mergeMergedLists, ownerType, prKey, viewTargets } from "./data.js";
import { agentName, buildStacks, ciOk, companionPr, hasNewCommitsSinceReview, laneOf, matchesFilter, needsYou, nextChip, reviewedMergedItems, reviewItem, reviewLaneOf, reviewRequestedForViewer, sortComparator } from "./prs.js";
import { openNewTaskDialog } from "./settings.js";
import { closeSidebar, openSidebar, PASS_LABELS, renderSidebar, sessionTarget, sidebarKey } from "./sidebar.js";

const laneEmptyText = (name, reviewBoard) => reviewBoard
  ? (name === "Waiting" ? "You're all caught up" : "Nothing reviewed yet")
  : ({ Working: "Nothing in progress", Waiting: "Nothing awaiting review", Ready: "Nothing ready to merge" }[name] || "Nothing here");

function reviewerHtml(login, avatarUrl, stateClass, stateIcon, title) {
  const avatar = avatarUrl
    ? `<img src="${esc(avatarUrl)}&s=48" alt="${esc(login)}">`
    : `<span class="team-avatar">${esc(login.slice(0, 2).toUpperCase())}</span>`;
  return `<span class="reviewer" title="${esc(title)}">${avatar}<span class="state-dot ${stateClass}">${stateIcon}</span></span>`;
}

function authorAvatar(pr) {
  const login = pr.author?.login || "ghost";
  const avatar = pr.author?.avatarUrl
    ? `<img src="${esc(pr.author.avatarUrl)}&s=48" alt="${esc(login)}">`
    : `<span class="team-avatar">${esc(login.slice(0, 2).toUpperCase())}</span>`;
  return `<span class="reviewer" title="opened by ${esc(login)}">${avatar}</span>`;
}

function buildReviewers(pr) {
  const parts = [];
  const reviewed = new Set();
  for (const r of pr.latestReviews?.nodes || []) {
    if (!r.author || r.author.login === pr.author?.login) continue;
    if (reviewed.has(r.author.login)) continue;
    reviewed.add(r.author.login);
    const map = {
      APPROVED: ["state-approved", ICONS.check, "approved"],
      CHANGES_REQUESTED: ["state-changes", ICONS.changes, "requested changes"],
      COMMENTED: ["state-commented", ICONS.comment, "commented"],
      DISMISSED: ["state-commented", ICONS.dot, "review dismissed"],
      PENDING: ["state-pending", ICONS.clock, "review pending"],
    };
    const [cls, icon, verb] = map[r.state] || map.COMMENTED;
    parts.push(reviewerHtml(r.author.login, r.author.avatarUrl, cls, icon, `${r.author.login} ${verb}`));
  }
  for (const req of pr.reviewRequests?.nodes || []) {
    const rr = req.requestedReviewer;
    if (!rr) continue;
    const login = rr.login || rr.name;
    if (reviewed.has(login)) continue;
    parts.push(reviewerHtml(login, rr.avatarUrl, "state-pending", ICONS.clock, `${login} — review requested`));
  }
  return parts.join("");
}

function reviewBadge(pr) {
  if (pr.reviewDecision === "APPROVED")
    return `<span class="review-badge approved">${ICONS.check} Approved</span>`;
  if (pr.reviewDecision === "CHANGES_REQUESTED")
    return `<span class="review-badge changes">${ICONS.changes} Changes requested</span>`;
  return (pr.reviewRequests?.nodes || []).length > 0
    ? `<span class="review-badge pending">${ICONS.clock} Awaiting review</span>`
    : `<span class="review-badge none">Needs review</span>`;
}

function viewerReviewBadge(pr) {
  if (reviewRequestedForViewer(pr))
    return `<span class="review-badge pending">${ICONS.clock} Review requested</span>`;
  const reviewState = pr.viewerLatestReview?.state;
  if (reviewState === "DISMISSED")
    return `<span class="review-badge pending">${ICONS.dot} Your review was dismissed</span>`;
  if (reviewState === "APPROVED")
    return `<span class="review-badge approved">${ICONS.check} You approved</span>`;
  if (reviewState === "CHANGES_REQUESTED")
    return `<span class="review-badge changes">${ICONS.changes} You requested changes</span>`;
  if (reviewState === "COMMENTED")
    return `<span class="review-badge none">${ICONS.comment} You commented</span>`;
  return `<span class="review-badge none">Review submitted</span>`;
}

function reviewUpdateBadge(pr) {
  return hasNewCommitsSinceReview(pr)
    ? `<span class="review-badge pending">${ICONS.clock} Updated since your review</span>`
    : "";
}

function authorRepliedBadge(pr) {
  const mine = (pr.reviewThreads?.nodes || [])
    .filter((t) => !t.isResolved && t.opener?.nodes?.[0]?.author?.login === state.viewer);
  const replied = mine.filter((t) => t.comments?.nodes?.[0]?.author?.login === pr.author?.login).length;
  const comments = pr.recentComments?.nodes || [];
  const since = [pr.viewerLatestReview?.submittedAt,
    ...comments.filter((c) => c.author?.login === state.viewer).map((c) => c.createdAt)].filter(Boolean).sort().at(-1);
  const commented = !!since && comments.some((c) => c.author?.login === pr.author?.login && c.createdAt > since);
  if (!replied && !commented) return "";
  const count = replied && replied < mine.length ? ` · ${replied} of ${mine.length}` : "";
  const title = [replied && `${pr.author.login} replied last on ${replied} of your ${mine.length} open thread${mine.length === 1 ? "" : "s"}`,
    commented && `${pr.author.login} commented after your review`].filter(Boolean).join("; ");
  return `<span class="review-badge pending" title="${esc(title)}">${ICONS.comment} Author replied${count}</span>`;
}

function pendingReviewBadge(pr) {
  const pending = (pr.pendingReviews?.nodes || []).some((review) => review.author?.login === state.viewer);
  return pending
    ? `<span class="review-badge none" title="You have an unsubmitted review on GitHub">${ICONS.comment} Review in progress</span>`
    : "";
}

export function ciHtml(pr) {
  const rollup = pr.commits?.nodes?.[0]?.commit?.statusCheckRollup;
  if (!rollup) return "";
  const map = {
    SUCCESS: ["success", ICONS.check, "Checks passing"],
    FAILURE: ["failure", ICONS.x, "Checks failing"],
    ERROR: ["failure", ICONS.x, "Checks errored"],
    PENDING: ["pending", ICONS.dot, "Checks running"],
    EXPECTED: ["pending", ICONS.dot, "Checks expected"],
  };
  const [cls, icon, title] = map[rollup.state] || map.PENDING;
  return `<span class="ci ${cls}" title="${title}">${icon}</span>`;
}

export const launchTarget = (checkout) => state.launcher.target === "workspace"
  ? (checkout.workspace || checkout.path)
  : checkout.path;

const launcherUrl = (target) => {
  const encodedPath = String(target).split("/").map(encodeURIComponent).join("/");
  return state.launcher.url.replaceAll("{path}", encodedPath);
};

export async function openInApp(target) {
  if (state.launcher.mode === "url") {
    window.location.href = launcherUrl(target);
    return;
  }
  try {
    const res = await companionPost(`open-app?path=${encodeURIComponent(target)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) showError(data.error || `Open in ${state.launcher.name} failed (${res.status}).`);
  } catch (err) {
    showError(err.message);
  }
}

async function openAgent(path, session) {
  try {
    const res = await companionPost(`open-agent?path=${encodeURIComponent(path)}${session ? `&session=${encodeURIComponent(session)}` : ""}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) showError(data.error || `Open in ${state.agent.name} failed (${res.status}).`);
  } catch (err) {
    showError(err.message);
  }
}

function agentButton(pr) {
  const agent = state.agent;
  const checkout = sessionTarget(pr);
  if (!agent || !checkout) return "";
  const icon = AGENT_ICONS[agent.sessions] || DEFAULT_AGENT_ICON;
  const title = `Open the PR session in ${agent.name} (last active ${timeAgo(checkout.session.updated * 1000)})`;
  if (agent.mode === "command") {
    return `<button class="wt-link wt-agent" data-path="${esc(checkout.path)}" data-session="${esc(checkout.session.id)}" title="${esc(title)}">${icon}</button>`;
  }
  const url = agent.url
    .replaceAll("{session}", encodeURIComponent(checkout.session.id))
    .replaceAll("{path}", String(checkout.path).split("/").map(encodeURIComponent).join("/"));
  return `<a class="wt-link wt-agent" href="${esc(url)}" title="${esc(title)}">${icon}</a>`;
}

export function openCheckoutButton(wt, label = "") {
  const target = launchTarget(wt);
  const inner = label ? `${LAUNCH_ICON}<span>${esc(label)}</span>` : LAUNCH_ICON;
  const mod = wt.main ? " current" : " worktree";  // green for main clone, purple for a worktree
  const appName = state.launcher.name || "configured app";
  const title = wt.main
    ? `This branch is checked out in your main repo — open it in ${appName}`
    : `This branch is checked out in a worktree — open it in ${appName}`;
  return state.launcher.mode === "command"
    ? `<button class="wt-link wt-open${mod}" data-target="${esc(target)}" title="${esc(title)}">${inner}</button>`
    : `<a class="wt-link${mod}" href="${esc(launcherUrl(target))}" title="${esc(title)}">${inner}</a>`;
}

function githubLink(pr) {
  return `<a class="wt-link" href="${esc(pr.url)}" target="_blank" rel="noopener" title="Open on GitHub">${GITHUB_ICON}</a>`;
}

// A merged PR's branch may still be checked out locally, with a session worth
// returning to for follow-up ideas left in review. Offer only what already exists:
// no checking out or moving a branch that's done.
function mergedWorktreeLinks(pr) {
  const wt = worktreeFor(pr);
  const local = wt ? `${agentButton(pr)}${openCheckoutButton(wt)}` : "";
  return `<div class="card-icons"><span class="wt-links">${githubLink(pr)}${local}</span></div>`;
}

function worktreeLinks(pr) {
  // A single "Open" control; where it opens (and how it
  // looks) depends on where the branch is checked out.
  const repoName = pr.repository?.nameWithOwner || "";
  const repo = esc(repoName);
  const branch = esc(pr.headRefName);
  const wt = worktreeFor(pr);

  // Already checked out somewhere → open that checkout directly. Green marks the
  // one that's the main repo's current branch.
  if (wt) {
    return `<span class="wt-links">${githubLink(pr)}${agentButton(pr)}${openCheckoutButton(wt)}</span>`;
  }

  // A review worktree is detached, so it has no branch entry among the pushed worktrees.
  const cp = companionPr(pr);
  if (cp?.path) {
    return `<span class="wt-links">${githubLink(pr)}${agentButton(pr)}${openCheckoutButton({ path: cp.path, workspace: null, main: false })}</span>`;
  }

  // Not checked out anywhere → open by checking the branch out in the main repo
  // (blocked if the main repo has uncommitted changes).
  const mainWt = Object.values(state.worktrees).find((w) => w.repo === repoName && w.main);
  const mainOn = mainWt && mainWt.branch ? ` (on ${mainWt.branch})` : "";
  const mainBtn = mainWt && mainWt.dirty
    ? `<button class="wt-link wt-main blocked" disabled title="${esc(`Can't check out here — your main clone${mainOn} has uncommitted changes. Commit or stash them there first.`)}">${LAUNCH_ICON}</button>`
    : `<button class="wt-link wt-main" data-repo="${repo}" data-branch="${branch}" title="Check out in your main clone and open in ${esc(state.launcher.name)}">${LAUNCH_ICON}</button>`;
  return `<span class="wt-links">${githubLink(pr)}${mainBtn}${agentButton(pr)}</span>`;
}

// Where the owner/repo link goes: on My PRs, your authored PRs; on Reviews, the
// repo's review queue scoped to the PR's column (still waiting on you vs. reviewed).
// Merged items (from Recently merged) carry mergedAt, so scope those to is:closed —
// they're no longer open and wouldn't show under the open filter.
// A dismissed review lands in Waiting but has no matching search qualifier, so it
// falls back to reviewed-by:@me — the one list GitHub will actually return it in.
function repoPullsUrl(pr) {
  const base = `https://github.com/${pr.repository?.nameWithOwner || ""}/pulls`;
  const openState = pr.mergedAt ? "is:closed" : "is:open";
  if (state.filter === "review") {
    const who = !pr.mergedAt && reviewRequestedForViewer(pr) ? "user-review-requested:@me" : "reviewed-by:@me";
    return `${base}?q=${encodeURIComponent(`${openState} is:pr ${who}`)}`;
  }
  if (pr.mergedAt) return `${base}?q=${encodeURIComponent(`is:pr is:closed author:${state.viewer || "@me"}`)}`;
  return state.viewer ? `${base}/${state.viewer}` : base;
}

function mergedRepoPrefix(pr) {
  const repoName = pr.repository?.nameWithOwner;
  return repoName && repoName !== getCurrentRepo()
    ? `<a href="${esc(repoPullsUrl(pr))}" target="_blank" rel="noopener"><strong>${esc(repoName)}</strong></a> ` : "";
}

function mergedCard(pr) {
  return `
    <div class="${cardClasses(pr, "merged")}" data-key="${esc(prKey(pr))}">
      <div class="card-title"><span class="pr-icon merged">${ICONS.merged}</span><span>${fmtTitle(pr.title)}</span></div>
      <div class="card-meta">${mergedRepoPrefix(pr)}<span class="pr-num">#${pr.number}</span> merged ${agoSpan(pr.mergedAt)} ${pr.author?.login !== state.viewer ? authorRepliedBadge(pr) : ""}</div>
      ${mergedWorktreeLinks(pr)}
      ${nextRowHtml(pr)}
    </div>`;
}

const collapsedLanes = new Set((() => {
  try { return JSON.parse(localStorage.getItem("ghpr.collapsedLanes") || "[]"); } catch { return []; }
})());

function toggleLane(key) {
  if (!collapsedLanes.delete(key)) collapsedLanes.add(key);
  try { localStorage.setItem("ghpr.collapsedLanes", JSON.stringify([...collapsedLanes])); } catch { /* storage unavailable */ }
  render();
}

const mergedKind = () => (state.filter === "review" ? "review" : "mine");

const mergedItems = () => (mergedKind() === "review" ? reviewedMergedItems() : state.merged);

const mergedHasMore = () => state[MERGED_SEARCHES[mergedKind()].cursorKey] != null;

function mergedSection(items, renderItem) {
  const shown = items.slice(0, MERGED_INITIAL + state.mergedExtra);
  let out = shown.map(renderItem).join("");
  const actions = state.mergedLoading
    ? `<button type="button" class="merged-more" disabled>Loading…</button>`
    : (items.length > shown.length || mergedHasMore()
        ? `<button type="button" class="merged-more" data-merged="more">Show more</button>` : "")
      + (state.mergedExtra > 0
        ? `<button type="button" class="merged-more" data-merged="less">Show less</button>` : "");
  if (actions) out += `<div class="merged-actions">${actions}</div>`;
  return out;
}

// Reveal another batch, walking the search further back when the local list runs dry.
async function revealMoreMerged() {
  state.mergedExtra += MERGED_STEP;
  const kind = mergedKind();
  const { cursorKey } = MERGED_SEARCHES[kind];
  const want = MERGED_INITIAL + state.mergedExtra;
  if (mergedItems().length >= want || !state[cursorKey] || state.mergedLoading) { render(); return; }
  state.mergedLoading = true;
  render();
  try {
    for (let page = 0; page < MERGED_MAX_PAGES_PER_CLICK; page++) {
      const { prs, after } = await fetchMergedPage(kind, state[cursorKey]);
      state.mergedHistory[kind] = mergeMergedLists(state.mergedHistory[kind], prs);
      state[cursorKey] = after;
      applyServerPrs();
      if (!after || mergedItems().length >= want) break;
    }
  } catch (err) {
    console.warn("Shipyard: loading more merged PRs failed —", err.message);
  } finally {
    state.mergedLoading = false;
    render();
  }
}

function autoMergeChip(pr) {
  const on = !!pr.autoMergeRequest;
  if (on && !pr.viewerCanDisableAutoMerge)
    return `<span class="am-toggle on" title="Auto-merge is on">${ICONS.check} Auto-merge</span>`;
  if (!on && !pr.viewerCanEnableAutoMerge) return "";
  return on
    ? `<button class="am-toggle on" data-pr="${esc(pr.id)}" data-action="disable" title="Auto-merge is on. Click to disable.">${ICONS.check} Auto-merge</button>`
    : `<button class="am-toggle off" data-pr="${esc(pr.id)}" data-action="enable" title="Auto-merge is off. Click to enable.">Auto-merge</button>`;
}

function prCard(pr, parent, showAutoMerge = false, reviewMode = false) {
  const repoName = pr.repository?.nameWithOwner;
  const repoPrefix = repoName && repoName !== getCurrentRepo()
    ? `<a href="${esc(repoPullsUrl(pr))}" target="_blank" rel="noopener">${esc(repoName)}</a> `
    : "";
  const basedOn = parent
    ? `<a class="card-stack" href="${esc(parent.url)}" target="_blank" rel="noopener" title="Stacked on #${parent.number}">↳ #${parent.number}</a>`
    : "";
  const comments = pr.comments.totalCount
    ? `<span class="comments" title="${pr.comments.totalCount} comments">${ICONS.comment}${pr.comments.totalCount}</span>`
    : "";
  const draft = pr.isDraft && reviewMode ? '<span class="draft-pill">Draft</span>' : "";
  const markReady = pr.isDraft && !reviewMode
    ? `<button type="button" class="am-toggle off draft-toggle" data-pr="${esc(pr.id)}" title="Draft. Click to mark it ready for review.">Mark ready</button>`
    : "";
  const labels = (pr.labels?.nodes || [])
    .map((l) => `<span class="label-pill" style="${labelColors(l.color)}">${esc(l.name)}</span>`)
    .join("");
  const chip = nextRowHtml(pr);
  // The Next row names the next step for changes requested or approved-but-red, so the
  // state badge would only repeat it. Once review is re-requested, the PR is waiting on
  // the reviewer, and "Changes requested" would read as yours to act on.
  const awaitingReview = (pr.reviewRequests?.nodes || []).length > 0;
  const nextKind = nextChip(pr)?.kind;
  const badge = reviewMode
    ? nextKind === "review" || nextKind === "re_review" ? "" : viewerReviewBadge(pr)
    : !chip && ((pr.reviewDecision === "CHANGES_REQUESTED" && !awaitingReview) ||
        (pr.reviewDecision === "APPROVED" && !ciOk(pr)))
      ? reviewBadge(pr)
      : "";
  const reviewUpdate = reviewMode ? reviewUpdateBadge(pr) : "";
  const authorReplied = reviewMode && reviewLaneOf(pr) === 1 ? authorRepliedBadge(pr) : "";
  const pendingReview = reviewMode && reviewLaneOf(pr) === 0 ? pendingReviewBadge(pr) : "";
  return `
    <div class="${cardClasses(pr, pr.isDraft ? "draft" : "")}" data-key="${esc(prKey(pr))}">
      <div class="card-title">${fmtTitle(pr.title)}${draft}${labels}</div>
      <div class="card-meta">${repoPrefix}<span class="pr-num">#${pr.number}</span> by ${esc(pr.author?.login || "ghost")} · ${agoSpan(pr.updatedAt)}</div>
      <div class="card-icons">
        ${ciHtml(pr)}
        ${comments}
        <span class="reviewers">${reviewMode ? authorAvatar(pr) : buildReviewers(pr)}</span>
        ${badge}
        ${reviewUpdate}
        ${authorReplied}
        ${pendingReview}
        ${basedOn}
        ${markReady}
        ${showAutoMerge ? autoMergeChip(pr) : ""}
        ${worktreeLinks(pr)}
      </div>
      ${chip}
    </div>`;
}

function sizeSortSelect() {
  const sel = $("#sort-select");
  const opt = sel.options[sel.selectedIndex];
  if (!opt) return;
  const cs = getComputedStyle(sel);
  const probe = document.createElement("span");
  probe.style.cssText = "position:absolute;visibility:hidden;white-space:nowrap";
  probe.style.fontSize = cs.fontSize;
  probe.style.fontFamily = cs.fontFamily;
  probe.style.fontWeight = cs.fontWeight;
  probe.textContent = opt.textContent;
  document.body.appendChild(probe);
  // border-box: width must cover text + the element's own padding and border
  // (both scale with the rem base), plus a hair of slack for the caret.
  const chrome = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight)
    + parseFloat(cs.borderLeftWidth) + parseFloat(cs.borderRightWidth) + 2;
  sel.style.width = probe.offsetWidth + chrome + "px";
  probe.remove();
}

export function render() {
  renderBoard();
  renderSidebar();
}

function renderBoard() {
  $("#companion-status").innerHTML = companionStatusHtml();

  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.filter === state.filter));
  const reviewSet = [...new Map([...state.prs, ...state.reviewedPrs].map((pr) =>
    [prKey(pr), pr]
  )).values()];
  const stepsFor = (prs) => prs.filter(needsYou).length;
  const mineCount = state.companionState
    ? stepsFor(state.prs.filter((pr) => pr.author?.login === state.viewer))
    : state.prs.filter((pr) => {
      if (pr.author?.login !== state.viewer || pr.isDraft) return false;
      const lane = laneOf(pr);
      return lane === 0 || (lane === 2 && !pr.autoMergeRequest);
    }).length;
  const mineCountEl = $("#count-mine");
  mineCountEl.textContent = mineCount || "";
  mineCountEl.classList.toggle("hidden", mineCount === 0);
  mineCountEl.title = mineCount
    ? `${mineCount} pull request${mineCount === 1 ? "" : "s"} ${mineCount === 1 ? "needs" : "need"} your attention`
    : "";
  const reviewCount = state.companionState
    ? stepsFor(reviewSet.filter(reviewItem))
    : reviewSet.filter((pr) => reviewItem(pr) && reviewLaneOf(pr) === 0).length;
  const reviewCountEl = $("#count-review");
  reviewCountEl.textContent = reviewCount || "";
  reviewCountEl.classList.toggle("hidden", reviewCount === 0);
  reviewCountEl.title = reviewCount
    ? `${reviewCount} pull request${reviewCount === 1 ? "" : "s"} ${reviewCount === 1 ? "needs" : "need"} your review`
    : "";
  const attention = mineCount + reviewCount;
  document.title = attention ? `Shipyard (${attention})` : "Shipyard";
  const reviewBoard = state.filter === "review";
  const dataset = reviewBoard ? reviewSet : state.prs;
  $("#sort-select").value = state.sort;
  sizeSortSelect();  // a native select sizes to its widest option; shrink to the selected one so the caret hugs the text

  const { parents } = buildStacks(dataset);
  const matches = dataset.filter(reviewBoard ? reviewItem : matchesFilter);
  const count = matches.length;
  const laneItems = reviewBoard ? [[], []] : [[], [], []];
  for (const pr of matches) laneItems[reviewBoard ? reviewLaneOf(pr) : laneOf(pr)].push(pr);
  const defs = reviewBoard
    ? [["Waiting", "yellow"], ["Review submitted", "green"]]
    : [["Working", "blue"], ["Waiting", "yellow"], ["Ready", "green"]];
  const recentMerged = mergedItems();
  let html = `<div class="board${reviewBoard ? " two-lane" : ""}">` + defs.map(([name, color], i) => {
    const inLane = laneItems[i];
    const laneNums = new Set(inLane.map((p) => p.number));
    const kids = new Map();
    const roots = [];
    for (const pr of inLane) {
      const parent = parents.get(pr.number);
      if (parent && laneNums.has(parent.number)) {
        if (!kids.has(parent.number)) kids.set(parent.number, []);
        kids.get(parent.number).push(pr);
      } else {
        roots.push(pr);
      }
    }
    roots.sort(sortComparator());
    const seen = new Set();
    const renderCardTree = (pr, depth) => {
      if (seen.has(pr.number)) return "";
      seen.add(pr.number);
      const parent = parents.get(pr.number);
      const chipParent = parent && !laneNums.has(parent.number) ? parent : null;
      const showAutoMerge = !reviewBoard && i >= 1;
      let out = depth === 0
        ? prCard(pr, chipParent, showAutoMerge, reviewBoard)
        : `<div class="card-nest" style="margin-left: ${(depth - 1) * 14 / 16}rem"><span class="stack-elbow" title="Stacked PR"></span>${prCard(pr, chipParent, showAutoMerge, reviewBoard)}</div>`;
      for (const kid of kids.get(pr.number) || []) out += renderCardTree(kid, depth + 1);
      return out;
    };
    // Non-drafts first; group drafts at the bottom under a labeled divider (Working lane).
    const renderGroup = (keep) => {
      let out = roots.filter(keep).map((r) => renderCardTree(r, 0)).join("");
      for (const pr of inLane) if (keep(pr) && !seen.has(pr.number)) out += renderCardTree(pr, 0);
      return out;
    };
    let laneHtml = renderGroup((p) => !p.isDraft);
    const draftHtml = renderGroup((p) => p.isDraft);
    if (draftHtml) laneHtml += '<div class="drafts-divider"></div>' + draftHtml;
    const needsLabel = (n) => n ? `<span class="lane-needs">${n} need${n === 1 ? "s" : ""} you</span>` : "";
    if (i === (reviewBoard ? 1 : 2) && recentMerged.length) {
      laneHtml += '<div class="merged-divider"></div>' + mergedSection(recentMerged, mergedCard);
    }
    const laneAction = (!reviewBoard && i === 0)
      ? `<button type="button" class="lane-new-task">${BRANCH_ICON}Start work</button>`
      : "";
    const laneKey = `${reviewBoard ? "review" : "mine"}:${name}`;
    const needs = needsLabel(state.companionState ? stepsFor(inLane) : 0);
    const laneCount = `<span class="lane-count">${needs && `${needs} · `}${inLane.length}</span>`;
    const collapsible = narrowViewport.matches;
    const collapsed = collapsible && collapsedLanes.has(laneKey);
    const header = collapsible
      ? `<div class="lane-header" role="button" tabindex="0" aria-expanded="${!collapsed}" data-lane="${esc(laneKey)}"><span class="lane-dot ${color}"></span>${name}${laneCount}<span class="lane-chevron">${LANE_CHEVRON}</span></div>`
      : `<div class="lane-header"><span class="lane-dot ${color}"></span>${name}${laneCount}</div>`;
    return `
    <div class="lane${collapsed ? " collapsed" : ""}">
      ${header}
      ${laneHtml || `<div class="lane-empty">${EMPTY_ICON}<span>${laneEmptyText(name, reviewBoard)}</span></div>`}
      ${laneAction}
    </div>`;
  }).join("") + "</div>";
  if (!count && !recentMerged.length) html = "";
  const targetLink = viewTargets(getCurrentRepo()).map((target) => {
    const targetUrl = target.includes("/") ? `https://github.com/${esc(target)}/pulls`
      : ownerType(target) === "org" ? `https://github.com/orgs/${esc(target)}/repositories`
      : ownerType(target) === "user" ? `https://github.com/${esc(target)}?tab=repositories`
      : `https://github.com/${esc(target)}`;
    return `<a href="${targetUrl}" target="_blank" rel="noopener">${esc(target)}</a>`;
  }).join(", ");

  // Until the server's first poll, show the spinner (or why it failed) rather than
  // the empty "No pull requests match." message.
  if (!state.targetedLoaded) {
    if (!getCurrentRepo()) return;  // not configured: keep the setup prompt
    $("#list-title").innerHTML = `Pull requests · ${targetLink}`;
    listBody.innerHTML = githubProblem()
      ? loadErrorHtml()
      : '<div class="loading"><div class="spinner"></div>Loading pull requests…</div>';
    return;
  }
  const titleLabel = state.filter === "review"
    ? `${count} review pull request${count === 1 ? "" : "s"}`
    : `${count} open pull request${count === 1 ? "" : "s"}`;
  $("#list-title").innerHTML = `${titleLabel} · ${targetLink}`;
  listBody.innerHTML = html || `<div class="empty">${EMPTY_ICON}<span>No pull requests match.</span></div>`;
}

function loadErrorHtml() {
  return `<div class="error-box">${esc(githubProblem())}</div>`;
}

// List view hangs merges off their own card outside #list-body, so both roots listen.
function handleMergedClick(e) {
  const btn = e.target.closest("button.merged-more");
  if (!btn) return false;
  if (btn.dataset.merged === "less") { state.mergedExtra = 0; render(); }
  else revealMoreMerged();
  return true;
}

listBody.addEventListener("click", async (e) => {
  if (e.target.closest("button.lane-new-task")) { openNewTaskDialog(); return; }
  const chip = e.target.closest("button.next-row");
  if (chip) { openSidebar(chip.dataset.open); return; }
  // A click anywhere on a card that isn't a link or control opens its sidebar.
  const card = e.target.closest(".card[data-key]");
  if (card && !e.target.closest("a, button, input, select, textarea")) {
    if (sidebarKey === card.dataset.key) closeSidebar(); else openSidebar(card.dataset.key);
    return;
  }
  const laneHeader = e.target.closest(".lane-header[data-lane]");
  if (laneHeader) { toggleLane(laneHeader.dataset.lane); return; }
  if (handleMergedClick(e)) return;

  const openBtn = e.target.closest("button.wt-open");
  if (openBtn) {
    openBtn.disabled = true;
    try {
      await openInApp(openBtn.dataset.target);
    } finally {
      openBtn.disabled = false;
    }
    return;
  }


  const agentBtn = e.target.closest("button.wt-agent");
  if (agentBtn) {
    agentBtn.disabled = true;
    try {
      await openAgent(agentBtn.dataset.path || agentBtn.dataset.agentPath, agentBtn.dataset.session || agentBtn.dataset.agentSession);
    } finally {
      agentBtn.disabled = false;
    }
    return;
  }

  const mainBtn = e.target.closest("button.wt-main");
  if (mainBtn) {
    mainBtn.disabled = true;
    const original = mainBtn.innerHTML;
    mainBtn.textContent = "…";
    try {
      const res = await companionPost(`open-in-main?repo=${encodeURIComponent(mainBtn.dataset.repo)}&branch=${encodeURIComponent(mainBtn.dataset.branch)}`);
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        await openInApp(launchTarget(data));
      } else {
        showError(data.error || `Couldn't open in your main clone (${res.status}).`, "Can't open in main clone");
      }
    } catch (err) {
      showError(err.message, "Can't open in main clone");
    } finally {
      mainBtn.disabled = false;
      mainBtn.innerHTML = original;
    }
    return;
  }

  const draftBtn = e.target.closest("button.draft-toggle");
  if (draftBtn) {
    // Marking ready notifies reviewers, so a stray tap shouldn't do it.
    const draftPr = state.prs.find((p) => p.id === draftBtn.dataset.pr);
    if (!draftPr || !confirm(`Mark #${draftPr.number} ready for review? Requested reviewers will be notified.`)) return;
    draftBtn.disabled = true;
    draftBtn.textContent = "…";
    try {
      await companionCall(`companion/pr-action?key=${encodeURIComponent(prKey(draftPr))}&action=mark_ready`, { payload: {} });
      render();
    } catch (err) {
      alert(`Mark ready: ${err.message}`);
      render();
    }
    return;
  }

  const btn = e.target.closest("button.am-toggle");
  const amPr = btn && state.prs.find((p) => p.id === btn.dataset.pr);
  if (!amPr) return;
  btn.disabled = true;
  btn.textContent = "…";
  const action = btn.dataset.action === "enable" ? "enable_auto_merge" : "disable_auto_merge";
  try {
    await companionCall(`companion/pr-action?key=${encodeURIComponent(prKey(amPr))}&action=${action}`, { payload: {} });
    render();
  } catch (err) {
    alert(`Auto-merge: ${err.message}`);
    render();
  }
});

$("#sort-select").addEventListener("change", (e) => {
  state.sort = e.target.value;
  localStorage.setItem("ghpr.sort", state.sort);
  render();
});

listBody.addEventListener("keydown", (e) => {
  const laneHeader = e.target.closest(".lane-header[data-lane]");
  if (!laneHeader || (e.key !== "Enter" && e.key !== " ")) return;
  e.preventDefault();
  toggleLane(laneHeader.dataset.lane);
  listBody.querySelector(`.lane-header[data-lane="${CSS.escape(laneHeader.dataset.lane)}"]`)?.focus();
});

narrowViewport.addEventListener("change", render);

document.querySelectorAll(".tab").forEach((tab) =>
  tab.addEventListener("click", () => {
    state.filter = tab.dataset.filter;
    state.mergedExtra = 0;
    localStorage.setItem(LS.FILTER, state.filter);
    render();
    load();
  })
);

// The card's bottom row: the PR's next step, when it has one. It opens the sidebar.
function nextRowHtml(pr) {
  const chip = nextChip(pr);
  const cp = companionPr(pr);
  const ready = (cp?.cards || []).find((c) => c.kind === "pass" || c.kind === "answer");
  const dot = cp?.running
    ? `<span class="next-spinner" title="${esc(agentName())} is working on this PR"></span>`
    : cp?.planReady && ready ? `<span class="plan-dot" title="${esc(agentName())} has looked at this — open it to see"></span>` : "";
  if (!chip && !dot) return "";
  const label = chip ? chip.label : cp.running ? "Reading…" : PASS_LABELS[ready?.pass] || "Ready to read";
  const prefix = chip?.kind === "snoozed" ? "" : `<span class="next-prefix">→ Next:</span>`;
  return `<button type="button" class="next-row ${chip?.tone || ""}${chip?.optional ? " optional" : ""}" data-open="${esc(prKey(pr))}" title="${esc(chip?.title || "Open details")}">${prefix}${dot}<span class="next-label">${esc(label)}</span>${chip && cp?.running ? '<span class="next-status">Working…</span>' : ""}</button>`;
}

function cardClasses(pr, extra) {
  const snoozed = companionPr(pr)?.chip?.kind === "snoozed";
  return ["card", extra, snoozed ? "snoozed" : "", sidebarKey === prKey(pr) ? "selected" : ""].filter(Boolean).join(" ");
}
