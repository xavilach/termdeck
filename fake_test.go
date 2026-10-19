package main

import (
	"strings"
	"testing"
	"time"

	"termdeck/internal/serial"
)

// readUntil reads from the port until the text has appeared.
func readUntil(t *testing.T, port serial.Port, want string) string {
	t.Helper()
	got := make(chan string, 1)
	go func() {
		var all []byte
		buf := make([]byte, 256)
		for !strings.Contains(string(all), want) {
			n, err := port.Read(buf)
			if err != nil {
				break
			}
			all = append(all, buf[:n]...)
		}
		got <- string(all)
	}()
	select {
	case text := <-got:
		return text
	case <-time.After(3 * time.Second):
		t.Fatalf("timeout waiting for %q", want)
		return ""
	}
}

func openFakeFor(t *testing.T, device string) serial.Port {
	t.Helper()
	port, err := openPort(Profile{Device: device})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { port.Close() })
	return port
}

func TestFakePortsAreListedApartAndUnknownOnesFail(t *testing.T) {
	ports, _ := listPorts()
	if strings.Contains(strings.Join(ports, " "), "fake:") {
		t.Errorf("fake ports are among the real ones: %v", ports)
	}
	found := false
	for _, v := range virtualPorts() {
		found = found || v.Name == "fake:shell" && v.What != ""
	}
	if !found {
		t.Error("fake:shell is not in the virtual ports")
	}
	if _, err := openPort(Profile{Device: "fake:nothing"}); err == nil {
		t.Error("an unknown fake port opened")
	}
}

func TestFakeEchoSendsBackAndClosesCleanly(t *testing.T) {
	port := openFakeFor(t, "fake:echo")
	port.Write([]byte("hello"))
	readUntil(t, port, "hello")
	done := make(chan error, 1)
	go func() { _, err := port.Read(make([]byte, 1)); done <- err }()
	time.Sleep(20 * time.Millisecond)
	port.Close()
	select {
	case err := <-done:
		if err == nil {
			t.Error("a blocked Read must fail when the port closes")
		}
	case <-time.After(time.Second):
		t.Error("Close did not wake the reader")
	}
}

func TestFakeShellLoginAndCommands(t *testing.T) {
	port := openFakeFor(t, "fake:shell")
	readUntil(t, port, "login: ")
	port.Write([]byte("admin\rbad\r"))
	readUntil(t, port, "Login incorrect")
	port.Write([]byte("admin\r"))
	readUntil(t, port, "Password: ")
	port.Write([]byte("admin\r\n"))
	text := readUntil(t, port, "admin# ")
	if strings.Contains(text, "admin\r\nadmin") {
		t.Errorf("the password was echoed: %q", text)
	}
	port.Write([]byte("echo hi there\r"))
	readUntil(t, port, "hi there\r\nadmin# ")
	port.Write([]byte("flood 3\r"))
	readUntil(t, port, "line 3 of 3")
	port.Write([]byte("nosuch\r"))
	readUntil(t, port, "nosuch: command not found")
}

func TestFakeLinkConnectsTwoPortsLikeANullModemCable(t *testing.T) {
	a := openFakeFor(t, "fake:link-a")
	b := openFakeFor(t, "fake:link-b")
	a.Write([]byte("to b"))
	readUntil(t, b, "to b")
	b.Write([]byte("to a"))
	readUntil(t, a, "to a")
	a.SetRTS(true)
	a.SetDTR(false)
	bits, _ := b.GetModemStatusBits()
	if !bits.CTS || bits.DSR {
		t.Errorf("RTS of one end is CTS of the other, DTR is DSR: %+v", bits)
	}
	if _, err := openPort(Profile{Device: "fake:link-a"}); err == nil {
		t.Error("one end of the cable opened twice")
	}
	a.Close()
	if _, err := b.Write([]byte("nobody")); err != nil {
		t.Errorf("writing to a cable with a free end must not fail: %v", err)
	}
	again := openFakeFor(t, "fake:link-a") // the end is free again
	again.Write([]byte("back"))
	readUntil(t, b, "back")
}

func TestFakeCounterAndBinarySendOnTheirOwn(t *testing.T) {
	readUntil(t, openFakeFor(t, "fake:counter"), "tick 1 ")
	port := openFakeFor(t, "fake:binary")
	frame := readUntil(t, port, "\xaa\x55")
	if !strings.HasPrefix(frame, "\xaa\x55") {
		t.Errorf("frame %x", frame)
	}
}

func TestFakeShellCommands(t *testing.T) {
	port := openFakeFor(t, "fake:shell")
	readUntil(t, port, "login: ")
	port.Write([]byte("admin\radmin\r"))
	readUntil(t, port, "admin# ")
	for command, want := range map[string]string{
		"help":    "flood N",
		"version": "fake device",
		"time":    "20",
		"colors":  "\x1b[31mcolour 31",
		"bell":    "\a",
		"bin":     "\xfe\xff",
		"flood x": "a number from 1 to 5000",
		"slow":    "........",
	} {
		port.Write([]byte(command + "\r"))
		readUntil(t, port, want)
	}
	port.Write([]byte("logout\r"))
	readUntil(t, port, "login: ")
	port.Write([]byte("abc\x7f\x7fx\x03"))
	readUntil(t, port, "^C")
}

func TestFakePortMethodsThatDoNothing(t *testing.T) {
	port := openFakeFor(t, "fake:echo")
	port.Write([]byte("zz"))
	if err := port.ResetInputBuffer(); err != nil {
		t.Fatal(err)
	}
	for _, err := range []error{port.SetMode(nil), port.Drain(), port.ResetOutputBuffer(), port.SetReadTimeout(0)} {
		if err != nil {
			t.Error(err)
		}
	}
	port.Break(time.Millisecond)
	readUntil(t, port, "<break>")
}
