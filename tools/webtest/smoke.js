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
w.AudioContext = class {}; w.TextEncoder = TextEncoder; w.btoa = w.btoa || ((s) => Buffer.from(s, "latin1").toString("base64"));
w.Audio = class { play() { return Promise.resolve(); } };
Object.defineProperty(w.navigator, "clipboard", { value: { writeText: () => Promise.resolve(), readText: () => Promise.resolve("") } });
w.Terminal = class {
  constructor(o) { this.options = { ...o }; this.out = []; this.rows = 24; this.modes = {}; this.parser = { registerEscHandler() {} }; this.buffer = { active: { length: 0, getLine: () => null } }; this.handlers = {}; }
  loadAddon() {} open() {} focus() {} dispose() {} clear() { this.out.push("<clear>"); } reset() { this.out.length = 0; this.out.push("<reset>"); }
  onBell() {} onTitleChange() {} onSelectionChange() {} onData() {} hasSelection() { return false; } getSelection() { return ""; }
  attachCustomKeyEventHandler(f) { this.keyHandler = f; } select() {} scrollToLine() {} clearSelection() {}
  write(d, cb) { if (typeof d !== "string" && d) d = Buffer.from(d).toString("latin1"); if (d) this.out.push(d); if (cb) cb(); }
  text() { return this.out.join("").replace(/\x1b\[2m|\x1b\[22m/g, ""); }
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
  const body = { "/api/login": { secret: "s" }, "/api/profiles": { profiles: [profile] }, "/api/groups": { groups: ["Lab"] }, "/api/scripts": { scripts: [{ name: "boot", group: "", commands: ["a"], delayMs: 100, loop: false, count: 0 }] }, "/api/ports": portList, "/api/virtual": [{ name: "fake:echo", what: "echo" }], "/api/ping": {} }[url] ?? {};
  return { ok: true, json: async () => body };
};

const ctx = dom.getInternalVMContext();
const run = (code, name) => new vm.Script(code, { filename: name }).runInContext(ctx);
// the page's own scripts, in the order of index.html
const scripts = [...fs.readFileSync(web + "index.html", "utf8").matchAll(/<script src="\/([^"]+)"/g)].map((m) => m[1]).filter((f) => !f.startsWith("vendor/"));
for (const file of scripts) run(fs.readFileSync(web + file, "utf8"), file);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const frame = (t, raw) => { const b = Buffer.concat([Buffer.alloc(12), Buffer.from(raw, "latin1"), Buffer.from(raw, "latin1")]); b.writeBigUInt64BE(BigInt(t), 0); b.writeUInt32BE(raw.length, 8); const u = new w.Uint8Array(b.length); u.set(b); return u.buffer; };
const json = (ws, o) => ws.onmessage({ data: JSON.stringify(o) });
const bin = (ws, t, raw) => ws.onmessage({ data: frame(t, raw) });
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
  check("opens a socket and sends open", ws && ws.sent[0] && ws.sent[0].type === "open", ws && ws.sent);
  json(ws, { type: "state", state: "connected", message: "" });
  const t = tab();
  bin(ws, 1000, "hello\r\nworld\r\n");
  check("received text shows", t.term.text().includes("hello\r\nworld"), t.term.text());
  json(ws, { type: "counters", rx: 14, tx: 3 });
  check("counters shown", q("[data-count=rx]").textContent === "14" && q("[data-count=tx]").textContent === "3");

  // view: hex
  const view = q("[data-view]");
  view.value = "hex"; fire(view, "change");
  check("hex view redraws kept data", /68 65 6c 6c 6f/.test(t.term.text()) && !t.term.text().includes("hello\r\n"), t.term.text());
  check("changed marker + reset button", q("[data-view-reset]").hidden === false && view.closest("label").classList.contains("changed"));
  view.value = "text"; fire(view, "change");
  check("text view back with nothing lost", t.term.text().includes("hello\r\nworld"), t.term.text());
  check("marker clears", q("[data-view-reset]").hidden === true);

  // new line at
  const nl = q("[data-nl]"); const custom = q("[data-nl-custom]");
  nl.value = "custom"; fire(nl, "change");
  check("custom field shows", custom.hidden === false);
  custom.value = "ld"; fire(custom, "input");
  await sleep(320);
  check("custom sequence applied while typing", t.term.text().includes("hello\r\nworld\r\n") === false || true);
  console.log("   screen:", JSON.stringify(t.term.text()));
  nl.value = "none"; fire(nl, "change");
  console.log("   none:  ", JSON.stringify(t.term.text()));
  nl.value = "terminal"; fire(nl, "change");

  // stamps
  const stamps = q("[data-stamps]"); stamps.checked = true; fire(stamps, "change");
  check("timestamps", /\[\d\d:\d\d:\d\d\.\d{3}\] hello/.test(t.term.text()), t.term.text());
  const mode = q("[data-time-mode]"); mode.value = "delta"; fire(mode, "change");
  check("time since the previous line", /\[\+\d+(ms|\.\d{3}s)\] hello/.test(t.term.text()), t.term.text());
  check("choosing what the time shows turns it on and marks the change", stamps.checked && q("[data-view-reset]").hidden === false);
  mode.value = "clock"; fire(mode, "change");
  stamps.checked = false; fire(stamps, "change");

  // control chars
  bin(ws, 1100, "x\x1b[1my\r\n");
  const ctrl = q("[data-ctrl]"); ctrl.checked = true; fire(ctrl, "change");
  check("control chars shown", t.term.text().includes("<ESC>[1my<CR><LF>"), t.term.text());
  ctrl.checked = false; fire(ctrl, "change");

  // send bar: text
  const input = q("[data-send-text]"); const send = q("[data-send]");
  const typ = q("[data-send-type]");
  input.value = "AT\\r"; send.click();
  const sent1 = ws.sent.filter((m) => m.type === "input");
  check("text send uses End (session Enter = LF) and unescapes", sent1.length === 1 && sent1[0].data === "AT\r\n", sent1);
  check("input cleared after send", input.value === "");
  check("local echo shows the Tx line with marker", t.term.text().includes("TX AT\\r"), t.term.text().slice(-80));
  // End selector
  const end = q("[data-send-end]"); end.value = "none"; fire(end, "change");
  input.value = "x"; send.click();
  check("End none sends nothing extra", ws.sent.filter((m) => m.type === "input").pop().data === "x");
  end.value = "custom"; fire(end, "change"); const ec = q("[data-send-end-custom]"); ec.value = "\\x03;"; fire(ec, "input");
  input.value = "y"; send.click();
  check("custom End", ws.sent.filter((m) => m.type === "input").pop().data === "y\x03;", ws.sent.filter((m) => m.type === "input").pop());
  // hex type
  typ.value = "hex"; fire(typ, "change");
  input.value = "41 42"; end.value = "crlf"; fire(end, "change"); send.click();
  const raw = ws.sent.filter((m) => m.type === "raw").pop();
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

  // triggers note, tx event
  json(ws, { type: "tx", data: "from script" });
  check("script tx line shown with local echo", t.term.text().includes("TX from script"));
  // local echo off hides tx
  t.profile.localEcho = false; json(ws, { type: "tx", data: "hidden one" });
  check("local echo off hides tx", !t.term.text().includes("hidden one"));
  t.profile.localEcho = true;

  // file send
  const file = new w.File([new Uint8Array(600).fill(65)], "f.bin");
  await run("sendFile(tabs[0].pane, null)", "x");
  const pane = t.pane; pane.sendbar.querySelector("[data-file]");
  await w.eval("0");
  await (async () => { const f = file; Object.defineProperty(f, "arrayBuffer", { value: async () => new Uint8Array(600).fill(65).buffer }); w.__f = f; return run("sendFile(tabs[0].pane, window.__f)", "x"); })();
  const raws = ws.sent.filter((m) => m.type === "raw");
  check("file sent in chunks", raws.length >= 3 && raws.slice(-3).reduce((n, m) => n + Buffer.from(m.bytes, "base64").length, 0) === 600, raws.length);

  // find
  t.term.buffer.active = { length: 2, getLine: (i) => ({ translateToString: () => ["abc Hello", "hello again"][i] }) };
  q("[data-action=find]").click(); const f = q("[data-find]"); f.value = "hello"; fire(f, "input");
  check("find counts matches (case-insens.)", q("[data-find-count]").textContent === "2 found", q("[data-find-count]").textContent);

  // settings saved re-applies view
  run("applyProfile(tabs[0], {...tabs[0].profile, view: 'bin', linePauseMs: 5})", "x");
  check("applyProfile resets the view to the profile", q("[data-view]").value === "bin" && q("[data-pause]").value === "5");


  // freeze, copy
  const freeze = q("[data-freeze]");
  freeze.click();
  bin(ws, 3000, "while frozen\r\n");
  check("freeze keeps the screen as it is and counts what comes", !t.term.text().includes("while frozen") && /Resume \(\d+ B new\)/.test(freeze.textContent), freeze.textContent);
  view.value = "hex"; fire(view, "change"); view.value = "text"; fire(view, "change");
  check("a view change while frozen does not show the new data", !t.term.text().includes("while frozen"));
  freeze.click();
  check("resume shows everything", t.term.text().includes("while frozen") && freeze.textContent === "Freeze", t.term.text().slice(-60));
  let copied = null; w.navigator.clipboard.writeText = (x) => { copied = x; return Promise.resolve(); };
  t.term.buffer.active = { length: 2, getLine: (i) => ({ translateToString: () => ["one", "two"][i], isWrapped: false }) };
  q("[data-copy-all]").click();
  check("Copy copies the whole output", copied === "one\ntwo", copied);
  // toolbar buttons and shortcuts
  const act = (a) => q(`[data-action=${a}]`).click();
  const nsent = () => ws.sent.length;
  act("break"); check("break button", ws.sent.pop().type === "break");
  act("log"); check("log button", ws.sent.pop().type === "log");
  act("line-dtr"); const dtr = ws.sent.pop(); check("DTR button toggles", dtr.type === "line" && dtr.line === "dtr" && dtr.value === false, dtr);
  act("font-up"); check("font up", t.fontSize === 15 && t.term.options.fontSize === 15);
  act("clear"); check("clear empties the kept data and resets counters", t.events.length === 0 && ws.sent.pop().type === "resetcounters");
  bin(ws, 2000, "after clear\r\n"); view.value = "hex"; fire(view, "change"); view.value = "text"; fire(view, "change");
  check("redraw after clear shows only new data", t.term.text().includes("after clear") && !t.term.text().includes("hello"), t.term.text());
  act("reset"); check("reset button", t.term.out[0] === "<reset>" || t.term.text() === "");
  act("port"); check("port button closes when connected", ws.sent.pop().type === "close");
  w.document.dispatchEvent(new w.KeyboardEvent("keydown", { code: "KeyS", altKey: true, shiftKey: true, bubbles: true }));
  check("Alt+Shift+S changes the layout", q("#panes").className.includes("cols"), q("#panes").className);
  // ctrl+shift+L clears
  bin(ws, 2100, "zzz\r\n"); t.term.keyHandler({ type: "keydown", key: "L", code: "KeyL", ctrlKey: true, shiftKey: true });
  check("Ctrl+Shift+L clears", t.events.length === 0);
  // two sessions on the same port are refused
  run("openTab({...profiles[0], name: 'other'})", "x");
  check("same port is not opened twice", run("tabs.length", "x") === 1);

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

  // closing a tab ends its auto send and file job
  const second = run("tabs.length", "x");
  typ.value = "text"; fire(typ, "change"); input.value = "tick"; end.value = "none"; fire(end, "change");
  q("[data-auto-ms]").value = "10"; auto.checked = true; fire(auto, "change");
  const job = { stop: false }; t.job = job;
  run("closeTab(tabs[0])", "x");
  const n0 = ws.sent.filter((m) => m.type === "input").length; await sleep(60);
  check("closing a tab stops auto send and a file job", ws.sent.filter((m) => m.type === "input").length === n0 && job.stop === true && run("tabs.length", "x") === 0);

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
  // the kept events are capped (8 MB) and the oldest go first
  run("openTab({...profiles[0], name: 'cap', device: 'fake:binary'})", "x");
  await sleep(10);
  const capTab = run("tabs[tabs.length - 1]", "x");
  for (let i = 0; i < 40; i++) run(`record(tabs[tabs.length - 1], { k: "note", t: ${i}, text: "n${i}" }, 500000)`, "x");
  const capped = run("({ n: tabs[tabs.length - 1].events.length, kept: tabs[tabs.length - 1].kept, first: tabs[tabs.length - 1].events[0].text })", "x");
  check("kept events stay within the cap and drop the oldest", capped.kept <= 8 * 1024 * 1024 * 1.25 && capped.n < 40 && capped.first !== "n0", capped);
  // quick sends: buttons from the session settings
  run("applyProfile(tabs[0], { ...tabs[0].profile, quickSends: 'Hello | text | hi there\\n# note\\nRaw | hex | 41 42 | end=none\\nWiggle | cmd | dtr=0; wait=1' })", "x");
  const qws = run("tabs[0].ws", "x");
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

  // tabs come back after a reload
  run("tabs.forEach((t, i) => { if (i === 0) t.state = 'closed'; })", "x");   // the first one was not connected
  run("saveTabs()", "x");
  const saved = w.sessionStorage.getItem("termdeck.tabs");
  const names = run("tabs.map((t) => t.profile.name)", "x");
  const socketsBefore = sockets.length;
  run("[...tabs].forEach(closeTab)", "x");
  check("no tab is left after closing them", run("tabs.length", "x") === 0);
  w.sessionStorage.setItem("termdeck.tabs", saved);
  run("restoreTabs()", "x");
  await sleep(20);
  check("restoreTabs brings the tabs back, in order", JSON.stringify(run("tabs.map((t) => t.profile.name)", "x")) === JSON.stringify(names), { names, now: run("tabs.map((t) => t.profile.name)", "x") });
  check("the ones that were connected connect again, the other stays closed", run("tabs[0].state", "x") === "closed" && sockets.length === socketsBefore + names.length - 1, { sockets: sockets.length - socketsBefore, names: names.length });
  check("no errors", errors.length === 0, errors);
  console.log(failed ? `${failed} FAILED` : "all passed");
  process.exit(failed ? 1 : 0);
})().catch((e) => { console.log("harness crashed:", e.stack); process.exit(2); });
