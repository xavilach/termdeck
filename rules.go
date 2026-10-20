package main

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Rules: what a session does by itself, one rule per line, "when => do".
//
//	when: a pattern (text, any case, or /regex/) in what the device sends · on connect · on disconnect ·
//	      every 500ms (or 2s, 1m) · button Name (a button above the send bar) · key Alt+1 · idle 5s (nothing received for
//	      so long) · on CTS (or DSR, DCD, RI: the line changed) · bad frame (a frame whose check fails)
//	do:   colours and styles of the line (red, green, yellow, blue, magenta, cyan, white, grey, orange, bold,
//	      dim, underline, inverse) · hide (the line is not shown) · notify (a desktop notification) · sound ·
//	      graph Name (the number after the match, or the first group of a /regex/) · log on|off ·
//	      then one of: send <script line> · run <script> · mark [words] · note [words]
//
// A pattern's colours, hide and graph apply to the whole line; its other actions happen as soon as the
// text arrives, without waiting for the end of the line (a prompt has none).

type rule struct {
	text string // as written, for notes and errors

	when    string // match, connect, disconnect, every, button, idle, line, frame
	pattern Pattern
	every   time.Duration // every, idle
	name    string        // button: its name; line: cts, dsr, dcd or ri

	sgr    string // the colour of the line
	hide   bool
	notify bool
	sound  bool
	graph  string
	log    string // on, off or ""

	act  string // send, run, mark, note or ""
	arg  string // what follows act
	sent []step // send: the steps
}

var ruleStyles = map[string]string{"red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36",
	"white": "37", "grey": "90", "orange": "38;5;208", "bold": "1", "dim": "2", "underline": "4", "inverse": "7"}

var durationText = regexp.MustCompile(`^(\d+)\s*(ms|s|m)?$`)

func parseDuration(text string) (time.Duration, error) {
	m := durationText.FindStringSubmatch(strings.TrimSpace(strings.ToLower(text)))
	if m == nil {
		return 0, fmt.Errorf("%q is not a time: write 500ms, 2s or 1m", text)
	}
	n, _ := strconv.Atoi(m[1])
	unit := map[string]time.Duration{"": time.Millisecond, "ms": time.Millisecond, "s": time.Second, "m": time.Minute}[m[2]]
	d := time.Duration(n) * unit
	if d < 10*time.Millisecond || d > 24*time.Hour {
		return 0, fmt.Errorf("%s: from 10ms to 24 hours", text)
	}
	return d, nil
}

// parseWhen reads the left side of a rule.
func (r *rule) parseWhen(when string) error {
	lower := strings.ToLower(when)
	word, rest, _ := strings.Cut(lower, " ")
	rest = strings.TrimSpace(rest)
	var err error
	switch {
	case lower == "on connect" || lower == "on disconnect":
		r.when = rest
	case lower == "bad frame":
		r.when = "frame"
	case word == "on" && oneOf(rest, []string{"cts", "dsr", "dcd", "ri"}):
		r.when, r.name = "line", rest
	case word == "on":
		err = fmt.Errorf("on is followed by connect, disconnect, CTS, DSR, DCD or RI (write /on %s/ for the text)", rest)
	case word == "every" || word == "idle":
		r.when = word
		r.every, err = parseDuration(rest)
	case word == "key":
		r.when, r.name = "key", normalizeKey(strings.TrimSpace(when[len("key"):]))
		if r.name == "" {
			err = fmt.Errorf("key is followed by a key such as Alt+1 or Ctrl+Shift+F5, with Alt or Ctrl")
		}
	case word == "button":
		r.when, r.name = "button", strings.TrimSpace(when[len("button"):])
		if r.name == "" || len(r.name) > 40 {
			err = fmt.Errorf("a button has a name of 1 to 40 characters")
		}
	default:
		r.when = "match"
		r.pattern, err = ParsePattern(when)
	}
	return err
}

// parseDo reads the right side of a rule: words, then at most one action that takes the rest of the line.
func (r *rule) parseDo(do string) error {
	rest := strings.TrimSpace(do)
	for rest != "" {
		word, after, _ := strings.Cut(rest, " ")
		after = strings.TrimSpace(after)
		lower := strings.ToLower(word)
		switch {
		case ruleStyles[lower] != "":
			r.sgr = strings.Trim(r.sgr+";"+ruleStyles[lower], ";")
		case lower == "hide":
			r.hide = true
		case lower == "notify":
			r.notify = true
		case lower == "sound":
			r.sound = true
		case lower == "graph" || lower == "log":
			value, more, _ := strings.Cut(after, " ")
			if lower == "graph" {
				if value == "" || len(value) > 30 {
					return fmt.Errorf("graph needs the name of the series (up to 30 characters)")
				}
				r.graph = value
			} else {
				if value = strings.ToLower(value); value != "on" && value != "off" {
					return fmt.Errorf("log is on or off")
				}
				r.log = value
			}
			rest = strings.TrimSpace(more)
			continue
		case oneOf(lower, []string{"send", "run", "mark", "note"}):
			r.act, r.arg = lower, after
			switch {
			case lower == "send":
				steps, err := scriptSteps(after)
				if err != nil {
					return err
				}
				r.sent = steps
			case lower == "run" && after == "":
				return fmt.Errorf("run needs the name of a script")
			}
			return nil
		default:
			return fmt.Errorf("%q: do is colours, hide, notify, sound, graph Name, log on|off, then send, run, mark or note", word)
		}
		rest = after
	}
	return nil
}

