// The first-run welcome: what Shipyard does, whether GitHub and your agent work, views to narrow the board, and whether it
// may act on its own. It shows once per install.
import { $, state } from "./base.js";
import { esc, fmtCode, showError } from "./html.js";
import { apiCall, apiGet, automationHeld, getRepos, saveSetting, saveViews, turnOnAutomation } from "./server.js";
import { render } from "./board.js";
import { renderRepoSelect } from "./settings.js";

const PASS_STARTS = {
  ci_failed: "looks into failing checks on your PRs",
  pre_review: "pre-reviews PRs when your review is requested",
  re_review: "re-reviews PRs you reviewed when they change",
  feedback: "plans a response when a reviewer requests changes",
};
const AUTO_STARTS = {
  rerun_checks: "re-runs flaky checks",
  approve_rereview: "approves re-reviews once the small things you asked for are done",
  apply_fix: "pushes small CI fixes",
};
const listed = (items) => items.length > 1 ? `${items.slice(0, -1).join(", ")} and ${items.at(-1)}` : items[0] || "";

// What turning automation on would start, read from your settings so it matches them.
function automationList() {
  const config = state.serverState?.config || {};
  const passes = Object.entries(config.rules?.passes || {}).filter(([, mode]) => mode === "auto").map(([p]) => PASS_STARTS[p]).filter(Boolean);
  const pressed = Object.entries(config.auto || {}).filter(([, on]) => on).map(([a]) => AUTO_STARTS[a]).filter(Boolean);
  return [passes.length && `Your coding agent ${listed(passes)}.`,
    pressed.length && `When the agent is confident, Shipyard ${listed(pressed)}.`].filter(Boolean);
}

// Owners of the PRs you're involved in, most first, then those of your local clones.
function suggestedViews() {
  const count = new Map();
  for (const key of Object.keys(state.serverState?.prs || {})) {
    const owner = key.split("/")[0];
    count.set(owner, (count.get(owner) || 0) + 1);
  }
  for (const w of Object.values(state.worktrees)) {
    const owner = (w.repo || "").split("/")[0];
    if (owner && !count.has(owner)) count.set(owner, 0);
  }
  return [...count].filter(([owner]) => !getRepos().includes(owner))
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 6);
}

let steps = ["intro", "checks", "views", "agent"];
let step = 0;
let shown = false;
const picked = new Set();
let folders = [];  // where your clones seem to be, offered when no folders are set
let checks = null;  // null while the server is checking

async function runChecks() {
  checks = null;
  try {
    checks = (await apiGet("api/setup-check")).checks;
  } catch (err) {
    checks = [{ label: "Shipyard", ok: false, detail: err.message }];
  }
  if (steps[step] === "checks") renderWelcome();
}

// Folders Shipyard found that hold your clones, when none are set yet.
export async function folderCandidates() {
  if ((state.serverState?.roots || []).length) return [];
  try {
    return (await apiGet("api/folder-candidates")).candidates;
  } catch {
    return [];
  }
}

export async function maybeShowWelcome() {
  // After the first poll, so the views and folders it suggests come from your PRs, or
  // after a failed one, so the checks say what to fix.
  const status = state.serverState?.status;
  if (shown || !state.serverState?.welcome || !status || !(status.lastPoll || status.github !== "starting") || location.hash.startsWith("#settings")) return;
  shown = true;
  runChecks();
  folders = await folderCandidates();
  steps = ["intro", "checks", "views", ...(folders.length ? ["folders"] : []), "agent"];
  renderWelcome();
  $("#welcome-dialog").showModal();
}

const checkRow = (c) => `<div class="wel-check"><span class="wel-mark ${c.ok ? "ok" : "bad"}">${c.ok ? "✓" : "✕"}</span>
  <div><strong>${esc(c.label)}</strong><span>${fmtCode(c.detail)}</span>${c.fix ? `<span class="wel-fix">${fmtCode(c.fix)}</span>` : ""}</div></div>`;

const clonesText = (c) => `${c.clones} clone${c.clones === 1 ? "" : "s"}${c.matching ? `, ${c.matching} from your PRs` : ""}`;

