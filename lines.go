package main

import (
	"fmt"
	"time"

	"termdeck/internal/serial"
)

// What a session reports about the port besides its data: the control lines and the byte counters.

// watchCounters reports the bytes received and sent when they change.
func (t *termSession) watchCounters(gen int) {
	var rx, tx int64 = -1, -1
	for ; ; time.Sleep(250 * time.Millisecond) {
		t.mu.Lock()
		current := t.gen == gen
		t.mu.Unlock()
		if !current {
			return
		}
		if r, x := t.rx.Load(), t.tx.Load(); r != rx || x != tx {
			rx, tx = r, x
			t.sendJSON(map[string]any{"type": "counters", "rx": r, "tx": x})
		}
	}
}

// resetCounters sets both counters to zero.
func (t *termSession) resetCounters() {
	t.rx.Store(0)
	t.tx.Store(0)
}

// watchLines reports the control lines (the ones we drive and the ones the device drives) when they change.
func (t *termSession) watchLines(gen int, port serial.Port, dtr, rts bool) {
	var last map[string]any
	for ; ; time.Sleep(400 * time.Millisecond) {
		t.mu.Lock()
		current := t.gen == gen
		if current {
			dtr, rts = t.dtr, t.rts
		}
		t.mu.Unlock()
		if !current {
			return
		}
		bits, err := port.GetModemStatusBits()
		if err != nil {
			return // not every port has status lines (a pty, some adapters)
		}
		now := map[string]any{"type": "lines", "cts": bits.CTS, "dsr": bits.DSR, "dcd": bits.DCD, "ri": bits.RI, "dtr": dtr, "rts": rts}
		if fmt.Sprint(now) != fmt.Sprint(last) {
			last = now
			t.sendJSON(now)
		}
	}
}

// setLine drives DTR or RTS.
func (t *termSession) setLine(line string, value bool) {
	t.mu.Lock()
	port := t.port
	t.mu.Unlock()
	if port == nil {
		return
	}
	var err error
	switch line {
	case "dtr":
		if err = port.SetDTR(value); err == nil {
			t.mu.Lock()
			t.dtr = value
			t.mu.Unlock()
		}
	case "rts":
		if err = port.SetRTS(value); err == nil {
			t.mu.Lock()
			t.rts = value
			t.mu.Unlock()
		}
	}
	if err != nil {
		t.sendJSON(map[string]string{"type": "notice", "text": "cannot set " + line + ": " + err.Error()})
	}
}

func (t *termSession) sendBreak() {
	t.mu.Lock()
	port := t.port
	t.mu.Unlock()
	reply := map[string]any{"type": "break", "ok": port != nil}
	if port == nil {
		reply["message"] = "the port is closed"
	} else if err := port.Break(250 * time.Millisecond); err != nil {
		reply["ok"], reply["message"] = false, err.Error()
	}
	t.sendJSON(reply)
}
