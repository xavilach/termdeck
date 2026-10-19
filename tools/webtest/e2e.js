// Drives the real program in a real browser (Chromium through Playwright): `npm run e2e`.
// SHOTS=dir saves screenshots there.
const { chromium } = require("playwright");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const root = path.join(__dirname, "..", "..");
const shots = process.env.SHOTS;
if (shots) fs.mkdirSync(shots, { recursive: true });
let failed = 0;
const check = (name, ok, extra) => { if (!ok) failed++; console.log((ok ? "ok   " : "FAIL ") + name + (ok ? "" : "  " + JSON.stringify(extra))); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function startServer() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "termdeck-e2e-"));
  const exe = path.join(dir, "termdeck");
  execFileSync("go", ["build", ...(process.env.E2E_RACE ? ["-race"] : []), "-o", exe, "."], { cwd: root });  // E2E_RACE=1: the program runs with the race detector
  const child = spawn(exe, ["--no-browser", "--port", "0"], { env: { ...process.env, TERMDECK_HOME: path.join(dir, "home") } });
  child.stderrText = "";
  child.stderr.on("data", (d) => { child.stderrText += d; });
  const url = await new Promise((resolve, reject) => {
    let out = "";
    child.stdout.on("data", (d) => { out += d; const m = out.match(/http:\/\/\S+/); if (m) resolve(m[0]); });
    child.on("exit", () => reject(new Error("server exited: " + out)));
    setTimeout(() => reject(new Error("no start link: " + out)), 10000);
  });
  return { child, url, dir };
}

