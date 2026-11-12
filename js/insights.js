// The Insights tab: how your merges and reviews have gone over the last 12 weeks, drawn as
// plain SVG from one cached server read, plus what Shipyard did lately.
import { state } from "./base.js";
import { apiGet, getCurrentRepo } from "./server.js";
import { prKey } from "./data.js";
import { esc } from "./html.js";

const WEEKS = 12;
const DAY_MS = 86_400_000;
const FRESH_MS = 15 * 60_000;
const RETRY_MS = 60_000;
// Matches the stylesheet's breakpoint where the panels stack into one column.
export const stackedPanels = matchMedia("(max-width: 900px)");
const SERIES = [{ key: "mine", name: "Yours" }, { key: "review", name: "Reviewed by you" }];

let merged = null;   // { view, at, since, merged: { mine: [row], review: [row] }, shipyard }
let loading = false;
let failed = null;   // { view, at, message }

function load(view, rerender) {
  const fresh = merged?.view === view && Date.now() - merged.at < FRESH_MS;
  const backingOff = failed?.view === view && Date.now() - failed.at < RETRY_MS;
  if (loading || fresh || backingOff) return;
  loading = true;
  apiGet(`api/insights?view=${encodeURIComponent(view)}`)
    .then((data) => { merged = { ...data, view, at: Date.now() }; failed = null; })
    .catch((err) => { failed = { view, at: Date.now(), message: err.message }; })
    .finally(() => { loading = false; rerender(); });
}

function weekStart(date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  return d;
}

function weeks() {
  const last = weekStart(new Date());
  return Array.from({ length: WEEKS }, (_, i) => {
    const start = new Date(last);
    start.setDate(start.getDate() - (WEEKS - 1 - i) * 7);
    return start;
  });
}

const shortDate = (d) => d.toLocaleDateString([], { month: "short", day: "numeric" });

function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = sorted.length >> 1;
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

// The axis top: four even steps, each a 1, 2, or 5 times a power of ten.
function niceMax(value, minStep = 1) {
  const raw = Math.max(value / 4, minStep);
  const pow = 10 ** Math.floor(Math.log10(raw));
  return 4 * [1, 2, 5, 10].map((m) => m * pow).find((m) => m >= raw);
}

const fmtDays = (days) => days < 1 ? `${Math.round(days * 24)}h`
  : `${Number.isInteger(days) || days >= 10 ? Math.round(days) : days.toFixed(1)}d`;

// Every chart is H tall in its own units; a full-width one is wider so its text stays the same size.
const H = 200, PAD = { top: 12, right: 44, bottom: 24, left: 32 };
const plotH = H - PAD.top - PAD.bottom;
const yAt = (v, max) => PAD.top + plotH - (v / max) * plotH;

function axisHtml(W, max, fmt, labels) {
  const plotW = W - PAD.left - PAD.right;
  const slot = plotW / labels.length;
  const grid = [0, 1, 2, 3, 4].map((i) => {
    const v = (max * i) / 4;
    const y = yAt(v, max);
    return `<line class="grid" x1="${PAD.left}" x2="${W - PAD.right}" y1="${y}" y2="${y}"/>`
      + `<text class="tick" x="${PAD.left - 6}" y="${y + 3}" text-anchor="end">${esc(fmt(v))}</text>`;
  }).join("");
  const every = labels.length > 6 ? 2 : 1;
  const xs = labels.map((label, i) => (labels.length - 1 - i) % every ? "" :
    `<text class="tick" x="${PAD.left + slot * (i + 0.5)}" y="${H - 6}" text-anchor="middle">${esc(label)}</text>`).join("");
  return grid + xs;
}

// A column with a 4px rounded cap and a square foot on the baseline.
function columnPath(x, y, w, base) {
  const r = Math.min(4, w / 2, base - y);
  return `M${x},${base}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${base}Z`;
}

function legendHtml(names = {}) {
  return `<div class="ins-legend">${SERIES.map((s) => `<span><i class="sw s-${s.key}"></i>${esc(names[s.key] || s.name)}</span>`).join("")}</div>`;
}

