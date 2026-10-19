package main

import (
	"context"
	"time"
)

// Slow send: some devices lose characters when they come at full speed. With a delay between characters
// (and after a line end) the data goes out through one writer, in order, that can be stopped.

// outItem is data for the writer: the bytes for the port, and what is recorded in the log once it is sent.
type outItem struct{ out, logged []byte }

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
	t.mu.Lock()
	port, log := t.port, t.log
	t.mu.Unlock()
	if port == nil {
		return
	}
	flushed := t.flushed.Load()
	for i, b := range item.out {
		if t.flushed.Load() != flushed {
			return
		}
		t.writeMu.Lock()
		n, err := port.Write(item.out[i : i+1])
		t.writeMu.Unlock()
		if err != nil {
			return
		}
		t.tx.Add(int64(n))
		pause := charDelay
		if b == '\r' || b == '\n' {
			pause += lineDelay
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(pause):
		}
	}
	log.Write(dirTx, item.logged)
}

// flushOut drops what is waiting to be sent, and stops what is being sent.
func (t *termSession) flushOut() {
	t.flushed.Add(1)
	t.mu.Lock()
	queue := t.outQ
	t.mu.Unlock()
	for queue != nil {
		select {
		case <-queue:
		default:
			return
		}
	}
}
