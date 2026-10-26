// The PR sidebar: next steps, proposals, the pinned session, and activity.
import { $, narrowViewport, state } from "./base.js";
import { AGENT_ICONS, agoSpan, clip, CLOSE_ICON, DEFAULT_AGENT_ICON, esc, fmtTitle, GITHUB_ICON, showError, timeAgo } from "./html.js";
import { companionCall, companionGet, companionPost, worktreeFor } from "./server.js";
import { prKey } from "./data.js";
import { agentName, companionPr, companionSteps, failingChecks, findPr, isReviewPr, markMerged, markRequested, markReviewed, REVIEW_STATES, rollupState, stageOf } from "./prs.js";
import { ciHtml, openCheckoutButton, openInApp, render } from "./board.js";

// One component whose sections appear when the companion is connected, like the
// worktree icons: Header, Next, PR session, Review, Activity.
export let sidebarKey = null;

const drafts = new Map();       // unsent text (edited replies, the Ask field), by field id

const shownDiffs = new Set();   // proposals whose diff was shown; Apply fix waits for that

const detailCache = new Map();  // key -> { updatedAt, loading, data, error }

let sessionChoices = null;      // { key, sessions } while the re-pin picker is open

let lastSidebarHtml = "";

const AUTO_HELD_LABELS = { approve: "Auto-approve", rerun_checks: "Auto re-run", apply_fix: "Auto-fix",
  enable_auto_merge: "Auto-merge" };

const ACTION_LABELS = {
  rerun_checks: "Re-run failed jobs", apply_fix: "Apply fix and push", post_reply: "Post reply",
  approve: "Approve", comment_unresolved: "Comment on unresolved", submit_review: "Submit as review",
  enable_auto_merge: "Enable auto-merge", merge: "Merge", mark_ready: "Mark ready", nudge: "Post nudge",
  rerequest_review: "Re-request review",
};

// Actions that put words in front of someone show the full text, editable, before they post.
const TEXT_ACTIONS = new Set(["post_reply", "comment_unresolved", "submit_review", "nudge", "approve"]);

const PASS_FOR_CHIP = { fix_ci: "ci_failed", review: "pre_review", re_review: "re_review" };

export const PASS_LABELS = { feedback: "Plan for the feedback", ci_failed: "Why CI failed", pre_review: "Pre-review", re_review: "Re-review", ask: "Answer", new_session: "New session" };

const HOW_PINNED = { "pr-url": "opened this PR", branch: "ran gh pr create for this branch",
  latest: "latest in the checkout", manual: "picked by you", shipyard: "started by Shipyard", review: "review worktree" };

