"use strict";

// The left column: groups with their sessions and scripts, and the ports.

// ---------------------------------------------------------------- the tree on the left
// Groups, each with its sessions and scripts, then the serial ports; every level can be reduced.

let storedGroups = [];
let portNames = [];
let virtualPortList = [];
let portDetails = {};  // what each port is (the USB product), by port name
let selectedGroup = null;     // the group "+ Session" and "+ Script" create in
let selectedSession = null;
let selectedScript = null;
const GENERAL = "General";
const NO_GROUP = "";           // stored as the empty group, shown as General
const collapsed = new Set();

try { for (const key of JSON.parse(localStorage.getItem("termdeck.collapsed") || "[]")) collapsed.add(key); } catch { /* no storage */ }
// Sections that start reduced: the first time they are seen only.
try {
  const seen = JSON.parse(localStorage.getItem("termdeck.seen") || "[]");
  if (!seen.includes("virtual")) {
    collapsed.add("virtual");
    localStorage.setItem("termdeck.seen", JSON.stringify([...seen, "virtual"]));
    localStorage.setItem("termdeck.collapsed", JSON.stringify([...collapsed]));
  }
} catch { /* no storage */ }
function remember() {
  try { localStorage.setItem("termdeck.collapsed", JSON.stringify([...collapsed])); } catch { /* no storage */ }
}

const allGroups = () => [...new Set([...storedGroups, ...profiles.map((p) => p.group), ...scripts.map((s) => s.group)])]
  .filter(Boolean).sort((a, b) => a.localeCompare(b));

function toolButton(text, title, handler, disabled) {
  const button = el("button", "", text);
  button.title = title;
  button.setAttribute("aria-label", title);
  button.disabled = !!disabled;
  button.addEventListener("click", (e) => { e.stopPropagation(); handler(); });
  return button;
}

// A header with a reduce/expand arrow, a title and small buttons, and a body shown unless reduced.
function node(key, titleText, className, tools, buildBody, onTitle) {
  const wrap = el("div", "node");
  const head = el("div", `node-head ${className}`);
  const body = el("div", "node-body");
  const isOpen = () => !collapsed.has(key);
  const toggle = el("button", "toggle", isOpen() ? "▾" : "▸");
  toggle.setAttribute("aria-expanded", String(isOpen()));
  toggle.setAttribute("aria-label", `Reduce or expand ${titleText}`);
  const flip = () => {
    if (isOpen()) collapsed.add(key); else collapsed.delete(key);
    remember();
    body.hidden = !isOpen();
    toggle.textContent = isOpen() ? "▾" : "▸";
    toggle.setAttribute("aria-expanded", String(isOpen()));
  };
  toggle.addEventListener("click", (e) => { e.stopPropagation(); flip(); });
  const title = el("span", "title", titleText);
  title.addEventListener("click", () => (onTitle ? onTitle() : flip()));
  const toolbox = el("span", "tools");
  toolbox.append(...tools);
  head.append(toggle, title, toolbox);
  body.hidden = !isOpen();
  buildBody(body);
  wrap.append(head, body);
  return { wrap, head };
}

// A short message where the action happened: in the lines of the current session, or in the bottom bar when none is open.
let tellTimer = null;
function tell(text) {
  if (active) return localNote(active, text);
  const node = $("#notice");
  node.textContent = text;
  clearTimeout(tellTimer);
  tellTimer = setTimeout(() => { node.textContent = ""; }, 6000);
}

// Left click only selects; double click opens or plays; right click edits.
function selectRow(row, remember) {
  document.querySelectorAll("#tree .item.selected, #port-tree .item.selected").forEach((n) => n.classList.remove("selected"));
  row.classList.add("selected");
  remember();
}

function sessionRow(p) {
  const row = el("div", "item");
  row.tabIndex = 0;
  row.setAttribute("role", "option");
  makeDraggable(row, { kind: "session", name: p.name });
  if (selectedSession === p.name) row.classList.add("selected");
  const text = el("span", "text");
  text.append(el("span", "name", p.name), el("span", "detail", summary(p)));
  row.append(text, toolButton("❐", `Duplicate ${p.name}`, () => editProfile(null, { ...p, name: `${p.name} copy` })),
    toolButton("✎", `Edit ${p.name}`, () => editProfile(p)));
  row.addEventListener("click", () => selectRow(row, () => { selectedSession = p.name; }));
  row.addEventListener("dblclick", () => openTab(p));
  row.addEventListener("keydown", (e) => {
    if (e.target !== row) return;
    if (e.key === "Enter") openTab(p);
    else if (e.key === "F2") editProfile(p);
    else if (e.key === "ArrowDown") row.nextElementSibling?.focus();
    else if (e.key === "ArrowUp") row.previousElementSibling?.focus();
  });
  row.addEventListener("contextmenu", (e) => { e.preventDefault(); editProfile(p); });
  return row;
}

