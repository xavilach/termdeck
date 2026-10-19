package main

import (
	"context"
	"errors"
	"fmt"
	"regexp"
	"time"
)

const maxRepliesPerSecond = 20

// What a session sends on its own: script lines, steps typed in Cmd and replies to triggers.

// runSteps does the steps in order. It returns nil when all were done, the error of the context when
// it ended, or what went wrong (an expect that did not come in time).
func (t *termSession) runSteps(ctx context.Context, steps []step) error {
	timeout := defaultExpectTimeout
	for _, s := range steps {
		switch s.kind {
		case "dtr", "rts":
			t.setLine(s.kind, s.on)
		case "hex":
			t.writeRaw(s.bytes)
		case "timeout":
			timeout = time.Duration(s.ms) * time.Millisecond
		case "expect":
			if err := t.expect(ctx, s.pattern, timeout); err != nil {
				if ctx.Err() != nil {
					return ctx.Err()
				}
				return fmt.Errorf("waited %v for %q: %w", timeout, s.label, err)
			}
		case "wait":
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(time.Duration(s.ms) * time.Millisecond):
			}
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
	}
	return nil
}

// match looks for the triggers in what the device sent and answers them. It returns the end of the data, to
// look at again with the next chunk.
func (t *termSession) match(ctx context.Context, triggers []trigger, window, text []byte) []byte {
	if len(triggers) == 0 {
		return nil
	}
	t.mu.Lock()
	enter := enterBytes[t.profile.Enter]
	t.mu.Unlock()
	window = append(window, text...)
	for fired := true; fired; {
		fired = false
		for _, tr := range triggers {
			if loc := tr.re.FindIndex(window); loc != nil {
				window = window[loc[1]:]
				fired = true
				t.answer(ctx, tr, enter)
				break
			}
		}
	}
	if len(window) > 4096 {
		window = window[len(window)-4096:]
	}
	return window
}

// answer sends the reply of a trigger in the background, in order with the others. Too many waiting are dropped.
func (t *termSession) answer(ctx context.Context, tr trigger, enter string) {
	if t.looping() {
		return
	}
	if t.pending.Add(1) > maxPendingReplies {
		t.pending.Add(-1)
		t.sendJSON(map[string]string{"type": "notice", "text": "trigger replies are backed up: dropped " + tr.label})
		return
	}
	go func() {
		defer t.pending.Add(-1)
		t.replyMu.Lock()
		defer t.replyMu.Unlock()
		if ctx.Err() != nil {
			return
		}
		t.sendJSON(map[string]string{"type": "trigger", "match": tr.label})
		if tr.steps != nil {
			t.reportSteps(ctx, t.runSteps(ctx, tr.steps))
			return
		}
		t.write([]byte(tr.reply + enter))
		t.sendJSON(map[string]string{"type": "tx", "data": tr.reply})
	}()
}

// runScript sends the commands of the named script, pausing after each, in the background.
func (t *termSession) runScript(name string) {
	script, err := t.srv.scripts.Get(name)
	t.mu.Lock()
	open := t.port != nil
	if err == nil && open && t.stopRun != nil {
		t.stopRun()
	}
	var ctx context.Context
	if err == nil && open {
		var cancel context.CancelFunc
		ctx, cancel = context.WithCancel(t.ctx)
		t.stopRun = cancel
	}
	enter := enterBytes[t.profile.Enter]
	t.mu.Unlock()
	switch {
	case err != nil:
		t.sendJSON(map[string]string{"type": "script", "state": "error", "name": name, "message": err.Error()})
		return
	case !open:
		t.sendJSON(map[string]string{"type": "script", "state": "error", "name": name, "message": "the port is closed"})
		return
	}
	t.sendJSON(map[string]string{"type": "script", "state": "running", "name": name})
	go func() {
		failure := t.scriptRounds(ctx, script, enter) // what stopped the script, besides a Stop
		switch {
		case failure != nil:
			t.sendJSON(map[string]string{"type": "script", "state": "error", "name": name, "message": failure.Error()})
		case ctx.Err() != nil:
			t.sendJSON(map[string]string{"type": "script", "state": "stopped", "name": name})
		default:
			t.sendJSON(map[string]string{"type": "script", "state": "done", "name": name})
		}
	}()
}

