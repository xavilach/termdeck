package main

import (
	"io"
	"os"
	"testing"
	"time"

	"termdeck/internal/serial"
)

// line is the other side of the port a test opens: what the session sends comes out here, and what
// is written here reaches the session.
type line interface {
	io.ReadWriter
	SetReadDeadline(time.Time) error
}

// newPtyLine is set where there are ptys (Linux).
var newPtyLine func(t *testing.T) (line, string)

// newLine returns the other side and the name of the port, which stands in for a serial port: a pty
// where there is one, else the fake null-modem cable (one per test). TERMDECK_FAKE_LINE=1 takes the
// cable on Linux too, to run the tests as they run on Windows.
func newLine(t *testing.T) (line, string) {
	t.Helper()
	if newPtyLine != nil && os.Getenv("TERMDECK_FAKE_LINE") == "" {
		return newPtyLine(t)
	}
	port, err := openFake("fake:link-b", true, true)
	if err != nil {
		t.Fatal(err)
	}
	l := &fakeLine{port: port, data: make(chan []byte, 64)}
	t.Cleanup(func() { port.Close() })
	go l.pump()
	return l, "fake:link-a"
}

// fakeLine gives the end of the fake cable the read deadline of a file: one reader in the
// background, so a read that times out loses nothing.
type fakeLine struct {
	port     serial.Port
	data     chan []byte
	rest     []byte
	deadline time.Time
}

func (l *fakeLine) pump() {
	for {
		buf := make([]byte, 256)
		n, err := l.port.Read(buf)
		if err != nil {
			close(l.data)
			return
		}
		l.data <- buf[:n]
	}
}

func (l *fakeLine) Read(p []byte) (int, error) {
	if len(l.rest) == 0 {
		var timeout <-chan time.Time
		if !l.deadline.IsZero() {
			timer := time.NewTimer(time.Until(l.deadline))
			defer timer.Stop()
			timeout = timer.C
		}
		select {
		case data, ok := <-l.data:
			if !ok {
				return 0, io.EOF
			}
			l.rest = data
		case <-timeout:
			return 0, os.ErrDeadlineExceeded
		}
	}
	n := copy(p, l.rest)
	l.rest = l.rest[n:]
	return n, nil
}

func (l *fakeLine) Write(p []byte) (int, error)       { return l.port.Write(p) }
func (l *fakeLine) SetReadDeadline(d time.Time) error { l.deadline = d; return nil }
