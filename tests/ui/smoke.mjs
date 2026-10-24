// Browser smoke test for the board chips and the sidebar.
//
//   node tests/ui/smoke.mjs [screenshot-dir]
//
// Starts tests/ui/demo_server.py (the real companion server over fake GitHub and agent
// data) and a plain static server (hosted mode), answers the page's own GitHub calls
// with matching fixtures, and checks what renders. Needs Playwright with Chromium.
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
const STATIC_PORT = 4398;

const ago = (h) => new Date(Date.now() - h * 3600e3).toISOString();
const user = (login) => ({ __typename: "User", login, avatarUrl: `https://avatars.example/${login}?v=4` });

function pr(number, extra = {}) {
  const author = extra.author || "me";
  return {
    id: `PR_o/r_${number}`, number, title: extra.title || `PR ${number}`, url: `https://github.com/o/r/pull/${number}`,
    isDraft: !!extra.draft, createdAt: ago(100), updatedAt: extra.updated || ago(1), mergedAt: extra.mergedAt || null,
    headRefName: extra.branch || `me/pr-${number}`, baseRefName: "main", headRefOid: `h${number}`,
    additions: 40, deletions: 12, changedFiles: 3, autoMergeRequest: null, labels: { nodes: [] },
    viewerCanEnableAutoMerge: true, viewerCanDisableAutoMerge: false,
    repository: { nameWithOwner: "o/r" }, author: { login: author, avatarUrl: `https://avatars.example/${author}?v=4` },
    reviewDecision: extra.decision || null, comments: { totalCount: extra.comments || 0 },
    reviewRequests: { nodes: (extra.requested || []).map((l) => ({ requestedReviewer: user(l) })) },
    latestReviews: { nodes: (extra.reviews || []).map(([state, login]) => ({ state, author: { login, avatarUrl: null } })) },
    reviewThreads: { nodes: (extra.threads || []).map((login) => ({ isResolved: false, comments: { nodes: [{ author: { login } }] } })) },
    commits: { nodes: [{ commit: { oid: `h${number}`, statusCheckRollup: extra.ci === null ? null : {
      state: extra.ci || "SUCCESS",
      contexts: { nodes: (extra.failing || []).map((name) => ({ __typename: "CheckRun", name, conclusion: "FAILURE",
        detailsUrl: `https://github.com/o/r/actions/runs/8/job/7` })) } } } }] },
    pendingReviews: { nodes: [] },
    viewerLatestReview: extra.myReview || null,
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
];
const REQUESTED = [
  pr(201, { author: "alice", title: "Fuzzy search in the index", requested: ["me"] }),
  pr(202, { author: "bob", title: "Export to CSV", requested: ["me"] }),
];
const REVIEWED = [pr(202, { author: "bob", title: "Export to CSV", requested: ["me"],
  myReview: { state: "CHANGES_REQUESTED", commit: { oid: "old" }, onBehalfOf: { nodes: [] } } })];
const MERGED = [pr(99, { title: "Old feature", branch: "me/old-feature", mergedAt: ago(20) })];

function answer(body) {
  const q = body.variables?.q || "";
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
  if (q.includes("is:merged")) nodes = q.includes("author:@me") ? MERGED : [];
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
  await context.route("https://api.github.com/graphql", (route) =>
    route.fulfill({ json: { data: answer(JSON.parse(route.request().postData())) } }));
  await context.route("https://avatars.example/**", (route) => route.fulfill({ status: 404, body: "" }));
  await context.route("https://fonts.googleapis.com/**", (route) => route.fulfill({ body: "" }));
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });
  await page.goto(`http://localhost:${port}/`);
  await page.waitForSelector(".card[data-key]");
  return { page, errors, context };
}

const chipText = (page, n) => page.locator(`.card[data-key="o/r#${n}"] .next-row .next-label`).first().textContent().catch(() => null);

