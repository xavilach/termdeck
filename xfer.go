package main

import (
	"context"
	"errors"
	"strings"
	"time"
)

// A file transfer of a session: while it runs, what the device sends goes to the transfer and not to the
// page, the log or the triggers.

type xfer struct {
	in chan byte
}

// sessionLink lets the XMODEM engine use the port of a session.
type sessionLink struct {
	t *termSession
	x *xfer
}

func (l sessionLink) write(data []byte) error {
	l.t.mu.Lock()
	port := l.t.port
	l.t.mu.Unlock()
	if port == nil {
		return errors.New("the port is closed")
	}
	l.t.writeMu.Lock()
	defer l.t.writeMu.Unlock()
	mode, delay := l.t.rs485()
	n, err := transmit(port, mode, delay, func() (int, error) { return port.Write(data) })
	l.t.tx.Add(int64(n))
	return err
}

func (l sessionLink) read(ctx context.Context, timeout time.Duration) (byte, error) {
	select {
	case b := <-l.x.in:
		return b, nil
	case <-time.After(timeout):
		return 0, errXTimeout
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

// feedTransfer gives data from the device to the running transfer, and says whether there is one.
func (t *termSession) feedTransfer(raw []byte) bool {
	t.mu.Lock()
	x := t.xfer
	t.mu.Unlock()
	if x == nil {
		return false
	}
	for _, b := range raw {
		select {
		case x.in <- b:
		default: // the engine is not reading: drop, it will ask again
		}
	}
	return true
}

// startXmodem sends or receives a file over the port, as a job.
func (t *termSession) startXmodem(m clientMessage, times xmodemTimes) {
	t.mu.Lock()
	running := t.xfer != nil
	t.mu.Unlock()
	if running {
		t.notice("a file transfer is already running")
		return
	}
	dir := m.Action
	proto := m.Proto
	if proto == "" {
		proto = "xmodem"
	}
	report := func(extra map[string]any) {
		message := map[string]any{"type": "xfer", "dir": dir, "name": m.Name, "total": len(m.Bytes), "proto": m.Proto}
		for k, v := range extra {
			message[k] = v
		}
		t.sendJSON(message)
	}
	name := strings.ToUpper(proto) + " " + dir
	if m.Name != "" {
		name += " " + m.Name
	}
	_, err := t.startJob("transfer", name, int64(len(m.Bytes)), func(ctx context.Context, j *job) error {
		x := &xfer{in: make(chan byte, 1<<16)}
		t.mu.Lock()
		t.xfer = x
		t.mu.Unlock()
		defer func() {
			t.mu.Lock()
			t.xfer = nil
			t.mu.Unlock()
		}()
		link := sessionLink{t, x}
		last := time.Time{}
		progress := func(done int) {
			t.progress(j, int64(done))
			if time.Since(last) > 100*time.Millisecond {
				last = time.Now()
				report(map[string]any{"state": "running", "done": done})
			}
		}
		report(map[string]any{"state": "running", "done": 0})
		var err error
		var received []byte
		receivedName := ""
		switch {
		case dir == "send" && proto == "ymodem":
			err = ymodemSend(ctx, link, m.Name, m.Bytes, times, progress)
		case dir == "send":
			err = xmodemSend(ctx, link, m.Bytes, m.OneK, times, progress)
		case proto == "ymodem":
			receivedName, received, err = ymodemReceive(ctx, link, times, progress)
		default:
			received, err = xmodemReceive(ctx, link, times, progress)
		}
		switch {
		case errors.Is(err, context.Canceled):
			link.write([]byte{xCAN, xCAN, xCAN})
			report(map[string]any{"state": "cancelled"})
		case err != nil:
			report(map[string]any{"state": "failed", "message": err.Error()})
		case dir == "send":
			report(map[string]any{"state": "done", "done": len(m.Bytes)})
		default:
			report(map[string]any{"state": "done", "done": len(received), "data": received, "received": receivedName})
		}
		return nil // the dialog and its notes say how it ended
	})
	if err != nil {
		t.notice("open the port before a file transfer")
	}
}

// cancelTransfer stops a running file transfer.
func (t *termSession) cancelTransfer() {
	t.mu.Lock()
	var stop []*job
	for _, j := range t.jobs {
		if j.Kind == "transfer" {
			stop = append(stop, j)
		}
	}
	t.mu.Unlock()
	for _, j := range stop {
		j.cancel()
	}
}
