package main

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// Steps: everything termdeck sends is a list of steps. The send bar, quick sends, script lines, Cmd and
// trigger replies all become steps, and run the same way (automation.go).
//
//	text=hello\r   text, typed with \r \n \t \0 \\ \xNN (a ; is written \x3B), in the device's character set
//	hex=0D 0A      bytes; dec=13 10 and bin=00001101 too
//	end=crlf       a line end: cr, lf, crlf, enter (the session's Enter key), none, or text like \x03
//	check=crc8     the check (see checks.go) of the bytes sent since the start or the last check
//	dtr=1 rts=0    control lines; break=250 a break of so many milliseconds
//	wait=500       a pause; expect=login: waits for what the device says, for as long as timeout=3000 says
//	baud=9600      another speed for the open port
//	file=fw.bin    the content of a file on this computer
//	log=on         start or stop the log; mark=flashing a mark in the output and the log
//
// Byte steps that follow each other go out as one write, shown as one sent line.

type step struct {
	kind    string  // see stepParsers
	on      bool    // dtr, rts, log
	ms      int     // wait, timeout, break
	n       int     // baud
	bytes   []byte  // text (before the character set), hex, dec, bin, end
	value   string  // check, file, mark; end=enter
	pattern Pattern // expect
}

const maxWaitMs = 3_600_000

// stepParsers read the value of each kind of step.
var stepParsers = map[string]func(value string) (step, error){
	"dtr":  func(v string) (step, error) { return onOff("dtr", v, "0", "1") },
	"rts":  func(v string) (step, error) { return onOff("rts", v, "0", "1") },
	"log":  func(v string) (step, error) { return onOff("log", v, "off", "on") },
	"wait": func(v string) (step, error) { return millis("wait", v, 0, maxWaitMs) },
	"timeout": func(v string) (step, error) {
		return millis("timeout", v, 1, maxWaitMs)
	},
	"break": func(v string) (step, error) { return millis("break", v, 1, 10_000) },
	"text":  func(v string) (step, error) { return step{kind: "text", bytes: unescapeText(v)}, nil },
	"hex": func(v string) (step, error) {
		data, err := parseHex(v)
		return step{kind: "hex", bytes: data}, err
	},
	"dec": func(v string) (step, error) {
		data, err := parseNumberWords(v, 10)
		return step{kind: "dec", bytes: data}, err
	},
	"bin": func(v string) (step, error) {
		data, err := parseNumberWords(v, 2)
		return step{kind: "bin", bytes: data}, err
	},
	"end": func(v string) (step, error) {
		switch strings.ToLower(v) {
		case "enter":
			return step{kind: "end", value: "enter"}, nil
		case "none", "":
			return step{kind: "end"}, nil
		}
		if b, ok := lineEnds[strings.ToLower(v)]; ok {
			return step{kind: "end", bytes: []byte(b)}, nil
		}
		return step{kind: "end", bytes: unescapeText(v)}, nil
	},
	"check": func(v string) (step, error) {
		if _, ok := findCheck(v); !ok {
			return step{}, fmt.Errorf("check is one of %s", strings.Join(checkNames()[1:], ", "))
		}
		return step{kind: "check", value: v}, nil
	},
	"expect": func(v string) (step, error) {
		if v == "" {
			return step{}, fmt.Errorf("expect needs the text to wait for")
		}
		p, err := ParsePattern(v)
		return step{kind: "expect", pattern: p}, err
	},
	"baud": func(v string) (step, error) {
		n, err := strconv.Atoi(v)
		if err != nil || n < 50 || n > 10_000_000 {
			return step{}, fmt.Errorf("baud is a speed from 50 to 10000000")
		}
		return step{kind: "baud", n: n}, nil
	},
	"file": func(v string) (step, error) {
		if v == "" {
			return step{}, fmt.Errorf("file needs the path of the file to send")
		}
		return step{kind: "file", value: v}, nil
	},
	"mark": func(v string) (step, error) { return step{kind: "mark", value: v}, nil },
}

var lineEnds = map[string]string{"cr": "\r", "lf": "\n", "crlf": "\r\n"}

func onOff(kind, value, off, on string) (step, error) {
	switch strings.ToLower(value) {
	case on:
		return step{kind: kind, on: true}, nil
	case off:
		return step{kind: kind}, nil
	}
	return step{}, fmt.Errorf("%s is %s or %s", kind, off, on)
}

func millis(kind, value string, min, max int) (step, error) {
	ms, err := strconv.Atoi(value)
	if err != nil || ms < min || ms > max {
		return step{}, fmt.Errorf("%s is a number of milliseconds (%d to %d)", kind, min, max)
	}
	return step{kind: kind, ms: ms}, nil
}

func stepKinds() string {
	return "text, hex, dec, bin, end, check, dtr, rts, wait, expect, timeout, break, baud, file, log, mark"
}

// parseSteps reads steps separated by semicolons: "dtr=1; wait=500; dtr=0; hex=0D 0A; expect=login:".
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
			return nil, fmt.Errorf("%q: unknown step %q (%s)", item, key, stepKinds())
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

var binaryWord = regexp.MustCompile(`^([01]{1,8}|([01]{8})+)$`)

