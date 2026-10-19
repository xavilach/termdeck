"use strict";

const $ = (selector) => document.querySelector(selector);
const el = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;  // never innerHTML: names and port output are untrusted
  return node;
};

let secret = "";
let profiles = [];
let scripts = [];
const tabs = [];
let active = null;

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-Termdeck-Auth": secret, "Content-Type": "application/json" },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

async function login() {
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  history.replaceState(null, "", location.pathname);
  if (!token) throw new Error("Open termdeck with the link it printed.");
  const response = await fetch("/api/login", { method: "POST", body: JSON.stringify({ token }) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error);
  secret = data.secret;
}

function showText(node, text) {
  node.textContent = text || "";
  node.hidden = !text;
}

const summary = (p) => `${p.device} ${p.baud} ${p.dataBits}${p.parity[0].toUpperCase()}${p.stopBits}` +
  (p.flow && p.flow !== "none" ? ` ${p.flow === "rtscts" ? "RTS/CTS" : "XON/XOFF"}` : "");

// ---------------------------------------------------------------- views
// Editing a session or a script happens in a dialog over the lines.

const PAGES = ["#profile-form", "#script-form"];

function showPage(selector) {
  for (const page of PAGES) $(page).hidden = page !== selector;
  if (!$("#panel").open) $("#panel").showModal();
}

function showLines() {
  if ($("#panel").open) $("#panel").close();
  if (active) { fitAll(); requestAnimationFrame(() => active.term.focus()); }
}
$("#panel").addEventListener("close", () => { if (active) active.term.focus(); });  // also after Esc

document.addEventListener("click", (e) => { if (e.target.closest("[data-back]")) showLines(); });

// ---------------------------------------------------------------- profiles

// ---------------------------------------------------------------- the tree on the left
// Groups, each with its sessions and scripts, then the serial ports; every level can be reduced.

let storedGroups = [];
let portNames = [];
let selectedGroup = null;     // the group "+ Session" and "+ Script" create in
let selectedSession = null;
let selectedScript = null;
const GENERAL = "General";
const NO_GROUP = "";           // stored as the empty group, shown as General
const collapsed = new Set();

try { for (const key of JSON.parse(localStorage.getItem("termdeck.collapsed") || "[]")) collapsed.add(key); } catch { /* no storage */ }
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
  if (active) return note(active, text);
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
  row.append(text, toolButton("✎", `Edit ${p.name}`, () => editProfile(p)));
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
      [toolButton("+", `New session in ${label}`, () => editProfile(null, { group }))], (inner) => {
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

function renderTree() {
  const tree = $("#tree");
  const portTree = $("#port-tree");
  tree.replaceChildren();
  const ports = node("ports", `Serial ports (${portNames.length})`, "group",
    [toolButton("⟳", "Refresh the port list", loadPorts)], (body) => {
      for (const port of portNames) {
        const row = el("div", "item port", port);
        row.title = "Double-click to open with default settings; drag onto a group to make a session";
        makeDraggable(row, { kind: "port", name: port });
        row.addEventListener("click", () => selectRow(row, () => {}));
        row.addEventListener("dblclick", () => openTab(quickProfile(port, 115200)));
        body.append(row);
      }
      if (!portNames.length) body.append(el("div", "empty-note", "none found"));
    });
  ports.wrap.classList.add("group-node");
  ports.wrap.style.setProperty("--gc", "var(--control)");
  portTree.replaceChildren(ports.wrap);
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

async function loadPorts() {
  try {
    portNames = await api("/api/ports");
  } catch (error) {
    portNames = [];
    showText($("#problem"), error.message);
  }
  $("#ports").replaceChildren(...portNames.map((port) => { const o = el("option"); o.value = port; return o; }));
  renderTree();
}

async function deleteGroup(group) {
  try {
    await api(`/api/groups/${encodeURIComponent(group)}`, { method: "DELETE" });
    if (selectedGroup === group) selectedGroup = null;
    await loadGroups();
  } catch (error) { showText($("#problem"), error.message); }
}

const form = $("#profile-form");
let editing = null;       // the saved profile being edited, or null for a new one

const DEFAULTS = { baud: 115200, dataBits: 8, parity: "none", stopBits: "1", flow: "none", enter: "cr", dtr: true, rts: true,
                   backspace: "del", bell: "visual", theme: "auto", fontSize: 14, cursor: "block", scrollback: 10000,
                   charset: "utf-8", logExisting: "append", warnClose: true,
                   homeEnd: "xterm", funcKeys: "xterm", bold: "colour", rightClick: "copypaste", autoWrap: true };

function quickProfile(device, baud) {
  return { ...DEFAULTS, name: device, device, baud, lineEdit: false, localEcho: false, group: "" };
}

function fillForm(formEl, values) {
  for (const field of formEl.elements) {
    if (!field.name) continue;
    if (field.type === "checkbox") field.checked = ["dtr", "rts", "warnClose", "autoWrap"].includes(field.name) ? values[field.name] !== false : !!values[field.name];
    else field.value = values[field.name] ?? "";
  }
}

function readForm(formEl) {
  const out = {};
  for (const field of formEl.elements) {
    if (!field.name) continue;
    out[field.name] = field.type === "checkbox" ? field.checked : field.value;
  }
  return out;
}

function resetDelete(button) {
  button.textContent = "Delete";
  delete button.dataset.sure;
}

// A second click confirms; no popup.
function askTwice(button, action) {
  button.addEventListener("click", () => {
    if (button.dataset.sure) { resetDelete(button); action(); return; }
    button.dataset.sure = "1";
    button.textContent = "Click again to delete";
    setTimeout(() => resetDelete(button), 4000);
  });
}

function editProfile(profile, prefill = {}) {
  editing = profile;
  $("#profile-title").textContent = profile ? `Settings of ${profile.name}` : "New profile";
  $("#profile-delete").hidden = !profile;
  resetDelete($("#profile-delete"));
  showText($("#profile-error"), "");
  fillForm(form, profile || { ...DEFAULTS, group: selectedGroup ?? "", ...prefill });
  $("#bell-file").value = "";
  $("#bell-name").textContent = form.elements.bellSound.value ? "custom sound set" : "";
  showPage("#profile-form");
  form.elements.name.focus();
}

function profileFromForm() {
  const p = readForm(form);
  for (const key of ["baud", "dataBits", "fontSize", "scrollback"]) p[key] = Number(p[key]);
  return p;
}

async function saveProfile() {
  const p = profileFromForm();
  const path = editing ? `/api/profiles/${encodeURIComponent(editing.name)}` : "/api/profiles";
  const saved = await api(path, { method: editing ? "PUT" : "POST", body: p });
  await loadProfiles();
  // Sessions already open follow the new settings; a connected one is reopened with them.
  if (editing) for (const t of tabs) if (t.profile.name === editing.name) applyProfile(t, saved);
  return saved;
}

// Give an open tab the saved settings: what is shown changes now, and a connected port is reopened.
function applyProfile(tab, p) {
  tab.profile = p;
  tab.label.textContent = p.name;
  tab.fontSize = p.fontSize || 14;
  const o = tab.term.options;
  o.fontSize = tab.fontSize;
  o.fontFamily = (p.fontFamily ? p.fontFamily + ", " : "") + MONO_FONTS;
  o.cursorStyle = p.cursor || "block";
  o.cursorBlink = !!p.cursorBlink;
  o.scrollback = p.scrollback || 10000;
  o.convertEol = !!p.implicitCR;
  o.theme = terminalTheme(p);
  Object.assign(o, boldOptions(p));
  renderToolbar(tab.pane);
  fitPane(tab.pane);
  if (tab.state === "connected") {
    note(tab, "settings saved: reopening the port");
    connect(tab);
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await saveProfile(); showLines(); } catch (error) { showText($("#profile-error"), error.message); }
});
$("#profile-connect").addEventListener("click", async () => {
  if (!form.reportValidity()) return;
  try {
    const saved = await saveProfile();
    const tab = tabs.find((t) => t.profile.name === saved.name);
    if (!tab) { showLines(); openTab(saved); return; }
    activate(tab);
    if (tab.state !== "connected") connect(tab);  // a connected one was already reopened by the save
    showLines();
  } catch (error) { showText($("#profile-error"), error.message); }
});
// A custom bell sound is read here and saved in the session as a data: URL.
$("#bell-file").addEventListener("change", () => {
  const file = $("#bell-file").files[0];
  if (!file) return;
  if (file.size > 300 * 1024) {
    showText($("#profile-error"), "The bell sound is limited to 300 KB.");
    $("#bell-file").value = "";
    return;
  }
  const reader = new FileReader();
  reader.onload = () => {
    form.elements.bellSound.value = reader.result;
    $("#bell-name").textContent = file.name;
  };
  reader.readAsDataURL(file);
});
$("#bell-clear").addEventListener("click", () => {
  form.elements.bellSound.value = "";
  $("#bell-file").value = "";
  $("#bell-name").textContent = "";
});

askTwice($("#profile-delete"), async () => {
  try {
    await api(`/api/profiles/${encodeURIComponent(editing.name)}`, { method: "DELETE" });
    await loadProfiles();
    showLines();
  } catch (error) { showText($("#profile-error"), error.message); }
});

// ---------------------------------------------------------------- scripts


async function loadScripts() {
  const data = await api("/api/scripts");
  scripts = data.scripts;
  renderTree();
}

function scriptSummary(s) {
  const loop = s.loop ? (s.count ? `, ${s.count} rounds` : ", loops") : "";
  return `${s.commands.length} command${s.commands.length === 1 ? "" : "s"}, ${s.delayMs} ms apart${loop}`;
}

const scriptForm = $("#script-form");
let editingScript = null;

function editScript(script, group) {
  editingScript = script;
  $("#script-title").textContent = script ? `Script ${script.name}` : "New script";
  $("#script-delete").hidden = !script;
  resetDelete($("#script-delete"));
  showText($("#script-error"), "");
  fillForm(scriptForm, script ? { ...script, commands: script.commands.join("\n") }
                             : { group: group ?? selectedGroup ?? "", delayMs: 1000, count: 0, loop: false });
  showPage("#script-form");
  scriptForm.elements.name.focus();
}

scriptForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const s = readForm(scriptForm);
  s.commands = s.commands.split(/\r?\n/);
  s.delayMs = Number(s.delayMs);
  s.count = Number(s.count);
  try {
    const path = editingScript ? `/api/scripts/${encodeURIComponent(editingScript.name)}` : "/api/scripts";
    await api(path, { method: editingScript ? "PUT" : "POST", body: s });
    await loadScripts();
    showLines();
  } catch (error) { showText($("#script-error"), error.message); }
});
askTwice($("#script-delete"), async () => {
  try {
    await api(`/api/scripts/${encodeURIComponent(editingScript.name)}`, { method: "DELETE" });
    await loadScripts();
    showLines();
  } catch (error) { showText($("#script-error"), error.message); }
});

