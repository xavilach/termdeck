"use strict";

// The send bar: type, data, End, Check, Auto send, files and the quick sends above it.

const SEND_TYPES = { t: "text", d: "dec", h: "hex", b: "bin" };

// What is typed in the send bar belongs to the tab: the type and the data are kept when another tab is shown.
function restoreDraft(pane, tab) {
  const select = pane.sendbar.querySelector("[data-send-type]");
  const input = pane.sendbar.querySelector("[data-send-text]");
  if (pane.draftTab) pane.draftTab.draft = { type: select.value, text: input.value };
  pane.draftTab = tab;
  showDraft(pane, tab.draft || { type: "text", text: "" });
}

// Put a type and its data in the send bar as they are.
function showDraft(pane, draft) {
  const input = pane.sendbar.querySelector("[data-send-text]");
  pane.sendbar.querySelector("[data-send-type]").value = input.dataset.type = pane.sendbar.dataset.type = draft.type;
  input.value = draft.text;
  showEnding(pane);
}

// The end box of the send bar: hidden for Cmd, and the custom field only for Custom.
function showEnding(pane) {
  const mode = pane.sendbar.querySelector("[data-send-end]").value;
  pane.sendbar.querySelector(".end-hint").hidden = pane.sendbar.dataset.type === "cmd";
  pane.sendbar.querySelector("[data-send-end-custom]").hidden = mode !== "custom";
  pane.sendbar.querySelector(".check-hint").hidden = !["hex", "dec", "bin"].includes(pane.sendbar.dataset.type);
}

// What the send bar adds after the data, as bytes.
function endingBytes(tab) {
  const { mode, custom } = tab.sendEnd;
  if (mode === "custom") return textBytes(unescapeText(custom));
  return mode === "none" ? [] : NEWLINES[mode];
}

// Group what is in the input and keep the caret after the same digit.
function regroup(input, type) {
  if (!ALLOWED[type]) return;
  const digitsBefore = input.value.slice(0, input.selectionStart).replace(/\s/g, "").length;
  const grouped = groupDigits(type, input.value);
  if (grouped === input.value) return;
  input.value = grouped;
  let at = 0;
  for (let seen = 0; at < grouped.length && seen < digitsBefore; at++) if (grouped[at] !== " ") seen++;
  input.setSelectionRange(at, at);
}

// The type was changed: the text to send is rewritten in the new type, when it can be.
function setSendType(pane, type) {
  const select = pane.sendbar.querySelector("[data-send-type]");
  const input = pane.sendbar.querySelector("[data-send-text]");
  const old = input.dataset.type || "text";
  select.value = type;
  pane.sendbar.dataset.type = type;
  input.dataset.type = type;
  showEnding(pane);
  if (old === type || !input.value) return;
  if (type === "cmd") return;  // any text is fine as a command
  if (old === "cmd") { input.value = ""; return; }  // steps cannot be shown as bytes
  try {
    const bytes = old === "text" ? ENC.encode(unescapeText(input.value)) : parseNumbers(old, input.value);
    input.value = formatBytes(type, bytes);
  } catch {
    input.value = groupDigits(type, clean(type, input.value));  // not complete yet (one hex digit, say): keep what fits
  }
}

function sendRaw(tab, bytes) {
  if (tab.state === "connected") sendMessage(tab, { type: "raw", bytes: b64(bytes) });
}

// What was sent from the send bar is remembered per session, also after the page is reloaded.
const HISTORY_MAX = 50;

const historyKey = (name) => `termdeck.history.${name}`;

const loadHistory = (name) => stored.read(historyKey(name), []).filter((e) => e && typeof e.text === "string").slice(-HISTORY_MAX);

function rememberSent(tab, entry) {
  const last = tab.sent[tab.sent.length - 1];
  if (entry.text && !(last && last.type === entry.type && last.text === entry.text)) tab.sent.push(entry);
  tab.sent = tab.sent.slice(-HISTORY_MAX);
  tab.sentAt = tab.sent.length;
  stored.write(historyKey(tab.profile.name), tab.sent);
}

