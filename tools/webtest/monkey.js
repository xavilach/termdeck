// Random use of the real page: clicks, typing and choices at random, looking for errors the page reports.
// `node monkey.js [seed] [steps]`; prints what went wrong with the seed, so that it can be run again.
const { chromium } = require("playwright");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const seed = Number(process.argv[2] || Date.now() % 100000);
const steps = Number(process.argv[3] || 300);
let state = seed;
const random = () => { state = (state * 1664525 + 1013904223) % 4294967296; return state / 4294967296; };
const pick = (list) => list[Math.floor(random() * list.length)];

(async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "termdeck-monkey-"));
  execFileSync("go", ["build", "-o", path.join(dir, "termdeck"), "."], { cwd: path.join(__dirname, "..", "..") });
  const child = spawn(path.join(dir, "termdeck"), ["--no-browser"], { env: { ...process.env, TERMDECK_HOME: path.join(dir, "home") } });
  let stderr = "";
  child.stderr.on("data", (d) => { stderr += d; });
  const url = await new Promise((resolve) => { let out = ""; child.stdout.on("data", (d) => { out += d; const m = out.match(/http:\/\/\S+/); if (m) resolve(m[0]); }); });
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  const page = await context.newPage();
  const problems = [];
  page.on("pageerror", (e) => problems.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) problems.push("console: " + m.text()); });
  page.on("dialog", (d) => d.dismiss());
  await page.goto(url);
  await page.waitForSelector("#port-tree .item.port", { state: "attached" });
  await page.locator("#port-tree .node-head", { hasText: "Virtual ports" }).locator(".toggle").click();
  const ports = ["fake:echo", "fake:shell", "fake:counter", "fake:binary", "fake:stream", "fake:flaky", "fake:link-a", "fake:link-b"];
  const words = ["hello", "41 42", "\\r\\n", "dtr=1; wait=10", "AT", "300", "0100 0001", "x".repeat(200), "/(/", "!tick", "tick 1"];
  const log = [];
  for (let i = 0; i < steps && problems.length === 0; i++) {
    const action = pick(["open", "open", "click", "click", "click", "select", "type", "shortcut", "view", "close", "wait", "settings", "script"]);
    log.push(action);
    try {
      if (action === "open") {
        const port = pick(ports);
        await page.locator("#port-tree .item.port .name").filter({ hasText: new RegExp(`^${port}$`) }).dblclick({ timeout: 1500 });
      } else if (action === "click") {
        const buttons = await page.locator("button:visible:not([disabled])").all();
        const skip = /Close port|Reset|Clear|Import|Export|Cancel transfer/;
        const candidates = [];
        for (const b of buttons) { const t = (await b.textContent().catch(() => "")) || ""; if (!skip.test(t)) candidates.push(b); }
        if (candidates.length) await pick(candidates).click({ timeout: 1500, noWaitAfter: true });
      } else if (action === "select") {
        const selects = await page.locator("select:visible:not([disabled])").all();
        if (selects.length) {
          const s = pick(selects);
          const options = await s.locator("option").evaluateAll((os) => os.map((o) => o.value));
          await s.selectOption(pick(options), { timeout: 1500 });
        }
      } else if (action === "type") {
        const inputs = await page.locator("input[type=text]:visible:not([disabled]), input:not([type]):visible:not([disabled]), input[type=search]:visible, textarea:visible").all();
        if (inputs.length) await pick(inputs).fill(pick(words), { timeout: 1500 });
      } else if (action === "shortcut") {
        await page.keyboard.press(pick(["Alt+Shift+M", "Alt+Shift+S", "Alt+Shift+B", "Alt+Shift+PageDown", "Alt+Shift+PageUp", "Alt+Shift+H", "Escape", "Control+Shift+L", "Control+Shift+F", "Alt+Shift+ArrowLeft", "Alt+Shift+ArrowRight"]));
      } else if (action === "view") {
        const view = page.locator(".viewbar [data-view]:visible").first();
        if (await view.count()) await view.selectOption(pick(["text", "hex", "dec", "bin", "frames"]), { timeout: 1500 });
      } else if (action === "close") {
        const tab = page.locator(".tab .close").first();
        if (await tab.count()) { await tab.click({ timeout: 1500 }); if (await page.locator(".tab .close.confirm").count()) await page.locator(".tab .close.confirm").first().click({ timeout: 1500 }); }
      } else if (action === "settings") {
        // the settings dialog of the active tab: random values in its fields, then Save
        await page.locator(".toolbar [data-action=settings]:visible").first().click({ timeout: 1500 });
        await page.waitForFunction(() => document.querySelector("#panel").open, null, { timeout: 1500 });
        const pages = await page.locator("#profile-form .form-tabs button").all();
        for (const tabButton of pages) {  // every page of the dialog gets some random values
          await tabButton.click({ timeout: 1000 });
          const fields = await page.locator("#profile-form .form-page:not([hidden]) :is(input:not([type=hidden]):not([type=file]), select, textarea)").all();
          for (const f of fields) {
            if (random() < 0.7) continue;
            const tag = await f.evaluate((e) => e.tagName + ":" + e.type);
            try {
              if (tag.startsWith("SELECT")) await f.selectOption(pick(await f.locator("option").evaluateAll((os) => os.map((o) => o.value))), { timeout: 500 });
              else if (tag.endsWith("checkbox")) await f.setChecked(random() < 0.5, { timeout: 500 });
              else if (tag.endsWith("number")) await f.fill(pick(["0", "5", "100", "-1", "99999999", "abc"]), { timeout: 500 });
              else await f.fill(pick(["", "ERROR = red", "x | y | z", "a => b", "name | text | hi", "sync = AA\nsize = 4", "/(/", "logs/x-{date}.log", "..", "ä€"]), { timeout: 500 });
            } catch { /* a field that cannot be filled */ }
          }
        }
        await page.locator("#profile-form button[type=submit]").click({ timeout: 1500 });
        await page.waitForTimeout(150);
      } else if (action === "script") {
        await page.locator("button[title='New script for Generic']").first().click({ timeout: 1500 });
        await page.fill("#script-form [name=name]", "m" + Math.floor(random() * 1000));
        await page.fill("#script-form [name=commands]", pick(["echo hi", "@expect=x; timeout=50", "@dtr=0; wait=10", "a\nb\n@hex=41", "@@x", "@speed=1"]));
        await page.locator("#script-form button[type=submit]").click({ timeout: 1500 });
        await page.waitForTimeout(150);
      } else await page.waitForTimeout(100 + Math.floor(random() * 400));
    } catch (e) {
      if (!/Timeout|detached|not attached|closed|intercepts pointer|outside of the viewport|not visible|not stable/i.test(e.message)) problems.push(`step ${i} (${action}): ${e.message.split("\n")[0]}`);
    }
    // a dialog left open would block the next clicks: close it
    await page.evaluate(() => document.querySelectorAll("dialog[open]").forEach((d) => d.close())).catch(() => {});
  }
  await page.waitForTimeout(500);
  const race = /DATA RACE|panic:/.test(stderr) ? ["the program: " + stderr.slice(0, 400)] : [];
  console.log(`seed ${seed}, ${log.length} steps: ${problems.length + race.length ? "PROBLEMS" : "no problems"}`);
  for (const p of [...problems, ...race]) console.log("  " + p);
  await browser.close();
  child.kill();
  process.exit(problems.length + race.length ? 1 : 0);
})().catch((e) => { console.log("monkey crashed:", e.stack); process.exit(2); });