function runScript(tab, name) {
  tab.ws?.send(JSON.stringify({ type: "script", name }));
}
function stopScript(tab) {
  tab.ws?.send(JSON.stringify({ type: "stop" }));
}

// ---------------------------------------------------------------- the lines of a tab

const MONO_FONTS = '"Cascadia Mono", "JetBrains Mono", Menlo, Consolas, monospace';
const ENTER = { cr: "\r", lf: "\n", crlf: "\r\n" };

// Colour lines "name=#rrggbb" of a profile, as xterm.js theme keys.
function colourOverrides(text) {
  const theme = {};
  for (const line of (text || "").split("\n")) {
    const [name, value] = line.split("=").map((x) => x.trim());
    if (name && /^#[0-9a-f]{6}$/i.test(value || "")) theme[name === "selection" ? "selectionBackground" : name] = value;
  }
  return theme;
}

function terminalTheme(profile) {
  const choice = profile?.theme || "auto";
  const light = choice === "light" || (choice === "auto" && matchMedia("(prefers-color-scheme: light)").matches);
  const base = light ? { background: "#ffffff", foreground: "#1b1f27", cursor: "#1b1f27", selectionBackground: "#d4e3fb" }
                     : { background: "#0f1218", foreground: "#e4e7ee", cursor: "#e4e7ee", selectionBackground: "#24406b" };
  return { ...base, ...colourOverrides(profile?.colours) };
}

// How bold text is drawn: a bright colour, a bold font, or both.
function boldOptions(profile) {
  const how = profile.bold || "colour";
  return { drawBoldTextInBrightColors: how !== "font", fontWeightBold: how === "colour" ? "normal" : "bold" };
}

let lastBeep = 0;
function bell(tab) {
  const mode = tab.profile.bell || "visual";
  if (mode === "visual" || mode === "both") {
    tab.box.classList.add("bell");
    setTimeout(() => tab.box.classList.remove("bell"), 150);
  }
  if (tab.profile.bellTab && tab.pane.active !== tab) {  // rung out of sight: the tab flashes until it is opened
    tab.belled = true;
    tab.tabEl.classList.add("belled");
  }
  if ((mode === "sound" || mode === "both") && Date.now() - lastBeep > 300) {  // a burst of bells is one beep
    lastBeep = Date.now();
    if (tab.profile.bellSound) {
      new Audio(tab.profile.bellSound).play().catch(() => {});
      return;
    }
    try {
      const audio = new AudioContext();
      const tone = audio.createOscillator();
      tone.frequency.value = 880;
      tone.connect(audio.destination);
      tone.start();
      tone.stop(audio.currentTime + 0.12);
      tone.onended = () => audio.close();
    } catch { /* no audio */ }
  }
}

// "Implicit LF in every CR": a CR that is not followed by LF becomes CR LF (across chunks too).
function addImplicitLF(tab, bytes) {
  const out = [];
  if (tab.afterCR && bytes.length && bytes[0] !== 10) out.push(10);
  tab.afterCR = false;
  for (let i = 0; i < bytes.length; i++) {
    out.push(bytes[i]);
    if (bytes[i] === 13) {
      if (i + 1 < bytes.length) { if (bytes[i + 1] !== 10) out.push(10); } else tab.afterCR = true;
    }
  }
  return Uint8Array.from(out);
}

// Received bytes, with the Rx marker at the start of each line.
const MOUSE_MODES = /\x1b\[\?(?:9|100[0-6]|1015|1016)[hl]/g;

function showRx(tab, bytes) {
  if (tab.profile.answerback && bytes.includes(5)) {  // ENQ: the device asks who we are
    for (const b of bytes) if (b === 5) send(tab, tab.profile.answerback);
  }
  if (tab.profile.noMouseReport && bytes.includes(0x1b)) {
    bytes = Uint8Array.from(Array.from(String.fromCharCode(...bytes).replace(MOUSE_MODES, ""), (c) => c.charCodeAt(0)));
  }
  if (tab.profile.implicitLF) bytes = addImplicitLF(tab, bytes);
  const marker = tab.profile.rxMarker;
  if (!marker) {
    tab.term.write(bytes);
    const last = bytes.lastIndexOf(10);
    if (last >= 0) tab.lineStart = last === bytes.length - 1;
    else if (bytes.length) tab.lineStart = false;
    return;
  }
  let start = 0;
  for (let i = 0; i < bytes.length; i++) {
    if (tab.lineStart) {
      if (i > start) tab.term.write(bytes.subarray(start, i));
      start = i;
      tab.term.write(marker);
      tab.lineStart = false;
    }
    if (bytes[i] === 10) {
      tab.term.write(bytes.subarray(start, i + 1));
      start = i + 1;
      tab.lineStart = true;
    }
  }
  if (start < bytes.length) tab.term.write(bytes.subarray(start));
}

// Text we send and show ourselves (typed lines, local echo, scripts), with the Tx marker.
function showTx(tab, text, finish) {
  const marker = tab.profile.txMarker;
  if (!tab.txOpen && !tab.lineStart) {
    tab.term.write("\r\n");
    tab.lineStart = true;
  }
  if (tab.lineStart && marker) tab.term.write(marker);
  tab.lineStart = false;
  tab.txOpen = true;
  tab.term.write(text);
  if (finish) {
    tab.term.write("\r\n");
    tab.lineStart = true;
    tab.txOpen = false;
  }
}

function note(tab, text) {
  if (!tab.lineStart) tab.term.write("\r\n");
  tab.term.write(`\x1b[2m[termdeck] ${text}\x1b[0m\r\n`);
  tab.lineStart = true;
  tab.txOpen = false;
}

// ---------------------------------------------------------------- tabs

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
                 empty: root.querySelector(".empty"), tabs: [], active: null };
  if (panes.length === 0) {  // the first pane carries the button for the left column and the layout buttons
    root.querySelector(".sidebar-toggle").id = "sidebar-toggle";
    layoutEl = root.querySelector(".layout");
    for (const button of root.querySelectorAll("[data-layout]")) button.addEventListener("click", () => setLayout(button.dataset.layout));
  } else {
    root.querySelector(".sidebar-toggle").remove();
    root.querySelector(".layout").remove();
  }
  root.addEventListener("mousedown", () => focus(pane), true);
  root.addEventListener("focusin", () => focus(pane));
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

function focus(pane) {
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
  }
  pane.empty.hidden = pane.tabs.length > 0;
}