const children = [];
try {
  children.push(await start("python3", ["tests/ui/demo_server.py", String(COMPANION_PORT)], /demo companion on/));
  children.push(await start("python3", ["-u", "-m", "http.server", String(STATIC_PORT), "--bind", "127.0.0.1"], /Serving HTTP/));
  const browser = await chromium.launch();

  console.log("companion mode");
  const { page, errors } = await openPage(browser, COMPANION_PORT);
  await page.waitForSelector('.card[data-key="o/r#105"] .next-row');
  check((await chipText(page, 101))?.includes("Address feedback · 1"), "changes requested → Address feedback · 1");
  check((await chipText(page, 102))?.includes("Fix CI · rspec"), "failing check → Fix CI · rspec");
  check((await chipText(page, 106))?.includes("Re-request review · 1"), "replied last on every thread → Re-request review · 1");
  check((await chipText(page, 107))?.includes("Address feedback · 1"), "a comment review on top of changes requested → Address feedback");
  check(await page.locator('.card[data-key="o/r#108"] .next-row').count() === 0, "re-requested after changes → no chip, waiting on the reviewer");
  check(await page.locator('.card[data-key^="o/r#10"] .review-badge.changes').count() === 0, "no standalone Changes requested badge");
  check((await chipText(page, 103))?.trim() === "Merge", "approved and green → Merge");
  check(await page.locator('.card[data-key="o/r#104"] .next-row').count() === 0, "an idle draft gets no chip");
  check((await chipText(page, 105))?.includes("Nudge · 1d"), "companion memory chip → Nudge · 1d");
  check(await page.locator('.card[data-key="o/r#101"] .plan-dot').count() === 1, "plan-ready dot on a card with a pass");
  check(await page.locator('.card[data-key="o/r#99"] .wt-links').count() === 1, "a merged card keeps its worktree links");
  check((await page.locator("#companion-status").textContent()).includes("watching your PRs"), "footer shows the companion watching");
  if (shots) await page.screenshot({ path: `${shots}/board.png`, fullPage: true });

  await page.locator('.card[data-key="o/r#101"] .card-meta').click();
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

  const respond = await sb.locator('a.btn-link:has-text("Respond in")').getAttribute("href");
  check(respond?.startsWith("claude://code/new?folder=") && respond.includes(`q=${encodeURIComponent("Address the review feedback on https://github.com/o/r/pull/101 (branch me/webhook-retry)")}`),
    "Respond opens a new session with the feedback prompt typed in");
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

  await page.locator('.card[data-key="o/r#105"] .card-meta').click();
  await page.waitForSelector("#sidebar select[data-snooze]");
  await sb.locator("select[data-snooze]").selectOption("24");
  await page.waitForSelector('.card.snoozed[data-key="o/r#105"]');
  check((await chipText(page, 105))?.startsWith("Snoozed"), "snoozed card dims in place with the until-date on the chip");

  await page.locator('.tab[data-filter="review"]').click();
  await page.waitForSelector('.card[data-key="o/r#201"] .next-row');
  check((await chipText(page, 201))?.trim() === "Review", "review request → Review");
  check((await chipText(page, 202))?.includes("Re-review · 2 of 3 addressed"), "re-request → Re-review · 2 of 3 addressed");
  await page.locator('.card[data-key="o/r#201"] .next-row').click();
  await page.waitForSelector('#sidebar button.nc-run[data-action="submit_review"]');
  check((await sb.locator(".nc-inline").textContent()).includes("search/tokenize.py:12"), "pre-review shows its inline comments before submitting");
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

  console.log("hosted mode (no companion)");
  const hosted = await openPage(browser, STATIC_PORT);
  await hosted.page.waitForSelector('.card[data-key="o/r#101"] .next-row');
  check((await chipText(hosted.page, 101))?.includes("Address feedback"), "GitHub-derived chips work hosted");
  check((await chipText(hosted.page, 103))?.trim() === "Merge", "Merge chip works hosted");
  check(await hosted.page.locator('.card[data-key="o/r#105"] .next-row').count() === 0, "memory chips are absent hosted, not broken");
  await hosted.page.locator('.card[data-key="o/r#101"] .card-meta').click();
  await hosted.page.waitForSelector("#sidebar .sb-activity li");
  check(await hosted.page.locator("#sidebar .sb-h", { hasText: "Next" }).count() === 0, "hosted sidebar has no Next section");
  check(await hosted.page.locator("#sidebar .sb-h", { hasText: "Activity" }).count() === 1, "hosted sidebar still has Activity");
  check(hosted.errors.length === 0, `no page errors hosted${hosted.errors.length ? `: ${hosted.errors.join(" | ")}` : ""}`);

  await browser.close();
} finally {
  for (const child of children) child.kill();
}
console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
