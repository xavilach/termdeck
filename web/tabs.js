"use strict";

// Panes, tabs, the connection of a tab, the toolbar, the shortcuts and the start.

// ---------------------------------------------------------------- panes and tabs
// One pane, or two (side by side or stacked), each with its own tabs, toolbar and lines.
// Only the left column is shared. A tab can be dragged from one pane to the other.

const panes = [];
let focusPane = null;
let layout = "single";        // single, cols (side by side) or rows (top and bottom)
let dragged = null;           // the tab being dragged
let layoutEl = null;          // the layout buttons: always at the right end of the top right pane

function createPane() {
  const root = $("#pane-template").content.firstElementChild.cloneNode(true);
  const pane = { el: root, tabsEl: root.querySelector(".tabs"), toolbar: root.querySelector(".toolbar"),
                 terminals: root.querySelector(".terminals"), workspace: root.querySelector(".workspace"),
                 empty: root.querySelector(".empty"), tabs: [], active: null,
                 viewbar: root.querySelector(".viewbar"), quickbar: root.querySelector(".quickbar"), graphbar: root.querySelector(".graphbar"), sendbar: root.querySelector(".sendbar"), findbar: root.querySelector(".findbar"),
                 find: { index: -1, matches: [], key: "" } };
  wireBars(pane);
  if (panes.length === 0) {  // the first pane carries the button for the left column and the layout buttons
    root.querySelector(".sidebar-toggle").id = "sidebar-toggle";
    layoutEl = root.querySelector(".layout");
    for (const button of root.querySelectorAll("[data-layout]")) button.addEventListener("click", () => setLayout(button.dataset.layout));
  } else {
    root.querySelector(".sidebar-toggle").remove();
    root.querySelector(".layout").remove();
  }
  root.addEventListener("mousedown", () => focusOn(pane), true);
  root.addEventListener("focusin", () => focusOn(pane));
  pane.toolbar.addEventListener("click", (e) => onToolbar(pane, e));
  for (const area of [pane.tabsEl, pane.terminals]) {  // a dragged tab dropped on the pane moves here
    area.addEventListener("dragover", (e) => {
      if (!dragged || e.target.closest(".tab")) return;
      e.preventDefault();
      root.classList.add("drop-target");
    });
    area.addEventListener("dragleave", (e) => { if (!area.contains(e.relatedTarget)) root.classList.remove("drop-target"); });
    area.addEventListener("drop", (e) => {
      if (!dragged || e.target.closest(".tab")) return;
      e.preventDefault();
      root.classList.remove("drop-target");
      moveTab(dragged, pane, pane.tabs.length);
    });
  }
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(() => fitPane(pane)).observe(pane.terminals);
  $("#panes").append(root);
  panes.push(pane);
  return pane;
}

function focusOn(pane) {
  if (focusPane === pane) return;
  focusPane = pane;
  active = pane.active;
  for (const p of panes) p.el.classList.toggle("focused", p === pane);
  renderToolbars();
  renderTree();
}

function fitPane(pane) {
  if (pane.active) pane.active.fit.fit();
}
function fitAll() {
  requestAnimationFrame(() => panes.forEach(fitPane));
}

function refreshPane(pane) {
  for (const t of pane.tabs) {
    t.box.classList.toggle("active", t === pane.active);
    t.tabEl.classList.toggle("active", t === pane.active);
    if (t === pane.active) t.tabMain.setAttribute("aria-current", "true"); else t.tabMain.removeAttribute("aria-current");
  }
  pane.empty.hidden = pane.tabs.length > 0;
}

function activate(tab) {
  const pane = tab.pane;
  pane.active = tab;
  focusPane = pane;
  active = tab;
  tab.belled = false;
  tab.tabEl.classList.remove("belled", "activity");
  tab.unread = false;
  showUnread();
  refreshPane(pane);
  for (const p of panes) p.el.classList.toggle("focused", p === pane);
  syncBars(pane);
  renderToolbars();
  renderTree();
  requestAnimationFrame(() => { fitPane(pane); tab.term.focus(); });
}

// Control characters in pasted text are dropped (line breaks and tabs stay), unless the profile allows them.
function pasteText(tab, text) {
  const input = tab.pane.sendbar.querySelector("[data-send-text]");
  const type = tab.pane.sendbar.querySelector("[data-send-type]").value;
  input.setRangeText(clean(type, text.replace(/\s+/g, " ")), input.selectionStart, input.selectionEnd, "end");
  regroup(input, type);
  input.focus();
}
function pasteFromClipboard(tab) {
  navigator.clipboard.readText().then((text) => pasteText(tab, text)).catch(() => {});
}

