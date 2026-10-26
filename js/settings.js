// The settings dialog's views and the Start work dialog.
import { $, state } from "./base.js";
import { esc, REMOVE_ICON } from "./html.js";
import { apiGet, apiPost, getCurrentRepo, getRepos, saveSetting, saveViews } from "./server.js";
import { refreshNow, resetData, viewTargets } from "./data.js";
import { launchTarget, openInApp, render } from "./board.js";

export function renderRepoSelect() {
  const repos = getRepos();
  const current = getCurrentRepo();
  const sel = $("#repo-select");
  sel.innerHTML = repos.length
    ? repos.map((r) => `<option value="${esc(r)}" ${r === current ? "selected" : ""}>${esc(r)}</option>`).join("")
    : '<option value="">— add a view in settings —</option>';
}

export function renderViewsList() {
  const repos = getRepos();
  $("#views-list").innerHTML = repos.length
    ? repos.map((r) => `<li class="view-item">
        <span class="view-name">${esc(r)}</span>
        <button type="button" class="view-remove" data-view="${esc(r)}" title="Remove ${esc(r)}" aria-label="Remove ${esc(r)}">${REMOVE_ICON}</button>
      </li>`).join("")
    : '<li class="view-empty">No views yet — add one below.</li>';
}

function removeView(v) {
  const wasCurrent = getCurrentRepo() === v;
  const remaining = getRepos().filter((r) => r !== v);
  saveViews(remaining, wasCurrent ? remaining[0] || "" : getCurrentRepo());
  if (wasCurrent) resetData();
  renderRepoSelect();
  renderViewsList();
  if (wasCurrent) {
    render();
  } else {
    render();
  }
}

$("#views-list").addEventListener("click", (e) => {
  const btn = e.target.closest(".view-remove");
  if (btn) removeView(btn.dataset.view);
});

$("#settings-btn").addEventListener("click", openSettings);

$("#settings-close").addEventListener("click", () => $("#settings-dialog").close());

// ---------- every other setting, built from the server's field list ----------
let settingSections = [];
let settingFields = [];

export async function openSettings() {
  renderViewsList();
  $("#settings-fields").innerHTML = "";
  $("#settings-dialog").showModal();
  try {
    ({ sections: settingSections, fields: settingFields } = await apiGet("api/settings"));
    renderSettings();
  } catch (err) {
    $("#settings-fields").innerHTML = `<p class="set-error">${esc(err.message)}</p>`;
  }
}

const fieldKey = (f) => f.path.join(".");
const fieldAt = (key) => settingFields.find((f) => fieldKey(f) === key);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function shownValue(f, value) {
  if (f.kind === "switch") return value ? "On" : "Off";
  if (f.kind === "choice") return f.choices.find(([v]) => v === value)?.[1] || String(value);
  if (value === "" || value == null) return "none";
  return f.kind === "lines" ? value.split("\n").join(", ") : String(value);
}

function settingControl(f, id, off) {
  const dis = off ? " disabled" : "";
  const mono = /url|command|prompt|roots/.test(fieldKey(f)) ? " set-mono" : "";
  switch (f.kind) {
    case "switch":
      return `<input type="checkbox" id="${id}" class="set-switch"${f.value ? " checked" : ""}${dis}>`;
    case "locked":
      return `<span class="set-locked">Always manual</span>`;
    case "choice":
      return `<select id="${id}"${dis}>${f.choices.map(([v, label]) =>
        `<option value="${esc(v)}"${v === f.value ? " selected" : ""}>${esc(label)}</option>`).join("")}</select>`;
    case "number":
      return `<input type="number" id="${id}" min="0" step="any" value="${esc(f.value)}"${dis}>`;
    case "lines":
      return `<textarea id="${id}" rows="3" class="${mono.trim()}"${dis}>${esc(f.value)}</textarea>`;
    default:
      return `<input type="text" id="${id}" class="${mono.trim()}" value="${esc(f.value)}" spellcheck="false"${dis}>`;
  }
}