// scriptRounds sends the commands of the script, pausing after each, once or as many rounds as it asks for.
// It returns what went wrong (an expect that did not come), or nil when it ended or was stopped.
func (t *termSession) scriptRounds(ctx context.Context, script Script, enter string) error {
	pause := func() bool {
		select {
		case <-ctx.Done():
			return false
		case <-time.After(time.Duration(script.DelayMs) * time.Millisecond):
			return true
		}
	}
	for round := 1; ; round++ {
		for i, command := range script.Commands {
			if ctx.Err() != nil {
				return nil
			}
			if err := t.runCommand(ctx, command, enter); err != nil {
				return err
			}
			last := i == len(script.Commands)-1 && (!script.Loop || (script.Count > 0 && round >= script.Count))
			if last || !pause() {
				return nil
			}
		}
	}
}

// runCommand does one line of a script: steps, or text sent with the line end.
func (t *termSession) runCommand(ctx context.Context, command, enter string) error {
	if steps, isSteps, _ := scriptSteps(command); isSteps {
		if err := t.runSteps(ctx, steps); err != nil && ctx.Err() == nil {
			return err
		}
		return nil
	}
	command = scriptText(command)
	t.expectClear() // an expect after this line sees the answer to it
	t.write([]byte(command + enter))
	t.sendJSON(map[string]string{"type": "tx", "data": command})
	return nil
}

// runCmd does the steps typed in the Cmd input ("dtr=1; wait=500; dtr=0") in the background.
func (t *termSession) runCmd(text string) {
	steps, err := parseSteps(text)
	if err != nil {
		t.sendJSON(map[string]string{"type": "notice", "text": err.Error()})
		return
	}
	t.mu.Lock()
	ctx := t.runCtx
	t.mu.Unlock()
	if ctx == nil {
		t.sendJSON(map[string]string{"type": "notice", "text": "open the port first"})
		return
	}
	go func() { t.reportSteps(ctx, t.runSteps(ctx, steps)) }()
}

func (t *termSession) stopScript() {
	t.mu.Lock()
	stop := t.stopRun
	t.mu.Unlock()
	if stop != nil {
		stop()
	}
	t.flushOut() // what the script left waiting in a slow send is not sent
}

// looping notes a reply and says whether the triggers are in a loop: a reply that makes the device send
// what triggers it again (an echo, say) would go on for ever. After too many replies in a second the
// triggers are paused until the port is opened again.
func (t *termSession) looping() bool {
	t.trigMu.Lock()
	defer t.trigMu.Unlock()
	if t.triggersPaused {
		return true
	}
	now := time.Now()
	recent := t.replyTimes[:0]
	for _, at := range t.replyTimes {
		if now.Sub(at) < time.Second {
			recent = append(recent, at)
		}
	}
	t.replyTimes = append(recent, now)
	if len(t.replyTimes) <= maxRepliesPerSecond {
		return false
	}
	t.triggersPaused = true
	t.sendJSON(map[string]string{"type": "notice", "text": "triggers paused: more than 20 replies in a second, a loop? Open the port again to resume."})
	return true
}

// reportSteps tells the page why steps typed in Cmd or run by a trigger stopped, unless the port closed.
func (t *termSession) reportSteps(ctx context.Context, err error) {
	if err != nil && ctx.Err() == nil {
		t.sendJSON(map[string]string{"type": "notice", "text": err.Error()})
	}
}

const defaultExpectTimeout = 10 * time.Second

// expect waits until the data received since the last line a script sent (or since the port opened) has the
// pattern in it; what came up to the match is used up.
func (t *termSession) expect(ctx context.Context, re *regexp.Regexp, timeout time.Duration) error {
	deadline := time.NewTimer(timeout)
	defer deadline.Stop()
	for {
		t.expMu.Lock()
		if loc := re.FindIndex(t.expBuf); loc != nil {
			t.expBuf = t.expBuf[loc[1]:]
			t.expMu.Unlock()
			return nil
		}
		t.expMu.Unlock()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-deadline.C:
			return errors.New("it did not come")
		case <-time.After(15 * time.Millisecond):
		}
	}
}

// expectFeed keeps the end of what the device sent, for expect.
func (t *termSession) expectFeed(text []byte) {
	t.expMu.Lock()
	t.expBuf = append(t.expBuf, text...)
	if len(t.expBuf) > 16384 {
		t.expBuf = t.expBuf[len(t.expBuf)-16384:]
	}
	t.expMu.Unlock()
}

func (t *termSession) expectClear() {
	t.expMu.Lock()
	t.expBuf = nil
	t.expMu.Unlock()
}
