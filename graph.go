package main

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// Graph: numbers found in the received lines. The series come from the session settings, one per line:
// "name | prefix | suffix" takes the number after the prefix (up to the suffix, if there is one), and
// "name | /regex/" takes the first group of the match (the whole match when there is no group). Prefix and
// suffix are patterns: text in any case, or /regex/.
// The matching is done here with Go's regular expressions, which cannot run away on a long line.

type graphSpec struct {
	name           string
	prefix, suffix Pattern
	hasSuffix      bool
}

type graphValue struct {
	Name  string  `json:"name"`
	Value float64 `json:"value"`
}

var leadingNumber = regexp.MustCompile(`^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)`)

// maxGraphLine is how much of a line is looked at.
const maxGraphLine = 4096

// parseGraphSeries reads the lines of the settings. Empty lines and lines starting with # are skipped.
func parseGraphSeries(text string) ([]graphSpec, error) {
	var specs []graphSpec
	seen := map[string]bool{}
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := strings.Split(line, "|")
		if len(parts) < 2 || len(parts) > 3 {
			return nil, fmt.Errorf("graph series %q: use  name | prefix | suffix  or  name | /regex/", line)
		}
		spec := graphSpec{name: strings.TrimSpace(parts[0])}
		prefix, suffix := strings.TrimSpace(parts[1]), ""
		if len(parts) == 3 {
			suffix = strings.TrimSpace(parts[2])
		}
		if spec.name == "" || prefix == "" || len(spec.name) > 30 {
			return nil, fmt.Errorf("graph series %q: use  name | prefix | suffix  or  name | /regex/", line)
		}
		if seen[spec.name] {
			return nil, fmt.Errorf("graph series %q is there twice", spec.name)
		}
		seen[spec.name] = true
		var err error
		if spec.prefix, err = ParsePattern(prefix); err != nil {
			return nil, fmt.Errorf("graph series %q: %w", spec.name, err)
		}
		if suffix != "" {
			spec.hasSuffix = true
			if spec.suffix, err = ParsePattern(suffix); err != nil {
				return nil, fmt.Errorf("graph series %q: %w", spec.name, err)
			}
		}
		specs = append(specs, spec)
	}
	return specs, nil
}

// extractValues gives the numbers of a line, one for each series that finds one.
func extractValues(line string, specs []graphSpec) []graphValue {
	if len(line) > maxGraphLine {
		line = line[:maxGraphLine]
	}
	var out []graphValue
	for _, spec := range specs {
		text, ok := spec.find(line)
		if !ok {
			continue
		}
		if m := leadingNumber.FindStringSubmatch(text); m != nil {
			if v, err := strconv.ParseFloat(m[1], 64); err == nil {
				out = append(out, graphValue{spec.name, v})
			}
		}
	}
	return out
}

// find is the text a series looks for a number in.
// A /regex/ alone gives its first group (or the whole match); otherwise the number follows the prefix.
func (s graphSpec) find(line string) (string, bool) {
	if s.prefix.IsRegex() && !s.hasSuffix {
		m := s.prefix.FindStringSubmatch(line)
		switch {
		case m == nil:
			return "", false
		case len(m) > 1:
			return m[1], true
		}
		return m[0], true
	}
	loc := s.prefix.FindStringIndex(line)
	if loc == nil {
		return "", false
	}
	text := line[loc[1]:]
	if s.hasSuffix {
		end := s.suffix.FindStringIndex(text)
		if end == nil {
			return "", false
		}
		text = text[:end[0]]
	}
	return text, true
}

// lineSplitter cuts received text into lines at CR, LF or CR LF, keeping the unfinished line for the next chunk.
type lineSplitter struct{ partial string }

func (l *lineSplitter) feed(text []byte) []string {
	all := l.partial + string(text)
	var lines []string
	start := 0
	for i := 0; i < len(all); i++ {
		if all[i] == '\r' || all[i] == '\n' {
			if i > start {
				lines = append(lines, all[start:i])
			}
			start = i + 1
		}
	}
	l.partial = all[start:]
	if len(l.partial) > maxGraphLine {
		l.partial = l.partial[len(l.partial)-maxGraphLine:]
	}
	return lines
}
