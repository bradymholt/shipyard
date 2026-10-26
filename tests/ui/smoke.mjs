// Browser smoke test for the board chips and the sidebar.
//
//   node tests/ui/smoke.mjs [screenshot-dir]
//
// Starts tests/ui/demo_server.py (the real companion server over fake GitHub and agent
// data), answers the page's own GitHub calls with matching fixtures, and checks what renders. Needs Playwright with Chromium.
import { execFileSync, spawn } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
// The repo has no package.json, so fall back to a global `npm i -g playwright`, which
// require() doesn't search on its own.
function loadPlaywright() {
  try {
    return require("playwright");
  } catch {
    const globalRoot = execFileSync("npm", ["root", "-g"], { encoding: "utf8" }).trim();
    return require(path.join(globalRoot, "playwright"));
  }
}
const { chromium } = loadPlaywright();
const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
const shots = process.argv[2] || null;
const COMPANION_PORT = 4399;

const ago = (h) => new Date(Date.now() - h * 3600e3).toISOString();
const user = (login) => ({ __typename: "User", login, avatarUrl: `https://avatars.example/${login}?v=4` });

function pr(number, extra = {}) {
  const author = extra.author || "me";
  return {
    id: `PR_o/r_${number}`, number, title: extra.title || `PR ${number}`, url: `https://github.com/o/r/pull/${number}`,
    isDraft: !!extra.draft, createdAt: ago(100), updatedAt: extra.updated || "2026-10-20T10:00:00Z", mergedAt: extra.mergedAt || null,
    headRefName: extra.branch || `me/pr-${number}`, baseRefName: "main", headRefOid: `h${number}`,
    additions: 40, deletions: 12, changedFiles: 3, autoMergeRequest: null, labels: { nodes: [] },
    viewerCanEnableAutoMerge: true, viewerCanDisableAutoMerge: false, ...(extra.autoMerge && { autoMergeRequest: { enabledAt: ago(5) } }),
    repository: { nameWithOwner: "o/r" }, author: { login: author, avatarUrl: `https://avatars.example/${author}?v=4` },
    reviewDecision: extra.decision || null, comments: { totalCount: extra.comments || 0 },
    reviewRequests: { nodes: (extra.requested || []).map((l) => ({ requestedReviewer: user(l) })) },
    latestReviews: { nodes: (extra.reviews || []).map(([state, login, body]) => ({ state, body, author: { login, avatarUrl: null } })) },
    reviewThreads: { nodes: (extra.threads || []).map((t) => [].concat(t)).map((c) => ({ isResolved: false,
      opener: { nodes: [{ author: { login: c[0] } }] }, comments: { nodes: [{ author: { login: c.at(-1) } }] } })) },
    commits: { nodes: [{ commit: { oid: `h${number}`, statusCheckRollup: extra.ci === null ? null : {
      state: extra.ci || "SUCCESS",
      contexts: { nodes: (extra.failing || []).map((name) => ({ __typename: "CheckRun", name, conclusion: "FAILURE",
        detailsUrl: `https://github.com/o/r/actions/runs/8/job/7` })) } } } }] },
    pendingReviews: { nodes: [] },
    recentComments: { nodes: (extra.commentsBy || []).map(([login, h]) => ({ author: { login }, createdAt: ago(h) })) },
    viewerLatestReview: extra.myReview || null,
    latestOpinionatedReviews: { nodes: (extra.standing || []).map(([state, login]) => ({ state, author: { login } })) },
  };
}

