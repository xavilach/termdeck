package main

import (
	"bytes"
	"strings"
	"testing"
	"time"
)

func TestAnAutomaticSendGoesOnWhileThePageReloadsAndStopsWhenAsked(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newLine(t)
	c, ctx := dialTerminalID(t, ts, "auto-tab")
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "auto", "format": "text", "data": "ping", "end": "lf", "ms": 50})
	if got := readDevice(t, master, 15); string(got) != "ping\nping\nping\n" {
		t.Fatalf("sent %q", got)
	}
	c.CloseNow() // the page reloads
	time.Sleep(150 * time.Millisecond)
	if got := readDevice(t, master, 5); !strings.HasPrefix(string(got), "ping\n") {
		t.Fatalf("while no page is there: %q", got)
	}
	c, ctx = dialTerminalID(t, ts, "auto-tab")
	sendJSON(t, c, ctx, map[string]any{"type": "hello", "view": map[string]any{"mode": "text"}})
	m := waitFor(t, c, ctx, "session", "")
	jobs := m["jobs"].([]any)
	if len(jobs) != 1 || jobs[0].(map[string]any)["kind"] != "auto" {
		t.Fatalf("the page finds the automatic send: %v", m["jobs"])
	}
	sendJSON(t, c, ctx, map[string]any{"type": "stopjob", "job": jobs[0].(map[string]any)["id"]})
	for m := waitFor(t, c, ctx, "jobs", ""); len(m["jobs"].([]any)) > 0; m = waitFor(t, c, ctx, "jobs", "") {
	}
	got := make(chan int, 64)
	go func() { // a deadline does not wake a read of a pty here: read in the background
		buf := make([]byte, 256)
		for {
			n, err := master.Read(buf)
			if err != nil {
				return
			}
			got <- n
		}
	}()
	time.Sleep(150 * time.Millisecond) // what was on its way
	for len(got) > 0 {
		<-got
	}
	time.Sleep(300 * time.Millisecond)
	if len(got) > 0 {
		t.Errorf("still sending after Stop")
	}
}

func TestAFileIsSentAsAJobWithItsProgressAndCanBeStopped(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newLine(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave}})
	waitFor(t, c, ctx, "state", "connected")
	data := bytes.Repeat([]byte("0123456789"), 100)
	sendJSON(t, c, ctx, map[string]any{"type": "file", "name": "/some/where/fw.bin", "bytes": data})
	if got := readDevice(t, master, len(data)); !bytes.Equal(got, data) {
		t.Fatalf("the device got %d bytes", len(got))
	}
	waitNote(t, c, ctx, "file fw.bin sent (1000 bytes)")

	// a slow one, stopped half way
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "charDelayMs": 5}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "file", "name": "big.bin", "bytes": data})
	var progress map[string]any
	for progress == nil {
		m := waitFor(t, c, ctx, "jobs", "")
		if list := m["jobs"].([]any); len(list) == 1 && list[0].(map[string]any)["done"].(float64) > 0 {
			progress = list[0].(map[string]any)
		}
	}
	if progress["kind"] != "file" || progress["name"] != "big.bin" || progress["total"] != float64(1000) {
		t.Errorf("progress %v", progress)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "stop"})
	if n := waitNote(t, c, ctx, "big.bin"); !strings.Contains(n, "stopped after") {
		t.Errorf("note %q", n)
	}
}

func TestAJobNeedsAnOpenPortAndEndsWithIt(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "auto", "format": "text", "data": "x", "ms": 10})
	waitNote(t, c, ctx, "automatic send: the port is closed")
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "auto", "format": "hex", "data": "41", "ms": 10})
	for m := waitFor(t, c, ctx, "jobs", ""); len(m["jobs"].([]any)) == 0; m = waitFor(t, c, ctx, "jobs", "") {
	}
	sendJSON(t, c, ctx, map[string]any{"type": "close"})
	for m := waitFor(t, c, ctx, "jobs", ""); len(m["jobs"].([]any)) > 0; m = waitFor(t, c, ctx, "jobs", "") {
	}
}
