"use strict";

// Commands: every action of the page, defined once with its label, its key and whether it needs a tab. The
// toolbar, the keys, the help and the command palette (Alt+Shift+P) are made of this list.

const COMMANDS = [
  { id: "palette", label: "Commands and quick connect", key: "Alt+Shift+P", run: () => openPalette() },
  { id: "new-device", label: "New device", key: "Alt+Shift+N", run: () => editDevice(null) },
  { id: "help", label: "Help", key: "Alt+Shift+H", run: () => openHelp() },
  { id: "sidebar", label: "Hide or show the left column", key: "Alt+Shift+B", run: () => toggleSidebar() },
  { id: "layout", label: "Next layout (one pane, side by side, stacked)", key: "Alt+Shift+S", run: () => setLayout({ single: "cols", cols: "rows", rows: "single" }[layout]) },
  { id: "prev-tab", label: "Previous tab", key: "Alt+Shift+PageUp", tab: true, run: () => moveFocus(-1) },
  { id: "next-tab", label: "Next tab", key: "Alt+Shift+PageDown", tab: true, run: () => moveFocus(1) },
  { id: "tab-left", label: "Move the tab left", key: "Alt+Shift+ArrowLeft", tab: true, run: () => shiftTab(-1) },
  { id: "tab-right", label: "Move the tab right", key: "Alt+Shift+ArrowRight", tab: true, run: () => shiftTab(1) },
  { id: "close-tab", label: "Close the tab", key: "Alt+Shift+W", tab: true, run: (tab) => requestClose(tab) },
  { id: "mark", label: "Add a mark", key: "Alt+Shift+M", tab: true, run: (tab) => addMark(tab) },
  { id: "copy", label: "Copy the selection", key: "Ctrl+Shift+C", tab: true, run: (tab) => navigator.clipboard.writeText(tab.term.getSelection()).catch(() => {}) },
  { id: "paste", label: "Paste into the send bar", key: "Ctrl+Shift+V", tab: true, run: (tab) => pasteFromClipboard(tab) },
  { id: "clear", label: "Clear the lines", key: "Ctrl+Shift+L", tab: true, run: (tab) => clearTab(tab) },
  { id: "find", label: "Find in the lines", key: "Ctrl+Shift+F", tab: true, run: (tab) => openFind(tab.pane) },
  { id: "port", label: "Close the port, or open it again", tab: true, run: (tab) => { if (tab.state === "connected" || tab.state === "reconnecting") sendMessage(tab, { type: "close" }); else connect(tab); } },
  { id: "break", label: "Send a break", tab: true, run: (tab) => sendMessage(tab, { type: "break" }) },
  { id: "reset", label: "Reset the terminal", tab: true, run: (tab) => { clearTab(tab); tab.term.reset(); applyModes(tab); } },
  { id: "log", label: "Start or stop the log", tab: true, run: (tab) => sendMessage(tab, { type: "log", value: !tab.logging }) },
  { id: "settings", label: "Settings of the port", tab: true, run: (tab) => openSettings(tab) },
  { id: "font-up", label: "Larger text", tab: true, run: (tab) => changeFont(tab, 1) },
  { id: "font-down", label: "Smaller text", tab: true, run: (tab) => changeFont(tab, -1) },
  { id: "line-dtr", label: "Toggle DTR", tab: true, run: (tab) => toggleLine(tab, "dtr") },
  { id: "line-rts", label: "Toggle RTS", tab: true, run: (tab) => toggleLine(tab, "rts") },
  { id: "freeze", label: "Freeze or resume the screen", tab: true, run: (tab) => toggleFreeze(tab.pane) },
  { id: "graph", label: "Show or hide the graph", tab: true, run: (tab) => toggleGraph(tab.pane) },
  { id: "stop-all", label: "Stop everything that runs on the port", tab: true, run: (tab) => sendMessage(tab, { type: "stop" }) },
  { id: "transfer", label: "XMODEM / YMODEM transfer", tab: true, run: (tab) => openXmodem(tab) },
  { id: "tunnel", label: "Tunnel between two ports", run: () => $("#open-tunnel").click() },
];

