package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

const maxRepliesPerSecond = 20

// What a session sends: the send bar, quick sends, script lines, Cmd and trigger replies, all as steps.

// stepRun is how steps run: in a script, an expect looks at the answer to what was sent just before it.
type stepRun struct {
	script bool
}

// runSteps does the steps in order. Byte steps that follow each other go out as one write. It returns nil
// when all were done, the error of the context when it ended, or what went wrong (an expect that did not
// come in time, a file that cannot be read).
func (t *termSession) runSteps(ctx context.Context, steps []step, how stepRun) error {
	t.mu.Lock()
	enter, codec := []byte(enterBytes[t.profile.Enter]), t.codec
	t.mu.Unlock()
	timeout := defaultExpectTimeout
	var out, typed []byte // what goes out together, and how it was typed when it is text only
	text, checkFrom := true, 0
	flush := func() {
		if len(out) == 0 {
			return
		}
		if how.script {
			t.expectClear() // an expect after this sees the answer to it
		}
		shown := sentText(typed)
		if !text {
			shown = []byte(hexText(out))
		}
		t.sendData(out, shown)
		out, typed, text, checkFrom = nil, nil, true, 0
	}
	for _, s := range steps {
		switch s.kind {
		case "text":
			out, typed = append(out, codec.ToDevice(s.bytes)...), append(typed, s.bytes...)
		case "hex", "dec", "bin":
			out, text = append(out, s.bytes...), false
		case "end":
			end := s.bytes
			if s.value == "enter" {
				end = enter
			}
			out, typed = append(out, end...), append(typed, end...)
		case "check":
			out, text = append(out, checksum(s.value, out[checkFrom:])...), false
			checkFrom = len(out)
		default:
			flush()
			if err := t.runStep(ctx, s, &timeout); err != nil {
				return err
			}
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
	}
	flush()
	return nil
}

// runStep does a step that is not bytes to send.
func (t *termSession) runStep(ctx context.Context, s step, timeout *time.Duration) error {
	switch s.kind {
	case "dtr", "rts":
		t.setLine(s.kind, s.on)
	case "timeout":
		*timeout = time.Duration(s.ms) * time.Millisecond
	case "expect":
		if err := t.expect(ctx, s.pattern, *timeout); err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			return fmt.Errorf("waited %v for %q: %w", *timeout, s.pattern.Text, err)
		}
	case "wait":
		return sleepCtx(ctx, time.Duration(s.ms)*time.Millisecond)
	case "break":
		t.sendBreak(time.Duration(s.ms) * time.Millisecond)
	case "baud":
		return t.setBaud(s.n)
	case "file":
		return t.sendFile(ctx, s.value)
	case "log":
		t.setLogging(s.on)
	case "mark":
		t.markWith(s.value)
	}
	return nil
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-time.After(d):
		return nil
	}
}

// maxFileStep is the largest file a file step sends.
const maxFileStep = 64 << 20

// sendFile sends the content of a file on this computer, as it is, a piece at a time.
func (t *termSession) sendFile(ctx context.Context, path string) error {
	info, err := os.Stat(path)
	if err == nil && info.Size() > maxFileStep {
		err = fmt.Errorf("it is bigger than %d MB", maxFileStep>>20)
	}
	var data []byte
	if err == nil {
		data, err = os.ReadFile(path)
	}
	if err != nil {
		return fmt.Errorf("file %s: %w", path, err)
	}
	sent := 0
	t.sendChunks(ctx, data, func(done int) { sent = done })
	t.note(fmt.Sprintf("file %s: %d of %d bytes sent", filepath.Base(path), sent, len(data)))
	return nil
}

// blocks says whether steps take time (and run in the background) or only send.
func blocks(steps []step) bool {
	for _, s := range steps {
		if !oneOf(s.kind, []string{"text", "hex", "dec", "bin", "end", "check", "dtr", "rts", "mark", "log"}) {
			return true
		}
	}
	return false
}

