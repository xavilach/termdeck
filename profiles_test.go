package main

import (
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"math/rand"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestNormalizeDefaultsAndErrors(t *testing.T) {
	p := Profile{Name: "board", Device: "COM3"}
	if err := p.Normalize(); err != nil {
		t.Fatal(err)
	}
	if p.Baud != 115200 || p.DataBits != 8 || p.Parity != "none" || p.StopBits != "1" || p.Flow != "none" || p.Enter != "cr" {
		t.Fatalf("defaults not applied: %+v", p)
	}
	if p.Bell != "visual" || p.Theme != "auto" || p.Cursor != "block" || p.FontSize != 14 || p.Scrollback != 10000 {
		t.Fatalf("display defaults not applied: %+v", p)
	}
	if !asserted(p.DTR) || !asserted(p.RTS) {
		t.Error("DTR and RTS should be asserted unless turned off")
	}
	off := false
	if asserted(&off) {
		t.Error("a line set to false is not asserted")
	}
	for _, bad := range []Profile{
		{Device: "x", Charset: "klingon"}, {Device: "x", LogExisting: "maybe"}, {Device: "x", Bell: "loud"}, {Device: "x", Theme: "pink"}, {Device: "x", Cursor: "star"},
		{Device: "x", FontSize: 3}, {Device: "x", FontSize: 200}, {Device: "x", Scrollback: 5},
		{Device: ""}, {Device: "x", Baud: 1}, {Device: "x", DataBits: 9},
		{Device: "x", Parity: "weird"}, {Device: "x", Flow: "dtr"}, {Device: "x", Enter: "tab"},
	} {
		if err := bad.Normalize(); err == nil {
			t.Errorf("%+v should be refused", bad)
		}
	}
}

func TestStoreAddRenameDelete(t *testing.T) {
	s := newDeviceStore(filepath.Join(t.TempDir(), "sub", "devices.json"))
	if err := s.Put("", Device{Name: "b", Settings: map[string]any{"flow": "rtscts"}}); err != nil {
		t.Fatal(err)
	}
	if err := s.Put("", Device{Name: "A"}); err != nil {
		t.Fatal(err)
	}
	if err := s.Put("", Device{Name: "a"}); err == nil {
		t.Error("a name differing only by case should be refused")
	}
	if err := s.Put("b", Device{Name: "c", Settings: map[string]any{"flow": "rtscts"}}); err != nil {
		t.Fatal(err)
	}
	if err := s.Put("missing", Device{Name: "z"}); err == nil {
		t.Error("editing a missing device should fail")
	}
	list, _ := s.List()
	if len(list) != 2 || list[0].Name != "A" || list[1].Name != "c" || list[1].Settings["flow"] != "rtscts" {
		t.Fatalf("unexpected list: %+v", list)
	}
	if err := s.Delete("A"); err != nil {
		t.Fatal(err)
	}
	if err := s.Delete("A"); err == nil {
		t.Error("deleting twice should fail")
	}
}

func TestScriptNormalizeAndStore(t *testing.T) {
	for _, bad := range []Script{
		{Name: "x"}, {Name: "x", Commands: []string{"", " "}},
		{Name: "x", Commands: []string{"a"}, DelayMs: -1}, {Name: "x", Commands: []string{"a"}, Count: -1},
	} {
		if err := bad.Normalize(); err == nil {
			t.Errorf("%+v should be refused", bad)
		}
	}
	s := NewScriptStore(filepath.Join(t.TempDir(), "scripts.json"))
	if err := s.Put("", Script{Name: "boot", Device: "Lab", Commands: []string{"help", "", "ver", ""}, DelayMs: 200, Loop: true}); err != nil {
		t.Fatal(err)
	}
	got, err := s.Get("boot")
	if err != nil || len(got.Commands) != 3 || got.Device != "Lab" || !got.Loop {
		t.Fatalf("unexpected script: %+v %v", got, err)
	}
	if _, err := s.Get("nope"); err == nil {
		t.Error("a missing script should be an error")
	}
	if err := s.Delete("boot"); err != nil {
		t.Fatal(err)
	}
}

func TestAScriptOfGenericIsForEveryPort(t *testing.T) {
	s := Script{Name: "a", Device: " generic ", Commands: []string{"x"}}
	if err := s.Normalize(); err != nil || s.Device != "" {
		t.Errorf("device %q, err %v", s.Device, err)
	}
}

func TestPuttyStyleOptionsAreValidated(t *testing.T) {
	good := Profile{Device: "x", Bold: "font", RightClick: "paste",
		Colours: "background=#101820\n\nforeground = #AABBCC\nbrightRed=#ff0000"}
	if err := good.Normalize(); err != nil {
		t.Fatal(err)
	}
	def := Profile{Device: "x"}
	def.Normalize()
	if def.Bold != "colour" || def.RightClick != "copypaste" {
		t.Errorf("defaults: %+v", def)
	}
	if !asserted(def.AutoWrap) {
		t.Error("auto wrap is on unless turned off")
	}
	for _, bad := range []Profile{
		{Device: "x", Bold: "huge"}, {Device: "x", RightClick: "menu"},
		{Device: "x", Colours: "background=blue"}, {Device: "x", Colours: "sky=#112233"}, {Device: "x", Colours: "background"},
		{Device: "x", BellSound: "http://example.invalid/a.mp3"},
	} {
		if err := bad.Normalize(); err == nil {
			t.Errorf("%+v should be refused", bad)
		}
	}
	okSound := Profile{Device: "x", BellSound: "data:audio/wav;base64,AAAA"}
	if err := okSound.Normalize(); err != nil {
		t.Error(err)
	}
}

func TestStepsAndRulesAreValidated(t *testing.T) {
	for _, bad := range []string{"@dtr=2", "@wait=-1", "@hex=0", "@hex=zz", "@speed=1", "@dtr"} {
		sc := Script{Name: "s", Commands: []string{bad}}
		if sc.Normalize() == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
	good := Script{Name: "s", Commands: []string{"@dtr=1; wait=500; dtr=0", "@hex=0x0d,0a", "@@literal"}}
	if err := good.Normalize(); err != nil {
		t.Error(err)
	}
	for _, bad := range []string{"no arrow", " => send x", "/(/ => send x", "a => send @wait=x"} {
		p := Profile{Device: "x", Rules: bad}
		if p.Normalize() == nil {
			t.Errorf("rule %q should be refused", bad)
		}
	}
	p := Profile{Device: "x", Rules: "# comment\nlogin: => send root\n/a+/ => send @wait=1", View: "hex", LinePauseMs: 100}
	if err := p.Normalize(); err != nil {
		t.Error(err)
	}
	if (&Profile{Device: "x", CharDelayMs: -1}).Normalize() == nil || (&Profile{Device: "x", LineDelayMs: 6000}).Normalize() == nil {
		t.Error("bad send delays were accepted")
	}
	if (&Profile{Device: "x", TimeMode: "never"}).Normalize() == nil {
		t.Error("a bad time mode was accepted")
	}
	if (&Profile{Device: "x", View: "octal"}).Normalize() == nil || (&Profile{Device: "x", LinePauseMs: -1}).Normalize() == nil {
		t.Error("bad view or pause accepted")
	}
}

func FuzzParseSteps(f *testing.F) {
	for _, seed := range []string{"dtr=1; wait=500; dtr=0", "hex=0D 0A", "", ";;", "wait=99999999999999999999", "hex=zz", "dtr", "=", "hex=0x0d,0a;wait=1"} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, text string) {
		steps, err := parseSteps(text)
		if err == nil && len(steps) == 0 {
			t.Fatalf("%q parsed to nothing without an error", text)
		}
		for _, s := range steps {
			if s.ms < 0 || s.ms > maxWaitMs {
				t.Fatalf("%q: wait %d out of range", text, s.ms)
			}
		}
	})
}

