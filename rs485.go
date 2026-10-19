package main

import (
	"time"

	"termdeck/internal/serial"
)

// RS-485 over a two-wire adapter that is driven by RTS: the adapter transmits while RTS is in one state and
// listens while it is in the other. The profile says which state transmits ("high" or "low"), and for how
// long RTS stays there after the last byte went out.

var rtsModes = []string{"off", "high", "low"}

// rs485Idle is the state of RTS while the port listens.
func rs485Idle(mode string) bool { return mode != "high" }

// transmit runs write with RTS in the transmitting state, waits until the bytes are out, then puts RTS
// back. With mode "off" (or empty) it only runs write.
func transmit(port serial.Port, mode string, delay time.Duration, write func() (int, error)) (int, error) {
	if mode == "" || mode == "off" {
		return write()
	}
	port.SetRTS(!rs485Idle(mode))
	n, err := write()
	port.Drain()
	if delay > 0 {
		time.Sleep(delay)
	}
	port.SetRTS(rs485Idle(mode))
	return n, err
}
