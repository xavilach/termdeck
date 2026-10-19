"use strict";

// The dialogs that edit a session or a script.

const form = $("#profile-form");
let editing = null;       // the saved profile being edited, or null for a new one

const DEFAULTS = { baud: 115200, dataBits: 8, parity: "none", stopBits: "1", flow: "none", enter: "cr", dtr: true, rts: true,
                   bell: "visual", theme: "auto", fontSize: 14, cursor: "block", scrollback: 10000,
                   charset: "utf-8", logExisting: "append", warnClose: true, view: "text", newLine: "terminal", timeMode: "clock", linePauseMs: 0, charDelayMs: 0, lineDelayMs: 0, rtsMode: "off", rtsDelayMs: 0, logMaxMB: 0, logKeep: 5,
                   bold: "colour", rightClick: "copypaste", autoWrap: true };

function quickProfile(device, baud) {
  return { ...DEFAULTS, name: device, device, baud, localEcho: false, group: "" };
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
  for (const key of ["baud", "dataBits", "fontSize", "scrollback", "linePauseMs", "charDelayMs", "lineDelayMs", "rtsDelayMs", "logMaxMB", "logKeep"]) p[key] = Number(p[key]);
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
  tab.view = viewOf(p);
  updateGraphSpecs(tab);
  tab.frameWarned = false;
  tab.highlights = parseHighlights(p.highlights);
  syncBars(tab.pane);
  redraw(tab);
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
  sendMessage(tab, { type: "script", name });
}
function stopScript(tab) {
  sendMessage(tab, { type: "stop" });
}
