package main

import "strings"

// The filter of a view: it shows only the lines that match, or hides them (! in front).

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
