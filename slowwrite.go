package main

import (
	"context"
	"time"
)

// Slow send: some devices lose characters when they come at full speed. With a delay between characters
// (and after a line end) the data goes out through one writer, in order, that can be stopped.

// outItem is data for the writer: the bytes for the port, how they show once they are sent, and (when the
// sender waits for them to be out) a channel closed once they are written or dropped.
type outItem struct {
	out, shown []byte
	done       chan struct{}
}

func (item outItem) finish() {
	if item.done != nil {
		close(item.done)
	}
}

const outQueue = 256

// startWriter starts the writer of the port that is being opened, when the profile asks for delays.
// The caller holds t.mu.
func (t *termSession) startWriter(profile Profile, ctx context.Context) {
	t.outQ = nil
	if profile.CharDelayMs == 0 && profile.LineDelayMs == 0 {
		return
	}
	t.outQ = make(chan outItem, outQueue)
	go t.writer(t.outQ, ctx, time.Duration(profile.CharDelayMs)*time.Millisecond, time.Duration(profile.LineDelayMs)*time.Millisecond)
}

func (t *termSession) writer(queue chan outItem, ctx context.Context, charDelay, lineDelay time.Duration) {
	for {
		select {
		case <-ctx.Done():
			return
		case item := <-queue:
			t.writeSlowly(ctx, item, charDelay, lineDelay)
		}
	}
}

// writeSlowly sends the bytes one at a time. A flush while it runs stops it where it is.
func (t *termSession) writeSlowly(ctx context.Context, item outItem, charDelay, lineDelay time.Duration) {
	defer item.finish()
	t.mu.Lock()
	port := t.port
	t.mu.Unlock()
	if port == nil {
		return
	}
	flushed := t.flushed.Load()
	mode, delay := t.rs485()
	t.writeMu.Lock() // the direction stays "sending" for the whole item
	defer t.writeMu.Unlock()
	completed := true
	transmit(port, mode, delay, func() (int, error) {
		for i, b := range item.out {
			if t.flushed.Load() != flushed {
				completed = false
				return i, nil
			}
			n, err := port.Write(item.out[i : i+1])
			if err != nil {
				completed = false
				return i, err
			}
			t.tx.Add(int64(n))
			pause := charDelay
			if b == '\r' || b == '\n' {
				pause += lineDelay
			}
			select {
			case <-ctx.Done():
				completed = false
				return i + 1, nil
			case <-time.After(pause):
			}
		}
		return len(item.out), nil
	})
	if completed && item.shown != nil {
		t.recordSent(item.out, item.shown)
	}
}

// flushOut drops what is waiting to be sent, and stops what is being sent.
func (t *termSession) flushOut() {
	t.flushed.Add(1)
	t.mu.Lock()
	queue := t.outQ
	t.mu.Unlock()
	for queue != nil {
		select {
		case item := <-queue:
			item.finish()
		default:
			return
		}
	}
}
