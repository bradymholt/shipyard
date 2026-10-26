// Icons, escaping, time formatting, and the error dialog.
import { $ } from "./base.js";

export function githubAge(updatedAt) {
  const seconds = Math.max(0, Math.floor((Date.now() - updatedAt) / 1000));
  if (seconds < 10) return "just now";
  if (seconds < 60) return `${Math.floor(seconds / 10) * 10}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export const ICONS = {
  pr: '<svg width="16" height="16" viewBox="0 0 16 16"><path d="M1.5 3.25a2.25 2.25 0 1 1 3 2.122v5.256a2.251 2.251 0 1 1-1.5 0V5.372A2.25 2.25 0 0 1 1.5 3.25Zm5.677-.177L9.573.677A.25.25 0 0 1 10 .854V2.5h1A2.5 2.5 0 0 1 13.5 5v5.628a2.251 2.251 0 1 1-1.5 0V5a1 1 0 0 0-1-1h-1v1.646a.25.25 0 0 1-.427.177L7.177 3.427a.25.25 0 0 1 0-.354ZM3.75 2.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm0 9.5a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Zm8.25.75a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Z"/></svg>',
  check: '<svg viewBox="0 0 16 16" width="12" height="12"><path d="M13.78 4.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.751.751 0 0 1 .018-1.042.751.751 0 0 1 1.042-.018L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z"/></svg>',
  x: '<svg viewBox="0 0 16 16" width="12" height="12"><path d="M3.72 3.72a.75.75 0 0 1 1.06 0L8 6.94l3.22-3.22a.749.749 0 0 1 1.275.326.749.749 0 0 1-.215.734L9.06 8l3.22 3.22a.749.749 0 0 1-.326 1.275.749.749 0 0 1-.734-.215L8 9.06l-3.22 3.22a.751.751 0 0 1-1.042-.018.751.751 0 0 1-.018-1.042L6.94 8 3.72 4.78a.75.75 0 0 1 0-1.06Z"/></svg>',
  dot: '<svg viewBox="0 0 16 16" width="12" height="12"><path d="M8 4a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z"/></svg>',
  merged: '<svg viewBox="0 0 16 16" width="12" height="12"><path d="M5.45 5.154A4.25 4.25 0 0 0 9.25 7.5h1.378a2.251 2.251 0 1 1 0 1.5H9.25A5.734 5.734 0 0 1 5 7.123v3.505a2.25 2.25 0 1 1-1.5 0V5.372a2.25 2.25 0 1 1 1.95-.218ZM4.25 13.5a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Zm0-8a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Zm8.5 4.5a.75.75 0 1 0 0-1.5.75.75 0 0 0 0 1.5Z"/></svg>',
  clock: '<svg viewBox="0 0 16 16" width="12" height="12"><path d="M8 0a8 8 0 1 1 0 16A8 8 0 0 1 8 0ZM1.5 8a6.5 6.5 0 1 0 13 0 6.5 6.5 0 0 0-13 0Zm7-3.25v2.992l2.028 1.352a.75.75 0 0 1-.832 1.248l-2.36-1.573A.75.75 0 0 1 7 8.25v-3.5a.75.75 0 0 1 1.5 0Z"/></svg>',
  comment: '<svg viewBox="0 0 16 16" width="14" height="14"><path d="M1 2.75C1 1.784 1.784 1 2.75 1h10.5c.966 0 1.75.784 1.75 1.75v7.5A1.75 1.75 0 0 1 13.25 12H9.06l-2.573 2.573A1.458 1.458 0 0 1 4 13.543V12H2.75A1.75 1.75 0 0 1 1 10.25Zm1.75-.25a.25.25 0 0 0-.25.25v7.5c0 .138.112.25.25.25h2a.75.75 0 0 1 .75.75v2.19l2.72-2.72a.749.749 0 0 1 .53-.22h4.5a.25.25 0 0 0 .25-.25v-7.5a.25.25 0 0 0-.25-.25Z"/></svg>',
  changes: '<svg viewBox="0 0 16 16" width="12" height="12"><path d="M4.72 3.22a.75.75 0 0 1 1.06 1.06L2.06 8l3.72 3.72a.75.75 0 1 1-1.06 1.06L.47 8.53a.75.75 0 0 1 0-1.06Zm6.56 0a.75.75 0 1 0-1.06 1.06L13.94 8l-3.72 3.72a.75.75 0 1 0 1.06 1.06l4.25-4.25a.75.75 0 0 0 0-1.06Z"/></svg>',
};

export const GITHUB_ICON = '<svg viewBox="0 0 16 16" width="15" height="15"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.6 7.6 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg>';

export const LAUNCH_ICON = '<svg viewBox="0 0 16 16" width="15" height="15"><path d="M9 1h6v6h-1.5V3.56L7.53 9.53 6.47 8.47 12.44 2.5H9V1ZM2.75 3.5H7V5H2.75a.25.25 0 0 0-.25.25v8c0 .138.112.25.25.25h8a.25.25 0 0 0 .25-.25V9H14.5v4.25A1.75 1.75 0 0 1 12.75 15h-8A1.75 1.75 0 0 1 3 13.25v-8A1.75 1.75 0 0 1 4.75 3.5Z"/></svg>';

// A stylized tree — trunk, two limbs, three canopy nodes — for anything that lands a
// branch in a worktree.
export const AGENT_ICONS = {
  "claude-code": '<svg viewBox="0 0 120 120" width="15" height="15"><path d="M23 80L47 67L47 66L47 65L46 65L42 65L28 64L17 64L5 63L3 63L0 59L0 58L3 56L6 56L14 57L25 58L33 58L45 59L47 59L48 58L47 58L46 58L35 50L22 41L15 36L12 34L10 32L9 27L12 23L17 23L18 24L22 27L32 34L44 43L46 45L47 44L46 43L39 31L32 18L29 13L28 10C28 9 28 8 28 6L31 1L33 1L38 1L40 3L43 10L48 21L56 36L58 41L60 45L60 46L61 46L61 45L61 37L63 27L64 13L64 9L66 5L70 2L73 4L75 7L75 9L73 19L71 33L69 43L70 43L71 42L76 35L84 25L88 21L92 16L95 14L100 14L104 20L102 25L97 32L92 38L86 46L82 53L82 54L83 53L97 50L105 49L114 48L118 49L119 51L117 55L107 58L96 60L79 64L79 65L87 65L90 66L98 66L113 67L117 69L119 72L119 75L113 78L105 76L86 71L79 70L78 70L84 76L94 85L106 96L107 99L105 101L103 101L92 93L88 89L79 81L78 81L78 82L80 85L92 103L93 108L92 110L89 111L85 110L79 101L71 90L66 80L65 81L62 117L60 119L57 120L54 118L52 114L54 107L55 97L57 89L58 80L59 77L51 87L41 101L32 110L30 111L26 109L27 106L29 103L41 88L48 79L52 73L52 72L21 93L15 94L12 91L13 88L14 86L23 80Z"/></svg>',
};

export const DEFAULT_AGENT_ICON = '<svg viewBox="0 0 16 16" width="15" height="15"><path d="M0 2.75C0 1.784.784 1 1.75 1h12.5c.966 0 1.75.784 1.75 1.75v10.5A1.75 1.75 0 0 1 14.25 15H1.75A1.75 1.75 0 0 1 0 13.25Zm1.75-.25a.25.25 0 0 0-.25.25v10.5c0 .138.112.25.25.25h12.5a.25.25 0 0 0 .25-.25V2.75a.25.25 0 0 0-.25-.25ZM7 11a.75.75 0 0 1 .75-.75h3.5a.75.75 0 0 1 0 1.5h-3.5A.75.75 0 0 1 7 11ZM4.22 5.22a.75.75 0 0 1 1.06 0l2.25 2.25a.75.75 0 0 1 0 1.06l-2.25 2.25a.75.75 0 0 1-1.06-1.06L5.94 8 4.22 6.28a.75.75 0 0 1 0-1.06Z"/></svg>';

export const EMPTY_ICON = '<svg viewBox="0 0 16 16" width="22" height="22" aria-hidden="true"><path d="M8 1.5a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM0 8a8 8 0 1 1 16 0A8 8 0 0 1 0 8Zm11.78-2.53a.75.75 0 0 1 0 1.06l-4 4a.75.75 0 0 1-1.06 0l-2-2a.75.75 0 1 1 1.06-1.06L7.25 8.94l3.47-3.47a.75.75 0 0 1 1.06 0Z"/></svg>';

export const BRANCH_ICON = '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M9.5 3.25a2.25 2.25 0 1 1 3 2.122V6A2.5 2.5 0 0 1 10 8.5H6a1 1 0 0 0-1 1v1.128a2.251 2.251 0 1 1-1.5 0V5.372a2.25 2.25 0 1 1 1.5 0v1.836A2.493 2.493 0 0 1 6 7h4a1 1 0 0 0 1-1v-.628A2.25 2.25 0 0 1 9.5 3.25Zm-6 0a.75.75 0 1 0 1.5 0 .75.75 0 0 0-1.5 0Zm8.25-.75a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5ZM4.25 12a.75.75 0 1 0 0 1.5.75.75 0 0 0 0-1.5Z"/></svg>';

export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Render `backtick`-delimited spans in a PR title as inline code, like GitHub.
// esc() runs first, so the code content is already HTML-safe.
export const fmtTitle = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>");

export function timeAgo(iso) {
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  const units = [[31536000, "year"], [2592000, "month"], [604800, "week"], [86400, "day"], [3600, "hour"], [60, "minute"]];
  for (const [s, name] of units) {
    const n = Math.floor(secs / s);
    if (n >= 1) return `${n} ${name}${n > 1 ? "s" : ""} ago`;
  }
  return "just now";
}

// The server's quiet polls don't re-render, so these labels age in place.
function agoText(at, short) {
  const text = timeAgo(at);
  return short ? text.replace(" ago", "") : text;
}

export const agoSpan = (at, short = false) =>
  `<span data-ago="${new Date(at).getTime()}"${short ? " data-short" : ""}>${agoText(at, short)}</span>`;

export function refreshAgoLabels() {
  document.querySelectorAll("[data-ago]").forEach((el) => {
    el.textContent = agoText(Number(el.dataset.ago), el.hasAttribute("data-short"));
  });
}

// GitHub label colors are arbitrary; pick dark or light text by background luminance.
export function labelColors(hex) {
  const r = parseInt(hex.slice(0, 2), 16), g = parseInt(hex.slice(2, 4), 16), b = parseInt(hex.slice(4, 6), 16);
  const luma = 0.299 * r + 0.587 * g + 0.114 * b;
  return `color: ${luma > 160 ? "var(--fg)" : "var(--surface)"}; background: #${hex}`;
}