export function openSidebar(key) {
  sidebarKey = key;
  sessionChoices = null;
  document.body.classList.add("sidebar-open");
  render();
  if (narrowViewport.matches || matchMedia("(max-width: 900px)").matches) {
    $("#sidebar").scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

export function closeSidebar() {
  sidebarKey = null;
  document.body.classList.remove("sidebar-open");
  render();
}

// The timeline is read on demand, once per PR update.
async function loadDetail(key) {
  const pr = findPr(key);
  if (!pr) return;
  const cached = detailCache.get(key);
  if (cached && (cached.loading || cached.updatedAt === pr.updatedAt)) return;
  detailCache.set(key, { updatedAt: pr.updatedAt, loading: true, data: cached?.data || null });
  let entry;
  try {
    const { node } = await companionGet(`companion/timeline?id=${encodeURIComponent(pr.id)}`);
    entry = { updatedAt: pr.updatedAt, loading: false, data: node };
  } catch (err) {
    entry = { updatedAt: pr.updatedAt, loading: false, data: cached?.data || null, error: err.message };
  }
  detailCache.set(key, entry);
  if (sidebarKey === key) renderSidebar();
}

function checksText(pr) {
  const s = rollupState(pr);
  if (!s) return "";
  const failing = failingChecks(pr);
  const text = { SUCCESS: "Checks passing", PENDING: "Checks running", EXPECTED: "Checks expected" }[s]
    || `${failing.length || "Checks"} failing`;
  return `<span class="ci ${s === "SUCCESS" ? "success" : s === "PENDING" || s === "EXPECTED" ? "pending" : "failure"}">${ciHtml(pr).replace(/<\/?span[^>]*>/g, "")}${esc(text)}</span>`;
}

// Resume a specific session (the pinned one) in the agent's app.
function sessionLink(path, sessionId, inner, cls, key) {
  const agent = state.agent;
  if (!agent || !sessionId || !path) return "";
  const data = `data-continue="${esc(key || "")}"`;
  if (agent.mode === "command") {
    return `<button type="button" class="${cls}" ${data} data-agent-path="${esc(path)}" data-agent-session="${esc(sessionId)}" title="Open the PR session in ${esc(agent.name)}">${inner}</button>`;
  }
  const url = agent.url
    .replaceAll("{session}", encodeURIComponent(sessionId))
    .replaceAll("{path}", String(path).split("/").map(encodeURIComponent).join("/"));
  return `<a class="${cls}" ${data} href="${esc(url)}" title="Open the PR session in ${esc(agent.name)}">${inner}</a>`;
}

// The session every link for a PR opens: the pinned one, or none if nothing is pinned.
export function sessionTarget(pr) {
  const cp = companionPr(pr);
  return cp?.session?.id && cp.path ? { path: cp.path, session: cp.session } : null;
}

function continueTarget(pr) {
  const target = sessionTarget(pr);
  return target && { path: target.path, session: target.session.id };
}

function continueButton(pr) {
  const target = continueTarget(pr);
  return target ? sessionLink(target.path, target.session, `Continue in ${esc(agentName())}`, "btn-link", prKey(pr)) : "";
}

// A new session with the feedback prompt typed in; agents without a prompt URL resume instead.
function respondButton(pr) {
  const url = state.agent?.prompt_url;
  const template = state.rules?.address_feedback?.prompt;
  if (!url || !template) return continueButton(pr);
  const prompt = template.replaceAll("{url}", pr.url).replaceAll("{repo}", pr.repository?.nameWithOwner || "")
    .replaceAll("{number}", pr.number).replaceAll("{branch}", pr.headRefName || "");
  const path = continueTarget(pr)?.path || worktreeFor(pr)?.path || companionPr(pr)?.path || "";
  const href = url.replaceAll("{prompt}", encodeURIComponent(prompt)).replaceAll("{path}", encodeURIComponent(path));
  return `<a class="btn-link" data-continue="${esc(prKey(pr))}" href="${esc(href)}" title="${esc(prompt)}">Address in ${esc(agentName())}</a>`;
}

function sidebarHeader(pr) {
  const key = prKey(pr);
  const cp = companionPr(pr);
  const wt = worktreeFor(pr);
  const comments = pr.comments?.totalCount || 0;
  const size = pr.additions != null
    ? `<span><span class="f-add">+${pr.additions}</span> <span class="f-del">−${pr.deletions}</span> · ${pr.changedFiles} file${pr.changedFiles === 1 ? "" : "s"}</span>` : "";
  let launch = `<a class="wt-link" href="${esc(pr.url)}" target="_blank" rel="noopener" title="Open on GitHub">${GITHUB_ICON}<span>GitHub</span></a>`;
  const target = continueTarget(pr);
  const icon = AGENT_ICONS[state.agent?.sessions] || DEFAULT_AGENT_ICON;
  if (target) launch += sessionLink(target.path, target.session, `${icon}<span>${esc(agentName())}</span>`, "wt-link wt-agent", key);
  const checkout = wt || (cp?.path ? { path: cp.path, workspace: null, main: false } : null);
  if (checkout) launch += openCheckoutButton(checkout, state.launcher.name || "Open");
  return `
    <div class="sb-top">
      <div class="sb-where">${esc(pr.repository?.nameWithOwner || "")} · #${pr.number} · ${esc(pr.headRefName || "")}</div>
      <button type="button" class="sb-close" data-close title="Close (Esc)" aria-label="Close">${CLOSE_ICON}</button>
    </div>
    <div class="sb-title">${fmtTitle(pr.title)}</div>
    <div class="sb-state"><span>${esc(stageOf(pr))}</span>${checksText(pr)}${size}${comments ? `<span>${comments} comment${comments === 1 ? "" : "s"}</span>` : ""}</div>
    <div class="sb-launch">${launch}</div>`;
}

function textField(field, text) {
  const value = drafts.has(field) ? drafts.get(field) : text || "";
  const rows = Math.min(10, Math.max(3, value.split("\n").length + 1));
  return `<textarea rows="${rows}" data-field="${esc(field)}">${esc(value)}</textarea>`;
}

// A review card leads with its verdict: what to do with the PR, then where to do it.
const VERDICTS = { APPROVE: ["Approve", "green"], REQUEST_CHANGES: ["Request changes", "red"], COMMENT: ["Comment", ""] };

const verdictProposal = (card) => (card.pass === "pre_review" || card.pass === "re_review")
  ? (card.proposals || []).findIndex((p) => p.action === "finish_on_github" || p.action === "approve") : -1;

function verdictHtml(p) {
  const [label, tone] = VERDICTS[p.action === "approve" ? "APPROVE" : p.payload?.event] || VERDICTS.COMMENT;
  return `<div class="nc-verdict ${tone}"><div class="nc-verdict-head"><span class="nc-verdict-pre">Recommends</span><span class="nc-verdict-label">${label}</span><span class="nc-verdict-conf">${esc(p.confidence)} confidence</span></div>
    ${p.reason ? `<p class="nc-verdict-why">${esc(p.reason)}</p>` : ""}</div>`;
}

function proposalHtml(pr, card, p, i, bare = false) {
  if (p.action === "continue") return p.reason ? `<p class="nc-reason">${esc(p.reason)}</p>` : "";
  if (p.action === "finish_on_github") {
    const n = p.payload?.comments || 0;
    const drafted = p.payload?.error ? esc(p.payload.error)
      : `Drafted on GitHub as a pending review${n ? ` with ${n} comment${n === 1 ? "" : "s"}` : ""}, visible only to you. Edit it there, then submit.`;
    return `
    <div class="nc-proposal">
      <p class="nc-reason">${drafted}</p>
      <div class="nc-actions"><a class="btn-link primary" href="${esc(`${pr.url}/files`)}" target="_blank" rel="noopener">Finish review on GitHub</a></div>
    </div>`;
  }
  const field = `c${card.id}-${i}`;
  const label = ACTION_LABELS[p.action] || p.action;
  let body = "";
  const ctx = p.action === "post_reply" ? p.context : null;
  if (ctx?.hunk) {
    body += `<div class="nc-snippet-where">${esc(ctx.path || "")}${ctx.line ? `:${esc(ctx.line)}` : ""}${ctx.outdated ? " · outdated" : ""}</div>
      <pre class="nc-snippet">${esc(ctx.hunk)}</pre>`;
  }
  if (TEXT_ACTIONS.has(p.action)) body += textField(field, p.payload?.body);
  if (p.action === "submit_review") {
    const event = drafts.get(`${field}-event`) || p.payload?.event || "COMMENT";
    body += `<div class="nc-actions"><select data-field="${field}-event">${["COMMENT", "APPROVE", "REQUEST_CHANGES"].map((e) =>
      `<option value="${e}"${e === event ? " selected" : ""}>${{ COMMENT: "Comment", APPROVE: "Approve", REQUEST_CHANGES: "Request changes" }[e]}</option>`).join("")}</select></div>`;
    const inline = p.payload?.comments || [];
    if (inline.length) {
      body += `<ul class="nc-inline">${inline.map((c) => `<li><code>${esc(c.path)}:${esc(c.line)}</code> ${esc(c.body)}</li>`).join("")}</ul>`;
    }
  }
  let button = `<button type="button" class="nc-run" data-card="${card.id}" data-index="${i}" data-action="${esc(p.action)}" data-field="${field}">${esc(label)}</button>`;
  if (p.action === "apply_fix") {
    // Never the default: the diff shows first, and the button stays plain.
    button = shownDiffs.has(field)
      ? `<div class="nc-reason">Commit: ${esc(p.payload?.message || "")}</div><pre>${esc(p.payload?.diff || "(no diff)")}</pre>${button}`
      : `<button type="button" data-show-diff="${field}">Show diff</button>`;
  }
  const where = p.action === "post_reply" && p.payload?.comment_id
    ? ` · replies on <a class="nc-thread" href="${esc(`${pr.url}#discussion_r${p.payload.comment_id}`)}" target="_blank" rel="noopener">the review thread</a>` : "";
  return `
    <div class="nc-proposal">
      ${bare ? "" : `<div class="nc-reason"><strong>${esc(label)}</strong> · ${esc(p.confidence)} confidence${where} — ${esc(p.reason || "")}</div>`}
      ${body}
      <div class="nc-actions">${button}</div>
    </div>`;
}

function companionCardHtml(pr, card, first) {
  const cls = `next-card${first ? " first" : ""}`;
  const dismiss = `<button type="button" class="sb-close nc-x" data-dismiss="${card.id}" title="Dismiss" aria-label="Dismiss">${CLOSE_ICON}</button>`;
  const title = esc(PASS_LABELS[card.pass] || card.title || "Next");
  const viewLog = card.pass === "ci_failed" && failingChecks(pr)[0]?.url
    ? `<a class="btn-link" href="${esc(failingChecks(pr)[0].url)}" target="_blank" rel="noopener">View log</a>` : "";
  // Whether a re-run makes sense can hinge on what only you know (say, you freed a quota).
  const rerun = card.pass === "ci_failed" && !(card.proposals || []).some((p) => p.action === "rerun_checks")
    ? `<button type="button" class="nc-run" data-pr-action="rerun_checks" data-done-card="${card.id}">${esc(ACTION_LABELS.rerun_checks)}</button>` : "";
  if (card.kind === "running") {
    return `<div class="${cls}"><div class="nc-head"><span class="plan-dot running"></span>${title}</div>
      <p class="nc-reason">${esc(agentName())} is reading the PR${card.question ? `: “${esc(card.question)}”` : ""}…</p></div>`;
  }
  if (card.kind === "offer") {
    return `<div class="${cls}"><div class="nc-head">${title}${dismiss}</div>
      <div class="nc-actions"><button type="button" data-run-pass="${esc(card.pass)}">Ask ${esc(agentName())}</button>${continueButton(pr)}</div></div>`;
  }
  if (card.kind === "error" || card.kind === "raw") {
    const raw = card.raw ? `<pre>${esc(card.raw)}</pre><div class="nc-actions"><button type="button" data-copy="${card.id}">Copy</button>${continueButton(pr)}</div>` : "";
    const retry = card.kind === "error" && card.pass && card.pass !== "ask"
      ? `<div class="nc-actions"><button type="button" data-run-pass="${esc(card.pass)}">Try again</button>${continueButton(pr)}</div>` : "";
    return `<div class="${cls}"><div class="nc-head">${title}${dismiss}</div>
      <p class="${card.kind === "error" ? "nc-error" : "nc-reason"}">${esc(card.error || "")}</p>${raw}${retry}</div>`;
  }
  if (card.kind === "proposal") {
    return `<div class="${cls}"><div class="nc-head">${esc(card.title)}${dismiss}</div>
      <p class="nc-reason">${esc(card.summary || "")}</p>
      <div class="nc-actions"><button type="button" class="nc-run" data-card="${card.id}" data-index="0" data-action="${esc(card.action)}">${esc(ACTION_LABELS[card.action] || "Do it")}</button>
        <button type="button" data-dismiss="${card.id}">Dismiss</button></div></div>`;
  }
  const chip = card.pass === "feedback"
    ? companionSteps(pr).find((s) => s.kind === "address_feedback" || s.kind === "rerequest_review") : null;
  const rerequest = card.pass === "feedback" ? respondButton(pr) + (chip?.approval ? "" : rerequestButton(chip?.reviewers, card.id)) : "";
  const openKept = card.kept
    ? sessionLink(card.kept.path, card.kept.session, card.kept.fork ? "Open the fork" : "Open the session", "btn-link", prKey(pr)) : "";
  const question = card.kind === "answer" && card.question ? `<p class="nc-reason">“${esc(card.question)}”</p>` : "";
  const vi = verdictProposal(card);
  const verdict = vi >= 0 ? verdictHtml(card.proposals[vi]) : "";
  const held = card.auto_held ? `<p class="nc-held">${AUTO_HELD_LABELS[card.auto_action] || AUTO_HELD_LABELS.approve} is on, but this one waits for you: ${esc(card.auto_held)}.</p>`
    : card.auto_wait?.until ? `<p class="nc-held">Auto-approves at ${esc(new Date(card.auto_wait.until).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }))}, a few minutes after the push.</p>`
    : card.auto_wait ? `<p class="nc-held">Re-runs the failed checks automatically once the running ones finish.</p>` : "";
  const proposals = (card.proposals || []).map((p, i) => proposalHtml(pr, card, p, i, i === vi)).join("");
  return `<div class="${cls}"><div class="nc-head">${title}${dismiss}</div>${question}${verdict}${held}
    ${verdict ? '<div class="nc-label">Summary</div>' : ""}<p>${esc(card.summary || "")}</p>${proposals}
    ${card.pass === "ask" ? "" : `<div class="nc-actions">${openKept}${continueButton(pr)}${rerun}${rerequest}${viewLog}</div>`}</div>`;
}

function rerequestButton(reviewers, cardId) {
  if (!reviewers?.length) return "";
  const done = cardId ? ` data-done-card="${cardId}"` : "";
  return `<button type="button" class="nc-run" data-pr-action="rerequest_review" data-reviewers="${esc(reviewers.join(","))}"${done}>Re-request review</button>`;
}

// With no card from the companion, the chip still implies the buttons that fit it.
function derivedCardHtml(pr, chip) {
  if (!chip) return "";
  const key = prKey(pr);
  const field = `d-${key}-${chip.kind}`;
  const run = (action, label, cls = "") => `<button type="button" class="nc-run ${cls}" data-pr-action="${esc(action)}" data-field="${esc(field)}">${esc(label)}</button>`;
  const rerequest = rerequestButton(chip.reviewers);
  let body = "";
  let actionsHtml = "";
  if (chip.kind === "merge") {
    body = "Approved and checks are green.";
    actionsHtml = run("merge", "Merge") + (pr.viewerCanEnableAutoMerge ? run("enable_auto_merge", "Enable auto-merge") : "");
  } else if (chip.kind === "nudge") {
    const owner = (pr.repository?.nameWithOwner || "").split("/")[0];
    const mentions = (chip.reviewers || []).map((r) => r.startsWith("team:") ? `@${owner}/${r.slice(5)}` : `@${r}`).join(" ");
    body = `Waiting on review for ${chip.label.split("· ")[1] || "a while"}. The nudge posts as a PR comment under your name:`;
    actionsHtml = textField(field, `${mentions} friendly nudge: this is ready for review when you have a minute. Thanks!`) + run("nudge", "Post nudge");
  } else if (chip.kind === "rerequest_review") {
    body = `${chip.title}. Ask ${chip.reviewers.join(", ")} to look again:`;
    actionsHtml = rerequest + continueButton(pr);
  } else if (chip.kind === "address_feedback") {
    const open = companionPr(pr)?.openThreads || 0;
    const verb = chip.approval ? "approved with comments"
      : pr.reviewDecision === "CHANGES_REQUESTED" ? "requested changes" : "left review comments";
    body = `${chip.reviewers.join(", ")} ${verb}${open ? ` · ${open} open thread${open === 1 ? "" : "s"}` : ""}.`;
    actionsHtml = respondButton(pr) + (chip.approval ? "" : rerequest);
  } else if (PASS_FOR_CHIP[chip.kind]) {
    body = { fix_ci: `Failing: ${failingChecks(pr).map((c) => c.name).join(", ") || "checks"}.`,
      review: "Your review is requested.", re_review: "The author asked you to look again." }[chip.kind];
    actionsHtml = `<button type="button" data-run-pass="${PASS_FOR_CHIP[chip.kind]}">Ask ${esc(agentName())}</button>` + continueButton(pr)
      + (chip.kind === "fix_ci" && failingChecks(pr)[0]?.url ? `<a class="btn-link" href="${esc(failingChecks(pr)[0].url)}" target="_blank" rel="noopener">View log</a>` : "");
  } else {
    return "";
  }
  return `<div class="next-card first"><div class="nc-head">${esc(chip.label)}</div><p class="nc-reason">${esc(body)}</p>
    <div class="nc-actions">${actionsHtml}</div></div>`;
}

function nextSection(pr) {
  const key = prKey(pr);
  const cp = companionPr(pr);
  const open = !pr.mergedAt;
  // Your own asks and new sessions show under PR session.
  const cards = (cp?.cards || []).filter((c) => c.pass !== "new_session" && c.pass !== "ask");
  let html = cards.map((c, i) => companionCardHtml(pr, c, i === 0)).join("");
  if (!html && cp) html = derivedCardHtml(pr, companionSteps(pr)[0] || (cp.chip?.kind !== "snoozed" ? cp.chip : null));
  if (!html) {
    html = !cp && open
      ? `<div class="sb-empty">Shipyard follows PRs you opened, were asked to review, or reviewed. This one isn't among them${state.companionState ? "" : " yet"}.</div>`
      : `<div class="sb-empty">Nothing to do right now.</div>`;
  }
  html += (cp?.handled || []).map((c) => {
    const h = c.handled || {};
    return `<div class="sb-handled"><strong>Handled:</strong> ${esc(h.result || "")}${h.reason ? ` — ${esc(h.reason)}` : ""}
      ${h.undoable ? `<button type="button" data-undo="${c.id}">Undo</button>` : ""}
      <button type="button" data-dismiss="${c.id}">OK</button></div>`;
  }).join("");
  let snooze = "";
  if (cp && open) {
    const until = cp.snoozedUntil && new Date(cp.snoozedUntil) > new Date() ? cp.snoozedUntil : null;
    snooze = `<span class="sb-h-aside snooze-menu"><select data-snooze="${esc(key)}" title="Snooze dims the card until then">
      <option value="">${until ? `Snoozed until ${new Date(until).toLocaleString([], { weekday: "short", hour: "numeric" })}` : "Snooze…"}</option>
      ${until ? '<option value="0">Unsnooze</option>' : ""}
      <option value="4">4 hours</option><option value="24">1 day</option><option value="72">3 days</option><option value="168">1 week</option>
    </select></span>`;
  }
  return `<div class="sb-section"><div class="sb-h">Next${snooze}</div>${html}</div>`;
}

const AUTO_BY = { review: "Set by the pre-review", you: "Set by you" };

function autoApproveSection(pr) {
  const cp = companionPr(pr);
  const setting = cp?.autoApprove;
  if (!setting || pr.mergedAt || !isReviewPr(pr)) return "";
  const rule = state.companionState?.config?.approveRereview;
  const why = !rule ? "Off for every PR: auto.approve_rereview is false in shipyard.config.json."
    : AUTO_BY[setting.by] ? `${AUTO_BY[setting.by]}${setting.why ? `: ${setting.why}` : "."}`
    : setting.on ? "On because your last review approved it." : "Off because your last review didn't approve it.";
  return `<div class="sb-section"><div class="sb-h">Auto-approve</div><div class="sb-auto">
    <label><input type="checkbox" data-auto-approve="${esc(prKey(pr))}"${setting.on && rule ? " checked" : ""}${rule ? "" : " disabled"}>
      Approve on its own when a re-review finds my review addressed</label>
    <div class="auto-why">${esc(why)}</div></div></div>`;
}

// A question runs in a fork of the pinned session, so it lives in that session's section.
function askHtml(pr, cp) {
  if (pr.mergedAt) return "";
  const key = prKey(pr);
  const q = drafts.get(`ask-${key}`) || "";
  return `<form class="sb-ask" data-ask="${esc(key)}"><input type="text" data-field="ask-${esc(key)}" value="${esc(q)}" placeholder="Ask ${esc(agentName())} about this PR" autocomplete="off"><button type="submit">Ask</button></form>`
    + (cp.cards || []).filter((c) => c.pass === "ask").map((c) => companionCardHtml(pr, c, false)).join("");
}

function sessionSection(pr) {
  if (!state.agent) return "";
  const key = prKey(pr);
  const cp = companionPr(pr);
  const newSession = (cls) => `<button type="button" class="${cls}" data-run-pass="new_session" data-node="${esc(pr.id)}" title="Start a ${esc(agentName())} session about this PR and pin it">New session</button>`;
  if (!cp) {
    return `<div class="sb-section"><div class="sb-h">PR session</div><div class="sb-session">
      <div class="sb-empty">No ${esc(agentName())} session for this PR.</div><div class="nc-actions">${newSession("")}</div></div></div>`;
  }
  const s = cp.session;
  const starting = (cp.cards || []).find((c) => c.pass === "new_session");
  if (starting?.kind === "running") {
    return `<div class="sb-section"><div class="sb-h">PR session</div><div class="sb-session">
      <div class="sb-empty">Starting a new ${esc(agentName())} session…</div></div>${askHtml(pr, cp)}</div>`;
  }
  const failed = ["error", "raw"].includes(starting?.kind) ? `<p class="nc-error">${esc(starting.error || "Couldn't start the session.")}</p>` : "";
  let picker = "";
  if (sessionChoices?.key === key) {
    picker = sessionChoices.sessions.length
      ? `<div class="nc-actions"><select data-field="pin-${esc(key)}">${sessionChoices.sessions.map((x) =>
          `<option value="${esc(x.id)}"${s?.id === x.id ? " selected" : ""}>${esc(clip(x.title || x.id, 60))} · ${esc(timeAgo(x.updated * 1000))}</option>`).join("")}</select>
          <button type="button" data-pin="${esc(key)}">Pin</button><button type="button" class="nc-quiet" data-repin-cancel>Cancel</button></div>`
      : `<div class="sb-empty">No ${esc(agentName())} sessions in this checkout.</div>`;
  }
  const repin = cp.path ? `<button type="button" class="nc-quiet" data-repin="${esc(key)}">Re-pin</button>` : "";
  let body;
  if (!s) {
    body = `<div class="sb-empty">${cp.path ? `No ${esc(agentName())} session pinned.` : "No local checkout for this PR."}</div>
      <div class="nc-actions">${newSession("")}${repin}</div>`;
  } else {
    const yours = Math.max(0, s.turns - (s.shipyardTurns || 0));
    const ex = cp.exchange;
    body = `
      <div class="sess-title">${esc(s.title || s.id)}</div>
      <div class="sess-meta"><span class="dot ${s.live ? "ok" : "off"}"></span>${s.live ? "active now" : `idle · ${agoSpan(s.updated * 1000)}`}
        · ${yours} yours · ${s.shipyardTurns || 0} Shipyard's${s.how ? ` · ${esc(HOW_PINNED[s.how] || s.how)}` : ""}</div>
      ${ex ? `<div class="sess-exchange"><strong>${esc(PASS_LABELS[ex.pass] || ex.pass)}</strong> · ${agoSpan(ex.at)}${ex.mode === "fork" ? " · in a fork" : ""}<br>${esc(clip(ex.summary || "", 280))}</div>` : ""}
      <div class="nc-actions">${repin}
        <button type="button" class="nc-quiet" data-unpin="${esc(key)}" title="Stop using this session for the PR">Unpin</button>${newSession("nc-quiet")}</div>`;
  }
  return `<div class="sb-section"><div class="sb-h">PR session</div><div class="sb-session">${failed}${body}${picker}</div>${askHtml(pr, cp)}</div>`;
}

function timelineEntries(d) {
  const out = [];
  for (const n of d?.timelineItems?.nodes || []) {
    const who = (x) => x?.login ? `@${x.login}` : "someone";
    switch (n.__typename) {
      case "PullRequestCommit": out.push({ at: n.commit.committedDate, text: `commit: ${n.commit.messageHeadline}` }); break;
      case "PullRequestReview": out.push({ at: n.submittedAt, text: `${who(n.author)} ${{ APPROVED: "approved", CHANGES_REQUESTED: "requested changes", COMMENTED: "reviewed", DISMISSED: "review dismissed" }[n.state] || "reviewed"}` }); break;
      case "ReviewRequestedEvent": out.push({ at: n.createdAt, text: `review requested from ${n.requestedReviewer?.login ? `@${n.requestedReviewer.login}` : n.requestedReviewer?.name || "a reviewer"}` }); break;
      case "IssueComment": out.push({ at: n.createdAt, text: `${who(n.author)} commented` }); break;
      case "ReadyForReviewEvent": out.push({ at: n.createdAt, text: "marked ready for review" }); break;
      case "ConvertToDraftEvent": out.push({ at: n.createdAt, text: "converted to draft" }); break;
      case "MergedEvent": out.push({ at: n.createdAt, text: `merged by ${who(n.actor)}` }); break;
      case "HeadRefForcePushedEvent": out.push({ at: n.createdAt, text: "force-pushed" }); break;
      case "ReviewDismissedEvent": out.push({ at: n.createdAt, text: "a review was dismissed" }); break;
    }
  }
  return out;
}

function activitySection(pr) {
  const key = prKey(pr);
  const entries = [
    ...timelineEntries(detailCache.get(key)?.data),
    ...(companionPr(pr)?.activity || []).map((a) => ({ at: a.at, text: a.text, shipyard: true })),
  ].filter((e) => e.at).sort((a, b) => new Date(b.at) - new Date(a.at)).slice(0, 25);
  if (!entries.length) return "";
  return `<div class="sb-section"><div class="sb-h">Activity</div><ul class="sb-activity">${entries.map((e) =>
    `<li><span class="act-when">${agoSpan(e.at, true)}</span><span>${e.shipyard ? '<span class="act-shipyard">Shipyard</span> ' : ""}${esc(e.text)}</span></li>`).join("")}</ul></div>`;
}

export function renderSidebar() {
  const el = $("#sidebar");
  const pr = sidebarKey && findPr(sidebarKey);
  if (!pr) {
    if (sidebarKey) { sidebarKey = null; document.body.classList.remove("sidebar-open"); }
    if (lastSidebarHtml) { el.innerHTML = ""; lastSidebarHtml = ""; }
    return;
  }
  loadDetail(sidebarKey);
  const html = sidebarHeader(pr) + nextSection(pr) + autoApproveSection(pr) + sessionSection(pr) + activitySection(pr);
  if (html === lastSidebarHtml) return;
  // Re-rendering on every pushed update must not eat what you're typing.
  const active = document.activeElement;
  const field = el.contains(active) ? active.dataset.field : null;
  const caret = field && "selectionStart" in active ? [active.selectionStart, active.selectionEnd] : null;
  const scroll = el.scrollTop;
  el.innerHTML = html;
  lastSidebarHtml = html;
  el.scrollTop = scroll;
  if (field) {
    const again = el.querySelector(`[data-field="${CSS.escape(field)}"]`);
    if (again) {
      again.focus();
      if (caret && again.setSelectionRange) again.setSelectionRange(...caret);
    }
  }
}

// Run a companion request from a button, showing progress on it and any error in a dialog.
async function busy(btn, fn, title = "Couldn't do that") {
  const original = btn ? btn.innerHTML : "";
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    await fn();
  } catch (err) {
    showError(err.message, title);
  } finally {
    if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = original; }
  }
}

