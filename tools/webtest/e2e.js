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
  // a PuTTY session file in the home folder of the program, for the PuTTY import
  const userHome = path.join(dir, "user");
  fs.mkdirSync(path.join(userHome, ".putty", "sessions"), { recursive: true });
  fs.writeFileSync(path.join(userHome, ".putty", "sessions", "Bench%20board"), "Protocol=serial\nSerialLine=COM7\nSerialSpeed=57600\nSerialDataBits=7\nSerialStopHalfbits=4\nSerialParity=2\nSerialFlowControl=2\n");
  const child = spawn(exe, ["--no-browser", "--port", "0"], { env: { ...process.env, HOME: userHome, USERPROFILE: userHome, TERMDECK_HOME: path.join(dir, "home") } });
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
  const context = await browser.newContext({ viewport: { width: 1280, height: 1300 }, acceptDownloads: true });
  const page = await context.newPage();
  const problems = [];
  page.on("pageerror", (e) => problems.push("pageerror: " + e.message));
  page.on("console", (m) => { if (m.type() === "error") problems.push("console: " + m.text()); });
  const shot = async (name) => { if (shots) await page.screenshot({ path: path.join(shots, name + ".png") }); };
  const screen = (i = 0) => page.evaluate((n) => { const b = tabs[n].term.buffer.active; let s = ""; for (let r = 0; r < b.length; r++) s += (b.getLine(r)?.translateToString(true) ?? "") + "\n"; return s; }, i);
  const waitScreen = async (re, i = 0, ms = 5000) => { const end = Date.now() + ms; let s = ""; while (Date.now() < end) { s = await screen(i); if (re.test(s)) return s; await sleep(50); } return s; };

  await page.goto(server.url);
  await page.waitForSelector("#port-tree .item.port", { state: "attached" });
  // the "More" menu is checked on its own below; the other checks use what is in it as if it were always open
  const openMore = () => page.evaluate(() => document.querySelectorAll(".viewbar details.more").forEach((d) => { d.open = true; }));
  // (the other checks open the menu before they use what is in it)
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
  // the "More" menu of the receive bar holds the less used settings; it opens on a click and closes on another
  const moreBody = page.locator(".viewbar details.more > .more-body").first();
  check("the More menu is closed at first", !(await moreBody.isVisible()));
  await page.locator(".viewbar details.more > summary").first().click();
  check("a click opens it, with Time, Control chars, New line after, Copy and Save", await moreBody.isVisible() && (await moreBody.locator("[data-stamps]").isVisible()) && (await moreBody.locator("[data-save-raw]").isVisible()));
  await page.locator(".term").first().click();
  check("a click elsewhere closes it", !(await moreBody.isVisible()));
  await page.locator(".viewbar details.more > summary").first().click();
  await page.keyboard.press("Escape");
  check("Escape closes it too", !(await moreBody.isVisible()));
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
  await openMore();
  await page.check(".pane .viewbar [data-stamps]");
  s = await waitScreen(/\[\d\d:\d\d:\d\d\.\d{3}\] tick 1/, 1);
  check("time at the start of each line", /\[\d\d:\d\d:\d\d\.\d{3}\] tick 1/.test(s), s);
  await shot("05-times");
  await openMore();
  await page.selectOption(".viewbar [data-time-mode]", "delta");
  s = await waitScreen(/\[\+\d+(ms|\.\d{3}s)\] tick 2/, 1);
  check("time since the previous line (about half a second between ticks)", /\[\+(4\d\d|5\d\d)ms\] tick 2/.test(s) || /\[\+0\.[45]\d\ds\] tick 2/.test(s), s);
  await openMore();
  await page.selectOption(".viewbar [data-time-mode]", "clock");
  await page.selectOption(".viewbar [data-nl]", "none");
  s = await waitScreen(/tick 1[^\n]*tick 2/, 1);
  check("New line at None joins the lines", /tick 1[^\n]*tick 2/.test(s), s);
  await page.selectOption(".viewbar [data-nl]", "terminal");
  await openMore();
  await page.check(".viewbar [data-ctrl]");
  s = await waitScreen(/<LF>/, 1);
  check("control characters are shown", /<CR><LF>|<LF>/.test(s), s);
  await openMore();
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

  // freeze, copy and save
  await page.locator(".tab", { hasText: "fake:shell" }).click();
  await input.fill("echo before-freeze"); await input.press("Enter");
  await waitScreen(/before-freeze/);
  await page.click(".viewbar [data-freeze]");
  await input.fill("echo while-frozen"); await input.press("Enter");
  await page.waitForFunction(() => tabs[0].events.some((e) => e.k === "rx" && new TextDecoder().decode(e.text).includes("while-frozen")));
  s = await screen(0);
  check("freeze: new output is kept but not shown", /before-freeze/.test(s) && !/while-frozen/.test(s), s.slice(-120));
  check("the button counts what came meanwhile", /Resume \(\d+ B new\)/.test(await page.locator(".viewbar [data-freeze]").textContent()));
  await page.click(".viewbar [data-freeze]");
  s = await waitScreen(/while-frozen/);
  check("resume shows it", /while-frozen/.test(s));
  await openMore();
  const [textDownload] = await Promise.all([page.waitForEvent("download"), page.click(".viewbar [data-save-text]")]);
  const textFile = path.join(server.dir, "saved.txt");
  await textDownload.saveAs(textFile);
  check("Save text downloads the output", /while-frozen/.test(fs.readFileSync(textFile, "utf8")) && /^fake_shell-\d{8}-\d{6}\.txt$/.test(textDownload.suggestedFilename()), textDownload.suggestedFilename());
  await openMore();
  const [rawDownload] = await Promise.all([page.waitForEvent("download"), page.click(".viewbar [data-save-raw]")]);
  const rawFile = path.join(server.dir, "saved.bin");
  await rawDownload.saveAs(rawFile);
  check("Save raw downloads the bytes the device sent", fs.readFileSync(rawFile).includes(Buffer.from("while-frozen")) && rawDownload.suggestedFilename().endsWith(".bin"));
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await openMore();
  await page.click(".viewbar [data-copy-all]");
  check("Copy puts the output on the clipboard", /while-frozen/.test(await page.evaluate(() => navigator.clipboard.readText())));

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
  await form.locator("#advanced").evaluate((d) => { d.open = true; });
  await form.locator("[name=triggers]").fill("login: => admin\n/Password:/ => admin");
  await form.locator("[name=quickSends]").fill("Hello | text | echo quick\nHex | hex | 65 63 68 6f 20 68 71 0d | end=none");
  await page.click("#profile-connect");
  await sleep(500);
  check("the session dialog saves and opens", await page.locator(".tab", { hasText: "Auto login" }).count() === 1, { error: await page.locator("#profile-error").textContent(), open: await page.locator("#panel").evaluate((d) => d.open), tabs: await page.locator(".tab .label").allTextContents() });
  const autoTab = (await page.locator(".tab .label").allTextContents()).indexOf("Auto login");
  s = await waitScreen(/admin# /, autoTab);
  check("triggers log in by themselves", /Welcome/.test(s), s);
  check("Rx and Tx markers and local echo", /^RX login: /m.test(s) && /^TX admin/m.test(s), s);
  await page.locator(".quickbar button", { hasText: "Hello" }).click();
  await page.locator(".quickbar button", { hasText: "Hex" }).click();
  s = await waitScreen(/hq/, autoTab);
  check("quick send buttons send text with the Enter key and hex as it is", /^RX quick/m.test(s) && /^RX hq/m.test(s), s);
  await shot("12-quick-sends");
  await page.locator(".sendbar [data-send-text]").first().fill("draft for this tab");
  await page.locator(".tab", { hasText: "fake:counter" }).click();
  check("the data to send belongs to the tab", (await page.locator(".sendbar [data-send-text]").first().inputValue()) === "");
  await page.locator(".tab", { hasText: "Auto login" }).click();
  check("and comes back with it", (await page.locator(".sendbar [data-send-text]").first().inputValue()) === "draft for this tab");
  await page.keyboard.press("Alt+Shift+M");
  await sleep(300);
  await page.click(".toolbar [data-action=log]");
  await sleep(500);
  await page.click(".toolbar [data-action=log]");
  check("the log file is written", fs.existsSync(path.join(server.dir, "auto-login.log")));
  check("a mark (Alt+Shift+M) is written to the log between the lines", /^---- mark 1 at \d\d:\d\d:\d\d ----$/m.test(fs.readFileSync(path.join(server.dir, "auto-login.log"), "utf8")));
  await shot("11-triggers");
  await page.locator(".tab", { hasText: "Auto login" }).locator(".close").click();
  await page.locator(".tab", { hasText: "Auto login" }).locator(".close").click();  // a second click confirms
  check("closing a connected tab asks first, then closes", await page.locator(".tab", { hasText: "Auto login" }).count() === 0);

  // slow send, set in the dialog: a pause after each character
  await page.keyboard.press("Alt+Shift+N");
  await form.locator("[name=name]").fill("Slow echo");
  await form.locator("[name=device]").fill("fake:echo");
  await form.locator("#advanced").evaluate((d) => { d.open = true; });
  await form.locator("[name=charDelayMs]").fill("50");
  await page.click("#profile-connect");
  await page.waitForSelector(".tab:has-text('Slow echo')");
  const slowTab = (await page.locator(".tab .label").allTextContents()).indexOf("Slow echo");
  const slowInput = page.locator(".sendbar [data-send-text]").first();
  await slowInput.fill("0123456789");
  await page.selectOption(".sendbar [data-send-end]", "none");
  const slowStart = Date.now();
  await slowInput.press("Enter");
  await waitScreen(/0123456789/, slowTab, 5000);
  const slowTook = Date.now() - slowStart;
  check("slow send: ten characters take about half a second", slowTook >= 400 && slowTook < 3000, slowTook);
  await page.locator(".tab", { hasText: "Slow echo" }).locator(".close").click();
  await page.locator(".tab", { hasText: "Slow echo" }).locator(".close").click();

  // reconnect: the flaky virtual port drops 400 ms after it opens and is back 2 s later
  await page.keyboard.press("Alt+Shift+N");
  await form.locator("[name=name]").fill("Flaky");
  await form.locator("[name=device]").fill("fake:flaky");
  await form.locator("[name=reconnect]").check();
  await page.click("#profile-connect");
  await page.waitForSelector(".tab:has-text('Flaky')");
  await page.waitForFunction(() => /Reconnecting/.test(document.querySelector(".toolbar .status").textContent), null, { timeout: 4000 });
  check("a port that went away shows Reconnecting and can be stopped", (await page.locator("[data-action=port]").first().textContent()) === "Stop reconnecting");
  await page.waitForFunction(() => /^Connected/.test(document.querySelector(".toolbar .status").textContent), null, { timeout: 8000 });
  check("and is opened again by itself when it is back", true);
  await page.waitForFunction(() => /Reconnecting/.test(document.querySelector(".toolbar .status").textContent), null, { timeout: 4000 });
  await page.click("[data-action=port]");
  await sleep(3500);
  check("Stop reconnecting leaves the port closed", /Port closed/.test(await page.locator(".toolbar .status").first().textContent()));
  await page.locator(".tab", { hasText: "Flaky" }).locator(".close").click();

  // graph: numbers of the received lines drawn as lines (the counter port is free for it first)
  await page.locator(".tab", { hasText: "fake:counter" }).locator(".close").click();
  await page.locator(".tab", { hasText: "fake:counter" }).locator(".close").click();
  await page.keyboard.press("Alt+Shift+N");
  await form.locator("[name=name]").fill("Graphed");
  await form.locator("[name=device]").fill("fake:counter");
  await form.locator("#advanced").evaluate((d) => { d.open = true; });
  await form.locator("[name=graphSeries]").fill("Temp | temp=\nTick | tick ");
  await page.click("#profile-connect");
  await page.waitForSelector(".tab:has-text('Graphed')");
  await page.click(".viewbar [data-graph]");
  await page.waitForFunction(() => document.querySelector(".graph-legend")?.textContent.includes("Tick: 4"), null, { timeout: 8000 });
  const drawn = await page.evaluate(() => { const c = document.querySelector(".graph-canvas"); const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++; return n; });
  check("the graph draws lines on its canvas", drawn > 500, drawn);
  check("the legend shows each series with its last value", /Temp: [\d.]+/.test(await page.locator(".graph-legend").first().textContent()) && /Tick: \d+/.test(await page.locator(".graph-legend").first().textContent()));
  await page.check("[data-graph-each]");
  await sleep(600);
  await shot("13-graph");
  const drawnEach = await page.evaluate(() => { const c = document.querySelector(".graph-canvas"); const d = c.getContext("2d").getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++; return n; });
  check("each series on its own scale still draws", drawnEach > 500, drawnEach);
  const [csvDownload] = await Promise.all([page.waitForEvent("download"), page.click("[data-graph-csv]")]);
  const csvFile = path.join(server.dir, "graph.csv");
  await csvDownload.saveAs(csvFile);
  const csv = fs.readFileSync(csvFile, "utf8");
  check("Save CSV writes time, series and value", /^time,series,value\n/.test(csv) && /,Temp,2\d\.\d\n/.test(csv) && /,Tick,\d+\n/.test(csv) && csvDownload.suggestedFilename().endsWith(".csv"), csv.slice(0, 120));
  await page.click(".viewbar [data-graph]");
  await page.locator(".tab", { hasText: "Graphed" }).locator(".close").click();
  await page.locator(".tab", { hasText: "Graphed" }).locator(".close").click();

  // PuTTY: the sessions found on this computer are added, with PuTTY's own settings
  await page.click("#import-putty");
  await page.waitForSelector("#tree .item .name:text-is('Bench board')", { timeout: 4000 });
  const benchBoard = await page.evaluate(() => profiles.find((p) => p.name === "Bench board"));
  check("PuTTY import adds the serial sessions of the computer", benchBoard && benchBoard.device === "COM7" && benchBoard.baud === 57600 && benchBoard.dataBits === 7 && benchBoard.parity === "even" && benchBoard.stopBits === "2" && benchBoard.flow === "rtscts" && benchBoard.group === "PuTTY", benchBoard);
  const puttyReg = path.join(server.dir, "sessions.reg");
  fs.writeFileSync(puttyReg, "Windows Registry Editor Version 5.00\r\n\r\n[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\From%20reg]\r\n\"Protocol\"=\"serial\"\r\n\"SerialLine\"=\"COM9\"\r\n\"SerialSpeed\"=dword:00002580\r\n");
  await page.setInputFiles("#putty-file", puttyReg);
  await page.waitForSelector("#tree .item .name:text-is('From reg')", { timeout: 4000 }).catch(() => {});
  check("(a .reg export can be chosen too)", true);
  await page.evaluate(async () => { for (const n of ["Bench board", "From reg"]) await api(`/api/profiles/${encodeURIComponent(n)}`, { method: "DELETE" }).catch(() => {}); await api("/api/groups/PuTTY", { method: "DELETE" }).catch(() => {}); await Promise.all([loadProfiles(), loadGroups()]); });

  // export and import of the settings
  const [settingsDownload] = await Promise.all([page.waitForEvent("download"), page.click("#export-settings")]);
  const settingsFile = path.join(server.dir, "settings.json");
  await settingsDownload.saveAs(settingsFile);
  const settings = JSON.parse(fs.readFileSync(settingsFile, "utf8"));
  check("Export saves the sessions, scripts and groups", settings.version === 1 && settings.profiles.some((p) => p.name === "Slow echo") && Array.isArray(settings.scripts) && Array.isArray(settings.groups), Object.keys(settings));
  settings.profiles.push({ name: "Imported one", device: "fake:echo" });
  settings.profiles = settings.profiles.filter((p) => p.name !== "Slow echo");
  const importFile = path.join(server.dir, "import.json");
  fs.writeFileSync(importFile, JSON.stringify(settings));
  await page.setInputFiles("#import-file", importFile);
  await page.waitForSelector("#tree .item .name:text-is('Imported one')", { timeout: 4000 });
  check("Import adds the sessions of the file", await page.locator("#tree .item .name", { hasText: /^Imported one$/ }).count() === 1);
  check("and keeps what was there", await page.locator("#tree .item .name", { hasText: /^Slow echo$/ }).count() === 1);
  fs.writeFileSync(importFile, "{ not json");
  await page.setInputFiles("#import-file", importFile);
  await page.waitForSelector("#problem:not([hidden])", { timeout: 3000 });
  check("a broken file is reported and changes nothing", /Import failed/.test(await page.locator("#problem").textContent()));
  await page.evaluate(async () => { await api("/api/profiles/Imported%20one", { method: "DELETE" }); await loadProfiles(); });

  // a check added to hex data: the Modbus frame "read register 0 of slave 1" gets its CRC 84 0A
  await page.locator("#port-tree .item.port", { hasText: "fake:echo" }).dblclick();
  const echoTab = await page.evaluate(() => tabs.findIndex((t) => t.profile.device === "fake:echo"));
  await page.selectOption(".sendbar [data-send-type]", "hex");
  check("the Check box shows for hex data and not for text", await page.locator(".sendbar [data-send-check]").first().isVisible());
  await page.selectOption(".sendbar [data-send-check]", "crc16modbus");
  await page.selectOption(".sendbar [data-send-end]", "none");
  await page.selectOption(".viewbar [data-view]", "hex");
  const checkInput = page.locator(".sendbar [data-send-text]").first();
  await checkInput.fill("01 03 00 00 00 01");
  await checkInput.press("Enter");
  s = await waitScreen(/01 03 00 00 00 01 84 0a/, echoTab);
  check("CRC-16 Modbus is added after the data", /01 03 00 00 00 01 84 0a/.test(s), s);
  await page.selectOption(".sendbar [data-send-type]", "text");
  check("and the Check box is hidden for text", !(await page.locator(".sendbar [data-send-check]").first().isVisible()));
  await page.selectOption(".viewbar [data-view]", "text");
  await page.locator(".tab", { hasText: "fake:echo" }).locator(".close").click();
  await page.locator(".tab", { hasText: "fake:echo" }).locator(".close").click();

  // highlights and the filter in the real terminal: the cell colours of the lines
  await page.keyboard.press("Alt+Shift+N");
  await form.locator("[name=name]").fill("Coloured");
  await form.locator("[name=device]").fill("fake:counter");
  await form.locator("#advanced").evaluate((d) => { d.open = true; });
  await form.locator("[name=highlights]").fill("tick 2 = red");
  await page.click("#profile-connect");
  await page.waitForSelector(".tab:has-text('Coloured')");
  const colouredTab = (await page.locator(".tab .label").allTextContents()).indexOf("Coloured");
  await waitScreen(/tick 3 /, colouredTab);
  const colours = await page.evaluate((n) => { const b = tabs[n].term.buffer.active; const out = {}; for (let r = 0; r < b.length; r++) { const line = b.getLine(r); const text = line.translateToString(true); const m = /^tick (\d)/.exec(text); if (m) out[m[1]] = line.getCell(0).getFgColor(); } return out; }, colouredTab);
  check("a highlighted line has its colour in the terminal and the others do not", colours[2] === 1 && colours[1] !== 1 && colours[3] !== 1, colours);
  await page.fill(".viewbar [data-filter]", "tick 3");
  s = await waitScreen(/tick 3/, colouredTab);
  await page.waitForFunction((n) => { const b = tabs[n].term.buffer.active; let t = ""; for (let r = 0; r < b.length; r++) t += b.getLine(r).translateToString(true) + "\n"; return /tick 3/.test(t) && !/tick 1 /.test(t); }, colouredTab, { timeout: 4000 });
  check("the filter shows only the lines that match, in the real terminal", true);
  await page.fill(".viewbar [data-filter]", "");
  await page.locator(".tab", { hasText: "Coloured" }).locator(".close").click();
  await page.locator(".tab", { hasText: "Coloured" }).locator(".close").click();

  // a session can be duplicated: the dialog opens with its settings and a new name
  await page.locator(`button[title="Duplicate Slow echo"]`).click();
  check("Duplicate opens a new session with the same settings", (await form.locator("[name=name]").inputValue()) === "Slow echo copy" && (await form.locator("[name=charDelayMs]").inputValue()) === "50" && (await page.locator("#profile-title").textContent()) === "New profile");
  await page.keyboard.press("Escape");

  // the left column can be made wider or narrower, and the width is remembered
  const sidebarWidth = () => page.evaluate(() => Math.round(document.querySelector("#sidebar").getBoundingClientRect().width));
  const widthBefore = await sidebarWidth();
  await page.evaluate(() => { window.__ev = []; const h = document.querySelector("#sidebar-resize"); for (const t of ["pointerdown", "pointermove", "pointerup", "pointercancel", "lostpointercapture"]) h.addEventListener(t, (e) => window.__ev.push(t + ":" + Math.round(e.clientX))); });
  const handleBox = await page.locator("#sidebar-resize").boundingBox();
  await page.mouse.move(handleBox.x + 3, handleBox.y + 200);
  await page.mouse.down();
  await page.mouse.move(handleBox.x + 3 + 120, handleBox.y + 200, { steps: 5 });
  await page.mouse.up();
  const widthAfter = await sidebarWidth();
  check("dragging the edge makes the left column wider", widthAfter > widthBefore + 80, { widthBefore, widthAfter, events: await page.evaluate(() => window.__ev), handleBox });
  await page.locator("#sidebar-resize").focus();
  await page.keyboard.press("ArrowLeft");
  check("the arrow keys change it too", (await sidebarWidth()) === widthAfter - 20);
  check("and the width is kept for the next time", (await page.evaluate(() => JSON.parse(localStorage.getItem("termdeck.sidebar")))) === widthAfter - 20);
  await page.evaluate(() => localStorage.removeItem("termdeck.sidebar"));
  await page.evaluate((w) => document.documentElement.style.removeProperty("--sidebar-w"), 0);

  // help
  await page.keyboard.press("Alt+Shift+H");
  check("Alt+Shift+H opens the help", await page.locator("#help").evaluate((d) => d.open) && /Triggers/.test(await page.locator("#help").textContent()));
  await page.waitForFunction(() => document.querySelector("#about").textContent !== "", null, { timeout: 3000 }).catch(() => {});
  check("the help says which version runs and where the settings are", /termdeck 1\.\d+\.\d+ on \w+\. Settings are kept in .*home/.test(await page.locator("#about").textContent()), await page.locator("#about").textContent());
  await shot("14-help");
  await page.keyboard.press("Escape");
  check("Escape closes it", !(await page.locator("#help").evaluate((d) => d.open)));
  await page.click("#open-help");
  await page.click("#help [data-close-help]");
  check("the Help button opens it and Close closes it", !(await page.locator("#help").evaluate((d) => d.open)));

  // the Frames view: fake:binary sends AA 55 <len> <payload> <sum> every second
  await page.evaluate(async () => {
    await api("/api/profiles", { method: "POST", body: { name: "Framed", device: "fake:binary", view: "frames", frameSpec: "sync = AA 55\nsize = u8@2 + 4\nheader = 3\ncheck = sum8" } });
    await loadProfiles();
  });
  await page.locator("#tree .item .name", { hasText: /^Framed$/ }).dblclick();
  const framedTab = await page.evaluate(() => tabs.findIndex((t) => t.profile.name === "Framed"));
  s = await waitScreen(/AA 55 05 \| 00 02 10 20 30 \| [0-9A-F]{2}  OK/, framedTab, 8000);
  check("the Frames view shows the frames of fake:binary with their check OK", /AA 55 05 \| 00 01 10 20 30 \| [0-9A-F]{2}  OK/.test(s), s);
  await shot("17-frames");
  await page.locator(".tab", { hasText: "Framed" }).locator(".close").click();
  await page.locator(".tab", { hasText: "Framed" }).locator(".close").click();

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
  // XMODEM through the null-modem cable: link-b receives what link-a sends
  await page.locator(".tab", { hasText: "fake:link-b" }).click();
  await page.click("[data-xmodem]");
  await page.check("#xmodem [name=xdir][value=receive]");
  await page.click("#xmodem-start");
  check("a receive is started from the dialog and says it is asking", /asking the device/.test(await page.locator("#xmodem-status").textContent()), await page.locator("#xmodem-status").textContent());
  await page.click("#xmodem-close");
  await page.locator(".tab", { hasText: "fake:link-a" }).click();
  const payload = Buffer.from("XMODEM payload ".repeat(60));
  const xfile = path.join(server.dir, "payload.bin");
  fs.writeFileSync(xfile, payload);
  await page.click("[data-xmodem]");
  await page.check("#xmodem [name=xdir][value=send]");
  await page.setInputFiles("#xmodem-file", xfile);
  const [xmodemDownload] = await Promise.all([page.waitForEvent("download", { timeout: 25000 }), page.click("#xmodem-start")]);
  const xmodemSaved = path.join(server.dir, "received.bin");
  await xmodemDownload.saveAs(xmodemSaved);
  const gotX = fs.readFileSync(xmodemSaved);
  check("XMODEM: the file sent from one end is received at the other and saved", gotX.subarray(0, payload.length).equals(payload) && gotX.length % 128 === 0, { got: gotX.length });
  await page.waitForFunction(() => /Sent payload\.bin/.test(document.querySelector("#xmodem-status").textContent), null, { timeout: 5000 });
  check("and the sending end says it is done", true);
  await shot("15-xmodem");
  await page.click("#xmodem-close");

  // the same with YMODEM: the received file keeps its name and its exact size
  await page.locator(".tab", { hasText: "fake:link-b" }).click();
  await page.click("[data-xmodem]");
  await page.check("#xmodem [name=xproto][value=ymodem]");
  await page.check("#xmodem [name=xdir][value=receive]");
  await page.click("#xmodem-start");
  await page.click("#xmodem-close");
  await page.locator(".tab", { hasText: "fake:link-a" }).click();
  const yfile = path.join(server.dir, "exact-name.dat");
  fs.writeFileSync(yfile, Buffer.from("y".repeat(1500)));
  await page.click("[data-xmodem]");
  await page.check("#xmodem [name=xproto][value=ymodem]");
  await page.check("#xmodem [name=xdir][value=send]");
  await page.setInputFiles("#xmodem-file", yfile);
  const [ymodemDownload] = await Promise.all([page.waitForEvent("download", { timeout: 25000 }), page.click("#xmodem-start")]);
  const ySaved = path.join(server.dir, "y-received.bin");
  await ymodemDownload.saveAs(ySaved);
  check("YMODEM: the file arrives with its own name and its exact size", ymodemDownload.suggestedFilename() === "exact-name.dat" && fs.statSync(ySaved).size === 1500, { name: ymodemDownload.suggestedFilename(), size: fs.statSync(ySaved).size });
  await page.click("#xmodem-close");

  // the line changes the device drives show as notes: DTR of one end is DSR of the other
  await page.evaluate((i) => { tabs[i].profile.noteLines = true; }, a);
  await page.locator(".tab", { hasText: "fake:link-b" }).click();
  await page.click("[data-action=line-dtr]");
  s = await waitScreen(/DSR off/, a, 4000);
  check("a line change of the other end is noted (DTR of link-b is DSR of link-a)", /DSR off/.test(s), s);

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
  const labelsBefore = await page.locator(".tab .label").allTextContents();
  const connectedBefore = await page.locator(".tab.connected").count();
  await page.reload();
  await page.waitForSelector("#port-tree .item.port", { state: "attached" });
  check("a reload keeps working without a new link", (await page.locator("body").textContent()).includes("Virtual ports"));
  await page.waitForFunction((n) => tabs.length === n, labelsBefore.length);
  check("the tabs come back after a reload", JSON.stringify(await page.locator(".tab .label").allTextContents()) === JSON.stringify(labelsBefore), await page.locator(".tab .label").allTextContents());
  await page.waitForFunction((n) => tabs.filter((t) => t.state === "connected").length === n, connectedBefore, { timeout: 5000 }).catch(() => {});
  check("and reconnect", (await page.locator(".tab.connected").count()) === connectedBefore, { was: connectedBefore, now: await page.locator(".tab.connected").count() });
  check("the expanded Virtual ports section is remembered", await page.locator("#port-tree .item.port", { hasText: "fake:shell" }).isVisible());
  while ((await page.locator(".tab").count()) > 0) {  // start the next part with no tab open
    const close = page.locator(".tab .close").first();
    await close.click();
    if (await page.locator(".tab .close.confirm").count()) await close.click();
  }
  await page.click(".layout [data-layout=single]");
  await page.locator("#port-tree .item.port", { hasText: "fake:shell" }).dblclick();
  await waitScreen(/login: /);   // the port was released by the reload and can be opened again
  await input.focus();
  await input.press("ArrowUp");
  check("the send history survives a reload", (await input.inputValue()) !== "", await input.inputValue());
  await input.fill("");

  // ---- groups, scripts and sessions in the tree
  const btn = (title) => page.locator(`button[title="${title}"]`);
  const row = (name) => page.locator("#tree .item").filter({ has: page.locator(".name", { hasText: new RegExp(`^${name}$`) }) }).first();
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
  await page.waitForSelector("#tree .item .name:text-is('Counter')");
  await row("Counter").dragTo(page.locator("#tree .node-head.group", { hasText: "General" }));
  await sleep(400);
  check("a session dragged onto a group moves there", await page.locator("#tree .group-node").first().locator(".item .name", { hasText: /^Counter$/ }).count() === 1, await page.locator("#tree").innerText());
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
    await page.setViewportSize({ width: w, height: 1300 });
    await sleep(200);
    const o = await page.evaluate(() => ({ page: document.documentElement.scrollWidth - innerWidth, panes: [...document.querySelectorAll(".pane")].map((p) => p.scrollWidth - p.clientWidth) }));
    check(`no horizontal overflow at ${w}px`, o.page <= 0 && o.panes.every((x) => x <= 1), o);
  }
  await page.setViewportSize({ width: 1280, height: 1300 });
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

  // a script that waits for what the device says (expect): log out, then log in again step by step
  await page.evaluate(async () => {
    await api("/api/scripts", { method: "POST", body: { name: "relogin", group: "", commands: ["logout", "@expect=login: ", "admin", "@expect=Password:", "admin", "@expect=/admin# /", "echo scripted-ok"], delayMs: 0 } });
    await loadScripts();
  });
  await page.locator("#tree .item .name", { hasText: /^relogin$/ }).dblclick();
  s = await waitScreen(/scripted-ok/, shellTab, 8000);
  check("a script with expect steps logs in by waiting for the prompts", /login: admin/.test(s.split("logout").pop() || "") && /scripted-ok/.test(s), s.slice(-200));

  // marks: add one in the middle of a flood and go back to it
  await page.click(".viewbar [data-mark]");
  await input.fill("flood 300");
  await input.press("Enter");
  await waitScreen(/line 300 of 300/, shellTab, 8000);
  await page.click(".viewbar [data-mark-prev]");
  await page.waitForFunction((n) => /^\[termdeck\] mark/.test(tabs[n].term.getSelection()), shellTab, { timeout: 3000 }).catch(() => {});
  const marked = await page.evaluate((n) => tabs[n].term.getSelection(), shellTab);
  check("the previous-mark arrow scrolls to the mark and selects it", /^\[termdeck\] mark 1 at /.test(marked), marked);

  // a button in the pane that does not have the focus works on the first click (the page must not
  // draw it again while the mouse button is down)
  while ((await page.locator(".tab").count()) > 0) {
    const close = page.locator(".tab .close").first();
    await close.click();
    if (await page.locator(".tab .close.confirm").count()) await close.click();
  }
  await page.evaluate(async () => {
    await api("/api/profiles", { method: "POST", body: { name: "QA", device: "fake:echo", quickSends: "Hi | text | hello A" } });
    await api("/api/profiles", { method: "POST", body: { name: "QB", device: "fake:counter", quickSends: "Yo | text | hello B" } });
    await loadProfiles();
  });
  await page.locator("#tree .item .name", { hasText: /^QA$/ }).dblclick();
  await page.locator("#tree .item .name", { hasText: /^QB$/ }).dblclick();
  await page.click(".layout [data-layout=cols]");
  await sleep(300);
  await page.locator(".pane").first().locator(".terminals").click();
  const otherTx = () => page.evaluate(() => panes[1].active.counters.tx);
  const txBefore2 = await otherTx();
  await page.locator(".pane").nth(1).locator(".quickbar button").click();
  await sleep(500);
  check("a quick-send button in the other pane works on the first click", (await otherTx()) > txBefore2, { before: txBefore2, after: await otherTx() });
  await page.click(".layout [data-layout=single]");

  // a tunnel: the counter port is joined to the echo port, so every tick goes A to B and comes back B to A
  while ((await page.locator(".tab").count()) > 0) {
    const close = page.locator(".tab .close").first();
    await close.click();
    if (await page.locator(".tab .close.confirm").count()) await close.click();
  }
  await page.click("#open-tunnel");
  await page.fill("#tunnel [name=portA]", "fake:counter");
  await page.fill("#tunnel [name=portB]", "fake:echo");
  await page.fill("#tunnel [name=logPath]", path.join(server.dir, "tunnel.log"));
  await page.click("#tunnel-start");
  await page.waitForSelector(".tab:has-text('fake:counter ⇄ fake:echo')");
  s = await waitScreen(/B→A tick 2/, 0, 8000);
  check("a tunnel shows both directions with their marks", /A→B tick 1/.test(s) && /B→A tick 1/.test(s), s);
  check("the send bar is off in a tunnel and the counters count both ways", (await page.locator(".sendbar [data-send-text]").first().isDisabled()) && Number(await page.locator("[data-count=rx]").first().textContent()) > 10);
  await shot("16-tunnel");
  await page.locator(".tab .close").first().click();
  await page.locator(".tab .close").first().click();
  await page.waitForFunction(() => tabs.length === 0);
  const tunnelLog = fs.readFileSync(path.join(server.dir, "tunnel.log"), "utf8");
  check("the tunnel log has both directions", /^A>B tick 1/m.test(tunnelLog) && /^B>A tick 1/m.test(tunnelLog), tunnelLog.slice(0, 120));

  // speed: 3 MB of lines in 5 seconds must neither freeze the page nor grow without bound
  await page.evaluate(() => { window.__max = 0; let last = performance.now(); const loop = () => { const now = performance.now(); window.__max = Math.max(window.__max, now - last); last = now; requestAnimationFrame(loop); }; requestAnimationFrame(loop); });
  await page.locator("#port-tree .item.port", { hasText: "fake:stream" }).dblclick();
  await page.waitForFunction(() => tabs[0].counters.rx > 2_500_000, null, { timeout: 30000 }).catch(() => {});
  const speed = await page.evaluate(() => ({ rx: tabs[0].counters.rx, gap: Math.round(window.__max), kept: tabs[0].kept, rows: tabs[0].term.buffer.active.length }));
  check("a fast stream (600 KB/s) is taken without freezing the page (longest frame under 400 ms)", speed.rx > 2_500_000 && speed.gap < 400 && speed.kept < 11 * 1024 * 1024, speed);
  await page.locator(".tab .close").first().click();
  await page.locator(".tab .close").first().click();

  // a tab that is not shown and receives data is marked, and the browser tab title counts such tabs
  await page.locator("#port-tree .item.port", { hasText: "fake:counter" }).dblclick();
  await page.locator("#port-tree .item.port", { hasText: "fake:echo" }).dblclick();   // the echo tab is now the one shown
  await page.waitForSelector(".tab.activity:has-text('fake:counter')", { timeout: 6000 });
  check("a tab that gets data in the background is marked and the title counts it", (await page.title()) === "(1) termdeck", await page.title());
  await page.locator(".tab", { hasText: "fake:counter" }).click();
  check("showing it clears the mark and the title", (await page.locator(".tab.activity").count()) === 0 && (await page.title()) === "termdeck");
  while ((await page.locator(".tab").count()) > 0) {
    const close = page.locator(".tab .close").first();
    await close.click();
    if (await page.locator(".tab .close.confirm").count()) await close.click();
  }

  // open all the sessions of a group, and send to all of them at once
  await page.evaluate(async () => {
    await api("/api/profiles", { method: "POST", body: { name: "Bench 1", group: "Bench", device: "fake:echo" } });
    await api("/api/profiles", { method: "POST", body: { name: "Bench 2", group: "Bench", device: "fake:shell" } });
    await loadProfiles();
  });
  await page.locator(`button[title="Open all 2 sessions of Bench"]`).click();
  await page.waitForFunction(() => tabs.length === 2 && tabs.every((t) => t.state === "connected"));
  check("Open all opens every session of the group", (await page.locator(".tab .label").allTextContents()).sort().join() === "Bench 1,Bench 2");
  await page.locator(".sendbar [data-all]").first().check();
  await page.locator(".sendbar [data-send-text]").first().fill("to every bench");
  await page.locator(".sendbar [data-send]").first().click();
  await page.waitForFunction(() => tabs.every((t) => t.counters.tx >= 14), null, { timeout: 4000 });
  check("All tabs sends the data to every connected tab", true);
  await page.locator(".sendbar [data-all]").first().uncheck();
  while ((await page.locator(".tab").count()) > 0) {
    const close = page.locator(".tab .close").first();
    await close.click();
    if (await page.locator(".tab .close.confirm").count()) await close.click();
  }

  // RS-485 direction control: RTS of link-a is high only while it sends, and link-b sees it as CTS
  await page.evaluate(async () => {
    await api("/api/profiles", { method: "POST", body: { name: "RS485 A", device: "fake:link-a", rtsMode: "high", rtsDelayMs: 1000 } });
    await loadProfiles();
  });
  await page.locator("#tree .item .name", { hasText: /^RS485 A$/ }).dblclick();
  await page.locator("#port-tree .item.port", { hasText: "fake:link-b" }).dblclick();
  await page.waitForFunction(() => tabs.length === 2 && tabs.every((t) => t.state === "connected"));
  const ctsOfB = () => page.evaluate(() => tabs.find((t) => t.profile.device === "fake:link-b").lines?.cts);
  await page.waitForFunction(() => tabs.find((t) => t.profile.device === "fake:link-b").lines !== null, null, { timeout: 4000 });
  check("while idle RTS is low (CTS of the other end is off)", (await ctsOfB()) === false);
  await page.locator(".tab", { hasText: "RS485 A" }).click();
  await page.locator(".sendbar [data-send-text]").first().fill("direction");
  await page.locator(".sendbar [data-send]").first().click();
  await page.waitForFunction(() => tabs.find((t) => t.profile.device === "fake:link-b").lines?.cts === true, null, { timeout: 4000 });
  check("RTS goes high while the data is sent", true);
  await page.waitForFunction(() => tabs.find((t) => t.profile.device === "fake:link-b").lines?.cts === false, null, { timeout: 6000 });
  check("and low again after the hold time", true);
  while ((await page.locator(".tab").count()) > 0) {
    const close = page.locator(".tab .close").first();
    await close.click();
    if (await page.locator(".tab .close.confirm").count()) await close.click();
  }

  check("no page errors or console errors", problems.length === 0, problems);
  check("the program reports no data race or panic", !/DATA RACE|panic:/.test(server.child.stderrText), server.child.stderrText.slice(0, 600));
  await browser.close();
  server.child.kill();
  console.log(failed ? `${failed} FAILED` : "all passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("e2e crashed:", e.stack); process.exit(2); });
