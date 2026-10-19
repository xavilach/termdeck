"use strict";

// The receive bar, the send bar and Find, under and over the lines.

// ---------------------------------------------------------------- view bar, send bar, find

const SEND_TYPES = { t: "text", d: "dec", h: "hex", b: "bin" };

// Show the view settings of the pane's tab in the bar above the lines.
function syncBars(pane) {
  const tab = pane.active;
  if (!tab) return;
  const q = (selector) => pane.viewbar.querySelector(selector);
  q("[data-view]").value = tab.view.mode;
  q("[data-view]").dataset.mode = tab.view.mode;
  q("[data-stamps]").checked = tab.view.stamps;
  q("[data-time-mode]").value = tab.view.timeMode;
  q("[data-pause]").value = tab.view.pause;
  q("[data-ctrl]").checked = tab.view.ctrl;
  q("[data-nl]").value = tab.view.nl;
  q("[data-nl-custom]").value = tab.view.nlCustom;
  q("[data-nl-custom]").hidden = tab.view.nl !== "custom";
  markChanged(pane);
  updateCounters(tab);
  if (pane.auto && pane.auto.tab !== tab) stopAuto(pane);  // auto send belongs to the tab it was started in
  restoreDraft(pane, tab);
  const end = pane.sendbar.querySelector("[data-send-end]");
  end.value = tab.sendEnd.mode;
  pane.sendbar.querySelector("[data-send-end-custom]").value = tab.sendEnd.custom;
  showEnding(pane);
}

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
}

// What the send bar adds after the data, as bytes.
function endingBytes(tab) {
  const { mode, custom } = tab.sendEnd;
  if (mode === "custom") return textBytes(unescapeText(custom));
  return mode === "none" ? [] : NEWLINES[mode];
}

// The view can be changed for an open tab; what differs from the session settings is marked.
function markChanged(pane) {
  const tab = pane.active;
  if (!tab) return;
  const saved = viewOf(tab.profile);
  const differs = { "[data-view]": tab.view.mode !== saved.mode, "[data-stamps]": tab.view.stamps !== saved.stamps || tab.view.timeMode !== saved.timeMode, "[data-ctrl]": tab.view.ctrl !== saved.ctrl, "[data-pause]": tab.view.pause !== saved.pause,
    "[data-nl]": tab.view.nl !== saved.nl || (tab.view.nl === "custom" && tab.view.nlCustom !== saved.nlCustom) };
  for (const [selector, changed] of Object.entries(differs)) pane.viewbar.querySelector(selector).closest("label").classList.toggle("changed", changed);
  pane.viewbar.querySelector("[data-view-reset]").hidden = !Object.values(differs).some(Boolean);
}

function updateCounters(tab) {
  if (tab !== tab.pane.active) return;
  for (const key of ["rx", "tx"]) tab.pane.el.querySelector(`[data-count=${key}]`).textContent = (tab.counters[key] || 0).toLocaleString();
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
  if (tab.ws?.readyState === WebSocket.OPEN && tab.state === "connected") tab.ws.send(JSON.stringify({ type: "raw", bytes: b64(bytes) }));
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
  const input = pane.sendbar.querySelector("[data-send-text]");
  const text = input.value;
  if (!text && type !== "text") return false;
  const ending = type === "cmd" ? [] : endingBytes(tab);
  try {
    if (type === "cmd") {
      tab.ws.send(JSON.stringify({ type: "steps", data: text }));
    } else if (type === "text") {
      const data = unescapeText(text) + String.fromCharCode(...ending);
      tab.ws.send(JSON.stringify({ type: "input", data }));
      echoTx(tab, text, ENC.encode(data), text.length + 32);
    } else {
      const numbers = parseNumbers(type, text);
      const bytes = Uint8Array.from([...numbers, ...ending]);
      sendRaw(tab, bytes);
      echoTx(tab, Array.from(bytes, hex2).join(" "), bytes, bytes.length * 3 + 32);
    }
  } catch (error) {
    tell(error.message);
    return false;
  }
  rememberSent(tab, { type, text });
  if (!auto) pane.sendbar.querySelector("[data-send-text]").value = "";
  return true;
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

// ---- find in the lines on screen
function openFind(pane) {
  pane.findbar.hidden = false;
  const input = pane.findbar.querySelector("[data-find]");
  input.focus();
  input.select();
}

function closeFind(pane) {
  pane.findbar.hidden = true;
  pane.active?.term.focus();
}

function findMatches(tab, query, matchCase, regex) {
  let re;
  try { re = new RegExp(regex ? query : query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), matchCase ? "g" : "gi"); } catch { return null; }
  const buffer = tab.term.buffer.active;
  const found = [];
  for (let row = 0; row < buffer.length && found.length < 10000; row++) {
    const text = buffer.getLine(row)?.translateToString(true) || "";
    re.lastIndex = 0;
    for (let m; (m = re.exec(text)) && found.length < 10000;) {
      if (m[0] === "") { re.lastIndex++; continue; }
      found.push({ row, col: m.index, length: m[0].length });
    }
  }
  return found;
}

