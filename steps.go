package main

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// step is one action of a script line starting with "@", of a Cmd input or of a trigger reply.
type step struct {
	kind  string // dtr, rts, wait or hex
	on    bool   // dtr and rts
	ms    int    // wait
	bytes []byte // hex
}

const maxWaitMs = 3_600_000

// parseSteps reads "dtr=1; wait=500; dtr=0; hex=0D 0A". Items are separated by semicolons.
func parseSteps(text string) ([]step, error) {
	var steps []step
	for _, item := range strings.Split(text, ";") {
		item = strings.TrimSpace(item)
		if item == "" {
			continue
		}
		key, value, ok := strings.Cut(item, "=")
		key, value = strings.ToLower(strings.TrimSpace(key)), strings.TrimSpace(value)
		if !ok {
			return nil, fmt.Errorf("%q: use name=value, for example dtr=1", item)
		}
		switch key {
		case "dtr", "rts":
			if value != "0" && value != "1" {
				return nil, fmt.Errorf("%q: %s is 0 or 1", item, key)
			}
			steps = append(steps, step{kind: key, on: value == "1"})
		case "wait":
			ms, err := strconv.Atoi(value)
			if err != nil || ms < 0 || ms > maxWaitMs {
				return nil, fmt.Errorf("%q: wait is a number of milliseconds (0 to %d)", item, maxWaitMs)
			}
			steps = append(steps, step{kind: key, ms: ms})
		case "hex":
			data, err := parseHex(value)
			if err != nil {
				return nil, fmt.Errorf("%q: %w", item, err)
			}
			steps = append(steps, step{kind: key, bytes: data})
		default:
			return nil, fmt.Errorf("%q: unknown step %q (dtr, rts, wait, hex)", item, key)
		}
	}
	if len(steps) == 0 {
		return nil, fmt.Errorf("nothing to do")
	}
	return steps, nil
}

// parseHex reads bytes written as hex, with or without spaces, commas and 0x prefixes.
func parseHex(text string) ([]byte, error) {
	text = strings.NewReplacer(",", " ", "0x", " ", "0X", " ").Replace(text)
	digits := strings.Join(strings.Fields(text), "")
	if len(digits)%2 != 0 {
		return nil, fmt.Errorf("hex needs two digits per byte")
	}
	out := make([]byte, 0, len(digits)/2)
	for i := 0; i < len(digits); i += 2 {
		b, err := strconv.ParseUint(digits[i:i+2], 16, 8)
		if err != nil {
			return nil, fmt.Errorf("%q is not hex", digits[i:i+2])
		}
		out = append(out, byte(b))
	}
	return out, nil
}

// scriptSteps says whether a script command is a list of steps, and which.
// "@@text" is the text "@text".
func scriptSteps(command string) (steps []step, isSteps bool, err error) {
	if !strings.HasPrefix(command, "@") || strings.HasPrefix(command, "@@") {
		return nil, false, nil
	}
	steps, err = parseSteps(command[1:])
	return steps, true, err
}

// scriptText is the text a plain script command sends.
func scriptText(command string) string { return strings.TrimPrefix(command, "@") }

// trigger sends a reply when the pattern shows up in what the device sends.
type trigger struct {
	label string // the pattern as written
	re    *regexp.Regexp
	reply string
	steps []step // set when the reply is "@..." steps
}

// parseTriggers reads lines "pattern => reply". The pattern is text, or /regex/. The reply is text
// (the session's Enter key is added) or "@" and steps. Empty lines and lines starting with # are skipped.
func parseTriggers(text string) ([]trigger, error) {
	var out []trigger
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		pattern, reply, ok := strings.Cut(line, "=>")
		pattern, reply = strings.TrimSpace(pattern), strings.TrimSpace(reply)
		if !ok || pattern == "" {
			return nil, fmt.Errorf("trigger %q: use  text => reply", line)
		}
		expr := regexp.QuoteMeta(pattern)
		if len(pattern) > 2 && strings.HasPrefix(pattern, "/") && strings.HasSuffix(pattern, "/") {
			expr = pattern[1 : len(pattern)-1]
		}
		re, err := regexp.Compile(expr)
		if err != nil {
			return nil, fmt.Errorf("trigger %q: %w", pattern, err)
		}
		t := trigger{label: pattern, re: re, reply: reply}
		if steps, isSteps, err := scriptSteps(reply); err != nil {
			return nil, fmt.Errorf("trigger %q: %w", pattern, err)
		} else if isSteps {
			t.steps = steps
		} else {
			t.reply = scriptText(reply)
		}
		out = append(out, t)
	}
	return out, nil
}
