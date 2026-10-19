//go:build linux

package main

import (
	"bytes"
	"context"
	"math/rand"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// The engine against the reference programs of the lrzsz package (sz and rz). They are used when LRZSZ
// names the folder that holds lsz and lrz (or sz and rz); otherwise these tests are skipped.
func lrzsz(t *testing.T, name string) string {
	t.Helper()
	dir := os.Getenv("LRZSZ")
	if dir == "" {
		t.Skip("set LRZSZ to the folder with lsz and lrz to test against the reference programs")
	}
	for _, candidate := range []string{name, "l" + name} {
		if path := filepath.Join(dir, candidate); fileExists(path) {
			return path
		}
	}
	t.Skip(name + " is not in " + dir)
	return ""
}

func fileExists(path string) bool { _, err := os.Stat(path); return err == nil }

// masterLink is the pty master as an XMODEM link; a program runs on the slave side.
type masterLink struct {
	f  *os.File
	in chan byte
}

func newMasterLink(f *os.File) *masterLink {
	l := &masterLink{f: f, in: make(chan byte, 1<<16)}
	go func() {
		buf := make([]byte, 1024)
		for {
			n, err := f.Read(buf)
			for _, b := range buf[:n] {
				l.in <- b
			}
			if err != nil {
				return
			}
		}
	}()
	return l
}

func (l *masterLink) write(data []byte) error { _, err := l.f.Write(data); return err }
func (l *masterLink) read(ctx context.Context, timeout time.Duration) (byte, error) {
	select {
	case b := <-l.in:
		return b, nil
	case <-time.After(timeout):
		return 0, errXTimeout
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

// runOnPty starts a program with the slave side of a pty as its terminal and gives back the master link.
func runOnPty(t *testing.T, dir string, name string, args ...string) (*masterLink, func() error) {
	t.Helper()
	master, slavePath := newPty(t)
	slave, err := os.OpenFile(slavePath, os.O_RDWR|unix.O_NOCTTY, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { slave.Close() })
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	cmd.Stdin, cmd.Stdout = slave, slave
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cmd.Process.Kill(); cmd.Wait() })
	return newMasterLink(master), cmd.Wait
}

func TestAFileFromTheReferenceSenderIsReceived(t *testing.T) {
	sz := lrzsz(t, "sz")
	for _, kind := range []struct {
		name string
		args []string
	}{{"128", []string{"-X"}}, {"1K", []string{"-X", "-k"}}} {
		dir := t.TempDir()
		data := make([]byte, 2500)
		rand.New(rand.NewSource(3)).Read(data)
		os.WriteFile(filepath.Join(dir, "in.bin"), data, 0o644)
		link, wait := runOnPty(t, dir, sz, append(kind.args, "in.bin")...)
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		got, err := xmodemReceive(ctx, link, xmodemTimes{block: 5 * time.Second, invite: time.Second}, func(int) {})
		cancel()
		if err != nil {
			t.Fatalf("%s blocks: %v", kind.name, err)
		}
		wait()
		if !bytes.HasPrefix(got, data) {
			t.Errorf("%s blocks: received %d bytes that do not start with the file", kind.name, len(got))
		}
	}
}

func TestAFileSentIsReceivedByTheReferenceReceiver(t *testing.T) {
	rz := lrzsz(t, "rz")
	for _, oneK := range []bool{false, true} {
		dir := t.TempDir()
		data := make([]byte, 3000)
		rand.New(rand.NewSource(4)).Read(data)
		link, wait := runOnPty(t, dir, rz, "-X", "--overwrite", "out.bin")
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		err := xmodemSend(ctx, link, data, oneK, xmodemTimes{start: 10 * time.Second, block: 5 * time.Second}, func(int) {})
		cancel()
		if err != nil {
			t.Fatalf("1K %v: %v", oneK, err)
		}
		wait()
		got, _ := os.ReadFile(filepath.Join(dir, "out.bin"))
		if !bytes.HasPrefix(got, data) {
			t.Errorf("1K %v: the reference receiver saved %d bytes that do not start with the file", oneK, len(got))
		}
	}
}

func TestAFileFromTheReferenceYmodemSenderKeepsItsNameAndSize(t *testing.T) {
	sz := lrzsz(t, "sz")
	dir := t.TempDir()
	data := make([]byte, 3333)
	rand.New(rand.NewSource(5)).Read(data)
	os.WriteFile(filepath.Join(dir, "photo.jpg"), data, 0o644)
	link, wait := runOnPty(t, dir, sz, "--ymodem", "photo.jpg")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	name, got, err := ymodemReceive(ctx, link, xmodemTimes{block: 5 * time.Second, invite: time.Second}, func(int) {})
	cancel()
	if err != nil {
		t.Fatal(err)
	}
	wait()
	if name != "photo.jpg" || !bytes.Equal(got, data) {
		t.Errorf("name %q, %d bytes instead of %d", name, len(got), len(data))
	}
}

func TestAFileSentWithYmodemIsSavedByTheReferenceReceiver(t *testing.T) {
	rz := lrzsz(t, "rz")
	dir := t.TempDir()
	data := make([]byte, 4100)
	rand.New(rand.NewSource(6)).Read(data)
	link, wait := runOnPty(t, dir, rz, "--ymodem", "--overwrite")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	err := ymodemSend(ctx, link, "sent.bin", data, xmodemTimes{start: 10 * time.Second, block: 5 * time.Second}, func(int) {})
	cancel()
	if err != nil {
		t.Fatal(err)
	}
	wait()
	got, _ := os.ReadFile(filepath.Join(dir, "sent.bin"))
	if !bytes.Equal(got, data) {
		t.Errorf("the reference receiver saved %d bytes instead of %d", len(got), len(data))
	}
}