function findStep(pane, direction) {
  const tab = pane.active;
  const bar = pane.findbar;
  const query = bar.querySelector("[data-find]").value;
  const count = bar.querySelector("[data-find-count]");
  if (!tab || !query) { count.textContent = ""; return; }
  const matchCase = bar.querySelector("[data-find-case]").getAttribute("aria-pressed") === "true";
  const regex = bar.querySelector("[data-find-regex]").getAttribute("aria-pressed") === "true";
  const key = `${query}\u0000${matchCase}${regex}`;
  const f = pane.find;
  f.matches = findMatches(tab, query, matchCase, regex);  // the screen may have changed since the last step
  if (f.key !== key || direction === 0) f.index = -1;
  f.key = key;
  if (f.matches === null) { count.textContent = "not a valid expression"; return; }
  if (!f.matches.length) { count.textContent = "no match"; tab.term.clearSelection(); return; }
  if (direction === 0) { count.textContent = `${f.matches.length} found`; return; }
  f.index = f.index < 0 ? (direction > 0 ? 0 : f.matches.length - 1) : (f.index + direction + f.matches.length) % f.matches.length;
  const m = f.matches[f.index];
  tab.term.select(m.col, m.row, m.length);
  tab.term.scrollToLine(Math.max(0, m.row - Math.floor(tab.term.rows / 2)));
  count.textContent = `${f.index + 1} of ${f.matches.length}`;
}

// The receive bar: what changes the view of the open tab.
function wireViewBar(pane) {
  const view = (selector) => pane.viewbar.querySelector(selector);
  const change = (apply) => () => { const tab = pane.active; if (tab) { apply(tab); markChanged(pane); redraw(tab); } };
  view("[data-view]").addEventListener("change", change((tab) => {
    tab.view.mode = view("[data-view]").value;
    view("[data-view]").dataset.mode = tab.view.mode;
  }));
  view("[data-stamps]").addEventListener("change", change((tab) => { tab.view.stamps = view("[data-stamps]").checked; }));
  view("[data-time-mode]").addEventListener("change", change((tab) => {
    tab.view.timeMode = view("[data-time-mode]").value;
    tab.view.stamps = view("[data-stamps]").checked = true;  // choosing what the time shows turns it on
  }));
  view("[data-nl]").addEventListener("change", change((tab) => {
    tab.view.nl = view("[data-nl]").value;
    view("[data-nl-custom]").hidden = tab.view.nl !== "custom";
    if (!view("[data-nl-custom]").hidden) view("[data-nl-custom]").focus();
  }));
  // typed values apply as you type, after a short pause
  const later = (apply) => { let timer; const run = change(apply); return () => { clearTimeout(timer); timer = setTimeout(run, 250); }; };
  view("[data-nl-custom]").addEventListener("input", later((tab) => { tab.view.nlCustom = view("[data-nl-custom]").value; }));
  view("[data-ctrl]").addEventListener("change", change((tab) => { tab.view.ctrl = view("[data-ctrl]").checked; }));
  view("[data-pause]").addEventListener("input", later((tab) => {
    tab.view.pause = Math.min(Math.max(Math.floor(Number(view("[data-pause]").value)) || 0, 0), 60000);
  }));
  view("[data-view-reset]").addEventListener("click", () => {
    const tab = pane.active;
    if (!tab) return;
    tab.view = viewOf(tab.profile);
    syncBars(pane);
    redraw(tab);
  });
  view("[data-action=find]").addEventListener("click", () => openFind(pane));
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
  send("[data-file]").addEventListener("change", () => {
    const file = send("[data-file]").files[0];
    send("[data-file]").value = "";
    sendFile(pane, file);
  });
  send("[data-file-cancel]").addEventListener("click", () => { if (pane.active?.job) pane.active.job.stop = true; });
}

// The find bar.
function wireFindBar(pane) {
  const bar = pane.findbar;
  const find = bar.querySelector("[data-find]");
  find.addEventListener("input", () => findStep(pane, 0));
  find.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); findStep(pane, e.shiftKey ? -1 : 1); }
    else if (e.key === "Escape") closeFind(pane);
  });
  for (const toggle of ["[data-find-case]", "[data-find-regex]"]) {
    bar.querySelector(toggle).addEventListener("click", (e) => {
      e.currentTarget.setAttribute("aria-pressed", String(e.currentTarget.getAttribute("aria-pressed") !== "true"));
      findStep(pane, 0);
    });
  }
  bar.querySelector("[data-find-next]").addEventListener("click", () => findStep(pane, 1));
  bar.querySelector("[data-find-prev]").addEventListener("click", () => findStep(pane, -1));
  bar.querySelector("[data-find-close]").addEventListener("click", () => closeFind(pane));
}

function wireBars(pane) {
  wireViewBar(pane);
  wireSendBar(pane);
  wireFindBar(pane);
}
