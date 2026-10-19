package main

import (
	"os"
	"path/filepath"
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