// The send bar: what is typed goes out as text, bytes or steps. It says whether something was sent.
// The input is emptied afterwards, except for an automatic send, which sends it again.
function submitSend(pane, auto = false) {
  const tab = pane.active;
  if (!tab || tab.state !== "connected") return false;
  const type = pane.sendbar.querySelector("[data-send-type]").value;
  const text = pane.sendbar.querySelector("[data-send-text]").value;
  if (!text && type !== "text") return false;
  if (!sendData(tab, type, text, type === "cmd" ? [] : endingBytes(tab), tab.sendCheck)) return false;
  rememberSent(tab, { type, text });
  if (!auto) pane.sendbar.querySelector("[data-send-text]").value = "";
  return true;
}

// Send data of a type with its end (bytes), and show it when Local echo is on. Says whether it went out.
function sendData(tab, type, text, ending, check = "none") {
  try {
    if (type === "cmd") {
      sendMessage(tab, { type: "steps", data: text });
    } else if (type === "text") {
      const data = unescapeText(text) + String.fromCharCode(...ending);
      sendMessage(tab, { type: "input", data });
      echoTx(tab, text, ENC.encode(data), text.length + 32);
    } else {
      const numbers = parseNumbers(type, text);
      const bytes = Uint8Array.from([...numbers, ...checksum(check, numbers), ...ending]);
      sendRaw(tab, bytes);
      echoTx(tab, Array.from(bytes, hex2).join(" "), bytes, bytes.length * 3 + 32);
    }
  } catch (error) {
    tell(error.message);
    return false;
  }
  tab.term.scrollToBottom();  // what you send is answered at the bottom: show it
  return true;
}

// ---- quick sends: buttons above the send bar, from the session's settings
function quickEnding(tab, entry) {
  const end = entry.end || (entry.type === "text" ? "session" : "none");
  if (end === "none" || entry.type === "cmd") return [];
  return NEWLINES[end === "session" ? tab.profile.enter || "cr" : end];
}

function sendQuick(pane, entry) {
  const tab = pane.active;
  if (tab?.state === "connected") sendData(tab, entry.type, entry.data, quickEnding(tab, entry), entry.check);
}

function showQuick(pane) {
  const tab = pane.active;
  const entries = tab ? parseQuickSends(tab.profile.quickSends) : [];
  const bar = pane.quickbar;
  bar.hidden = entries.length === 0;
  const signature = JSON.stringify(entries);
  if (bar.dataset.signature !== signature) {  // the buttons are drawn again only when they changed: a click that is in progress keeps its button
    bar.dataset.signature = signature;
    bar.replaceChildren(...entries.map((entry) => {
      const button = el("button", `quick ${entry.type}`, entry.name);
      button.title = `${entry.type}: ${entry.data}${entry.end ? ` (end ${entry.end})` : ""}`;
      button.addEventListener("click", () => sendQuick(pane, entry));
      return button;
    }));
  }
  bar.querySelectorAll("button").forEach((button) => { button.disabled = tab.state !== "connected"; });
}

// ---- automatic send: the input goes out again and again, every so many milliseconds
function stopAuto(pane) {
  if (!pane.auto) return;
  clearInterval(pane.auto.timer);
  pane.auto = null;
  const box = pane.sendbar.querySelector("[data-auto]");
  box.checked = false;
  box.closest("label").classList.remove("on");
}

