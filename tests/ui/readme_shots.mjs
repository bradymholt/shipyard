// Regenerates the README screenshots in docs/ from tests/ui/readme_demo.py.
//
//   node tests/ui/readme_shots.mjs
import { execFileSync, spawn } from "node:child_process";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
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
const docs = path.join(repo, "docs");
const PORT = 4398;

const server = spawn("python3", ["tests/ui/readme_demo.py", String(PORT)], { cwd: repo, stdio: ["ignore", "pipe", "inherit"] });
await new Promise((resolve, reject) => {
  server.stdout.on("data", (d) => { if (/readme demo on/.test(String(d))) resolve(); });
  server.on("exit", (code) => reject(new Error(`readme_demo.py exited ${code}`)));
});

const browser = await chromium.launch();
try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 600 }, deviceScaleFactor: 2 });
  await context.route("https://fonts.googleapis.com/**", (route) => route.fulfill({ body: "" }));
  const page = await context.newPage();
  await page.goto(`http://localhost:${PORT}/`);
  await page.waitForSelector(".card[data-key]");
  await page.waitForTimeout(1000);
  await page.screenshot({ path: path.join(docs, "board.png"), fullPage: true });

  // Crops the sidebar to its header and first `sections` sections.
  const sidebar = async (number, file, sections) => {
    await page.setViewportSize({ width: 1280, height: 1200 });
    await page.click(`.card[data-key="acme/storefront#${number}"]`);
    await page.waitForTimeout(800);
    const box = await page.locator("#sidebar").boundingBox();
    const last = await page.locator("#sidebar .sb-section").nth(sections - 1).boundingBox();
    await page.screenshot({ path: path.join(docs, file),
                            clip: { x: box.x, y: box.y, width: box.width, height: last.y + last.height + 16 - box.y } });
    await page.keyboard.press("Escape");
    await page.setViewportSize({ width: 1280, height: 600 });
    await page.waitForTimeout(300);
  };
  await sidebar(412, "feedback.png", 1);
  await sidebar(415, "ci-failure.png", 1);
  await page.click(`.tab[data-filter="review"]`);
  await page.waitForTimeout(800);
  await page.screenshot({ path: path.join(docs, "reviews.png"), fullPage: true });
  await sidebar(421, "pre-review.png", 2);
} finally {
  await browser.close();
  server.kill();
}
