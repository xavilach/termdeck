package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestMetersCountWhatTheRulesSayAndTheSpeed(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:counter",
		"frameSpec": "end = 0A", "rules": "tick => count Ticks\ntick 2 => lap"}})
	waitFor(t, c, ctx, "state", "connected")
	waitNote(t, c, ctx, "lap 1: +")
	var last map[string]any
	for deadline := time.Now().Add(3 * time.Second); time.Now().Before(deadline); {
		m := waitFor(t, c, ctx, "counters", "")
		last = m
		counts, _ := m["counts"].([]any)
		if len(counts) == 1 && counts[0].(map[string]any)["value"].(float64) >= 3 && m["rxRate"].(float64) > 0 {
			break
		}
	}
	counts, _ := last["counts"].([]any)
	if len(counts) != 1 || counts[0].(map[string]any)["name"] != "Ticks" || last["rxRate"].(float64) <= 0 || last["laps"] != float64(1) {
		t.Errorf("meters %v", last)
	}
	if rules, _ := parseRules("tick => count Ticks"); len(rules) != 1 || rules[0].count != "Ticks" {
		t.Error("count")
	}
	if _, series := func() ([]string, []string) {
		b, _, s := ruleNames(mustRules(t, "x => count A\ny => graph B"))
		return b, s
	}(); strings.Join(series, ",") != "B,A" && strings.Join(series, ",") != "A,B" {
		t.Errorf("a count is a series of the graph: %v", series)
	}
}

func mustRules(t *testing.T, text string) []rule {
	t.Helper()
	rules, err := parseRules(text)
	if err != nil {
		t.Fatal(err)
	}
	return rules
}

func TestAMonitorListensToBothPortsAndCarriesNothing(t *testing.T) {
	ts := newTestServer(t)
	masterA, slaveA := newPty(t)
	masterB, slaveB := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "mon", "device": slaveA, "monitor": true, "peer": map[string]any{"device": slaveB}}})
	waitFor(t, c, ctx, "state", "connected")
	masterA.Write([]byte("from A\n"))
	masterB.Write([]byte("from B\n"))
	var screen string
	for !strings.Contains(screen, "from A") || !strings.Contains(screen, "from B") {
		text, err := readBinary(c, ctx)
		if err != nil {
			t.Fatalf("screen %q: %v", screen, err)
		}
		screen += string(text)
	}
	got := make(chan int, 4)
	go func() {
		buf := make([]byte, 64)
		n, _ := masterB.Read(buf)
		got <- n
	}()
	select {
	case n := <-got:
		t.Errorf("%d bytes were carried to B", n)
	case <-time.After(300 * time.Millisecond):
	}
}

func TestACaptureIsReplayedAsAPort(t *testing.T) {
	ts := newTestServer(t)
	file := filepath.Join(t.TempDir(), "capture.bin")
	os.WriteFile(file, []byte("line one\r\nline two\r\n"), 0o600)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "r", "device": "replay:" + file, "baud": 115200}})
	waitFor(t, c, ctx, "state", "connected")
	if text := readFor(t, c, ctx, 500*time.Millisecond); !strings.Contains(text, "line one\r\nline two") {
		t.Errorf("replayed %q", text)
	}
	c, ctx = dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "r", "device": "replay:" + file + ".missing"}})
	waitFor(t, c, ctx, "state", "failed")
}
