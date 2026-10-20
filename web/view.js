"use strict";

// What a tab shows: theme and bell, the screen output of the server, notifications and notes.

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
  const base = light ? { background: "#ffffff", foreground: "#1b1f27", cursor: "#1b1f27", selectionBackground: "#d4e3fb", brightBlack: "#5b6474" }
                     : { background: "#0f1218", foreground: "#e4e7ee", cursor: "#e4e7ee", selectionBackground: "#24406b", brightBlack: "#9aa2b3" };
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

// ---------------------------------------------------------------- what is shown
// The server keeps the records of the tab and draws them for its view (text or hex, times, new line at...):
// the page writes what it sends into the terminal. A view change asks the server to draw everything again.

const { ENC, unescapeText, textBytes, NEWLINES, parseNumbers, parseQuickSends, seriesNames, CHECKS, checksum, ALLOWED, clean, groupDigits, formatBytes, b64 } = Format;
const viewOf = (p) => ({ mode: p.view || "text", stamps: !!p.timestamps, timeMode: p.timeMode || "clock", pause: p.linePauseMs || 0, ctrl: !!p.showControl, filter: p.filter || "",
                                    nl: p.newLine || "terminal", nlCustom: p.newLineCustom || "" });

// Screen output of the server: written, unless the screen is frozen (Resume draws it all again).
function showOutput(tab, data) {
  if (tab.paused) {
    tab.paused.bytes += data.byteLength;
    if (tab === tab.pane.active) showFreeze(tab.pane);
  } else {
    tab.term.write(new Uint8Array(data));
  }
  if (!tab.quiet) markActivity(tab);
}

// A tab that is not shown and receives data gets a mark, and the page title counts such tabs.
function markActivity(tab) {
  if (tab === tab.pane.active && !document.hidden) return;
  if (!tab.unread) {
    tab.unread = true;
    tab.tabEl.classList.add("activity");
    showUnread();
  }
}

function showUnread() {
  const count = tabs.filter((t) => t.unread).length;
  document.title = count ? `(${count}) termdeck` : "termdeck";
}

// The server draws the records again with the tab's view.
function redraw(tab) {
  sendMessage(tab, { type: "view", view: tab.view });
}

// The server is drawing the records again: the screen starts empty, and old bells and titles are not news.
function startRedraw(tab) {
  tab.quiet = true;
  tab.term.reset();
  applyModes(tab);
}

function endRedraw(tab) {
  tab.term.write("", () => { tab.quiet = false; });
}

function clearTab(tab) {
  tab.term.clear();
  tab.counters = { rx: 0, tx: 0 };
  sendMessage(tab, { type: "clear" });
  updateCounters(tab);
}

// A line that matched a rule with "notify" gives a desktop notification when the tab is not in sight (at most one per 5 s per tab).
function notifyLine(tab, text) {
  if (tab === tab.pane.active && !document.hidden) return;
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  if (Date.now() - (tab.notifiedAt || 0) < 5000) return;
  tab.notifiedAt = Date.now();
  const notification = new Notification(tab.profile.name, { body: text, tag: `termdeck-${tab.profile.name}` });
  notification.onclick = () => { window.focus(); activate(tab); showLines(); notification.close(); };
}

// A message of termdeck about the tab, kept with its records (and written to its log).
function note(tab, text) {
  if (!sendMessage(tab, { type: "note", data: text })) localNote(tab, text);
}

// A message of the page only (an answer to a click, the server gone): on the screen, not kept.
function localNote(tab, text) {
  tab.term.write(`\r\n\x1b[90m[termdeck] ${text}\x1b[0m\r\n`);
}