const fieldValue = (field) => {
  const el = $("#sidebar").querySelector(`[data-field="${CSS.escape(field)}"]`);
  return el ? el.value : drafts.get(field);
};

$("#sidebar").addEventListener("input", (e) => {
  const field = e.target.dataset?.field;
  if (field) drafts.set(field, e.target.value);
});

$("#sidebar").addEventListener("change", async (e) => {
  const t = e.target;
  if (t.dataset.field) drafts.set(t.dataset.field, t.value);
  if (t.dataset.snooze !== undefined && t.value !== "") {
    const hours = Number(t.value) || null;
    await busy(null, () => companionCall(`companion/snooze?key=${encodeURIComponent(t.dataset.snooze)}`, { hours }));
  }
  if (t.dataset.autoApprove !== undefined) {
    await busy(null, () => companionCall(`companion/auto-approve?key=${encodeURIComponent(t.dataset.autoApprove)}`, { on: t.checked }));
  }
});

$("#sidebar").addEventListener("submit", async (e) => {
  const form = e.target.closest("form[data-ask]");
  if (!form) return;
  e.preventDefault();
  const key = form.dataset.ask;
  const question = fieldValue(`ask-${key}`) || "";
  await busy(form.querySelector("button"), async () => {
    await companionCall(`companion/ask?key=${encodeURIComponent(key)}`, { question });
    drafts.delete(`ask-${key}`);
  }, "Couldn't ask");
});

