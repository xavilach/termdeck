package main

import (
	"path/filepath"
	"sort"
	"strings"
	"testing"
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
	s := NewStore(filepath.Join(t.TempDir(), "sub", "profiles.json"))
	if err := s.Put("", Profile{Name: "b", Device: "COM2", Flow: "rtscts"}); err != nil {
		t.Fatal(err)
	}
	if err := s.Put("", Profile{Name: "A", Device: "COM1"}); err != nil {
		t.Fatal(err)
	}
	if err := s.Put("", Profile{Name: "a", Device: "COM9"}); err == nil {
		t.Error("a name differing only by case should be refused")
	}
	if err := s.Put("b", Profile{Name: "c", Device: "COM2", Flow: "rtscts"}); err != nil {
		t.Fatal(err)
	}
	if err := s.Put("missing", Profile{Name: "z", Device: "COM2"}); err == nil {
		t.Error("editing a missing profile should fail")
	}
	list, _ := s.List()
	if len(list) != 2 || list[0].Name != "A" || list[1].Name != "c" || list[1].Flow != "rtscts" {
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
	if err := s.Put("", Script{Name: "boot", Group: "Lab", Commands: []string{"help", "", "ver", ""}, DelayMs: 200, Loop: true}); err != nil {
		t.Fatal(err)
	}
	got, err := s.Get("boot")
	if err != nil || len(got.Commands) != 3 || got.Group != "Lab" || !got.Loop {
		t.Fatalf("unexpected script: %+v %v", got, err)
	}
	if _, err := s.Get("nope"); err == nil {
		t.Error("a missing script should be an error")
	}
	if err := s.Delete("boot"); err != nil {
		t.Fatal(err)
	}
}

func TestGeneralIsTheEmptyGroup(t *testing.T) {
	p := Profile{Name: "a", Device: "COM1", Group: " general "}
	if err := p.Normalize(); err != nil || p.Group != "" {
		t.Errorf("group %q, err %v", p.Group, err)
	}
	s := Script{Name: "a", Group: "General", Commands: []string{"x"}}
	if err := s.Normalize(); err != nil || s.Group != "" {
		t.Errorf("group %q, err %v", s.Group, err)
	}
	if err := NewGroupStore(filepath.Join(t.TempDir(), "g.json")).Put("", "General"); err == nil {
		t.Error("a group called General should be refused")
	}
}

func TestPuttyStyleOptionsAreValidated(t *testing.T) {
	good := Profile{Device: "x", Bold: "font", RightClick: "paste", Answerback: "termdeck",
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
		{Device: "x", Answerback: strings.Repeat("a", 300)}, {Device: "x", BellSound: "http://example.invalid/a.mp3"},
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

func TestStepsAndTriggersAreValidated(t *testing.T) {
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
	for _, bad := range []string{"no arrow", " => reply", "/(/ => x", "a => @wait=x"} {
		p := Profile{Device: "x", Triggers: bad}
		if p.Normalize() == nil {
			t.Errorf("trigger %q should be refused", bad)
		}
	}
	p := Profile{Device: "x", Triggers: "# comment\nlogin: => root\n/a+/ => @wait=1", View: "hex", LinePauseMs: 100}
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

func FuzzParseTriggers(f *testing.F) {
	for _, seed := range []string{"login: => admin", "/a+/ => @wait=1", "# c", "=>", "/(/ => x", "a => @hex=zz", "//=>x", "/ / => y"} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, text string) {
		triggers, err := parseTriggers(text)
		if err != nil {
			return
		}
		for _, tr := range triggers {
			tr.re.FindIndex([]byte("some text the device sent"))
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

func TestQuickSendsAreParsedAndValidated(t *testing.T) {
	good := "# resets\nReset | hex | 41 42 | end=cr\nHello | text | AT+GMR\\r\nLines | dec | 1, 2 ,255\nBits | bin | 01000001 10\nWiggle | cmd | dtr=0; wait=100; dtr=1\nPipe | text | a | b | end=lf\n"
	got, err := parseQuickSends(good)
	if err != nil || len(got) != 6 {
		t.Fatalf("%v %+v", err, got)
	}
	if got[0].End != "cr" || got[0].Data != "41 42" || got[1].End != "" || got[5].Data != "a | b" || got[5].End != "lf" {
		t.Errorf("parsed %+v", got)
	}
	withCheck, err := parseQuickSends("Modbus | hex | 01 03 00 00 00 01 | check=crc16modbus | end=none")
	if err != nil || withCheck[0].Check != "crc16modbus" || withCheck[0].End != "none" || withCheck[0].Data != "01 03 00 00 00 01" {
		t.Errorf("%v %+v", err, withCheck)
	}
	for _, bad := range []string{"x | hex | 41 | check=crc99", "no bars", "x | float | 1", " | text | a", "x | hex | 4", "x | dec | 256", "x | bin | 102", "x | cmd | speed=1", "x | text | a | end=never", strings.Repeat("n", 41) + " | text | a"} {
		if _, err := parseQuickSends(bad); err == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
	p := Profile{Device: "x", QuickSends: "bad"}
	if p.Normalize() == nil {
		t.Error("a profile with a bad quick send was accepted")
	}
}

func TestExpectAndTimeoutSteps(t *testing.T) {
	steps, err := parseSteps("timeout=3000; expect=login: ; expect=/pass(word)?:/; dtr=1")
	if err != nil || len(steps) != 4 || steps[0].ms != 3000 || steps[1].label != "login:" || !steps[2].pattern.MatchString("password:") || steps[1].pattern.MatchString("LOGIN:") {
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