function settingRow(f, error) {
  const key = fieldKey(f);
  const id = `set-${key.replace(/[^\w]/g, "-")}`;
  const parent = f.parent && fieldAt(f.parent.join("."));
  const off = !!parent && !parent.value;
  const inline = f.kind === "switch" || f.kind === "locked";
  const control = settingControl(f, id, off);
  const changed = f.set && !same(f.value, f.default);
  const tag = f.kind === "locked" ? "" : `<span class="set-tag${changed ? " changed" : ""}">${changed ? "Changed" : "Default"}</span>`;
  const note = !f.set ? "" : (changed ? `<span class="set-default">Default: ${esc(shownValue(f, f.default))}</span>`
    : `<span class="set-default">Set to the default in the file</span>`)
    + `<button type="button" class="set-reset" data-reset="${esc(key)}">Reset</button>`;
  return `<div class="set-row${parent ? " set-child" : ""}${off ? " set-off" : ""}" data-path="${esc(key)}">
    <div class="set-head"><label for="${id}">${esc(f.label)}</label>${tag}${inline ? control : ""}</div>
    ${inline ? "" : control}
    ${f.help || note ? `<div class="set-note">${f.help ? `<span>${esc(f.help)}</span>` : ""}${note}</div>` : ""}
    ${error ? `<div class="set-error">${esc(error)}</div>` : ""}
  </div>`;
}

function renderSettings(errors = {}) {
  const focused = document.activeElement?.id;
  $("#settings-fields").innerHTML = settingSections.map((s) => `<section class="set-section">
      <h3>${esc(s.title)}</h3><p class="set-intro">${esc(s.help)}</p>
      ${settingFields.filter((f) => f.section === s.id).map((f) => settingRow(f, errors[fieldKey(f)])).join("")}
    </section>`).join("");
  if (focused) document.getElementById(focused)?.focus();
}

async function saveField(f, change) {
  try {
    ({ fields: settingFields } = await saveSetting(f.path, { ...change, expect: f.value }));
    renderSettings();
  } catch (err) {
    renderSettings({ [fieldKey(f)]: err.message });
  }
}

// Text saves when it's committed (Enter or leaving the field), not on every keystroke.
$("#settings-fields").addEventListener("change", (e) => {
  const row = e.target.closest(".set-row");
  const f = row && fieldAt(row.dataset.path);
  if (!f) return;
  if (f.kind === "switch") return saveField(f, { value: e.target.checked });
  if (f.kind === "number") {
    return e.target.value === "" ? saveField(f, { reset: true }) : saveField(f, { value: Number(e.target.value) });
  }
  saveField(f, { value: e.target.value });
});

$("#settings-fields").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-reset]");
  if (btn) saveField(fieldAt(btn.dataset.reset), { reset: true });
});

$("#refresh-btn").addEventListener("click", refreshNow);

$("#stale-retry-btn").addEventListener("click", refreshNow);

// The repo picker offers local clones under the current view only, so it matches the
// view selector: a repo view contributes just itself; an org/username view contributes
// every local clone under it. Repos you worked in most recently sort first — by the
// server's per-worktree activity stamp, so work done outside Shipyard counts too —
// and the most recent one is preselected. Clones with no activity fall in alphabetically.
function ntRepoOptionsHtml() {
  const localRepos = [...new Set(Object.values(state.worktrees).map((w) => w.repo).filter(Boolean))];
  const activity = new Map();  // repo -> newest activity across its worktrees
  for (const w of Object.values(state.worktrees)) {
    if (w.repo) activity.set(w.repo, Math.max(activity.get(w.repo) || 0, w.activity || 0));
  }
  const seenAt = (r) => activity.get(r) || 0;
  const targets = viewTargets(getCurrentRepo());
  const available = localRepos.filter((r) =>
    targets.some((t) => t.includes("/") ? r === t : r.split("/")[0] === t)
  ).sort((a, b) => seenAt(b) - seenAt(a) || a.localeCompare(b));
  return available.map((r, i) =>
    `<option value="${esc(r)}"${i === 0 ? " selected" : ""}>${esc(r)}</option>`).join("");
}

export function openNewTaskDialog() {
  const html = ntRepoOptionsHtml();
  $("#nt-repo").innerHTML = html || '<option value="">— no local clones in this view —</option>';
  const branch = $("#nt-branch");
  branch.value = "";  // start blank so the default action is "Open default"
  const status = $("#nt-status");
  status.textContent = "";
  status.classList.remove("error");
  updateNtButtons();
  $("#new-task-dialog").showModal();
  branch.focus();
  branch.setSelectionRange(branch.value.length, branch.value.length);
}