function tableHtml(head, rows) {
  return `<details class="ins-table"><summary>Show as table</summary><table>
    <thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead>
    <tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></details>`;
}

// groups: [{ label, tip, values: { mine, review } }]
function columnChart(groups, title, { W = 600, names = {}, fmt = (v) => String(Math.round(v)), minStep = 1 } = {}) {
  const plotW = W - PAD.left - PAD.right;
  const max = niceMax(Math.max(minStep, ...groups.flatMap((g) => SERIES.map((s) => g.values[s.key] || 0))), minStep);
  const slot = plotW / groups.length;
  const barW = Math.min(18, (slot - 8) / 2);
  const base = yAt(0, max);
  const bars = groups.map((g, i) => {
    const left = PAD.left + slot * i + (slot - (barW * 2 + 2)) / 2;
    const cols = SERIES.map((s, j) => {
      const v = g.values[s.key];
      return v ? `<path class="s-${s.key}" d="${columnPath(left + j * (barW + 2), yAt(v, max), barW, base)}"/>` : "";
    }).join("");
    const tip = `${g.tip}\n${SERIES.map((s) => `${names[s.key] || s.name}: ${g.values[s.key] == null ? "–" : fmt(g.values[s.key])}`).join("\n")}`;
    return `<g class="hit"><title>${esc(tip)}</title><rect x="${PAD.left + slot * i}" y="${PAD.top}" width="${slot}" height="${plotH}"/>${cols}</g>`;
  }).join("");
  return `<svg class="ins-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">
    ${axisHtml(W, max, (v) => v ? fmt(v) : "0", groups.map((g) => g.label))}${bars}</svg>`;
}

// series values: per group, a number or null for a week with nothing to measure.
function lineChart(labels, tips, values, title, names = {}, W = 600) {
  const plotW = W - PAD.left - PAD.right;
  const all = SERIES.flatMap((s) => values[s.key]).filter((v) => v != null);
  const max = niceMax(Math.max(...all, 0), 0.25);
  const slot = plotW / labels.length;
  const x = (i) => PAD.left + slot * (i + 0.5);
  // Each line's last value labels its end; two ends that would overlap are pushed apart.
  const ends = SERIES.map((s) => {
    const i = values[s.key].findLastIndex((v) => v != null);
    return i < 0 ? null : { x: x(i), y: yAt(values[s.key][i], max) + 3, v: values[s.key][i] };
  });
  if (ends[0] && ends[1] && ends[0].x === ends[1].x && Math.abs(ends[0].y - ends[1].y) < 11) {
    const [hi, lo] = ends[0].y <= ends[1].y ? ends : [ends[1], ends[0]];
    const mid = (hi.y + lo.y) / 2;
    hi.y = mid - 5.5;
    lo.y = mid + 5.5;
  }
  const lines = SERIES.map((s, j) => {
    const pts = values[s.key].map((v, i) => v == null ? null : [x(i), yAt(v, max), v, i]).filter(Boolean);
    let d = "";
    values[s.key].forEach((v, i) => {
      if (v == null) return;
      d += `${i && values[s.key][i - 1] != null ? "L" : "M"}${x(i)},${yAt(v, max)}`;
    });
    const e = ends[j];
    const end = e ? `<text class="end-label" x="${e.x + 8}" y="${e.y}">${esc(fmtDays(e.v))}</text>` : "";
    const dots = pts.map(([px, py, v, i]) => `<circle class="s-${s.key}" cx="${px}" cy="${py}" r="4"><title>${esc(`${tips[i]}\n${names[s.key] || s.name}: ${fmtDays(v)} median`)}</title></circle>`).join("");
    return `<path class="line s-${s.key}" d="${d}"/>${dots}${end}`;
  }).join("");
  return `<svg class="ins-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">
    ${axisHtml(W, max, (v) => v ? fmtDays(v) : "0", labels)}${lines}</svg>`;
}