const MINE = [
  pr(101, { title: "Add retry to the webhook sender", branch: "me/webhook-retry", decision: "CHANGES_REQUESTED",
    reviews: [["CHANGES_REQUESTED", "rev"]], comments: 2 }),
  pr(102, { title: "Speed up the CI cache", branch: "me/ci-cache", ci: "FAILURE", failing: ["rspec"], requested: ["jm"] }),
  pr(103, { title: "Bump the parser", decision: "APPROVED", reviews: [["APPROVED", "jm"]] }),
  pr(104, { title: "New onboarding flow", draft: true, updated: ago(120) }),
  pr(105, { title: "Refactor auth middleware", requested: ["jm"], updated: ago(30) }),
  pr(106, { title: "Tidy the README", decision: "CHANGES_REQUESTED", reviews: [["CHANGES_REQUESTED", "rev"]], threads: ["me"] }),
  pr(107, { title: "Fix the typo", decision: "CHANGES_REQUESTED", reviews: [["COMMENTED", "rev"]], threads: ["me", "rev"] }),
  pr(108, { title: "Rename the flag", decision: "CHANGES_REQUESTED", requested: ["rev"] }),
  pr(109, { title: "Cache the lookups", decision: "APPROVED", autoMerge: true,
    reviews: [["APPROVED", "jm", "Approving, though I'd prefer a Map here."]] }),
];
const REQUESTED = [
  pr(201, { author: "alice", title: "Fuzzy search in the index", requested: ["me"] }),
  pr(202, { author: "bob", title: "Export to CSV", requested: ["me"] }),
];
const REVIEWED = [pr(202, { author: "bob", title: "Export to CSV", requested: ["me"],
  myReview: { state: "CHANGES_REQUESTED", commit: { oid: "old" }, onBehalfOf: { nodes: [] } } }),
  pr(203, { author: "cy", title: "Follow-up after changes", standing: [["CHANGES_REQUESTED", "me"]], threads: [["me", "cy"], ["me"]],
    myReview: { state: "COMMENTED", commit: { oid: "h203" }, onBehalfOf: { nodes: [] } } }),
  pr(204, { author: "di", title: "Drive-by comment",
    myReview: { state: "COMMENTED", commit: { oid: "h204" }, onBehalfOf: { nodes: [] } } })];
const MERGED = [pr(99, { title: "Old feature", branch: "me/old-feature", mergedAt: ago(20) }),
  pr(98, { title: "Auto-merged on approval", decision: "APPROVED", mergedAt: ago(10),
    reviews: [["APPROVED", "jm", "Approving, though I'd prefer a Map here."]] })];
const REVIEWED_MERGED = [pr(97, { author: "cy", title: "Merged with a reply", mergedAt: ago(5), threads: [["me", "cy"]],
  myReview: { state: "APPROVED", commit: { oid: "h97" }, onBehalfOf: { nodes: [] } } }),
  pr(96, { author: "cy", title: "Merged after a comment", mergedAt: ago(1), commentsBy: [["cy", 1]],
    myReview: { state: "APPROVED", submittedAt: ago(2), commit: { oid: "h96" }, onBehalfOf: { nodes: [] } } }),
  pr(95, { author: "cy", title: "Merged, comment answered", mergedAt: ago(1), commentsBy: [["cy", 3], ["me", 2]],
    myReview: { state: "APPROVED", submittedAt: ago(4), commit: { oid: "h95" }, onBehalfOf: { nodes: [] } } })];

function answer(body) {
  const q = body.variables?.q || "";
  if (body.query.includes("nodes(ids:")) {
    const all = [...MINE, ...REQUESTED, ...REVIEWED, ...MERGED, ...REVIEWED_MERGED];
    return { nodes: body.variables.ids.map((id) => {
      const found = all.find((p) => p.id === id);
      return found && { ...found, state: found.mergedAt ? "MERGED" : "OPEN" };
    }) };
  }
  if (body.query.includes("node(id:")) {
    return { node: {
      files: { totalCount: 3, nodes: [{ path: "app/webhooks/sender.py", additions: 30, deletions: 8 },
        { path: "tests/test_sender.py", additions: 10, deletions: 4 }, { path: "README.md", additions: 0, deletions: 0 }] },
      reviews: { nodes: [{ state: "CHANGES_REQUESTED", submittedAt: ago(2), author: { login: "rev", avatarUrl: null }, comments: { totalCount: 2 } }] },
      timelineItems: { nodes: [
        { __typename: "PullRequestCommit", commit: { messageHeadline: "Retry failed deliveries", committedDate: ago(5) } },
        { __typename: "PullRequestReview", state: "CHANGES_REQUESTED", submittedAt: ago(2), author: { login: "rev" } }] },
    } };
  }
  let nodes = [];
  if (q.includes("is:merged")) nodes = q.includes("reviewed-by:@me") ? REVIEWED_MERGED : MERGED;
  else if (q.includes("review-requested:@me")) nodes = REQUESTED;
  else if (q.includes("reviewed-by:@me")) nodes = REVIEWED;
  else if (q.includes("author:@me")) nodes = MINE;
  return { viewer: { login: "me" }, search: { issueCount: nodes.length, pageInfo: { hasNextPage: false, endCursor: null }, nodes } };
}

