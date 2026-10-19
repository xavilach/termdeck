"use strict";

// The connection of a tab to the server: opening the port, what the server says, the state of the tab.

// What the server says besides the port output, by message type.
const REFRESH = (tab) => { if (tab === tab.pane.active) renderToolbar(tab.pane); };

const MESSAGES = {
  state(tab, m) {
    setState(tab, m.state);
    if (m.state === "failed" || m.state === "ended") note(tab, m.message || m.state);
    else if (m.state === "reconnecting") note(tab, `${m.message}: opening the port again as soon as it is back`);
    else if (m.state === "connected") note(tab, `connected to ${summary(tab.profile)}`);
    else if (m.state === "closed") note(tab, "port closed");
  },
  break: (tab, m) => note(tab, m.ok ? "break sent" : `break failed: ${m.message}`),
  logging(tab, m) { tab.logging = m.on; REFRESH(tab); },
  logexists: (tab, m) => askLog(tab, m.path),
  lines(tab, m) {
    if (tab.profile.noteLines && tab.lines) {  // the lines the device drives, when one changes
      for (const line of ["cts", "dsr", "dcd", "ri"]) {
        if (Boolean(tab.lines[line]) !== Boolean(m[line])) note(tab, `${line.toUpperCase()} ${m[line] ? "on" : "off"}`);
      }
    }
    tab.lines = m;
    REFRESH(tab);
  },
  tx: (tab, m) => echoTx(tab, m.data, undefined, m.data.length + 16),
  points: receivePoints,
  tunnel(tab, m) {  // data that went through the tunnel, one way or the other
    const bytes = Uint8Array.from(atob(m.data), (c) => c.charCodeAt(0));
    emit(tab, { k: "rx", t: m.t, raw: bytes, text: bytes, dir: m.dir }, bytes.length + 32);
  },
  xfer: receiveTransfer,
  counters(tab, m) { tab.counters = m; updateCounters(tab); },
  trigger: (tab, m) => note(tab, `trigger: ${m.match}`),
  notice: (tab, m) => note(tab, m.text),
  script(tab, m) {
    tab.script = m.state === "running" ? m.name : null;
    if (m.state === "error") note(tab, `script ${m.name}: ${m.message}`);
    else if (m.state !== "running") note(tab, `script ${m.name} ${m.state}`);
    if (tab === tab.pane.active) { renderToolbar(tab.pane); if (tab === active) renderTree(); }
  },
};

// Port output arrives as a binary frame: time (8 bytes), length of the raw bytes (4), the raw bytes, then the same as UTF-8.
function receiveFrame(tab, data) {
  const buf = new Uint8Array(data);
  const view = new DataView(data);
  const rawLength = view.getUint32(8);
  const ev = { k: "rx", t: Number(view.getBigUint64(0)), raw: buf.subarray(12, 12 + rawLength), text: buf.subarray(12 + rawLength) };
  if (tab.profile.answerback && ev.text.includes(5)) send(tab, tab.profile.answerback);  // ENQ: who are you?
  emit(tab, ev, buf.length);
}

function connect(tab) {
  const open = () => tab.ws.send(JSON.stringify({ type: "open", profile: tab.profile }));
  if (tab.ws && tab.ws.readyState === WebSocket.OPEN) return open();
  tab.ws?.close();
  const ws = new WebSocket(`ws://${location.host}/api/terminal`, ["termdeck", `auth.${secret}`]);
  ws.binaryType = "arraybuffer";
  tab.ws = ws;
  setState(tab, "connecting");
  ws.onopen = open;
  ws.onmessage = (event) => {
    if (typeof event.data !== "string") return receiveFrame(tab, event.data);
    const m = JSON.parse(event.data);
    MESSAGES[m.type]?.(tab, m);
  };
  ws.onclose = () => {
    if (tab.ws !== ws || !tabs.includes(tab)) return;
    setState(tab, "ended");
    note(tab, "connection to termdeck lost");
  };
}

// The log file is already there: append, overwrite or do not log. The port opens once the answer is sent.
function askLog(tab, path) {
  const dialog = $("#log-ask");
  $("#log-ask-text").textContent = `The log file ${path} already exists for ${tab.profile.name}. What should termdeck do?`;
  const answer = (value) => sendMessage(tab, { type: "loganswer", answer: value });
  dialog.addEventListener("close", () => answer(dialog.returnValue || "none"), { once: true });
  dialog.returnValue = "none";
  dialog.showModal();
}

function setState(tab, state) {
  tab.state = state;
  if (state !== "connected") { tab.script = null; tab.lines = null; tab.logging = false; }
  tab.tabEl.className = `tab ${state}${tab === tab.pane.active ? " active" : ""}${tab.belled ? " belled" : ""}`;
  if (tab === tab.pane.active) { renderToolbar(tab.pane); if (tab === active) renderTree(); }
}

const STATUS = { reconnecting: "Reconnecting…", connecting: "Connecting…", connected: "Connected", failed: "Failed", ended: "Disconnected", closed: "Port closed" };

function send(tab, data) {
  if (tab.ws?.readyState === WebSocket.OPEN && tab.state === "connected") {
    sendMessage(tab, { type: "input", data });
  }
}

// What each toolbar button does for the pane's tab.
// A message to the server, when the connection of the tab is open (it is not while it is still opening or lost).
function sendMessage(tab, message) {
  if (tab.ws?.readyState !== WebSocket.OPEN) return false;
  tab.ws.send(JSON.stringify(message));
  return true;
}