export function showError(message, title = "Can't open") {
  $("#error-title").textContent = title;
  $("#error-message").textContent = message;
  $("#error-dialog").showModal();
}

export const LANE_CHEVRON = '<svg viewBox="0 0 16 16" width="12" height="12"><path d="M12.78 5.22a.749.749 0 0 1 0 1.06l-4.25 4.25a.749.749 0 0 1-1.06 0L3.22 6.28a.749.749 0 1 1 1.06-1.06L8 8.94l3.72-3.72a.749.749 0 0 1 1.06 0Z"/></svg>';

export const REMOVE_ICON = '<svg viewBox="0 0 16 16" width="12" height="12"><path d="M4.22 4.22a.75.75 0 0 1 1.06 0L8 6.94l2.72-2.72a.75.75 0 1 1 1.06 1.06L9.06 8l2.72 2.72a.75.75 0 1 1-1.06 1.06L8 9.06l-2.72 2.72a.75.75 0 0 1-1.06-1.06L6.94 8 4.22 5.28a.75.75 0 0 1 0-1.06Z"/></svg>';

$("#error-close").addEventListener("click", () => $("#error-dialog").close());

export const clip = (s, n) => (s.length > n ? `${s.slice(0, n - 1)}…` : s);

export const CLOSE_ICON = '<svg viewBox="0 0 16 16" width="16" height="16"><path d="M3.72 3.72a.75.75 0 0 1 1.06 0L8 6.94l3.22-3.22a.749.749 0 0 1 1.275.326.749.749 0 0 1-.215.734L9.06 8l3.22 3.22a.749.749 0 0 1-.326 1.275.749.749 0 0 1-.734-.215L8 9.06l-3.22 3.22a.751.751 0 0 1-1.042-.018.751.751 0 0 1-.018-1.042L6.94 8 3.72 4.78a.75.75 0 0 1 0-1.06Z"/></svg>';
