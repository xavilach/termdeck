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

func TestTheGraphComesBackAfterAReloadUntilItIsCleared(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminalID(t, ts, "graph-tab")
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:counter", "rules": "tick => graph Tick"}})
	waitFor(t, c, ctx, "state", "connected")
	waitFor(t, c, ctx, "points", "")
	waitFor(t, c, ctx, "points", "")
	c.CloseNow()

	c, ctx = dialTerminalID(t, ts, "graph-tab")
	sendJSON(t, c, ctx, map[string]any{"type": "hello", "view": map[string]any{"mode": "text"}})
	m := waitFor(t, c, ctx, "graph", "")
	ticks, _ := m["series"].(map[string]any)["Tick"].([]any)
	if len(ticks) < 2 || ticks[0].(map[string]any)["v"] != float64(1) {
		t.Fatalf("the points after a reload: %v", m)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "cleargraph"})
	sendJSON(t, c, ctx, map[string]any{"type": "close"})
	waitFor(t, c, ctx, "state", "closed")
	sendJSON(t, c, ctx, map[string]any{"type": "hello", "view": map[string]any{"mode": "text"}})
	if m := waitFor(t, c, ctx, "graph", ""); len(m["series"].(map[string]any)) != 0 {
		t.Errorf("a cleared graph stays cleared: %v", m)
	}
}

func TestARuleCanAskForACommandOfThePage(t *testing.T) {
	if !oneOf("clear", pageCommands()) || !oneOf("freeze", pageCommands()) || len(pageCommands()) < 20 {
		t.Fatalf("the commands of the page: %v", pageCommands())
	}
	if _, err := parseRules("REBOOT => command clear\non connect => command graph"); err != nil {
		t.Error(err)
	}
	for _, bad := range []string{"x => command", "x => command explode"} {
		if _, err := parseRules(bad); err == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo", "rules": "REBOOT => command clear"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "raw", "bytes": []byte("REBOOT\n")})
	if m := waitFor(t, c, ctx, "command", ""); m["id"] != "clear" {
		t.Errorf("command %v", m)
	}
}
