"use strict";
// Run with: node --test web/
const test = require("node:test");
const assert = require("node:assert/strict");
const F = require("./format.js");


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

test("b64", () => {
  assert.equal(F.b64(Uint8Array.from([0, 255, 65])), "AP9B");
  assert.equal(F.b64(new Uint8Array(20000).fill(65)).length, Math.ceil(20000 / 3) * 4);
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


