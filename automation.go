package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
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

// ---------------------------------------------------------------- rules (see rules.go)

// hasRule says whether a rule waits for this moment.
func hasRule(rules []rule, when string) bool {
	for _, r := range rules {
		if r.when == when {
			return true
		}
	}
	return false
}

func hasRuleNamed(rules []rule, when, name string) bool {
	for _, r := range rules {
		if r.when == when && strings.EqualFold(r.name, name) {
			return true
		}
	}
	return false
}

func (t *termSession) ruleList() []rule {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.rules
}

// fire does what the rules that wait for this moment say (name: the button, or the line).
func (t *termSession) fire(when, name, context string) {
	t.mu.Lock()
	rules, ctx := t.rules, t.runCtx
	t.mu.Unlock()
	if ctx == nil || ctx.Err() != nil {
		ctx = t.ctx // after a disconnect: notes, marks and alerts only
	}
	for _, r := range rules {
		if r.when == when && (name == "" || strings.EqualFold(r.name, name)) {
			t.act(ctx, r, context)
		}
	}
}

// startRules does the "on connect" rules, and starts the "every" rules as jobs of the port.
func (t *termSession) startRules() {
	t.fire("connect", "", "")
	for _, r := range t.ruleList() {
		if r.when != "every" {
			continue
		}
		r := r
		t.startJob("rule", r.text, 0, func(ctx context.Context, j *job) error {
			for round := int64(1); ; round++ {
				if err := sleepCtx(ctx, r.every); err != nil {
					return nil
				}
				t.act(ctx, r, "")
				t.progress(j, round)
			}
		})
	}
}

// pressButton does what the rule of a button says.
func (t *termSession) pressButton(name string) {
	if !hasRuleNamed(t.ruleList(), "button", name) {
		t.notice("no button named " + name)
		return
	}
	t.fire("button", name, "")
}

// matchRules looks for the patterns of the rules in what the device sent, and does what they say as soon
// as the text is there. Each rule has its own window (the end of the data it has not matched yet, to look
// at again with the next chunk), so rules on the same text do not take it from each other.
func (t *termSession) matchRules(ctx context.Context, windows map[int][]byte, text []byte) {
	t.mu.Lock()
	t.lastRx = time.Now()
	clear(t.idleFired)
	t.mu.Unlock()
	for i, r := range t.ruleList() {
		if r.when != "match" || !r.streamRule() {
			continue
		}
		window := append(windows[i], text...)
		for {
			loc := r.pattern.FindIndex(window)
			if loc == nil || loc[1] == 0 {
				break
			}
			match := string(window[loc[0]:loc[1]])
			window = window[loc[1]:]
			t.act(ctx, r, match)
		}
		if len(window) > 4096 {
			window = window[len(window)-4096:]
		}
		windows[i] = window
	}
}

// lineRules: a complete line that matches a rule with notify or sound tells the page.
func (t *termSession) lineRules(rules []rule, line string) {
	for _, r := range rules {
		if r.when == "match" && (r.notify || r.sound) && r.pattern.MatchString(line) {
			t.alert(r, line)
		}
	}
}

// alert asks the page for a desktop notification or a sound.
func (t *termSession) alert(r rule, text string) {
	if text == "" {
		text = r.text
	}
	if r.notify {
		t.sendJSON(map[string]string{"type": "notify", "text": text})
	}
	if r.sound {
		t.sendJSON(map[string]string{"type": "sound"})
	}
}

// frameRules gives the received bytes to the frames, for the rules on bad frames.
func (t *termSession) frameRules(raw []byte) {
	t.mu.Lock()
	framer := t.framer
	t.mu.Unlock()
	if framer == nil {
		return
	}
	for _, row := range framer.feed(raw) {
		if row.checked {
			t.countFrame(row.ok)
		}
		if row.checked && !row.ok {
			t.fire("frame", "", "bad frame "+frameLine(framer.spec, row))
		}
	}
}

// idleRules fire the "idle" rules whose time without data has come, once each until data comes again.
func (t *termSession) idleRules(now time.Time) {
	t.mu.Lock()
	ctx := t.runCtx
	var due []rule
	if t.port != nil && ctx != nil {
		for i, r := range t.rules {
			if r.when == "idle" && now.Sub(t.lastRx) >= r.every && !t.idleFired[i] {
				t.idleFired[i] = true
				due = append(due, r)
			}
		}
	}
	t.mu.Unlock()
	for _, r := range due {
		t.act(ctx, r, fmt.Sprintf("nothing received for %v", r.every))
	}
}

// act does what a rule says, in the background, in order with the others. Too many waiting are dropped,
// and rules that send in a loop (each send making the device send what fires it again) are paused.
func (t *termSession) act(ctx context.Context, r rule, context string) {
	if r.when != "match" {
		t.alert(r, context) // a line that matches alerts by itself, with the whole line (lineRules)
	}
	if r.count != "" {
		t.countUp(r.count)
	}
	if r.lap {
		t.lap(context)
	}
	if r.cmd != "" {
		t.sendJSON(map[string]string{"type": "command", "id": r.cmd})
	}
	if r.act == "" && r.log == "" {
		return
	}
	if (r.act == "send" || r.act == "run") && t.looping() {
		return
	}
	if t.pending.Add(1) > maxPendingReplies {
		t.pending.Add(-1)
		t.notice("rules are backed up: dropped " + r.text)
		return
	}
	go func() {
		defer t.pending.Add(-1)
		t.replyMu.Lock()
		defer t.replyMu.Unlock()
		if ctx.Err() != nil {
			return
		}
		if r.log != "" {
			t.setLogging(r.log == "on")
		}
		switch r.act {
		case "send":
			t.note("rule: " + r.text)
			t.reportSteps(ctx, t.runSteps(ctx, r.sent, stepRun{}))
		case "run":
			t.runScript(r.arg)
		case "mark":
			t.markWith(strings.TrimSpace(r.arg + " " + context))
		case "note":
			t.note(strings.TrimSpace(r.arg + " " + context))
		}
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

// looping notes a send of a rule and says whether the rules are in a loop: a reply that makes the device
// send what fires it again (an echo, say) would go on for ever. After too many sends in a second the
// rules are paused until the port is opened again.
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
	t.notice("rules paused: more than 20 sends in a second, a loop? Open the port again to resume.")
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
