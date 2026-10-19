package main

import (
	"fmt"
	"testing"
	"time"

	"termdeck/internal/serial"
)

// recordingPort notes what happens to the port, in order.
type recordingPort struct {
	serial.Port
	events []string
}

func (p *recordingPort) SetRTS(on bool) error {
	p.events = append(p.events, fmt.Sprintf("rts=%v", on))
	return nil
}
func (p *recordingPort) Drain() error { p.events = append(p.events, "drain"); return nil }

func TestTransmitPutsRTSInTheSendingStateOnlyWhileSending(t *testing.T) {
	for _, tc := range []struct{ mode, want string }{
		{"high", "rts=true write drain rts=false"},
		{"low", "rts=false write drain rts=true"},
	} {
		port := &recordingPort{}
		n, err := transmit(port, tc.mode, 5*time.Millisecond, func() (int, error) {
			port.events = append(port.events, "write")
			return 3, nil
		})
		got := ""
		for i, e := range port.events {
			if i > 0 {
				got += " "
			}
			got += e
		}
		if n != 3 || err != nil || got != tc.want {
			t.Errorf("%s: %q (n=%d err=%v), want %q", tc.mode, got, n, err, tc.want)
		}
	}
	port := &recordingPort{}
	transmit(port, "off", 0, func() (int, error) { return 1, nil })
	transmit(port, "", 0, func() (int, error) { return 1, nil })
	if len(port.events) != 0 {
		t.Errorf("without direction control the lines are left alone: %v", port.events)
	}
}

func TestRTSDirectionIsValidated(t *testing.T) {
	if (&Profile{Device: "x", RTSMode: "sideways"}).Normalize() == nil || (&Profile{Device: "x", RTSDelayMs: 5000}).Normalize() == nil {
		t.Error("bad values were accepted")
	}
	p := Profile{Device: "x"}
	p.Normalize()
	if p.RTSMode != "off" {
		t.Errorf("default %q", p.RTSMode)
	}
}
