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

// watchLines reports the control lines (the ones we drive and the ones the device drives) when they change,
// and notes in the stream the ones the device drives, when the session asks for it.
func (t *termSession) watchLines(gen int, port serial.Port, dtr, rts bool) {
	var last map[string]any
	var lastBits *serial.ModemStatusBits
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
		t.noteLines(lastBits, bits)
		lastBits = bits
		now := map[string]any{"type": "lines", "cts": bits.CTS, "dsr": bits.DSR, "dcd": bits.DCD, "ri": bits.RI, "dtr": dtr, "rts": rts}
		if fmt.Sprint(now) != fmt.Sprint(last) {
			last = now
			t.sendJSON(now)
		}
	}
}

// noteLines notes the lines of the device that changed, when the session asks for it.
func (t *termSession) noteLines(before, now *serial.ModemStatusBits) {
	t.mu.Lock()
	wanted := t.profile.NoteLines
	t.mu.Unlock()
	if before == nil || !wanted {
		return
	}
	for _, l := range []struct {
		name     string
		was, now bool
	}{{"CTS", before.CTS, now.CTS}, {"DSR", before.DSR, now.DSR}, {"DCD", before.DCD, now.DCD}, {"RI", before.RI, now.RI}} {
		if l.was != l.now {
			state := "off"
			if l.now {
				state = "on"
			}
			t.note(l.name + " " + state)
		}
	}
}

// setBaud changes the speed of the open port, and notes it.
func (t *termSession) setBaud(baud int) error {
	t.mu.Lock()
	port := t.port
	t.profile.Baud = baud
	profile := t.profile
	t.mu.Unlock()
	if port == nil {
		return fmt.Errorf("the port is closed")
	}
	if err := port.SetMode(serialMode(profile)); err != nil {
		return fmt.Errorf("baud %d: %w", baud, err)
	}
	t.note(fmt.Sprintf("baud %d", baud))
	return nil
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
		t.notice("cannot set " + line + ": " + err.Error())
	}
}

// sendBreak holds the line in the break state for d.
func (t *termSession) sendBreak(d time.Duration) {
	t.mu.Lock()
	port := t.port
	t.mu.Unlock()
	switch {
	case port == nil:
		t.note("break failed: the port is closed")
	default:
		if err := port.Break(d); err != nil {
			t.note("break failed: " + err.Error())
		} else {
			t.note("break sent")
		}
	}
}
