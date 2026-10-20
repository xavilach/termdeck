package main

import (
	"context"
	"errors"
	"time"
)

// A file transfer of a session: while it runs, what the device sends goes to the transfer and not to the
// page, the log or the triggers.

type xfer struct {
	in     chan byte
	cancel context.CancelFunc
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

// startXmodem sends or receives a file over the port, in the background.
func (t *termSession) startXmodem(m clientMessage, times xmodemTimes) {
	t.mu.Lock()
	ctx, port, running := t.runCtx, t.port, t.xfer != nil
	t.mu.Unlock()
	switch {
	case port == nil || ctx == nil:
		t.notice("open the port before a file transfer")
		return
	case running:
		t.notice("a file transfer is already running")
		return
	}
	ctx, cancel := context.WithCancel(ctx)
	x := &xfer{in: make(chan byte, 1<<16), cancel: cancel}
	t.mu.Lock()
	t.xfer = x
	t.mu.Unlock()
	link := sessionLink{t, x}
	dir := m.Action
	report := func(extra map[string]any) {
		message := map[string]any{"type": "xfer", "dir": dir, "name": m.Name, "total": len(m.Bytes), "proto": m.Proto}
		for k, v := range extra {
			message[k] = v
		}
		t.sendJSON(message)
	}
	go func() {
		defer func() {
			t.mu.Lock()
			t.xfer = nil
			t.mu.Unlock()
			cancel()
		}()
		last := time.Time{}
		progress := func(done int) {
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
		case dir == "send" && m.Proto == "ymodem":
			err = ymodemSend(ctx, link, m.Name, m.Bytes, times, progress)
		case dir == "send":
			err = xmodemSend(ctx, link, m.Bytes, m.OneK, times, progress)
		case m.Proto == "ymodem":
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
	}()
}

// cancelTransfer stops a running file transfer.
func (t *termSession) cancelTransfer() {
	t.mu.Lock()
	x := t.xfer
	t.mu.Unlock()
	if x != nil {
		x.cancel()
	}
}