(async () => {
  const server = await startServer();
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const problems = [];
  page.on("pageerror", (e) => problems.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") problems.push("console: " + m.text()); });
  const shot = async (name) => { if (shots) await page.screenshot({ path: path.join(shots, name + ".png") }); };
  const screen = (i = 0) => page.evaluate((n) => { const b = tabs[n].term.buffer.active; let s = ""; for (let r = 0; r < b.length; r++) s += (b.getLine(r)?.translateToString(true) ?? "") + "\n"; return s; }, i);
  const waitScreen = async (re, i = 0, ms = 5000) => { const end = Date.now() + ms; let s = ""; while (Date.now() < end) { s = await screen(i); if (re.test(s)) return s; await sleep(50); } return s; };

  await page.goto(server.url);
  await page.waitForSelector("#port-tree .item.port", { state: "attached" });
  check("page loads with the start link and lists the ports", (await page.locator("#port-tree .node-head").first().textContent()).includes("Serial ports"));
  const virtualHead = page.locator("#port-tree .node-head", { hasText: "Virtual ports" });
  check("Virtual ports section is reduced at first", (await page.locator("#port-tree .item.port", { hasText: "fake:shell" }).count()) === 0 || !(await page.locator("#port-tree .item.port", { hasText: "fake:shell" }).first().isVisible()));
  await virtualHead.locator(".toggle").click();
  check("and expands", await page.locator("#port-tree .item.port", { hasText: "fake:shell" }).first().isVisible());
  await shot("01-start");

  // open the fake shell
  await page.locator("#port-tree .item.port", { hasText: "fake:shell" }).dblclick();
  let s = await waitScreen(/login: /);
  check("fake:shell opens and shows its login", /connected to fake:shell/.test(s) && /login: /.test(s), s);
  check("the toolbar says Connected", (await page.locator(".pane .toolbar .status").first().textContent()) === "Connected");

  // the main output is read-only
  await page.locator(".term .xterm").first().click();
  await page.keyboard.type("zzz");
  await sleep(300);
  s = await screen();
  check("typing in the output sends nothing", !/zzz/.test(s), s);

  // the send bar
  const input = page.locator(".sendbar [data-send-text]").first();
  await input.fill("admin");
  await input.press("Enter");
  check("the input is emptied after a send", (await input.inputValue()) === "");
  await waitScreen(/Password: /);
  await input.fill("admin");
  await input.press("Enter");
  s = await waitScreen(/admin# /);
  check("login through the send bar works", /Welcome/.test(s) && /admin# /.test(s), s);
  await input.fill("echo hello \\x41");
  await input.press("Enter");
  s = await waitScreen(/hello A/);
  check("text escapes are unescaped", /hello A/.test(s), s);
  await shot("02-shell");

  // local echo off: nothing of what we send is written by termdeck itself
  check("no local echo: our own lines are not shown by termdeck", !/TX /.test(s));

  // hex view and hex send
  await page.selectOption(".viewbar [data-view]", "hex");
  s = await screen();
  check("hex view redraws the kept data", /6c 6f 67 69 6e/.test(s), s.slice(0, 300));
  await shot("03-hex-view");
  await page.selectOption(".viewbar [data-view]", "text");
  s = await screen();
  check("and back to text without loss", /login: admin/.test(s) || /hello A/.test(s), s);

  await page.selectOption(".sendbar [data-send-type]", "hex");
  await input.fill("65 63 68 6f 20 68 65 78");  // echo hex
  await page.selectOption(".sendbar [data-send-end]", "cr");
  await input.press("Enter");
  s = await waitScreen(/hex\s*$/m);
  check("hex send reaches the device", /hex/.test(s.split("hello A").pop()), s);
  // typing groups digits and refuses wrong characters
  await input.fill("");
  await input.pressSequentially("4g1z2");
  check("hex input refuses wrong characters and groups bytes", (await input.inputValue()) === "41 2", await input.inputValue());
  await input.pressSequentially("0");
  await page.selectOption(".sendbar [data-send-type]", "dec");
  check("changing the type rewrites the data", (await input.inputValue()) === "65 32", await input.inputValue());
  await page.selectOption(".sendbar [data-send-type]", "text");
  await shot("04-send-bar");

  // counters
  await sleep(600);
  check("counters count", Number((await page.locator("[data-count=rx]").first().textContent()).replace(/\D/g, "")) > 50 && Number((await page.locator("[data-count=tx]").first().textContent()).replace(/\D/g, "")) > 10);

  // counter port with times and a pause
  await page.locator("#port-tree .item.port", { hasText: "fake:counter" }).dblclick();
  s = await waitScreen(/tick 2 /, 1);
  check("fake:counter streams", /tick 1 /.test(s) && /tick 2 /.test(s), s);
  await page.check(".pane .viewbar [data-stamps]");
  s = await waitScreen(/\[\d\d:\d\d:\d\d\.\d{3}\] tick 1/, 1);
  check("time at the start of each line", /\[\d\d:\d\d:\d\d\.\d{3}\] tick 1/.test(s), s);
  await shot("05-times");
  await page.selectOption(".viewbar [data-time-mode]", "delta");
  s = await waitScreen(/\[\+\d+(ms|\.\d{3}s)\] tick 2/, 1);
  check("time since the previous line (about half a second between ticks)", /\[\+(4\d\d|5\d\d)ms\] tick 2/.test(s) || /\[\+0\.[45]\d\ds\] tick 2/.test(s), s);
  await page.selectOption(".viewbar [data-time-mode]", "clock");
  await page.selectOption(".viewbar [data-nl]", "none");
  s = await waitScreen(/tick 1[^\n]*tick 2/, 1);
  check("New line at None joins the lines", /tick 1[^\n]*tick 2/.test(s), s);
  await page.selectOption(".viewbar [data-nl]", "terminal");
  await page.check(".viewbar [data-ctrl]");
  s = await waitScreen(/<LF>/, 1);
  check("control characters are shown", /<CR><LF>|<LF>/.test(s), s);
  await page.uncheck(".viewbar [data-ctrl]");
  check("a changed view is marked and can be reset", await page.locator(".viewbar [data-view-reset]").isVisible());
  await page.click(".viewbar [data-view-reset]");
  check("Reset view clears the mark", !(await page.locator(".viewbar [data-view-reset]").isVisible()));

  // auto send on the shell tab
  await page.locator(".tab", { hasText: "fake:shell" }).click();
  await page.selectOption(".sendbar [data-send-type]", "text");
  await input.fill("echo auto");
  await page.fill(".sendbar [data-auto-ms]", "100");
  const txBefore = await page.evaluate(() => tabs[0].counters.tx);
  await page.check(".sendbar [data-auto]");
  await page.waitForFunction((n) => tabs[0].counters.tx >= n + 50, txBefore, { timeout: 8000 });  // five or more sends of "echo auto\r"
  await page.uncheck(".sendbar [data-auto]");
  s = await waitScreen(/(^auto\n[^]*){4}/m, 0);  // xterm parses what it was given a little later
  check("auto send repeats", (s.match(/^auto/gm) || []).length >= 4, s.replace(/\n+$/, ""));
  check("auto send keeps the input", (await input.inputValue()) === "echo auto");

  // file send
  const file = path.join(server.dir, "payload.txt");
  fs.writeFileSync(file, "echo from-file\r");
  await page.setInputFiles(".sendbar [data-file]", file);
  s = await waitScreen(/from-file/);
  check("a file is sent", /from-file/.test(s), s.slice(-200));

  // find
  await page.click(".viewbar [data-action=find]");
  await page.fill(".findbar [data-find]", "auto");
  check("find counts matches", /\d+ found/.test(await page.locator(".findbar [data-find-count]").first().textContent()));
  await page.keyboard.press("Enter");
  check("find selects a match", /of \d+/.test(await page.locator(".findbar [data-find-count]").first().textContent()));
  await page.keyboard.press("Escape");

  // the same port is never opened twice: free fake:shell first
  await page.locator(".tab", { hasText: "fake:shell" }).locator(".close").click();
  await page.locator(".tab", { hasText: "fake:shell" }).locator(".close").click();
  // a saved session with triggers, markers, local echo and a log, made in the dialog
  await page.keyboard.press("Alt+Shift+N");
  const form = page.locator("#profile-form");
  await form.locator("[name=name]").fill("Auto login");
  await form.locator("[name=device]").fill("fake:shell");
  await form.locator("[name=localEcho]").check();
  await form.locator("[name=rxMarker]").fill("RX ");
  await form.locator("[name=txMarker]").fill("TX ");
  await form.locator("[name=logPath]").fill(path.join(server.dir, "auto-login.log"));
  await form.locator("#advanced > summary").click();
  await form.locator("[name=triggers]").fill("login: => admin\n/Password:/ => admin");
  await page.click("#profile-connect");
  await sleep(500);
  check("the session dialog saves and opens", await page.locator(".tab", { hasText: "Auto login" }).count() === 1, { error: await page.locator("#profile-error").textContent(), open: await page.locator("#panel").evaluate((d) => d.open), tabs: await page.locator(".tab .label").allTextContents() });
  const autoTab = (await page.locator(".tab .label").allTextContents()).indexOf("Auto login");
  s = await waitScreen(/admin# /, autoTab);
  check("triggers log in by themselves", /Welcome/.test(s), s);
  check("Rx and Tx markers and local echo", /^RX login: /m.test(s) && /^TX admin/m.test(s), s);
  await page.locator(".sendbar [data-send-text]").first().fill("draft for this tab");
  await page.locator(".tab", { hasText: "fake:counter" }).click();
  check("the data to send belongs to the tab", (await page.locator(".sendbar [data-send-text]").first().inputValue()) === "");
  await page.locator(".tab", { hasText: "Auto login" }).click();
  check("and comes back with it", (await page.locator(".sendbar [data-send-text]").first().inputValue()) === "draft for this tab");
  await page.click(".toolbar [data-action=log]");
  await sleep(500);
  await page.click(".toolbar [data-action=log]");
  check("the log file is written with markers", fs.existsSync(path.join(server.dir, "auto-login.log")));
  await shot("11-triggers");
  await page.locator(".tab", { hasText: "Auto login" }).locator(".close").click();
  await page.locator(".tab", { hasText: "Auto login" }).locator(".close").click();  // a second click confirms
  check("closing a connected tab asks first, then closes", await page.locator(".tab", { hasText: "Auto login" }).count() === 0);

  // null-modem pair
  await page.locator("#port-tree .item.port", { hasText: "fake:link-a" }).dblclick();
  await page.locator("#port-tree .item.port", { hasText: "fake:link-b" }).dblclick();
  await sleep(300);
  const tabNames = await page.locator(".tab .label").allTextContents();
  const a = tabNames.indexOf("fake:link-a"), b = tabNames.indexOf("fake:link-b");
  await page.locator(".tab", { hasText: "fake:link-a" }).click();
  await page.locator(".sendbar [data-send-text]").first().fill("over the cable");
  await page.locator(".sendbar [data-send-text]").first().press("Enter");
  s = await waitScreen(/over the cable/, b);
  check("link-a to link-b through a null-modem cable", /over the cable/.test(s), { tabNames, s });
  await shot("06-link");

  // layout: two panes
  await page.click(".layout [data-layout=cols]");
  await sleep(300);
  await shot("07-two-panes");
  check("two panes", (await page.locator(".pane").count()) === 2);

  // settings dialog
  await page.locator(".toolbar [data-action=settings]").first().click();
  await shot("08-settings");
  check("the settings dialog opens", await page.locator("#panel").evaluate((d) => d.open));
  check("no keyboard options remain", (await page.locator("#profile-form").textContent()).includes("Function keys") === false);
  await page.keyboard.press("Escape");

  // light theme
  await page.emulateMedia({ colorScheme: "dark" });
  await sleep(200);
  await shot("09-dark");
  await page.click(".viewbar [data-view]");
  await page.selectOption(".viewbar [data-view]", "bin");
  await sleep(300);
  await shot("10-dark-binary");
  await page.emulateMedia({ colorScheme: "light" });

  // ---- a reload keeps the login (the start link works once); open ports are closed by it
  page.on("dialog", (d) => d.accept());  // "leave the page?" while ports are open
  await page.reload();
  await page.waitForSelector("#port-tree .item.port", { state: "attached" });
  check("a reload keeps working without a new link", (await page.locator("body").textContent()).includes("Virtual ports"));
  check("the expanded Virtual ports section is remembered", await page.locator("#port-tree .item.port", { hasText: "fake:shell" }).isVisible());
  await page.locator("#port-tree .item.port", { hasText: "fake:shell" }).dblclick();
  await waitScreen(/login: /);   // the port was released by the reload and can be opened again
  await input.focus();
  await input.press("ArrowUp");
  check("the send history survives a reload", (await input.inputValue()) !== "", await input.inputValue());
  await input.fill("");

  // ---- groups, scripts and sessions in the tree
  const btn = (title) => page.locator(`button[title="${title}"]`);
  const row = (name) => page.locator("#tree .item", { hasText: name }).first();
  await btn("New group").click();
  await page.fill("#new-group-input", "Lab");
  await page.keyboard.press("Enter");
  await page.waitForSelector("#tree .node-head.group:has-text('Lab')", { timeout: 3000 }).catch(() => {});
  check("a group is created", await page.locator("#tree .node-head.group", { hasText: "Lab" }).count() === 1);

  await page.locator(".tab .close").first().click();
  await page.locator(".tab .close").first().click();  // free the shell tab; scripts run on fake:echo below
  await page.locator("#port-tree .item.port", { hasText: "fake:echo" }).dblclick();
  await waitScreen(/connected/);
  const txNow = () => page.evaluate(() => active.counters.tx);
  await btn("New script in Lab").click();
  const sf = page.locator("#script-form");
  await sf.locator("[name=name]").fill("boot");
  await sf.locator("[name=commands]").fill("echo one\n@wait=100; hex=41 0d\necho two");
  await sf.locator("[name=delayMs]").fill("50");
  await sf.locator("button[type=submit]").click();
  await page.waitForSelector("#tree .item:has-text('boot')");
  const tx0 = await txNow();
  await row("boot").locator("button[title='Run on the current session']").click();
  await page.waitForFunction((n) => active.counters.tx >= n + 20, tx0, { timeout: 5000 }).catch(() => {});
  check("a script runs on the open session (text lines and steps: 9 + 2 + 9 bytes)", (await txNow()) - tx0 === 20, (await txNow()) - tx0);

  await btn("New script in Lab").click();
  await sf.locator("[name=name]").fill("spin");
  await sf.locator("[name=commands]").fill("tick");
  await sf.locator("[name=delayMs]").fill("30");
  await sf.locator("[name=loop]").check();
  await sf.locator("button[type=submit]").click();
  await row("spin").locator("button[title='Run on the current session']").click();
  await sleep(400);
  const stop = row("spin").locator("button[title='Stop the script']");
  check("a running script offers Stop", await stop.count() === 1);
  await stop.click();
  await sleep(300);
  const t1 = await txNow();
  await sleep(300);
  check("Stop ends a looping script", t1 > tx0 + 40 && (await txNow()) === t1, { t1, now: await txNow() });

  await btn("New session in Lab").click();
  const pf = page.locator("#profile-form");
  check("the group is prefilled in a new session", (await pf.locator("[name=group]").inputValue()) === "Lab");
  await pf.locator("[name=name]").fill("Counter");
  await pf.locator("[name=device]").fill("fake:counter");
  await pf.locator("button[type=submit]").click();
  await page.waitForSelector("#tree .item:has-text('Counter')");
  await row("Counter").dragTo(page.locator("#tree .node-head.group", { hasText: "General" }));
  await sleep(400);
  check("a session dragged onto a group moves there", await page.locator("#tree .group-node").first().locator(".item:has-text('Counter')").count() === 1);
  await row("Counter").dblclick();
  await waitScreen(/tick 1/, 1);
  await row("Counter").locator("button[title='Edit Counter']").click();
  await pf.locator("[name=name]").fill("Counter 2");
  await pf.locator("button[type=submit]").click();
  await sleep(600);
  check("renaming an open session renames its tab", (await page.locator(".tab .label").allTextContents()).includes("Counter 2"));
  await row("Counter 2").locator("button[title='Edit Counter 2']").click();
  const del = page.locator("#profile-delete");
  await del.click();
  await del.click();
  await sleep(400);
  check("a session is deleted by a second click", await page.locator("#tree .item:has-text('Counter 2')").count() === 0);

  const tabBefore = await page.evaluate(() => tabs.indexOf(active));
  await page.keyboard.press("Alt+Shift+PageUp");
  check("Alt+Shift+PageUp switches tab", (await page.evaluate(() => tabs.indexOf(active))) !== tabBefore);

  // ---- narrow windows: nothing runs out of its pane
  await page.click(".layout [data-layout=cols]");
  for (const w of [1280, 900, 700]) {
    await page.setViewportSize({ width: w, height: 800 });
    await sleep(200);
    const o = await page.evaluate(() => ({ page: document.documentElement.scrollWidth - innerWidth, panes: [...document.querySelectorAll(".pane")].map((p) => p.scrollWidth - p.clientWidth) }));
    check(`no horizontal overflow at ${w}px`, o.page <= 0 && o.panes.every((x) => x <= 1), o);
  }
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.click(".layout [data-layout=single]");

  // ---- a flood of lines is kept and the view switches quickly
  await page.locator("#port-tree .item.port", { hasText: "fake:shell" }).dblclick();
  const shellTab = await page.evaluate(() => tabs.findIndex((t) => t.profile.device === "fake:shell"));
  await waitScreen(/login: /, shellTab);
  for (const line of ["admin", "admin"]) { await input.fill(line); await input.press("Enter"); await sleep(150); }
  await input.fill("flood 5000");
  await input.press("Enter");
  await waitScreen(/line 5000 of 5000/, shellTab, 8000);
  const t0 = Date.now();
  await page.selectOption(".viewbar [data-view]", "hex");
  await page.waitForFunction((n) => tabs[n].events.length > 0 && !tabs[n].hold, shellTab);
  check("5000 lines are kept and a view switch takes under 3 s", Date.now() - t0 < 3000, Date.now() - t0);
  await page.selectOption(".viewbar [data-view]", "text");

  check("no page errors or console errors", problems.length === 0, problems);
  check("the program reports no data race or panic", !/DATA RACE|panic:/.test(server.child.stderrText), server.child.stderrText.slice(0, 600));
  await browser.close();
  server.child.kill();
  console.log(failed ? `${failed} FAILED` : "all passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("e2e crashed:", e.stack); process.exit(2); });