// The screen modes a profile wants from the start (also after a reset).
function applyModes(tab) {
  const p = tab.profile;
  let modes = "";
  if (p.autoWrap === false) modes += "\x1b[?7l";
  if (p.originMode) modes += "\x1b[?6h";
  if (modes) tab.term.write(modes);
}

const portName = (device) => device.replace(/^\\\\\.\\/, "").toLowerCase();

// The same session, or the same port, is never opened twice: its tab is shown instead.
// The session or the port is already open in a tab: the tab, or null.
function openElsewhere(profile) {
  const portsOf = (p) => [p.device, p.peer?.device].filter(Boolean).map(portName);
  const sameSession = tabs.find((t) => t.profile.name === profile.name);
  const samePort = tabs.find((t) => (t.state === "connected" || t.state === "connecting") && portsOf(t.profile).some((port) => portsOf(profile).includes(port)));
  const existing = sameSession || samePort;
  if (!existing) return null;
  const why = sameSession ? `${profile.name} is already open` : `${profile.device} is already open in the tab ${existing.profile.name}`;
  activate(existing);
  showLines();
  tell(existing.state === "connected" ? `${why}: showing it.` : `${why}: showing it (use Open port to connect again).`);
  return existing;
}

function newTerminal(tab) {
  const profile = tab.profile;
  tab.box = el("div", "term");
  tab.pane.terminals.append(tab.box);
  tab.term = new Terminal({
    fontSize: tab.fontSize,
    fontFamily: (profile.fontFamily ? profile.fontFamily + ", " : "") + MONO_FONTS,
    scrollback: profile.scrollback || 10000,
    cursorStyle: profile.cursor || "block",
    cursorBlink: !!profile.cursorBlink,
    convertEol: !!profile.implicitCR,
    theme: terminalTheme(profile),
    ...boldOptions(profile),
  });
  tab.term.onBell(() => bell(tab));
  tab.fit = new FitAddon.FitAddon();
  tab.term.loadAddon(tab.fit);
  tab.term.open(tab.box);
  applyModes(tab);
}

// The lines are read-only: only the send bar at the bottom sends. Copy, paste, clear and find still work.
const SHORTCUTS = {
  KeyC: (tab) => navigator.clipboard.writeText(tab.term.getSelection()).catch(() => {}),
  KeyV: pasteFromClipboard,
  KeyL: clearTab,
  KeyF: (tab) => openFind(tab.pane),
};

function listenToTerminal(tab) {
  tab.term.onTitleChange((title) => {
    if (tab.profile.ignoreTitle) return;
    tab.title = title;
    tab.tabEl.title = title;
    if (tab === tab.pane.active) renderToolbar(tab.pane);
  });
  tab.term.onSelectionChange(() => {
    if (tab.profile.copyOnSelect && tab.term.hasSelection()) navigator.clipboard.writeText(tab.term.getSelection()).catch(() => {});
  });
  tab.term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;
    if (e.ctrlKey && e.shiftKey) {
      const shortcut = SHORTCUTS[e.code];
      shortcut?.(tab);
      return !shortcut;
    }
    return e.ctrlKey && e.code === "KeyC" && tab.term.hasSelection();  // Ctrl+C copies a selection; no key sends
  });
  // Pasting (menu, Ctrl+V, middle click) goes into the send bar.
  tab.box.addEventListener("paste", (e) => {
    e.preventDefault();
    e.stopPropagation();
    pasteText(tab, e.clipboardData.getData("text"));
  }, true);
  tab.box.addEventListener("contextmenu", (e) => {
    const action = tab.profile.rightClick || "copypaste";
    if (action === "none") return;  // the browser's own menu
    e.preventDefault();
    const selection = tab.term.getSelection();
    if (selection && action === "copypaste") navigator.clipboard.writeText(selection).catch(() => {});
    else pasteFromClipboard(tab);
  });
}