const ntError = (msg) => { const s = $("#nt-status"); s.textContent = msg; s.classList.add("error"); };

// With a branch name, the primary action creates that branch in the main clone
// (which can't switch with uncommitted work); blank, it opens the default branch
// (a blank slate). The secondary Worktree action needs a name.
function updateNtButtons() {
  const repo = $("#nt-repo").value;
  const hasName = !!$("#nt-branch").value.trim().replace(/\/+$/, "");
  const mainWt = Object.values(state.worktrees).find((w) => w.repo === repo && w.main);
  const dirty = !!(mainWt && mainWt.dirty);
  const mainOn = mainWt && mainWt.branch ? ` (on ${mainWt.branch})` : "";
  const worktreeBtn = $("#nt-worktree");
  worktreeBtn.classList.toggle("hidden", !hasName);  // only meaningful with a branch name
  worktreeBtn.textContent = "Create worktree";
  worktreeBtn.title = "Create the branch in an isolated worktree and open it";
  const mainBtn = $("#nt-main");
  mainBtn.disabled = dirty;
  mainBtn.textContent = hasName ? "Create branch" : "Open default";
  mainBtn.title = dirty
    ? `Your main clone${mainOn} has uncommitted changes — commit or stash them there first`
    : hasName
      ? "Create the branch in your main clone and open it"
      : "Switch your main clone to the default branch and open it — a blank slate";
}

$("#nt-repo").addEventListener("change", updateNtButtons);

$("#nt-branch").addEventListener("input", updateNtButtons);

$("#nt-branch").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.isComposing) return;
  e.preventDefault();
  const mainBtn = $("#nt-main");
  if (!mainBtn.disabled) mainBtn.click();
});

$("#nt-cancel").addEventListener("click", () => $("#new-task-dialog").close());

$("#new-task-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const repo = $("#nt-repo").value;
  const branch = $("#nt-branch").value.trim();
  $("#nt-status").classList.remove("error");
  if (!repo) return ntError("No repository selected.");
  const blankSlate = !branch;
  if (!blankSlate && branch.endsWith("/")) return ntError("Enter a branch name.");
  const submitBtn = e.submitter || $("#nt-main");
  const toWorktree = submitBtn.dataset.dest === "worktree";
  if (toWorktree && blankSlate) return ntError("Enter a branch name to create a worktree.");
  submitBtn.disabled = true;
  $("#nt-status").textContent = toWorktree ? "Creating worktree…"
    : blankSlate ? "Opening the default branch…" : "Creating branch in main clone…";
  try {
    const endpoint = toWorktree ? "new-task" : blankSlate ? "open-default-main" : "new-branch-main";
    const query = blankSlate
      ? `${endpoint}?repo=${encodeURIComponent(repo)}`
      : `${endpoint}?repo=${encodeURIComponent(repo)}&branch=${encodeURIComponent(branch)}`;
    const res = await apiPost(query);
    const data = await res.json().catch(() => ({}));
    if (res.ok && data.ok) {
      await openInApp(launchTarget(data));
      $("#new-task-dialog").close();
    } else {
      ntError(data.error || `Failed (${res.status})`);
    }
  } catch (err) {
    ntError(err.message);
  } finally {
    updateNtButtons();
  }
});

$("#add-repo-btn").addEventListener("click", () => {
  const targets = viewTargets($("#add-repo-input").value)
    .map((t) => t.replace(/^https:\/\/github\.com\//, "").replace(/\/$/, ""));
  if (!targets.length || !targets.every((t) => /^[\w.-]+(\/[\w.-]+)?$/.test(t))) {
    alert("Enter owner/repo, an org, or a username (comma-separate several to combine them)");
    return;
  }
  const v = targets.join(", ");
  const repos = getRepos();
  saveViews(repos.includes(v) ? repos : [...repos, v], v);
  $("#add-repo-input").value = "";
  renderRepoSelect();
  renderViewsList();
  resetData();
  render();
});

$("#add-repo-input").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#add-repo-btn").click(); });

$("#repo-select").addEventListener("change", (e) => {
  saveViews(getRepos(), e.target.value);
  resetData();
  render();
});
