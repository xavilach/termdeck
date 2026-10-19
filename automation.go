package main

import (
	"context"
	"time"
)

const maxRepliesPerSecond = 20

// What a session sends on its own: script lines, steps typed in Cmd and replies to triggers.

// runSteps does the steps in order; it stops early when ctx ends and says whether it did them all.
func (t *termSession) runSteps(ctx context.Context, steps []step) bool {
	for _, s := range steps {
		switch s.kind {
		case "dtr", "rts":
			t.setLine(s.kind, s.on)
		case "hex":
			t.writeRaw(s.bytes)
		case "wait":
			select {
			case <-ctx.Done():
				return false
			case <-time.After(time.Duration(s.ms) * time.Millisecond):
			}
		}
		if ctx.Err() != nil {
			return false
		}
	}
	return true
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
			t.runSteps(ctx, tr.steps)
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
		defer func() {
			state := "done"
			if ctx.Err() != nil {
				state = "stopped"
			}
			t.sendJSON(map[string]string{"type": "script", "state": state, "name": name})
		}()
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
					return
				}
				if steps, isSteps, _ := scriptSteps(command); isSteps {
					if !t.runSteps(ctx, steps) {
						return
					}
				} else {
					command = scriptText(command)
					t.write([]byte(command + enter))
					t.sendJSON(map[string]string{"type": "tx", "data": command})
				}
				last := i == len(script.Commands)-1 && (!script.Loop || (script.Count > 0 && round >= script.Count))
				if last || !pause() {
					return
				}
			}
		}
	}()
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
	go t.runSteps(ctx, steps)
}

func (t *termSession) stopScript() {
	t.mu.Lock()
	stop := t.stopRun
	t.mu.Unlock()
	if stop != nil {
		stop()
	}
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
