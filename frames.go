package main

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
)

// Frames: the received bytes cut into frames described by the session, one line each, with the check verified.
//
//	sync = AA 55        the bytes a frame starts with (optional; other bytes are noise)
//	size = u8@2 + 5     the size of the whole frame: a number, or a field (u8, u16be, u16le at an offset) plus a number
//	end = 0D 0A         the frame ends with these bytes (instead of size)
//	header = 3          bytes shown apart before the payload
//	check = crc16modbus check bytes at the end of the frame, over the bytes before them (see checks.go)

type frameSpec struct {
	sync, end   []byte
	fixed       int    // size = 16
	field       string // u8, u16be or u16le; empty for a fixed size or an end
	at, add     int    // the field's offset and what is added to it
	sized       bool
	header      int
	check       checkKind
	hasCheck    bool
	description string
}

var (
	frameField   = regexp.MustCompile(`^(u8|u16be|u16le)@(\d+)(?:\s*\+\s*(\d+))?$`)
	frameHexList = regexp.MustCompile(`^\s*([0-9a-fA-F]{2}(\s*|,))+$`)
)

const maxFrame = 65536

// frameKeys set each name of a description.
var frameKeys = map[string]func(spec *frameSpec, value string) error{
	"sync": func(spec *frameSpec, value string) (err error) { spec.sync, err = frameBytes("sync", value); return },
	"end":  func(spec *frameSpec, value string) (err error) { spec.end, err = frameBytes("end", value); return },
	"size": func(spec *frameSpec, value string) error {
		spec.sized = true
		if m := frameField.FindStringSubmatch(value); m != nil {
			spec.field = m[1]
			spec.at, _ = strconv.Atoi(m[2])
			if m[3] != "" {
				spec.add, _ = strconv.Atoi(m[3])
			}
			return nil
		}
		n, err := strconv.Atoi(value)
		if err != nil {
			return fmt.Errorf("size is a number or u8@2 + 5 (u8, u16be or u16le at an offset)")
		}
		spec.fixed = n
		return nil
	},
	"header": func(spec *frameSpec, value string) error {
		n, err := strconv.Atoi(value)
		if err != nil || n < 0 {
			return fmt.Errorf("header is a number of bytes")
		}
		spec.header = n
		return nil
	},
	"check": func(spec *frameSpec, value string) error {
		if value == "none" {
			return nil
		}
		k, ok := findCheck(value)
		if !ok {
			return fmt.Errorf("check is one of %s", strings.Join(checkNames(), ", "))
		}
		spec.check, spec.hasCheck = k, true
		return nil
	},
}

func frameBytes(name, value string) ([]byte, error) {
	if !frameHexList.MatchString(value) {
		return nil, fmt.Errorf("%s is bytes in hex, like AA 55", name)
	}
	return parseHex(value)
}

// parseFrameSpec reads a description. An empty one (nothing but notes) gives nil, nil.
func parseFrameSpec(text string) (*frameSpec, error) {
	spec := &frameSpec{description: text}
	seen := false
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
			return nil, fmt.Errorf("frames %q: use  name = value", line)
		}
		set := frameKeys[key]
		if set == nil {
			return nil, fmt.Errorf("frames %q: unknown name %q (sync, size, end, header, check)", line, key)
		}
		if err := set(spec, value); err != nil {
			return nil, fmt.Errorf("frames %q: %w", line, err)
		}
		seen = true
	}
	switch {
	case !seen:
		return nil, nil
	case !spec.sized && len(spec.end) == 0:
		return nil, fmt.Errorf("frames: give a size or an end")
	case spec.sized && len(spec.end) > 0:
		return nil, fmt.Errorf("frames: give a size or an end, not both")
	}
	return spec, nil
}

// checkFrameSpec validates a description when the session is saved.
func checkFrameSpec(text string) error {
	_, err := parseFrameSpec(text)
	return err
}

// frameRow is a frame found, or bytes that are not one (noise).
type frameRow struct {
	bytes    []byte
	noise    bool
	checked  bool // the frame has a check, and it was verified
	ok       bool
	expected []byte
}

