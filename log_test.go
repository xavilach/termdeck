package main

import (
	"fmt"
	"math/rand"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

func TestLogLinesCarryTheirMarkers(t *testing.T) {
	path := filepath.Join(t.TempDir(), "logs", "{name}-{date}.log")
	l, err := OpenLog(Profile{Name: "a/b", LogPath: path, LogRxMarker: "<< ", LogTxMarker: ">> "}, "append")
	if err != nil {
		t.Fatal(err)
	}
	l.Write(dirTx, []byte("AT"))
	l.Write(dirTx, []byte("\r\n"))
	l.Write(dirRx, []byte("OK\r\nREA"))
	l.Write(dirRx, []byte("DY\r\n\r\n"))
	l.Write(dirTx, []byte("ver\r"))
	l.Write(dirRx, []byte("prompt> "))
	l.Close() // writes the unfinished line
	files, _ := filepath.Glob(filepath.Join(filepath.Dir(path), "a_b-*.log"))
	if len(files) != 1 {
		t.Fatalf("expected one log file, got %v", files)
	}
	data, _ := os.ReadFile(files[0])
	want := ">> AT\n<< OK\n<< READY\n<< \n>> ver\n<< prompt> \n"
	if string(data) != want {
		t.Errorf("got %q, want %q", data, want)
	}
}

func TestLogWritesAnUnfinishedLineAfterAPause(t *testing.T) {
	path := filepath.Join(t.TempDir(), "x.log")
	l, _ := OpenLog(Profile{LogPath: path}, "append")
	defer l.Close()
	l.Write(dirRx, []byte("login: "))
	time.Sleep(logIdle + 200*time.Millisecond)
	if data, _ := os.ReadFile(path); string(data) != "login: \n" {
		t.Errorf("got %q", data)
	}
}

func TestNoLogWithoutAPathAndNilIsSafe(t *testing.T) {
	l, err := OpenLog(Profile{}, "append")
	if l != nil || err != nil {
		t.Fatalf("got %v %v", l, err)
	}
	l.Write(dirRx, []byte("x"))
	l.Close()
}

func TestPrintableOnlyDropsEscapesAndControls(t *testing.T) {
	cases := map[string]string{
		"plain":                   "plain",
		"\x1b[31mred\x1b[0m text": "red text",
		"\x1b]0;title\x07after":   "after",
		"\x1b]0;title\x1b\\after": "after",
		"ab\bc":                   "ac",
		"\x1b(0lqk\x1b(B":         "lqk",
		"tab\there\x00\x07":       "tab\there",
		"caf\u00e9\x1b[K":         "caf\u00e9",
		"\b\bx":                   "x",
	}
	for in, want := range cases {
		if got := string(printableOnly([]byte(in))); got != want {
			t.Errorf("printableOnly(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestLogOptionsOverwriteHeaderAndPrintable(t *testing.T) {
	path := filepath.Join(t.TempDir(), "x.log")
	os.WriteFile(path, []byte("old\n"), 0o600)
	p := Profile{Name: "n", Device: "COM9", Baud: 9600, LogPath: path, LogHeader: true, LogPrintable: true}
	if !logExists(p) {
		t.Fatal("the file is there")
	}
	l, err := OpenLog(p, "overwrite")
	if err != nil {
		t.Fatal(err)
	}
	l.Write(dirRx, []byte("\x1b[1mhi\x1b[0m\r\n"))
	l.Close()
	data, _ := os.ReadFile(path)
	text := string(data)
	if strings.Contains(text, "old") || !strings.HasPrefix(text, "=~=~=~=~= termdeck log ") || !strings.HasSuffix(text, "COM9 9600 baud =~=~=~=~=\nhi\n") {
		t.Errorf("unexpected log %q", text)
	}
	l2, _ := OpenLog(p, "append")
	l2.Write(dirRx, []byte("more\r\n"))
	l2.Close()
	data, _ = os.ReadFile(path)
	if !strings.HasSuffix(string(data), "more\n") || !strings.Contains(string(data), "hi\n=~=") || strings.Count(string(data), "termdeck log") != 2 {
		t.Errorf("append kept the file: %q", data)
	}
	if l3, err := OpenLog(p, "none"); l3 != nil || err != nil {
		t.Error("answer none means no log")
	}
}

func TestALogThatCannotBeWrittenReportsItsFirstError(t *testing.T) {
	l, err := OpenLog(Profile{LogPath: filepath.Join(t.TempDir(), "x.log")}, "overwrite")
	if err != nil {
		t.Fatal(err)
	}
	l.f.Close() // as if the disk went away
	l.Write(dirRx, []byte("lost\n"))
	if l.Err() == nil {
		t.Error("the write error was not kept")
	}
	l.f = nil
	var none *Logger
	if none.Err() != nil {
		t.Error("a nil logger has no error")
	}
}

func TestAMarkIsWrittenBetweenTheLines(t *testing.T) {
	path := filepath.Join(t.TempDir(), "m.log")
	l, err := OpenLog(Profile{LogPath: path}, "overwrite")
	if err != nil {
		t.Fatal(err)
	}
	l.Write(dirRx, []byte("one\nunfinished"))
	l.Mark("mark 1 at 10:00:00")
	l.Write(dirRx, []byte("two\n"))
	l.Close()
	got, _ := os.ReadFile(path)
	if string(got) != "one\nunfinished\n---- mark 1 at 10:00:00 ----\ntwo\n" {
		t.Errorf("log %q", got)
	}
	var none *Logger
	none.Mark("x") // safe
}

func TestLogLinesCanStartWithTheirTime(t *testing.T) {
	path := filepath.Join(t.TempDir(), "t.log")
	l, err := OpenLog(Profile{LogPath: path, LogTimestamps: true, LogRxMarker: "RX "}, "overwrite")
	if err != nil {
		t.Fatal(err)
	}
	l.Write(dirRx, []byte("one\n\ntwo\n"))
	l.Close()
	got, _ := os.ReadFile(path)
	lines := strings.Split(strings.TrimRight(string(got), "\n"), "\n")
	re := regexp.MustCompile(`^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} RX (one|two)?$`)
	if len(lines) != 3 || !re.MatchString(lines[0]) || !re.MatchString(lines[1]) || !re.MatchString(lines[2]) {
		t.Errorf("log %q", got)
	}
}

func TestALogThatGetsBigStartsANewFileAndKeepsTheOldOnes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "r.log")
	l, err := OpenLog(Profile{LogPath: path, LogKeep: 2}, "overwrite")
	if err != nil {
		t.Fatal(err)
	}
	l.maxBytes = 30 // a few lines, not megabytes
	for i := 1; i <= 12; i++ {
		l.Write(dirRx, []byte(fmt.Sprintf("line %02d....\n", i))) // 11 bytes: three lines fill a file
	}
	l.Close()
	read := func(name string) string { b, _ := os.ReadFile(name); return string(b) }
	// three 12-byte lines fill a file (30 bytes): 12 lines make four full files, and two are kept
	if got := read(path + ".1"); !strings.Contains(got, "line 10") || !strings.Contains(got, "line 12") || strings.Contains(got, "line 09") {
		t.Errorf("the newest full file %q", got)
	}
	if got := read(path + ".2"); !strings.Contains(got, "line 07") || !strings.Contains(got, "line 09") {
		t.Errorf("the one before %q", got)
	}
	if _, err := os.Stat(path + ".3"); err == nil {
		t.Error("more files are kept than asked for")
	}
	if got := read(path); strings.Contains(got, "line 1") {
		t.Errorf("the current file should be a fresh one: %q", got)
	}
}

func TestPrintableOnlyKeepsNoControlCharactersAndIsStable(t *testing.T) {
	rng := rand.New(rand.NewSource(21))
	alphabet := []byte("ab \t\r\n\x1b[0m31;\x08\x00\x07]\\\x7f")
	for i := 0; i < 2000; i++ {
		in := make([]byte, rng.Intn(30))
		for j := range in {
			in[j] = alphabet[rng.Intn(len(alphabet))]
		}
		once := printableOnly(in)
		for _, b := range once {
			if b < 0x20 && b != '\t' || b == 0x7f {
				t.Fatalf("%q gave %q with a control character", in, once)
			}
		}
		if twice := printableOnly(once); string(twice) != string(once) {
			t.Fatalf("%q: not stable: %q then %q", in, once, twice)
		}
	}
}
