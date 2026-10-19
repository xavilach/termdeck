package main

import (
	"context"
	"sync/atomic"
	"time"

	"termdeck/internal/serial"
)

// A tunnel joins two ports: what one port receives is written to the other, and both directions are shown
// in the tab and written to the log. It is a session with a second port, the peer of the profile.

type tunnel struct {
	a, b           serial.Port
	devA, devB     string
	log            *Logger
	cancel         context.CancelFunc
	a2b, b2a       atomic.Int64
	writeA, writeB chanMutex // one write at a time to each port
}

// chanMutex is a lock made of a channel.
type chanMutex chan struct{}

func newChanMutex() chanMutex { return make(chanMutex, 1) }

func (m chanMutex) lock()   { m <- struct{}{} }
func (m chanMutex) unlock() { <-m }

const tunnelA2B, tunnelB2A = "a2b", "b2a"

// openTunnel opens both ports of profile and its peer, and starts carrying data between them.
func (t *termSession) openTunnel(profile Profile) bool {
	t.close()
	if err := profile.Normalize(); err != nil {
		t.sendState("failed", err.Error())
		return false
	}
	peer := profile.Peer // normalized with it
	t.sendState("connecting", "")
	if !t.srv.claimPort(profile.Device, t) {
		t.sendState("failed", profile.Device+" is already open in another tab")
		return false
	}
	if !t.srv.claimPort(peer.Device, t) {
		t.srv.releasePort(profile.Device, t)
		t.sendState("failed", peer.Device+" is already open in another tab")
		return false
	}
	release := func() { t.srv.releasePort(profile.Device, t); t.srv.releasePort(peer.Device, t) }
	a, err := openPort(profile)
	if err != nil {
		release()
		t.sendState("failed", err.Error())
		return false
	}
	b, err := openPort(*peer)
	if err != nil {
		a.Close()
		release()
		t.sendState("failed", err.Error())
		return false
	}
	logProfile := profile
	if logProfile.LogRxMarker == "" && logProfile.LogTxMarker == "" {
		logProfile.LogRxMarker, logProfile.LogTxMarker = "A>B ", "B>A "
	}
	existing := profile.LogExisting
	if existing == "ask" {
		existing = "append"
		if logExists(profile) {
			existing = t.askAboutLog(profile)
		}
	}
	log, logErr := OpenLog(logProfile, existing)
	ctx, cancel := context.WithCancel(t.ctx)
	tun := &tunnel{a: a, b: b, devA: profile.Device, devB: peer.Device, log: log, cancel: cancel, writeA: newChanMutex(), writeB: newChanMutex()}
	t.mu.Lock()
	t.gen++
	gen := t.gen
	t.tun, t.profile = tun, profile
	t.mu.Unlock()
	t.sendState("connected", "")
	t.sendJSON(map[string]any{"type": "logging", "on": log != nil})
	if logErr != nil {
		t.sendJSON(map[string]string{"type": "notice", "text": "log not written: " + logErr.Error()})
	}
	go t.pump(gen, ctx, tun, a, b, tun.writeB, tunnelA2B, &tun.a2b)
	go t.pump(gen, ctx, tun, b, a, tun.writeA, tunnelB2A, &tun.b2a)
	go t.watchTunnel(gen, ctx, tun)
	return true
}

// pump carries what from receives to to, and tells the page.
func (t *termSession) pump(gen int, ctx context.Context, tun *tunnel, from, to serial.Port, toLock chanMutex, dir string, count *atomic.Int64) {
	buf := make([]byte, 4096)
	for {
		n, err := from.Read(buf)
		if n > 0 {
			data := append([]byte(nil), buf[:n]...)
			toLock.lock()
			_, werr := to.Write(data)
			toLock.unlock()
			count.Add(int64(n))
			if dir == tunnelA2B {
				tun.log.Write(dirRx, data)
			} else {
				tun.log.Write(dirTx, data)
			}
			t.sendJSON(map[string]any{"type": "tunnel", "dir": dir, "t": time.Now().UnixMilli(), "data": data})
			if werr != nil && err == nil {
				err = werr
			}
		}
		if err != nil || n == 0 {
			t.mu.Lock()
			current := t.gen == gen
			t.mu.Unlock()
			if current && ctx.Err() == nil {
				t.close()
				message := "a port was closed"
				if err != nil {
					message = err.Error()
				}
				t.sendState("ended", message)
			}
			return
		}
	}
}

// watchTunnel reports the bytes carried each way, as the counters of the tab: received is A to B, sent is B to A.
func (t *termSession) watchTunnel(gen int, ctx context.Context, tun *tunnel) {
	var last [2]int64 = [2]int64{-1, -1}
	for ctx.Err() == nil {
		select {
		case <-ctx.Done():
			return
		case <-time.After(250 * time.Millisecond):
		}
		if now := [2]int64{tun.a2b.Load(), tun.b2a.Load()}; now != last {
			last = now
			t.sendJSON(map[string]any{"type": "counters", "rx": now[0], "tx": now[1]})
		}
	}
}

// closeTunnel closes the ports of a tunnel and its log.
func (t *termSession) closeTunnel(tun *tunnel) {
	tun.cancel()
	tun.a.Close()
	tun.b.Close()
	t.srv.releasePort(tun.devA, t)
	t.srv.releasePort(tun.devB, t)
	tun.log.Close()
}
