package main

import (
	"encoding/hex"
	"encoding/json"
	"math/rand"
	"os"
	"regexp"
	"strings"
	"testing"
	"time"
)

// shape feeds chunks to a shaper of the view and returns what it gave, without the grey of the names.
func shape(view viewSettings, ansi bool, chunks ...string) string {
	s := newLineShaper(view, ansi)
	var out []byte
	for _, c := range chunks {
		out = append(out, s.shape([]byte(c))...)
	}
	return strings.NewReplacer("\x1b[90m", "", "\x1b[39m", "").Replace(string(out))
}

func TestNewLineAtTerminalPassesThroughAndShowsControlsWhenAsked(t *testing.T) {
	if got := shape(viewSettings{NL: "terminal"}, true, "a\r\nb"); got != "a\r\nb" {
		t.Errorf("terminal %q", got)
	}
	if got := shape(viewSettings{NL: "terminal", Ctrl: true}, true, "a\r\nb\x1b"); got != "a<CR><LF>\r\nb<ESC>" {
		t.Errorf("terminal with controls %q", got)
	}
	if got := shape(viewSettings{NL: "terminal"}, false, "a\r\nb\rc\n"); got != "a\nbc\n" {
		t.Errorf("plain text (the log): LF ends a line, %q", got)
	}
}

func TestTheSequenceEndsTheLineAndOtherCRAndLFAreDropped(t *testing.T) {
	for _, c := range []struct {
		view   viewSettings
		chunks []string
		want   string
	}{
		{viewSettings{NL: "crlf"}, []string{"a\r\nb\rc\nd\r", "\ne"}, "a\r\nbcd\r\ne"},
		{viewSettings{NL: "cr"}, []string{"a\r\nb"}, "a\r\nb"},
		{viewSettings{NL: "none"}, []string{"a\r\nb"}, "ab"},
		{viewSettings{NL: "custom", NLCustom: ";"}, []string{"a;b;c"}, "a;\r\nb;\r\nc"},
		{viewSettings{NL: "custom", NLCustom: "END"}, []string{"aENDbEN", "DcEX"}, "aEND\r\nbEND\r\ncEX"},
		{viewSettings{NL: "custom", NLCustom: ""}, []string{"a\nb"}, "ab"},
		{viewSettings{NL: "crlf", Ctrl: true}, []string{"a\r\nb"}, "a<CR><LF>\r\nb"},
		{viewSettings{NL: "custom", NLCustom: `\x03`, Ctrl: true}, []string{"a\x03b"}, "a<ETX>\r\nb"},
		{viewSettings{NL: "lf", Ctrl: true}, []string{"a\rb\x00"}, "a<CR>b<NUL>"},
		{viewSettings{NL: "crlf"}, []string{"a\r", "x"}, "ax"}, // a CR not followed by LF is dropped, later
	} {
		if got := shape(c.view, true, c.chunks...); got != c.want {
			t.Errorf("%+v %q: got %q, want %q", c.view, c.chunks, got, c.want)
		}
	}
}

func TestShapingIsTheSameHoweverTheTextIsCut(t *testing.T) {
	rng := rand.New(rand.NewSource(14))
	views := []viewSettings{{NL: "terminal"}, {NL: "cr"}, {NL: "lf"}, {NL: "crlf"}, {NL: "none"}, {NL: "custom", NLCustom: ";"}, {NL: "crlf", Ctrl: true}}
	for i := 0; i < 300; i++ {
		var b strings.Builder
		for n := rng.Intn(40); n > 0; n-- {
			b.WriteByte("ab;\r\n\x1b c"[rng.Intn(8)])
		}
		text, view := b.String(), views[rng.Intn(len(views))]
		cut := rng.Intn(len(text) + 1)
		whole, pieces := shape(view, true, text), shape(view, true, text[:cut], text[cut:])
		if whole != pieces {
			t.Fatalf("%+v %q cut at %d: %q and %q", view, text, cut, whole, pieces)
		}
		if !view.Ctrl {
			kept := strings.ReplaceAll(whole, "\r\n", "")
			for _, ch := range "ab c" {
				if strings.Count(kept, string(ch)) != strings.Count(text, string(ch)) {
					t.Fatalf("%q in %q: %q", ch, text, whole)
				}
			}
		}
	}
}

func TestTextTypedWithEscapesIsReadAndWrittenBack(t *testing.T) {
	if got := string(unescapeText(`a\r\n\t\0\\\x41\q`)); got != "a\r\n\t\x00\\A\\q" {
		t.Errorf("unescape %q", got)
	}
	if got := escapeText([]byte("a\r\n\t\x00\\\x1bé\xff")); got != `a\r\n\t\0\\\x1Bé\xFF` {
		t.Errorf("escape %q", got)
	}
}