$("#sidebar").addEventListener("click", async (e) => {
  const t = (sel) => e.target.closest(sel);
  const key = sidebarKey;
  const pr = key && findPr(key);
  let btn;
  if (t("[data-close]")) return closeSidebar();
  if ((btn = t("[data-continue]"))) {
    // Log it; the link itself opens the session (or the companion runs the command).
    companionCall(`companion/pr-action?key=${encodeURIComponent(key)}&action=continue`, {}).catch(() => {});
    if (btn.dataset.agentSession) {
      e.preventDefault();
      await busy(btn, async () => {
        const res = await companionPost(`open-agent?path=${encodeURIComponent(btn.dataset.agentPath)}&session=${encodeURIComponent(btn.dataset.agentSession)}`);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ok) throw new Error(data.error || `Open in ${agentName()} failed (${res.status}).`);
      });
    }
    return;
  }
  if ((btn = t("button.wt-open"))) return busy(btn, () => openInApp(btn.dataset.target));
  if ((btn = t("[data-show-diff]"))) { shownDiffs.add(btn.dataset.showDiff); return renderSidebar(); }
  if ((btn = t("[data-copy]"))) {
    const card = (companionPr(pr)?.cards || []).find((c) => String(c.id) === btn.dataset.copy);
    try { await navigator.clipboard.writeText(card?.raw || ""); btn.textContent = "Copied"; } catch { showError("Copy failed; select the text instead."); }
    return;
  }
  if ((btn = t("[data-run-pass]"))) {
    const node = btn.dataset.node ? `&node=${encodeURIComponent(btn.dataset.node)}` : "";
    return busy(btn, () => companionCall(`companion/run-pass?key=${encodeURIComponent(key)}&pass=${encodeURIComponent(btn.dataset.runPass)}${node}`), `Couldn't ask ${agentName()}`);
  }
  if ((btn = t("[data-dismiss]"))) return busy(btn, () => companionCall(`companion/dismiss?card=${btn.dataset.dismiss}`));
  if ((btn = t("[data-undo]"))) return busy(btn, () => companionCall(`companion/undo?card=${btn.dataset.undo}`), "Couldn't undo");
  if ((btn = t("[data-repin]"))) {
    return busy(btn, async () => {
      const res = await fetch(`companion/sessions?key=${encodeURIComponent(btn.dataset.repin)}`, { cache: "no-store" });
      const data = await res.json();
      if (!res.ok || !data.ok) throw new Error(data.error || "Couldn't list sessions.");
      sessionChoices = { key: btn.dataset.repin, sessions: data.sessions };
      lastSidebarHtml = "";
      renderSidebar();
    });
  }
  if (t("[data-repin-cancel]")) { sessionChoices = null; return renderSidebar(); }
  if ((btn = t("[data-unpin]"))) return busy(btn, () => companionCall(`companion/unpin?key=${encodeURIComponent(btn.dataset.unpin)}`));
  if ((btn = t("[data-pin]"))) {
    const session = fieldValue(`pin-${btn.dataset.pin}`);
    return busy(btn, async () => {
      await companionCall(`companion/pin?key=${encodeURIComponent(btn.dataset.pin)}&session=${encodeURIComponent(session || "")}`);
      sessionChoices = null;
    });
  }
  if ((btn = t("button.nc-run"))) {
    const action = btn.dataset.action || btn.dataset.prAction;
    const field = btn.dataset.field;
    const payload = {};
    if (field && TEXT_ACTIONS.has(action)) payload.body = fieldValue(field) || "";
    if (action === "submit_review") payload.event = fieldValue(`${field}-event`) || "COMMENT";
    if (btn.dataset.reviewers) payload.reviewers = btn.dataset.reviewers.split(",");
    return busy(btn, async () => {
      if (btn.dataset.prAction) {
        const done = btn.dataset.doneCard ? `&card=${encodeURIComponent(btn.dataset.doneCard)}` : "";
        await companionCall(`companion/pr-action?key=${encodeURIComponent(key)}&action=${encodeURIComponent(action)}${done}`, { payload });
      } else {
        await companionCall(`companion/card-action?card=${btn.dataset.card}&index=${btn.dataset.index}`, { payload });
      }
      if (field) drafts.delete(field);
      // The companion's update and GitHub's catch up in a second or two; show the result now.
      const cp = pr && companionPr(pr);
      const cardId = btn.dataset.card || btn.dataset.doneCard;
      const card = (cp?.cards || []).find((c) => String(c.id) === cardId);
      if (action === "rerequest_review") markRequested(pr, payload.reviewers || card?.payload?.reviewers);
      const reviewState = REVIEW_STATES[action === "submit_review" ? payload.event : action];
      if (reviewState && pr) markReviewed(pr, reviewState);
      if (action === "merge" && pr) markMerged(pr);
      if (action === "mark_ready" && pr) pr.isDraft = false;
      if (action === "enable_auto_merge" && pr) pr.autoMergeRequest = { enabledAt: new Date().toISOString() };
      if (card) cp.cards = cp.cards.filter((c) => c !== card);
      if (action === "merge") closeSidebar();
      else render();
    });
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && sidebarKey && !document.querySelector("dialog[open]")) closeSidebar();
});