function scriptRow(sc) {
  const row = el("div", "item");
  makeDraggable(row, { kind: "script", name: sc.name });
  const running = active?.script === sc.name;
  const text = el("span", "text");
  text.append(el("span", "name", sc.name), el("span", "detail", scriptSummary(sc)));
  const canRun = running || active?.state === "connected";
  const play = () => {
    if (running) return stopScript(active);
    if (canRun) return runScript(active, sc.name);
    tell(`Script ${sc.name} not run: ${active ? "the port of the current session is not open" : "no session is open"}.`);
  };
  if (selectedScript === sc.name) row.classList.add("selected");
  row.append(text, toolButton(running ? "■" : "▶", running ? "Stop the script" : "Run on the current session", play, !canRun),
    toolButton("✎", `Edit ${sc.name}`, () => editScript(sc)));
  row.addEventListener("click", () => selectRow(row, () => { selectedScript = sc.name; }));
  row.addEventListener("dblclick", play);
  row.addEventListener("contextmenu", (e) => { e.preventDefault(); editScript(sc); });
  return row;
}

// Drag a session or script onto a group to move it there; drag a port onto a group to make a session.
let dragItem = null;

function makeDraggable(row, item) {
  row.draggable = true;
  row.addEventListener("dragstart", (e) => {
    dragItem = item;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", item.name);
  });
  row.addEventListener("dragend", () => {
    dragItem = null;
    document.querySelectorAll(".drop-target").forEach((n) => n.classList.remove("drop-target"));
  });
}

async function dropOnGroup(group) {
  const item = dragItem;
  dragItem = null;
  if (!item) return;
  try {
    if (item.kind === "port") {
      selectedGroup = group;
      return editProfile(null, { group, device: item.name, name: item.name });
    }
    const list = item.kind === "session" ? profiles : scripts;
    const found = list.find((x) => x.name === item.name);
    if (!found || found.group === group) return;
    const path = item.kind === "session" ? "/api/profiles/" : "/api/scripts/";
    await api(path + encodeURIComponent(item.name), { method: "PUT", body: { ...found, group } });
    collapsed.delete(`g:${group}`);
    await Promise.all([loadProfiles(), loadScripts()]);
  } catch (error) { showText($("#problem"), error.message); }
}

function makeDropTarget(wrap, group) {
  wrap.addEventListener("dragover", (e) => {
    if (!dragItem) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    wrap.classList.add("drop-target");
  });
  wrap.addEventListener("dragleave", (e) => {
    if (!wrap.contains(e.relatedTarget)) wrap.classList.remove("drop-target");
  });
  wrap.addEventListener("drop", (e) => {
    e.preventDefault();
    wrap.classList.remove("drop-target");
    dropOnGroup(group);
  });
}

// Each group has its own colour (General and the ports are grey), so groups are easy to tell apart.
function groupColor(group) {
  if (!group) return "var(--control)";
  let hash = 0;
  for (const ch of group) hash = (hash * 31 + ch.codePointAt(0)) % 360;
  return `hsl(${hash} 55% 52%)`;
}

function groupNode(group) {
  const label = group || GENERAL;
  const mine = profiles.filter((p) => p.group === group);
  const mineScripts = scripts.filter((s) => s.group === group);
  const tools = [];
  if (!group) tools.push(toolButton("+", "New group", () => { addingGroup = true; renderTree(); $("#new-group-input").focus(); }));
  if (group && !mine.length && !mineScripts.length) tools.push(toolButton("✕", `Delete the empty group ${group}`, () => deleteGroup(group)));
  const select = () => { selectedGroup = group; renderTree(); };
  const { wrap, head } = node(`g:${group}`, label, "group", tools, (body) => {
    const sessions = node(`g:${group}/s`, `Sessions · ${mine.length}`, "sub",
      [toolButton("▶▶", `Open all ${mine.length} sessions of ${label}`, () => mine.forEach((p) => openTab(p)), mine.length < 2),
       toolButton("+", `New session in ${label}`, () => editProfile(null, { group }))], (inner) => {
        for (const p of mine) inner.append(sessionRow(p));
        if (!mine.length) inner.append(el("div", "empty-note", "none yet"));
      });
    const scriptsNode = node(`g:${group}/c`, `Scripts · ${mineScripts.length}`, "sub",
      [toolButton("+", `New script in ${label}`, () => editScript(null, group))], (inner) => {
        for (const sc of mineScripts) inner.append(scriptRow(sc));
        if (!mineScripts.length) inner.append(el("div", "empty-note", "none yet"));
      });
    body.append(sessions.wrap, scriptsNode.wrap);
  }, select);
  wrap.classList.add("group-node");
  wrap.style.setProperty("--gc", groupColor(group));
  makeDropTarget(wrap, group);
  if (selectedGroup === group) head.classList.add("selected");
  head.title = group ? "Click to select the group" : "Sessions and scripts without a group";
  if (!group && addingGroup) head.after(addGroupForm());
  return wrap;
}

