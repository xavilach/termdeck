"use strict";

// The left column: the ports (each with the device it opens with), then the devices with their scripts and
// the ports remembered with them.

// ---------------------------------------------------------------- the tree on the left

let portNames = [];
let virtualPortList = [];
let portDetails = {};  // what each port is (the USB product, its serial number), by port name
let selectedDevice = null;     // the device "+ Script" creates in
let selectedScript = null;
const GENERIC = "Generic";
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

// A short message where the action happened: in the lines of the current tab, or in the bottom bar when none is open.
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

// ---- ports and their device

// What termdeck remembers about a port: found by its adapter's serial number first, then by its name.
function entryOf(port) {
  const serial = portDetails[port]?.serial;
  return (serial && portEntries.find((e) => e.serial === serial)) || portEntries.find((e) => portName(e.port) === portName(port)) || null;
}

const deviceOf = (port) => entryOf(port)?.device || GENERIC;

// Open a port with a device (its last one when none is given): the server merges the layers of its settings.
async function openPort(port, device) {
  try {
    const serial = portDetails[port]?.serial || "";
    const layers = await api(`/api/layers?port=${encodeURIComponent(port)}&serial=${encodeURIComponent(serial)}&device=${encodeURIComponent(device || "")}`);
    if (layers.error) throw new Error(layers.error);
    const name = layers.device.name === GENERIC ? "" : layers.device.name;
    const entry = layers.port;
    if (!entryOf(port) || (entry.device || "") !== name || portName(entry.port) !== portName(port)) {
      await api("/api/portentries", { method: "PUT", body: { ...entry, port, serial, device: name } });  // the device it opens with next time
      await loadPortEntries();
    }
    openTab(layers.profile, { port, device: layers.device.name });
  } catch (error) { tell(`Cannot open ${port}: ${error.message}`); }
}

// Another device for a port: remembered, and an open tab of the port follows it.
async function chooseDevice(port, device) {
  if (device === "+new") return newDeviceFromPort(port);
  const entry = entryOf(port) || { port, settings: {} };
  try {
    await api("/api/portentries", { method: "PUT", body: { ...entry, port, serial: portDetails[port]?.serial || "", device: device === GENERIC ? "" : device } });
    await loadPortEntries();
    await refreshTabs();
  } catch (error) { showText($("#problem"), error.message); }
}

// A select of the devices for a port: Generic, the devices, and a new one made of the port's changes.
function deviceChooser(port) {
  const select = el("select", "device-choice");
  select.setAttribute("aria-label", `Device of ${port}`);
  select.title = "The device this port opens with: its settings come first, then the port's own changes";
  for (const d of devices) {
    const option = el("option", "", d.name);
    option.value = d.name;
    select.append(option);
  }
  const make = el("option", "", "New device…");
  make.value = "+new";
  select.append(make);
  select.value = deviceOf(port);
  select.addEventListener("click", (e) => e.stopPropagation());
  select.addEventListener("dblclick", (e) => e.stopPropagation());
  select.addEventListener("change", () => chooseDevice(port, select.value));
  return select;
}

function portRow(port, detail, title) {
  const row = el("div", "item port");
  row.tabIndex = 0;
  const line = el("span", "port-line");
  line.append(el("span", "name", port), deviceChooser(port));
  row.append(line);
  if (detail) row.append(el("span", "detail", detail));
  row.title = `${title}Double-click to open it with its device; right-click for its settings`;
  row.addEventListener("click", () => selectRow(row, () => {}));
  row.addEventListener("dblclick", () => openPort(port));
  row.addEventListener("keydown", (e) => {
    if (e.target !== row) return;
    if (e.key === "Enter") openPort(port);
    else if (e.key === "F2") editPort(port);
  });
  row.addEventListener("contextmenu", (e) => { e.preventDefault(); editPort(port); });
  return row;
}

// ---- devices

function scriptRow(sc) {
  const row = el("div", "item");
  const running = active?.script === sc.name;
  const text = el("span", "text");
  text.append(el("span", "name", sc.name), el("span", "detail", scriptSummary(sc)));
  const canRun = running || active?.state === "connected";
  const play = () => {
    if (running) return stopScript(active);
    if (canRun) return runScript(active, sc.name);
    tell(`Script ${sc.name} not run: ${active ? "the port of the current tab is not open" : "no port is open"}.`);
  };
  if (selectedScript === sc.name) row.classList.add("selected");
  row.append(text, toolButton(running ? "■" : "▶", running ? "Stop the script" : "Run on the current tab", play, !canRun),
    toolButton("✎", `Edit ${sc.name}`, () => editScript(sc)));
  row.addEventListener("click", () => selectRow(row, () => { selectedScript = sc.name; }));
  row.addEventListener("dblclick", play);
  row.addEventListener("contextmenu", (e) => { e.preventDefault(); editScript(sc); });
  return row;
}