func TestFormatDelta(t *testing.T) {
	for d, want := range map[time.Duration]string{0: "+0ms", 87400 * time.Microsecond: "+87ms", 1250 * time.Millisecond: "+1.250s",
		59999 * time.Millisecond: "+59.999s", 123400 * time.Millisecond: "+2m03.4s"} {
		if got := formatDelta(d); got != want {
			t.Errorf("%v: got %q, want %q", d, got, want)
		}
	}
}

// testdata/checks.json is checked here and by web/format_test.js, so the send bar and the frames agree.
func TestChecksMatchTheSharedTable(t *testing.T) {
	data, err := os.ReadFile("testdata/checks.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct{ Check, Data, Want string }
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	for _, c := range cases {
		in, _ := hex.DecodeString(c.Data)
		if got := strings.ToUpper(hex.EncodeToString(checksum(c.Check, in))); got != c.Want {
			t.Errorf("%s of %s: got %s, want %s", c.Check, c.Data, got, c.Want)
		}
	}
	if got := hex.EncodeToString(checksum("crc16modbus", []byte("123456789"))); got != "374b" {
		t.Errorf("CRC-16 Modbus of 123456789 is 0x4B37, low byte first: %s", got)
	}
	if checksum("none", []byte("x")) != nil {
		t.Error("none adds nothing")
	}
}

func TestHighlightsPickAStyleAndCanNotify(t *testing.T) {
	rules, err := parseHighlights("# c\nERROR = red+bold\n/warn(ing)?/ = orange\nok = green\nreboot = notify")
	if err != nil || len(rules) != 4 {
		t.Fatal(err, len(rules))
	}
	for line, want := range map[string]string{"an ERROR happened": "31;1", "warning: low": "38;5;208", "all OK": "32", "nothing": "", "reboot now": ""} {
		if got := styleOf(rules, line); got != want {
			t.Errorf("%q: got %q, want %q", line, got, want)
		}
	}
	if !notifies(rules, "reboot now") || notifies(rules, "all ok") {
		t.Error("notify")
	}
	if got := plainLine([]byte("\x1b[1mERROR\x1b[0m\r\n")); got != "ERROR" {
		t.Errorf("the colours the device sent do not get in the way: %q", got)
	}
}

func TestTheFilterShowsOrHidesLines(t *testing.T) {
	show, _ := parseFilter("tick")
	hide, _ := parseFilter("!heartbeat")
	re, _ := parseFilter("/^t[0-9]+$/")
	for _, c := range []struct {
		f    *lineFilter
		line string
		want bool
	}{{show, "Tick 4", true}, {show, "other", false}, {hide, "heartbeat 3", false}, {hide, "data", true}, {re, "t42", true}, {re, "t42x", false}, {nil, "x", true}} {
		if got := c.f.passes(c.line); got != c.want {
			t.Errorf("%+v on %q: %v", c.f, c.line, got)
		}
	}
	if f, err := parseFilter("!"); f != nil || err != nil {
		t.Error("! alone is no filter")
	}
	if _, err := parseFilter("/(/"); err == nil {
		t.Error("a broken pattern is refused")
	}
}

func TestFramesAreCutFromAStreamWithTheCheckVerified(t *testing.T) {
	spec, err := parseFrameSpec("# a frame\nsync = AA 55\nsize = u8@2 + 5\nheader = 3\ncheck = crc16modbus")
	if err != nil {
		t.Fatal(err)
	}
	frame := func(payload ...byte) []byte {
		body := append([]byte{0xaa, 0x55, byte(len(payload))}, payload...)
		return append(body, checksum("crc16modbus", body)...)
	}
	good, bad := frame(1, 2, 3), frame(9, 8)
	bad[len(bad)-1] ^= 0xff
	stream := append(append(append([]byte{0x00, 0x11}, good...), bad...), good...)
	p := &frameParser{spec: spec}
	var rows []frameRow
	for i := 0; i < len(stream); i += 4 {
		rows = append(rows, p.feed(stream[i:min(i+4, len(stream))])...)
	}
	var lines []string
	for _, r := range rows {
		lines = append(lines, frameLine(spec, r))
	}
	if len(lines) != 4 || lines[0] != "?? 00 11" || !regexp.MustCompile(`^AA 55 03 \| 01 02 03 \| [0-9A-F]{2} [0-9A-F]{2}  OK$`).MatchString(lines[1]) ||
		!regexp.MustCompile(`^AA 55 02 \| 09 08 \| [0-9A-F]{2} [0-9A-F]{2}  BAD \(expected [0-9A-F]{2} [0-9A-F]{2}\)$`).MatchString(lines[2]) || !strings.HasSuffix(lines[3], "OK") {
		t.Errorf("lines %q", lines)
	}
}

func TestFramesCanEndWithADelimiterOrHaveAFixedSize(t *testing.T) {
	ended, _ := parseFrameSpec("end = 0D 0A")
	p := &frameParser{spec: ended}
	if rows := p.feed([]byte("AB\r")); len(rows) != 0 {
		t.Errorf("an unfinished frame %v", rows)
	}
	if rows := p.feed([]byte("\nC\r\n")); len(rows) != 2 || string(rows[0].bytes) != "AB\r\n" || string(rows[1].bytes) != "C\r\n" {
		t.Errorf("rows %v", rows)
	}
	fixed, _ := parseFrameSpec("sync = 7E\nsize = 4\ncheck = sum8")
	got := (&frameParser{spec: fixed}).feed([]byte{0x7e, 1, 2, 0x81, 0x7e, 1, 2, 0x00})
	if len(got) != 2 || !got[0].ok || got[1].ok {
		t.Errorf("rows %+v", got)
	}
	zero, _ := parseFrameSpec("size = u8@0 + 0")
	for _, r := range (&frameParser{spec: zero}).feed([]byte{0, 5}) {
		if !r.noise {
			t.Error("a size that cannot be a frame is noise")
		}
	}
}

func TestFramesAreCutTheSameWayHoweverTheBytesArrive(t *testing.T) {
	rng := rand.New(rand.NewSource(15))
	spec, _ := parseFrameSpec("sync = AA 55\nsize = u8@2 + 4\nheader = 3\ncheck = sum8")
	for i := 0; i < 100; i++ {
		var stream []byte
		for f := 0; f < 6; f++ {
			if rng.Float64() < 0.4 {
				for n := 1 + rng.Intn(4); n > 0; n-- {
					stream = append(stream, byte(rng.Intn(0x50))) // noise, never a sync byte
				}
			}
			payload := make([]byte, rng.Intn(8))
			rng.Read(payload)
			body := append([]byte{0xaa, 0x55, byte(len(payload))}, payload...)
			stream = append(append(stream, body...), checksum("sum8", body)...)
		}
		frames := func(rows []frameRow) (out []string, noise []byte) {
			for _, r := range rows {
				if r.noise {
					noise = append(noise, r.bytes...)
				} else {
					out = append(out, frameLine(spec, r))
				}
			}
			return
		}
		whole, wholeNoise := frames((&frameParser{spec: spec}).feed(stream))
		p := &frameParser{spec: spec}
		var rows []frameRow
		for at := 0; at < len(stream); {
			n := 1 + rng.Intn(7)
			rows = append(rows, p.feed(stream[at:min(at+n, len(stream))])...)
			at += n
		}
		pieces, piecesNoise := frames(rows)
		if strings.Join(whole, "|") != strings.Join(pieces, "|") || string(wholeNoise) != string(piecesNoise) || len(whole) != 6 {
			t.Fatalf("whole %q, in pieces %q", whole, pieces)
		}
	}
}

// ---- the renderer

var at0 = time.Date(2026, 10, 20, 12, 0, 0, 0, time.Local)

func rx(text string, ms int) record {
	return record{at: at0.Add(time.Duration(ms) * time.Millisecond), src: srcRx, raw: []byte(text), text: []byte(text)}
}

func render(f format, records ...record) string {
	r := newRenderer(f)
	for _, rec := range records {
		r.add(rec)
	}
	return string(r.take())
}

func TestTheScreenShowsTimesMarkersAndWhatWasSent(t *testing.T) {
	f := screenFormat(Profile{RxMarker: "< ", TxMarker: "> ", LocalEcho: true}, viewSettings{Mode: "text", Stamps: true})
	got := render(f, rx("hello\r\nwor", 0), rx("ld\r\n", 5), record{at: at0, src: srcTx, raw: []byte("AT\r"), text: sentText([]byte("AT\r"))},
		record{at: at0, src: srcNote, text: []byte("mark 1")})
	want := "\x1b[90m[12:00:00.000]\x1b[39m < hello\r\n\x1b[90m[12:00:00.000]\x1b[39m < world\r\n" +
		"\x1b[90m[12:00:00.000]\x1b[39m > AT\r\n\x1b[90m[termdeck] mark 1\x1b[0m\r\n"
	if got != want {
		t.Errorf("got  %q\nwant %q", got, want)
	}
	if got := render(screenFormat(Profile{}, viewSettings{Mode: "text"}), record{at: at0, src: srcTx, raw: []byte("x"), text: []byte("x")}); got != "" {
		t.Errorf("without local echo what is sent is not shown: %q", got)
	}
}

func TestTheNumberViewsShowRowsOfBytesWithTheirText(t *testing.T) {
	got := render(screenFormat(Profile{}, viewSettings{Mode: "hex"}), rx("hello", 0))
	if !strings.HasPrefix(got, "68 65 6c 6c 6f ") {
		t.Errorf("hex %q", got)
	}
	r := newRenderer(screenFormat(Profile{}, viewSettings{Mode: "hex"}))
	r.add(rx("0123456789abcdefXY", 0))
	if out := string(r.take()); !strings.Contains(out, "|0123456789abcdef|\r\n58 59 ") {
		t.Errorf("a full row ends with its text %q", out)
	}
	r.add(record{at: at0, src: srcNote, text: []byte("n")})
	if out := string(r.take()); !strings.HasPrefix(out, strings.Repeat(" ", 14*3)+" |XY|\r\n") {
		t.Errorf("a row is finished before a note %q", out)
	}
	if got := render(screenFormat(Profile{}, viewSettings{Mode: "bin"}), rx("A", 0)); !strings.HasPrefix(got, "01000001 ") {
		t.Errorf("bin %q", got)
	}
}

func TestTheFilterAndHighlightsWorkOnWholeLines(t *testing.T) {
	f := screenFormat(Profile{Highlights: "ERROR = red"}, viewSettings{Mode: "text", Filter: "!tick"})
	got := render(f, rx("tick 1\r\nan ER", 0), rx("ROR\r\nok\r\n", 1))
	if got != "\x1b[31man ERROR\x1b[0m\r\nok\r\n" {
		t.Errorf("got %q", got)
	}
	r := newRenderer(f)
	r.add(rx("login: ", 0))
	if out := r.take(); len(out) != 0 {
		t.Errorf("an unfinished line waits for its end: %q", out)
	}
	r.idle(at0.Add(lineIdle))
	if out := string(r.take()); out != "login: " {
		t.Errorf("then shows after a pause: %q", out)
	}
	var notified []string
	r = newRenderer(screenFormat(Profile{Highlights: "FAULT = notify"}, viewSettings{Mode: "text"}))
	r.onNotify = func(line string) { notified = append(notified, line) }
	r.add(rx("a FAULT here\r\nfine\r\n", 0))
	if len(notified) != 1 || notified[0] != "a FAULT here" {
		t.Errorf("notified %q", notified)
	}
}

func TestANewLineAfterAPauseAndTheDirectionChanges(t *testing.T) {
	f := screenFormat(Profile{}, viewSettings{Mode: "text", Pause: 100})
	if got := render(f, rx("a", 0), rx("b", 50), rx("c", 300)); got != "ab\r\nc" {
		t.Errorf("pause %q", got)
	}
	tunnel := screenFormat(Profile{}, viewSettings{Mode: "text"})
	got := render(tunnel, record{at: at0, src: srcA2B, raw: []byte("x"), text: []byte("x")}, record{at: at0, src: srcB2A, raw: []byte("y\n"), text: []byte("y\n")})
	if got != "\x1b[32mA→B \x1b[0mx\r\n\x1b[36mB→A \x1b[0my\n" {
		t.Errorf("tunnel %q", got)
	}
}

func TestTheFramesViewShowsAFrameALineAndSaysWhenItHasNoDescription(t *testing.T) {
	got := render(screenFormat(Profile{FrameSpec: "sync = 7E\nsize = 4\ncheck = sum8"}, viewSettings{Mode: "frames"}), rx("\x7e\x01\x02\x81\x7e\x01\x02\x00", 0))
	if got != "7E 01 02 | 81  OK\r\n\x1b[31m7E 01 02 | 00  BAD (expected 81)\x1b[0m\r\n" {
		t.Errorf("frames %q", got)
	}
	got = render(screenFormat(Profile{}, viewSettings{Mode: "frames"}), rx("A", 0))
	if !strings.Contains(got, "Frames view: no frame description") || !strings.Contains(got, "41 ") {
		t.Errorf("no description %q", got)
	}
}

func TestTheGraphLinesFollowTheNewLineOfTheSession(t *testing.T) {
	var lines []string
	r := newRenderer(linesFormat(Profile{NewLine: "custom", NewLineCustom: ";"}))
	r.onLine = func(src, line string, at time.Time) { lines = append(lines, line) }
	r.add(rx("t=1;t=", 0))
	r.add(rx("2;t", 1))
	if strings.Join(lines, "|") != "t=1;|t=2;" {
		t.Errorf("lines %q", lines)
	}
}
