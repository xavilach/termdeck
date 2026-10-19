"use strict";

// Export and import of the sessions, scripts and groups (the buttons at the top of the left column).

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
    await Promise.all([loadProfiles(), loadGroups(), loadScripts()]);
    showText($("#problem"), "");
    tell(`Imported ${result.sessions} sessions, ${result.scripts} scripts and ${result.groups} groups (${result.replaced} replaced).`);
  } catch (error) { showText($("#problem"), `Import failed: ${error.message}`); }
}

const helpDialog = $("#help");
$("#open-help").addEventListener("click", async () => {
  helpDialog.showModal();
  try {
    const info = await api("/api/info");
    $("#about").textContent = `termdeck ${info.version} on ${info.os}. Settings are kept in ${info.home}.`;
  } catch { /* the help is still there */ }
});
helpDialog.querySelector("[data-close-help]").addEventListener("click", () => helpDialog.close());
helpDialog.addEventListener("click", (e) => { if (e.target === helpDialog) helpDialog.close(); });  // a click outside

$("#export-settings").addEventListener("click", exportSettings);
$("#import-settings").addEventListener("click", () => $("#import-file").click());
$("#import-file").addEventListener("change", () => {
  const file = $("#import-file").files[0];
  $("#import-file").value = "";
  if (file) importSettings(file);
});
