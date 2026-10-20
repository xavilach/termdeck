package main

import (
	"regexp"
	"strconv"
)

// Graph: numbers found in the received lines, by the rules with "graph Name" (see rules.go). The numbers
// are found here, with Go's regular expressions, which cannot run away on a long line.

type graphValue struct {
	Name  string  `json:"name"`
	Value float64 `json:"value"`
}

var leadingNumber = regexp.MustCompile(`^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)`)

// maxGraphLine is how much of a line is looked at.
const maxGraphLine = 4096

// leadingValue is the number a text starts with (after spaces).
func leadingValue(text string) (float64, bool) {
	m := leadingNumber.FindStringSubmatch(text)
	if m == nil {
		return 0, false
	}
	v, err := strconv.ParseFloat(m[1], 64)
	return v, err == nil
}
