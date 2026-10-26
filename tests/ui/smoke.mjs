// Browser smoke test for the board chips and the sidebar.
//
//   node tests/ui/smoke.mjs [screenshot-dir]
//
// Starts tests/ui/demo_server.py (the real companion server over fake GitHub and agent
// data) and checks what renders. Needs Playwright with Chromium.
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
    localStorage.setItem("ghpr.repos", JSON.stringify(["o/r"]));
    localStorage.setItem("ghpr.currentRepo", "o/r");
  });
  const direct = [];
  await context.route("https://api.github.com/**", (route) => { direct.push(route.request().url()); return route.abort(); });
  await context.route("https://avatars.example/**", (route) => route.fulfill({ status: 404, body: "" }));
  await context.route("https://fonts.googleapis.com/**", (route) => route.fulfill({ body: "" }));
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });
  await page.goto(`http://localhost:${port}/`);
  await page.waitForSelector(".card[data-key]");
  return { page, errors, context, direct };
}

const chipText = (page, n) => page.locator(`.card[data-key="o/r#${n}"] .next-row .next-label`).first().textContent().catch(() => null);

const children = [];
try {
  children.push(await start("python3", ["tests/ui/demo_server.py", String(COMPANION_PORT)], /demo companion on/));
  const browser = await chromium.launch();

  console.log("companion mode");
  const { page, errors, direct } = await openPage(browser, COMPANION_PORT);
  await page.waitForSelector('.card[data-key="o/r#105"] .next-row');
  check((await chipText(page, 101))?.includes("Address feedback · 1"), "changes requested → Address feedback · 1");
  check((await chipText(page, 102))?.includes("Fix CI · rspec"), "failing check → Fix CI · rspec");
  check((await chipText(page, 106))?.includes("Re-request review · 1"), "replied last on every thread → Re-request review · 1");
  check((await chipText(page, 107))?.includes("Address feedback · 1"), "a comment review on top of changes requested → Address feedback");
  check(await page.locator('.card[data-key="o/r#108"] .next-row').count() === 0, "re-requested after changes → no chip, waiting on the reviewer");
  check(await page.locator('.card[data-key^="o/r#10"] .review-badge.changes').count() === 0, "no standalone Changes requested badge");
  check((await chipText(page, 103))?.trim() === "Merge", "approved and green → Merge");
  check((await chipText(page, 109))?.includes("Reply to approval · 1"), "approved with comments, auto-merge on → Reply to approval · 1");
  check(await page.locator('.card[data-key="o/r#104"] .next-row').count() === 0, "an idle draft gets no chip");
  check((await chipText(page, 105))?.includes("Nudge · 1d"), "companion memory chip → Nudge · 1d");
  check(await page.locator('.card[data-key="o/r#105"] .next-row.optional').count() === 1, "a nudge is optional, so it doesn't count as needing you");
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
  check((await page.locator("#companion-status").textContent()).trim() === "Connected", "footer shows Shipyard connected");
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
  const timeline = await page.waitForFunction(() => document.querySelector("#sidebar .sb-activity")?.textContent.includes("Retry failed deliveries"),
    null, { timeout: 5000 }).then(() => true, () => false);
  check(timeline, "Activity includes the PR's GitHub timeline, read through the server");
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

  check(direct.length === 0, `the page never calls api.github.com itself${direct.length ? `: ${direct.join(" | ")}` : ""}`);

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

  console.log("GitHub failure");
  await narrow.page.evaluate(() => {
    const { state, updateGitHubStatus } = window.__dashboard;
    state.companionState.status.github = "GitHub rate limit: You have exceeded a secondary rate limit. Retrying at 3:05 PM.";
    updateGitHubStatus();
  });
  await narrow.page.waitForSelector("#stale-banner:not(.hidden)");
  check((await narrow.page.locator("#stale-detail").textContent()).includes("You have exceeded a secondary rate limit."),
    "a failed server poll shows GitHub's message in the stale banner");
  check(narrow.errors.length === 0, `no page errors narrow${narrow.errors.length ? `: ${narrow.errors.join(" | ")}` : ""}`);

  await browser.close();
} finally {
  for (const child of children) child.kill();
}
console.log(failures.length ? `\n${failures.length} check(s) failed` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
