// The Insights tab: your review load and how merges are trending, drawn as plain SVG from
// the PRs the page already has plus one cached server read of the last 12 weeks of merges.
import { state } from "./base.js";
import { apiGet, getCurrentRepo } from "./server.js";
import { prKey } from "./data.js";
import { esc, timeAgo } from "./html.js";
import { reviewItem, reviewLaneOf, serverPr } from "./prs.js";

const WEEKS = 12;
const DAY_MS = 86_400_000;
const FRESH_MS = 15 * 60_000;
const RETRY_MS = 60_000;
// Matches the stylesheet's breakpoint where the panels stack into one column.
export const stackedPanels = matchMedia("(max-width: 900px)");
const SERIES = [{ key: "mine", name: "Yours" }, { key: "review", name: "Reviewed by you" }];

let merged = null;   // { view, at, since, merged: { mine: [{created, merged}], review: [...] } }
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
function columnChart(groups, title, { W = 600, names = {} } = {}) {
  const plotW = W - PAD.left - PAD.right;
  const max = niceMax(Math.max(1, ...groups.flatMap((g) => SERIES.map((s) => g.values[s.key]))));
  const slot = plotW / groups.length;
  const barW = Math.min(18, (slot - 8) / 2);
  const base = yAt(0, max);
  const bars = groups.map((g, i) => {
    const left = PAD.left + slot * i + (slot - (barW * 2 + 2)) / 2;
    const cols = SERIES.map((s, j) => {
      const v = g.values[s.key];
      return v ? `<path class="s-${s.key}" d="${columnPath(left + j * (barW + 2), yAt(v, max), barW, base)}"/>` : "";
    }).join("");
    const tip = `${g.tip}\n${SERIES.map((s) => `${names[s.key] || s.name}: ${g.values[s.key]}`).join("\n")}`;
    return `<g class="hit"><title>${esc(tip)}</title><rect x="${PAD.left + slot * i}" y="${PAD.top}" width="${slot}" height="${plotH}"/>${cols}</g>`;
  }).join("");
  return `<svg class="ins-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">
    ${axisHtml(W, max, (v) => String(Math.round(v)), groups.map((g) => g.label))}${bars}</svg>`;
}

