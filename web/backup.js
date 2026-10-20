"use strict";

// Export and import of the devices, ports and scripts (the buttons at the top of the left column).

async function exportSettings() {
  try {
    const response = await fetch("/api/export", { headers: { "X-Termdeck-Auth": secret }, cache: "no-store" });
    if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || response.statusText);
    download("termdeck-settings.json", await response.blob());
  } catch (error) { showText($("#problem"), `Export failed: ${error.message}`); }
}

async function importSettings(file) {
  try {
    const result = await api("/api/import", { method: "POST", body: JSON.parse(await file.text()) });
    await Promise.all([loadDevices(), loadPortEntries(), loadScripts()]);
    showText($("#problem"), "");
    tell(`Imported ${result.devices} devices, ${result.ports} ports and ${result.scripts} scripts (${result.replaced} replaced).`);
  } catch (error) { showText($("#problem"), `Import failed: ${error.message}`); }
}

const helpDialog = $("#help");
async function openHelp() {
  helpDialog.showModal();
  try {
    const info = await api("/api/info");
    $("#about").textContent = `termdeck ${info.version} on ${info.os}. Settings are kept in ${info.home}.`;
  } catch { /* the help is still there */ }
}
$("#open-help").addEventListener("click", openHelp);
helpDialog.querySelector("[data-close-help]").addEventListener("click", () => helpDialog.close());
helpDialog.addEventListener("click", (e) => { if (e.target === helpDialog) helpDialog.close(); });  // a click outside

$("#export-settings").addEventListener("click", exportSettings);
$("#import-settings").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", () => {
  const file = $("#import-file").files[0];
  $("#import-file").value = "";
  if (file) importSettings(file);
});

// The serial sessions of PuTTY (those found on this computer, or else those of a file the user chooses)
// come as settings to import: each session is a device, and its port is remembered with it.
async function importSessions(settings) {
  const result = await api("/api/import", { method: "POST", body: settings });
  await Promise.all([loadDevices(), loadPortEntries()]);
  tell(`Imported ${result.devices} serial sessions of PuTTY as devices (${result.replaced} replaced).`);
}

$("#import-putty").addEventListener("click", async () => {
  try {
    const found = await api("/api/putty");
    if (found.devices.length) await importSessions(found);
    else $("#putty-file").click();  // nothing on this computer: choose a .reg export or a session file
  } catch (error) { showText($("#problem"), `PuTTY import failed: ${error.message}`); }
});

$("#putty-file").addEventListener("change", async () => {
  const file = $("#putty-file").files[0];
  $("#putty-file").value = "";
  if (!file) return;
  try {
    const bytes = new Uint8Array(await file.arrayBuffer());
    const text = bytes[0] === 0xff && bytes[1] === 0xfe ? new TextDecoder("utf-16le").decode(bytes) : new TextDecoder().decode(bytes);
    const settings = await api("/api/putty", { method: "POST", body: { name: file.name, text } });
    if (!settings.devices.length) showText($("#problem"), `No serial session of PuTTY in ${file.name}.`);
    else await importSessions(settings);
  } catch (error) { showText($("#problem"), `PuTTY import failed: ${error.message}`); }
});
