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

// checkFrameSpec validates lines "sync = AA 55", "size = u8@2 + 5" (or a number), "end = 0D 0A",
// "header = 3" and "check = crc16modbus". Empty lines and # notes are skipped; an empty text is fine.
func checkFrameSpec(text string) error {
	if strings.TrimSpace(text) == "" {
		return nil
	}
	var hasSize, hasEnd, any bool
	for _, line := range strings.Split(text, "\n") {
		if at := strings.Index(line, "#"); at >= 0 {
			line = line[:at]
		}
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		any = true
		key, value, ok := strings.Cut(line, "=")
		key, value = strings.ToLower(strings.TrimSpace(key)), strings.TrimSpace(value)
		if !ok || key == "" {
			return fmt.Errorf("frames %q: use  name = value", line)
		}
		switch key {
		case "sync", "end":
			if !frameHexList.MatchString(value) {
				return fmt.Errorf("frames %q: %s is bytes in hex, like AA 55", line, key)
			}
			hasEnd = hasEnd || key == "end"
		case "size":
			if _, err := strconv.Atoi(value); err != nil && !frameField.MatchString(value) {
				return fmt.Errorf("frames %q: size is a number or u8@2 + 5 (u8, u16be or u16le at an offset)", line)
			}
			hasSize = true
		case "header":
			if n, err := strconv.Atoi(value); err != nil || n < 0 {
				return fmt.Errorf("frames %q: header is a number of bytes", line)
			}
		case "check":
			if !oneOf(value, quickChecks) {
				return fmt.Errorf("frames %q: check is one of %s", line, strings.Join(quickChecks, ", "))
			}
		default:
			return fmt.Errorf("frames %q: unknown name %q (sync, size, end, header, check)", line, key)
		}
	}
	switch {
	case !any:
		return nil
	case !hasSize && !hasEnd:
		return fmt.Errorf("frames: give a size or an end")
	case hasSize && hasEnd:
		return fmt.Errorf("frames: give a size or an end, not both")
	}
	return nil
}