// series values: per group, a number or null for a week with nothing to measure.
function lineChart(labels, tips, values, title, W = 600) {
  const plotW = W - PAD.left - PAD.right;
  const all = SERIES.flatMap((s) => values[s.key]).filter((v) => v != null);
  const max = niceMax(Math.max(...all, 0), 0.25);
  const slot = plotW / labels.length;
  const x = (i) => PAD.left + slot * (i + 0.5);
  const lines = SERIES.map((s) => {
    const pts = values[s.key].map((v, i) => v == null ? null : [x(i), yAt(v, max), v, i]).filter(Boolean);
    let d = "";
    values[s.key].forEach((v, i) => {
      if (v == null) return;
      d += `${i && values[s.key][i - 1] != null ? "L" : "M"}${x(i)},${yAt(v, max)}`;
    });
    const last = pts.at(-1);
    const end = last ? `<text class="end-label" x="${last[0] + 8}" y="${last[1] + 3}">${esc(fmtDays(last[2]))}</text>` : "";
    const dots = pts.map(([px, py, v, i]) => `<circle class="s-${s.key}" cx="${px}" cy="${py}" r="4"><title>${esc(`${tips[i]}\n${s.name}: ${fmtDays(v)} median`)}</title></circle>`).join("");
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

function reviewWaiting() {
  const open = [...new Map([...state.prs, ...state.reviewedPrs].map((pr) => [prKey(pr), pr])).values()];
  return open.filter((pr) => reviewItem(pr) && reviewLaneOf(pr) === 0);
}

function loadTiles(rows) {
  const waiting = reviewWaiting();
  const since = waiting.map((pr) => serverPr(pr)?.waitingSince).filter(Boolean).sort()[0];
  const reviewed = new Set();
  for (const pr of [...state.reviewedPrs, ...state.reviewedMerged]) {
    const at = pr.viewerLatestReview?.submittedAt;
    if (pr.author?.login !== state.viewer && at && Date.now() - new Date(at) < 7 * DAY_MS) reviewed.add(prKey(pr));
  }
  const thisWeek = weekStart(new Date());
  const mergedThisWeek = rows ? rows.mine.filter((r) => new Date(r.merged) >= thisWeek).length : null;
  return `<div class="ins-tiles">
    ${tile(String(waiting.length), "Waiting on your review")}
    ${tile(since ? timeAgo(since).replace(/ ago$/, "") : "None", "Oldest review request", since ? `since ${shortDate(new Date(since))}` : "")}
    ${tile(String(reviewed.size), "Reviewed in the last 7 days")}
    ${tile(mergedThisWeek == null ? "…" : String(mergedThisWeek), "Merged this week", "your PRs")}
  </div>`;
}

function ageChart() {
  const buckets = [["Under 1 day", 0, 1], ["1–3 days", 1, 3], ["3–7 days", 3, 7], ["Over a week", 7, Infinity]];
  const ages = {
    mine: state.prs.filter((pr) => pr.author?.login === state.viewer).map((pr) => (Date.now() - new Date(pr.createdAt)) / DAY_MS),
    review: reviewWaiting().map((pr) => serverPr(pr)?.waitingSince || pr.createdAt).map((at) => (Date.now() - new Date(at)) / DAY_MS),
  };
  const groups = buckets.map(([label, lo, hi]) => ({
    label, tip: label,
    values: Object.fromEntries(SERIES.map((s) => [s.key, ages[s.key].filter((a) => a >= lo && a < hi).length])),
  }));
  const names = { review: "Waiting on your review" };
  return panel("Open PR age", "Your open PRs by age, and reviews waiting on you by how long they've waited",
    legendHtml(names) + columnChart(groups, "Open PR age", { names })
    + tableHtml(["Age", "Yours", names.review], groups.map((g) => [g.label, ...SERIES.map((s) => g.values[s.key])])));
}

function trendCharts(rows) {
  const starts = weeks();
  const labels = starts.map(shortDate);
  const tips = starts.map((d) => `Week of ${shortDate(d)}`);
  const weekOf = (iso) => {
    const t = new Date(iso).getTime();
    return starts.findLastIndex((s) => t >= s.getTime());
  };
  const counts = {}, days = {};
  for (const s of SERIES) {
    counts[s.key] = starts.map(() => 0);
    days[s.key] = starts.map(() => []);
    for (const r of rows[s.key] || []) {
      const i = weekOf(r.merged);
      if (i < 0) continue;
      counts[s.key][i] += 1;
      days[s.key][i].push((new Date(r.merged) - new Date(r.created)) / DAY_MS);
    }
  }
  const medians = Object.fromEntries(SERIES.map((s) => [s.key, days[s.key].map(median)]));
  const groups = starts.map((_, i) => ({ label: labels[i], tip: tips[i],
    values: Object.fromEntries(SERIES.map((s) => [s.key, counts[s.key][i]])) }));
  const perWeek = panel("Merged per week", "PRs you authored and PRs you reviewed, by the week they merged", legendHtml()
    + columnChart(groups, "Merged per week", { W: stackedPanels.matches ? 600 : 1240 })
    + tableHtml(["Week of", ...SERIES.map((s) => s.name)], groups.map((g) => [g.label, ...SERIES.map((s) => g.values[s.key])])), true);
  const toMerge = panel("Time to merge", "Median time from opening to merging, by the week they merged", legendHtml()
    + lineChart(labels, tips, medians, "Median time to merge")
    + tableHtml(["Week of", ...SERIES.map((s) => s.name)], labels.map((l, i) => [l, ...SERIES.map((s) =>
      medians[s.key][i] == null ? "–" : fmtDays(medians[s.key][i]))])));
  return { perWeek, toMerge };
}

export function insightsHtml(rerender) {
  const view = getCurrentRepo();
  load(view, rerender);
  const rows = merged?.view === view ? merged.merged : null;
  if (rows) {
    const { perWeek, toMerge } = trendCharts(rows);
    return `<div class="insights">${loadTiles(rows)}${perWeek}${toMerge}${ageChart()}</div>`;
  }
  const waitBody = failed?.view === view ? `<div class="error-box">${esc(failed.message)}</div>`
    : '<div class="loading"><div class="spinner"></div>Loading the last 12 weeks of merges…</div>';
  return `<div class="insights">${loadTiles(rows)}${panel("Merge trends", "", waitBody, true)}${ageChart()}</div>`;
}