function activate(tab) {
  const pane = tab.pane;
  pane.active = tab;
  focusPane = pane;
  active = tab;
  tab.belled = false;
  tab.tabEl.classList.remove("belled");
  refreshPane(pane);
  for (const p of panes) p.el.classList.toggle("focused", p === pane);
  renderToolbars();
  renderTree();
  requestAnimationFrame(() => { fitPane(pane); tab.term.focus(); });
}

// Control characters in pasted text are dropped (line breaks and tabs stay), unless the profile allows them.
function pasteText(tab, text) {
  handleInput(tab, tab.profile.pasteControls ? text : text.replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, ""));
}
function pasteFromClipboard(tab) {
  navigator.clipboard.readText().then((text) => pasteText(tab, text)).catch(() => {});
}

// The screen modes a profile wants from the start (also after a reset).
function applyModes(tab) {
  const p = tab.profile;
  let modes = "";
  if (p.appCursor) modes += "\x1b[?1h";
  if (p.appKeypad) modes += "\x1b=";
  if (p.autoWrap === false) modes += "\x1b[?7l";
  if (p.originMode) modes += "\x1b[?6h";
  tab.appKeypad = !!p.appKeypad;
  if (modes) tab.term.write(modes);
}

const portName = (device) => device.replace(/^\\\\\.\\/, "").toLowerCase();

