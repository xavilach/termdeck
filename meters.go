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
	t.addPoints(time.Now(), []graphValue{{name, float64(n)}})
}

// maxPoints is how many points of each series the session keeps, as the page does.
const maxPoints = 20000

// graphPoint is a point of the graph as the session keeps it.
type graphPoint struct {
	T int64   `json:"t"`
	V float64 `json:"v"`
}

// addPoints keeps the points of the graph (so that a reload draws them again) and sends them to the page.
func (t *termSession) addPoints(at time.Time, values []graphValue) {
	ms := at.UnixMilli()
	t.mu.Lock()
	if t.points == nil {
		t.points = map[string][]graphPoint{}
	}
	for _, v := range values {
		list := append(t.points[v.Name], graphPoint{ms, v.Value})
		if len(list) > maxPoints*5/4 { // drop the oldest in one go
			list = append([]graphPoint(nil), list[len(list)-maxPoints:]...)
		}
		t.points[v.Name] = list
	}
	t.mu.Unlock()
	t.sendJSON(map[string]any{"type": "points", "t": ms, "values": values})
}

// sendGraph gives the page every point kept, for a page that attaches (after a reload).
func (t *termSession) sendGraph() {
	t.mu.Lock()
	all := map[string][]graphPoint{}
	for name, list := range t.points {
		all[name] = append([]graphPoint(nil), list[max(len(list)-maxPoints, 0):]...)
	}
	t.mu.Unlock()
	t.sendJSON(map[string]any{"type": "graph", "series": all})
}

// clearGraph forgets the points (Clear graph in the page).
func (t *termSession) clearGraph() {
	t.mu.Lock()
	t.points = nil
	t.mu.Unlock()
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