// parseRules reads the rules of a session. Empty lines and lines starting with # are notes.
func parseRules(text string) ([]rule, error) {
	var out []rule
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		when, do, ok := strings.Cut(line, "=>")
		when, do = strings.TrimSpace(when), strings.TrimSpace(do)
		if !ok || when == "" || do == "" {
			return nil, fmt.Errorf("rule %q: write  when => do", line)
		}
		r := rule{text: line}
		if err := r.parseWhen(when); err != nil {
			return nil, fmt.Errorf("rule %q: %w", line, err)
		}
		if err := r.parseDo(do); err != nil {
			return nil, fmt.Errorf("rule %q: %w", line, err)
		}
		if r.when != "match" && (r.sgr != "" || r.hide || r.graph != "") {
			return nil, fmt.Errorf("rule %q: colours, hide and graph are for lines that match a pattern", line)
		}
		out = append(out, r)
	}
	return out, nil
}

func checkRules(text string) error {
	_, err := parseRules(text)
	return err
}

// lineRule: the rule acts on whole lines (their colour, whether they show, the numbers in them).
func (r rule) lineRule() bool { return r.when == "match" && (r.sgr != "" || r.hide || r.graph != "") }

// eventRule: the rule does something when its moment comes (a match in the stream, a connect...).
func (r rule) eventRule() bool { return r.act != "" || r.log != "" || r.notify || r.sound }

// normalizeKey writes a key the way the page names it: Ctrl, Alt, Shift, Meta in this order, then the key
// (Alt+1, Ctrl+Shift+F5). A key without Ctrl or Alt is refused: it would be typing.
func normalizeKey(text string) string {
	mods := map[string]bool{}
	key := ""
	for _, part := range strings.Split(text, "+") {
		part = strings.TrimSpace(part)
		switch strings.ToLower(part) {
		case "ctrl", "alt", "shift", "meta":
			mods[strings.ToLower(part)] = true
		default:
			if key != "" || part == "" {
				return ""
			}
			key = strings.ToUpper(part[:1]) + part[1:]
		}
	}
	if key == "" || (!mods["ctrl"] && !mods["alt"]) {
		return ""
	}
	var out []string
	for _, m := range []string{"Ctrl", "Alt", "Shift", "Meta"} {
		if mods[strings.ToLower(m)] {
			out = append(out, m)
		}
	}
	return strings.Join(append(out, key), "+")
}

// ruleNames are the buttons, the keys and the graph series the rules make, in order, for the page.
func ruleNames(rules []rule) (buttons, keys, series []string) {
	for _, r := range rules {
		if r.when == "button" {
			buttons = append(buttons, r.name)
		}
		if r.when == "key" {
			keys = append(keys, r.name)
		}
		if r.graph != "" && !oneOf(r.graph, series) {
			series = append(series, r.graph)
		}
	}
	return buttons, keys, series
}

// styleOfLine is what the rules do with a line: its colour, and whether it is hidden.
func styleOfLine(rules []rule, line string) (sgr string, hide bool) {
	for _, r := range rules {
		if r.when != "match" || (r.sgr == "" && !r.hide) || !r.pattern.MatchString(line) {
			continue
		}
		if r.hide {
			return "", true
		}
		if sgr == "" {
			sgr = r.sgr
		}
	}
	return sgr, false
}

// graphValues are the numbers the rules take from a line.
func graphValues(rules []rule, line string) []graphValue {
	if len(line) > maxGraphLine {
		line = line[:maxGraphLine]
	}
	var out []graphValue
	for _, r := range rules {
		if r.graph == "" {
			continue
		}
		m := r.pattern.FindStringSubmatchIndex(line)
		if m == nil {
			continue
		}
		text := line[m[1]:] // the number after the match
		if len(m) >= 4 && m[2] >= 0 {
			text = line[m[2]:m[3]] // or the first group
		}
		if v, ok := leadingValue(text); ok {
			out = append(out, graphValue{r.graph, v})
		}
	}
	return out
}
