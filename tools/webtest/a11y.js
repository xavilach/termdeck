// Accessibility checks of the real page with axe-core: `npm run a11y`. Prints the violations; exits 1 when there are any.
const { chromium } = require("playwright");
const { AxeBuilder } = require("@axe-core/playwright");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

(async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "termdeck-a11y-"));
  execFileSync("go", ["build", "-o", path.join(dir, "termdeck"), "."], { cwd: path.join(__dirname, "..", "..") });
  const child = spawn(path.join(dir, "termdeck"), ["--no-browser"], { env: { ...process.env, TERMDECK_HOME: path.join(dir, "home") } });
  const url = await new Promise((resolve) => { let out = ""; child.stdout.on("data", (d) => { out += d; const m = out.match(/http:\/\/\S+/); if (m) resolve(m[0]); }); });
  const browser = await chromium.launch();
  let total = 0;
  const page = await (await browser.newContext({ viewport: { width: 1280, height: 900 } })).newPage();  // the start link works once
  await page.goto(url);
  await page.waitForSelector("#port-tree .item.port", { state: "attached" });
  await page.locator("#port-tree .node-head", { hasText: "Virtual ports" }).locator(".toggle").click();
  await page.locator("#port-tree .item.port", { hasText: "fake:counter" }).dblclick();
  await page.waitForFunction(() => tabs[0]?.state === "connected");
  for (const scheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.evaluate(() => document.querySelectorAll("dialog[open]").forEach((d) => d.close()));
    await page.reload();
    await page.waitForFunction(() => tabs[0]?.state === "connected", null, { timeout: 8000 });
    const states = {
      "session open": async () => {},
      "more menu": async () => { await page.locator(".viewbar details.more > summary").click(); },
      "graph": async () => { await page.locator(".viewbar details.more > summary").click(); await page.click(".viewbar [data-graph]"); },
      "settings dialog": async () => { await page.click(".toolbar [data-action=settings]"); },
      "help": async () => { await page.keyboard.press("Escape"); await page.click("#open-help"); },
      "tunnel dialog": async () => { await page.keyboard.press("Escape"); await page.click("#open-tunnel"); },
    };
    for (const [name, enter] of Object.entries(states)) {
      await enter();
      await page.waitForTimeout(200);
      const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      for (const v of results.violations) {
        total++;
        console.log(`[${scheme}] ${name}: ${v.id} (${v.impact}) ${v.help} — ${v.nodes.length} node(s): ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
      }
    }
  }
  await browser.close();
  child.kill();
  console.log(total ? `${total} violation group(s)` : "no violations");
  process.exit(total ? 1 : 0);
})().catch((e) => { console.log("a11y crashed:", e.stack); process.exit(2); });
