package main

import "time"

// The stream of a session: what was received and sent, and the notes about it, stamped once when it
// happened and kept (the last 8 MB) so that a tab can show it again with another view, or after a reload.

const (
	srcRx   = "rx"   // received from the port
	srcTx   = "tx"   // sent to the port
	srcA2B  = "a2b"  // a tunnel: received on the first port, sent to the second
	srcB2A  = "b2a"  // and the other way
	srcNote = "note" // a message of termdeck: connected, a mark, a script that ended...
)

// record is one piece of the stream.
type record struct {
	at   time.Time
	src  string
	raw  []byte // the bytes as they went through the port
	text []byte // received: the text in UTF-8; sent: the text as typed (control characters escaped); a note: the message
}

func (r record) size() int { return len(r.raw) + len(r.text) + 48 }

const keepBytes = 8 << 20

// history keeps the most recent records, up to keepBytes.
type history struct {
	records []record
	bytes   int
}

func (h *history) add(r record) {
	h.records = append(h.records, r)
	h.bytes += r.size()
	if h.bytes > keepBytes*5/4 { // drop the oldest in one go, not one at a time
		drop := 0
		for h.bytes > keepBytes && drop < len(h.records)-1 {
			h.bytes -= h.records[drop].size()
			drop++
		}
		h.records = append([]record(nil), h.records[drop:]...)
	}
}

func (h *history) clear() { h.records, h.bytes = nil, 0 }

// raw is what the device sent, as the bytes it sent.
func (h *history) raw() []byte {
	var out []byte
	for _, r := range h.records {
		if r.src == srcRx || r.src == srcA2B || r.src == srcB2A {
			out = append(out, r.raw...)
		}
	}
	return out
}
