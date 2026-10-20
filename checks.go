package main

// Checks: bytes added after data to check it (the Check of the send bar, the check of a frame). The same
// kinds and results as Format.CHECKS in the page; testdata/checks.json holds the cases both check.

type checkKind struct {
	name, label string
	size        int
	make        func([]byte) []byte
}

var checkKinds = []checkKind{
	{"sum8", "Sum (1 byte)", 1, func(b []byte) []byte {
		var s byte
		for _, x := range b {
			s += x
		}
		return []byte{s}
	}},
	{"xor8", "XOR (1 byte)", 1, func(b []byte) []byte {
		var s byte
		for _, x := range b {
			s ^= x
		}
		return []byte{s}
	}},
	{"crc8", "CRC-8", 1, func(b []byte) []byte { return bigEndian(crc(b, 8, 0x07, 0, false, 0), 1) }},
	{"crc16modbus", "CRC-16 Modbus (low byte first)", 2, func(b []byte) []byte { return littleEndian(crc(b, 16, 0x8005, 0xffff, true, 0), 2) }},
	{"crc16ccitt", "CRC-16 CCITT (high byte first)", 2, func(b []byte) []byte { return bigEndian(crc(b, 16, 0x1021, 0xffff, false, 0), 2) }},
	{"crc16xmodem", "CRC-16 XMODEM (high byte first)", 2, func(b []byte) []byte { return bigEndian(crc(b, 16, 0x1021, 0, false, 0), 2) }},
	{"crc32", "CRC-32 (low byte first)", 4, func(b []byte) []byte {
		return littleEndian(crc(b, 32, 0x04c11db7, 0xffffffff, true, 0xffffffff), 4)
	}},
}

// checkNames are the kinds of check, with "none" first.
func checkNames() []string {
	names := []string{"none"}
	for _, k := range checkKinds {
		names = append(names, k.name)
	}
	return names
}

func findCheck(name string) (checkKind, bool) {
	for _, k := range checkKinds {
		if k.name == name {
			return k, true
		}
	}
	return checkKind{}, false
}

// checksum is the check bytes of a kind; "none" or an unknown kind gives nothing.
func checksum(name string, data []byte) []byte {
	if k, ok := findCheck(name); ok {
		return k.make(data)
	}
	return nil
}

// crc computes a CRC of width bits, MSB first, with the bytes and the result reflected when asked.
func crc(data []byte, width int, poly, init uint32, reflect bool, xorOut uint32) uint32 {
	top := uint32(1) << (width - 1)
	mask := uint32(1<<width - 1)
	if width == 32 {
		mask = 0xffffffff
	}
	value := init
	for _, byt := range data {
		b := uint32(byt)
		if reflect {
			b = reverseBits(b, 8)
		}
		value ^= b << (width - 8)
		for i := 0; i < 8; i++ {
			if value&top != 0 {
				value = (value << 1) ^ poly
			} else {
				value <<= 1
			}
			value &= mask
		}
	}
	if reflect {
		value = reverseBits(value, width)
	}
	return value ^ xorOut
}

func reverseBits(value uint32, width int) uint32 {
	var out uint32
	for i := 0; i < width; i++ {
		out = out<<1 | (value>>i)&1
	}
	return out
}

func bigEndian(value uint32, size int) []byte {
	out := make([]byte, size)
	for i := range out {
		out[i] = byte(value >> (8 * (size - 1 - i)))
	}
	return out
}

func littleEndian(value uint32, size int) []byte {
	out := bigEndian(value, size)
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out
}
