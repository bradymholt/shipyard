// Talking to the server: requests, its event stream, saved views, and local checkouts.
import { DEFAULT_LAUNCHER, state } from "./base.js";
import { clip, esc, refreshAgoLabels, showError } from "./html.js";
import { applyServerPrs, githubProblem, lastPollAt, resetData, updateGitHubStatus } from "./data.js";
import { render } from "./board.js";
import { renderRepoSelect, renderViewsList } from "./settings.js";
import { hasUnsentText } from "./sidebar.js";

export const getRepos = () => state.views.views;

// "" is everything you're involved in; a view only narrows it.
export const getCurrentRepo = () => state.views.current || "";

// Settings live in the server's config file, one path at a time: `change` is
// { value } or { reset: true }, plus the `expect`ed value this page last showed, so a
// hand edit to the file since then is reported instead of overwritten.
export function saveSetting(path, change) {
  return apiCall("api/settings", { path, ...change });
}

// A new install holds passes and automatic actions until you turn them on.
export const automationHeld = () => state.serverState?.automation === "held";

export async function turnOnAutomation() {
  try {
    await apiCall("api/automation", { on: true });
    state.serverState.automation = "on";
    render();
  } catch (err) {
    showError(err.message, "Couldn't turn it on");
  }
}

// Show the change now; the server's push then confirms it in every open tab.
let savingViews = 0;

export async function saveViews(views, current) {
  const before = state.views;
  state.views = { views, current };
  savingViews++;
  try {
    if (JSON.stringify(views) !== JSON.stringify(before.views)) await saveSetting(["views"], { value: views, expect: before.views });
    const saved = await apiCall("api/current-view", { view: current });
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

export function apiPost(url, body = undefined) {
  const headers = { "X-Shipyard-Token": state.sessionToken };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  return fetch(url, {
    method: "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

// POST to the server and return its JSON, throwing its error message on failure.
export async function apiCall(url, body = {}) {
  return apiJson(await apiPost(url, body));
}

export async function apiGet(url) {
  let res;
  try {
    res = await fetch(url, { cache: "no-store" });
  } catch {
    throw new Error("Couldn't reach Shipyard. Is it still running?");
  }
  return apiJson(res);
}

async function apiJson(res) {
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) throw new Error(data.error || `Shipyard request failed (${res.status}).`);
  return data;
}

// The server re-scans checkouts with every poll and after its own branch actions;
// this asks for one now, for changes made outside Shipyard.
export const rescanWorktrees = () => apiCall("api/rescan-worktrees").catch(() => {});

function applyWorktrees() {
  const map = {};
  for (const w of state.serverState?.worktrees || []) map[`${w.repo || ""}#${w.branch}`] = w;
  state.worktrees = map;
}

export function worktreeFor(pr) {
  return state.worktrees[`${pr.repository?.nameWithOwner || ""}#${pr.headRefName}`];
}

// The server pushes its whole state (chips, cards, sessions, activity) over SSE
// whenever something changes; EventSource reconnects on its own if it restarts.
let serverEvents = null;

const serverLive = () => !!state.serverState && serverEvents?.readyState === EventSource.OPEN;

export function connectServerEvents() {
  if (serverEvents || typeof EventSource === "undefined") return;
  serverEvents = new EventSource("api/events");
  serverEvents.addEventListener("error", () => render());
  serverEvents.addEventListener("state", (e) => {
    const polledAt = lastPollAt();
    try {
      state.serverState = JSON.parse(e.data);
    } catch { return; }
    if (lastPollAt() !== polledAt || githubProblem()) state.refreshing = false;
    if (state.pageVersion && state.serverState.pageVersion && state.serverState.pageVersion !== state.pageVersion) {
      reloadWhenIdle();
    }
    const config = state.serverState.config;
    if (config) Object.assign(state, { rules: config.rules, agent: config.agent, launcher: config.launcher || DEFAULT_LAUNCHER, approveReplies: config.approveReplies || [] });
    applyViews();
    applyServerPrs();
    applyWorktrees();
    updateGitHubStatus();
    render();
  });
  serverEvents.addEventListener("merged", (e) => {
    try {
      state.recentMerged = JSON.parse(e.data);
    } catch { return; }
    applyServerPrs();
    render();
  });
  serverEvents.addEventListener("poll", (e) => {
    if (!state.serverState?.status) return;
    try {
      state.serverState.status.lastPoll = JSON.parse(e.data).lastPoll;
    } catch { return; }
    state.refreshing = false;
    updateGitHubStatus();
    refreshAgoLabels();
  });
}

// Views changed in another tab, or the server's answer to a save from this one.
function applyViews() {
  const views = state.serverState.views;
  if (!views || savingViews || JSON.stringify(views) === JSON.stringify(state.views)) return;
  const was = getCurrentRepo();
  state.views = views;
  renderRepoSelect();
  renderViewsList();
  if (getCurrentRepo() !== was) resetData();
}

export function serverStatusHtml() {
  const status = state.serverState?.status;
  if (!status) return "";
  if (!serverLive()) return '<span class="dot off"></span>Shipyard not running · reconnecting…';
  const agent = status.agent && !status.agent.available ? ` · ${esc(status.agent.name)} CLI not found` : "";
  if (status.github && status.github !== "ok" && status.github !== "starting") {
    return `<span class="dot off"></span><span class="server-err" title="${esc(status.github)}">Can't reach GitHub: ${esc(clip(status.github, 80))}</span>${agent}`;
  }
  return `<span class="dot ok"></span>Connected${agent}`;
}

// The server polls about once a minute; coming back to the tab asks it to look now.
const FOCUS_POLL_MS = 30 * 1000;

// A poll re-scans checkouts too, so ask for one or the other.
// New page code is on disk: reload once nothing would be lost, right away for a hidden tab.
let reloadTimer = null;

function reloadWhenIdle() {
  const tryReload = () => {
    const busy = document.querySelector("dialog[open]") || hasUnsentText()
      || document.activeElement?.matches("input, textarea, select, [contenteditable]");
    if (document.hidden || !busy) location.reload();
  };
  if (!reloadTimer) reloadTimer = setInterval(tryReload, 5000);
  tryReload();
}

export function refreshOnFocus() {
  if (document.hidden) return;
  if (serverLive() && Date.now() - lastPollAt() >= FOCUS_POLL_MS) {
    apiCall("api/check-now").catch(() => {});
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
