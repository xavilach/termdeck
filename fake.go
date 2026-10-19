package main

import (
	"errors"
	"fmt"
	"io"
	"strings"
	"sync"
	"time"

	"termdeck/internal/serial"
)

// Fake ports are devices inside termdeck, for trying the program without hardware. Their name is
// "fake:" and a kind. The baud rate and the other line settings do not matter.
type fakeKind struct {
	name, what string
	setup      func(f *fakePort) error // gives the port its behaviour
}

var fakeKinds = []fakeKind{
	{"echo", "sends back everything it receives", func(f *fakePort) error {
		f.onWrite = func(p *fakePort, data []byte) { p.feed(data) }
		return nil
	}},
	{"shell", "a small device with a login and commands (type help); login admin, password admin", func(f *fakePort) error {
		f.onWrite = newFakeShell(f).receive
		return nil
	}},
	{"counter", "sends a line with a counter and a temperature twice a second", func(f *fakePort) error {
		go f.every(500*time.Millisecond, func(n int) []byte {
			return []byte(fmt.Sprintf("tick %d temp=%.1f\r\n", n, 20+float64(n%40)/4))
		})
		return nil
	}},
	{"binary", "sends a binary frame (AA 55 length counter checksum) every second", func(f *fakePort) error {
		go f.every(time.Second, binaryFrame)
		return nil
	}},
	{"flaky", "drops a moment after it opens and is gone for 2 seconds, then comes back: to try Reconnect", setupFlaky},
	{"link-a", "one end of a null-modem cable: what is written here comes out of fake:link-b", setupLink},
	{"link-b", "the other end of the cable", setupLink},
}

const fakePrefix = "fake:"

func isFake(device string) bool { return strings.HasPrefix(strings.ToLower(device), fakePrefix) }

// virtualPort describes a fake port for the page.
type virtualPort struct {
	Name string `json:"name"`
	What string `json:"what"`
}

func virtualPorts() []virtualPort {
	out := make([]virtualPort, len(fakeKinds))
	for i, k := range fakeKinds {
		out[i] = virtualPort{fakePrefix + k.name, k.what}
	}
	return out
}

var (
	fakeMu    sync.Mutex
	fakeLinks = map[string]*fakePort{} // the open ends of the cable, by kind
)

// fakePort is a serial.Port that talks to a program instead of a wire.
type fakePort struct {
	kind string
	stop chan struct{}

	mu       sync.Mutex
	cond     *sync.Cond
	rx       []byte // what the device has sent and the page has not read yet
	closed   bool
	dtr, rts bool
	onWrite  func(p *fakePort, data []byte) // how the device reacts to what it receives
}

// openFake opens the fake port called device.
func openFake(device string, dtr, rts bool) (serial.Port, error) {
	kind := strings.TrimPrefix(strings.ToLower(device), fakePrefix)
	for _, k := range fakeKinds {
		if k.name != kind {
			continue
		}
		f := &fakePort{kind: kind, stop: make(chan struct{}), dtr: dtr, rts: rts}
		f.cond = sync.NewCond(&f.mu)
		if err := k.setup(f); err != nil {
			return nil, fmt.Errorf("cannot open %s: %w", device, err)
		}
		return f, nil
	}
	names := make([]string, len(fakeKinds))
	for i, k := range fakeKinds {
		names[i] = fakePrefix + k.name
	}
	return nil, fmt.Errorf("cannot open %s: no such fake port (%s)", device, strings.Join(names, ", "))
}

// binaryFrame is the n-th frame of fake:binary: AA 55, the length, a payload with the counter, the sum of all before.
func binaryFrame(n int) []byte {
	payload := []byte{byte(n >> 8), byte(n), 0x10, 0x20, 0x30}
	frame := append([]byte{0xAA, 0x55, byte(len(payload))}, payload...)
	sum := byte(0)
	for _, b := range frame {
		sum += b
	}
	return append(frame, sum)
}

var flakyBack time.Time // the flaky port is there again after this time

