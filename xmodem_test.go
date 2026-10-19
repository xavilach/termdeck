package main

import (
	"bytes"
	"context"
	"math/rand"
	"testing"
	"time"
)

// pipeLink is one end of an in-memory cable.
type pipeLink struct {
	in, out chan byte
	corrupt func(written []byte) []byte // changes what is written, for the tests
}

func newPipe() (*pipeLink, *pipeLink) {
	a, b := make(chan byte, 1<<16), make(chan byte, 1<<16)
	return &pipeLink{in: a, out: b}, &pipeLink{in: b, out: a}
}

func (p *pipeLink) write(data []byte) error {
	if p.corrupt != nil {
		data = p.corrupt(data)
	}
	for _, b := range data {
		p.out <- b
	}
	return nil
}

func (p *pipeLink) read(ctx context.Context, timeout time.Duration) (byte, error) {
	select {
	case b := <-p.in:
		return b, nil
	case <-time.After(timeout):
		return 0, errXTimeout
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

var fastTimes = xmodemTimes{start: 400 * time.Millisecond, block: 80 * time.Millisecond, invite: 30 * time.Millisecond}

func transfer(t *testing.T, data []byte, oneK bool, senderSide func(*pipeLink)) ([]byte, error, error) {
	t.Helper()
	tx, rx := newPipe()
	if senderSide != nil {
		senderSide(tx)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	sendErr := make(chan error, 1)
	go func() { sendErr <- xmodemSend(ctx, tx, data, oneK, fastTimes, func(int) {}) }()
	got, recvErr := xmodemReceive(ctx, rx, fastTimes, func(int) {})
	return got, <-sendErr, recvErr
}

func TestXmodemSendsAndReceivesFilesOfAnySize(t *testing.T) {
	rng := rand.New(rand.NewSource(1))
	for _, size := range []int{1, 127, 128, 129, 1024, 3000} {
		for _, oneK := range []bool{false, true} {
			data := make([]byte, size)
			rng.Read(data)
			got, sendErr, recvErr := transfer(t, data, oneK, nil)
			if sendErr != nil || recvErr != nil {
				t.Fatalf("%d bytes, 1K %v: send %v receive %v", size, oneK, sendErr, recvErr)
			}
			if !bytes.HasPrefix(got, data) || len(got)%128 != 0 || len(got)-len(data) >= 1024 || bytes.Count(got[len(data):], []byte{xPAD}) != len(got)-len(data) {
				t.Errorf("%d bytes, 1K %v: received %d bytes that do not hold the data and then padding", size, oneK, len(got))
			}
		}
	}
}

func TestAnEmptyFileIsJustTheEndOfTransmission(t *testing.T) {
	got, sendErr, recvErr := transfer(t, nil, false, nil)
	if sendErr != nil || recvErr != nil || len(got) != 0 {
		t.Errorf("%v %v %d", sendErr, recvErr, len(got))
	}
}

func TestADamagedBlockIsSentAgain(t *testing.T) {
	data := bytes.Repeat([]byte("0123456789"), 40)
	damaged := false
	got, sendErr, recvErr := transfer(t, data, false, func(tx *pipeLink) {
		tx.corrupt = func(w []byte) []byte {
			if !damaged && len(w) == 133 && w[0] == xSOH && w[1] == 2 { // the second block, once
				damaged = true
				w = bytes.Clone(w)
				w[10] ^= 0xff
			}
			return w
		}
	})
	if sendErr != nil || recvErr != nil || !bytes.HasPrefix(got, data) || !damaged {
		t.Errorf("send %v receive %v damaged %v", sendErr, recvErr, damaged)
	}
}

func TestAReceiverThatWantsTheOldChecksumGetsIt(t *testing.T) {
	tx, rx := newPipe()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	data := []byte("hello xmodem")
	done := make(chan error, 1)
	go func() { done <- xmodemSend(ctx, tx, data, false, fastTimes, func(int) {}) }()
	rx.write([]byte{xNAK}) // no CRC
	block := make([]byte, 0, 132)
	for len(block) < 132 { // SOH, number, inverse, 128 bytes, one checksum byte
		b, err := rx.read(ctx, time.Second)
		if err != nil {
			t.Fatal(err)
		}
		block = append(block, b)
	}
	var sum byte
	for _, b := range block[3:131] {
		sum += b
	}
	if block[0] != xSOH || block[131] != sum || !bytes.HasPrefix(block[3:], data) {
		t.Fatalf("block %x", block)
	}
	rx.write([]byte{xACK})
	if b, _ := rx.read(ctx, time.Second); b != xEOT {
		t.Fatalf("expected EOT, got %x", b)
	}
	rx.write([]byte{xACK})
	if err := <-done; err != nil {
		t.Error(err)
	}
}

func TestACancelledOrSilentTransferFails(t *testing.T) {
	tx, rx := newPipe()
	ctx := context.Background()
	rx.write([]byte{xCRC})
	go func() {
		for { // answer the first block with CAN
			if b, err := rx.read(ctx, time.Second); err == nil && b == xSOH {
				rx.write([]byte{xCAN})
				return
			}
		}
	}()
	if err := xmodemSend(ctx, tx, []byte("x"), false, fastTimes, func(int) {}); err == nil {
		t.Error("a cancelled transfer should fail")
	}
	quiet, _ := newPipe()
	if err := xmodemSend(ctx, quiet, []byte("x"), false, xmodemTimes{start: 100 * time.Millisecond, block: 20 * time.Millisecond}, func(int) {}); err == nil {
		t.Error("a receiver that never asks should fail")
	}
}

func TestCRC16XmodemHasItsPublishedCheckValue(t *testing.T) {
	if got := crc16xmodem([]byte("123456789")); got != 0x31c3 {
		t.Errorf("%04x", got)
	}
}

func TestYmodemCarriesTheNameAndTheExactSize(t *testing.T) {
	rng := rand.New(rand.NewSource(2))
	for _, size := range []int{1, 1000, 1024, 1025, 5000} {
		data := make([]byte, size)
		rng.Read(data)
		tx, rx := newPipe()
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		sendErr := make(chan error, 1)
		go func() { sendErr <- ymodemSend(ctx, tx, "firmware.bin", data, fastTimes, func(int) {}) }()
		name, got, err := ymodemReceive(ctx, rx, fastTimes, func(int) {})
		cancel()
		if err != nil || <-sendErr != nil {
			t.Fatalf("%d bytes: %v", size, err)
		}
		if name != "firmware.bin" || !bytes.Equal(got, data) {
			t.Errorf("%d bytes: name %q, received %d bytes", size, name, len(got))
		}
	}
}

func TestYmodemHeaderHoldsNameAndSize(t *testing.T) {
	h := ymodemHeader("a.txt", 123)
	if h[0] != xSOH || h[1] != 0 || h[2] != 0xff || !bytes.HasPrefix(h[3:], []byte("a.txt\x00123")) || len(h) != 133 {
		t.Errorf("header %x", h)
	}
	if end := ymodemHeader("", 0); end[3] != 0 {
		t.Error("the end of the batch has an empty name")
	}
}