// A port remembered with a device, under the device.
function rememberedRow(entry, device) {
  const row = el("div", "item");
  const text = el("span", "text");
  const changes = Object.keys(entry.settings || {}).length;
  text.append(el("span", "name", entry.port), el("span", "detail", changes ? `${changes} change${changes === 1 ? "" : "s"} of its own` : "the device's settings"));
  row.append(text, toolButton("✎", `Settings of ${entry.port}`, () => editPort(entry.port)));
  row.title = "Double-click to open it with this device; right-click for its settings";
  row.addEventListener("click", () => selectRow(row, () => {}));
  row.addEventListener("dblclick", () => openPort(entry.port, device));
  row.addEventListener("contextmenu", (e) => { e.preventDefault(); editPort(entry.port); });
  return row;
}

// Each device has its own colour (Generic and the ports are grey), so devices are easy to tell apart.
function deviceColor(name) {
  if (name === GENERIC) return "var(--control)";
  let hash = 0;
  for (const ch of name) hash = (hash * 31 + ch.codePointAt(0)) % 360;
  return `hsl(${hash} 55% 52%)`;
}

function deviceNode(device) {
  const name = device.name;
  const generic = name === GENERIC;
  const stored = generic ? "" : name;
  const mineScripts = scripts.filter((s) => (s.device || "") === stored);
  const minePorts = portEntries.filter((e) => (e.device || "") === stored);
  const settings = Object.keys(device.settings || {}).length;
  const tools = [toolButton("✎", `Settings of ${name}`, () => editDevice(device))];
  if (!generic) tools.push(toolButton("❐", `A new device starting from ${name}`, () => editDevice(null, { name: `${name} copy`, settings: device.settings })));
  const select = () => { selectedDevice = name; renderTree(); };
  const { wrap, head } = node(`d:${name}`, name, "group", tools, (body) => {
    body.append(el("div", "empty-note", generic ? `what every port starts from · ${settings} change${settings === 1 ? "" : "s"}` : `${settings} setting${settings === 1 ? "" : "s"}`));
    const portsNode = node(`d:${name}/p`, `Ports · ${minePorts.length}`, "sub",
      [toolButton("▶▶", `Open all ${minePorts.length} ports of ${name}`, () => minePorts.forEach((e) => openPort(e.port, name)), minePorts.length < 2)], (inner) => {
        for (const e of minePorts) inner.append(rememberedRow(e, name));
        if (!minePorts.length) inner.append(el("div", "empty-note", "none yet"));
      });
    const scriptsNode = node(`d:${name}/c`, `Scripts · ${mineScripts.length}`, "sub",
      [toolButton("+", `New script for ${name}`, () => editScript(null, stored))], (inner) => {
        for (const sc of mineScripts) inner.append(scriptRow(sc));
        if (!mineScripts.length) inner.append(el("div", "empty-note", "none yet"));
      });
    body.append(portsNode.wrap, scriptsNode.wrap);
  }, select);
  wrap.classList.add("group-node");
  wrap.style.setProperty("--gc", deviceColor(name));
  if (selectedDevice === name) head.classList.add("selected");
  head.title = generic ? "The settings every port starts from (click to select)" : "Click to select the device";
  head.addEventListener("contextmenu", (e) => { e.preventDefault(); editDevice(device); });
  return wrap;
}

// The empty pane says what to do, and offers the virtual ports to try when the computer has no serial port.
function renderEmpty() {
  for (const pane of panes) {
    const hint = pane.empty.querySelector("[data-empty-try]");
    if (!hint) continue;
    const show = portNames.length === 0 && portEntries.length === 0 && virtualPortList.length > 0;
    hint.hidden = !show;
    hint.querySelectorAll("button").forEach((b) => b.remove());
    if (!show) continue;
    for (const name of ["fake:shell", "fake:counter", "fake:echo"]) {
      const button = el("button", "", name);
      button.addEventListener("click", () => openPort(name));
      hint.append(button);
    }
  }
}

function renderTree() {
  renderEmpty();
  const ports = node("ports", `Serial ports (${portNames.length})`, "group",
    [toolButton("⟳", "List the ports again (this also happens every few seconds)", () => loadPorts())], (body) => {
      for (const port of portNames) body.append(portRow(port, portDetails[port]?.info, portDetails[port] ? `${portDetails[port].info}. ` : ""));
      if (!portNames.length) body.append(el("div", "empty-note", "none found"));
    });
  ports.wrap.classList.add("group-node");
  ports.wrap.style.setProperty("--gc", "var(--control)");
  const virtual = node("virtual", `Virtual ports (${virtualPortList.length})`, "group", [], (body) => {
    for (const port of virtualPortList) body.append(portRow(port.name, port.what, `${port.what}. `));
  });
  virtual.wrap.classList.add("group-node");
  virtual.wrap.style.setProperty("--gc", "var(--control)");
  $("#port-tree").replaceChildren(ports.wrap, virtual.wrap);
  const head = el("div", "devices-head");
  head.append(el("span", "", "Devices"), toolButton("+", "New device", () => editDevice(null)));
  $("#tree").replaceChildren(head, ...devices.map(deviceNode));
  $("#devices").replaceChildren(...devices.map((d) => { const o = el("option"); o.value = d.name; return o; }));
}

async function loadDevices() {
  const data = await api("/api/devices");
  devices = data.devices;
  showText($("#problem"), data.error);
  renderTree();
}

async function loadPortEntries() {
  const data = await api("/api/portentries");
  portEntries = data.ports;
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
