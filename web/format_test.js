"use strict";
// Run with: node --test web/
const test = require("node:test");
const assert = require("node:assert/strict");
const F = require("./format.js");

const bytesOf = (text) => Uint8Array.from(Buffer.from(text, "latin1"));
const shape = (view, chunks, held = []) => chunks.map((c) => Buffer.from(F.shapeText(bytesOf(c), view, held)).toString("latin1").replace(/\x1b\[2m|\x1b\[22m/g, "")).join("");

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