func FuzzParseRules(f *testing.F) {
	for _, seed := range []string{"login: => send admin", "/a+/ => send @wait=1", "# c", "=>", "/(/ => send x", "a => send @hex=zz", "//=>red", "/ / => hide",
		"every 1s => mark", "button A => send a", "on cts => note", "x => graph T", "idle 2m => sound"} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, text string) {
		rules, err := parseRules(text)
		if err != nil {
			return
		}
		for _, r := range rules {
			if r.when == "match" {
				r.pattern.FindIndex([]byte("some text the device sent"))
			}
			if (r.when == "every" || r.when == "idle") && r.every < 10*time.Millisecond {
				t.Fatalf("%q: every %v", text, r.every)
			}
		}
	})
}

func TestPortsAreSortedNaturally(t *testing.T) {
	ports := []string{"COM10", "COM2", "COM1", "/dev/ttyUSB10", "/dev/ttyUSB2", "/dev/ttyACM0", "COM3"}
	sort.SliceStable(ports, func(i, j int) bool { return naturalLess(ports[i], ports[j]) })
	want := []string{"/dev/ttyACM0", "/dev/ttyUSB2", "/dev/ttyUSB10", "COM1", "COM2", "COM3", "COM10"}
	if strings.Join(ports, " ") != strings.Join(want, " ") {
		t.Errorf("got %v", ports)
	}
	if naturalLess("a", "a") || !naturalLess("a", "a1") || naturalLess("COM01", "COM1") && naturalLess("COM1", "COM01") {
		t.Error("equal and prefix names")
	}
}