function stepHtml() {
  const name = steps[step];
  if (name === "intro") {
    return {
      body: `<h2 id="welcome-title">Welcome to Shipyard</h2>
        <p class="wel-lead">Every pull request you're involved in, on one board, with the next step on each card.</p>
        <ul class="wel-points">
          <li><strong>Your board.</strong> My PRs sorts yours into Working, Waiting, and Ready. Reviews holds the ones waiting on you.</li>
          <li><strong>Open a card.</strong> Its sidebar shows what changed and what to do next, with buttons to do it.</li>
          <li><strong>Your coding agent.</strong> Looks into failing checks and drafts reviews. It prepares; you decide.</li>
        </ul>`,
      actions: `<button type="button" class="primary" data-wel="next">Next</button>`,
    };
  }
  if (name === "checks") {
    const failed = checks?.some((c) => !c.ok);
    return {
      body: `<h2 id="welcome-title">Checking your setup</h2>
        <p class="wel-lead">Shipyard reads your PRs through the GitHub CLI and hands work to your coding agent.</p>
        <div class="wel-checks">${checks ? checks.map(checkRow).join("")
          : `<div class="wel-check"><span class="spinner wel-mark"></span><div><span>Checking…</span></div></div>`}</div>
        ${failed ? `<p class="wel-note">You can keep going and fix these later.</p>` : ""}`,
      actions: `${failed ? `<button type="button" data-wel="recheck">Check Again</button>` : ""}
        <button type="button" class="primary" data-wel="next">Next</button>`,
    };
  }
  if (name === "folders") {
    return {
      body: `<h2 id="welcome-title">Where's your code?</h2>
        <p class="wel-lead">Shipyard opens checkouts, resumes your agent's sessions, and starts work in your local clones. It found:</p>
        <div class="wel-folders">${folders.map((c) => `<div class="wel-folder"><code>${esc(c.path)}</code>
          <span>${esc(clonesText(c))}</span><button type="button" class="primary" data-use-folder="${esc(c.path)}">Use</button></div>`).join("")}</div>
        <p class="wel-note">The board works without one. You can change folders anytime in Settings.</p>`,
      actions: `<button type="button" data-wel="next">Skip</button>`,
    };
  }
  if (name === "views") {
    const views = suggestedViews();
    return {
      body: `<h2 id="welcome-title">Narrow it down?</h2>
        <p class="wel-lead">The board shows every PR you're involved in. A view narrows it to one owner; switch views in the header anytime.</p>
        ${views.length ? `<div class="wel-chips">${views.map(([owner, n]) => `<button type="button" class="wel-chip${picked.has(owner) ? " on" : ""}"
          data-pick="${esc(owner)}" aria-pressed="${picked.has(owner)}">${esc(owner)}${n ? ` <em>${n} PR${n === 1 ? "" : "s"}</em>` : ""}</button>`).join("")}</div>`
          : `<p class="wel-note">No suggestions yet. You can add views anytime in Settings.</p>`}`,
      actions: picked.size
        ? `<button type="button" class="primary" data-wel="views">Add ${picked.size} view${picked.size === 1 ? "" : "s"}</button>`
        : `<button type="button" class="primary" data-wel="next">Keep Everything</button>`,
    };
  }
  if (!automationHeld()) {
    return {
      body: `<h2 id="welcome-title">You're set</h2><p class="wel-lead">Shipyard already works on your PRs on its own, as your settings say.</p>`,
      actions: `<button type="button" class="primary" data-wel="done">Done</button>`,
    };
  }
  const what = automationList();
  return {
    body: `<h2 id="welcome-title">Let Shipyard work on its own?</h2>
      <p class="wel-lead">Nothing happens without a click yet: the agent waits for Ask, and no button is pressed for you.${what.length ? " Turned on:" : ""}</p>
      ${what.length ? `<ul class="wel-points">${what.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
      <p class="wel-note">Pre-reviews stay drafts only you can see, and replies never post on their own. You can change any of it in Settings.</p>`,
    actions: `<button type="button" data-wel="settings">Choose in Settings</button><button type="button" data-wel="done">Not now</button>
      <button type="button" class="primary" data-wel="on">Turn it on</button>`,
  };
}

function renderWelcome() {
  const { body, actions } = stepHtml();
  const dots = steps.map((_, i) => `<i class="${i === step ? "on" : ""}"></i>`).join("");
  $("#welcome-body").innerHTML = `<button type="button" class="wel-close" data-wel="done" aria-label="Close">×</button>
    <div class="wel-main">${body}</div>
    <div class="wel-foot"><span class="wel-dots" aria-label="Step ${step + 1} of ${steps.length}">${dots}</span>
      ${step ? `<button type="button" data-wel="back">Back</button>` : ""}<span class="wel-gap"></span>${actions}</div>`;
}

function finish() {
  if ($("#welcome-dialog").open) $("#welcome-dialog").close();
  state.serverState.welcome = false;
  apiCall("api/welcome").catch((err) => showError(err.message, "Couldn't save that"));
  render();
}

$("#welcome-dialog").addEventListener("cancel", (e) => {
  e.preventDefault();
  finish();
});

$("#welcome-dialog").addEventListener("click", async (e) => {
  const use = e.target.closest("[data-use-folder]");
  if (use) {
    try {
      await saveSetting(["roots"], { value: use.dataset.useFolder, expect: "" });
    } catch (err) {
      return showError(err.message, "Couldn't use that folder");
    }
    step += 1;
    return renderWelcome();
  }
  const pick = e.target.closest("[data-pick]");
  if (pick) {
    if (!picked.delete(pick.dataset.pick)) picked.add(pick.dataset.pick);
    return renderWelcome();
  }
  const action = e.target.closest("[data-wel]")?.dataset.wel;
  if (action === "next" || action === "back") {
    step += action === "next" ? 1 : -1;
    return renderWelcome();
  }
  if (action === "recheck") {
    runChecks();
    return renderWelcome();
  }
  if (action === "views") {
    saveViews([...getRepos(), ...picked], state.views.current);
    picked.clear();
    renderRepoSelect();
    step += 1;
    return renderWelcome();
  }
  if (action === "on") await turnOnAutomation();
  if (action === "on" || action === "done" || action === "settings") finish();
  if (action === "settings") location.hash = "#settings/rules";
});
