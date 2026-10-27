// The settings dialog's views and the Start work dialog.
import { $, state } from "./base.js";
import { esc, REMOVE_ICON } from "./html.js";
import { apiGet, apiPost, automationHeld, getCurrentRepo, getRepos, saveSetting, saveViews, turnOnAutomation } from "./server.js";
import { refreshNow, resetData, viewTargets } from "./data.js";
import { folderCandidates } from "./welcome.js";
import { launchTarget, openInApp, render } from "./board.js";

export function renderRepoSelect() {
  const repos = getRepos();
  const current = getCurrentRepo();
  const sel = $("#repo-select");
  sel.innerHTML = `<option value="">Everything</option>`
    + repos.map((r) => `<option value="${esc(r)}" ${r === current ? "selected" : ""}>${esc(r)}</option>`).join("");
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
  saveViews(remaining, wasCurrent ? "" : getCurrentRepo());
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

// ---------- the settings page, at #settings/<pane> ----------
// Every pane but Views is built from the server's field list.
let settingSections = [];
let settingFields = [];
let settingsLoaded = false;
let commandLineRoots = [];
let foundFolders = [];
let pane = "views";
let openedFromBoard = false;
const VIEWS_PANE = { id: "views", title: "Views" };

$("#settings-btn").addEventListener("click", () => {
  openedFromBoard = true;
  location.hash = "#settings";
});

// The browser's back button and Back to board both return to the board, not through panes.
$("#settings-back").addEventListener("click", () => {
  if (openedFromBoard) return history.back();
  history.replaceState(null, "", location.pathname + location.search);
  route();
});

$("#set-nav").addEventListener("click", (e) => {
  const item = e.target.closest("[data-pane]");
  if (!item) return;
  e.preventDefault();
  history.replaceState(null, "", `#settings/${item.dataset.pane}`);
  route();
});

addEventListener("hashchange", route);

export function route() {
  const match = location.hash.match(/^#settings(?:\/([\w-]+))?$/);
  document.body.classList.toggle("settings-open", !!match);
  $("#settings-page").classList.toggle("hidden", !match);
  if (!match) {
    settingsLoaded = false;
    openedFromBoard = false;
    return;
  }
  pane = match[1] || "views";
  renderViewsList();
  renderSettings();
  if (!settingsLoaded) loadSettings();
  scrollTo(0, 0);
  $("#set-nav .active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

async function loadSettings() {
  settingsLoaded = true;
  try {
    ({ sections: settingSections, fields: settingFields, commandLineRoots } = await apiGet("api/settings"));
    foundFolders = await folderCandidates();
    renderSettings();
    $("#set-nav .active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  } catch (err) {
    $("#settings-fields").innerHTML = `<p class="set-error">${esc(err.message)}</p>`;
  }
}

const fieldKey = (f) => f.path.join(".");
const fieldAt = (key) => settingFields.find((f) => fieldKey(f) === key);
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

function shownValue(f, value) {
  if (f.kind === "switch") return value ? "On" : "Off";
  if (f.choices) return f.choices.find(([v]) => v === value)?.[1] || String(value);
  if (value === "" || value == null) return "none";
  return f.kind === "lines" ? value.split("\n").join(", ") : String(value);
}

const controlId = (f) => `set-${fieldKey(f).replace(/[^\w]/g, "-")}`;

function settingControl(f, id) {
  const mono = /url|command|prompt|roots/.test(fieldKey(f)) ? " set-mono" : "";
  switch (f.kind) {
    case "segments":
      return `<span class="set-seg" id="${id}">${f.choices.map(([v, label]) =>
        `<button type="button" data-value="${esc(v)}" aria-pressed="${v === f.value}">${esc(label)}</button>`).join("")}</span>`;
    case "switch":
      return `<input type="checkbox" id="${id}" class="set-switch"${f.value ? " checked" : ""}>`;
    case "locked":
      return `<span class="set-locked">Always manual</span>`;
    case "choice":
      return `<select id="${id}">${f.choices.map(([v, label]) =>
        `<option value="${esc(v)}"${v === f.value ? " selected" : ""}>${esc(label)}</option>`).join("")}</select>`;
    case "number":
      return `<input type="number" id="${id}" min="0" step="any" value="${esc(f.value)}">`;
    case "lines":
      return `<textarea id="${id}" rows="3" class="${mono.trim()}">${esc(f.value)}</textarea>`;
    default:
      return `<input type="text" id="${id}" class="${mono.trim()}" value="${esc(f.value)}" spellcheck="false">`;
  }
}

const isChanged = (f) => f.set && !same(f.value, f.default);

// A child named in its setting's sentence, shown as a small control inside that sentence.
function inlineControl(f) {
  const title = isChanged(f) ? ` title="Default: ${esc(shownValue(f, f.default))}"` : "";
  const control = settingControl(f, controlId(f)).replace(/^<(\w+)/, `<$1 class="set-mini"${title}`);
  return `<span class="set-inline${isChanged(f) ? " changed" : ""}" data-path="${esc(fieldKey(f))}">${control}</span>`;
}

const inlineKids = (f, fields) => f.sentence && isOn(f)
  ? fields.filter((c) => c.parent && same(c.parent, f.path) && f.sentence.includes(`{${c.path.at(-1)}}`)) : [];

function settingRow(f, depth, errors, kids = []) {
  const key = fieldKey(f);
  const id = controlId(f);
  const inline = f.kind === "switch" || f.kind === "locked" || f.kind === "segments";
  const control = settingControl(f, id);
  const help = kids.length
    ? esc(f.sentence).replace(/\{(\w+)\}/g, (m, name) => { const c = kids.find((k) => k.path.at(-1) === name); return c ? inlineControl(c) : m; })
    : esc(f.help || "");
  const resettable = [f, ...kids].filter((x) => x.set);
  const changed = [f, ...kids].filter(isChanged);
  const note = !resettable.length ? "" : `<span class="set-default">${changed.length
    ? `Default: ${changed.map((x) => esc(shownValue(x, x.default))).join(", ")}` : "Set to the default in the file"}</span>`
    + `<button type="button" class="set-reset" data-reset="${esc(resettable.map(fieldKey).join(" "))}">Reset</button>`;
  const dot = changed.length ? `<span class="set-dot" title="Changed from the default"></span>` : "";
  const error = [f, ...kids].map((x) => errors[fieldKey(x)]).find(Boolean);
  return `<div class="set-row${depth ? ` set-child set-depth${depth}` : ""}" data-path="${esc(key)}">
    <div class="set-head"><label for="${id}">${esc(f.label)}</label>${dot}${inline ? control : ""}</div>
    ${inline ? "" : control}
    ${help || note ? `<div class="set-note">${help ? `<span>${help}</span>` : ""}${note}</div>` : ""}
    ${error ? `<div class="set-error">${esc(error)}</div>` : ""}
  </div>`;
}

const changedIn = (section) => settingFields.filter((f) => f.section === section && f.set && !same(f.value, f.default)).length;

const unusedByMode = (f) => {
  const key = f.path.at(-1);
  const mode = fieldAt([...f.path.slice(0, -1), "mode"].join("."));
  return !!mode && (key === "url" || key === "command") && mode.value !== key;
};

const isOn = (f) => f.value !== false && f.value !== "off";
const shown = (f) => { const home = homeOf(f); return !home || (isOn(home) && shown(home)); };

// A setting sits under its parent or the setting it's `under`, and shows only while that one is on.
function homeOf(f) {
  const path = f.under || f.parent;
  return path ? fieldAt(path.join(".")) : null;
}

// Settings a choice above fills in (an app, an agent) sit under one Advanced heading. A
// section with `cards` shows each top-level setting as a card holding what's under it.
function sectionRows(section, errors) {
  const fields = settingFields.filter((f) => f.section === section.id && !unusedByMode(f));
  // A step's agent pass comes before the step's own options.
  const under = (home) => fields.filter((f) => homeOf(f) === home).sort((a, b) => !!b.under - !!a.under);
  let headed = false;
  const rows = (home, depth, skip = []) => under(home).filter((f) => !skip.includes(f)).map((f) => {
    const kids = inlineKids(f, fields);
    const inner = isOn(f) ? rows(f, depth + 1, kids) : "";
    let heading = "";
    if (f.group === "advanced" && !headed) {
      headed = true;
      heading = `<div class="set-group${depth ? ` set-child set-depth${depth}` : ""}">
        <span>Advanced</span> Filled in by your choice above; change one only to override it.</div>`;
    }
    if (f.heading) heading = `<h3 class="set-heading">${esc(f.heading)}</h3>` + heading;
    const row = settingRow(f, depth, errors, kids);
    if (section.cards && !depth) return `${heading}<div class="set-card">${row}${inner ? `<div class="set-card-body">${inner}</div>` : ""}</div>`;
    return heading + row + inner;
  }).join("");
  return rows(null, 0);
}

function renderSettings(errors = {}) {
  const focused = document.activeElement?.id;
  const panes = [VIEWS_PANE, ...settingSections];
  if (settingsLoaded && settingSections.length && !panes.some((p) => p.id === pane)) pane = "views";
  $("#set-nav").innerHTML = panes.map((p) => {
    const count = p.id === "views" ? 0 : changedIn(p.id);
    return `<a href="#settings/${p.id}" data-pane="${p.id}" class="set-nav-item${p.id === pane ? " active" : ""}"${p.id === pane ? ' aria-current="page"' : ""}>
      <span>${esc(p.title)}</span>${count ? `<span class="set-nav-count" title="${count} changed from the default">${count}</span>` : ""}</a>`;
  }).join("");
  $("#set-views").classList.toggle("hidden", pane !== "views");
  const section = settingSections.find((s) => s.id === pane);
  const fixed = pane === "roots" && commandLineRoots.length
    ? `<div class="set-held">Folders given on the command line hold for this run: ${esc(commandLineRoots.join(", "))}. The ones here apply when Shipyard starts without any.</div>` : "";
  const rootsField = fieldAt("roots");
  const found = pane === "roots" && rootsField && !rootsField.value && foundFolders.length
    ? foundFolders.map((c) => `<div class="set-found"><span>Found your code in <code>${esc(c.path)}</code>
        (${c.clones} clone${c.clones === 1 ? "" : "s"}${c.matching ? `, ${c.matching} from your PRs` : ""})</span>
        <button type="button" class="primary" data-use-folder="${esc(c.path)}">Use</button></div>`).join("") : "";
  const held = automationHeld() && pane === "rules"
    ? `<div class="set-held">Held for now: until you turn it on, passes wait for Ask and nothing is pressed for you.
        <button type="button" class="primary" data-automation="on">Turn it on</button></div>` : "";
  $("#settings-fields").innerHTML = section ? `<h2>${esc(section.title)}</h2><p class="set-intro">${esc(section.help)}</p>
    ${fixed}${found}${held}${sectionRows(section, errors)}` : "";
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
  const f = fieldAt(e.target.closest("[data-path]")?.dataset.path);
  if (!f) return;
  if (f.kind === "switch") return saveField(f, { value: e.target.checked });
  if (f.kind === "number") {
    return e.target.value === "" ? saveField(f, { reset: true }) : saveField(f, { value: Number(e.target.value) });
  }
  saveField(f, { value: e.target.value });
});

$("#settings-fields").addEventListener("click", async (e) => {
  const use = e.target.closest("[data-use-folder]");
  if (use) return saveField(fieldAt("roots"), { value: use.dataset.useFolder });
  if (e.target.closest("[data-automation]")) {
    await turnOnAutomation();
    return renderSettings();
  }
  const seg = e.target.closest(".set-seg [data-value]");
  if (seg) return saveField(fieldAt(seg.closest("[data-path]").dataset.path), { value: seg.dataset.value });
  const btn = e.target.closest("[data-reset]");
  if (btn) for (const key of btn.dataset.reset.split(" ")) await saveField(fieldAt(key), { reset: true });
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
  const available = localRepos.filter((r) => !targets.length ||
    targets.some((t) => t.includes("/") ? r === t : r.split("/")[0] === t)
  ).sort((a, b) => seenAt(b) - seenAt(a) || a.localeCompare(b));
  return available.map((r, i) =>
    `<option value="${esc(r)}"${i === 0 ? " selected" : ""}>${esc(r)}</option>`).join("");
}

export function openNewTaskDialog() {
  const html = ntRepoOptionsHtml();
  const scanning = (state.serverState?.roots || []).length > 0;
  $("#nt-repo").innerHTML = html || `<option value="">— ${scanning ? "no local clones in this view" : "not scanning any folders yet"} —</option>`;
  const branch = $("#nt-branch");
  branch.value = "";  // start blank so the default action is "Open default"
  const status = $("#nt-status");
  status.innerHTML = scanning ? ""
    : 'Shipyard works from your local clones here. <a href="#settings/roots" data-close-nt>Add a folder to scan in Settings</a>.';
  status.classList.remove("error");
  updateNtButtons();
  $("#new-task-dialog").showModal();
  branch.focus();
  branch.setSelectionRange(branch.value.length, branch.value.length);
}

$("#nt-status").addEventListener("click", (e) => {
  if (e.target.closest("[data-close-nt]")) $("#new-task-dialog").close();
});

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