const commandById = (id) => COMMANDS.find((c) => c.id === id);

// The key of an event as the commands write it: Alt+Shift+N, Ctrl+Shift+F, Alt+1.
function keyName(e) {
  const code = e.code.replace(/^Key/, "").replace(/^Digit/, "");
  return [e.ctrlKey && "Ctrl", e.altKey && "Alt", e.shiftKey && "Shift", e.metaKey && "Meta", code].filter(Boolean).join("+");
}

// Run a command; it says whether it applied (one that needs a tab does nothing without one).
function runCommand(id, tab = active) {
  const command = commandById(id);
  if (!command || (command.tab && !tab)) return false;
  command.run(tab);
  return true;
}

// The command of a key, if any.
const commandOfKey = (name) => COMMANDS.find((c) => c.key === name);

// The keys section of the help, made of the commands.
function showKeys() {
  const list = $("#help-keys");
  list.replaceChildren(...COMMANDS.filter((c) => c.key).map((c) => {
    const item = el("li");
    item.append(el("kbd", "", c.key), ` ${c.label}`);
    return item;
  }));
}

// ---- the command palette: commands, ports to open (also typed as COM3@115200), scripts to run

const palette = $("#palette");
let paletteItems = [];

// A quick-connect text: a port, and a speed after @ (COM3@115200, /dev/ttyUSB0@9600).
function parseQuickConnect(text) {
  const m = /^\s*([^@\s]+)(?:@(\d+))?\s*$/.exec(text);
  return m ? { port: m[1], baud: m[2] ? Number(m[2]) : 0 } : null;
}

function quickConnect(text) {
  const q = parseQuickConnect(text);
  if (!q) return tell(`${text}: write a port, and a speed after @ (COM3@115200)`);
  openPort(q.port, undefined, q.baud ? { baud: q.baud } : {});
}

function paletteChoices(query) {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  const fits = (text) => words.every((w) => text.toLowerCase().includes(w));
  const out = COMMANDS.filter((c) => (!c.tab || active) && fits(c.label)).map((c) => ({ label: c.label, key: c.key, run: () => runCommand(c.id) }));
  for (const port of [...portNames, ...virtualPortList.map((v) => v.name)]) {
    const label = `Open ${port} with ${deviceOf(port)}`;
    if (fits(label)) out.push({ label, run: () => openPort(port) });
  }
  if (active?.state === "connected") {
    for (const s of scripts) if (fits(`Run script ${s.name}`)) out.push({ label: `Run script ${s.name}`, run: () => runScript(active, s.name) });
  }
  const q = parseQuickConnect(query);
  if (q && /@/.test(query)) out.unshift({ label: `Open ${q.port} at ${q.baud || "its"} baud`, run: () => quickConnect(query) });
  return out.slice(0, 30);
}

function showPalette() {
  const query = $("#palette-input").value;
  paletteItems = paletteChoices(query);
  palette.selected = Math.min(palette.selected || 0, Math.max(paletteItems.length - 1, 0));
  $("#palette-list").replaceChildren(...paletteItems.map((item, i) => {
    const row = el("li", i === palette.selected ? "selected" : "", item.label);
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", String(i === palette.selected));
    if (item.key) row.append(el("kbd", "", item.key));
    row.addEventListener("click", () => choose(i));
    return row;
  }));
}

function choose(i) {
  const item = paletteItems[i];
  palette.close();
  item?.run();
}

function openPalette() {
  $("#palette-input").value = "";
  palette.selected = 0;
  showPalette();
  palette.showModal();
  $("#palette-input").focus();
}

$("#palette-input").addEventListener("input", () => { palette.selected = 0; showPalette(); });
$("#palette-input").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    palette.selected = (palette.selected + (e.key === "ArrowDown" ? 1 : -1) + paletteItems.length) % Math.max(paletteItems.length, 1);
    showPalette();
  } else if (e.key === "Enter") {
    e.preventDefault();
    choose(palette.selected);
  }
});
palette.addEventListener("click", (e) => { if (e.target === palette) palette.close(); });
