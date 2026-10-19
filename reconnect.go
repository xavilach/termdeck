package main

import (
	"context"
	"time"
)

// Reconnect: a port that went away (a USB adapter pulled out) is opened again when it comes back.

const reconnectEvery = time.Second

// startReconnect tries to open the port of profile again every second, until it opens, or the page
// closes the port or opens another one (which end the loop through close).
func (t *termSession) startReconnect(profile Profile) {
	ctx, stop := context.WithCancel(t.ctx)
	t.mu.Lock()
	t.reconnectStop = stop
	t.mu.Unlock()
	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			case <-time.After(reconnectEvery):
			}
			t.opMu.Lock()
			if ctx.Err() == nil && t.openPort(profile, true) {
				t.mu.Lock()
				t.reconnectStop = nil
				t.mu.Unlock()
				t.opMu.Unlock()
				return
			}
			t.opMu.Unlock()
		}
	}()
}