// The name field for a new group, shown under the General header.
let addingGroup = false;
function addGroupForm() {
  const formEl = el("form", "add-group");
  formEl.autocomplete = "off";
  const input = el("input");
  input.id = "new-group-input";
  input.placeholder = "New group name, then Enter";
  input.spellcheck = false;
  input.setAttribute("aria-label", "New group name");
  input.addEventListener("keydown", (e) => { if (e.key === "Escape") { addingGroup = false; renderTree(); } });
  formEl.append(input);
  formEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = input.value.trim();
    if (!name) { addingGroup = false; return renderTree(); }
    try {
      await api("/api/groups", { method: "POST", body: { name } });
      addingGroup = false;
      selectedGroup = name;
      collapsed.delete(`g:${name}`);
      showText($("#problem"), "");
      await loadGroups();
    } catch (error) { showText($("#problem"), error.message); }
  });
  return formEl;
}

// The empty pane says what to do, and offers the virtual ports to try when the computer has no serial port.
function renderEmpty() {
  for (const pane of panes) {
    const hint = pane.empty.querySelector("[data-empty-try]");
    if (!hint) continue;
    const show = portNames.length === 0 && profiles.length === 0 && virtualPortList.length > 0;
    hint.hidden = !show;
    hint.querySelectorAll("button").forEach((b) => b.remove());
    if (!show) continue;
    for (const name of ["fake:shell", "fake:counter", "fake:echo"]) {
      const button = el("button", "", name);
      button.addEventListener("click", () => openTab(quickProfile(name, 115200)));
      hint.append(button);
    }
  }
}

function renderTree() {
  renderEmpty();
  const tree = $("#tree");
  const portTree = $("#port-tree");
  tree.replaceChildren();
  const ports = node("ports", `Serial ports (${portNames.length})`, "group",
    [toolButton("⟳", "List the ports again (this also happens every few seconds)", () => loadPorts())], (body) => {
      for (const port of portNames) {
        const row = el("div", "item port");
        row.append(el("span", "name", port));
        const detail = portDetails[port];
        if (detail) row.append(el("span", "detail", detail.info));
        row.title = `${detail ? detail.info + ". " : ""}Double-click to open with default settings; drag onto a group to make a session`;
        makeDraggable(row, { kind: "port", name: port });
        row.addEventListener("click", () => selectRow(row, () => {}));
        row.addEventListener("dblclick", () => openTab(quickProfile(port, 115200)));
        body.append(row);
      }
      if (!portNames.length) body.append(el("div", "empty-note", "none found"));
    });
  ports.wrap.classList.add("group-node");
  ports.wrap.style.setProperty("--gc", "var(--control)");
  const virtual = node("virtual", `Virtual ports (${virtualPortList.length})`, "group",
    [], (body) => {
      for (const port of virtualPortList) {
        const row = el("div", "item port", port.name);
        row.title = `${port.what}. Double-click to open; drag onto a group to make a session`;
        makeDraggable(row, { kind: "port", name: port.name });
        row.addEventListener("click", () => selectRow(row, () => {}));
        row.addEventListener("dblclick", () => openTab(quickProfile(port.name, 115200)));
        body.append(row);
      }
    });
  virtual.wrap.classList.add("group-node");
  virtual.wrap.style.setProperty("--gc", "var(--control)");
  portTree.replaceChildren(ports.wrap, virtual.wrap);
  const groups = allGroups();
  tree.append(groupNode(NO_GROUP));
  for (const group of groups) tree.append(groupNode(group));
  $("#groups").replaceChildren(...groups.map((g) => { const o = el("option"); o.value = g; return o; }));
}

async function loadProfiles() {
  const data = await api("/api/profiles");
  profiles = data.profiles;
  showText($("#problem"), data.error);
  renderTree();
}

async function loadGroups() {
  storedGroups = (await api("/api/groups")).groups;
  renderTree();
}

// The ports are listed again (⟳, and every few seconds); the tree is drawn again only when the list changed,
// so that a USB adapter that is plugged in or pulled out shows up by itself.
let portPollMs = 2000;
async function loadPorts(quiet = false) {
  let names = [];
  try {
    names = await api("/api/ports");
  } catch (error) {
    if (!quiet) showText($("#problem"), error.message);
    if (quiet) return;  // a poll that fails (the server is down) leaves the list as it is; the server check says so
  }
  if (quiet && JSON.stringify(names) === JSON.stringify(portNames)) return;
  portNames = names;
  try { portDetails = await api("/api/portinfo"); } catch { portDetails = {}; }
  try { virtualPortList = await api("/api/virtual"); } catch { virtualPortList = []; }
  $("#ports").replaceChildren(...[...portNames, ...virtualPortList.map((v) => v.name)].map((port) => { const o = el("option"); o.value = port; return o; }));
  renderTree();
}

function pollPorts() {
  if (!document.hidden && secret) loadPorts(true);
  setTimeout(pollPorts, portPollMs);
}

async function deleteGroup(group) {
  try {
    await api(`/api/groups/${encodeURIComponent(group)}`, { method: "DELETE" });
    if (selectedGroup === group) selectedGroup = null;
    await loadGroups();
  } catch (error) { showText($("#problem"), error.message); }
}
