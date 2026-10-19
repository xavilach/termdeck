package main

import (
	"unicode/utf8"

	"golang.org/x/text/encoding/charmap"
)

// charsets are the single-byte character sets a device can use besides UTF-8.
var charsets = []struct {
	Name string
	Map  *charmap.Charmap
}{
	{"utf-8", nil},
	{"iso-8859-1", charmap.ISO8859_1},
	{"iso-8859-2", charmap.ISO8859_2},
	{"iso-8859-15", charmap.ISO8859_15},
	{"windows-1250", charmap.Windows1250},
	{"windows-1251", charmap.Windows1251},
	{"windows-1252", charmap.Windows1252},
	{"cp437", charmap.CodePage437},
	{"cp850", charmap.CodePage850},
	{"cp866", charmap.CodePage866},
	{"koi8-r", charmap.KOI8R},
	{"koi8-u", charmap.KOI8U},
	{"macintosh", charmap.Macintosh},
}

func charsetNames() []string {
	names := make([]string, len(charsets))
	for i, c := range charsets {
		names[i] = c.Name
	}
	return names
}

// Codec converts between the device's character set and the UTF-8 the page uses.
// A nil Codec passes bytes through (the device already speaks UTF-8).
type Codec struct {
	cm *charmap.Charmap
}

func NewCodec(name string) *Codec {
	for _, c := range charsets {
		if c.Name == name && c.Map != nil {
			return &Codec{cm: c.Map}
		}
	}
	return nil
}

// FromDevice turns bytes from the device into UTF-8. Single-byte sets need no state between chunks.
func (c *Codec) FromDevice(b []byte) []byte {
	if c == nil {
		return b
	}
	out := make([]byte, 0, len(b))
	for _, x := range b {
		if x < 0x80 {
			out = append(out, x)
		} else {
			out = utf8.AppendRune(out, c.cm.DecodeByte(x))
		}
	}
	return out
}

// ToDevice turns UTF-8 typed in the page into the device's bytes; characters it cannot show become "?".
func (c *Codec) ToDevice(b []byte) []byte {
	if c == nil {
		return b
	}
	out := make([]byte, 0, len(b))
	for _, r := range string(b) {
		if x, ok := c.cm.EncodeRune(r); ok {
			out = append(out, x)
		} else {
			out = append(out, '?')
		}
	}
	return out
}