// parseNumberWords reads bytes written as decimal (0 to 255) or binary (up to 8 digits a byte, or several
// bytes of 8 digits written together), separated by spaces or commas.
func parseNumberWords(text string, base int) ([]byte, error) {
	var out []byte
	for _, word := range strings.FieldsFunc(text, func(r rune) bool { return r == ' ' || r == ',' }) {
		if base == 2 {
			if !binaryWord.MatchString(word) {
				return nil, fmt.Errorf("%q: binary bytes are up to 8 digits of 0 and 1", word)
			}
			for len(word) > 8 {
				n, _ := strconv.ParseUint(word[:8], 2, 8)
				out, word = append(out, byte(n)), word[8:]
			}
		}
		n, err := strconv.ParseUint(word, base, 8)
		if err != nil {
			if base == 2 {
				return nil, fmt.Errorf("%q: binary bytes are up to 8 digits of 0 and 1", word)
			}
			return nil, fmt.Errorf("%q: decimal bytes go from 0 to 255", word)
		}
		out = append(out, byte(n))
	}
	return out, nil
}

// payload is what the send bar (or a quick send) sends as steps: data of a format, then its check and its end.
// The format is text, hex, dec, bin or cmd (data is then steps, and has no check or end).
func payload(format, data, check string, end step) ([]step, error) {
	if format == "cmd" {
		return parseSteps(data)
	}
	if !oneOf(format, []string{"text", "hex", "dec", "bin"}) {
		return nil, fmt.Errorf("the type is one of text, hex, dec, bin, cmd")
	}
	if format == "text" {
		data = strings.ReplaceAll(data, ";", `\x3B`) // a payload is not split at semicolons
	}
	s, err := stepParsers[format](data)
	if err != nil {
		return nil, err
	}
	steps := []step{s}
	if check != "" && check != "none" {
		c, err := stepParsers["check"](check)
		if err != nil {
			return nil, err
		}
		steps = append(steps, c)
	}
	return append(steps, end), nil
}

// endStep is the end the send bar chose: none, cr, lf, crlf, enter, or custom with its text.
func endStep(mode, custom string) step {
	if mode == "custom" {
		return step{kind: "end", bytes: unescapeText(custom)}
	}
	s, _ := stepParsers["end"](mode) // a word that is not an end is read as text
	return s
}

// scriptSteps says what a script line does: "@" and steps, or text sent with the session's Enter.
// "@@text" is the text "@text".
func scriptSteps(command string) ([]step, error) {
	if strings.HasPrefix(command, "@") && !strings.HasPrefix(command, "@@") {
		return parseSteps(command[1:])
	}
	return []step{{kind: "text", bytes: []byte(strings.TrimPrefix(command, "@"))}, {kind: "end", value: "enter"}}, nil
}

// trigger sends a reply when the pattern shows up in what the device sends.
type trigger struct {
	pattern Pattern
	steps   []step
}

// parseTriggers reads lines "pattern => reply". The pattern is text (any case), or /regex/. The reply is a
// script line: text (sent with the session's Enter key) or "@" and steps. Empty lines and # notes are skipped.
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
		steps, err := scriptSteps(reply)
		if err != nil {
			return nil, fmt.Errorf("trigger %q: %w", pattern, err)
		}
		out = append(out, trigger{pattern: p, steps: steps})
	}
	return out, nil
}

// quickSend is a button of the send bar: a name and what it sends.
type quickSend struct {
	Name  string
	steps []step
}

var (
	quickTypes  = []string{"text", "hex", "dec", "bin", "cmd"}
	quickOption = regexp.MustCompile(`^\s*(end|check)=(\w+)\s*$`)
)

// parseQuickSends reads lines "name | type | data" with options after the data ("| end=cr", "| check=crc16modbus").
// The type is text, hex, dec, bin or cmd. The end is the Enter key of the session for text and none for the
// others when it is left out. Empty lines and lines starting with # are skipped.
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
		name, kind := strings.TrimSpace(parts[0]), strings.ToLower(strings.TrimSpace(parts[1]))
		end, check := "", ""
		for len(parts) > 3 {
			m := quickOption.FindStringSubmatch(parts[len(parts)-1])
			if m == nil {
				break
			}
			if m[1] == "end" {
				end = m[2]
			} else {
				check = m[2]
			}
			parts = parts[:len(parts)-1]
		}
		data := strings.TrimSpace(strings.Join(parts[2:], "|"))
		if name == "" || len(name) > 40 {
			return nil, fmt.Errorf("quick send %q: the name is 1 to 40 characters", line)
		}
		if !oneOf(kind, quickTypes) {
			return nil, fmt.Errorf("quick send %q: the type is one of %s", name, strings.Join(quickTypes, ", "))
		}
		switch {
		case end == "" && kind == "text", end == "session":
			end = "enter"
		case end == "":
			end = "none"
		case !oneOf(end, []string{"none", "cr", "lf", "crlf"}):
			return nil, fmt.Errorf("quick send %q: the end is one of session, none, cr, lf, crlf", name)
		}
		steps, err := payload(kind, data, check, endStep(end, ""))
		if err != nil {
			return nil, fmt.Errorf("quick send %q: %w", name, err)
		}
		out = append(out, quickSend{Name: name, steps: steps})
	}
	return out, nil
}
