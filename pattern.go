package main

import (
	"fmt"
	"regexp"
	"strings"
)

// Pattern is what triggers, expect, the graph, highlights and the filter look for. It is written the same way
// everywhere: text, matched in any case, or /regex/, matched as written (/regex/i in any case). Only the
// server matches patterns, so every feature agrees.

type Pattern struct {
	Text string // as written
	re   *regexp.Regexp
}

// ParsePattern compiles a pattern. An empty text is refused.
func ParsePattern(text string) (Pattern, error) {
	if text == "" {
		return Pattern{}, fmt.Errorf("the pattern is empty")
	}
	expr := "(?i)" + regexp.QuoteMeta(text)
	if body, flags, ok := regexBody(text); ok {
		expr = body
		if flags == "i" {
			expr = "(?i)" + body
		}
	}
	re, err := regexp.Compile(expr)
	if err != nil {
		return Pattern{}, fmt.Errorf("%q: %w", text, err)
	}
	return Pattern{Text: text, re: re}, nil
}

// regexBody returns the expression of /regex/ or /regex/i, and its flags.
func regexBody(text string) (body, flags string, ok bool) {
	if len(text) < 3 || text[0] != '/' {
		return "", "", false
	}
	if strings.HasSuffix(text, "/i") && len(text) > 3 {
		return text[1 : len(text)-2], "i", true
	}
	if strings.HasSuffix(text, "/") {
		return text[1 : len(text)-1], "", true
	}
	return "", "", false
}

func (p Pattern) FindIndex(b []byte) []int             { return p.re.FindIndex(b) }
func (p Pattern) MatchString(s string) bool            { return p.re.MatchString(s) }
func (p Pattern) FindStringSubmatch(s string) []string { return p.re.FindStringSubmatch(s) }
func (p Pattern) FindStringIndex(s string) []int       { return p.re.FindStringIndex(s) }
func (p Pattern) IsRegex() bool                        { _, _, ok := regexBody(p.Text); return ok }