// The same session, or the same port, is never opened twice: its tab is shown instead.
function openTab(profile) {
  const sameSession = tabs.find((t) => t.profile.name === profile.name);
  const samePort = tabs.find((t) => (t.state === "connected" || t.state === "connecting") && portName(t.profile.device) === portName(profile.device));
  const existing = sameSession || samePort;
  if (existing) {
    activate(existing);
    showLines();
    const why = sameSession ? `${profile.name} is already open` : `${profile.device} is already open in the tab ${existing.profile.name}`;
    tell(existing.state === "connected" ? `${why}: showing it.` : `${why}: showing it (use Open port to connect again).`);
    return;
  }
  const pane = focusPane || panes[0];
  const tab = { profile, pane, state: "connecting", line: "", history: [], historyAt: 0, ws: null,
                lineStart: true, txOpen: false, script: null };
  tab.box = el("div", "term");
  pane.terminals.append(tab.box);
  tab.fontSize = profile.fontSize || 14;
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
  tab.term.onData((data) => handleInput(tab, data));
  tab.appKeypad = !!profile.appKeypad;
  tab.term.parser.registerEscHandler({ final: "=" }, () => { tab.appKeypad = true; return false; });
  tab.term.parser.registerEscHandler({ final: ">" }, () => { tab.appKeypad = false; return false; });
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
      if (e.code === "KeyC") { navigator.clipboard.writeText(tab.term.getSelection()).catch(() => {}); return false; }
      if (e.code === "KeyV") { pasteFromClipboard(tab); return false; }
      if (e.code === "KeyL") { tab.term.clear(); return false; }
      return true;
    }
    // function, edit and keypad keys in the style the profile asks for
    const sequence = Keys.map(e, tab.profile, { appKeypad: tab.appKeypad, appCursor: tab.term.modes.applicationCursorKeysMode });
    if (sequence === null) return true;
    e.preventDefault();
    send(tab, sequence);
    return false;
  });
  // Pasting (menu, Ctrl+V, middle click) goes through one place, which can remove control characters.
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
  applyModes(tab);
  tab.label = el("span", "label", profile.name);
  tab.dot = el("span", "dot");
  const close = el("button", "close", "✕");
  close.setAttribute("aria-label", `Close ${profile.name}`);
  tab.closeBtn = close;
  close.addEventListener("click", (e) => { e.stopPropagation(); requestClose(tab); });
  tab.tabEl = el("div", "tab");
  tab.tabEl.setAttribute("role", "tab");
  tab.tabEl.draggable = true;
  tab.tabEl.append(tab.dot, tab.label, close);
  tab.tabEl.addEventListener("click", () => { activate(tab); showLines(); });
  addReorder(tab);
  pane.tabsEl.append(tab.tabEl);
  pane.tabs.push(tab);
  tabs.push(tab);
  activate(tab);
  showLines();
  connect(tab);
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
  tab.ws?.close();
  tab.term.dispose();
  tab.box.remove();
  tab.tabEl.remove();
  tabs.splice(tabs.indexOf(tab), 1);
  pane.tabs.splice(pane.tabs.indexOf(tab), 1);
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
    if (typeof event.data !== "string") return showRx(tab, new Uint8Array(event.data));
    const m = JSON.parse(event.data);
    if (m.type === "state") {
      setState(tab, m.state);
      if (m.state === "failed" || m.state === "ended") note(tab, m.message || m.state);
      else if (m.state === "connected") note(tab, `connected to ${summary(tab.profile)}`);
      else if (m.state === "closed") note(tab, "port closed");
    } else if (m.type === "break") {
      note(tab, m.ok ? "break sent" : `break failed: ${m.message}`);
    } else if (m.type === "logging") {
      tab.logging = m.on;
      if (tab === tab.pane.active) renderToolbar(tab.pane);
    } else if (m.type === "logexists") {
      askLog(tab, m.path);
    } else if (m.type === "lines") {
      tab.lines = m;
      if (tab === tab.pane.active) renderToolbar(tab.pane);
    } else if (m.type === "tx") {
      showTx(tab, m.data, true);
    } else if (m.type === "notice") {
      note(tab, m.text);
    } else if (m.type === "script") {
      tab.script = m.state === "running" ? m.name : null;
      if (m.state === "error") note(tab, `script ${m.name}: ${m.message}`);
      else if (m.state !== "running") note(tab, `script ${m.name} ${m.state}`);
      if (tab === tab.pane.active) { renderToolbar(tab.pane); if (tab === active) renderTree(); }
    }
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
  const answer = (value) => tab.ws?.send(JSON.stringify({ type: "loganswer", answer: value }));
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