function newTabElement(tab) {
  const close = el("button", "close", "✕");
  close.setAttribute("aria-label", `Close ${tab.profile.name}`);
  close.addEventListener("click", (e) => { e.stopPropagation(); requestClose(tab); });
  tab.label = el("span", "label", tab.profile.name);
  tab.dot = el("span", "dot");
  tab.closeBtn = close;
  tab.tabEl = el("div", "tab");
  // the tab itself is the button inside it: a button must not hold another
  tab.tabEl.draggable = true;
  tab.tabMain = el("button", "tabmain");
  tab.tabMain.append(tab.dot, tab.label);
  tab.tabEl.append(tab.tabMain, close);
  tab.tabEl.addEventListener("click", () => { activate(tab); showLines(); });
  addReorder(tab);
}

// The open tabs are remembered for the life of the browser tab, so that a reload (F5) brings them back:
// the ones that were connected connect again, the others come back with their port closed.
const TABS_KEY = "termdeck.tabs";

function saveTabs() {
  const data = { active: tabs.indexOf(active), tabs: tabs.map((t) => ({ profile: t.profile, connected: ["connected", "connecting", "reconnecting"].includes(t.state), pane: panes.indexOf(t.pane) })) };
  try { sessionStorage.setItem(TABS_KEY, JSON.stringify(data)); } catch { /* no storage */ }
}

function restoreTabs() {
  let data = null;
  try { data = JSON.parse(sessionStorage.getItem(TABS_KEY)); } catch { /* nothing kept */ }
  if (!data || !Array.isArray(data.tabs)) return;
  for (const saved of data.tabs) {
    focusPane = panes[Math.min(Math.max(saved.pane, 0), panes.length - 1)];
    openTab(saved.profile, { connect: saved.connected });
  }
  if (tabs[data.active]) { activate(tabs[data.active]); showLines(); }
}

addEventListener("pagehide", saveTabs);
document.addEventListener("visibilitychange", () => { if (!document.hidden && active) { active.unread = false; active.tabEl.classList.remove("activity"); showUnread(); } });

function openTab(profile, { connect: doConnect = true } = {}) {
  if (openElsewhere(profile)) return;
  const pane = focusPane || panes[0];
  const tab = { profile, pane, state: "connecting", ws: null, fontSize: profile.fontSize || 14,
                lineStart: true, txOpen: false, script: null, events: [], kept: 0, held: [], view: viewOf(profile),
                sendEnd: { mode: profile.enter || "cr", custom: "" }, sendCheck: "none", counters: { rx: 0, tx: 0 }, sent: loadHistory(profile.name), sentAt: 0, graph: newGraph(profile), frameFeed: null, frameSpec: null, pending: new Uint8Array(0), highlights: parseHighlights(profile.highlights) };
  tab.sentAt = tab.sent.length;
  frameParserOf(tab);
  newTerminal(tab);
  newTabElement(tab);
  listenToTerminal(tab);
  pane.tabsEl.append(tab.tabEl);
  pane.tabs.push(tab);
  tabs.push(tab);
  activate(tab);
  showLines();
  if (doConnect) connect(tab);
  else setState(tab, "closed");
  saveTabs();
}

// Drag a tab to reorder it, or onto the other pane to move it there.
function addReorder(tab) {
  const node = tab.tabEl;
  node.addEventListener("dragstart", (e) => {
    dragged = tab;
    node.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", tab.profile.name);
  });
  node.addEventListener("dragend", () => {
    dragged = null;
    for (const t of tabs) t.tabEl.classList.remove("dragging", "drop-before");
    for (const p of panes) p.el.classList.remove("drop-target");
  });
  node.addEventListener("dragover", (e) => {
    if (!dragged || dragged === tab) return;
    e.preventDefault();
    node.classList.add("drop-before");
  });
  node.addEventListener("dragleave", () => node.classList.remove("drop-before"));
  node.addEventListener("drop", (e) => {
    e.preventDefault();
    if (dragged && dragged !== tab) moveTab(dragged, tab.pane, tab.pane.tabs.indexOf(tab));
  });
}

// Put a tab at a position of a pane (the same pane to reorder, the other one to move it there).
function moveTab(tab, pane, index) {
  const from = tab.pane;
  from.tabs.splice(from.tabs.indexOf(tab), 1);
  if (from.active === tab) from.active = from.tabs[from.tabs.length - 1] || null;
  if (from !== pane) {
    tab.pane = pane;
    pane.terminals.append(tab.box);
  }
  pane.tabs.splice(Math.min(Math.max(index, 0), pane.tabs.length), 0, tab);
  for (const p of new Set([from, pane])) p.tabsEl.replaceChildren(...p.tabs.map((t) => t.tabEl));
  refreshPane(from);
  activate(tab);
  fitPane(from);
  saveTabs();
}

