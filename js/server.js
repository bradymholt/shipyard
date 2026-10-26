// Talking to the companion: requests, its event stream, saved views, and local checkouts.
import { DEFAULT_LAUNCHER, state } from "./base.js";
import { clip, esc, refreshAgoLabels, showError } from "./html.js";
import { applyServerPrs, githubProblem, lastPollAt, resetData, updateGitHubStatus } from "./data.js";
import { render } from "./board.js";
import { renderRepoSelect, renderViewsList } from "./settings.js";

export const getRepos = () => state.views.views;

export const getCurrentRepo = () => state.views.current || getRepos()[0] || "";

// Settings live in the companion's config file. `expect` is what this page last saw, so a
// hand edit to the file since then is reported instead of overwritten.
export function saveSettings(set, expect) {
  return companionCall("companion/settings", { set, expect });
}

// Show the change now; the server's push then confirms it in every open tab.
let savingViews = 0;

export async function saveViews(views, current) {
  const before = state.views;
  state.views = { views, current };
  savingViews++;
  try {
    if (JSON.stringify(views) !== JSON.stringify(before.views)) await saveSettings({ views }, { views: before.views });
    const saved = await companionCall("companion/current-view", { view: current });
    state.views = { views: saved.views, current: saved.current };
    return true;
  } catch (err) {
    state.views = before;
    renderRepoSelect();
    renderViewsList();
    showError(err.message, "Couldn't save your views");
    return false;
  } finally {
    savingViews--;
  }
}

export function companionPost(url, body = undefined) {
  const headers = { "X-Shipyard-Token": state.companionToken };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return fetch(url, {
    method: "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

// POST to the companion and return its JSON, throwing its error message on failure.
export async function companionCall(url, body = {}) {
  return companionJson(await companionPost(url, body));
}

export async function companionGet(url) {
  let res;
  try {
    res = await fetch(url, { cache: "no-store" });
  } catch {
    throw new Error("Couldn't reach Shipyard. Is it still running?");
  }
  return companionJson(res);
}

async function companionJson(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || `Shipyard request failed (${res.status}).`);
  return data;
}

// The server re-scans checkouts with every poll and after its own branch actions;
// this asks for one now, for changes made outside Shipyard.
export const rescanWorktrees = () => companionCall("companion/rescan-worktrees").catch(() => {});

function applyWorktrees() {
  const map = {};
  for (const w of state.companionState?.worktrees || []) map[`${w.repo || ""}#${w.branch}`] = w;
  state.worktrees = map;
}

export function worktreeFor(pr) {
  return state.worktrees[`${pr.repository?.nameWithOwner || ""}#${pr.headRefName}`];
}

// The companion pushes its whole state (chips, cards, sessions, activity) over SSE
// whenever something changes; EventSource reconnects on its own if it restarts.
let companionEvents = null;

const companionLive = () => !!state.companionState && companionEvents?.readyState === EventSource.OPEN;

export function connectCompanionEvents() {
  if (companionEvents || typeof EventSource === "undefined") return;
  companionEvents = new EventSource("companion/events");
  companionEvents.addEventListener("error", () => render());
  companionEvents.addEventListener("state", (e) => {
    const polledAt = lastPollAt();
    try {
      state.companionState = JSON.parse(e.data);
    } catch { return; }
    if (lastPollAt() !== polledAt || githubProblem()) state.refreshing = false;
    const config = state.companionState.config;
    if (config) Object.assign(state, { rules: config.rules, agent: config.agent, launcher: config.launcher || DEFAULT_LAUNCHER });
    applyViews();
    applyServerPrs();
    applyWorktrees();
    updateGitHubStatus();
    render();
  });
  companionEvents.addEventListener("merged", (e) => {
    try {
      state.recentMerged = JSON.parse(e.data);
    } catch { return; }
    applyServerPrs();
    render();
  });
  companionEvents.addEventListener("poll", (e) => {
    if (!state.companionState?.status) return;
    try {
      state.companionState.status.lastPoll = JSON.parse(e.data).lastPoll;
    } catch { return; }
    state.refreshing = false;
    updateGitHubStatus();
    refreshAgoLabels();
  });
}

// Views changed in another tab, or the server's answer to a save from this one.
function applyViews() {
  const views = state.companionState.views;
  if (!views || savingViews || JSON.stringify(views) === JSON.stringify(state.views)) return;
  const was = getCurrentRepo();
  state.views = views;
  renderRepoSelect();
  renderViewsList();
  if (getCurrentRepo() !== was) resetData();
}

export function companionStatusHtml() {
  const status = state.companionState?.status;
  if (!status) return "";
  if (!companionLive()) return '<span class="dot off"></span>Shipyard not running · reconnecting…';
  const agent = status.agent && !status.agent.available ? ` · ${esc(status.agent.name)} CLI not found` : "";
  if (status.github && status.github !== "ok" && status.github !== "starting") {
    return `<span class="dot off"></span><span class="companion-err" title="${esc(status.github)}">Can't reach GitHub: ${esc(clip(status.github, 80))}</span>${agent}`;
  }
  return `<span class="dot ok"></span>Connected${agent}`;
}

// The server polls about once a minute; coming back to the tab asks it to look now.
const FOCUS_POLL_MS = 30 * 1000;

// A poll re-scans checkouts too, so ask for one or the other.
export function refreshOnFocus() {
  if (document.hidden) return;
  if (companionLive() && Date.now() - lastPollAt() >= FOCUS_POLL_MS) {
    companionCall("companion/check-now").catch(() => {});
  } else {
    rescanWorktrees();
  }
}

// Views used to live in each browser; hand any this one has to the server, once.
export async function moveLocalViews() {
  let local = [];
  try { local = JSON.parse(localStorage.getItem("ghpr.repos") || "[]"); } catch { /* unreadable: nothing to move */ }
  const added = local.filter((v) => !getRepos().includes(v));
  const current = getCurrentRepo() || localStorage.getItem("ghpr.currentRepo") || added[0];
  if (added.length && !(await saveViews([...getRepos(), ...added], current))) return;
  localStorage.removeItem("ghpr.repos");
  localStorage.removeItem("ghpr.currentRepo");
}