const STATUS = { connecting: "Connecting…", connected: "Connected", failed: "Failed", ended: "Disconnected", closed: "Port closed" };

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
  if (!tab) return;
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
  portButton.textContent = tab.state === "connected" ? "Close port" : "Open port";
  portButton.disabled = tab.state === "connecting";
}
const renderToolbars = () => panes.forEach(renderToolbar);

focusPane = createPane();
focusPane.el.classList.add("focused");
try { const saved = localStorage.getItem("termdeck.layout"); if (saved === "cols" || saved === "rows") setLayout(saved); } catch { /* no storage */ }

// ---------------------------------------------------------------- input

function send(tab, data) {
  if (tab.ws?.readyState === WebSocket.OPEN && tab.state === "connected") {
    tab.ws.send(JSON.stringify({ type: "input", data }));
  }
}

function eraseLine(tab) {
  tab.term.write("\b \b".repeat([...tab.line].length));
  tab.line = "";
}

function handleInput(tab, data) {
  const p = tab.profile;
  const enter = ENTER[p.enter] || "\r";
  if (!p.lineEdit) {
    if (p.localEcho) {
      for (const ch of data) {
        if (ch === "\r") showTx(tab, "", true);
        else if (ch >= " ") showTx(tab, ch, false);
      }
    }
    send(tab, data.replace(/\r/g, enter).replace(/\x7f/g, p.backspace === "bs" ? "\b" : "\x7f"));
    return;
  }
  // Local line editing: the line is kept here and sent on Enter.
  if (data === "\x1b[A" || data === "\x1b[B") {
    if (!tab.history.length) return;
    tab.historyAt = Math.min(Math.max(tab.historyAt + (data === "\x1b[A" ? -1 : 1), 0), tab.history.length);
    eraseLine(tab);
    tab.line = tab.history[tab.historyAt] || "";
    if (tab.line) showTx(tab, tab.line, false);
    return;
  }
  if (data.startsWith("\x1b")) return;  // other keys (arrows, function keys) do nothing in a local line
  for (const ch of data) {
    if (ch === "\r" || ch === "\n") {
      showTx(tab, "", true);
      if (tab.line && tab.history[tab.history.length - 1] !== tab.line) tab.history.push(tab.line);
      tab.historyAt = tab.history.length;
      send(tab, tab.line + enter);
      tab.line = "";
    } else if (ch === "\x7f" || ch === "\b") {
      if (tab.line) {
        tab.line = [...tab.line].slice(0, -1).join("");
        tab.term.write("\b \b");
      }
    } else if (ch === "\x15") {
      eraseLine(tab);
    } else if (ch === "\x17") {
      const kept = tab.line.replace(/\s*\S*$/, "");
      tab.term.write("\b \b".repeat([...tab.line].length - [...kept].length));
      tab.line = kept;
    } else if (ch < " ") {
      send(tab, ch);  // Ctrl+C and the like go straight to the device
    } else {
      tab.line += ch;
      showTx(tab, ch, false);
    }
  }
}

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
try { if (localStorage.getItem("termdeck.sidebar") === "0") setSidebar(false); } catch { /* no storage */ }