function panel(title, sub, body, wide = false) {
  return `<section class="ins-panel${wide ? " wide" : ""}"><div class="ins-h">${esc(title)}</div>${sub ? `<div class="ins-sub">${esc(sub)}</div>` : ""}${body}</section>`;
}

function tile(value, label, note = "") {
  return `<div class="ins-tile"><div class="ins-value">${esc(value)}</div><div class="ins-label">${esc(label)}</div>${note ? `<div class="ins-note">${esc(note)}</div>` : ""}</div>`;
}

// Bots' PRs (dependency bumps) would swamp every review number, so they're left out.
const people = (rows) => rows.filter((r) => !r.bot);

// When a row got its first review that counts: anyone but the author on yours, you on theirs.
function firstReviewAt(row, kind) {
  const times = (row.reviews || []).filter(([by]) => kind === "mine" ? by && by !== row.author : by === state.viewer)
    .map(([, at]) => new Date(at).getTime());
  return times.length ? Math.min(...times) : null;
}
const waitDays = (row, kind) => {
  const at = firstReviewAt(row, kind);
  return at == null ? null : Math.max(0, at - new Date(row.created)) / DAY_MS;
};
const mergeDays = (row) => (new Date(row.merged) - new Date(row.created)) / DAY_MS;
const recent = (rows, days) => rows.filter((r) => Date.now() - new Date(r.merged) < days * DAY_MS);
const medianOf = (rows, fn) => median(rows.map(fn).filter((v) => v != null));

function tiles(rows) {
  const reviewed = new Set();
  for (const pr of [...state.reviewedPrs, ...state.reviewedMerged]) {
    const at = pr.viewerLatestReview?.submittedAt;
    if (pr.author?.login !== state.viewer && pr.author?.__typename !== "Bot" && at && Date.now() - new Date(at) < 7 * DAY_MS) reviewed.add(prKey(pr));
  }
  const thisWeek = weekStart(new Date());
  const days = (v) => v == null ? "–" : fmtDays(v);
  const firstOnYours = rows && medianOf(recent(rows.mine, 28), (r) => waitDays(r, "mine"));
  const yourTurnaround = rows && medianOf(recent(people(rows.review), 28), (r) => waitDays(r, "review"));
  return `<div class="ins-tiles">
    ${tile(rows ? String(rows.mine.filter((r) => new Date(r.merged) >= thisWeek).length) : "…", "Merged this week", "your PRs")}
    ${tile(String(reviewed.size), "Reviewed in the last 7 days")}
    ${tile(rows ? days(firstOnYours) : "…", "Until your PRs' first review", "median, last 4 weeks")}
    ${tile(rows ? days(yourTurnaround) : "…", "Your review turnaround", "median from opening, last 4 weeks")}
  </div>`;
}

function trendCharts(rows) {
  const starts = weeks();
  const labels = starts.map(shortDate);
  const tips = starts.map((d) => `Week of ${shortDate(d)}`);
  const weekOf = (iso) => {
    const t = new Date(iso).getTime();
    return starts.findLastIndex((s) => t >= s.getTime());
  };
  const byWeek = {};
  for (const s of SERIES) {
    byWeek[s.key] = starts.map(() => []);
    for (const r of (s.key === "review" ? people(rows.review) : rows.mine)) {
      const i = weekOf(r.merged);
      if (i >= 0) byWeek[s.key][i].push(r);
    }
  }
  const weekly = (fn) => Object.fromEntries(SERIES.map((s) => [s.key, byWeek[s.key].map((rs) => medianOf(rs, (r) => fn(r, s.key)))]));
  const table = (values) => tableHtml(["Week of", ...SERIES.map((s) => s.name)], labels.map((l, i) => [l, ...SERIES.map((s) =>
    values[s.key][i] == null ? "–" : fmtDays(values[s.key][i]))]));
  const groups = starts.map((_, i) => ({ label: labels[i], tip: tips[i],
    values: Object.fromEntries(SERIES.map((s) => [s.key, byWeek[s.key][i].length])) }));
  const perWeek = panel("Merged per week", "PRs you authored and PRs you reviewed, by the week they merged. Bot PRs are left out.", legendHtml()
    + columnChart(groups, "Merged per week", { W: stackedPanels.matches ? 600 : 1240 })
    + tableHtml(["Week of", ...SERIES.map((s) => s.name)], groups.map((g) => [g.label, ...SERIES.map((s) => g.values[s.key])])), true);
  const merge = weekly(mergeDays);
  const toMerge = panel("Time to merge", "Median time from opening to merging, by the week they merged", legendHtml()
    + lineChart(labels, tips, merge, "Median time to merge") + table(merge));
  const first = weekly(waitDays);
  const names = { mine: "First review on yours", review: "Your first review" };
  const toReview = panel("Time to first review", "Median time from opening to the first review: anyone's on your PRs, yours on others'",
    legendHtml(names) + lineChart(labels, tips, first, "Median time to first review", names) + table(first));
  return { perWeek, toMerge, toReview };
}

