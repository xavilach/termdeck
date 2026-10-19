package main

import (
	"strconv"
	"strings"
	"testing"
	"time"
)

func values(t *testing.T, specText, line string) string {
	t.Helper()
	specs, err := parseGraphSeries(specText)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, v := range extractValues(line, specs) {
		out = append(out, v.Name+"="+strconv.FormatFloat(v.Value, 'g', -1, 64))
	}
	return strings.Join(out, " ")
}

func TestGraphSeriesGiveTheNumbersOfALine(t *testing.T) {
	specs := "# c\nTemp | Temperature: | degC\nTick | tick \nHum | /hum=([0-9.]+)%/\nRaw | /v[0-9]/"
	for line, want := range map[string]string{
		"2025 - Temperature: 26.1 degC": "Temp=26.1",
		"Temperature: 26.1 K":           "", // the suffix is missing
		"tick 12 temp":                  "Tick=12",
		"tick -3.5e1":                   "Tick=-35",
		"tick abc":                      "",
		"a hum=45.5% Temperature: 20 degC tick 7": "Temp=20 Tick=7 Hum=45.5",
		"v9": "", // the match "v9" is not a number: it starts with a letter
	} {
		if got := values(t, specs, line); got != want {
			t.Errorf("%q: got %q, want %q", line, got, want)
		}
	}
}

func TestGraphSeriesAreChecked(t *testing.T) {
	for _, bad := range []string{"no bar", "x |", " | y", "a | b | c | d", "r | /(/", "same | a\nsame | b", strings.Repeat("n", 31) + " | a"} {
		if _, err := parseGraphSeries(bad); err == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
	if specs, err := parseGraphSeries(""); err != nil || len(specs) != 0 {
		t.Error("no series is fine")
	}
	if p := (Profile{Device: "x", GraphSeries: "broken"}); p.Normalize() == nil {
		t.Error("a profile with bad series was accepted")
	}
}

func TestAPatternThatBacktracksInAJavaScriptEngineIsHarmlessHere(t *testing.T) {
	specs, _ := parseGraphSeries("A | /(a+)+b/")
	start := time.Now()
	extractValues(strings.Repeat("a", 100000), specs)
	if time.Since(start) > time.Second {
		t.Error("a long line took too long")
	}
}

func TestLinesAreCutAtCRLFAndTheEndIsKept(t *testing.T) {
	var l lineSplitter
	got := append(l.feed([]byte("one\r\ntw")), l.feed([]byte("o\n\nthree\r"))...)
	got = append(got, l.feed([]byte("\nfour"))...)
	if strings.Join(got, "|") != "one|two|three" || l.partial != "four" {
		t.Errorf("lines %q, kept %q", got, l.partial)
	}
}