// frameParser takes bytes as they come and gives the frames found.
type frameParser struct {
	spec *frameSpec
	buf  []byte
}

// size is the size of the frame at the start of the buffer, 0 when more bytes are needed, -1 when it cannot be one.
func (p *frameParser) size() int {
	spec, buf := p.spec, p.buf
	if len(spec.end) > 0 {
		for i := len(spec.sync); i+len(spec.end) <= len(buf); i++ {
			if string(buf[i:i+len(spec.end)]) == string(spec.end) {
				return i + len(spec.end)
			}
		}
		if len(buf) > maxFrame {
			return -1
		}
		return 0
	}
	total := spec.fixed
	if spec.field != "" {
		width := 2
		if spec.field == "u8" {
			width = 1
		}
		if len(buf) < spec.at+width {
			return 0
		}
		switch spec.field {
		case "u8":
			total = int(buf[spec.at])
		case "u16be":
			total = int(buf[spec.at])<<8 | int(buf[spec.at+1])
		default:
			total = int(buf[spec.at]) | int(buf[spec.at+1])<<8
		}
		total += spec.add
	}
	if total < len(spec.sync)+1 || total > maxFrame {
		return -1
	}
	return total
}

// syncAt says whether the buffer at i starts with the sync bytes, or with the start of them at its end.
func (p *frameParser) syncAt(i int) bool {
	n := min(len(p.spec.sync), len(p.buf)-i)
	return string(p.buf[i:i+n]) == string(p.spec.sync[:n])
}

func (p *frameParser) feed(data []byte) []frameRow {
	p.buf = append(p.buf, data...)
	var rows []frameRow
	for {
		if len(p.spec.sync) > 0 { // noise before the sync bytes is its own row
			skip := 0
			for skip < len(p.buf) && !p.syncAt(skip) {
				skip++
			}
			if skip > 0 {
				rows = append(rows, frameRow{bytes: p.take(skip), noise: true})
			}
			if len(p.buf) == 0 {
				break
			}
		}
		size := p.size()
		if size == 0 {
			break
		}
		if size < 0 { // not a frame: one byte of noise, look again
			rows = append(rows, frameRow{bytes: p.take(1), noise: true})
			continue
		}
		if len(p.buf) < size {
			break
		}
		row := frameRow{bytes: p.take(size)}
		if p.spec.hasCheck && size > p.spec.check.size {
			body := row.bytes[:size-p.spec.check.size]
			row.checked, row.expected = true, p.spec.check.make(body)
			row.ok = string(row.expected) == string(row.bytes[size-p.spec.check.size:])
		}
		rows = append(rows, row)
	}
	return rows
}

func (p *frameParser) take(n int) []byte {
	out := append([]byte(nil), p.buf[:n]...)
	p.buf = p.buf[n:]
	return out
}

// frameLine is one frame as a line: header | payload | check, with OK or BAD (and what the check should be).
func frameLine(spec *frameSpec, row frameRow) string {
	if row.noise {
		return "?? " + hexText(row.bytes)
	}
	checkSize := 0
	if row.checked {
		checkSize = spec.check.size
	}
	split := min(spec.header, len(row.bytes)-checkSize)
	var parts []string
	for _, part := range [][]byte{row.bytes[:split], row.bytes[split : len(row.bytes)-checkSize], row.bytes[len(row.bytes)-checkSize:]} {
		if len(part) > 0 {
			parts = append(parts, hexText(part))
		}
	}
	text := strings.Join(parts, " | ")
	switch {
	case row.checked && row.ok:
		text += "  OK"
	case row.checked:
		text += "  BAD (expected " + hexText(row.expected) + ")"
	}
	return text
}

// hexText writes bytes as upper case hex pairs separated by spaces.
func hexText(data []byte) string {
	const digits = "0123456789ABCDEF"
	out := make([]byte, 0, len(data)*3)
	for i, b := range data {
		if i > 0 {
			out = append(out, ' ')
		}
		out = append(out, digits[b>>4], digits[b&15])
	}
	return string(out)
}
