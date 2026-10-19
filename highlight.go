package main

import (
	"fmt"
	"regexp"
	"strings"
)

// Highlights colour the received lines that match, and a filter shows or hides lines. Both are done by the
// page, with JavaScript regular expressions, which can run away on some patterns; a pattern that nests
// repeats, such as (a+)+, is refused here.

var highlightStyles = []string{"red", "green", "yellow", "blue", "magenta", "cyan", "white", "grey", "orange", "bold", "dim", "underline", "inverse", "notify"}

var nestedRepeat = regexp.MustCompile(`\([^()]*[+*][^()]*\)[+*{]`)

// checkPattern validates the text of a pattern: plain text, or /regex/.
func checkPattern(pattern string) error {
	if len(pattern) > 2 && strings.HasPrefix(pattern, "/") && strings.HasSuffix(pattern, "/") {
		expr := pattern[1 : len(pattern)-1]
		if _, err := regexp.Compile(expr); err != nil {
			return err
		}
		if nestedRepeat.MatchString(expr) {
			return fmt.Errorf("%q repeats a repeat, which can make the page hang", pattern)
		}
	}
	return nil
}

// checkHighlights validates lines "pattern = style" or "pattern = style+style". Lines starting with # are notes.
func checkHighlights(text string) error {
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		at := strings.LastIndex(line, "=")
		if at < 1 {
			return fmt.Errorf("highlight %q: use  pattern = colour", line)
		}
		pattern, styles := strings.TrimSpace(line[:at]), strings.TrimSpace(line[at+1:])
		if pattern == "" || styles == "" {
			return fmt.Errorf("highlight %q: use  pattern = colour", line)
		}
		for _, style := range strings.Split(styles, "+") {
			if !oneOf(strings.TrimSpace(style), highlightStyles) {
				return fmt.Errorf("highlight %q: the style is one of %s, joined with +", line, strings.Join(highlightStyles, ", "))
			}
		}
		if err := checkPattern(pattern); err != nil {
			return fmt.Errorf("highlight %q: %w", line, err)
		}
	}
	return nil
}

// checkFilter validates the filter: text or /regex/, with ! in front to hide the lines that match.
func checkFilter(text string) error {
	return checkPattern(strings.TrimPrefix(strings.TrimSpace(text), "!"))
}
