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

// What was sent from the send bar is remembered per port (whatever device it opens with), also after a reload.
const HISTORY_MAX = 50;

const historyKey = (name) => `termdeck.history.${name}`;

const loadHistory = (name) => stored.read(historyKey(name), []).filter((e) => e && typeof e.text === "string").slice(-HISTORY_MAX);

function rememberSent(tab, entry) {
  const last = tab.sent[tab.sent.length - 1];
  if (entry.text && !(last && last.type === entry.type && last.text === entry.text)) tab.sent.push(entry);
  tab.sent = tab.sent.slice(-HISTORY_MAX);
  tab.sentAt = tab.sent.length;
  stored.write(historyKey(tab.port || tab.profile.name), tab.sent);
}

// The send bar: what is typed goes out as text, bytes or steps. It says whether something was sent.
// The input is emptied afterwards.
function submitSend(pane) {
  const tab = pane.active;
  if (!tab || tab.state !== "connected") return false;
  const type = pane.sendbar.querySelector("[data-send-type]").value;
  const text = pane.sendbar.querySelector("[data-send-text]").value;
  if (!text && type !== "text") return false;
  const targets = pane.sendbar.querySelector("[data-all]").checked ? tabs.filter((t) => t.state === "connected" && !t.profile.peer) : [tab];
  const message = { type: "send", format: type, data: text, check: tab.sendCheck, end: tab.sendEnd.mode, endCustom: tab.sendEnd.custom };  // what the bar says goes to every tab
  let sent = false;
  for (const target of targets) sent = sendSteps(target, message) || sent;
  if (!sent) return false;
  rememberSent(tab, { type, text });
  pane.sendbar.querySelector("[data-send-text]").value = "";
  return true;
}

// Send what the bar holds (the server makes it steps: the data, its check, its end). Numbers are checked
// here first, so a mistake is told at once. Says whether it went out.
function sendSteps(tab, message) {
  try {
    if (["hex", "dec", "bin"].includes(message.format)) parseNumbers(message.format, message.data);
  } catch (error) {
    tell(error.message);
    return false;
  }
  if (!sendMessage(tab, message)) return false;
  tab.term.scrollToBottom();  // what you send is answered at the bottom: show it
  return true;
}

// ---- the buttons of the rules ("button Name => ..."), above the send bar: the session does what the rule says
function pressButton(pane, name) {
  const tab = pane.active;
  if (tab?.state !== "connected") return;
  const targets = pane.sendbar.querySelector("[data-all]").checked ? tabs.filter((t) => t.state === "connected" && !t.profile.peer) : [tab];
  for (const target of targets) if (sendMessage(target, { type: "button", name })) target.term.scrollToBottom();
}

function showQuick(pane) {
  const tab = pane.active;
  const names = tab?.buttons || [];
  const bar = pane.quickbar;
  bar.hidden = names.length === 0;
  const signature = JSON.stringify(names);
  if (bar.dataset.signature !== signature) {  // the buttons are drawn again only when they changed: a click that is in progress keeps its button
    bar.dataset.signature = signature;
    bar.replaceChildren(...names.map((name) => {
      const button = el("button", "quick", name);
      button.title = `Do what the rule of ${name} says`;
      button.addEventListener("click", () => pressButton(pane, name));
      return button;
    }));
  }
  bar.querySelectorAll("button").forEach((button) => { button.disabled = tab?.state !== "connected"; });
}

// ---- automatic send: the input goes out again and again, every so many milliseconds (a job on the server)
const autoJobOf = (tab) => tab?.jobs?.find((j) => j.kind === "auto");

function stopAuto(pane) {
  const tab = pane.active;
  const job = autoJobOf(tab);
  if (job) sendMessage(tab, { type: "stopjob", job: job.id });
}

function startAuto(pane) {
  const tab = pane.active;
  const box = pane.sendbar.querySelector("[data-auto]");
  const ms = Math.min(Math.max(Math.floor(Number(pane.sendbar.querySelector("[data-auto-ms]").value)) || 1000, 10), 3_600_000);
  pane.sendbar.querySelector("[data-auto-ms]").value = ms;
  const type = pane.sendbar.querySelector("[data-send-type]").value;
  const text = pane.sendbar.querySelector("[data-send-text]").value;
  if (!tab || tab.state !== "connected" || (!text && type !== "text")) { box.checked = false; return; }
  const targets = pane.sendbar.querySelector("[data-all]").checked ? tabs.filter((t) => t.state === "connected" && !t.profile.peer) : [tab];
  for (const target of targets) sendSteps(target, { type: "auto", format: type, data: text, check: tab.sendCheck, end: tab.sendEnd.mode, endCustom: tab.sendEnd.custom, ms });
  rememberSent(tab, { type, text });
}

// The Auto box of the send bar says whether the shown tab sends by itself.
function showAuto(pane) {
  const on = !!autoJobOf(pane.active);
  const box = pane.sendbar.querySelector("[data-auto]");
  box.checked = on;
  box.closest("label").classList.toggle("on", on);
}

// Send a file as it is: the server sends it, a piece at a time, and shows how far it is.
async function sendFile(pane, file) {
  const tab = pane.active;
  if (!tab || tab.state !== "connected" || !file) return;
  const data = new Uint8Array(await file.arrayBuffer());
  sendMessage(tab, { type: "file", name: file.name, bytes: b64(data) });
}

// The send bar: the type, the input, End, Auto send and files.
function wireSendBar(pane) {
  const send = (selector) => pane.sendbar.querySelector(selector);
  const input = send("[data-send-text]");
  pane.sendbar.dataset.type = "text";
  send("[data-send-type]").addEventListener("change", () => setSendType(pane, send("[data-send-type]").value));
  send("[data-send]").addEventListener("click", () => { submitSend(pane); input.focus(); });
  send("[data-auto]").addEventListener("change", () => (send("[data-auto]").checked ? startAuto(pane) : stopAuto(pane)));
  send("[data-auto-ms]").addEventListener("change", () => { if (autoJobOf(pane.active)) startAuto(pane); });
  const endChange = () => {
    const tab = pane.active;
    if (tab) tab.sendEnd = { mode: send("[data-send-end]").value, custom: send("[data-send-end-custom]").value };
    showEnding(pane);
  };
  send("[data-send-end]").addEventListener("change", () => { endChange(); if (send("[data-send-end]").value === "custom") send("[data-send-end-custom]").focus(); });
  send("[data-send-end-custom]").addEventListener("input", endChange);
  send("[data-send-check]").addEventListener("change", () => { if (pane.active) pane.active.sendCheck = send("[data-send-check]").value; });
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
;
}
