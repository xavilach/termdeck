"use strict";

// XMODEM file transfer: a dialog to send a file to the device or to receive one from it. The server does
// the protocol and keeps the transfer's data away from the output.

const xmodemDialog = $("#xmodem");
let xmodemTab = null;  // the tab the dialog works for

const xferSummary = (x) => {
  if (!x) return "Choose what to do and press Start. The device must be ready to receive or to send with XMODEM (with CRC).";
  const size = x.total ? ` of ${x.total.toLocaleString()} bytes` : "";
  return {
    running: `${x.dir === "send" ? "Sending" : "Receiving"}: ${(x.done || 0).toLocaleString()}${size}${x.dir === "receive" && !x.done ? " (asking the device to start)" : ""}`,
    done: x.dir === "send" ? `Sent ${x.name} (${x.total.toLocaleString()} bytes).` : `Received ${(x.done || 0).toLocaleString()} bytes: the file was saved by the browser (padded with 1A to a block).`,
    failed: `Failed: ${x.message}`,
    cancelled: "Cancelled.",
  }[x.state];
};

function showXmodem() {
  const tab = xmodemTab;
  const x = tab?.xfer;
  const running = x?.state === "running";
  $("#xmodem-status").textContent = xferSummary(x);
  const progress = $("#xmodem-progress");
  progress.max = x?.total || 1;
  progress.value = x?.done || 0;
  progress.hidden = !x || x.dir === "receive" || !x.total;
  $("#xmodem-start").disabled = running || tab?.state !== "connected";
  $("#xmodem-cancel").disabled = !running;
  for (const field of xmodemDialog.querySelectorAll("input")) field.disabled = running;
}

// What the server says about the transfer of a tab.
function receiveTransfer(tab, m) {
  tab.xfer = { dir: m.dir, name: m.name, total: m.total, done: m.done, state: m.state, message: m.message };
  if (m.state === "done" && m.dir === "receive" && m.data) {
    const bytes = Uint8Array.from(atob(m.data), (c) => c.charCodeAt(0));
    download(fileName(tab, "bin"), new Blob([bytes], { type: "application/octet-stream" }));
  }
  if (m.state !== "running") note(tab, `XMODEM ${m.dir}: ${xferSummary(tab.xfer)}`);
  if (tab === xmodemTab) showXmodem();
}

async function startXmodem() {
  const tab = xmodemTab;
  if (!tab || tab.state !== "connected") return;
  const send = xmodemDialog.querySelector("[name=xdir]:checked").value === "send";
  const message = { type: "xmodem", action: send ? "send" : "receive", oneK: $("#xmodem-1k").checked, name: "" };
  if (send) {
    const file = $("#xmodem-file").files[0];
    if (!file) { $("#xmodem-status").textContent = "Choose the file to send first."; return; }
    message.name = file.name;
    message.bytes = b64(new Uint8Array(await file.arrayBuffer()));
  }
  tab.xfer = { dir: message.action, name: message.name, total: send ? Math.ceil((message.bytes.length * 3) / 4) : 0, done: 0, state: "running" };
  sendMessage(tab, message);
  showXmodem();
}

function openXmodem(tab) {
  xmodemTab = tab;
  showXmodem();
  xmodemDialog.showModal();
}

$("#xmodem-start").addEventListener("click", startXmodem);
$("#xmodem-cancel").addEventListener("click", () => { if (xmodemTab) sendMessage(xmodemTab, { type: "xmodem", action: "cancel" }); });
$("#xmodem-close").addEventListener("click", () => xmodemDialog.close());
xmodemDialog.addEventListener("close", () => { xmodemTab = null; });
xmodemDialog.querySelectorAll("[name=xdir]").forEach((radio) => radio.addEventListener("change", () => {
  $("#xmodem-sendfile").hidden = radio.value !== "send" || !radio.checked;
}));