// ---------------------------------------------------------------- toolbar and keys

function onToolbar(pane, e) {
  const action = e.target.closest("[data-action]")?.dataset.action;
  const tab = pane.active;
  if (!action || !tab) return;
  if (action === "port") {
    if (tab.state === "connected") tab.ws.send(JSON.stringify({ type: "close" }));
    else connect(tab);
  } else if (action === "line-dtr" || action === "line-rts") {
    const line = action.slice(5);
    const value = !(tab.lines?.[line] ?? tab.profile[line] !== false);
    tab.lines = { ...(tab.lines || {}), [line]: value };  // shown at once; the device's own lines follow
    tab.ws?.send(JSON.stringify({ type: "line", line, value }));
    renderLines(pane);
  } else if (action === "break") tab.ws?.send(JSON.stringify({ type: "break" }));
  else if (action === "clear") tab.term.clear();
  else if (action === "reset") { tab.term.reset(); tab.lineStart = true; tab.txOpen = false; applyModes(tab); }
  else if (action === "log") tab.ws?.send(JSON.stringify({ type: "log", value: !tab.logging }));
  else if (action === "settings") {
    const saved = profiles.find((p) => p.name === tab.profile.name);
    editProfile(saved || null);
    if (!saved) fillForm(form, tab.profile);  // a quick-connect tab: start from its settings
    return;
  }
  else if (action === "stop-script") stopScript(tab);
  else if (action === "font-up" || action === "font-down") {
    tab.fontSize = Math.min(Math.max(tab.fontSize + (action === "font-up" ? 1 : -1), 6), 72);
    tab.term.options.fontSize = tab.fontSize;
    tab.fit.fit();
  }
  tab.term.focus();
}

