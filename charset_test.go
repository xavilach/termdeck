package main

import "testing"

func TestCodecs(t *testing.T) {
	if NewCodec("utf-8") != nil || NewCodec("nonsense") != nil {
		t.Error("UTF-8 needs no conversion")
	}
	var none *Codec
	if string(none.FromDevice([]byte("é"))) != "é" || string(none.ToDevice([]byte("é"))) != "é" {
		t.Error("a nil codec passes bytes through")
	}
	cases := []struct {
		set    string
		device []byte
		text   string
	}{
		{"cp437", []byte{0x82, 0xc4, 'A'}, "é─A"},
		{"cp850", []byte{0x82, 0xd5}, "éı"},
		{"iso-8859-1", []byte{0xe9, 0xfc}, "éü"},
		{"windows-1252", []byte{0x80, 0xe9}, "€é"},
		{"koi8-r", []byte{0xc1}, "а"},
		{"windows-1251", []byte{0xcf}, "П"},
	}
	for _, c := range cases {
		codec := NewCodec(c.set)
		if got := string(codec.FromDevice(c.device)); got != c.text {
			t.Errorf("%s: from device %x = %q, want %q", c.set, c.device, got, c.text)
		}
		if got := codec.ToDevice([]byte(c.text)); string(got) != string(c.device) {
			t.Errorf("%s: to device %q = %x, want %x", c.set, c.text, got, c.device)
		}
	}
	if got := NewCodec("cp437").ToDevice([]byte("日")); string(got) != "?" {
		t.Errorf("unmappable character: %q", got)
	}
}