// Closing a connected tab asks first (unless the profile says not to): click the close button twice.
function requestClose(tab) {
  const asks = tab.state === "connected" && tab.profile.warnClose !== false;
  if (!asks || tab.confirmClose) return closeTab(tab);
  tab.confirmClose = true;
  tab.closeBtn.textContent = "Close?";
  tab.closeBtn.classList.add("confirm");
  setTimeout(() => {
    tab.confirmClose = false;
    tab.closeBtn.textContent = "✕";
    tab.closeBtn.classList.remove("confirm");
  }, 3000);
}

addEventListener("beforeunload", (e) => {  // closing the page would drop the open ports
  if (tabs.some((t) => t.state === "connected" && t.profile.warnClose !== false)) { e.preventDefault(); e.returnValue = ""; }
});

function closeTab(tab) {
  const pane = tab.pane;
  if (tab.job) tab.job.stop = true;  // a file being sent
  if (pane.auto?.tab === tab) stopAuto(pane);
  tab.ws?.close();
  tab.term.dispose();
  tab.box.remove();
  tab.tabEl.remove();
  tabs.splice(tabs.indexOf(tab), 1);
  pane.tabs.splice(pane.tabs.indexOf(tab), 1);
  saveTabs();
  if (pane.active === tab) pane.active = pane.tabs[pane.tabs.length - 1] || null;
  if (pane === focusPane) active = pane.active;
  if (pane.active) return activate(pane.active);
  refreshPane(pane);
  renderToolbars();
  renderTree();
}

function setLayout(mode) {
  layout = mode;
  if (mode === "single") {
    while (panes.length > 1) {  // the tabs of the second pane join the first
      const gone = panes.pop();
      for (const t of gone.tabs) {
        t.pane = panes[0];
        panes[0].tabs.push(t);
        panes[0].terminals.append(t.box);
      }
      gone.el.remove();
    }
    const first = panes[0];
    first.tabsEl.replaceChildren(...first.tabs.map((t) => t.tabEl));
    first.active = first.active || first.tabs[0] || null;
    focusPane = first;
    active = first.active;
    refreshPane(first);
  } else if (panes.length < 2) {
    const keep = active;
    const second = createPane();
    const other = [...panes[0].tabs].reverse().find((t) => t !== panes[0].active);
    if (other) moveTab(other, second, 0);  // the second pane starts with another session, if there is one
    if (keep) activate(keep);
  }
  $("#panes").className = mode;
  // side by side: the right pane is the top right one; otherwise the first pane spans the top
  panes[mode === "cols" ? 1 : 0].el.querySelector(".tabbar").append(layoutEl);
  for (const button of document.querySelectorAll("[data-layout]")) button.setAttribute("aria-pressed", String(button.dataset.layout === mode));
  for (const p of panes) p.el.classList.toggle("focused", p === focusPane);
  try { localStorage.setItem("termdeck.layout", mode); } catch { /* no storage */ }
  renderToolbars();
  renderTree();
  fitAll();
}

function renderLines(pane) {
  const tab = pane.active;
  const lines = tab.lines || {};
  const connected = tab.state === "connected";
  pane.toolbar.querySelector(".lines").classList.toggle("off", !connected);
  for (const line of ["dtr", "rts"]) {
    const value = connected ? (lines[line] ?? tab.profile[line] !== false) : false;
    pane.toolbar.querySelector(`[data-action=line-${line}]`).setAttribute("aria-pressed", String(value));
  }
  for (const led of ["cts", "dsr", "dcd", "ri"]) pane.toolbar.querySelector(`[data-led=${led}]`).classList.toggle("on", connected && !!lines[led]);
}