const SIZES = [["Under 50", 0, 50], ["50–199", 50, 200], ["200–499", 200, 500], ["500–999", 500, 1000], ["1,000+", 1000, Infinity]];

function sizeChart(rows) {
  const sets = { mine: rows.mine, review: people(rows.review) };
  const groups = SIZES.map(([label, lo, hi]) => {
    const values = {}, counts = {};
    for (const s of SERIES) {
      const inSize = sets[s.key].filter((r) => r.size >= lo && r.size < hi);
      values[s.key] = medianOf(inSize, mergeDays);
      counts[s.key] = inSize.length;
    }
    return { label, tip: `${label} lines changed`, values, counts };
  });
  return panel("Size and time to merge", "Median time to merge by lines changed. Smaller PRs usually merge sooner.",
    legendHtml() + columnChart(groups, "Median time to merge by PR size", { W: stackedPanels.matches ? 600 : 1240, fmt: fmtDays, minStep: 0.25 })
    + tableHtml(["Lines changed", ...SERIES.flatMap((s) => [s.name, "PRs"])], groups.map((g) =>
      [g.label, ...SERIES.flatMap((s) => [g.values[s.key] == null ? "–" : fmtDays(g.values[s.key]), g.counts[s.key]])])), true);
}

// The people you trade reviews with most: count, and how fast the first review came.
function peopleTable(rows, kind, who, head) {
  const by = new Map();
  for (const r of rows) {
    const names = kind === "mine"
      ? [...new Set((r.reviews || []).map(([login]) => login).filter((l) => l && l !== r.author))]
      : [r.author];
    for (const name of names) {
      if (!by.has(name)) by.set(name, []);
      by.get(name).push(r);
    }
  }
  const ranked = [...by].sort((a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0])).slice(0, 6);
  if (!ranked.length) return `<div class="ins-empty">${esc(who)}</div>`;
  const most = ranked[0][1].length;
  const wait = (name, rs) => medianOf(rs, (r) => {
    if (kind !== "mine") return waitDays(r, "review");
    const at = (r.reviews || []).filter(([l]) => l === name).map(([, t]) => new Date(t).getTime());
    return at.length ? Math.max(0, Math.min(...at) - new Date(r.created)) / DAY_MS : null;
  });
  return `<table class="ins-people bars"><thead><tr><th>${esc(head)}</th><th>PRs</th><th>First review</th></tr></thead><tbody>${ranked.map(([name, rs]) => {
    const w = wait(name, rs);
    return `<tr><td>${esc(name)}</td><td><span class="ins-bar s-${kind}" style="width:${Math.round((rs.length / most) * 100)}%"></span>${rs.length}</td><td>${w == null ? "–" : fmtDays(w)}</td></tr>`;
  }).join("")}</tbody></table>`;
}

