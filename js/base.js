// The page's shared state and the DOM handles every module uses. Imports nothing, so it loads first.

export const LS = {
  FILTER: "ghpr.filter",
};

export const $ = (sel) => document.querySelector(sel);

export const listBody = $("#list-body");

export const DEFAULT_LAUNCHER = {
  name: "VS Code",
  mode: "url",
  target: "workspace",
  url: "vscode://file/{path}",
  command: ["code", "{path}"],
};

// The server writes its session token and config into the page it serves.
const BOOT = JSON.parse(document.getElementById("shipyard-boot")?.textContent || "{}");

export const state = {
  prs: [],              // targeted set: my PRs plus open PRs awaiting my review
  merged: [],           // my recently merged PRs, shown as context on the My PRs tab
  reviewedPrs: [],      // open PRs I reviewed
  reviewedMerged: [],   // recently merged PRs I reviewed, shown on the review board
  recentMerged: { mine: [], review: [] },   // your latest merges everywhere, pushed by the server
  mergedHistory: { mine: [], review: [] },  // the view's older merges, fetched by Show more
  mergedCursor: undefined,   // how far back Show more has walked; null once it reaches the end
  reviewedMergedCursor: undefined,
  targetedLoaded: false,     // true once the server has polled GitHub
  viewer: null,
  ownerTypes: {},            // login -> "org" | "user", from the server's merged searches
  filter: localStorage.getItem(LS.FILTER) === "review" ? "review" : "mine",
  sort: localStorage.getItem("ghpr.sort") || "priority",
  worktrees: {},        // "repo#branch" -> { path, workspace }
  companionToken: BOOT.token || "",  // required for mutations
  views: BOOT.views || { views: [], current: "" },  // saved on the server
  launcher: BOOT.launcher || DEFAULT_LAUNCHER,
  agent: BOOT.agent || null,  // coding agent whose latest session a checkout can resume
  rules: BOOT.rules || null,  // the companion's "rules" config (which chips are on)
  companionState: null, // its per-PR chips, cards, sessions, and activity, pushed over SSE
  refreshing: false,       // asked the server to poll and waiting for it
  mergedExtra: 0,           // merged cards revealed past the first few
  mergedLoading: false,
};

// On a phone the lanes stack, so each can be collapsed to keep the next one in reach.
export const narrowViewport = matchMedia("(max-width: 700px)");