function renderToolbar(pane) {
  const tab = pane.active;
  pane.toolbar.hidden = !tab;
  pane.empty.hidden = pane.tabs.length > 0;
  if (!tab) { pane.viewbar.hidden = pane.sendbar.hidden = pane.findbar.hidden = pane.quickbar.hidden = pane.graphbar.hidden = true; return; }
  const q = (selector) => pane.toolbar.querySelector(selector);
  q(".status").textContent = STATUS[tab.state];
  q(".status").className = `status ${tab.state}`;
  q(".summary").textContent = summary(tab.profile) + (tab.title ? ` · ${tab.title}` : "");
  q(".script-status").hidden = !tab.script;
  q(".script-name").textContent = tab.script ? `Script ${tab.script}` : "";
  renderLines(pane);
  const logButton = q("[data-action=log]");
  logButton.setAttribute("aria-pressed", String(!!tab.logging));
  logButton.disabled = tab.state !== "connected";
  const portButton = q("[data-action=port]");
  portButton.textContent = { connected: "Close port", reconnecting: "Stop reconnecting" }[tab.state] || "Open port";
  portButton.disabled = tab.state === "connecting";
  pane.viewbar.hidden = pane.sendbar.hidden = false;
  const off = tab.state !== "connected" || !!tab.profile.peer;  // nothing is sent into a tunnel
  for (const field of pane.sendbar.querySelectorAll("select, input:not([data-file]), button:not([data-file-cancel])")) field.disabled = off;
  showQuick(pane);
  if (off) stopAuto(pane);
  renderFileStatus(pane);
}
const renderToolbars = () => panes.forEach(renderToolbar);

focusPane = createPane();
focusPane.el.classList.add("focused");
try { const saved = localStorage.getItem("termdeck.layout"); if (saved === "cols" || saved === "rows") setLayout(saved); } catch { /* no storage */ }

// ---------------------------------------------------------------- input

// ---------------------------------------------------------------- the left column

function setSidebar(visible) {
  document.body.classList.toggle("no-sidebar", !visible);
  const button = $("#sidebar-toggle");
  button.setAttribute("aria-expanded", String(visible));
  const label = visible ? "Hide the left column" : "Show the left column";
  button.setAttribute("aria-label", label);
  button.title = `${label} (Alt+Shift+B)`;
  button.textContent = visible ? "‹" : "›";
  try { localStorage.setItem("termdeck.sidebar", visible ? "1" : "0"); } catch { /* no storage */ }
  fitAll();
}
const toggleSidebar = () => setSidebar(document.body.classList.contains("no-sidebar"));
$("#sidebar-toggle").addEventListener("click", toggleSidebar);

// The width of the left column can be changed by dragging its edge; it is remembered.
const SIDEBAR_MIN = 180;
const SIDEBAR_MAX = 600;
function showSidebarWidth(width) {
  const handle = $("#sidebar-resize");
  handle.setAttribute("aria-valuenow", String(width));
  handle.setAttribute("aria-valuemin", String(SIDEBAR_MIN));
  handle.setAttribute("aria-valuemax", String(SIDEBAR_MAX));
}

function setSidebarWidth(px) {
  const width = Math.min(Math.max(Math.round(px), SIDEBAR_MIN), SIDEBAR_MAX);
  document.documentElement.style.setProperty("--sidebar-w", `${width}px`);
  showSidebarWidth(width);
  stored.write("termdeck.sidebar", width);
  fitAll();
  return width;
}
const savedSidebar = Math.min(Math.max(stored.read("termdeck.sidebar", 250), SIDEBAR_MIN), SIDEBAR_MAX);
if (savedSidebar !== 250) document.documentElement.style.setProperty("--sidebar-w", `${savedSidebar}px`);
showSidebarWidth(savedSidebar);

$("#sidebar-resize").addEventListener("pointerdown", (e) => {
  e.preventDefault();  // no text selection or drag of something under it starts instead
  const handle = e.currentTarget;
  handle.setPointerCapture(e.pointerId);
  handle.classList.add("dragging");
  const move = (m) => setSidebarWidth(m.clientX - $("#sidebar").getBoundingClientRect().left);
  const stop = () => { handle.classList.remove("dragging"); handle.removeEventListener("pointermove", move); handle.removeEventListener("pointerup", stop); };
  handle.addEventListener("pointermove", move);
  handle.addEventListener("pointerup", stop);
});
$("#sidebar-resize").addEventListener("keydown", (e) => {
  if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
  e.preventDefault();
  setSidebarWidth($("#sidebar").getBoundingClientRect().width + (e.key === "ArrowRight" ? 20 : -20));
});
try { if (localStorage.getItem("termdeck.sidebar") === "0") setSidebar(false); } catch { /* no storage */ }

// ---------------------------------------------------------------- toolbar and keys

