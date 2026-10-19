"use strict";

// What a tab shows: theme and bell, and the lines kept as events so that a view change can redraw them.

// ---------------------------------------------------------------- the lines of a tab

const MONO_FONTS = '"Cascadia Mono", "JetBrains Mono", Menlo, Consolas, monospace';

// Colour lines "name=#rrggbb" of a profile, as xterm.js theme keys.
function colourOverrides(text) {
  const theme = {};
  for (const line of (text || "").split("\n")) {
    const [name, value] = line.split("=").map((x) => x.trim());
    if (name && /^#[0-9a-f]{6}$/i.test(value || "")) theme[name === "selection" ? "selectionBackground" : name] = value;
  }
  return theme;
}

function terminalTheme(profile) {
  const choice = profile?.theme || "auto";
  const light = choice === "light" || (choice === "auto" && matchMedia("(prefers-color-scheme: light)").matches);
  const base = light ? { background: "#ffffff", foreground: "#1b1f27", cursor: "#1b1f27", selectionBackground: "#d4e3fb" }
                     : { background: "#0f1218", foreground: "#e4e7ee", cursor: "#e4e7ee", selectionBackground: "#24406b" };
  return { ...base, ...colourOverrides(profile?.colours) };
}

// How bold text is drawn: a bright colour, a bold font, or both.
function boldOptions(profile) {
  const how = profile.bold || "colour";
  return { drawBoldTextInBrightColors: how !== "font", fontWeightBold: how === "colour" ? "normal" : "bold" };
}

let lastBeep = 0;
function bell(tab) {
  if (tab.quiet) return;
  const mode = tab.profile.bell || "visual";
  if (mode === "visual" || mode === "both") {
    tab.box.classList.add("bell");
    setTimeout(() => tab.box.classList.remove("bell"), 150);
  }
  if (tab.profile.bellTab && tab.pane.active !== tab) {  // rung out of sight: the tab flashes until it is opened
    tab.belled = true;
    tab.tabEl.classList.add("belled");
  }
  if ((mode === "sound" || mode === "both") && Date.now() - lastBeep > 300) {  // a burst of bells is one beep
    lastBeep = Date.now();
    if (tab.profile.bellSound) {
      new Audio(tab.profile.bellSound).play().catch(() => {});
      return;
    }
    try {
      const audio = new AudioContext();
      const tone = audio.createOscillator();
      tone.frequency.value = 880;
      tone.connect(audio.destination);
      tone.start();
      tone.stop(audio.currentTime + 0.12);
      tone.onended = () => audio.close();
    } catch { /* no audio */ }
  }
}

// "Implicit LF in every CR": a CR that is not followed by LF becomes CR LF (across chunks too).
function addImplicitLF(tab, bytes) {
  const out = [];
  if (tab.afterCR && bytes.length && bytes[0] !== 10) out.push(10);
  tab.afterCR = false;
  for (let i = 0; i < bytes.length; i++) {
    out.push(bytes[i]);
    if (bytes[i] === 13) {
      if (i + 1 < bytes.length) { if (bytes[i + 1] !== 10) out.push(10); } else tab.afterCR = true;
    }
  }
  return Uint8Array.from(out);
}

// ---------------------------------------------------------------- what is shown, and kept to show again
// Everything that reaches the screen is also kept as an event (received data with its raw bytes and
// time, sent lines, notes). Changing the view (text or hex, times, new line after a pause) clears the
// screen and shows the events again, so a view can be left and come back with nothing lost.

const KEEP_BYTES = 8 * 1024 * 1024;
const { ENC, hex2, stamp, formatDelta, NEWLINES, shapeText, NUMBERS, unescapeText, textBytes, parseNumbers, parseQuickSends, parseSeries, extractValues, ALLOWED, clean, groupDigits, formatBytes, b64 } = Format;
const viewOf = (p) => ({ mode: p.view || "text", stamps: !!p.timestamps, timeMode: p.timeMode || "clock", pause: p.linePauseMs || 0, ctrl: !!p.showControl,
                                    nl: p.newLine || "terminal", nlCustom: p.newLineCustom || "" });

function record(tab, ev, size) {
  if (tab.replaying) return;
  ev.size = size;
  tab.events.push(ev);
  tab.kept += size;
  if (tab.kept > KEEP_BYTES * 1.25) {  // drop the oldest in one go, not one at a time
    let drop = 0;
    while (tab.kept > KEEP_BYTES && drop < tab.events.length - 1) tab.kept -= tab.events[drop++].size;
    tab.events.splice(0, drop);
  }
}

function render(tab, ev) {
  if (tab.hold || (tab.paused && !tab.replaying)) return;  // a frozen screen shows nothing new
  if (ev.k === "rx") showRx(tab, ev);
  else if (ev.k === "tx") showTx(tab, ev.text, true, ev.t, ev.bytes);
  else writeNote(tab, ev.text);
}

function emit(tab, ev, size) {
  record(tab, ev, size);
  if (ev.k === "rx") feedGraph(tab, ev);
  if (tab.paused) {
    tab.paused.bytes += size;
    if (tab === tab.pane.active) showFreeze(tab.pane);
  }
  render(tab, ev);
}

// The kept events a redraw shows: all of them, or up to the moment the screen was frozen.
function shownEvents(tab) {
  if (!tab.paused) return tab.events;
  const at = tab.paused.upTo ? tab.events.indexOf(tab.paused.upTo) + 1 : 0;
  return tab.events.slice(0, at);
}

// Clear the screen and show the kept events again with the tab's view.
function redraw(tab) {
  tab.hold = true;  // events that arrive meanwhile are kept, and shown by the redraw itself
  tab.term.write("", () => {
    tab.term.reset();
    Object.assign(tab, { lineStart: true, txOpen: false, afterCR: false, lastRx: 0, lastHead: 0, hex: null, held: [], quiet: true, hold: false });
    applyModes(tab);
    tab.replaying = true;
    for (const ev of shownEvents(tab)) render(tab, ev);
    tab.replaying = false;
    tab.term.write("", () => { tab.quiet = false; });  // the old bells and titles are not news
  });
}

function clearTab(tab) {
  tab.term.clear();
  tab.events = [];
  tab.kept = 0;
  tab.hex = null;
  tab.held = [];
  tab.counters = { rx: 0, tx: 0 };
  tab.ws?.send(JSON.stringify({ type: "resetcounters" }));
  updateCounters(tab);
}

// What starts a line: the time when asked for, then the marker.
function lineHead(tab, marker, at) {
  let head = "";
  if (tab.view.stamps) {
    const text = tab.view.timeMode === "delta" ? formatDelta(tab.lastHead ? at - tab.lastHead : 0) : stamp(at);
    head = `\x1b[2m[${text}]\x1b[22m `;
    tab.lastHead = at;
  }
  return head + (marker || "");
}

// ---- hex, decimal and binary rows: each byte as a number, then the text of the row
function endRow(tab, out) {
  const f = NUMBERS[tab.view.mode];
  out.push(" ".repeat((f.per - tab.hex.n) * f.width) + ` |${tab.hex.chars}|\r\n`);
  tab.hex = null;
  tab.lineStart = true;
}

// A row that is not full yet is finished before anything else is written.
function flushHex(tab) {
  if (!tab.hex) return;
  const out = [];
  endRow(tab, out);
  tab.term.write(out.join(""));
}

function hexWrite(tab, data, dir, at) {
  const f = NUMBERS[tab.view.mode];
  const out = [];
  if (tab.hex && (tab.hex.dir !== dir || (tab.view.pause > 0 && at - tab.hex.at > tab.view.pause))) endRow(tab, out);
  for (const b of data) {
    if (!tab.hex) {
      if (!tab.lineStart) out.push("\r\n");
      out.push(lineHead(tab, dir === "rx" ? tab.profile.rxMarker : tab.profile.txMarker, at));
      tab.hex = { dir, n: 0, chars: "", at };
      tab.lineStart = false;
    }
    out.push(f.show(b).padEnd(f.width));
    tab.hex.chars += b >= 0x20 && b < 0x7f ? String.fromCharCode(b) : ".";
    if (++tab.hex.n === f.per) endRow(tab, out);
  }
  if (tab.hex) tab.hex.at = at;
  tab.term.write(out.join(""));
}

// ---- text

const MOUSE_MODES = /\x1b\[\?(?:9|100[0-6]|1015|1016)[hl]/g;

// The received text before it is shaped into lines: mouse modes the profile refuses are removed, a CR gets its LF.
function prepareRx(tab, bytes) {
  if (tab.profile.noMouseReport && bytes.includes(0x1b)) {
    bytes = Uint8Array.from(Array.from(String.fromCharCode(...bytes).replace(MOUSE_MODES, ""), (c) => c.charCodeAt(0)));
  }
  if (tab.view.nl === "terminal" && !tab.view.ctrl && tab.profile.implicitLF) bytes = addImplicitLF(tab, bytes);
  return shapeText(bytes, tab.view, tab.held);
}

// Write text where every line starts with the time and the marker, and keep track of whether a line is open.
function writeLines(tab, bytes, marker, at) {
  if (!marker && !tab.view.stamps) {
    tab.term.write(bytes);
    const last = bytes.lastIndexOf(10);
    if (last >= 0) tab.lineStart = last === bytes.length - 1;
    else if (bytes.length) tab.lineStart = false;
    return;
  }
  let start = 0;
  for (let i = 0; i < bytes.length; i++) {
    if (tab.lineStart) {
      if (i > start) tab.term.write(bytes.subarray(start, i));
      start = i;
      tab.term.write(lineHead(tab, marker, at));
      tab.lineStart = false;
    }
    if (bytes[i] === 10) {
      tab.term.write(bytes.subarray(start, i + 1));
      start = i + 1;
      tab.lineStart = true;
    }
  }
  if (start < bytes.length) tab.term.write(bytes.subarray(start));
}

// Received data, with the time and the Rx marker at the start of each line.
function showRx(tab, ev) {
  const pause = tab.view.pause;
  if (tab.view.mode !== "text") {
    hexWrite(tab, ev.raw, "rx", ev.t);
  } else {
    const bytes = prepareRx(tab, ev.text);
    if (pause > 0 && !tab.lineStart && tab.lastRx && ev.t - tab.lastRx > pause) {
      tab.term.write("\r\n");  // nothing came for a while: a new line
      tab.lineStart = true;
    }
    writeLines(tab, bytes, tab.profile.rxMarker, ev.t);
  }
  tab.lastRx = ev.t;
}

// Text we send and show ourselves (typed lines, local echo, scripts), with the Tx marker.
// In a number view the bytes (or the text) show as rows like the received ones.
function showTx(tab, text, finish, at = Date.now(), bytes) {
  if (tab.view.mode !== "text") {
    hexWrite(tab, bytes || ENC.encode(text), "tx", at);
    if (finish) flushHex(tab);
    return;
  }
  const marker = tab.profile.txMarker;
  if (!tab.txOpen && !tab.lineStart) {
    tab.term.write("\r\n");
    tab.lineStart = true;
  }
  if (tab.lineStart) {
    const head = lineHead(tab, marker, at);
    if (head) tab.term.write(head);
  }
  tab.lineStart = false;
  tab.txOpen = true;
  tab.term.write(text);
  if (finish) {
    tab.term.write("\r\n");
    tab.lineStart = true;
    tab.txOpen = false;
  }
}

// What we send is shown only with Local echo; the device may echo it as well.
function echoTx(tab, text, bytes, size) {
  if (tab.profile.localEcho) emit(tab, { k: "tx", t: Date.now(), text, bytes }, size);
}

function note(tab, text) {
  emit(tab, { k: "note", t: Date.now(), text }, text.length + 16);
}

function writeNote(tab, text) {
  flushHex(tab);
  if (!tab.lineStart) tab.term.write("\r\n");
  tab.term.write(`\x1b[2m[termdeck] ${text}\x1b[0m\r\n`);
  tab.lineStart = true;
  tab.txOpen = false;
}