function start(cmd, args, ready) {
  const child = spawn(cmd, args, { cwd: repo, stdio: ["ignore", "pipe", "pipe"] });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${args.join(" ")} did not start`)), 15000);
    const onData = (d) => { if (ready.test(String(d))) { clearTimeout(timer); resolve(child); } };
    child.stdout.on("data", onData);
    child.stderr.on("data", onData);
    child.on("exit", (code) => reject(new Error(`${args.join(" ")} exited ${code}`)));
  });
}

const failures = [];
function check(cond, message) {
  if (cond) console.log(`  ok   ${message}`);
  else { console.log(`  FAIL ${message}`); failures.push(message); }
}

async function openPage(browser, port, viewport = { width: 1440, height: 900 }) {
  const context = await browser.newContext({ viewport });
  await context.addInitScript(() => {
    localStorage.setItem("ghpr.token", "test-token");
    localStorage.setItem("ghpr.repos", JSON.stringify(["o/r"]));
    localStorage.setItem("ghpr.currentRepo", "o/r");
  });
  const queries = [];
  await context.route("https://api.github.com/graphql", (route) => {
    const body = JSON.parse(route.request().postData());
    queries.push(body);
    return route.fulfill({ json: { data: answer(body) } });
  });
  await context.route("https://avatars.example/**", (route) => route.fulfill({ status: 404, body: "" }));
  await context.route("https://fonts.googleapis.com/**", (route) => route.fulfill({ body: "" }));
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });
  await page.goto(`http://localhost:${port}/`);
  await page.waitForSelector(".card[data-key]");
  return { page, errors, context, queries };
}

const chipText = (page, n) => page.locator(`.card[data-key="o/r#${n}"] .next-row .next-label`).first().textContent().catch(() => null);