// setupFlaky makes a port that is pulled out 400 ms after it opens, and plugged in again 2 s later.
func setupFlaky(f *fakePort) error {
	fakeMu.Lock()
	defer fakeMu.Unlock()
	if time.Now().Before(flakyBack) {
		return errors.New("no such device")
	}
	go func() {
		select {
		case <-f.stop:
			return
		case <-time.After(400 * time.Millisecond):
		}
		fakeMu.Lock()
		flakyBack = time.Now().Add(2 * time.Second)
		fakeMu.Unlock()
		f.Close()
	}()
	f.onWrite = func(p *fakePort, data []byte) { p.feed(data) }
	return nil
}

// setupLink makes the port one end of the cable; each end can be open once.
func setupLink(f *fakePort) error {
	fakeMu.Lock()
	defer fakeMu.Unlock()
	if fakeLinks[f.kind] != nil {
		return errors.New("already open")
	}
	fakeLinks[f.kind] = f
	f.onWrite = func(p *fakePort, data []byte) {
		if peer := p.peer(); peer != nil {
			peer.feed(data)
		}
	}
	return nil
}

// peer is the other end of the cable, if this is one and the other end is open.
func (f *fakePort) peer() *fakePort {
	other := map[string]string{"link-a": "link-b", "link-b": "link-a"}[f.kind]
	if other == "" {
		return nil
	}
	fakeMu.Lock()
	defer fakeMu.Unlock()
	return fakeLinks[other]
}

// every sends make(n) for n = 1, 2, ... at each interval, until the port closes.
func (f *fakePort) every(interval time.Duration, make func(n int) []byte) {
	tick := time.NewTicker(interval)
	defer tick.Stop()
	for n := 1; ; n++ {
		select {
		case <-f.stop:
			return
		case <-tick.C:
			f.feed(make(n))
		}
	}
}

// feed gives the page data from the device.
func (f *fakePort) feed(data []byte) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.closed {
		return
	}
	f.rx = append(f.rx, data...)
	f.cond.Broadcast()
}

func (f *fakePort) Read(p []byte) (int, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for len(f.rx) == 0 && !f.closed {
		f.cond.Wait()
	}
	if f.closed {
		return 0, io.EOF
	}
	n := copy(p, f.rx)
	f.rx = f.rx[n:]
	return n, nil
}

func (f *fakePort) Write(p []byte) (int, error) {
	f.mu.Lock()
	closed := f.closed
	f.mu.Unlock()
	if closed {
		return 0, io.ErrClosedPipe
	}
	if f.onWrite != nil {
		f.onWrite(f, append([]byte(nil), p...))
	}
	return len(p), nil
}

func (f *fakePort) Close() error {
	f.mu.Lock()
	if f.closed {
		f.mu.Unlock()
		return nil
	}
	f.closed = true
	close(f.stop)
	f.cond.Broadcast()
	f.mu.Unlock()
	fakeMu.Lock()
	if fakeLinks[f.kind] == f {
		delete(fakeLinks, f.kind)
	}
	fakeMu.Unlock()
	return nil
}

func (f *fakePort) SetDTR(on bool) error { f.mu.Lock(); f.dtr = on; f.mu.Unlock(); return nil }
func (f *fakePort) SetRTS(on bool) error { f.mu.Lock(); f.rts = on; f.mu.Unlock(); return nil }

// GetModemStatusBits: a link is wired like a null-modem cable (DTR to DSR and DCD, RTS to CTS), the
// other fake ports are looped back on themselves.
func (f *fakePort) GetModemStatusBits() (*serial.ModemStatusBits, error) {
	source := f
	if f.kind == "link-a" || f.kind == "link-b" {
		source = f.peer()
	}
	if source == nil {
		return &serial.ModemStatusBits{}, nil
	}
	source.mu.Lock()
	defer source.mu.Unlock()
	return &serial.ModemStatusBits{CTS: source.rts, DSR: source.dtr, DCD: source.dtr}, nil
}

func (f *fakePort) SetMode(*serial.Mode) error         { return nil }
func (f *fakePort) Drain() error                       { return nil }
func (f *fakePort) ResetInputBuffer() error            { f.mu.Lock(); f.rx = nil; f.mu.Unlock(); return nil }
func (f *fakePort) ResetOutputBuffer() error           { return nil }
func (f *fakePort) SetReadTimeout(time.Duration) error { return nil }
func (f *fakePort) Break(time.Duration) error          { f.feed([]byte("<break>\r\n")); return nil }
