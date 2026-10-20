"use strict";

// The connection of a tab to its session on the server: attaching (again after a reload), opening the port,
// what the server says, the state of the tab.

// What the server says besides the screen output, by message type.
const REFRESH = (tab) => { if (tab === tab.pane.active) renderToolbar(tab.pane); };

const MESSAGES = {
  // The answer to hello: the state of the session, which may have lived on while the page reloaded.
  session(tab, m) {
    setState(tab, m.state);
    tab.logging = m.logging;
    tab.script = m.script || null;
    if (tab.wantOpen && !["connected", "connecting", "reconnecting"].includes(m.state)) sendMessage(tab, { type: "open", profile: tab.profile });
    tab.wantOpen = false;
    REFRESH(tab);
  },
  state: (tab, m) => setState(tab, m.state),
  reset: startRedraw,
  replayed: endRedraw,
  logging(tab, m) { tab.logging = m.on; REFRESH(tab); },
  logexists: (tab, m) => askLog(tab, m.path),
  lines(tab, m) { tab.lines = m; REFRESH(tab); },
  points: receivePoints,
  notify: (tab, m) => notifyLine(tab, m.text),
  xfer: receiveTransfer,
  counters(tab, m) { tab.counters = m; updateCounters(tab); },
  script(tab, m) {
    tab.script = m.state === "running" ? m.name : null;
    if (tab === tab.pane.active) { renderToolbar(tab.pane); if (tab === active) renderTree(); }
  },
};

// Attach the tab to its session (the server starts one for a new id), then open the port when asked.
function connect(tab, open = true) {
  if (tab.ws && tab.ws.readyState === WebSocket.OPEN) {
    if (open) sendMessage(tab, { type: "open", profile: tab.profile });
    return;
  }
  tab.ws?.close();
  const ws = new WebSocket(`ws://${location.host}/api/terminal?id=${encodeURIComponent(tab.id)}`, ["termdeck", `auth.${secret}`]);
  ws.binaryType = "arraybuffer";
  tab.ws = ws;
  tab.wantOpen = open;
  if (open) setState(tab, "connecting");
  ws.onopen = () => ws.send(JSON.stringify({ type: "hello", profile: tab.profile, view: tab.view }));
  ws.onmessage = (event) => {
    if (typeof event.data !== "string") return showOutput(tab, event.data);
    const m = JSON.parse(event.data);
    MESSAGES[m.type]?.(tab, m);
  };
  ws.onclose = () => {
    if (tab.ws !== ws || !tabs.includes(tab)) return;
    setState(tab, "ended");
    localNote(tab, "connection to termdeck lost");
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

// A message to the server, when the connection of the tab is open (it is not while it is still opening or lost).
function sendMessage(tab, message) {
  if (tab.ws?.readyState !== WebSocket.OPEN) return false;
  tab.ws.send(JSON.stringify(message));
  return true;
}
