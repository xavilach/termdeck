const fs = require("fs");
const vm = require("vm");
const { JSDOM } = require("jsdom");
const web = require("path").join(__dirname, "..", "..", "web") + "/";
const html = fs.readFileSync(web + "index.html", "utf8").replace(/<script[^>]*><\/script>/g, "").replace(/<link[^>]*>/g, "");
const dom = new JSDOM(html, { url: "http://127.0.0.1:1234/#token=abc", runScripts: "outside-only", pretendToBeVisual: true });
const w = dom.window;
const errors = [];
w.addEventListener("error", (e) => errors.push("window error: " + e.message));
process.on("unhandledRejection", (e) => errors.push("unhandled rejection: " + (e && e.stack || e)));

// ---- stubs
w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event("close")); };
w.matchMedia = () => ({ matches: false, addEventListener() {} });
w.ResizeObserver = class { observe() {} };
w.AudioContext = class {}; w.TextEncoder = TextEncoder; w.TextDecoder = TextDecoder; w.btoa = w.btoa || ((s) => Buffer.from(s, "latin1").toString("base64"));
w.Audio = class { play() { return Promise.resolve(); } };
Object.defineProperty(w.navigator, "clipboard", { value: { writeText: () => Promise.resolve(), readText: () => Promise.resolve("") } });
w.Terminal = class {
  constructor(o) { this.options = { ...o }; this.out = []; this.rows = 24; this.modes = {}; this.parser = { registerEscHandler() {} }; this.buffer = { active: { length: 0, getLine: () => null } }; this.handlers = {}; }
  scrollToBottom() { this.bottomed = (this.bottomed || 0) + 1; } loadAddon() {} open() {} focus() {} dispose() {} clear() { this.out.push("<clear>"); } reset() { this.out.length = 0; this.out.push("<reset>"); }
  onBell() {} onTitleChange() {} onSelectionChange() {} onData() {} hasSelection() { return false; } getSelection() { return ""; }
  attachCustomKeyEventHandler(f) { this.keyHandler = f; } select() {} scrollToLine() {} clearSelection() {}
  write(d, cb) { if (typeof d !== "string" && d) d = Buffer.from(d).toString("latin1"); if (d) this.out.push(d); if (cb) cb(); }
  text() { return this.out.join("").replace(/\x1b\[90m|\x1b\[39m|\x1b\[0m/g, ""); }
};
w.FitAddon = { FitAddon: class { fit() {} } };
const sockets = [];
w.WebSocket = class {
  constructor(url, protocols) { this.url = url; this.sent = []; this.readyState = 0; this.bufferedAmount = 0; sockets.push(this); setTimeout(() => { this.readyState = 1; this.onopen && this.onopen(); }, 0); }
  send(d) { this.sent.push(JSON.parse(d)); } close() { this.readyState = 3; }
};
w.WebSocket.OPEN = 1;
let portList = ["/dev/ttyUSB0"];
const profile = { name: "p", device: "fake:echo", baud: 115200, dataBits: 8, parity: "none", stopBits: "1", flow: "none", enter: "lf", group: "", localEcho: true, rxMarker: "", txMarker: "TX ", view: "text", newLine: "terminal" };
w.fetch = async (url, opt) => {
  const body = { "/api/login": { secret: "s" }, "/api/profiles": { profiles: [profile] }, "/api/groups": { groups: ["Lab"] }, "/api/scripts": { scripts: [{ name: "boot", group: "", commands: ["a"], delayMs: 100, loop: false, count: 0 }] }, "/api/ports": portList, "/api/portinfo": { "/dev/ttyUSB0": { name: "FTDI FT232R", id: "0403:6001", info: "FTDI FT232R USB UART · 0403:6001 · A50285BI" } }, "/api/virtual": [{ name: "fake:echo", what: "echo" }], "/api/ping": {} }[url] ?? {};
  return { ok: true, json: async () => body };
};

const ctx = dom.getInternalVMContext();
const run = (code, name) => new vm.Script(code, { filename: name }).runInContext(ctx);
// the page's own scripts, in the order of index.html
const scripts = [...fs.readFileSync(web + "index.html", "utf8").matchAll(/<script src="\/([^"]+)"/g)].map((m) => m[1]).filter((f) => !f.startsWith("vendor/"));
for (const file of scripts) run(fs.readFileSync(web + file, "utf8"), file);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
// The server draws the screen: what it sends is written to the terminal as it is.
const frame = (raw) => { const b = Buffer.from(raw, "latin1"); const u = new w.Uint8Array(b.length); u.set(b); return u.buffer; };
const json = (ws, o) => ws.onmessage({ data: JSON.stringify(o) });
const bin = (ws, raw) => ws.onmessage({ data: frame(raw) });
const lastOf = (ws, type) => ws.sent.filter((m) => m.type === type).pop();
// the session answers the hello of a tab with its state
const attach = (ws, state = "closed") => json(ws, { type: "session", state, logging: false, script: "" });
let failed = 0;
const check = (name, ok, extra) => { if (!ok) failed++; console.log((ok ? "ok   " : "FAIL ") + name + (ok ? "" : "  " + JSON.stringify(extra))); };
const q = (s) => w.document.querySelector(s);
const fire = (el, type) => el.dispatchEvent(new w.Event(type, { bubbles: true }));
const tab = () => run("tabs[0]", "x");

(async () => {
  await sleep(50);
  check("startup did not replace the page", !!q("#panes .pane"), w.document.body.textContent.slice(0, 100));
  run('openTab(profiles[0])', "x");
  await sleep(10);
  const ws = sockets[0];
  check("a tab attaches to its session with an id, its settings and its view", ws && /\/api\/terminal\?id=[0-9a-f]{24}$/.test(ws.url) && ws.sent[0]?.type === "hello" && ws.sent[0].profile.name === "p" && ws.sent[0].view.mode === "text", ws && ws.sent);
  attach(ws);
  check("a new session is asked to open the port", ws.sent[1]?.type === "open" && ws.sent[1].profile.device === "fake:echo", ws.sent);
  json(ws, { type: "state", state: "connected", message: "" });
  const t = tab();
  bin(ws, "hello\r\nworld\r\n");
  check("what the server draws shows", t.term.text().includes("hello\r\nworld"), t.term.text());
  json(ws, { type: "counters", rx: 14, tx: 3 });
  check("counters shown", q("[data-count=rx]").textContent === "14" && q("[data-count=tx]").textContent === "3");

  // the view: the server draws everything again
  const view = q("[data-view]");
  view.value = "hex"; fire(view, "change");
  check("a view change asks the server to draw again", lastOf(ws, "view")?.view.mode === "hex", lastOf(ws, "view"));
  check("changed marker + reset button", q("[data-view-reset]").hidden === false && view.closest("label").classList.contains("changed"));
  json(ws, { type: "reset" });
  check("a redraw starts from an empty screen", t.term.out[0] === "<reset>" && t.quiet === true);
  bin(ws, "68 65 6c 6c 6f");
  json(ws, { type: "replayed" });
  check("and shows what the server drew", t.term.text().includes("68 65 6c") && t.quiet === false, t.term.text());
  view.value = "text"; fire(view, "change");
  check("marker clears", q("[data-view-reset]").hidden === true && lastOf(ws, "view").view.mode === "text");

  // new line at, time, control characters
  const nl = q("[data-nl]"); const custom = q("[data-nl-custom]");
  nl.value = "custom"; fire(nl, "change");
  check("custom field shows", custom.hidden === false);
  custom.value = "ld"; fire(custom, "input");
  await sleep(320);
  check("custom sequence sent while typing", lastOf(ws, "view").view.nl === "custom" && lastOf(ws, "view").view.nlCustom === "ld", lastOf(ws, "view"));
  nl.value = "terminal"; fire(nl, "change");
  const stamps = q("[data-stamps]"); stamps.checked = true; fire(stamps, "change");
  check("timestamps", lastOf(ws, "view").view.stamps === true);
  const mode = q("[data-time-mode]"); mode.value = "delta"; fire(mode, "change");
  check("choosing what the time shows turns it on and marks the change", stamps.checked && lastOf(ws, "view").view.timeMode === "delta" && q("[data-view-reset]").hidden === false);
  mode.value = "clock"; fire(mode, "change");
  stamps.checked = false; fire(stamps, "change");
  const ctrl = q("[data-ctrl]"); ctrl.checked = true; fire(ctrl, "change");
  check("control chars", lastOf(ws, "view").view.ctrl === true);
  ctrl.checked = false; fire(ctrl, "change");
  const flt = q("[data-filter]"); flt.value = "!tick"; fire(flt, "input");
  await sleep(320);
  check("the filter goes to the server and marks the change", lastOf(ws, "view").view.filter === "!tick" && flt.closest("label").classList.contains("changed"));
  flt.value = ""; fire(flt, "input");
  await sleep(320);

  // send bar: text
  const input = q("[data-send-text]"); const send = q("[data-send]");
  const typ = q("[data-send-type]");
  input.value = "AT\\r"; send.click();
  const sent1 = ws.sent.filter((m) => m.type === "input");
  check("text send uses End (session Enter = LF) and unescapes", sent1.length === 1 && sent1[0].data === "AT\r\n", sent1);
  check("input cleared after send", input.value === "");
  // End selector
  const end = q("[data-send-end]"); end.value = "none"; fire(end, "change");
  input.value = "x"; send.click();
  check("End none sends nothing extra", lastOf(ws, "input").data === "x");
  end.value = "custom"; fire(end, "change"); const ec = q("[data-send-end-custom]"); ec.value = "\\x03;"; fire(ec, "input");
  input.value = "y"; send.click();
  check("custom End", lastOf(ws, "input").data === "y\x03;", lastOf(ws, "input"));
  // hex type
  typ.value = "hex"; fire(typ, "change");
  input.value = "41 42"; end.value = "crlf"; fire(end, "change"); send.click();
  const raw = lastOf(ws, "raw");
  check("hex send raw with CRLF", raw && Buffer.from(raw.bytes, "base64").toString("hex") === "41420d0a", raw);
  // the history keeps type and data, and survives a reload (localStorage)
  check("sent data is remembered with its type", run("tabs[0].sent.length", "x") >= 3 && run("tabs[0].sent.some((e) => e.type === 'hex' && e.text === '41 42')", "x"));
  check("and stored for the session", run("JSON.parse(localStorage.getItem('termdeck.history.p')).length", "x") >= 3);
  input.value = ""; typ.value = "text"; fire(typ, "change");
  input.dispatchEvent(new w.KeyboardEvent("keydown", { key: "ArrowUp", bubbles: true }));
  check("Up recalls the last entry with its type", typ.value === "hex" && input.value === "41 42", { type: typ.value, value: input.value });
  input.value = ""; typ.value = "text"; fire(typ, "change");
  // type conversion
  typ.value = "text"; fire(typ, "change"); input.value = "Hi\\r"; typ.value = "hex"; fire(typ, "change");
  check("text -> hex converts", input.value === "48 69 0D", input.value);
  typ.value = "dec"; fire(typ, "change"); check("hex -> dec", input.value === "72 105 13", input.value);
  typ.value = "bin"; fire(typ, "change"); check("dec -> bin", input.value === "01001000 01101001 00001101", input.value);
  // invalid char filtering
  const be = new w.InputEvent("beforeinput", { data: "2", inputType: "insertText", cancelable: true, bubbles: true });
  input.dispatchEvent(be); check("binary refuses 2", be.defaultPrevented);
  // auto send
  typ.value = "text"; fire(typ, "change"); input.value = "ping"; end.value = "none"; fire(end, "change");
  const before = ws.sent.filter((m) => m.type === "input").length;
  q("[data-auto-ms]").value = "10"; const auto = q("[data-auto]"); auto.checked = true; fire(auto, "change");
  await sleep(80);
  const after = ws.sent.filter((m) => m.type === "input").length;
  check("auto send repeats and keeps input", after - before >= 3 && input.value === "ping", { before, after });
  auto.checked = false; fire(auto, "change"); await sleep(40);
  const stopped = ws.sent.filter((m) => m.type === "input").length; await sleep(40);
  check("auto send stops", ws.sent.filter((m) => m.type === "input").length === stopped);
  // disconnect stops auto + disables bar
  auto.checked = true; fire(auto, "change");
  json(ws, { type: "state", state: "ended", message: "gone" });
  check("bar disabled when not connected, auto off", input.disabled && !auto.checked);
  json(ws, { type: "state", state: "connected", message: "" });

  // main output is read-only
  const sentBefore = ws.sent.length;
  const keyRes = t.term.keyHandler({ type: "keydown", key: "a", code: "KeyA", ctrlKey: false, shiftKey: false });
  check("typing in the output sends nothing and is swallowed", keyRes === false && ws.sent.length === sentBefore);

  // file send
  const file = new w.File([new Uint8Array(600).fill(65)], "f.bin");
  await run("sendFile(tabs[0].pane, null)", "x");
  await (async () => { const f = file; Object.defineProperty(f, "arrayBuffer", { value: async () => new Uint8Array(600).fill(65).buffer }); w.__f = f; return run("sendFile(tabs[0].pane, window.__f)", "x"); })();
  const raws = ws.sent.filter((m) => m.type === "raw");
  check("file sent in chunks", raws.length >= 3 && raws.slice(-3).reduce((n, m) => n + Buffer.from(m.bytes, "base64").length, 0) === 600, raws.length);
  check("and noted with the records of the tab", ws.sent.some((m) => m.type === "note" && /f\.bin sent/.test(m.data)));

  // find
  t.term.buffer.active = { length: 2, getLine: (i) => ({ translateToString: () => ["abc Hello", "hello again"][i] }) };
  q("[data-action=find]").click(); const f = q("[data-find]"); f.value = "hello"; fire(f, "input");
  check("find counts matches (case-insens.)", q("[data-find-count]").textContent === "2 found", q("[data-find-count]").textContent);

  // settings saved re-applies the view and tells the session
  run("applyProfile(tabs[0], {...tabs[0].profile, view: 'bin', linePauseMs: 5})", "x");
  check("applyProfile resets the view to the profile", q("[data-view]").value === "bin" && q("[data-pause]").value === "5");
  check("and gives the session the settings and the view", lastOf(ws, "profile")?.profile.view === "bin" && lastOf(ws, "view").view.mode === "bin");

  // freeze, copy
  const freeze = q("[data-freeze]");
  freeze.click();
  bin(ws, "while frozen\r\n");
  check("freeze keeps the screen as it is and counts what comes", !t.term.text().includes("while frozen") && /Resume \(\d+ B new\)/.test(freeze.textContent), freeze.textContent);
  const views = ws.sent.filter((m) => m.type === "view").length;
  freeze.click();
  check("resume asks the server to draw everything again", ws.sent.filter((m) => m.type === "view").length === views + 1 && freeze.textContent === "Freeze");
  let copied = null; w.navigator.clipboard.writeText = (x) => { copied = x; return Promise.resolve(); };
  t.term.buffer.active = { length: 2, getLine: (i) => ({ translateToString: () => ["one", "two"][i], isWrapped: false }) };
  q("[data-copy-all]").click();
  check("Copy copies the whole output", copied === "one\ntwo", copied);
  // toolbar buttons and shortcuts
  const act = (a) => q(`[data-action=${a}]`).click();
  act("break"); check("break button", ws.sent.pop().type === "break");
  act("log"); check("log button", ws.sent.pop().type === "log");
  act("line-dtr"); const dtr = ws.sent.pop(); check("DTR button toggles", dtr.type === "line" && dtr.line === "dtr" && dtr.value === false, dtr);
  act("font-up"); check("font up", t.fontSize === 15 && t.term.options.fontSize === 15);
  act("clear"); check("clear asks the session to forget its records", ws.sent.pop().type === "clear");
  act("reset"); check("reset button", t.term.out[0] === "<reset>" || t.term.text() === "");
  act("port"); check("port button closes when connected", ws.sent.pop().type === "close");
  w.document.dispatchEvent(new w.KeyboardEvent("keydown", { code: "KeyS", altKey: true, shiftKey: true, bubbles: true }));
  check("Alt+Shift+S changes the layout", q("#panes").className.includes("cols"), q("#panes").className);
  t.term.keyHandler({ type: "keydown", key: "L", code: "KeyL", ctrlKey: true, shiftKey: true });
  check("Ctrl+Shift+L clears", ws.sent.pop().type === "clear");
  // two sessions on the same port are refused
  run("openTab({...profiles[0], name: 'other'})", "x");
  check("same port is not opened twice", run("tabs.length", "x") === 1);

  check("a USB port shows what it is under its name", /FTDI FT232R USB UART · 0403:6001/.test([...w.document.querySelectorAll("#port-tree .item.port")].map((r) => r.textContent).join("|")));

  // the port list follows plugging in and pulling out
  run("portPollMs = 30; pollPorts()", "x");
  portList = ["/dev/ttyUSB0", "/dev/ttyUSB1"];
  await sleep(150);
  check("a new port shows up by itself", [...w.document.querySelectorAll("#port-tree .item.port")].some((r) => r.textContent === "/dev/ttyUSB1"));
  portList = ["/dev/ttyUSB1"];
  await sleep(150);
  check("and a port that was pulled out goes", ![...w.document.querySelectorAll("#port-tree .item.port")].some((r) => r.textContent === "/dev/ttyUSB0"));
  const rowsBefore = w.document.querySelector("#port-tree .item.port");
  await sleep(100);
  check("an unchanged list is not drawn again", w.document.querySelector("#port-tree .item.port") === rowsBefore);
  run("portPollMs = 600000", "x");

  // a message while the connection is still opening is dropped, not thrown (found by the monkey test)
  const sentOk = run("openTab({ ...profiles[0], name: 'opening', device: 'fake:binary' }); sendMessage(tabs[tabs.length - 1], { type: 'mark' })", "x");
  check("a message to a connection that is still opening is not sent and does not throw", sentOk === false && errors.length === 0, errors);
  run("closeTab(tabs[tabs.length - 1])", "x");

  // closing a tab ends its session, its auto send and its file job
  typ.value = "text"; fire(typ, "change"); input.value = "tick"; end.value = "none"; fire(end, "change");
  json(ws, { type: "state", state: "connected", message: "" });
  q("[data-auto-ms]").value = "10"; auto.checked = true; fire(auto, "change");
  const job = { stop: false }; t.job = job;
  run("closeTab(tabs[0])", "x");
  const n0 = ws.sent.filter((m) => m.type === "input").length; await sleep(60);
  check("closing a tab stops auto send and a file job, and ends the session", ws.sent.filter((m) => m.type === "input").length === n0 && job.stop === true && run("tabs.length", "x") === 0 && ws.sent.some((m) => m.type === "end"));

  // style snapshot for refactoring the style sheet: SNAPSHOT=file node smoke.js
  if (process.env.SNAPSHOT) {
    q("#tree .item").click();
    run("openTab({...profiles[0], name: 'second', device: 'fake:shell'})", "x");
    await sleep(10);
    const rows = {};
    const css = fs.readFileSync(web + "app.css", "utf8");
    const style = w.document.createElement("style"); style.textContent = css; w.document.head.append(style);
    const dump = (label) => {
      for (const [i, node] of [...w.document.querySelectorAll("body *")].entries()) {
        const cs = w.getComputedStyle(node);
        rows[label + ":" + i + ":" + node.tagName + "." + node.className] = Array.from({ length: cs.length }, (_, n) => cs[n] + ":" + cs.getPropertyValue(cs[n])).join(";");
      }
    };
    dump("lines");
    run("editProfile(null)", "x"); dump("profile-dialog");
    run("showLines()", "x");
    fs.writeFileSync(process.env.SNAPSHOT, JSON.stringify(rows, null, 1));
  }
  run("openTab({...profiles[0], name: 'main', device: 'COM61'})", "x");
  await sleep(10);
  const mainWs = run("tabs[0].ws", "x");
  attach(mainWs, "connected");
  check("a session that is already open is not opened again", !mainWs.sent.some((m) => m.type === "open") && run("tabs[0].state", "x") === "connected");

  // graph: the server sends the numbers it found in the lines
  run("applyProfile(tabs[0], { ...tabs[0].profile, graphSeries: 'Temp | Temperature: | degC\\nTick | tick ' })", "x");
  json(mainWs, { type: "points", t: 5000, values: [{ name: "Temp", value: 26.1 }] });
  json(mainWs, { type: "points", t: 5100, values: [{ name: "Tick", value: 7 }, { name: "Temp", value: 26.3 }] });
  const gs = run("[...tabs[0].graph.series].map(([n, p]) => n + ':' + p.map((x) => x.v).join(','))", "x");
  check("points from the server are kept per series", JSON.stringify(gs) === JSON.stringify(["Temp:26.1,26.3", "Tick:7"]), gs);
  q("[data-graph]").click();
  check("the Graph button opens the graph with its legend", !q(".graphbar").hidden && q("[data-graph]").getAttribute("aria-pressed") === "true" && /Temp: 26.3/.test(q(".graph-legend").textContent), q(".graph-legend").textContent);
  q("[data-graph-clear]").click();
  check("Clear graph forgets the points", run("tabs[0].graph.series.size", "x") === 0);
  q("[data-graph]").click();
  check("and the button hides it again", q(".graphbar").hidden);

  // "All tabs": the same data goes to every connected tab
  run("openTab({ ...profiles[0], name: 'all one', device: 'COM81' }); openTab({ ...profiles[0], name: 'all two', device: 'COM82' })", "x");
  const allOne = run("tabs.find((t) => t.profile.name === 'all one')", "x"), allTwo = run("tabs.find((t) => t.profile.name === 'all two')", "x");
  await sleep(10);  // the sockets of the stub are open a moment after they are made
  json(allOne.ws, { type: "state", state: "connected", message: "" }); json(allTwo.ws, { type: "state", state: "connected", message: "" });
  run("activate(tabs.find((t) => t.profile.name === 'all one'))", "x");
  allOne.ws.sent.length = 0; allTwo.ws.sent.length = 0;
  const allBar = (selector) => allOne.pane.sendbar.querySelector(selector);  // the pane may not be the first one
  allBar("[data-send-text]").value = "to all"; allBar("[data-all]").checked = true; allBar("[data-send]").click();
  check("with All tabs the data goes to every connected tab", allOne.ws.sent.some((m) => m.type === "input" && m.data.startsWith("to all")) && allTwo.ws.sent.some((m) => m.type === "input" && m.data.startsWith("to all")));
  allOne.ws.sent.length = 0; allTwo.ws.sent.length = 0;
  allBar("[data-all]").checked = false; allBar("[data-send-text]").value = "only one"; allBar("[data-send]").click();
  check("and without it only to the shown one", allOne.ws.sent.some((m) => m.type === "input") && !allTwo.ws.sent.some((m) => m.type === "input"));
  run("closeTab(tabs.find((t) => t.profile.name === 'all one')); closeTab(tabs.find((t) => t.profile.name === 'all two'))", "x");

  // notifications for lines that match a notify rule, only while the tab is not in sight
  const sent = [];
  w.Notification = class { constructor(title, options) { sent.push({ title, body: options.body }); } static get permission() { return "granted"; } };
  run("openTab({ ...profiles[0], name: 'noisy', device: 'COM71', highlights: 'FAULT = red+notify' }); openTab({ ...profiles[0], name: 'quiet', device: 'COM72' })", "x");
  const noisy = run("tabs.find((t) => t.profile.name === 'noisy')", "x");
  await sleep(10);
  run("activate(tabs.find((t) => t.profile.name === 'noisy'))", "x");
  json(noisy.ws, { type: "notify", text: "a FAULT while shown" });
  check("no notification for a tab that is in sight", sent.length === 0, sent);
  run("activate(tabs.find((t) => t.profile.name === 'quiet'))", "x");
  json(noisy.ws, { type: "notify", text: "a FAULT in the background" });
  json(noisy.ws, { type: "notify", text: "another FAULT at once" });
  check("a notify rule gives one notification for a tab that is not in sight (not one per line)", sent.length === 1 && sent[0].title === "noisy" && /FAULT in the background/.test(sent[0].body), sent);
  run("closeTab(tabs.find((t) => t.profile.name === 'noisy')); closeTab(tabs.find((t) => t.profile.name === 'quiet'))", "x");

  // a check is added after hex data
  const checkWs = run("tabs[0].ws", "x"); json(checkWs, { type: "state", state: "connected", message: "" });
  q("[data-send-type]").value = "hex"; fire(q("[data-send-type]"), "change");
  q("[data-send-check]").value = "xor8"; fire(q("[data-send-check]"), "change");
  const fromCheck = checkWs.sent.length;
  q("[data-send-text]").value = "01 02 04"; q("[data-send]").click();
  const checked = checkWs.sent.slice(fromCheck).find((m) => m.type === "raw");
  check("a check is added after hex data (XOR of 01 02 04 is 07)", checked && Buffer.from(checked.bytes, "base64").toString("hex") === "010204070a", checked);  // the data, its check, then the End (LF)
  q("[data-send-check]").value = "none"; fire(q("[data-send-check]"), "change");
  q("[data-send-type]").value = "text"; fire(q("[data-send-type]"), "change");

  // sending jumps to the bottom of the output
  const bottomTab = run("tabs[0]", "x"); const bottomBefore = bottomTab.term.bottomed || 0;
  q("[data-send-text]").value = "go"; q("[data-send]").click();
  check("a send scrolls the output to the bottom", (bottomTab.term.bottomed || 0) === bottomBefore + 1, bottomTab.term.bottomed);

  // a tab that is not shown gets an activity mark when data arrives, and the title counts them
  run("openTab({ ...profiles[0], name: 'bg one', device: 'COM91' }); openTab({ ...profiles[0], name: 'bg two', device: 'COM92' })", "x");
  const bgTwo = run("tabs.find((t) => t.profile.name === 'bg two')", "x");
  run("activate(tabs.find((t) => t.profile.name === 'bg one'))", "x");
  bin(bgTwo.ws, "background data");
  check("data in a tab that is not shown marks it and the page title", bgTwo.tabEl.classList.contains("activity") && w.document.title === "(1) termdeck", w.document.title);
  run("activate(tabs.find((t) => t.profile.name === 'bg two'))", "x");
  check("showing the tab clears the mark", !bgTwo.tabEl.classList.contains("activity") && w.document.title === "termdeck", w.document.title);
  run("closeTab(tabs.find((t) => t.profile.name === 'bg two')); closeTab(tabs.find((t) => t.profile.name === 'bg one'))", "x");

  // marks
  mainWs.sent.length = 0;
  q("[data-mark]").click();
  check("Mark asks the session for a mark (it numbers it, shows it and logs it)", mainWs.sent.some((m) => m.type === "mark"), mainWs.sent);
  const mt = run("tabs[0].term", "x");
  mt.buffer.active = { length: 100, viewportY: 50, getLine: (i) => ({ translateToString: () => (i === 10 || i === 90 ? "[termdeck] mark 1 at 10:00:00" : "x") }) };
  let scrolled = null; mt.scrollToLine = (n) => { scrolled = n; };
  q("[data-mark-next]").click(); const afterNext = scrolled;
  q("[data-mark-prev]").click(); const afterPrev = scrolled;
  check("the arrows go to the next and the previous mark", afterNext === 90 && afterPrev === 10, { afterNext, afterPrev });

  // quick sends: buttons from the session settings
  run("applyProfile(tabs[0], { ...tabs[0].profile, quickSends: 'Hello | text | hi there\\n# note\\nRaw | hex | 41 42 | end=none\\nWiggle | cmd | dtr=0; wait=1' })", "x");
  const qws = mainWs;
  const quick = [...w.document.querySelectorAll(".quickbar button")];
  check("quick sends make buttons", quick.map((b) => b.textContent).join(",") === "Hello,Raw,Wiggle" && !q(".quickbar").hidden, quick.map((b) => b.textContent));
  json(qws, { type: "state", state: "connected", message: "" });
  const quickFrom = qws.sent.length;
  const live = [...w.document.querySelectorAll(".quickbar button")];  // drawn again when the state changed
  live[0].click(); live[1].click(); live[2].click();
  const sentNow = qws.sent.slice(quickFrom);
  check("a button sends its data: text with the session's Enter, hex as it is, steps", sentNow[0].type === "input" && sentNow[0].data === "hi there\n" && sentNow[1].type === "raw" && Buffer.from(sentNow[1].bytes, "base64").toString() === "AB" && sentNow[2].type === "steps" && sentNow[2].data === "dtr=0; wait=1", sentNow);
  json(qws, { type: "state", state: "ended", message: "" });
  check("the buttons are disabled when the port is closed", [...w.document.querySelectorAll(".quickbar button")].every((b) => b.disabled));
  json(qws, { type: "state", state: "connected", message: "" });
  run("applyProfile(tabs[0], { ...tabs[0].profile, quickSends: '' })", "x");
  check("no quick sends, no bar", q(".quickbar").hidden === true);

  // the state while the page waits for a port to come back
  json(qws, { type: "state", state: "reconnecting", message: "read error" });
  check("reconnecting: status and button", /Reconnecting/.test(q(".toolbar .status").textContent) && q("[data-action=port]").textContent === "Stop reconnecting" && run("tabs[0].tabEl.className", "x").includes("reconnecting"), q("[data-action=port]").textContent);
  qws.sent.length = 0; q("[data-action=port]").click();
  check("the button then stops it", qws.sent.pop().type === "close");
  json(qws, { type: "state", state: "connected", message: "" });

  // tabs come back after a reload, attached to their sessions
  run("openTab({ ...profiles[0], name: 'second tab', device: 'COM62' })", "x");
  await sleep(10);
  run("saveTabs()", "x");
  const saved = w.sessionStorage.getItem("termdeck.tabs");
  const names = run("tabs.map((t) => t.profile.name)", "x");
  const ids = run("tabs.map((t) => t.id)", "x");
  const socketsBefore = sockets.length;
  run("[...tabs].forEach(closeTab)", "x");
  check("no tab is left after closing them", run("tabs.length", "x") === 0);
  w.sessionStorage.setItem("termdeck.tabs", saved);
  run("restoreTabs()", "x");
  await sleep(20);
  check("restoreTabs brings the tabs back, in order", JSON.stringify(run("tabs.map((t) => t.profile.name)", "x")) === JSON.stringify(names), { names, now: run("tabs.map((t) => t.profile.name)", "x") });
  const again = sockets.slice(socketsBefore);
  check("each tab attaches again to its session, with its id", again.length === names.length && again.every((s, i) => s.url.endsWith(`id=${ids[i]}`) && s.sent[0]?.type === "hello"), again.map((s) => s.url));
  attach(again[0], "connected"); attach(again[1], "closed");
  check("a restored tab does not open a port by itself: the session says what it is doing", !again.some((s) => s.sent.some((m) => m.type === "open")) && run("tabs[0].state", "x") === "connected" && run("tabs[1].state", "x") === "closed");
  check("no errors", errors.length === 0, errors);
  console.log(failed ? `${failed} FAILED` : "all passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("harness crashed:", e.stack); process.exit(2); });
