package main

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// The frames view cuts the received bytes into frames described by the session (see frameSpec in the page's
// format.js, which does the work): the description is checked here so that a mistake is found when it is saved.

var (
	frameField   = regexp.MustCompile(`^(u8|u16be|u16le)@(\d+)(?:\s*\+\s*(\d+))?$`)
	frameHexList = regexp.MustCompile(`^\s*([0-9a-fA-F]{2}(\s*|,))+$`)
)

// frameKeys check the value of each name of a frame description.
var frameKeys = map[string]func(value string) error{
	"sync": checkFrameBytes("sync"),
	"end":  checkFrameBytes("end"),
	"size": func(value string) error {
		if _, err := strconv.Atoi(value); err != nil && !frameField.MatchString(value) {
			return fmt.Errorf("size is a number or u8@2 + 5 (u8, u16be or u16le at an offset)")
		}
		return nil
	},
	"header": func(value string) error {
		if n, err := strconv.Atoi(value); err != nil || n < 0 {
			return fmt.Errorf("header is a number of bytes")
		}
		return nil
	},
	"check": func(value string) error {
		if !oneOf(value, quickChecks) {
			return fmt.Errorf("check is one of %s", strings.Join(quickChecks, ", "))
		}
		return nil
	},
}

func checkFrameBytes(name string) func(string) error {
	return func(value string) error {
		if !frameHexList.MatchString(value) {
			return fmt.Errorf("%s is bytes in hex, like AA 55", name)
		}
		return nil
	}
}

// checkFrameSpec validates lines "sync = AA 55", "size = u8@2 + 5" (or a number), "end = 0D 0A",
// "header = 3" and "check = crc16modbus". Empty lines and # notes are skipped; an empty text is fine.
func checkFrameSpec(text string) error {
	seen := map[string]bool{}
	for _, line := range strings.Split(text, "\n") {
		if at := strings.Index(line, "#"); at >= 0 {
			line = line[:at]
		}
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		key, value = strings.ToLower(strings.TrimSpace(key)), strings.TrimSpace(value)
		if !ok || key == "" {
			return fmt.Errorf("frames %q: use  name = value", line)
		}
		check := frameKeys[key]
		if check == nil {
			return fmt.Errorf("frames %q: unknown name %q (sync, size, end, header, check)", line, key)
		}
		if err := check(value); err != nil {
			return fmt.Errorf("frames %q: %w", line, err)
		}
		seen[key] = true
	}
	switch {
	case len(seen) == 0:
		return nil
	case !seen["size"] && !seen["end"]:
		return fmt.Errorf("frames: give a size or an end")
	case seen["size"] && seen["end"]:
		return fmt.Errorf("frames: give a size or an end, not both")
	}
	return nil
}
