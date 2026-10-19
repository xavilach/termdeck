"use strict";

// Key sequences in the styles PuTTY offers, for devices that expect something other than what xterm.js sends.
// Every function returns the text to send, or null to let xterm.js handle the key as usual.
const Keys = (() => {
  // The number in ESC [ n ~ for F1..F20 (there are gaps).
  const TILDE = [null, 11, 12, 13, 14, 15, 17, 18, 19, 20, 21, 23, 24, 25, 26, 28, 29, 31, 32, 33, 34];
  const SCO = "MNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz@[\\]^_`{";
  const EDIT = { Home: 1, Insert: 2, Delete: 3, End: 4, PageUp: 5, PageDown: 6 };
  const ARROWS = { ArrowUp: "A", ArrowDown: "B", ArrowRight: "C", ArrowLeft: "D" };
  const ESC = "\x1b";

  function functionKey(style, n, shift, ctrl) {
    if (style === "xterm") return null;
    if (style === "sco") {
      let index = n - 1;
      if (shift) index += 12;
      if (ctrl) index += 24;
      return `${ESC}[${SCO[index]}`;
    }
    const index = shift && n <= 10 ? n + 10 : n;
    const code = TILDE[index];
    if (style === "vt100p") {
      const skip = code > 21 ? 2 : code > 15 ? 1 : 0;
      return `${ESC}O${String.fromCharCode(code + 80 - 11 - skip)}`;
    }
    if (style === "linux" && code <= 15) return `${ESC}[[${String.fromCharCode(code + 65 - 11)}`;
    return `${ESC}[${code}~`;
  }

  function editKey(style, homeEnd, name) {
    let code = EDIT[name];
    if (style === "vt400") code = [0, 2, 1, 4, 5, 3, 6][code];  // the VT220 edit block is in a different order
    if (style === "sco") return code === 3 ? "\x7f" : `${ESC}[${"HL.FIG"[code - 1]}`;
    if (code === 1 || code === 4) {
      if (homeEnd === "rxvt") return code === 1 ? `${ESC}[H` : `${ESC}Ow`;
      if (homeEnd === "standard") return `${ESC}[${code}~`;
    }
    return style === "vt400" ? `${ESC}[${code}~` : null;
  }

  function keypadKey(style, code, shift) {
    const xterm = style === "xterm";
    const old = style === "tilde" || style === "linux" || style === "vt400";  // NumLock / * - are PF1 to PF4
    let x = null;
    if (/^Numpad[0-9]$/.test(code)) x = "pqrstuvwxy"[Number(code.slice(6))];
    else if (code === "NumpadDecimal") x = "n";
    else if (code === "NumpadEnter") x = "M";
    else if (code === "NumpadAdd") x = xterm ? (shift ? "l" : "k") : (shift ? "m" : "l");
    else if (code === "NumpadDivide") x = xterm ? "o" : old ? "Q" : null;
    else if (code === "NumpadMultiply") x = xterm ? "j" : old ? "R" : null;
    else if (code === "NumpadSubtract") x = xterm ? "m" : old ? "S" : null;
    else if (code === "NumLock" && old) x = "P";
    return x ? `${ESC}O${x}` : null;
  }

  // `state` has appKeypad (the keypad is in application mode) and appCursor (the terminal's cursor keys are).
  function map(e, profile, state) {
    if (e.altKey || e.metaKey) return null;
    const style = profile.funcKeys || "xterm";
    const f = /^F([0-9]{1,2})$/.exec(e.key);
    if (f && Number(f[1]) >= 1 && Number(f[1]) <= 12) return functionKey(style, Number(f[1]), e.shiftKey, e.ctrlKey);
    if (e.key in EDIT && !e.ctrlKey) return editKey(style, profile.homeEnd || "xterm", e.key);
    if (profile.noAppCursor && state.appCursor && e.key in ARROWS && !e.ctrlKey) return `${ESC}[${ARROWS[e.key]}`;
    if (state.appKeypad && !profile.noAppKeypad && e.code && /^(Numpad|NumLock)/.test(e.code)) return keypadKey(style, e.code, e.shiftKey);
    return null;
  }

  return { map, functionKey, editKey, keypadKey };
})();
