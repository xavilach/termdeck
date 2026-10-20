package main

import (
	"fmt"
	"strings"
)

// Highlights colour the received lines that match (and can ask for a desktop notification), and a filter
// shows or hides lines. Both are matched here, on the lines the renderer cuts.

var highlightStyles = map[string]string{"red": "31", "green": "32", "yellow": "33", "blue": "34", "magenta": "35", "cyan": "36",
	"white": "37", "grey": "90", "orange": "38;5;208", "bold": "1", "dim": "2", "underline": "4", "inverse": "7", "notify": ""}

type highlight struct {
	pattern Pattern
	sgr     string // the SGR codes of the style, "" for notify alone
	notify  bool
}

// parseHighlights reads lines "pattern = style" or "pattern = style+style". Lines starting with # are notes.
func parseHighlights(text string) ([]highlight, error) {
	var out []highlight
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		at := strings.LastIndex(line, "=")
		if at < 1 {
			return nil, fmt.Errorf("highlight %q: use  pattern = colour", line)
		}
		pattern, styles := strings.TrimSpace(line[:at]), strings.TrimSpace(line[at+1:])
		if pattern == "" || styles == "" {
			return nil, fmt.Errorf("highlight %q: use  pattern = colour", line)
		}
		h := highlight{}
		var codes []string
		for _, style := range strings.Split(styles, "+") {
			code, ok := highlightStyles[strings.TrimSpace(style)]
			if !ok {
				return nil, fmt.Errorf("highlight %q: the style is one of %s, joined with +", line, styleNames)
			}
			if code == "" {
				h.notify = true
			} else {
				codes = append(codes, code)
			}
		}
		h.sgr = strings.Join(codes, ";")
		var err error
		if h.pattern, err = ParsePattern(pattern); err != nil {
			return nil, fmt.Errorf("highlight %q: %w", line, err)
		}
		out = append(out, h)
	}
	return out, nil
}

const styleNames = "red, green, yellow, blue, magenta, cyan, white, grey, orange, bold, dim, underline, inverse, notify"

func checkHighlights(text string) error {
	_, err := parseHighlights(text)
	return err
}

// styleOf is the style of the first highlight with a colour that the line matches, or "".
func styleOf(rules []highlight, line string) string {
	for _, h := range rules {
		if h.sgr != "" && h.pattern.MatchString(line) {
			return h.sgr
		}
	}
	return ""
}

// notifies says whether the line matches a highlight that asks for a notification.
func notifies(rules []highlight, line string) bool {
	for _, h := range rules {
		if h.notify && h.pattern.MatchString(line) {
			return true
		}
	}
	return false
}

// lineFilter shows only the lines that match its pattern, or hides them when inverted.
type lineFilter struct {
	pattern Pattern
	invert  bool
}

// parseFilter reads the filter: a pattern, with ! in front to hide the lines that match. Empty is no filter (nil).
func parseFilter(text string) (*lineFilter, error) {
	text = strings.TrimSpace(text)
	invert := strings.HasPrefix(text, "!")
	text = strings.TrimSpace(strings.TrimPrefix(text, "!"))
	if text == "" {
		return nil, nil
	}
	p, err := ParsePattern(text)
	if err != nil {
		return nil, err
	}
	return &lineFilter{pattern: p, invert: invert}, nil
}

func checkFilter(text string) error {
	_, err := parseFilter(text)
	return err
}

// passes says whether a line is shown. A nil filter shows every line.
func (f *lineFilter) passes(line string) bool {
	return f == nil || f.pattern.MatchString(line) != f.invert
}