function peoplePanels(rows) {
  return panel("Who reviews your PRs", "On your PRs merged in the last 12 weeks, with how soon each one first reviewed",
    peopleTable(rows.mine, "mine", "Nobody has reviewed your merged PRs yet.", "Reviewer"))
    + panel("Whose PRs you review", "PRs you reviewed that merged in the last 12 weeks, with how soon you first reviewed. Bot PRs are left out.",
      peopleTable(people(rows.review), "review", "No reviewed PRs merged yet.", "Author"));
}

function repoPanel(rows) {
  const repos = new Map();
  for (const s of SERIES) {
    for (const r of s.key === "review" ? people(rows.review) : rows.mine) {
      if (!r.repo) continue;
      if (!repos.has(r.repo)) repos.set(r.repo, { mine: [], review: [] });
      repos.get(r.repo)[s.key].push(r);
    }
  }
  if (repos.size < 2) return "";
  const sorted = [...repos].sort((a, b) => (b[1].mine.length + b[1].review.length) - (a[1].mine.length + a[1].review.length));
  const days = (v) => v == null ? "–" : fmtDays(v);
  return panel("By repository", "Merges in the last 12 weeks, and median time to merge, per repository",
    `<table class="ins-people"><thead><tr><th>Repository</th><th>Yours</th><th>Time to merge</th><th>Reviewed by you</th></tr></thead><tbody>${sorted.map(([repo, r]) =>
      `<tr><td>${esc(repo)}</td><td>${r.mine.length}</td><td>${days(medianOf(r.mine, mergeDays))}</td><td>${r.review.length}</td></tr>`).join("")}</tbody></table>`, true);
}

const PASS_NAMES = { pre_review: "Pre-reviews", re_review: "Re-reviews", ci_failed: "CI failures read", feedback: "Feedback plans",
  conflicts: "Conflicts resolved", ask: "Questions answered", new_session: "Sessions started" };
const ACTION_NAMES = { rerun_checks: ["re-run", "re-runs"], approve: ["approval", "approvals"], apply_fix: ["fix", "fixes"],
  enable_auto_merge: ["auto-merge", "auto-merges"] };

function shipyardPanel(work) {
  if (!work) return "";
  const passes = Object.entries(work.passes).sort((a, b) => b[1] - a[1]);
  const list = (counts) => Object.entries(counts).map(([a, n]) => {
    const [one, many] = ACTION_NAMES[a] || [a.replace(/_/g, " "), a.replace(/_/g, " ")];
    return `${n} ${n === 1 ? one : many}`;
  }).join(", ");
  const held = Object.values(work.held).reduce((a, b) => a + b, 0);
  const window = work.oldest && work.oldest > work.since ? `Since ${shortDate(new Date(work.oldest))}` : "Last 4 weeks";
  const body = passes.length
    ? `<div class="ins-work">${passes.map(([name, n]) => `<div><div class="ins-value">${n}</div><div class="ins-label">${esc(PASS_NAMES[name] || name)}</div></div>`).join("")}</div>
      <p class="ins-sub">${Object.keys(work.auto).length ? `Acted on its own: ${esc(list(work.auto))}.` : "Took no action on its own."}${held ? ` Left ${held} for you to decide.` : ""}</p>`
    : `<div class="ins-empty">Shipyard hasn't run anything for you yet.</div>`;
  return panel("What Shipyard did", `${window}, for PRs it still tracks`, body, true);
}

export function insightsHtml(rerender) {
  const view = getCurrentRepo();
  load(view, rerender);
  const data = merged?.view === view ? merged : null;
  const rows = data?.merged || null;
  if (rows) {
    const { perWeek, toMerge, toReview } = trendCharts(rows);
    return `<div class="insights">${tiles(rows)}${perWeek}${toMerge}${toReview}${peoplePanels(rows)}${sizeChart(rows)}${repoPanel(rows)}${shipyardPanel(data.shipyard)}</div>`;
  }
  const waitBody = failed?.view === view ? `<div class="error-box">${esc(failed.message)}</div>`
    : '<div class="loading"><div class="spinner"></div>Loading the last 12 weeks of merges…</div>';
  return `<div class="insights">${tiles(rows)}${panel("Merge trends", "", waitBody, true)}</div>`;
}
