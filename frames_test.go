package main

import "testing"

func TestFrameDescriptionsAreChecked(t *testing.T) {
	for _, good := range []string{"", "  \n# nothing\n", "sync = AA 55\nsize = u8@2 + 5\nheader = 3\ncheck = crc16modbus", "end = 0D 0A", "size = 16", "sync = 7E\nsize = u16le@1 + 3 # the length is little endian"} {
		if err := checkFrameSpec(good); err != nil {
			t.Errorf("%q: %v", good, err)
		}
	}
	for _, bad := range []string{"sync = AA", "sync = zz", "size = 4\nend = 0D", "size = banana", "colour = red", "size = 4\ncheck = md5", "size = 4\nheader = x", "nothing here", "sync = AA 55"} {
		if checkFrameSpec(bad) == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
	p := Profile{Device: "x", FrameSpec: "size = banana"}
	if p.Normalize() == nil {
		t.Error("a profile with a bad frame description was accepted")
	}
	if (&Profile{Device: "x", View: "frames"}).Normalize() != nil {
		t.Error("the frames view is a view")
	}
}
