package main

import (
	"fmt"
	"sort"
	"time"
)

// Meters: what a session counts and measures, shown in the bar above the lines: the bytes received and sent
// and their speed, the counts of the rules ("count Name"), the frames whose check is OK or BAD, and the laps
// of the stopwatch ("lap"). A count is also a point of the graph.

type meters struct {
	counts    map[string]int64
	framesOK  int64
	framesBad int64
	laps      int
	lastLap   time.Time
}

// resetMeters starts the meters again (the port opened, the lines were cleared). The caller holds t.mu.
func (t *termSession) resetMeters() {
	t.meters = meters{counts: map[string]int64{}, lastLap: time.Now()}
}

// countUp adds one to a count of the rules, and draws it on the graph.
func (t *termSession) countUp(name string) {
	t.mu.Lock()
	t.meters.counts[name]++
	n := t.meters.counts[name]
	t.mu.Unlock()
	t.sendJSON(map[string]any{"type": "points", "t": time.Now().UnixMilli(), "values": []graphValue{{name, float64(n)}}})
}

// countFrame counts a frame whose check was verified.
func (t *termSession) countFrame(ok bool) {
	t.mu.Lock()
	if ok {
		t.meters.framesOK++
	} else {
		t.meters.framesBad++
	}
	t.mu.Unlock()
}

// lap notes the time since the last lap (or since the port opened): a stopwatch for the rules.
func (t *termSession) lap(context string) {
	t.mu.Lock()
	t.meters.laps++
	n, since := t.meters.laps, time.Since(t.meters.lastLap)
	t.meters.lastLap = time.Now()
	t.mu.Unlock()
	text := fmt.Sprintf("lap %d: %s", n, formatDelta(since))
	if context != "" {
		text += " (" + context + ")"
	}
	t.note(text)
}

// meterMessage is what the page shows: the counters with their speed (bytes in the last second), and the meters.
func (t *termSession) meterMessage(rxRate, txRate int64) map[string]any {
	t.mu.Lock()
	defer t.mu.Unlock()
	names := make([]string, 0, len(t.meters.counts))
	for name := range t.meters.counts {
		names = append(names, name)
	}
	sort.Strings(names)
	counts := make([]map[string]any, 0, len(names))
	for _, name := range names {
		counts = append(counts, map[string]any{"name": name, "value": t.meters.counts[name]})
	}
	return map[string]any{"type": "counters", "rx": t.rx.Load(), "tx": t.tx.Load(), "rxRate": rxRate, "txRate": txRate,
		"counts": counts, "framesOK": t.meters.framesOK, "framesBad": t.meters.framesBad, "laps": t.meters.laps}
}