// runPayload sends what the send bar or a quick send asked for: at once, in order with what was sent before,
// or in the background when it waits.
func (t *termSession) runPayload(steps []step) {
	t.mu.Lock()
	ctx := t.runCtx
	t.mu.Unlock()
	if ctx == nil {
		t.notice("open the port first")
		return
	}
	if !blocks(steps) {
		t.reportSteps(ctx, t.runSteps(ctx, steps, stepRun{}))
		return
	}
	go func() { t.reportSteps(ctx, t.runSteps(ctx, steps, stepRun{})) }()
}

// sendBar sends what the send bar holds: data of a format, its check and its end.
func (t *termSession) sendBar(m clientMessage) {
	steps, err := payload(m.Format, m.Data, m.Check, endStep(m.End, m.EndCustom))
	if err != nil {
		t.notice(err.Error())
		return
	}
	t.runPayload(steps)
}

// sendQuick sends the quick send of the session with that name.
func (t *termSession) sendQuick(name string) {
	t.mu.Lock()
	quick, _ := parseQuickSends(t.profile.QuickSends) // checked by Normalize
	t.mu.Unlock()
	for _, q := range quick {
		if q.Name == name {
			t.runPayload(q.steps)
			return
		}
	}
	t.notice("no quick send named " + name)
}

// match looks for the triggers in what the device sent and answers them. It returns the end of the data, to
// look at again with the next chunk.
func (t *termSession) match(ctx context.Context, triggers []trigger, window, text []byte) []byte {
	if len(triggers) == 0 {
		return nil
	}
	window = append(window, text...)
	for fired := true; fired; {
		fired = false
		for _, tr := range triggers {
			if loc := tr.pattern.FindIndex(window); loc != nil {
				window = window[loc[1]:]
				fired = true
				t.answer(ctx, tr)
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
func (t *termSession) answer(ctx context.Context, tr trigger) {
	if t.looping() {
		return
	}
	if t.pending.Add(1) > maxPendingReplies {
		t.pending.Add(-1)
		t.notice("trigger replies are backed up: dropped " + tr.pattern.Text)
		return
	}
	go func() {
		defer t.pending.Add(-1)
		t.replyMu.Lock()
		defer t.replyMu.Unlock()
		if ctx.Err() != nil {
			return
		}
		t.note("trigger: " + tr.pattern.Text)
		t.reportSteps(ctx, t.runSteps(ctx, tr.steps, stepRun{}))
	}()
}

// runScript sends the commands of the named script, pausing after each, as a job.
func (t *termSession) runScript(name string) {
	script, err := t.srv.scripts.Get(name)
	if err == nil {
		_, err = t.startJob("script", name, 0, func(ctx context.Context, j *job) error { return t.scriptRounds(ctx, j, script) })
	}
	if err != nil {
		t.note("script " + name + ": " + err.Error())
	}
}

// scriptRounds sends the commands of the script, pausing after each, once or as many rounds as it asks for.
// It returns what went wrong (an expect that did not come), or nil when it ended or was stopped.
func (t *termSession) scriptRounds(ctx context.Context, j *job, script Script) error {
	pause := func() bool { return sleepCtx(ctx, time.Duration(script.DelayMs)*time.Millisecond) == nil }
	for round := 1; ; round++ {
		for i, command := range script.Commands {
			if ctx.Err() != nil {
				return nil
			}
			steps, _ := scriptSteps(command) // checked when the script was saved
			if err := t.runSteps(ctx, steps, stepRun{script: true}); err != nil && ctx.Err() == nil {
				return err
			}
			last := i == len(script.Commands)-1 && (!script.Loop || (script.Count > 0 && round >= script.Count))
			if last || !pause() {
				return nil
			}
		}
		t.progress(j, int64(round))
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
	t.notice("triggers paused: more than 20 replies in a second, a loop? Open the port again to resume.")
	return true
}

// reportSteps tells the page why steps typed in Cmd or run by a trigger stopped, unless the port closed.
func (t *termSession) reportSteps(ctx context.Context, err error) {
	if err != nil && ctx.Err() == nil {
		t.notice(err.Error())
	}
}

const defaultExpectTimeout = 10 * time.Second

// expect waits until the data received since the last line a script sent (or since the port opened) has the
// pattern in it; what came up to the match is used up.
func (t *termSession) expect(ctx context.Context, re Pattern, timeout time.Duration) error {
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
