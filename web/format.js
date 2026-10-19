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
    const mark = (b) => out.push(...ENC.encode(`\x1b[90m<${controlName(b)}>\x1b[39m`));
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

  // The buttons of the send bar, from the session's lines "name | type | data" and "... | end=cr" (the
  // server has checked them). Lines that do not fit are left out.
  function parseQuickSends(text) {
    const out = [];
    for (let line of (text || "").split("\n")) {
      line = line.trim();
      if (!line || line.startsWith("#")) continue;
      const parts = line.split("|");
      if (parts.length < 3) continue;
      const entry = { name: parts[0].trim(), type: parts[1].trim().toLowerCase(), end: "", check: "none" };
      for (let m; parts.length > 3 && (m = /^\s*(end|check)=(\w+)\s*$/.exec(parts[parts.length - 1]));) {
        entry[m[1]] = m[2];
        parts.pop();
      }
      entry.data = parts.slice(2).join("|").trim();
      if (entry.name) out.push(entry);
    }
    return out;
  }

  // The names of the graph series in the session settings, in order (the server finds the numbers).
  function seriesNames(text) {
    return (text || "").split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#") && l.split("|").length >= 2)
      .map((l) => l.split("|")[0].trim()).filter(Boolean);
  }

  // ---- highlights and filters on lines
  const STYLES = { red: "31", green: "32", yellow: "33", blue: "34", magenta: "35", cyan: "36", white: "37", grey: "90",
    orange: "38;5;208", bold: "1", dim: "2", underline: "4", inverse: "7" };

  // A pattern written as text (any case) or /regex/. Null when it is not valid.
  function compilePattern(pattern) {
    try {
      if (pattern.length > 2 && pattern.startsWith("/") && pattern.endsWith("/")) return new RegExp(pattern.slice(1, -1));
      return new RegExp(pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i");
    } catch { return null; }
  }

  // "pattern = colour" lines as rules { re, sgr }; the server has checked them.
  function parseHighlights(text) {
    const rules = [];
    for (let line of (text || "").split("\n")) {
      line = line.trim();
      const at = line.lastIndexOf("=");
      if (!line || line.startsWith("#") || at < 1) continue;
      const re = compilePattern(line.slice(0, at).trim());
      const sgr = line.slice(at + 1).split("+").map((s) => STYLES[s.trim()]).filter(Boolean).join(";");
      if (re && sgr) rules.push({ re, sgr });
    }
    return rules;
  }

  // What the line without its colours and line end looks like to the rules, cut short.
  const plainLine = (line) => line.replace(/\x1b\[[0-9;?]*[ -/]*[@-~]/g, "").replace(/[\r\n]+$/, "").slice(0, 500);

  // The style (a list of SGR codes) of the first rule the line matches, or "".
  function highlightOf(rules, line) {
    const text = plainLine(line);
    return rules.find((r) => r.re.test(text))?.sgr || "";
  }

  // The filter: text or /regex/ shows the lines that match, with ! in front it hides them. Null: no filter.
  function compileFilter(text) {
    text = (text || "").trim();
    const invert = text.startsWith("!");
    const re = text.length > (invert ? 1 : 0) ? compilePattern(invert ? text.slice(1).trim() : text) : null;
    return re ? { re, invert } : null;
  }

  const passesFilter = (filter, line) => !filter || filter.re.test(plainLine(line)) !== filter.invert;

  // ---- frames: a byte stream cut into frames by a description, one line each, with the check verified
  //   sync = AA 55            the bytes a frame starts with (optional; other bytes are noise)
  //   size = u8@2 + 5         the size of the whole frame: a number (size = 16) or a field plus a number
  //                           (u8, u16be, u16le at an offset); or  end = 0D 0A  : the frame ends with these bytes
  //   header = 3              bytes before the payload (shown apart)
  //   check = crc16modbus     check bytes at the end of the frame (a kind of Check in the send bar), over the bytes before them
  const FIELD = /^(u8|u16be|u16le)@(\d+)(?:\s*\+\s*(\d+))?$/;
  const hexList = (text) => (text.match(/[0-9a-fA-F]{2}/g) || []).map((h) => parseInt(h, 16));
  const CHECK_SIZES = { sum8: 1, xor8: 1, crc8: 1, crc16modbus: 2, crc16ccitt: 2, crc16xmodem: 2, crc32: 4 };
  const MAX_FRAME = 65536;

  // How each line of the description sets the spec; a handler returns an error text or nothing.
  const FRAME_KEYS = {
    sync: (spec, value) => { spec.sync = hexList(value); },
    end: (spec, value) => { spec.end = hexList(value); },
    header: (spec, value) => { spec.header = Number(value); },
    check: (spec, value) => { spec.check = value; },
    size(spec, value) {
      const m = FIELD.exec(value);
      if (m) spec.size = { field: m[1], at: Number(m[2]), add: Number(m[3] || 0) };
      else if (/^\d+$/.test(value)) spec.size = { fixed: Number(value) };
      else return `size = ${value}: use a number or u8@2 + 5`;
    },
  };

  // What is wrong with a whole description, or nothing.
  function frameSpecError(spec) {
    if (!spec.size && !(spec.end && spec.end.length)) return "give a size or an end";
    if (spec.size && spec.end && spec.end.length) return "give a size or an end, not both";
    if (!Number.isInteger(spec.header) || spec.header < 0) return "header is a number of bytes";
    if (spec.check !== "none" && !CHECK_SIZES[spec.check]) return `check: one of ${Object.keys(CHECK_SIZES).join(", ")}`;
    return "";
  }

  // The description of the frames as { sync, size, end, header, check } or { error }.
  function parseFrameSpec(text) {
    const spec = { sync: [], size: null, end: null, header: 0, check: "none" };
    for (let line of (text || "").split("\n")) {
      line = line.replace(/#.*/, "").trim();
      if (!line) continue;
      const at = line.indexOf("=");
      if (at < 1) return { error: `${line}: use  name = value` };
      const key = line.slice(0, at).trim().toLowerCase();
      const handler = FRAME_KEYS[key];
      if (!handler) return { error: `${key}: unknown (sync, size, end, header, check)` };
      const error = handler(spec, line.slice(at + 1).trim());
      if (error) return { error };
    }
    const error = frameSpecError(spec);
    return error ? { error } : spec;
  }

  // The size of the frame at the start of buf, or 0 when more bytes are needed, or -1 when it cannot be one.
  function frameSize(spec, buf) {
    if (spec.end && spec.end.length) {
      const stop = spec.end;
      for (let i = spec.sync.length; i + stop.length <= buf.length; i++) {
        if (stop.every((b, k) => buf[i + k] === b)) return i + stop.length;
      }
      return buf.length > MAX_FRAME ? -1 : 0;
    }
    const { fixed, field, at, add } = spec.size;
    let total = fixed;
    if (field) {
      const width = field === "u8" ? 1 : 2;
      if (buf.length < at + width) return 0;
      const value = field === "u8" ? buf[at] : field === "u16be" ? (buf[at] << 8) | buf[at + 1] : buf[at] | (buf[at + 1] << 8);
      total = value + add;
    }
    return total < spec.sync.length + 1 || total > MAX_FRAME ? -1 : total;
  }

  // A parser that takes bytes as they come and gives the frames found: [{ bytes, noise, ok, expected }].
  function makeFrameParser(spec) {
    let buf = [];
    const checkSize = CHECK_SIZES[spec.check] || 0;
    return function feed(bytes) {
      buf.push(...bytes);
      const rows = [];
      for (;;) {
        if (spec.sync.length) {  // noise before the sync bytes is its own row
          let skip = 0;
          while (skip < buf.length && !spec.sync.slice(0, Math.min(spec.sync.length, buf.length - skip)).every((b, k) => buf[skip + k] === b)) skip++;
          if (skip) { rows.push({ bytes: buf.splice(0, skip), noise: true }); }
          if (!buf.length) break;
        }
        const size = frameSize(spec, buf);
        if (size === 0) break;
        if (size < 0) { rows.push({ bytes: buf.splice(0, 1), noise: true }); continue; }  // not a frame: one byte of noise, look again
        if (buf.length < size) break;
        const bytesOfFrame = buf.splice(0, size);
        const row = { bytes: bytesOfFrame, noise: false, ok: null };
        if (checkSize && size > checkSize) {
          const expected = checksum(spec.check, bytesOfFrame.slice(0, size - checkSize));
          row.expected = expected;
          row.ok = expected.every((b, k) => b === bytesOfFrame[size - checkSize + k]);
        }
        rows.push(row);
      }
      return rows;
    };
  }

  // One frame as a line: header | payload | check, with OK or BAD (and what the check should be).
  function frameLine(spec, row) {
    const hex = (list) => list.map(hex2).join(" ");
    if (row.noise) return `?? ${hex(row.bytes)}`;
    const checkSize = row.ok === null ? 0 : CHECK_SIZES[spec.check];
    const split = Math.min(spec.header, row.bytes.length - checkSize);
    const parts = [row.bytes.slice(0, split), row.bytes.slice(split, row.bytes.length - checkSize), checkSize ? row.bytes.slice(-checkSize) : []];
    let text = parts.filter((p) => p.length).map(hex).join(" | ");
    if (row.ok === true) text += "  OK";
    if (row.ok === false) text += `  BAD (expected ${hex(row.expected)})`;
    return text;
  }

  // ---- checksums for data written as bytes: the bytes to add after the data
  function crc(bytes, width, poly, init, reflect, xorOut) {
    const top = width === 32 ? 0x80000000 : 1 << (width - 1);
    const mask = width === 32 ? 0xffffffff : (1 << width) - 1;
    let value = init;
    for (const byte of bytes) {
      const b = reflect ? reverseBits(byte, 8) : byte;
      value ^= width === 32 ? (b << 24) >>> 0 : b << (width - 8);
      for (let i = 0; i < 8; i++) value = ((value & top ? (value << 1) ^ poly : value << 1) & mask) >>> 0;
    }
    if (reflect) value = reverseBits(value, width);
    return (value ^ xorOut) >>> 0;
  }

  function reverseBits(value, width) {
    let out = 0;
    for (let i = 0; i < width; i++) out = ((out << 1) | ((value >>> i) & 1)) >>> 0;
    return out;
  }

  const bytesOfNumber = (value, size, littleEndian) => {
    const out = Array.from({ length: size }, (_, i) => (value >>> (8 * (size - 1 - i))) & 0xff);
    return littleEndian ? out.reverse() : out;
  };

  const CHECKS = {
    sum8: { label: "Sum (1 byte)", make: (b) => [b.reduce((s, x) => (s + x) & 0xff, 0)] },
    xor8: { label: "XOR (1 byte)", make: (b) => [b.reduce((s, x) => s ^ x, 0)] },
    crc8: { label: "CRC-8", make: (b) => bytesOfNumber(crc(b, 8, 0x07, 0, false, 0), 1) },
    crc16modbus: { label: "CRC-16 Modbus (low byte first)", make: (b) => bytesOfNumber(crc(b, 16, 0x8005, 0xffff, true, 0), 2, true) },
    crc16ccitt: { label: "CRC-16 CCITT (high byte first)", make: (b) => bytesOfNumber(crc(b, 16, 0x1021, 0xffff, false, 0), 2) },
    crc16xmodem: { label: "CRC-16 XMODEM (high byte first)", make: (b) => bytesOfNumber(crc(b, 16, 0x1021, 0, false, 0), 2) },
    crc32: { label: "CRC-32 (low byte first)", make: (b) => bytesOfNumber(crc(b, 32, 0x04c11db7, 0xffffffff, true, 0xffffffff), 4, true) },
  };

  // The check bytes of a kind ("none" or an unknown kind adds nothing).
  const checksum = (kind, bytes) => (CHECKS[kind] ? CHECKS[kind].make(Array.from(bytes)) : []);

  function b64(bytes) {
    let text = "";
    for (let i = 0; i < bytes.length; i += 8192) text += String.fromCharCode(...bytes.subarray(i, i + 8192));
    return btoa(text);
  }

  return { ENC, hex2, stamp, formatDelta, bufferText, controlName, NEWLINES, unescapeText, textBytes, newlineBytes, shapeText, NUMBERS,
           parseNumbers, parseQuickSends, seriesNames, CHECKS, checksum, parseFrameSpec, makeFrameParser, frameLine, parseHighlights, highlightOf, compileFilter, passesFilter, plainLine, ALLOWED, clean, groupDigits, formatBytes, b64 };
})();

if (typeof module !== "undefined") module.exports = Format;
