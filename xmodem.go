package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"time"
)

// XMODEM file transfer (with CRC-16, 128 or 1024 byte blocks) over a port that something else reads:
// the engine only needs to write bytes and to read one byte at a time with a timeout.

const (
	xSOH = 0x01
	xSTX = 0x02
	xEOT = 0x04
	xACK = 0x06
	xNAK = 0x15
	xCAN = 0x18
	xPAD = 0x1a
	xCRC = 'C'
)

// xmodemLink is what the engine talks through.
type xmodemLink interface {
	write(data []byte) error
	// read returns the next byte, or errXTimeout when none comes in time.
	read(ctx context.Context, timeout time.Duration) (byte, error)
}

var errXTimeout = errors.New("timeout")

// xmodemTimes are the waits of the protocol; the tests make them short.
type xmodemTimes struct {
	start  time.Duration // how long a sender waits for the receiver to ask
	block  time.Duration // how long to wait for the answer to a block, or for the next block
	invite time.Duration // how often a receiver asks for the first block
}

// xmodemTimesInUse is what file transfers use; the tests make it shorter.
var xmodemTimesInUse = xmodemTimes{start: 60 * time.Second, block: 10 * time.Second, invite: 3 * time.Second}

const xmodemRetries = 10

// crc16xmodem is CRC-16/XMODEM (polynomial 0x1021, initial value 0).
func crc16xmodem(data []byte) uint16 {
	var crc uint16
	for _, b := range data {
		crc ^= uint16(b) << 8
		for i := 0; i < 8; i++ {
			if crc&0x8000 != 0 {
				crc = crc<<1 ^ 0x1021
			} else {
				crc <<= 1
			}
		}
	}
	return crc
}

func xmodemBlock(number int, data []byte, oneK bool, crcMode bool) []byte {
	size, head := 128, byte(xSOH)
	if oneK {
		size, head = 1024, xSTX
	}
	payload := make([]byte, size)
	for i := range payload {
		payload[i] = xPAD
	}
	copy(payload, data)
	out := []byte{head, byte(number), ^byte(number)}
	out = append(out, payload...)
	if crcMode {
		crc := crc16xmodem(payload)
		return append(out, byte(crc>>8), byte(crc))
	}
	var sum byte
	for _, b := range payload {
		sum += b
	}
	return append(out, sum)
}

// xmodemSend sends data. progress is told how many bytes of data were accepted.
func xmodemSend(ctx context.Context, link xmodemLink, data []byte, oneK bool, times xmodemTimes, progress func(done int)) error {
	// the receiver asks: 'C' for CRC, NAK for the old checksum
	var crcMode bool
	for start := time.Now(); ; {
		b, err := link.read(ctx, time.Second)
		if err == nil && (b == xCRC || b == xNAK) {
			crcMode = b == xCRC
			break
		}
		if err != nil && !errors.Is(err, errXTimeout) {
			return err
		}
		if time.Since(start) > times.start {
			return errors.New("the device did not ask for the file (no C or NAK)")
		}
	}
	size := 128
	if oneK {
		size = 1024
	}
	number := 1
	for sent := 0; sent < len(data); {
		end := min(sent+size, len(data))
		block := xmodemBlock(number, data[sent:end], oneK, crcMode)
		if err := sendAndWait(ctx, link, block, times); err != nil {
			return fmt.Errorf("block %d: %w", number, err)
		}
		sent = end
		number++
		progress(sent)
	}
	if err := sendAndWait(ctx, link, []byte{xEOT}, times); err != nil {
		return fmt.Errorf("end of file: %w", err)
	}
	return nil
}

// sendAndWait sends a block until the other end acknowledges it.
func sendAndWait(ctx context.Context, link xmodemLink, block []byte, times xmodemTimes) error {
	for try := 0; try < xmodemRetries; try++ {
		if err := link.write(block); err != nil {
			return err
		}
		for {
			b, err := link.read(ctx, times.block)
			if errors.Is(err, errXTimeout) {
				break // send it again
			}
			if err != nil {
				return err
			}
			switch b {
			case xACK:
				return nil
			case xCAN:
				return errors.New("the device cancelled")
			case xNAK:
			default:
				continue // noise between the answers
			}
			break
		}
	}
	return errors.New("too many retries")
}

// xmodemReceive receives a file. The data keeps the padding (1A) of its last block.
func xmodemReceive(ctx context.Context, link xmodemLink, times xmodemTimes, progress func(done int)) ([]byte, error) {
	var file []byte
	expected := 1
	request := byte(xCRC)
	var lastInvite time.Time
	for invites := 0; ; {
		waiting := len(file) == 0 && expected == 1             // still asking for the first block
		if waiting && time.Since(lastInvite) >= times.invite { // by the clock: an echoing line must not use the asks up
			if invites >= xmodemRetries {
				return nil, errors.New("the device did not start sending")
			}
			if invites == 6 {
				request = xNAK // an old sender without CRC
			}
			if err := link.write([]byte{request}); err != nil {
				return nil, err
			}
			invites++
			lastInvite = time.Now()
		}
		wait := times.block
		if waiting {
			wait = max(times.invite-time.Since(lastInvite), time.Millisecond)
		}
		b, err := link.read(ctx, wait)
		if errors.Is(err, errXTimeout) {
			if !waiting {
				if err := link.write([]byte{xNAK}); err != nil {
					return nil, err
				}
			}
			continue
		}
		if err != nil {
			return nil, err
		}
		switch b {
		case xEOT:
			link.write([]byte{xACK})
			return file, nil
		case xCAN:
			return nil, errors.New("the device cancelled")
		case xSOH, xSTX:
			data, number, err := readXmodemBlock(ctx, link, b, request == xCRC, times)
			switch {
			case err != nil:
				link.write([]byte{xNAK})
			case number == (expected-1)&0xff: // the sender did not hear our ACK: it sends the block again
				link.write([]byte{xACK})
			case number == expected&0xff:
				file = append(file, data...)
				expected++
				progress(len(file))
				link.write([]byte{xACK})
			default:
				link.write([]byte{xCAN, xCAN})
				return nil, fmt.Errorf("block %d came instead of %d", number, expected&0xff)
			}
		}
	}
}

// readXmodemBlock reads the rest of a block after its first byte. A bad block gives an error.
func readXmodemBlock(ctx context.Context, link xmodemLink, head byte, crcMode bool, times xmodemTimes) ([]byte, int, error) {
	size, check := 128, 1
	if head == xSTX {
		size = 1024
	}
	if crcMode {
		check = 2
	}
	raw := make([]byte, 0, 2+size+check)
	for len(raw) < 2+size+check {
		b, err := link.read(ctx, times.block)
		if err != nil {
			return nil, 0, err
		}
		raw = append(raw, b)
	}
	number, inverse, data, tail := raw[0], raw[1], raw[2:2+size], raw[2+size:]
	if number != ^inverse {
		return nil, 0, errors.New("the block number is damaged")
	}
	if crcMode {
		if crc := crc16xmodem(data); tail[0] != byte(crc>>8) || tail[1] != byte(crc) {
			return nil, 0, errors.New("bad CRC")
		}
	} else {
		var sum byte
		for _, b := range data {
			sum += b
		}
		if sum != tail[0] {
			return nil, 0, errors.New("bad checksum")
		}
	}
	return bytes.Clone(data), int(number), nil
}
