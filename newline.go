package main

import (
	"regexp"
	"strconv"
	"unicode/utf8"
)

// "New line at": what ends a received line. Terminal leaves CR and LF to the terminal; CR, LF, CR LF and
// Custom end a line with that sequence (any other CR or LF is dropped); None ends lines only after a pause.

var controlNames = [32]string{"NUL", "SOH", "STX", "ETX", "EOT", "ENQ", "ACK", "BEL", "BS", "TAB", "LF", "VT", "FF", "CR", "SO", "SI",
	"DLE", "DC1", "DC2", "DC3", "DC4", "NAK", "SYN", "ETB", "CAN", "EM", "SUB", "ESC", "FS", "GS", "RS", "US"}

func isControl(b byte) bool { return b < 32 || b == 127 }

func controlName(b byte) string {
	if b == 127 {
		return "DEL"
	}
	return controlNames[b]
}

var textEscape = regexp.MustCompile(`\\(x[0-9a-fA-F]{2}|[rnt0\\])`)

// unescapeText reads text typed with \r \n \t \0 \\ and \xNN; \xNN is that byte.
func unescapeText(text string) []byte {
	var out []byte
	last := 0
	for _, m := range textEscape.FindAllStringSubmatchIndex(text, -1) {
		out = append(out, text[last:m[0]]...)
		code := text[m[2]:m[3]]
		switch code[0] {
		case 'x':
			n, _ := strconv.ParseUint(code[1:], 16, 8)
			out = append(out, byte(n))
		case 'r':
			out = append(out, '\r')
		case 'n':
			out = append(out, '\n')
		case 't':
			out = append(out, '\t')
		case '0':
			out = append(out, 0)
		default:
			out = append(out, '\\')
		}
		last = m[1]
	}
	return append(out, text[last:]...)
}

// escapeText writes bytes the way they are typed: \r \n \t \0 \\ and \xNN for the other control characters.
func escapeText(data []byte) string {
	var out []byte
	for len(data) > 0 {
		r, size := utf8.DecodeRune(data)
		b := data[0]
		switch {
		case b == '\r':
			out = append(out, `\r`...)
		case b == '\n':
			out = append(out, `\n`...)
		case b == '\t':
			out = append(out, `\t`...)
		case b == 0:
			out = append(out, `\0`...)
		case b == '\\':
			out = append(out, `\\`...)
		case isControl(b) || (r == utf8.RuneError && size == 1):
			out = append(out, `\x`+hexText([]byte{b})...)
		default:
			out = append(out, data[:size]...)
		}
		data = data[size:]
	}
	return string(out)
}

// lineShaper applies "New line at" to the received text, across chunks: the start of the sequence is held
// back until the next byte says whether it is one.
type lineShaper struct {
	ends   []byte // nil: the terminal does as it always does
	ctrl   bool   // control characters shown as <CR>, <ESC>...
	ansi   bool   // the names in grey, lines ended by CR LF
	held   []byte
	isTerm bool
}

// newLineShaper is the shaper of a view. Plain text (the log) has no terminal: there, LF ends a line.
func newLineShaper(view viewSettings, ansi bool) lineShaper {
	s := lineShaper{ctrl: view.Ctrl, ansi: ansi}
	switch view.NL {
	case "cr":
		s.ends = []byte{'\r'}
	case "lf":
		s.ends = []byte{'\n'}
	case "crlf":
		s.ends = []byte{'\r', '\n'}
	case "custom":
		s.ends = append([]byte{}, unescapeText(view.NLCustom)...) // never nil: empty ends no line
	case "none":
		s.ends = []byte{}
	default:
		s.isTerm = true
		if !ansi {
			s.ends = []byte{'\n'}
		}
	}
	return s
}

// terminal says whether CR and LF are left to the terminal.
func (s *lineShaper) terminal() bool { return s.isTerm }

func (s *lineShaper) mark(out []byte, b byte) []byte {
	if s.ansi {
		return append(out, "\x1b[90m<"+controlName(b)+">\x1b[39m"...)
	}
	return append(out, "<"+controlName(b)+">"...)
}

func (s *lineShaper) shape(data []byte) []byte {
	if s.ends == nil && !s.ctrl {
		return data
	}
	ends := s.ends
	if ends == nil {
		ends = []byte{'\n'} // the terminal's own way, with control characters shown: LF ends the line
	}
	out := make([]byte, 0, len(data)+8)
	plain := func(b byte) {
		switch {
		case s.ctrl && isControl(b):
			out = s.mark(out, b)
		case b != '\r' && b != '\n':
			out = append(out, b)
		}
	}
	for _, b := range data {
		s.held = append(s.held, b)
		for len(s.held) > 0 && !isPrefix(ends, s.held) {
			plain(s.held[0])
			s.held = s.held[1:]
		}
		if len(ends) > 0 && len(s.held) == len(ends) {
			for _, x := range s.held {
				switch {
				case !isControl(x):
					out = append(out, x)
				case s.ctrl:
					out = s.mark(out, x)
				}
			}
			if s.ansi {
				out = append(out, '\r', '\n')
			} else {
				out = append(out, '\n')
			}
			s.held = s.held[:0]
		}
	}
	return out
}

func isPrefix(sequence, data []byte) bool {
	return len(data) <= len(sequence) && string(sequence[:len(data)]) == string(data)
}
