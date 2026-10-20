package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/unix"
)

func TestEveryKindOfStepIsReadAndCheckedOnce(t *testing.T) {
	steps, err := parseSteps(`text=AT\r; hex=0D 0A; dec=1,2; bin=00000011; end=crlf; end=enter; end=\x03; check=sum8; dtr=1; rts=0; wait=5; ` +
		`timeout=50; expect=ok; break=100; baud=9600; file=fw.bin; log=on; mark=flashing`)
	want := "text=AT\\r hex=0D0A dec=0102 bin=03 end=0D0A end=enter end=03 check=sum8 dtr=1 rts=0 wait=5 timeout=50 expect=ok break=100 baud=9600 file=fw.bin log=1 mark=flashing"
	if err != nil || stepsText(steps) != want {
		t.Fatalf("%v\n%s\n%s", err, stepsText(steps), want)
	}
	for _, bad := range []string{"check=md5", "break=0", "baud=10", "file=", "log=maybe", "dec=256", "bin=2", "hex=1", "speed=1", "text"} {
		if _, err := parseSteps(bad); err == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
}

func TestTheSendBarAndScriptLinesBecomeSteps(t *testing.T) {
	steps, err := payload("text", "a;b", "", endStep("custom", `\x03`))
	if err != nil || stepsText(steps) != "text=a;b end=03" {
		t.Errorf("text keeps its semicolons: %v %s", err, stepsText(steps))
	}
	steps, _ = payload("hex", "01 02", "crc8", endStep("lf", ""))
	if stepsText(steps) != "hex=0102 check=crc8 end=0A" {
		t.Errorf("hex %s", stepsText(steps))
	}
	if _, err := payload("float", "1", "", step{}); err == nil {
		t.Error("an unknown type is refused")
	}
	for line, want := range map[string]string{"hello": "text=hello end=enter", "@@at": "text=@at end=enter", "@dtr=1; wait=2": "dtr=1 wait=2"} {
		if steps, err := scriptSteps(line); err != nil || stepsText(steps) != want {
			t.Errorf("%q: %v %s", line, err, stepsText(steps))
		}
	}
	rules, _ := parseRules("login: => send admin\npass => send @hex=31")
	if stepsText(rules[0].sent) != "text=admin end=enter" || stepsText(rules[1].sent) != "hex=31" {
		t.Errorf("what a rule sends is a script line: %+v", rules)
	}
}

func TestStepsGoOutAsOneWriteWithTheirCheckAndEnd(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "enter": "crlf", "localEcho": true, "txMarker": "> "}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "hex", "data": "01 02 04", "check": "xor8", "end": "lf"})
	if got := readDevice(t, master, 5); string(got) != "\x01\x02\x04\x07\n" {
		t.Errorf("hex with its check and end: %q", got)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "text", "data": `AT\x3B1`, "end": "enter"})
	if got := readDevice(t, master, 6); string(got) != "AT;1\r\n" {
		t.Errorf("text with the session's Enter: %q", got)
	}
	for screen := ""; !strings.Contains(screen, "> AT;1\r\n"); {
		text, err := readBinary(c, ctx)
		if err != nil {
			t.Fatalf("the sent line was not shown as one line: %q %v", screen, err)
		}
		screen += string(text)
		if strings.Contains(screen, "> 01 02 04 07 0A") && strings.Contains(screen, "> AT;1") {
			break
		}
	}
	file := filepath.Join(t.TempDir(), "fw.bin")
	os.WriteFile(file, []byte("FIRMWARE"), 0o600)
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "cmd", "data": "text=go; end=cr; wait=20; file=" + file + "; mark=sent it"})
	if got := readDevice(t, master, 11); string(got) != "go\rFIRMWARE" {
		t.Errorf("text, a wait, then a file: %q", got)
	}
	waitNote(t, c, ctx, "sent it")
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "cmd", "data": "file=" + filepath.Join(t.TempDir(), "missing")})
	waitNote(t, c, ctx, "missing")
}

func TestABaudStepChangesTheSpeedOfTheOpenPort(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "baud": 115200}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "cmd", "data": "baud=9600"})
	waitNote(t, c, ctx, "baud 9600")
	settings, err := unix.IoctlGetTermios(int(master.Fd()), unix.TCGETS)
	if err != nil || settings.Cflag&unix.CBAUD != unix.B9600 {
		t.Errorf("speed %v %#x", err, settings.Cflag&unix.CBAUD)
	}
}
