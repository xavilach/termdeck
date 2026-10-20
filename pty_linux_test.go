//go:build linux

package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"golang.org/x/sys/unix"
)

// The tests here need a pty: they read the settings of the port from its other side, or use two cables.

func init() { newPtyLine = func(t *testing.T) (line, string) { return newPty(t) } }

// newPty returns the master side and the name of the slave side, which stands in for a serial port.
func newPty(t *testing.T) (*os.File, string) {
	t.Helper()
	master, err := os.OpenFile("/dev/ptmx", os.O_RDWR|unix.O_NOCTTY, 0)
	if err != nil {
		t.Skip("no pty:", err)
	}
	t.Cleanup(func() { master.Close() })
	fd := int(master.Fd())
	if err := unix.IoctlSetPointerInt(fd, unix.TIOCSPTLCK, 0); err != nil {
		t.Fatal(err)
	}
	n, err := unix.IoctlGetInt(fd, unix.TIOCGPTN)
	if err != nil {
		t.Fatal(err)
	}
	return master, "/dev/pts/" + strconv.Itoa(n)
}

func TestTerminalCarriesDataBothWaysAndAppliesFlowControl(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	open, _ := json.Marshal(map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "baud": 9600, "flow": "rtscts"}})
	c.Write(ctx, websocket.MessageText, open)
	if state := readState(t, c, ctx); state != "connected:" {
		t.Fatalf("state %q", state)
	}
	settings, err := unix.IoctlGetTermios(int(master.Fd()), unix.TCGETS)
	if err != nil || settings.Cflag&unix.CRTSCTS == 0 {
		t.Errorf("RTS/CTS was not enabled: %v %#x", err, settings.Cflag)
	}

	master.Write([]byte("hello"))
	data, err := readBinary(c, ctx)
	if err != nil || string(data) != "hello" {
		t.Fatalf("device to page: %q %v", data, err)
	}
	c.Write(ctx, websocket.MessageText, []byte(`{"type":"send","format":"text","end":"none","data":"AT\r"}`))
	buf := make([]byte, 16)
	master.SetReadDeadline(time.Now().Add(5 * time.Second))
	n, err := master.Read(buf)
	if err != nil || string(buf[:n]) != "AT\r" {
		t.Fatalf("page to device: %q %v", buf[:n], err)
	}
}

func TestXonXoffIsApplied(t *testing.T) {
	master, slave := newPty(t)
	port, err := openPort(Profile{Device: slave, Baud: 9600, DataBits: 8, Parity: "none", StopBits: "1", Flow: "xonxoff"})
	if err != nil {
		t.Fatal(err)
	}
	defer port.Close()
	settings, err := unix.IoctlGetTermios(int(master.Fd()), unix.TCGETS)
	if err != nil {
		t.Fatal(err)
	}
	if settings.Iflag&unix.IXON == 0 || settings.Iflag&unix.IXOFF == 0 || settings.Cflag&unix.CRTSCTS != 0 {
		t.Errorf("unexpected flags: iflag %#x cflag %#x", settings.Iflag, settings.Cflag)
	}
}

func TestATunnelCarriesDataBothWaysAndShowsIt(t *testing.T) {
	ts := newTestServer(t)
	masterA, slaveA := newPty(t)
	masterB, slaveB := newPty(t)
	logFile := filepath.Join(t.TempDir(), "tunnel.log")
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{
		"name": "tun", "device": slaveA, "logPath": logFile, "peer": map[string]any{"device": slaveB, "baud": 9600}}})
	waitFor(t, c, ctx, "state", "connected")

	masterA.Write([]byte("from A\n"))
	if got := readDevice(t, masterB, 7); string(got) != "from A\n" {
		t.Fatalf("B got %q", got)
	}
	masterB.Write([]byte("from B\n"))
	if got := readDevice(t, masterA, 7); string(got) != "from B\n" {
		t.Fatalf("A got %q", got)
	}
	var screen string
	for !strings.Contains(screen, "B→A \x1b[0mfrom B") {
		text, err := readBinary(c, ctx)
		if err != nil {
			t.Fatalf("the page saw %q: %v", screen, err)
		}
		screen += string(text)
	}
	if !strings.Contains(screen, "A→B \x1b[0mfrom A\n") {
		t.Errorf("the page saw %q", screen)
	}
	for {
		if m := waitFor(t, c, ctx, "counters", ""); m["rx"] == float64(7) && m["tx"] == float64(7) {
			break
		}
	}
	sendJSON(t, c, ctx, map[string]any{"type": "close"})
	waitFor(t, c, ctx, "state", "closed")
	log, _ := os.ReadFile(logFile)
	if !strings.Contains(string(log), "A>B from A") || !strings.Contains(string(log), "B>A from B") {
		t.Errorf("log %q", log)
	}
	// both ports are free again
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t2", "device": slaveB}})
	waitFor(t, c, ctx, "state", "connected")
}

func TestAMonitorListensToBothPortsAndCarriesNothing(t *testing.T) {
	ts := newTestServer(t)
	masterA, slaveA := newPty(t)
	masterB, slaveB := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "mon", "device": slaveA, "monitor": true, "peer": map[string]any{"device": slaveB}}})
	waitFor(t, c, ctx, "state", "connected")
	masterA.Write([]byte("from A\n"))
	masterB.Write([]byte("from B\n"))
	var screen string
	for !strings.Contains(screen, "from A") || !strings.Contains(screen, "from B") {
		text, err := readBinary(c, ctx)
		if err != nil {
			t.Fatalf("screen %q: %v", screen, err)
		}
		screen += string(text)
	}
	got := make(chan int, 4)
	go func() {
		buf := make([]byte, 64)
		n, _ := masterB.Read(buf)
		got <- n
	}()
	select {
	case n := <-got:
		t.Errorf("%d bytes were carried to B", n)
	case <-time.After(300 * time.Millisecond):
	}
}

func TestABaudStepChangesTheSpeedOfTheOpenPort(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "baud": 115200}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "cmd", "data": "baud=9600"})
	waitNote(t, c, ctx, "baud 9600")
	settings, err := unix.IoctlGetTermios(int(master.Fd()), unix.TCGETS)
	if err != nil || settings.Cflag&unix.CBAUD != unix.B9600 {
		t.Errorf("speed %v %#x", err, settings.Cflag&unix.CBAUD)
	}
}

func TestDsrDtrFlowControlIsRefusedWhereItIsNotAvailable(t *testing.T) {
	ts := newTestServer(t)
	_, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "flow": "dsrdtr"}})
	if state := readState(t, c, ctx); !strings.Contains(state, "only available on Windows") {
		t.Errorf("state %q", state)
	}
}
