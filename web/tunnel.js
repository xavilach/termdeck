"use strict";

// The tunnel dialog: two ports joined, one tab showing what goes through in both directions.

const tunnelDialog = $("#tunnel");

function startTunnel() {
  const field = (name) => tunnelDialog.querySelector(`[name=${name}]`).value.trim();
  const a = field("portA");
  const b = field("portB");
  const error = $("#tunnel-error");
  if (!a || !b) return showText(error, "Choose the two ports.");
  if (a.toLowerCase() === b.toLowerCase()) return showText(error, "A tunnel needs two different ports.");
  const line = { dataBits: Number(field("dataBits")), parity: field("parity"), stopBits: field("stopBits"), flow: field("flow") };
  const profile = { name: `${a} ⇄ ${b}`, device: a, baud: Number(field("baudA")) || 115200, ...line, logPath: field("logPath"),
                    peer: { device: b, baud: Number(field("baudB")) || 115200, ...line } };
  showText(error, "");
  tunnelDialog.close();
  openTab(profile);
}

$("#open-tunnel").addEventListener("click", () => { showText($("#tunnel-error"), ""); tunnelDialog.showModal(); });
$("#tunnel-start").addEventListener("click", startTunnel);
$("#tunnel-close").addEventListener("click", () => tunnelDialog.close());
