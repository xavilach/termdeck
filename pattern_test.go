package main

import (
	"encoding/json"
	"os"
	"testing"
)

// testdata/patterns.json lists how patterns match (the help of the page says the same).
func TestPatternsMatchTheSharedTable(t *testing.T) {
	data, err := os.ReadFile("testdata/patterns.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases []struct {
		Pattern, Text string
		Match         bool
	}
	if err := json.Unmarshal(data, &cases); err != nil {
		t.Fatal(err)
	}
	for _, c := range cases {
		p, err := ParsePattern(c.Pattern)
		if err != nil {
			t.Errorf("%q: %v", c.Pattern, err)
			continue
		}
		if got := p.MatchString(c.Text); got != c.Match {
			t.Errorf("%q on %q: got %v", c.Pattern, c.Text, got)
		}
	}
}

func TestATriggerOrAnExpectWrittenAsTextIgnoresCase(t *testing.T) {
	triggers, err := parseTriggers("login: => admin")
	if err != nil || triggers[0].pattern.FindIndex([]byte("Login: ")) == nil {
		t.Error("the trigger did not match Login:")
	}
	steps, err := parseSteps("expect=password")
	if err != nil || !steps[0].pattern.MatchString("Password:") {
		t.Error("expect did not match Password:")
	}
	specs, _ := parseGraphSeries("Temp | temperature: | DEGC")
	if got := extractValues("Temperature: 21 degC", specs); len(got) != 1 || got[0].Value != 21 {
		t.Errorf("graph: %v", got)
	}
}

func TestAnEmptyOrBrokenPatternIsRefused(t *testing.T) {
	for _, bad := range []string{"", "/(/", "/a[/i"} {
		if _, err := ParsePattern(bad); err == nil {
			t.Errorf("%q was accepted", bad)
		}
	}
}
