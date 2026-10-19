"use strict";

// Pure helpers for what is received and what is typed in the send bar. No page, no terminal: the tests
// in format_test.js load this file in Node.
const Format = (() => {
  const ENC = new TextEncoder();
  const hex2 = (b) => b.toString(16).padStart(2, "0").toUpperCase();
  const two = (n) => String(n).padStart(2, "0");

  // hh:mm:ss.mmm, local time
  function stamp(at) {
    const d = new Date(at);
    return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}.${String(d.getMilliseconds()).padStart(3, "0")}`;
  }

  // The time since the previous line, short: +87ms, +1.250s, +2m03.4s
  function formatDelta(ms) {
    if (ms < 1000) return `+${Math.round(ms)}ms`;
    if (ms < 60000) return `+${(ms / 1000).toFixed(3)}s`;
    const minutes = Math.floor(ms / 60000);
    return `+${minutes}m${((ms - minutes * 60000) / 1000).toFixed(1).padStart(4, "0")}s`;
  }

  // The text of a terminal buffer (xterm.js), a row per line; a row that continues the one before joins it.
  function bufferText(buffer) {
    const lines = [];
    for (let row = 0; row < buffer.length; row++) {
      const line = buffer.getLine(row);
      if (!line) continue;
      const text = line.translateToString(true);
      if (line.isWrapped && lines.length) lines[lines.length - 1] += text;
      else lines.push(text);
    }
    while (lines.length && lines[lines.length - 1] === "") lines.pop();
    return lines.join("\n");
  }

  // ---- received text

  const CONTROL_NAMES = ["NUL", "SOH", "STX", "ETX", "EOT", "ENQ", "ACK", "BEL", "BS", "TAB", "LF", "VT", "FF", "CR", "SO", "SI",
    "DLE", "DC1", "DC2", "DC3", "DC4", "NAK", "SYN", "ETB", "CAN", "EM", "SUB", "ESC", "FS", "GS", "RS", "US"];
  const controlName = (b) => (b === 127 ? "DEL" : CONTROL_NAMES[b]);
  const isControl = (b) => b < 32 || b === 127;

  const NEWLINES = { cr: [13], lf: [10], crlf: [13, 10] };

  // Text typed as \r \n \t \0 \\ and \xNN, written as the characters.
  function unescapeText(text) {
    const named = { r: "\r", n: "\n", t: "\t", "0": "\0", "\\": "\\" };
    return text.replace(/\\(x[0-9a-fA-F]{2}|[rnt0\\])/g, (m, c) => (c[0] === "x" ? String.fromCharCode(parseInt(c.slice(1), 16)) : named[c]));
  }

  // Bytes of a text where each character below 256 is one byte (\xE9 is E9), the others are UTF-8.
  function textBytes(text) {
    const out = [];
    for (const ch of text) out.push(...(ch.codePointAt(0) < 256 ? [ch.codePointAt(0)] : ENC.encode(ch)));
    return out;
  }

  // The bytes that end a received line, or null when the terminal does as it always does.
  function newlineBytes(view) {
    if (view.nl === "custom") return textBytes(unescapeText(view.nlCustom));
    if (view.nl === "none") return [];
    return NEWLINES[view.nl] || null;
  }

  const isPrefix = (sequence, bytes) => bytes.length <= sequence.length && bytes.every((b, i) => b === sequence[i]);

  // "New line at": the chosen sequence ends a line. Its characters stay visible (control characters only
  // with view.ctrl, as <CR>, <ESC>...). A CR or LF that is not the sequence is dropped, or shown with view.ctrl.
  // The start of the sequence is held back in `held` until the next byte tells whether it is one, also
  // across chunks. Returns the bytes for the terminal.
  function shapeText(bytes, view, held) {
    const sequence = newlineBytes(view);
    if (!sequence && !view.ctrl) return bytes;
    const ends = sequence || [10];  // the terminal's own way, with control characters shown: LF ends the line
    const out = [];
    const mark = (b) => out.push(...ENC.encode(`\x1b[2m<${controlName(b)}>\x1b[22m`));
    const plain = (b) => {
      if (view.ctrl && isControl(b)) mark(b);
      else if (b !== 13 && b !== 10) out.push(b);
    };
    for (const b of bytes) {
      held.push(b);
      while (held.length && !isPrefix(ends, held)) plain(held.shift());
      if (ends.length && held.length === ends.length) {
        held.filter((x) => !isControl(x) || view.ctrl).forEach((x) => (isControl(x) ? mark(x) : out.push(x)));
        out.push(13, 10);
        held.length = 0;
      }
    }
    return Uint8Array.from(out);
  }

  // ---- hex, decimal and binary rows

  const NUMBERS = {
    hex: { width: 3, per: 16, show: (b) => b.toString(16).padStart(2, "0") },
    dec: { width: 4, per: 16, show: (b) => String(b).padStart(3, "0") },
    bin: { width: 9, per: 8, show: (b) => b.toString(2).padStart(8, "0") },
  };

  // ---- the send bar

  function hexBytes(text) {
    const digits = text.replace(/0x/gi, " ").replace(/[\s,]+/g, "");
    if (!/^[0-9a-f]*$/i.test(digits)) throw new Error("hex has digits 0-9 and A-F only");
    if (digits.length % 2) throw new Error("hex needs two digits per byte");
    return (digits.match(/../g) || []).map((pair) => parseInt(pair, 16));
  }

  function decimalByte(word) {
    if (!/^\d+$/.test(word) || Number(word) > 255) throw new Error(`${word}: decimal bytes go from 0 to 255`);
    return [Number(word)];
  }

  // A binary word is one byte of up to 8 digits, or several bytes of 8 digits written together.
  function binaryBytes(word) {
    const groups = word.length > 8 && word.length % 8 === 0 ? word.match(/.{8}/g) : [word];
    return groups.map((g) => {
      if (!/^[01]{1,8}$/.test(g)) throw new Error(`${word}: binary bytes are up to 8 digits of 0 and 1`);
      return parseInt(g, 2);
    });
  }

  // Numbers typed as hex, decimal or binary, as bytes. Throws an Error that says what is wrong.
  function parseNumbers(type, text) {
    if (type === "hex") return Uint8Array.from(hexBytes(text));
    const words = text.split(/[\s,]+/).filter(Boolean);
    return Uint8Array.from(words.flatMap(type === "dec" ? decimalByte : binaryBytes));
  }

  // Characters each type of input accepts; Text and Cmd take anything.
  const ALLOWED = { hex: /[^0-9a-f ]/gi, dec: /[^0-9 ]/g, bin: /[^01 ]/g };
  const clean = (type, text) => (ALLOWED[type] ? text.replace(ALLOWED[type], "") : text);

  // Digits typed in a number type are grouped into bytes as they are typed: hex by 2, binary by 8, and
  // decimal by 3 or sooner when the next digit would make more than 255. A space typed by hand ends a
  // decimal or binary number early (hex ignores spaces).
  function groupDigits(type, text) {
    const per = { hex: 2, bin: 8, dec: 3 }[type];
    if (!per) return text;
    const tokens = type === "hex" ? [text.replace(/\s+/g, "")] : text.split(/\s+/);
    const out = [];
    for (const token of tokens) {
      let group = "";
      for (const digit of token) {
        if (group.length === per || (type === "dec" && Number(group + digit) > 255)) { out.push(group); group = ""; }
        group += digit;
      }
      if (group || type !== "hex") out.push(group);
    }
    return out.filter((g, i) => g || i === out.length - 1).join(" ").replace(/^ +/, "");
  }

  // A byte list written the way the given type would be typed.
  function formatBytes(type, bytes) {
    if (type === "hex") return Array.from(bytes, hex2).join(" ");
    if (type === "dec") return Array.from(bytes).join(" ");
    if (type === "bin") return Array.from(bytes, (b) => b.toString(2).padStart(8, "0")).join(" ");
    const named = { 13: "\\r", 10: "\\n", 9: "\\t", 0: "\\0", 92: "\\\\" };
    return Array.from(bytes, (b) => named[b] ?? (b >= 0x20 && b < 0x7f ? String.fromCharCode(b) : `\\x${hex2(b)}`)).join("");
  }

  function b64(bytes) {
    let text = "";
    for (let i = 0; i < bytes.length; i += 8192) text += String.fromCharCode(...bytes.subarray(i, i + 8192));
    return btoa(text);
  }

  return { ENC, hex2, stamp, formatDelta, bufferText, controlName, NEWLINES, unescapeText, textBytes, newlineBytes, shapeText, NUMBERS,
           parseNumbers, ALLOWED, clean, groupDigits, formatBytes, b64 };
})();

if (typeof module !== "undefined") module.exports = Format;