addEventListener("resize", fitAll);
matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
  for (const t of tabs) t.term.options.theme = terminalTheme(t.profile);
});
document.addEventListener("keydown", (e) => {
  if (!(e.altKey && e.shiftKey)) return;
  const list = focusPane.tabs;
  const index = list.indexOf(active);
  if (e.code === "KeyN") { e.preventDefault(); editProfile(null); }
  else if (e.code === "KeyB") { e.preventDefault(); toggleSidebar(); }
  else if (e.code === "KeyS") { e.preventDefault(); setLayout({ single: "cols", cols: "rows", rows: "single" }[layout]); }
  else if (e.code === "KeyW" && active) { e.preventDefault(); requestClose(active); }
  else if ((e.code === "PageUp" || e.code === "PageDown") && list.length) {
    e.preventDefault();
    activate(list[(index + (e.code === "PageDown" ? 1 : -1) + list.length) % list.length]);
    showLines();
  } else if ((e.code === "ArrowLeft" || e.code === "ArrowRight") && active) {  // move the tab in its pane
    e.preventDefault();
    moveTab(active, focusPane, index + (e.code === "ArrowRight" ? 1 : -1));
    active.tabEl.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
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

login().then(() => { checkServer(); return Promise.all([loadProfiles(), loadGroups(), loadPorts(), loadScripts()]); }).catch((error) => {
  document.body.replaceChildren(el("p", "problem", error.message));
});