const TOOLBAR = {
  port(tab) { if (tab.state === "connected" || tab.state === "reconnecting") sendMessage(tab, { type: "close" }); else connect(tab); },
  break: (tab) => sendMessage(tab, { type: "break" }),
  clear: clearTab,
  reset(tab) {
    clearTab(tab);
    tab.term.reset();
    Object.assign(tab, { lineStart: true, txOpen: false, hex: null, held: [] });
    applyModes(tab);
  },
  log: (tab) => sendMessage(tab, { type: "log", value: !tab.logging }),
  "stop-script": stopScript,
  "font-up": (tab) => changeFont(tab, 1),
  "font-down": (tab) => changeFont(tab, -1),
};

function toggleLine(tab, line) {
  const value = !(tab.lines?.[line] ?? tab.profile[line] !== false);
  tab.lines = { ...(tab.lines || {}), [line]: value };  // shown at once; the device's own lines follow
  sendMessage(tab, { type: "line", line, value });
  renderLines(tab.pane);
}

function changeFont(tab, step) {
  tab.fontSize = Math.min(Math.max(tab.fontSize + step, 6), 72);
  tab.term.options.fontSize = tab.fontSize;
  tab.fit.fit();
}

function openSettings(tab) {
  const saved = profiles.find((p) => p.name === tab.profile.name);
  editProfile(saved || null);
  if (!saved) fillForm(form, tab.profile);  // a quick-connect tab: start from its settings
}

function onToolbar(pane, e) {
  const action = e.target.closest("[data-action]")?.dataset.action;
  const tab = pane.active;
  if (!action || !tab) return;
  if (action === "settings") return openSettings(tab);
  if (action === "line-dtr" || action === "line-rts") toggleLine(tab, action.slice(5));
  else TOOLBAR[action]?.(tab);
  tab.term.focus();
}

addEventListener("resize", fitAll);
matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
  for (const t of tabs) t.term.options.theme = terminalTheme(t.profile);
});
// Alt+Shift shortcuts of the page. A shortcut says whether it applies (an open tab, say) by returning false.
function moveFocus(step) {
  const list = focusPane.tabs;
  if (!list.length) return false;
  activate(list[(list.indexOf(active) + step + list.length) % list.length]);
  showLines();
}

function shiftTab(step) {
  if (!active) return false;
  moveTab(active, focusPane, focusPane.tabs.indexOf(active) + step);
  active.tabEl.scrollIntoView({ block: "nearest", inline: "nearest" });
}

const PAGE_SHORTCUTS = {
  KeyN: () => editProfile(null),
  KeyM: () => (active ? addMark(active) : false),
  KeyH: openHelp,
  KeyB: toggleSidebar,
  KeyS: () => setLayout({ single: "cols", cols: "rows", rows: "single" }[layout]),
  KeyW: () => (active ? requestClose(active) : false),
  PageUp: () => moveFocus(-1),
  PageDown: () => moveFocus(1),
  ArrowLeft: () => shiftTab(-1),
  ArrowRight: () => shiftTab(1),
};

document.addEventListener("click", (e) => {  // a click elsewhere closes the open "More" menus
  for (const menu of document.querySelectorAll(".viewbar details.more[open]")) if (!menu.contains(e.target)) menu.open = false;
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.querySelectorAll(".viewbar details.more[open]").forEach((menu) => { menu.open = false; });
  if (!(e.altKey && e.shiftKey)) return;
  if (PAGE_SHORTCUTS[e.code]?.() !== false && e.code in PAGE_SHORTCUTS) e.preventDefault();
});

// Is the server still there? Checked every few seconds; a restarted server has forgotten our secret.
let serverUp = null;
function showServer(state, text) {
  const node = $("#server-status");
  node.textContent = text;
  node.className = state;
  if (state === "down" && serverUp !== false) {
    for (const t of tabs) if (t.state !== "ended" && t.state !== "closed") { setState(t, "ended"); note(t, "termdeck stopped"); }
  }
  serverUp = state === "up";
}
async function checkServer() {
  if (!secret) return;
  try {
    const response = await fetch("/api/ping", { headers: { "X-Termdeck-Auth": secret }, cache: "no-store" });
    if (response.ok) showServer("up", "● Server running");
    else showServer("down", "● Server restarted: open the new link it printed");
  } catch {
    showServer("down", "● Server stopped: start termdeck again and open the new link");
  }
}
setInterval(checkServer, 3000);
setTimeout(pollPorts, portPollMs);

login().then(() => { checkServer(); return Promise.all([loadProfiles(), loadGroups(), loadPorts(), loadScripts()]); }).then(restoreTabs).catch((error) => {
  document.body.replaceChildren(el("p", "problem", error.message));
});