const children = [];
try {
  children.push(await start("python3", ["tests/ui/demo_server.py", String(COMPANION_PORT)], /demo companion on/));
  const browser = await chromium.launch();

  console.log("companion mode");
  const { page, errors, queries } = await openPage(browser, COMPANION_PORT);
  await page.waitForSelector('.card[data-key="o/r#105"] .next-row');
  check((await chipText(page, 101))?.includes("Address feedback · 1"), "changes requested → Address feedback · 1");
  check((await chipText(page, 102))?.includes("Fix CI · rspec"), "failing check → Fix CI · rspec");
  check((await chipText(page, 106))?.includes("Re-request review · 1"), "replied last on every thread → Re-request review · 1");
  check((await chipText(page, 107))?.includes("Address feedback · 1"), "a comment review on top of changes requested → Address feedback");
  check(await page.locator('.card[data-key="o/r#108"] .next-row').count() === 0, "re-requested after changes → no chip, waiting on the reviewer");
  await page.evaluate(() => {
    const { state, render } = window.__dashboard;
    state.prs.find((p) => p.number === 108).updatedAt = new Date().toISOString();
    render();
  });
  check((await chipText(page, 108))?.includes("Checking"), "a change the companion hasn't seen yet shows Checking…");
  await page.evaluate(() => {
    const { state, render } = window.__dashboard;
    const pr = state.prs.find((p) => p.number === 108);
    const cp = state.companionState.prs["o/r#108"];
    pr.updatedAt = cp.updated;
    pr.commits.nodes[0].commit.statusCheckRollup = { state: "FAILURE", contexts: { nodes: [] } };
    cp.ci = "PENDING";
    render();
  });
  check((await chipText(page, 108))?.includes("Checking"), "a CI result the companion hasn't seen yet shows Checking…");
  await page.evaluate(() => {
    const { state, render } = window.__dashboard;
    state.companionState.prs["o/r#108"].updated = "2099-01-01T00:00:00Z";
    render();
  });
  const reread = () => queries.some((b) => b.variables?.ids?.includes("PR_o/r_108"));
  for (let i = 0; i < 250 && !reread(); i++) await new Promise((r) => setTimeout(r, 100));
  check(reread(), "a change the companion saw first refreshes the page's GitHub data");
  check(queries.filter((b) => b.variables?.q).length === 5, "and re-reads it by id instead of searching again");
  check(await page.locator('.card[data-key^="o/r#10"] .review-badge.changes').count() === 0, "no standalone Changes requested badge");
  check((await chipText(page, 103))?.trim() === "Merge", "approved and green → Merge");
  check((await chipText(page, 109))?.includes("Reply to approval · 1"), "approved with comments, auto-merge on → Reply to approval · 1");
  check(await page.locator('.card[data-key="o/r#104"] .next-row').count() === 0, "an idle draft gets no chip");
  check((await chipText(page, 105))?.includes("Nudge · 1d"), "companion memory chip → Nudge · 1d");
  check(await page.locator('.card[data-key="o/r#105"] .next-row.optional').count() === 1, "a nudge is optional, so it doesn't count as needing you");
  const nudgeUpdated = await page.evaluate(() => {
    const { state, render } = window.__dashboard;
    const pr = state.prs.find((p) => p.number === 105);
    const was = pr.updatedAt;
    pr.updatedAt = new Date().toISOString();
    render();
    render();
    return was;
  });
  check((await chipText(page, 105))?.includes("Nudge · 1d"), "a nudge stays up instead of Checking… while the companion catches up");
  await page.evaluate((was) => {
    const { state, render } = window.__dashboard;
    state.prs.find((p) => p.number === 105).updatedAt = was;
    render();
  }, nudgeUpdated);
  check(await page.locator('.card[data-key="o/r#101"] .plan-dot').count() === 1, "plan-ready dot on a card with a pass");
  check(await page.locator('.card[data-key="o/r#99"] .wt-links').count() === 1, "a merged card keeps its worktree links");
  check((await chipText(page, 98))?.includes("Reply to approval · 1"), "merged before you answered the approval → Reply to approval");
  check(await page.locator('.card[data-key="o/r#99"] .next-row').count() === 0, "a merged card without approval comments gets no chip");
  await page.locator('.card[data-key="o/r#98"] .next-row').click();
  await page.waitForSelector("#sidebar .next-card");
  check((await page.locator("#sidebar").locator(".next-card").first().textContent()).includes("I'd prefer a Map") &&
    await page.locator("#sidebar").locator('[data-pr-action="rerequest_review"]').count() === 0,
    "a merged PR keeps its approval feedback card, without re-request");
  if (shots) await page.screenshot({ path: `${shots}/sidebar-merged-approval.png` });
  await page.keyboard.press("Escape");
  check((await page.locator("#companion-status").textContent()).trim() === "Companion connected", "footer shows the companion connected");
  const [badge, steps, lanes] = await page.evaluate(() => [
    document.querySelector("#count-mine").textContent,
    document.querySelectorAll(".card:not(.merged) .next-row:not(.muted):not(.optional)").length,
    [...document.querySelectorAll(".lane-needs")].reduce((n, el) => n + parseInt(el.textContent, 10), 0),
  ]);
  check(steps > 0 && badge === String(steps) && lanes === steps, "the My PRs badge and lane headers count the Next steps on the board");
  if (shots) await page.screenshot({ path: `${shots}/board.png`, fullPage: true });

  check(await page.locator('.card[data-key="o/r#101"] .wt-links a[title="Open on GitHub"]').count() === 1, "a card has an Open on GitHub icon");
  await page.locator('.card[data-key="o/r#101"] .card-title').click();
  await page.waitForSelector("#sidebar .next-card");
  const sb = page.locator("#sidebar");
  check(await sb.isVisible(), "clicking a card opens the sidebar");
  await page.waitForTimeout(400);
  const lefts = await page.evaluate(() => [".header-inner", ".container"].map((s) => document.querySelector(s).getBoundingClientRect().left));
  check(Math.abs(lefts[0] - lefts[1]) < 1, "the header stays lined up with the board when the sidebar opens");
  check((await sb.locator(".sb-title").textContent()).includes("Add retry to the webhook sender"), "sidebar header shows the PR");
  check((await sb.locator(".next-card.first p").first().textContent()).includes("Rev questions the retry count"), "Next card shows the agent's judgment");
  check((await sb.locator(".sess-title").textContent()).includes("Add retries to the webhook sender"), "PR session shows the pinned session's title");
  check((await sb.locator(".sess-meta").textContent()).includes("2 yours · 1 Shipyard's"), "session turn counts split yours vs Shipyard's");
  await page.waitForSelector("#sidebar .sb-activity li");
  check(await sb.locator(".sb-activity .act-shipyard").count() > 0, "Activity includes Shipyard's own actions");
  await page.waitForTimeout(400);  // let the slide-in finish
  if (shots) await page.screenshot({ path: `${shots}/sidebar-feedback.png` });

  const respond = await sb.locator('a.btn-link:has-text("Address in")').getAttribute("href");
  check(respond?.startsWith("claude://code/new?folder=") && respond.includes(`q=${encodeURIComponent("Address the review feedback on https://github.com/o/r/pull/101 (branch me/webhook-retry)")}`),
    "Address opens a new session with the feedback prompt typed in");
  check((await sb.locator(".nc-proposal a.nc-thread").getAttribute("href"))?.endsWith("/pull/101#discussion_r501"), "drafted reply links to its review thread");
  check((await sb.locator(".nc-proposal .nc-snippet").textContent())?.includes("+    retries = 5"), "drafted reply shows the code it answers");
  check(await sb.locator(".nc-proposal a.nc-thread").evaluate((a) => getComputedStyle(a).textDecorationLine) === "underline", "thread link looks like a link");
  const reply = sb.locator(".nc-proposal textarea");
  check((await reply.inputValue()).startsWith("GitHub doesn't retry"), "drafted reply is shown in full before posting");
  await reply.fill("Edited: dropping to 3 retries with jitter.");
  await sb.locator('button.nc-run[data-action="post_reply"]').click();
  await page.waitForFunction(() => !document.querySelector('#sidebar button.nc-run[data-action="post_reply"]'));
  const posted = await page.evaluate(() => window.__dashboard.state.companionState.prs["o/r#101"].activity.map((a) => a.text));
  check(posted.includes("posted a reply on a review thread"), "Post reply goes through the companion and the card closes");
  await sb.locator('button.nc-run[data-pr-action="rerequest_review"]').click();
  await page.waitForFunction(() => !document.querySelector('#sidebar [data-pr-action="rerequest_review"]'), null, { timeout: 2000 });
  check(await page.locator(".lane").nth(1).locator('.card[data-key="o/r#101"]').count() === 1, "Re-request review moves the card to Waiting right away");

  await page.locator('.card[data-key="o/r#102"] .next-row').click();
  await page.waitForSelector('#sidebar [data-show-diff]');
  check(await sb.locator('button.nc-run[data-action="apply_fix"]').count() === 0, "Apply fix stays hidden until the diff is shown");
  await sb.locator("[data-show-diff]").click();
  check((await sb.locator(".next-card pre").textContent()).includes("registry=https://mirror.example/"), "diff shows first");
  check(await sb.locator('button.nc-run[data-action="apply_fix"]').count() === 1, "then Apply fix and push appears");
  check(await sb.locator('a.btn-link:has-text("View log")').count() === 1, "CI card links the failing log");

  await page.locator('.card[data-key="o/r#103"] .next-row').click();
  await sb.locator('button.nc-run[data-pr-action="merge"]').click();
  await page.waitForSelector('.card.merged[data-key="o/r#103"]', { timeout: 2000 });
  check(await page.locator('.card[data-key="o/r#103"]:not(.merged)').count() === 0, "Merge moves the card to Recently merged right away");

  await page.locator('.card[data-key="o/r#105"] .card-meta').click();
  await page.waitForSelector("#sidebar select[data-snooze]");
  await sb.locator("select[data-snooze]").selectOption("24");
  await page.waitForSelector('.card.snoozed[data-key="o/r#105"]');
  check((await chipText(page, 105))?.startsWith("Snoozed"), "snoozed card dims in place with the until-date on the chip");

  await page.locator('.tab[data-filter="review"]').click();
  await page.waitForSelector('.card[data-key="o/r#201"] .next-row');
  check((await chipText(page, 201))?.trim() === "Review", "review request → Review");
  check((await chipText(page, 202))?.includes("Re-review · 2 of 3 addressed"), "re-request → Re-review · 2 of 3 addressed");
  check(await page.locator('.card[data-key="o/r#203"]').count() === 1, "a comment after your changes request keeps the PR on Reviews");
  check((await chipText(page, 203))?.includes("Checking"), "a PR the companion hasn't seen yet shows Checking…");
  check((await page.locator('.card[data-key="o/r#203"] .review-badge').allTextContents()).some((t) => t.includes("Author replied · 1 of 2")),
    "a reply from the author on your thread shows Author replied");
  check((await page.locator('.card.merged[data-key="o/r#97"] .review-badge').allTextContents()).some((t) => t.includes("Author replied")),
    "an author's reply after merge shows Author replied on the Recently merged card");
  check((await page.locator('.card.merged[data-key="o/r#96"] .review-badge').allTextContents()).some((t) => t.includes("Author replied")),
    "an author's comment after your review shows Author replied");
  check(await page.locator('.card.merged[data-key="o/r#95"] .review-badge').count() === 0,
    "an author's comment you already answered shows no Author replied");
  check(await page.locator('.card[data-key="o/r#204"]').count() === 0, "a comment-only review doesn't add the PR to Reviews");
  await page.locator('.card[data-key="o/r#201"] .next-row').click();
  await page.waitForSelector('#sidebar a.btn-link:has-text("Finish review on GitHub")');
  check((await sb.locator('a.btn-link:has-text("Finish review on GitHub")').getAttribute("href")).endsWith("/files"), "pre-review drafts on GitHub and links to the diff");
  check((await sb.locator(".nc-verdict").textContent()).includes("Request changes"), "the pre-review leads with its verdict");
  const auto = sb.locator("input[data-auto-approve]");
  check(await auto.isChecked() && (await sb.locator(".sb-auto .auto-why").textContent()).startsWith("Set by the pre-review"),
    "the pre-review turns auto-approve on for small asks");
  await auto.uncheck();
  await page.waitForFunction(() => document.querySelector("#sidebar .sb-auto .auto-why")?.textContent.startsWith("Set by you"));
  check(!(await auto.isChecked()), "unchecking auto-approve saves your choice");
  await page.waitForTimeout(400);
  if (shots) await page.screenshot({ path: `${shots}/sidebar-review.png` });
  await page.keyboard.press("Escape");
  check(!(await sb.isVisible()), "Esc closes the sidebar");
  check(errors.length === 0, `no page errors${errors.length ? `: ${errors.join(" | ")}` : ""}`);

  console.log("narrow window");
  const narrow = await openPage(browser, COMPANION_PORT, { width: 420, height: 900 });
  await narrow.page.locator('.card[data-key="o/r#101"] .card-meta').click();
  await narrow.page.waitForSelector("#sidebar .sb-title");
  const box = await narrow.page.locator("#sidebar").boundingBox();
  const listBox = await narrow.page.locator(".list").boundingBox();
  check(box.y >= listBox.y + listBox.height - 1 && box.width <= 420, "sidebar stacks below the board on a narrow window");
  const overflow = await narrow.page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(overflow <= 0, "no horizontal scroll on a phone-width window");
  if (shots) await narrow.page.screenshot({ path: `${shots}/narrow.png`, fullPage: true });

  console.log("rate limit");
  let limited = 0;
  await narrow.context.route("https://api.github.com/graphql", (route) => ++limited && route.fulfill({ status: 403,
    headers: { "retry-after": "120" }, json: { message: "You have exceeded a secondary rate limit. Please wait a few minutes." } }));
  await narrow.page.evaluate(() => window.__dashboard.load(true));
  await narrow.page.waitForSelector("#stale-banner:not(.hidden)");
  const limitText = await narrow.page.locator("#stale-detail").textContent();
  const sent = limited;
  await narrow.page.evaluate(() => window.__dashboard.load(true));
  check(limitText.includes("You have exceeded a secondary rate limit.") && limitText.includes("Retrying at") &&
    sent > 0 && limited === sent, "a rate-limited 403 shows GitHub's message and holds off further calls");
  check(narrow.errors.length === 0, `no page errors narrow${narrow.errors.length ? `: ${narrow.errors.join(" | ")}` : ""}`);

  await browser.close();
} finally {
  for (const child of children) child.kill();
}
console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
