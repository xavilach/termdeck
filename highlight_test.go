package main

import "testing"

func TestHighlightsAndFiltersAreChecked(t *testing.T) {
	good := "# colours\nERROR = red+bold\n/warn(ing)?/ = orange\nOK = green\na=b = cyan\nFAULT = red+notify\nreboot = notify"
	if err := checkHighlights(good); err != nil {
		t.Fatal(err)
	}
	for _, bad := range []string{"no equals", " = red", "x = ", "x = purple", "x = red+", "/(/ = red"} {
		if checkHighlights(bad) == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
	for _, ok := range []string{"", "heartbeat", "!heartbeat", "/^tick [0-9]+/", "!/err(or)?/"} {
		if err := checkFilter(ok); err != nil {
			t.Errorf("%q: %v", ok, err)
		}
	}
	for _, bad := range []string{"/(/", "!/(/"} {
		if checkFilter(bad) == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
	p := Profile{Device: "x", Highlights: "x = purple"}
	if p.Normalize() == nil {
		t.Error("a profile with a bad highlight was accepted")
	}
}