function startAuto(pane) {
  stopAuto(pane);
  const tab = pane.active;
  const box = pane.sendbar.querySelector("[data-auto]");
  const ms = Math.min(Math.max(Math.floor(Number(pane.sendbar.querySelector("[data-auto-ms]").value)) || 1000, 10), 3_600_000);
  pane.sendbar.querySelector("[data-auto-ms]").value = ms;
  if (!tab || tab.state !== "connected" || !submitSend(pane, true)) { box.checked = false; return; }
  box.checked = true;
  box.closest("label").classList.add("on");
  const tick = () => { if (pane.active !== tab || tab.state !== "connected" || !submitSend(pane, true)) stopAuto(pane); };
  pane.auto = { tab, timer: setInterval(tick, ms) };
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Send a file as it is, in small pieces so that it can be stopped.
async function sendFile(pane, file) {
  const tab = pane.active;
  if (!tab || tab.state !== "connected" || !file) return;
  const data = new Uint8Array(await file.arrayBuffer());
  const job = { name: file.name, total: data.length, sent: 0, stop: false };
  tab.job = job;
  note(tab, `sending ${file.name} (${data.length} bytes)`);
  for (let i = 0; i < data.length && !job.stop; i += 256) {
    while (tab.ws?.bufferedAmount > 2048 && !job.stop) await sleep(10);
    if (tab.state !== "connected") job.stop = true;
    if (job.stop) break;
    sendRaw(tab, data.subarray(i, i + 256));
    job.sent = Math.min(i + 256, data.length);
    renderFileStatus(tab.pane);
  }
  tab.job = null;
  note(tab, job.sent === data.length ? `${file.name} sent` : `${file.name} stopped after ${job.sent} bytes`);
  renderFileStatus(tab.pane);
}

function renderFileStatus(pane) {
  const job = pane.active?.job;
  const box = pane.sendbar.querySelector(".file-status");
  box.hidden = !job;
  if (job) pane.sendbar.querySelector("[data-file-text]").textContent = `${job.name} ${Math.floor((job.sent * 100) / Math.max(job.total, 1))}%`;
}

// The send bar: the type, the input, End, Auto send and files.
function wireSendBar(pane) {
  const send = (selector) => pane.sendbar.querySelector(selector);
  const input = send("[data-send-text]");
  pane.sendbar.dataset.type = "text";
  send("[data-send-type]").addEventListener("change", () => setSendType(pane, send("[data-send-type]").value));
  send("[data-send]").addEventListener("click", () => { submitSend(pane); input.focus(); });
  send("[data-auto]").addEventListener("change", () => (send("[data-auto]").checked ? startAuto(pane) : stopAuto(pane)));
  send("[data-auto-ms]").addEventListener("change", () => { if (pane.auto) startAuto(pane); });
  const endChange = () => {
    const tab = pane.active;
    if (tab) tab.sendEnd = { mode: send("[data-send-end]").value, custom: send("[data-send-end-custom]").value };
    showEnding(pane);
  };
  send("[data-send-end]").addEventListener("change", () => { endChange(); if (send("[data-send-end]").value === "custom") send("[data-send-end-custom]").focus(); });
  send("[data-send-end-custom]").addEventListener("input", endChange);
  send("[data-send-check]").addEventListener("change", () => { if (pane.active) pane.active.sendCheck = send("[data-send-check]").value; });
  for (const [kind, { label }] of Object.entries(CHECKS)) {
    const option = el("option", "", label);
    option.value = kind;
    send("[data-send-check]").append(option);
  }
  input.addEventListener("input", () => regroup(input, send("[data-send-type]").value));
  // Characters the type cannot have are not accepted, typed or pasted.
  input.addEventListener("beforeinput", (e) => {
    const text = e.data ?? e.dataTransfer?.getData("text");
    if (!text || !e.inputType.startsWith("insert")) return;
    const type = send("[data-send-type]").value;
    const kept = clean(type, e.inputType === "insertFromPaste" ? text.replace(/[\r\n\t]+/g, " ") : text);
    if (kept === text) return;
    e.preventDefault();
    if (kept) input.setRangeText(kept, input.selectionStart, input.selectionEnd, "end");
  });
  input.addEventListener("keydown", (e) => {
    const tab = pane.active;
    if (e.key === "Enter") { e.preventDefault(); submitSend(pane); }
    else if ((e.key === "ArrowUp" || e.key === "ArrowDown") && tab?.sent.length) {
      e.preventDefault();
      tab.sentAt = Math.min(Math.max(tab.sentAt + (e.key === "ArrowUp" ? -1 : 1), 0), tab.sent.length);
      showDraft(pane, tab.sent[tab.sentAt] || { type: input.dataset.type, text: "" });
    } else if (e.altKey && !e.ctrlKey && SEND_TYPES[e.key.toLowerCase()]) {
      e.preventDefault();
      setSendType(pane, SEND_TYPES[e.key.toLowerCase()]);
    }
  });
  send("[data-send-file]").addEventListener("click", () => send("[data-file]").click());
  send("[data-xmodem]").addEventListener("click", () => { if (pane.active) openXmodem(pane.active); });
  send("[data-file]").addEventListener("change", () => {
    const file = send("[data-file]").files[0];
    send("[data-file]").value = "";
    sendFile(pane, file);
  });
  send("[data-file-cancel]").addEventListener("click", () => {
    const tab = pane.active;
    if (!tab?.job) return;
    tab.job.stop = true;
    sendMessage(tab, { type: "flush" });  // what is waiting in a slow send is dropped too
  });
}
