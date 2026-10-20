package main

import (
	"encoding/json"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestRulesAreReadAndChecked(t *testing.T) {
	rules, err := parseRules(`# notes
ERROR => red bold notify
/warn(ing)?/ => orange
heartbeat => hide
login: => send admin
/Password:/ => send @hex=31 32; wait=1
Temperature: => graph Temp
/hum=([0-9.]+)%/ => graph Hum
on connect => send @dtr=0; wait=100; dtr=1
on disconnect => mark port gone
every 2s => send @hex=01 03
button Reset => send @dtr=0; wait=100; dtr=1
idle 5s => note quiet
on CTS => note
bad frame => sound mark
FAULT => log on run collect`)
	if err != nil {
		t.Fatal(err)
	}
	if len(rules) != 15 {
		t.Fatalf("%d rules", len(rules))
	}
	r := rules[0]
	if r.when != "match" || r.sgr != "31;1" || !r.notify || r.act != "" {
		t.Errorf("ERROR: %+v", r)
	}
	if r := rules[4]; r.act != "send" || stepsText(r.sent) != "hex=3132 wait=1" {
		t.Errorf("Password: %+v %s", r, stepsText(r.sent))
	}
	if r := rules[9]; r.when != "every" || r.every != 2*time.Second {
		t.Errorf("every: %+v", r)
	}
	if r := rules[10]; r.when != "button" || r.name != "Reset" || stepsText(r.sent) != "dtr=0 wait=100 dtr=1" {
		t.Errorf("button: %+v", r)
	}
	if r := rules[12]; r.when != "line" || r.name != "cts" || r.act != "note" {
		t.Errorf("on CTS: %+v", r)
	}
	if r := rules[14]; r.log != "on" || r.act != "run" || r.arg != "collect" {
		t.Errorf("FAULT: %+v", r)
	}
	buttons, keys, series := ruleNames(rules)
	if strings.Join(buttons, ",") != "Reset" || len(keys) != 0 || strings.Join(series, ",") != "Temp,Hum" {
		t.Errorf("names %v %v %v", buttons, keys, series)
	}
	for in, want := range map[string]string{"alt+1": "Alt+1", "shift+ctrl+f5": "Ctrl+Shift+F5", "Alt+Shift+x": "Alt+Shift+X", "Shift+A": "", "a": "", "Alt+": "", "Alt+1+2": ""} {
		if got := normalizeKey(in); got != want {
			t.Errorf("key %q: got %q, want %q", in, got, want)
		}
	}
	for _, bad := range []string{"no arrow", " => red", "x => ", "x => purple", "/(/ => red", "every => send x", "every 1 => send x",
		"button => send x", "key a => send x", "key => send x", "on connect => red", "x => graph", "x => log maybe", "x => run", "x => send @speed=1", "on lunch => note", "idle 99h => note"} {
		if _, err := parseRules(bad); err == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
	if (&Profile{Device: "x", Rules: "x => purple"}).Normalize() == nil {
		t.Error("a profile with a bad rule was accepted")
	}
}

func values(t *testing.T, text, line string) string {
	t.Helper()
	rules, err := parseRules(text)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, v := range graphValues(rules, line) {
		out = append(out, v.Name+"="+strconv.FormatFloat(v.Value, 'g', -1, 64))
	}
	return strings.Join(out, " ")
}

func TestGraphRulesGiveTheNumbersOfALine(t *testing.T) {
	rules := "Temperature: => graph Temp\ntick => graph Tick\n/hum=([0-9.]+)%/ => graph Hum\n/v[0-9]/ => graph Raw"
	for line, want := range map[string]string{
		"2025 - Temperature: 26.1 degC": "Temp=26.1",
		"temperature: 21":               "Temp=21", // text in any case
		"tick 12 temp":                  "Tick=12",
		"tick -3.5e1":                   "Tick=-35",
		"tick abc":                      "",
		"a hum=45.5% Temperature: 20 degC tick 7": "Temp=20 Tick=7 Hum=45.5",
		"v9 3": "Raw=3", // without a group: the number after the match
	} {
		if got := values(t, rules, line); got != want {
			t.Errorf("%q: got %q, want %q", line, got, want)
		}
	}
}

func TestAPatternThatBacktracksInAJavaScriptEngineIsHarmlessHere(t *testing.T) {
	rules, _ := parseRules("/(a+)+b/ => graph A\n/(a+)+b/ => red")
	start := time.Now()
	graphValues(rules, strings.Repeat("a", 100000))
	styleOfLine(rules, strings.Repeat("a", 100000))
	if time.Since(start) > time.Second {
		t.Error("a long line took too long")
	}
}

func TestTheFilterOfAViewIsChecked(t *testing.T) {
	for _, ok := range []string{"", "heartbeat", "!heartbeat", "/^tick [0-9]+/", "!/err(or)?/"} {
		if err := checkFilter(ok); err != nil {
			t.Errorf("%q: %v", ok, err)
		}
	}
	for _, bad := range []string{"/(/", "!/(/"} {
		if checkFilter(bad) == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
}

func TestLinesAreColouredOrHiddenByTheFirstRuleThatSaysSo(t *testing.T) {
	rules, _ := parseRules("ERROR => red bold\n/warn(ing)?/ => orange\nok => green\nheartbeat => hide\nreboot => notify")
	for line, want := range map[string]string{"an ERROR happened": "31;1", "warning: low": "38;5;208", "all OK": "32", "nothing": "", "reboot now": "", "heartbeat 3": "hidden"} {
		sgr, hide := styleOfLine(rules, line)
		if hide {
			sgr = "hidden"
		}
		if sgr != want {
			t.Errorf("%q: got %q, want %q", line, sgr, want)
		}
	}
}

func TestTheRulesOfASessionActWhenTheirMomentComes(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newLine(t)
	c, ctx := dialTerminal(t, ts)
	rules := "on connect => send @hex=43\n" + // C
		"every 100ms => send @hex=45\n" + // E (stopped below)
		"button Go => send @hex=47\n" + // G
		"key alt+1 => send @hex=4B\n" + // K
		"ping => notify send @hex=50\n" + // P, at once, without a line end
		"/temp=([0-9]+)/ => graph T\n" +
		"FAULT => sound\n" +
		"idle 300ms => note all quiet\n" +
		"on disconnect => note gone"
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "rules": rules}})
	waitFor(t, c, ctx, "state", "connected")
	if got := readDevice(t, master, 1); string(got) != "C" {
		t.Errorf("on connect: %q", got)
	}
	if got := readDevice(t, master, 2); string(got) != "EE" {
		t.Errorf("every: %q", got)
	}
	m := waitFor(t, c, ctx, "jobs", "")
	for len(m["jobs"].([]any)) == 0 {
		m = waitFor(t, c, ctx, "jobs", "")
	}
	sendJSON(t, c, ctx, map[string]any{"type": "stopjob", "job": m["jobs"].([]any)[0].(map[string]any)["id"]})
	time.Sleep(150 * time.Millisecond)
	sendJSON(t, c, ctx, map[string]any{"type": "button", "name": "go"})
	if got := readDevice(t, master, 1); !strings.HasSuffix(string(got), "G") {
		t.Errorf("button: %q", got)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "key", "name": "Alt+1"})
	if got := readDevice(t, master, 1); string(got) != "K" {
		t.Errorf("key: %q", got)
	}
	master.Write([]byte("ping"))
	if got := readDevice(t, master, 1); string(got) != "P" {
		t.Errorf("a pattern acts as soon as it comes: %q", got)
	}
	master.Write([]byte("\r\ntemp=21\r\nFAULT here\r\n"))
	seen := map[string]bool{}
	for !seen["points"] || !seen["sound"] {
		m := map[string]any{}
		kind, data, err := c.Read(ctx)
		if err != nil {
			t.Fatalf("seen %v: %v", seen, err)
		}
		if kind == websocket.MessageText && json.Unmarshal(data, &m) == nil {
			seen[m["type"].(string)] = true
			if m["type"] == "points" && m["values"].([]any)[0].(map[string]any)["value"] != float64(21) {
				t.Errorf("points %v", m)
			}
		}
	}
	waitNote(t, c, ctx, "all quiet")
	sendJSON(t, c, ctx, map[string]any{"type": "close"})
	waitNote(t, c, ctx, "gone")
}

func TestABadFrameAndALineChangeFireTheirRules(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:link-a",
		"frameSpec": "sync = 7E\nsize = 4\ncheck = sum8", "rules": "bad frame => note\non DSR => note"}})
	waitFor(t, c, ctx, "state", "connected")
	other, ctx2 := dialTerminal(t, ts)
	sendJSON(t, other, ctx2, map[string]any{"type": "open", "profile": map[string]any{"name": "u", "device": "fake:link-b"}})
	waitFor(t, other, ctx2, "state", "connected")
	sendJSON(t, other, ctx2, map[string]any{"type": "raw", "bytes": []byte{0x7e, 1, 2, 0x81, 0x7e, 1, 2, 0x00}})
	if n := waitNote(t, c, ctx, "bad frame"); !strings.Contains(n, "BAD") {
		t.Errorf("note %q", n)
	}
	for m := waitFor(t, c, ctx, "lines", ""); m["dsr"] != true; m = waitFor(t, c, ctx, "lines", "") {
	} // the other end is seen open before its DTR changes
	sendJSON(t, other, ctx2, map[string]any{"type": "line", "line": "dtr", "value": false})
	waitNote(t, c, ctx, "DSR off")
}
