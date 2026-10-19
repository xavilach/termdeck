"use strict";
// Run with: node --test web/
const test = require("node:test");
const assert = require("node:assert/strict");
const F = require("./format.js");

const bytesOf = (text) => Uint8Array.from(Buffer.from(text, "latin1"));
const shape = (view, chunks, held = []) => chunks.map((c) => Buffer.from(F.shapeText(bytesOf(c), view, held)).toString("latin1").replace(/\x1b\[90m|\x1b\[39m/g, "")).join("");

test("unescapeText", () => {
  assert.equal(F.unescapeText("a\\r\\n\\t\\0\\\\\\x41\\q"), "a\r\n\t\0\\A\\q");
});

test("textBytes: below 256 one byte each, the rest UTF-8", () => {
  assert.deepEqual(F.textBytes("A\u00e9\u20ac"), [0x41, 0xe9, 0xe2, 0x82, 0xac]);
});

test("parseNumbers", () => {
  assert.deepEqual([...F.parseNumbers("hex", "0x41, 42 4344")], [0x41, 0x42, 0x43, 0x44]);
  assert.deepEqual([...F.parseNumbers("dec", "65,66 255")], [65, 66, 255]);
  assert.deepEqual([...F.parseNumbers("bin", "01000001 1")], [65, 1]);
  assert.deepEqual([...F.parseNumbers("bin", "0100000101000010")], [65, 66]);
  assert.deepEqual([...F.parseNumbers("hex", "")], []);
  for (const [type, text] of [["hex", "4"], ["hex", "zz"], ["dec", "256"], ["dec", "a"], ["bin", "102"], ["bin", "123456789"]]) {
    assert.throws(() => F.parseNumbers(type, text), Error, `${type} ${text}`);
  }
});

test("clean keeps only what the type accepts", () => {
  assert.equal(F.clean("hex", "0x4g1 zz"), "041 ");
  assert.equal(F.clean("bin", "0102 1"), "010 1");
  assert.equal(F.clean("dec", "1a2 3"), "12 3");
  assert.equal(F.clean("text", "anything \\r"), "anything \\r");
});

test("groupDigits", () => {
  const cases = [
    ["hex", "4", "4"], ["hex", "41", "41"], ["hex", "412", "41 2"], ["hex", "41 2 3", "41 23"],
    ["bin", "010000011", "01000001 1"], ["bin", "0100 1", "0100 1"],
    ["dec", "65", "65"], ["dec", "655", "65 5"], ["dec", "300", "30 0"], ["dec", "2555", "255 5"], ["dec", "65 ", "65 "], ["dec", "100", "100"],
    ["text", "a b", "a b"],
  ];
  for (const [type, input, want] of cases) assert.equal(F.groupDigits(type, input), want, `${type} ${input}`);
});

test("formatBytes writes what parseNumbers reads", () => {
  const bytes = Uint8Array.from([0x41, 0x0d, 0x0a, 0x5c, 0xe9, 0x00]);
  assert.equal(F.formatBytes("text", bytes), "A\\r\\n\\\\\\xE9\\0");
  assert.deepEqual(F.textBytes(F.unescapeText(F.formatBytes("text", bytes))), [...bytes]);
  for (const type of ["hex", "dec", "bin"]) assert.deepEqual([...F.parseNumbers(type, F.formatBytes(type, bytes))], [...bytes], type);
});

test("stamp is hh:mm:ss.mmm", () => {
  assert.match(F.stamp(Date.now()), /^\d\d:\d\d:\d\d\.\d{3}$/);
});

test("formatDelta", () => {
  assert.equal(F.formatDelta(0), "+0ms");
  assert.equal(F.formatDelta(87.4), "+87ms");
  assert.equal(F.formatDelta(1250), "+1.250s");
  assert.equal(F.formatDelta(59999), "+59.999s");
  assert.equal(F.formatDelta(123400), "+2m03.4s");
});

test("bufferText joins wrapped rows and drops the empty rows at the end", () => {
  const rows = [["abc", false], ["def", true], ["", false], ["ghi", false], ["", false], ["", false]];
  const buffer = { length: rows.length, getLine: (i) => ({ translateToString: () => rows[i][0], isWrapped: rows[i][1] }) };
  assert.equal(F.bufferText(buffer), "abcdef\n\nghi");
});

test("parseQuickSends", () => {
  assert.deepEqual(F.parseQuickSends("M | hex | 01 03 | check=crc16modbus | end=none"), [{ name: "M", type: "hex", data: "01 03", end: "none", check: "crc16modbus" }]);
  const got = F.parseQuickSends("# c\nReset | hex | 41 42 | end=cr\nHello | TEXT | AT\\r\nPipe | text | a | b | end=lf\nbroken\n | text | x\n");
  assert.deepEqual(got, [
    { name: "Reset", type: "hex", data: "41 42", end: "cr", check: "none" },
    { name: "Hello", type: "text", data: "AT\\r", end: "", check: "none" },
    { name: "Pipe", type: "text", data: "a | b", end: "lf", check: "none" },
  ]);
});

test("seriesNames", () => {
  assert.deepEqual(F.seriesNames("# c\nTemp | Temperature: | degC\nTick | tick \nHum | /hum=([\\d.]+)%/\nnone\n | x"), ["Temp", "Tick", "Hum"]);
  assert.deepEqual(F.seriesNames(""), []);
});

test("checksums match the published check values of 123456789", () => {
  const data = [...Buffer.from("123456789")];
  const hex = (kind) => F.checksum(kind, data).map((b) => b.toString(16).padStart(2, "0")).join("");
  assert.equal(hex("sum8"), "dd");
  assert.equal(hex("xor8"), "31");   // 0x31 ^ 0x32 ^ ... ^ 0x39
  assert.equal(hex("crc8"), "f4");
  assert.equal(hex("crc16modbus"), "374b");  // 0x4B37, low byte first
  assert.equal(hex("crc16ccitt"), "29b1");
  assert.equal(hex("crc16xmodem"), "31c3");
  assert.equal(hex("crc32"), "2639f4cb");    // 0xCBF43926, low byte first
  assert.deepEqual(F.checksum("none", data), []);
  assert.deepEqual(F.checksum("crc16modbus", []), [0xff, 0xff]);
});

test("highlights pick a style for a line", () => {
  const rules = F.parseHighlights("# c\nERROR = red+bold\n/warn(ing)?/ = orange\nok = green\nbroken\n/(/ = red\nx = purple");
  assert.equal(rules.length, 3);
  assert.equal(F.highlightOf(rules, "an ERROR happened\r\n"), "31;1");
  assert.equal(F.highlightOf(rules, "warning: low\n"), "38;5;208");
  assert.equal(F.highlightOf(rules, "all OK"), "32");  // text is matched in any case
  assert.equal(F.highlightOf(rules, "\x1b[31mnothing\x1b[0m"), "");
  assert.equal(F.highlightOf(rules, "\x1b[1mERROR\x1b[0m"), "31;1");  // colours the device sent do not get in the way
});

test("the filter shows or hides lines", () => {
  const show = F.compileFilter("tick");
  const hide = F.compileFilter("!heartbeat");
  const re = F.compileFilter("/^t[0-9]+$/");
  assert.equal(F.passesFilter(show, "Tick 4\r\n"), true);
  assert.equal(F.passesFilter(show, "other\n"), false);
  assert.equal(F.passesFilter(hide, "heartbeat 3\n"), false);
  assert.equal(F.passesFilter(hide, "data\n"), true);
  assert.equal(F.passesFilter(re, "t42\r\n"), true);
  assert.equal(F.passesFilter(re, "t42x\n"), false);
  assert.equal(F.compileFilter(""), null);
  assert.equal(F.compileFilter("!"), null);
  assert.equal(F.compileFilter("/(/"), null);
  assert.equal(F.passesFilter(null, "anything"), true);
});

test("frames are cut from a byte stream, with the check verified", () => {
  const spec = F.parseFrameSpec("# a frame\nsync = AA 55\nsize = u8@2 + 5\nheader = 3\ncheck = crc16modbus");
  assert.equal(spec.error, undefined);
  const frame = (payload) => { const body = [0xaa, 0x55, payload.length, ...payload]; return [...body, ...F.checksum("crc16modbus", body)]; };
  const good = frame([1, 2, 3]);
  const bad = frame([9, 8]); bad[bad.length - 1] ^= 0xff;
  const feed = F.makeFrameParser(spec);
  const stream = [0x00, 0x11, ...good, ...bad, ...good];
  // the stream arrives in pieces
  const rows = [];
  for (let i = 0; i < stream.length; i += 4) rows.push(...feed(stream.slice(i, i + 4)));
  const lines = rows.map((r) => F.frameLine(spec, r));
  assert.equal(lines[0], "?? 00 11");
  assert.match(lines[1], /^AA 55 03 \| 01 02 03 \| [0-9A-F]{2} [0-9A-F]{2}  OK$/);
  assert.match(lines[2], /^AA 55 02 \| 09 08 \| [0-9A-F]{2} [0-9A-F]{2}  BAD \(expected [0-9A-F]{2} [0-9A-F]{2}\)$/);
  assert.match(lines[3], /OK$/);
  assert.equal(rows.length, 4);
});

test("frames can end with a delimiter or have a fixed size", () => {
  const ended = F.parseFrameSpec("end = 0D 0A");
  const feed = F.makeFrameParser(ended);
  assert.deepEqual(feed([0x41, 0x42, 0x0d]).length, 0);
  const rows = feed([0x0a, 0x43, 0x0d, 0x0a]);
  assert.deepEqual(rows.map((r) => r.bytes), [[0x41, 0x42, 0x0d, 0x0a], [0x43, 0x0d, 0x0a]]);
  const fixed = F.makeFrameParser(F.parseFrameSpec("sync = 7E\nsize = 4\ncheck = sum8"));
  const got = fixed([0x7e, 1, 2, 0x81, 0x7e, 1, 2, 0x00]);
  assert.equal(got[0].ok, true);   // 7E + 01 + 02 = 0x81
  assert.equal(got[1].ok, false);
  assert.equal(F.parseFrameSpec("size = u16be@1 + 3").size.at, 1);
});

test("a frame description is checked", () => {
  for (const bad of ["sync = AA", "size = 4\nend = 0D", "size = banana", "colour = red", "size = 4\ncheck = md5", "size = 4\nheader = x", "nothing here"]) {
    assert.ok(F.parseFrameSpec(bad).error, bad);
  }
  assert.ok(F.makeFrameParser(F.parseFrameSpec("size = u8@0 + 0"))([0, 5]).every((r) => r.noise), "a size that cannot be a frame is noise");
});

test("b64", () => {
  assert.equal(F.b64(Uint8Array.from([0, 255, 65])), "AP9B");
  assert.equal(F.b64(new Uint8Array(20000).fill(65)).length, Math.ceil(20000 / 3) * 4);
});

test("shapeText: terminal passes through, with control characters LF ends the line", () => {
  assert.equal(shape({ nl: "terminal" }, ["a\r\nb"]), "a\r\nb");
  assert.equal(shape({ nl: "terminal", ctrl: true }, ["a\r\nb\x1b"]), "a<CR><LF>\r\nb<ESC>");
});

test("shapeText: the sequence ends the line and stays visible, other CR and LF are dropped", () => {
  assert.equal(shape({ nl: "crlf" }, ["a\r\nb\rc\nd\r", "\ne"]), "a\r\nbcd\r\ne");
  assert.equal(shape({ nl: "cr" }, ["a\r\nb"]), "a\r\nb");
  assert.equal(shape({ nl: "none" }, ["a\r\nb"]), "ab");
  assert.equal(shape({ nl: "custom", nlCustom: ";" }, ["a;b;c"]), "a;\r\nb;\r\nc");
  assert.equal(shape({ nl: "custom", nlCustom: "END" }, ["aENDbEN", "DcEX"]), "aEND\r\nbEND\r\ncEX");
  assert.equal(shape({ nl: "custom", nlCustom: "" }, ["a\nb"]), "ab");
});

test("shapeText: control characters on show the sequence as tags before the line ends", () => {
  assert.equal(shape({ nl: "crlf", ctrl: true }, ["a\r\nb"]), "a<CR><LF>\r\nb");
  assert.equal(shape({ nl: "custom", nlCustom: "\\x03", ctrl: true }, ["a\x03b"]), "a<ETX>\r\nb");
  assert.equal(shape({ nl: "lf", ctrl: true }, ["a\rb\x00"]), "a<CR>b<NUL>");
});

test("shapeText: a held start of the sequence is not lost", () => {
  const held = [];
  assert.equal(shape({ nl: "crlf" }, ["a\r"], held), "a");
  assert.deepEqual(held, [13]);
  assert.equal(shape({ nl: "crlf" }, ["x"], held), "x");  // the CR was not followed by LF: dropped
  assert.deepEqual(held, []);
});

// ---- properties over random input (a fixed seed, so a failure can be repeated)
function rng(seed) { let s = seed; return () => { s = (s * 1664525 + 1013904223) % 4294967296; return s / 4294967296; }; }

test("random bytes survive being written and read in every type", () => {
  const rand = rng(11);
  for (let i = 0; i < 200; i++) {
    const bytes = Uint8Array.from({ length: Math.floor(rand() * 40) }, () => Math.floor(rand() * 256));
    for (const type of ["hex", "dec", "bin"]) {
      assert.deepEqual([...F.parseNumbers(type, F.formatBytes(type, bytes))], [...bytes], `${type} ${[...bytes]}`);
    }
    assert.deepEqual(F.textBytes(F.unescapeText(F.formatBytes("text", bytes))).length >= bytes.length, true);
  }
});

test("grouping digits is stable: grouping what is grouped changes nothing, and never changes the digits", () => {
  const rand = rng(12);
  for (let i = 0; i < 300; i++) {
    for (const [type, alphabet] of [["hex", "0123456789abcdefABCDEF "], ["dec", "0123456789 "], ["bin", "01 "]]) {
      const text = Array.from({ length: Math.floor(rand() * 30) }, () => alphabet[Math.floor(rand() * alphabet.length)]).join("");
      const once = F.groupDigits(type, text);
      assert.equal(F.groupDigits(type, once), once, `${type} ${JSON.stringify(text)}`);
      assert.equal(once.replace(/\s/g, ""), text.replace(/\s/g, ""), `${type} ${JSON.stringify(text)}`);
    }
  }
});

test("a decimal group is never more than 255", () => {
  const rand = rng(13);
  for (let i = 0; i < 300; i++) {
    const digits = Array.from({ length: 1 + Math.floor(rand() * 30) }, () => Math.floor(rand() * 10)).join("");
    for (const word of F.groupDigits("dec", digits).split(" ").filter(Boolean)) assert.ok(Number(word) <= 255, `${digits} -> ${word}`);
  }
});

test("shaping keeps every byte that is not a line end or a control character, in order", () => {
  const rand = rng(14);
  const views = [{ nl: "terminal" }, { nl: "cr" }, { nl: "lf" }, { nl: "crlf" }, { nl: "none" }, { nl: "custom", nlCustom: ";" }, { nl: "crlf", ctrl: true }];
  for (let i = 0; i < 300; i++) {
    const text = Array.from({ length: Math.floor(rand() * 40) }, () => "ab;\r\n\x1b c"[Math.floor(rand() * 10)]).join("");
    const view = views[Math.floor(rand() * views.length)];
    const held = [];
    // fed in pieces, the result is the same as fed at once
    const whole = shape(view, [text], []);
    const cut = Math.floor(rand() * (text.length + 1));
    const pieces = shape(view, [text.slice(0, cut), text.slice(cut)], held);
    assert.equal(pieces, whole, `${JSON.stringify(view)} ${JSON.stringify(text)} cut at ${cut}`);
    if (view.nl === "none" || !view.ctrl) {
      const kept = whole.replace(/\r\n/g, "");  // the line ends shapeText added
      for (const ch of "ab c") assert.equal(kept.split(ch).length, text.split(ch).length, `${ch} in ${JSON.stringify(text)}`);
    }
  }
});

test("frames cut the same way however the bytes arrive", () => {
  const rand = rng(15);
  const spec = F.parseFrameSpec("sync = AA 55\nsize = u8@2 + 4\nheader = 3\ncheck = sum8");
  for (let i = 0; i < 100; i++) {
    const stream = [];
    for (let f = 0; f < 6; f++) {
      if (rand() < 0.4) stream.push(...Array.from({ length: 1 + Math.floor(rand() * 4) }, () => Math.floor(rand() * 0x50)));  // noise, never a sync byte
      const payload = Array.from({ length: Math.floor(rand() * 8) }, () => Math.floor(rand() * 256));
      const body = [0xaa, 0x55, payload.length, ...payload];
      stream.push(...body, ...F.checksum("sum8", body));
    }
    const all = F.makeFrameParser(spec)(stream).map((r) => F.frameLine(spec, r));
    const feed = F.makeFrameParser(spec);
    const pieces = [];
    for (let at = 0; at < stream.length;) { const n = 1 + Math.floor(rand() * 7); pieces.push(...feed(stream.slice(at, at + n))); at += n; }
    // noise may come in more rows when it arrives in pieces; the frames and the noise bytes are the same
    const frames = (rows) => rows.filter((r) => !r.noise).map((r) => F.frameLine(spec, r));
    const noise = (rows) => rows.filter((r) => r.noise).flatMap((r) => r.bytes);
    const whole = F.makeFrameParser(spec)(stream);
    assert.deepEqual(frames(pieces), frames(whole));
    assert.deepEqual(noise(pieces), noise(whole));
    assert.ok(all.filter((l) => /OK$/.test(l)).length >= 6, "every frame was found");
  }
});
