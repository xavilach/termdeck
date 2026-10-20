package main

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// step is one action of a script line starting with "@", of a Cmd input or of a trigger reply.
type step struct {
	kind    string  // dtr, rts, wait, hex, expect or timeout
	on      bool    // dtr and rts
	ms      int     // wait and timeout
	bytes   []byte  // hex
	pattern Pattern // expect
}

const maxWaitMs = 3_600_000

// stepParsers read the value of each kind of step.
var stepParsers = map[string]func(value string) (step, error){
	"dtr": func(v string) (step, error) { return lineStep("dtr", v) },
	"rts": func(v string) (step, error) { return lineStep("rts", v) },
	"wait": func(v string) (step, error) {
		ms, err := strconv.Atoi(v)
		if err != nil || ms < 0 || ms > maxWaitMs {
			return step{}, fmt.Errorf("wait is a number of milliseconds (0 to %d)", maxWaitMs)
		}
		return step{kind: "wait", ms: ms}, nil
	},
	"timeout": func(v string) (step, error) {
		ms, err := strconv.Atoi(v)
		if err != nil || ms < 1 || ms > maxWaitMs {
			return step{}, fmt.Errorf("timeout is a number of milliseconds (1 to %d)", maxWaitMs)
		}
		return step{kind: "timeout", ms: ms}, nil
	},
	"hex": func(v string) (step, error) {
		data, err := parseHex(v)
		return step{kind: "hex", bytes: data}, err
	},
	"expect": func(v string) (step, error) {
		if v == "" {
			return step{}, fmt.Errorf("expect needs the text to wait for")
		}
		p, err := ParsePattern(v)
		return step{kind: "expect", pattern: p}, err
	},
}

func lineStep(kind, value string) (step, error) {
	if value != "0" && value != "1" {
		return step{}, fmt.Errorf("%s is 0 or 1", kind)
	}
	return step{kind: kind, on: value == "1"}, nil
}

// parseSteps reads "dtr=1; wait=500; dtr=0; hex=0D 0A; timeout=3000; expect=login:". Items are separated
// by semicolons. expect waits until the device has sent the text (or /regex/), for as long as the last
// timeout says (10 seconds at first), and fails the script when it does not come.
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
		parse := stepParsers[key]
		if parse == nil {
			return nil, fmt.Errorf("%q: unknown step %q (dtr, rts, wait, hex, timeout, expect)", item, key)
		}
		s, err := parse(value)
		if err != nil {
			return nil, fmt.Errorf("%q: %w", item, err)
		}
		steps = append(steps, s)
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
	pattern Pattern
	reply   string
	steps   []step // set when the reply is "@..." steps
}

// parseTriggers reads lines "pattern => reply". The pattern is text (any case), or /regex/. The reply is text
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
		p, err := ParsePattern(pattern)
		if err != nil {
			return nil, fmt.Errorf("trigger: %w", err)
		}
		t := trigger{pattern: p, reply: reply}
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

// quickSend is a button of the send bar: a name and what it sends.
type quickSend struct {
	Name, Type, Data, End, Check string
}

var (
	quickTypes  = []string{"text", "hex", "dec", "bin", "cmd"}
	quickEnds   = []string{"session", "none", "cr", "lf", "crlf"}
	quickChecks = []string{"none", "sum8", "xor8", "crc8", "crc16modbus", "crc16ccitt", "crc16xmodem", "crc32"}
	quickOption = regexp.MustCompile(`^\s*(end|check)=(\w+)\s*$`)
)

// parseQuickSends reads lines "name | type | data" with options after the data ("| end=cr", "| check=crc16modbus"). The type is text,
// hex, dec, bin or cmd. The end (session, none, cr, lf, crlf) is the Enter key of the session for text
// and none for the others when it is left out. Empty lines and lines starting with # are skipped.
func parseQuickSends(text string) ([]quickSend, error) {
	var out []quickSend
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.Split(line, "|")
		if len(parts) < 3 {
			return nil, fmt.Errorf("quick send %q: use  name | type | data", line)
		}
		q := quickSend{Name: strings.TrimSpace(parts[0]), Type: strings.ToLower(strings.TrimSpace(parts[1]))}
		for len(parts) > 3 {
			m := quickOption.FindStringSubmatch(parts[len(parts)-1])
			if m == nil {
				break
			}
			if m[1] == "end" {
				q.End = m[2]
			} else {
				q.Check = m[2]
			}
			parts = parts[:len(parts)-1]
		}
		q.Data = strings.TrimSpace(strings.Join(parts[2:], "|"))
		if q.Name == "" || len(q.Name) > 40 {
			return nil, fmt.Errorf("quick send %q: the name is 1 to 40 characters", line)
		}
		if !oneOf(q.Type, quickTypes) {
			return nil, fmt.Errorf("quick send %q: the type is one of %s", q.Name, strings.Join(quickTypes, ", "))
		}
		if q.End != "" && !oneOf(q.End, quickEnds) {
			return nil, fmt.Errorf("quick send %q: the end is one of %s", q.Name, strings.Join(quickEnds, ", "))
		}
		if q.Check != "" && !oneOf(q.Check, quickChecks) {
			return nil, fmt.Errorf("quick send %q: the check is one of %s", q.Name, strings.Join(quickChecks, ", "))
		}
		if err := checkQuickData(q); err != nil {
			return nil, fmt.Errorf("quick send %q: %w", q.Name, err)
		}
		out = append(out, q)
	}
	return out, nil
}

func checkQuickData(q quickSend) error {
	switch q.Type {
	case "hex":
		_, err := parseHex(q.Data)
		return err
	case "dec":
		for _, word := range strings.FieldsFunc(q.Data, func(r rune) bool { return r == ' ' || r == ',' }) {
			if n, err := strconv.Atoi(word); err != nil || n < 0 || n > 255 {
				return fmt.Errorf("%q: decimal bytes go from 0 to 255", word)
			}
		}
	case "bin":
		for _, word := range strings.FieldsFunc(q.Data, func(r rune) bool { return r == ' ' || r == ',' }) {
			if !regexp.MustCompile(`^([01]{1,8}|([01]{8})+)$`).MatchString(word) {
				return fmt.Errorf("%q: binary bytes are up to 8 digits of 0 and 1", word)
			}
		}
	case "cmd":
		_, err := parseSteps(q.Data)
		return err
	}
	return nil
}