// stepsText describes steps in a few words, for the tests.
func stepsText(steps []step) string {
	var out []string
	for _, s := range steps {
		switch s.kind {
		case "text":
			out = append(out, "text="+escapeText(s.bytes))
		case "hex", "dec", "bin":
			out = append(out, s.kind+"="+strings.ToUpper(hex.EncodeToString(s.bytes)))
		case "end":
			if s.value == "enter" {
				out = append(out, "end=enter")
			} else {
				out = append(out, "end="+strings.ToUpper(hex.EncodeToString(s.bytes)))
			}
		case "check", "file", "mark":
			out = append(out, s.kind+"="+s.value)
		case "dtr", "rts", "log":
			out = append(out, fmt.Sprintf("%s=%d", s.kind, map[bool]int{false: 0, true: 1}[s.on]))
		case "wait", "timeout", "break":
			out = append(out, fmt.Sprintf("%s=%d", s.kind, s.ms))
		case "baud":
			out = append(out, fmt.Sprintf("baud=%d", s.n))
		case "expect":
			out = append(out, "expect="+s.pattern.Text)
		}
	}
	return strings.Join(out, " ")
}

func TestExpectAndTimeoutSteps(t *testing.T) {
	steps, err := parseSteps("timeout=3000; expect=login: ; expect=/pass(word)?:/; dtr=1")
	if err != nil || len(steps) != 4 || steps[0].ms != 3000 || steps[1].pattern.Text != "login:" || !steps[2].pattern.MatchString("password:") || !steps[1].pattern.MatchString("LOGIN:") {
		t.Fatalf("%v %+v", err, steps)
	}
	for _, bad := range []string{"expect=", "expect=/(/", "timeout=0", "timeout=x", "timeout=99999999"} {
		if _, err := parseSteps(bad); err == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
	sc := Script{Name: "s", Commands: []string{"@expect=login:", "admin"}}
	if err := sc.Normalize(); err != nil {
		t.Error(err)
	}
}

func TestNaturalOrderIsAConsistentOrder(t *testing.T) {
	rng := rand.New(rand.NewSource(22))
	names := make([]string, 60)
	for i := range names {
		names[i] = []string{"COM", "ttyUSB", "ttyACM", "/dev/"}[rng.Intn(4)] + strconv.Itoa(rng.Intn(120))
	}
	for _, a := range names {
		if naturalLess(a, a) {
			t.Errorf("%q is less than itself", a)
		}
		for _, b := range names {
			if naturalLess(a, b) && naturalLess(b, a) {
				t.Errorf("%q and %q are each less than the other", a, b)
			}
			for _, c := range names {
				if naturalLess(a, b) && naturalLess(b, c) && !naturalLess(a, c) {
					t.Fatalf("not transitive: %q < %q < %q", a, b, c)
				}
			}
		}
	}
}

func TestOpeningAMissingOrBusyPortGivesAdvice(t *testing.T) {
	_, err := openPort(Profile{Device: "/dev/nothing-here-at-all"})
	if err == nil || !strings.Contains(err.Error(), "cannot open /dev/nothing-here-at-all") {
		t.Fatalf("%v", err)
	}
	if !strings.Contains(err.Error(), "no such port") && !strings.Contains(err.Error(), "No such file") {
		t.Errorf("no advice: %v", err)
	}
	for _, c := range []struct {
		err  error
		goos string
		want string
	}{
		{fs.ErrPermission, "linux", "dialout"},
		{fs.ErrPermission, "darwin", "no permission to open"},
		{errors.New("open COM3: Access is denied."), "windows", "open in another program"},
		{errors.New("device or resource busy"), "linux", "another program has the port open"},
		{errors.New("The system cannot find the file specified."), "windows", "no such port"},
	} {
		if got := openAdvice(c.err, c.goos); !strings.Contains(got, c.want) {
			t.Errorf("%v on %s: %q lacks %q", c.err, c.goos, got, c.want)
		}
	}
	if got := openAdvice(errors.New("plain"), "linux"); got != "plain" {
		t.Errorf("an error without a code is left as it is: %q", got)
	}
}
