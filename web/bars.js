"use strict";

// The receive bar (view, freeze, marks, saving the output, counters) and Find; the send bar is in send.js.

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
  q("[data-filter]").value = tab.view.filter;
  showFreeze(pane);
  showQuick(pane);
  syncGraph(pane);
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
  pane.sendbar.querySelector("[data-send-check]").value = tab.sendCheck;
  showEnding(pane);
}

// The view can be changed for an open tab; what differs from the session settings is marked.
function markChanged(pane) {
  const tab = pane.active;
  if (!tab) return;
  const saved = viewOf(tab.profile);
  const differs = { "[data-view]": tab.view.mode !== saved.mode, "[data-stamps]": tab.view.stamps !== saved.stamps || tab.view.timeMode !== saved.timeMode, "[data-ctrl]": tab.view.ctrl !== saved.ctrl, "[data-pause]": tab.view.pause !== saved.pause, "[data-filter]": tab.view.filter !== saved.filter,
    "[data-nl]": tab.view.nl !== saved.nl || (tab.view.nl === "custom" && tab.view.nlCustom !== saved.nlCustom) };
  for (const [selector, changed] of Object.entries(differs)) pane.viewbar.querySelector(selector).closest("label").classList.toggle("changed", changed);
  pane.viewbar.querySelector("[data-view-reset]").hidden = !Object.values(differs).some(Boolean);
  const more = pane.viewbar.querySelector("details.more");  // a change inside the menu shows on its name
  more.classList.toggle("changed", [...more.querySelectorAll("label.changed")].length > 0);
}

// Freeze keeps the screen as it is while the data is still received and kept; Resume shows it all.
function showFreeze(pane) {
  const tab = pane.active;
  const button = pane.viewbar.querySelector("[data-freeze]");
  button.setAttribute("aria-pressed", String(!!tab?.paused));
  button.textContent = tab?.paused ? `Resume (${tab.paused.bytes.toLocaleString()} B new)` : "Freeze";
}

function toggleFreeze(pane) {
  const tab = pane.active;
  if (!tab) return;
  if (tab.paused) {
    tab.paused = null;
    redraw(tab);
  } else {
    tab.paused = { upTo: tab.events[tab.events.length - 1] || null, bytes: 0 };
  }
  showFreeze(pane);
}

// ---- marks: a line of its own in the output (and in the log) to find a moment again
const MARK_ROW = /^\[termdeck\] mark \d+ at /;

function addMark(tab) {
  tab.markCount = (tab.markCount || 0) + 1;
  const text = `mark ${tab.markCount} at ${stamp(Date.now()).slice(0, 8)}`;
  note(tab, text);
  sendMessage(tab, { type: "mark", data: text });
}

// Scroll to the next (1) or previous (-1) mark above or below the top of the screen.
function jumpMark(tab, direction) {
  const buffer = tab.term.buffer.active;
  const rows = [];
  for (let row = 0; row < buffer.length; row++) {
    if (MARK_ROW.test(buffer.getLine(row)?.translateToString(true) || "")) rows.push(row);
  }
  const top = buffer.viewportY;
  const target = direction > 0 ? rows.find((r) => r > top) : [...rows].reverse().find((r) => r < top);
  if (target === undefined) return tell(rows.length ? `No ${direction > 0 ? "later" : "earlier"} mark.` : "No marks yet: Mark adds one.");
  tab.term.scrollToLine(target);
  tab.term.select(0, target, buffer.getLine(target).translateToString(true).length);
}

// Everything on the screen and in its scrollback, as text.
const outputText = (tab) => Format.bufferText(tab.term.buffer.active);

function download(name, blob) {
  const link = el("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

const fileStamp = () => new Date().toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);
const fileName = (tab, ext) => `${tab.profile.name.replace(/[^\w.-]+/g, "_")}-${fileStamp()}.${ext}`;

// What the device sent, as the bytes it sent (the kept data only: the last 8 MB).
function rawOutput(tab) {
  const parts = tab.events.filter((ev) => ev.k === "rx").map((ev) => ev.raw);
  const all = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let at = 0;
  for (const p of parts) { all.set(p, at); at += p.length; }
  return all;
}

function updateCounters(tab) {
  if (tab !== tab.pane.active) return;
  for (const key of ["rx", "tx"]) tab.pane.el.querySelector(`[data-count=${key}]`).textContent = (tab.counters[key] || 0).toLocaleString();
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
  view("[data-filter]").addEventListener("input", later((tab) => { tab.view.filter = view("[data-filter]").value; }));
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
  view("[data-freeze]").addEventListener("click", () => toggleFreeze(pane));
  view("[data-mark]").addEventListener("click", () => { if (pane.active) addMark(pane.active); });
  view("[data-mark-prev]").addEventListener("click", () => { if (pane.active) jumpMark(pane.active, -1); });
  view("[data-mark-next]").addEventListener("click", () => { if (pane.active) jumpMark(pane.active, 1); });
  view("[data-copy-all]").addEventListener("click", () => {
    if (pane.active) navigator.clipboard.writeText(outputText(pane.active)).then(() => tell("Output copied."), (e) => tell(`Cannot copy: ${e.message}`));
  });
  view("[data-save-text]").addEventListener("click", () => {
    if (pane.active) download(fileName(pane.active, "txt"), new Blob([outputText(pane.active)], { type: "text/plain" }));
  });
  view("[data-save-raw]").addEventListener("click", () => {
    if (pane.active) download(fileName(pane.active, "bin"), new Blob([rawOutput(pane.active)], { type: "application/octet-stream" }));
  });
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
  wireGraph(pane);
  wireViewBar(pane);
  wireSendBar(pane);
  wireFindBar(pane);
}
