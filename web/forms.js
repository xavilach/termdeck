"use strict";

// The dialogs that edit settings (of Generic, a device or a port) and scripts.
//
// Settings come in layers: Generic, then a device, then a port's own changes. The dialog edits one layer: it
// shows every value (the ones from the layers below in grey, with where they come from) and saves only
// what differs from the layers below.

const form = $("#profile-form");
let editing = null;       // what the dialog edits: { kind: "generic" | "device" | "port", name, port, serial, base, own, assign }

const NUMBERS_OF_FORM = ["baud", "dataBits", "fontSize", "scrollback", "linePauseMs", "charDelayMs", "lineDelayMs", "rtsDelayMs", "logMaxMB", "logKeep"];
const ON_WHEN_UNSET = ["dtr", "rts", "warnClose", "autoWrap"];  // these are on unless a layer turns them off
const NOT_SETTINGS = ["name", "device"];                          // the fields that say what is edited, not how

function fillForm(formEl, values) {
  for (const field of formEl.elements) {
    if (!field.name) continue;
    if (field.type === "checkbox") field.checked = ON_WHEN_UNSET.includes(field.name) ? values[field.name] !== false : !!values[field.name];
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

// A value as the form holds it, so that a value of a layer and a value of the form compare.
function formValue(name, value) {
  if (ON_WHEN_UNSET.includes(name)) return value !== false;
  if (NUMBERS_OF_FORM.includes(name)) return Number(value);
  if (typeof value === "boolean") return value;
  return value ?? "";
}

function settingsOfForm() {
  const out = {};
  for (const [name, value] of Object.entries(readForm(form))) if (!NOT_SETTINGS.includes(name)) out[name] = formValue(name, value);
  return out;
}

// The settings of the form that differ from the layers below: what the edited layer holds.
function ownSettings() {
  const own = {};
  for (const [name, value] of Object.entries(settingsOfForm())) if (formValue(name, editing.base[name]) !== value) own[name] = value;
  return own;
}

function resetDelete(button) {
  button.textContent = button.dataset.label || "Delete";
  delete button.dataset.sure;
}

// A second click confirms; no popup.
function askTwice(button, action) {
  button.addEventListener("click", () => {
    if (button.dataset.sure) { resetDelete(button); action(); return; }
    button.dataset.sure = "1";
    button.textContent = "Click again to confirm";
    setTimeout(() => resetDelete(button), 4000);
  });
}

// Where the values of the layers below come from, as the field shows it.
const belowName = () => ({ generic: "the defaults", device: "Generic", port: editing.device }[editing.kind]);

// Each field says whether the edited layer sets it (with a button back to the value below) or takes it from below.
function markLayers() {
  for (const field of form.elements) {
    if (!field.name || NOT_SETTINGS.includes(field.name) || field.type === "file") continue;
    const label = field.closest("label");
    if (!label) continue;
    const own = formValue(field.name, editing.base[field.name]) !== formValue(field.name, field.type === "checkbox" ? field.checked : field.value);
    label.classList.toggle("own", own);
    label.classList.toggle("inherited", !own);
    let reset = label.querySelector(".reset");
    if (!reset) {
      reset = el("button", "reset", "↺");
      reset.type = "button";
      reset.addEventListener("click", (e) => {
        e.preventDefault();
        fillField(field, editing.base[field.name]);
        markLayers();
      });
      label.append(reset);
    }
    reset.hidden = !own;
    reset.title = `Back to the value of ${belowName()}`;
    reset.setAttribute("aria-label", `${field.name}: back to the value of ${belowName()}`);
    label.dataset.from = own ? "" : belowName();
  }
}

function fillField(field, value) {
  if (field.type === "checkbox") field.checked = ON_WHEN_UNSET.includes(field.name) ? value !== false : !!value;
  else field.value = value ?? "";
}

// Show the dialog for a layer: base is the merge of the layers below, own the layer itself.
function showLayer(what, base, own, title, note) {
  editing = { ...what, base, own };
  $("#profile-title").textContent = title;
  $("#layer-note").textContent = note;
  form.classList.toggle("editing-device", what.kind === "device");
  form.classList.toggle("editing-port", what.kind === "port");
  form.elements.name.required = what.kind === "device";
  const remove = $("#profile-delete");
  remove.dataset.label = what.kind === "port" ? "Forget this port" : "Delete";
  remove.hidden = !(what.kind === "port" ? entryOf(what.port) : what.kind === "device" && what.name);
  resetDelete(remove);
  $("#profile-move").hidden = !(what.kind === "port" && what.device !== GENERIC);
  $("#profile-connect").hidden = !(what.kind === "port" || what.assign);
  $("#preset").value = "";
  showText($("#profile-error"), "");
  showFormPage("port");
  fillForm(form, { ...base, ...own, name: what.kind === "device" ? what.name || "" : "", device: what.port || "" });
  $("#bell-file").value = "";
  $("#bell-name").textContent = form.elements.bellSound.value ? "custom sound set" : "";
  markLayers();
  showPage("#profile-form");
  (what.kind === "device" ? form.elements.name : form.querySelector(".form-page:not([hidden]) input, .form-page:not([hidden]) select"))?.focus();
}

const merge = (...layers) => Object.assign({}, ...layers);

// Edit Generic or a device (null: a new one, prefilled with prefill.name and prefill.settings).
async function editDevice(device, prefill = {}) {
  try {
    const layers = await api(`/api/layers?device=${encodeURIComponent(device?.name || "")}`);
    if (device?.name === GENERIC) {
      return showLayer({ kind: "generic", name: GENERIC }, layers.defaults, layers.generic, "Generic settings",
        "What every port starts from. A device changes some of them, and a port changes some more.");
    }
    const own = device ? layers.device.settings : prefill.settings || {};
    showLayer({ kind: "device", name: device?.name || "", assign: prefill.assign }, merge(layers.defaults, layers.generic), own,
      device ? `Device ${device.name}` : "New device", "What this device speaks. Ports opened with it start from these settings; the grey ones come from Generic.");
    if (!device && prefill.name) form.elements.name.value = prefill.name;
  } catch (error) { showText($("#problem"), error.message); }
}

// Edit the own changes of a port, over the device it opens with.
async function editPort(port) {
  try {
    const serial = portDetails[port]?.serial || "";
    const layers = await api(`/api/layers?port=${encodeURIComponent(port)}&serial=${encodeURIComponent(serial)}`);
    if (layers.error) throw new Error(layers.error);
    const device = layers.device.name;
    showLayer({ kind: "port", port, serial, device }, merge(layers.defaults, layers.generic, layers.device.settings), layers.port.settings,
      `Port ${port}`, `Opens with ${device}. What you change here is this port's own; the grey values come from ${device === GENERIC ? "Generic" : `the device ${device}, then Generic`}.`);
  } catch (error) { showText($("#problem"), error.message); }
}

// A new device made of the changes of a port: the port then opens with it, without changes of its own.
async function newDeviceFromPort(port) {
  const entry = entryOf(port);
  await editDevice(null, { name: "", settings: entry?.settings || {}, assign: port });
}

// Save the edited layer. It returns the port to open, when the dialog was about one.
async function saveLayer() {
  const own = ownSettings();
  if (editing.kind === "generic") {
    await api(`/api/devices/${GENERIC}`, { method: "PUT", body: { name: GENERIC, settings: own } });
  } else if (editing.kind === "device") {
    const name = form.elements.name.value.trim();
    const path = editing.name ? `/api/devices/${encodeURIComponent(editing.name)}` : "/api/devices";
    await api(path, { method: editing.name ? "PUT" : "POST", body: { name, settings: own } });
    if (editing.assign) await api("/api/portentries", { method: "PUT", body: { port: editing.assign, serial: portDetails[editing.assign]?.serial || "", device: name, settings: {} } });
  } else {
    await api("/api/portentries", { method: "PUT", body: { ...entryOf(editing.port), port: editing.port, serial: editing.serial, device: editing.device === GENERIC ? "" : editing.device, settings: own } });
  }
  await Promise.all([loadDevices(), loadPortEntries(), loadScripts()]);
  await refreshTabs();
  return editing.kind === "port" ? editing.port : editing.assign;
}

// The tabs follow the settings saved: each asks the server again for the merge of its layers.
async function refreshTabs() {
  for (const tab of tabs) {
    if (!tab.port) continue;  // a tunnel has its settings in its tab only
    try {
      const layers = await api(`/api/layers?port=${encodeURIComponent(tab.port)}&serial=${encodeURIComponent(portDetails[tab.port]?.serial || "")}`);
      if (layers.profile && JSON.stringify(layers.profile) !== JSON.stringify(tab.profile)) applyProfile(tab, layers.profile, layers.device.name);
    } catch { /* the tab keeps its settings */ }
  }
}

// Give an open tab new settings: what is shown changes now, and a connected port is reopened.
function applyProfile(tab, p, device = tab.device) {
  tab.profile = p;
  tab.device = device;
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
  tab.view = viewOf(p);
  syncBars(tab.pane);
  sendMessage(tab, { type: "profile", profile: p });
  redraw(tab);
  renderToolbar(tab.pane);
  fitPane(tab.pane);
  if (tab.state === "connected") {
    note(tab, "settings saved: reopening the port");
    connect(tab);
  }
}

form.addEventListener("input", () => { if (editing) markLayers(); });
form.addEventListener("change", () => { if (editing) markLayers(); });
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!formIsValid()) return;
  try { await saveLayer(); showLines(); } catch (error) { showText($("#profile-error"), error.message); }
});
$("#profile-connect").addEventListener("click", async () => {
  if (!formIsValid()) return;
  try {
    const port = await saveLayer();
    showLines();
    const tab = tabs.find((t) => t.port && portName(t.port) === portName(port));
    if (!tab) return openPort(port);
    activate(tab);
    if (tab.state !== "connected") connect(tab);  // a connected one was already reopened by the save
  } catch (error) { showText($("#profile-error"), error.message); }
});
// The changes of a port become the settings of its device.
$("#profile-move").addEventListener("click", async () => {
  try {
    const device = devices.find((d) => d.name === editing.device);
    await api(`/api/devices/${encodeURIComponent(device.name)}`, { method: "PUT", body: { name: device.name, settings: { ...device.settings, ...ownSettings() } } });
    await api("/api/portentries", { method: "PUT", body: { ...entryOf(editing.port), port: editing.port, serial: editing.serial, device: device.name, settings: {} } });
    await Promise.all([loadDevices(), loadPortEntries()]);
    await refreshTabs();
    editPort(editing.port);
  } catch (error) { showText($("#profile-error"), error.message); }
});
let presetList = [];
$("#preset").addEventListener("focus", async () => {
  if (presetList.length) return;
  try {
    presetList = await api("/api/presets");
    $("#preset").append(...presetList.map((p, i) => { const o = el("option", "", `${p.name}: ${p.what}`); o.value = String(i); return o; }));
  } catch { /* no presets */ }
});
$("#preset").addEventListener("change", () => {
  const preset = presetList[Number($("#preset").value)];
  if (!preset) return;
  for (const [name, value] of Object.entries(preset.settings)) if (form.elements[name]) fillField(form.elements[name], value);
  markLayers();
  showText($("#profile-error"), `${preset.name}: ${Object.keys(preset.settings).join(", ")} filled in. Save to keep them.`);
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
    if (editing.kind === "port") await api(`/api/portentries/${encodeURIComponent(entryOf(editing.port).port)}`, { method: "DELETE" });
    else await api(`/api/devices/${encodeURIComponent(editing.name)}`, { method: "DELETE" });
    await Promise.all([loadDevices(), loadPortEntries()]);
    await refreshTabs();
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

function editScript(script, device) {
  editingScript = script;
  $("#script-title").textContent = script ? `Script ${script.name}` : "New script";
  $("#script-delete").hidden = !script;
  resetDelete($("#script-delete"));
  showText($("#script-error"), "");
  fillForm(scriptForm, script ? { ...script, commands: script.commands.join("\n") }
                             : { device: device ?? (selectedDevice === GENERIC ? "" : selectedDevice) ?? "", delayMs: 1000, count: 0, loop: false });
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
  sendMessage(tab, { type: "script", name });
}
function stopScript(tab) {
  const job = tab.jobs.find((j) => j.kind === "script");
  if (job) sendMessage(tab, { type: "stopjob", job: job.id });
}

// Desktop notifications need the browser's permission, asked on a click.
function showNotificationState() {
  const state = typeof Notification === "undefined" ? "not available in this browser" : { granted: "allowed", denied: "blocked in the browser settings", default: "not asked yet" }[Notification.permission];
  $("#notification-state").textContent = state;
}
showNotificationState();
$("#allow-notifications").addEventListener("click", async () => {
  if (typeof Notification !== "undefined") await Notification.requestPermission();
  showNotificationState();
});

// The settings of a session are on pages: Port, Receive, Send, Log, Display.
function showFormPage(name) {
  for (const page of form.querySelectorAll(".form-page")) page.hidden = page.dataset.formPage !== name;
  for (const button of form.querySelectorAll(".form-tabs button")) button.setAttribute("aria-pressed", String(button.dataset.formPage === name));
}
form.querySelectorAll(".form-tabs button").forEach((button) => button.addEventListener("click", () => showFormPage(button.dataset.formPage)));
// The browser cannot focus a field on a page that is not shown, so the form checks itself:
// it shows the page of the first field that is not valid and reports that field.
form.noValidate = true;
function formIsValid() {
  const invalid = form.querySelector(":invalid");
  if (!invalid) return true;
  showFormPage(invalid.closest(".form-page")?.dataset.formPage || "port");
  invalid.reportValidity();
  return false;
}

