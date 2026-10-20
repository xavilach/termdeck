"use strict";

// The tunnel dialog: two ports joined (or only listened to, a monitor), one tab showing both directions.

const tunnelDialog = $("#tunnel");

function startTunnel() {
  const field = (name) => tunnelDialog.querySelector(`[name=${name}]`).value.trim();
  const a = field("portA");
  const b = field("portB");
  const error = $("#tunnel-error");
  if (!a || !b) return showText(error, "Choose the two ports.");
  if (a.toLowerCase() === b.toLowerCase()) return showText(error, "A tunnel needs two different ports.");
  const line = { dataBits: Number(field("dataBits")), parity: field("parity"), stopBits: field("stopBits"), flow: field("flow") };
  const monitor = tunnelDialog.querySelector("[name=monitor]").checked;
  const profile = { name: `${a} ${monitor ? "+" : "⇄"} ${b}`, device: a, baud: Number(field("baudA")) || 115200, ...line, logPath: field("logPath"), monitor,
                    peer: { device: b, baud: Number(field("baudB")) || 115200, ...line } };
  showText(error, "");
  tunnelDialog.close();
  openTab(profile);
}

$("#open-tunnel").addEventListener("click", () => { showText($("#tunnel-error"), ""); tunnelDialog.showModal(); });
$("#tunnel-start").addEventListener("click", startTunnel);
$("#tunnel-close").addEventListener("click", () => tunnelDialog.close());
