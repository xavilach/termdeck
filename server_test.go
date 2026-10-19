//go:build linux

package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
	"termdeck/internal/serial"
	"testing"
	"time"

	"github.com/coder/websocket"
	"golang.org/x/sys/unix"
)

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
	return master, "/dev/pts/" + itoa(n)
}

func itoa(n int) string { b, _ := json.Marshal(n); return string(b) }

type testServer struct {
	*httptest.Server
	secret string
}

func newTestServer(t *testing.T) *testServer {
	t.Helper()
	dir := t.TempDir()
	s := NewServer(NewStore(filepath.Join(dir, "profiles.json")), NewScriptStore(filepath.Join(dir, "scripts.json")), NewGroupStore(filepath.Join(dir, "groups.json")))
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	return &testServer{Server: ts, secret: s.auth.Login(s.auth.NewLink())}
}

func (ts *testServer) do(t *testing.T, method, path, body, secret string) (int, string) {
	t.Helper()
	req, _ := http.NewRequest(method, ts.URL+path, strings.NewReader(body))
	if secret != "" {
		req.Header.Set(authHeader, secret)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var buf bytes.Buffer
	buf.ReadFrom(resp.Body)
	return resp.StatusCode, buf.String()
}

func TestAPIRequiresTheSecretAndLinksWorkOnce(t *testing.T) {
	ts := newTestServer(t)
	if code, _ := ts.do(t, "GET", "/api/profiles", "", ""); code != 401 {
		t.Errorf("no secret: got %d, want 401", code)
	}
	if code, _ := ts.do(t, "GET", "/api/profiles", "", "wrong"); code != 401 {
		t.Errorf("wrong secret: got %d, want 401", code)
	}
	if code, _ := ts.do(t, "GET", "/api/profiles", "", ts.secret); code != 200 {
		t.Errorf("right secret: got %d, want 200", code)
	}
	if code, _ := ts.do(t, "POST", "/api/login", `{"token":"nope"}`, ""); code != 403 {
		t.Errorf("bad token: got %d, want 403", code)
	}
	if code, body := ts.do(t, "GET", "/", "", ""); code != 200 || !strings.Contains(body, "termdeck") {
		t.Errorf("index: %d", code)
	}
}

func TestRefusesOtherHostsAndOrigins(t *testing.T) {
	ts := newTestServer(t)
	req, _ := http.NewRequest("GET", ts.URL+"/api/profiles", nil)
	req.Header.Set(authHeader, ts.secret)
	req.Header.Set("Origin", "http://evil.example")
	resp, _ := http.DefaultClient.Do(req)
	if resp.StatusCode != 403 {
		t.Errorf("cross-origin: got %d, want 403", resp.StatusCode)
	}
	req, _ = http.NewRequest("GET", ts.URL+"/", nil)
	req.Host = "evil.example"
	resp, _ = http.DefaultClient.Do(req)
	if resp.StatusCode != 403 {
		t.Errorf("foreign Host: got %d, want 403", resp.StatusCode)
	}
}

func TestProfilesOverHTTP(t *testing.T) {
	ts := newTestServer(t)
	if code, body := ts.do(t, "POST", "/api/profiles", `{"name":"board","device":"/dev/ttyUSB0","baud":9600,"flow":"xonxoff"}`, ts.secret); code != 200 {
		t.Fatalf("save: %d %s", code, body)
	}
	if code, _ := ts.do(t, "POST", "/api/profiles", `{"name":"bad","device":""}`, ts.secret); code != 400 {
		t.Errorf("invalid profile: got %d, want 400", code)
	}
	_, body := ts.do(t, "GET", "/api/profiles", "", ts.secret)
	if !strings.Contains(body, `"xonxoff"`) {
		t.Errorf("flow control was not stored: %s", body)
	}
	if code, _ := ts.do(t, "DELETE", "/api/profiles/board", "", ts.secret); code != 200 {
		t.Errorf("delete: %d", code)
	}
}

func dialTerminal(t *testing.T, ts *testServer) (*websocket.Conn, context.Context) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	c, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(ts.URL, "http")+"/api/terminal",
		&websocket.DialOptions{Subprotocols: []string{wsProtocol, "auth." + ts.secret}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.CloseNow() })
	return c, ctx
}

func readState(t *testing.T, c *websocket.Conn, ctx context.Context) string {
	t.Helper()
	for {
		kind, data, err := c.Read(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if kind != websocket.MessageText {
			continue
		}
		var m map[string]string
		json.Unmarshal(data, &m)
		if m["type"] == "state" && m["state"] != "connecting" {
			return m["state"] + ":" + m["message"]
		}
	}
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
	c.Write(ctx, websocket.MessageText, []byte(`{"type":"input","data":"AT\r"}`))
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

func TestOpeningAMissingPortFails(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	c.Write(ctx, websocket.MessageText, []byte(`{"type":"open","profile":{"name":"x","device":"/dev/does-not-exist"}}`))
	if state := readState(t, c, ctx); !strings.HasPrefix(state, "failed:cannot open /dev/does-not-exist") {
		t.Errorf("state %q", state)
	}
}

func sendJSON(t *testing.T, c *websocket.Conn, ctx context.Context, v any) {
	t.Helper()
	data, _ := json.Marshal(v)
	if err := c.Write(ctx, websocket.MessageText, data); err != nil {
		t.Fatal(err)
	}
}

// waitFor reads messages until one has the given type (and state, when not empty).
func waitFor(t *testing.T, c *websocket.Conn, ctx context.Context, kind, state string) map[string]any {
	t.Helper()
	for {
		mt, data, err := c.Read(ctx)
		if err != nil {
			t.Fatalf("waiting for %s/%s: %v", kind, state, err)
		}
		if mt != websocket.MessageText {
			continue
		}
		var m map[string]any
		json.Unmarshal(data, &m)
		if m["type"] == kind && (state == "" || m["state"] == state) {
			return m
		}
	}
}

func TestClosingAndReopeningThePortKeepsTheConnection(t *testing.T) {
	ts := newTestServer(t)
	_, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	profile := map[string]any{"name": "t", "device": slave}
	for range 2 {
		sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": profile})
		waitFor(t, c, ctx, "state", "connected")
		sendJSON(t, c, ctx, map[string]any{"type": "close"})
		waitFor(t, c, ctx, "state", "closed")
	}
}

func TestFailedOpenCreatesNoLogAndOpenWritesIt(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	logFile := filepath.Join(t.TempDir(), "run.log")
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{
		"name": "t", "device": "/dev/does-not-exist", "logPath": logFile}})
	waitFor(t, c, ctx, "state", "failed")
	if _, err := os.Stat(logFile); err == nil {
		t.Fatal("a log was created for a port that did not open")
	}
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{
		"name": "t", "device": slave, "logPath": logFile, "logRxMarker": "RX ", "logTxMarker": "TX "}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "input", "data": "hi\r"})
	master.Write([]byte("yo\r\n"))
	deadline := time.Now().Add(5 * time.Second)
	for {
		data, _ := os.ReadFile(logFile)
		if s := string(data); strings.Contains(s, "TX hi\n") && strings.Contains(s, "RX yo\n") {
			break
		}
		if time.Now().After(deadline) {
			data, _ := os.ReadFile(logFile)
			t.Fatalf("log was %q", data)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestScriptSendsItsCommandsWithADelayAndLoops(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	if code, body := ts.do(t, "POST", "/api/scripts",
		`{"name":"boot","group":"","commands":["one","two"],"delayMs":50,"loop":true,"count":2}`, ts.secret); code != 200 {
		t.Fatalf("save script: %d %s", code, body)
	}
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "enter": "lf"}})
	waitFor(t, c, ctx, "state", "connected")
	start := time.Now()
	sendJSON(t, c, ctx, map[string]any{"type": "script", "name": "boot"})
	waitFor(t, c, ctx, "script", "done")
	if elapsed := time.Since(start); elapsed < 150*time.Millisecond {
		t.Errorf("3 pauses of 50 ms expected, script took %v", elapsed)
	}
	buf := make([]byte, 64)
	master.SetReadDeadline(time.Now().Add(2 * time.Second))
	var got string
	for !strings.HasSuffix(got, "two\n") || strings.Count(got, "one") < 2 {
		n, err := master.Read(buf)
		if err != nil {
			t.Fatalf("read %q: %v", got, err)
		}
		got += string(buf[:n])
	}
	if got != "one\ntwo\none\ntwo\n" {
		t.Errorf("sent %q", got)
	}
}

func TestScriptCanBeStoppedAndNeedsAnOpenPort(t *testing.T) {
	ts := newTestServer(t)
	_, slave := newPty(t)
	ts.do(t, "POST", "/api/scripts", `{"name":"forever","commands":["x"],"delayMs":20,"loop":true}`, ts.secret)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "script", "name": "forever"})
	if m := waitFor(t, c, ctx, "script", "error"); m["message"] != "the port is closed" {
		t.Errorf("got %v", m)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "script", "name": "forever"})
	waitFor(t, c, ctx, "script", "running")
	sendJSON(t, c, ctx, map[string]any{"type": "stop"})
	waitFor(t, c, ctx, "script", "stopped")
}

func TestGroupsCanBeCreatedAndOnlyEmptyOnesDeleted(t *testing.T) {
	ts := newTestServer(t)
	if code, body := ts.do(t, "POST", "/api/groups", `{"name":"  Lab "}`, ts.secret); code != 200 {
		t.Fatalf("create: %d %s", code, body)
	}
	if code, _ := ts.do(t, "POST", "/api/groups", `{"name":"lab"}`, ts.secret); code != 400 {
		t.Errorf("duplicate group: got %d, want 400", code)
	}
	if code, _ := ts.do(t, "POST", "/api/groups", `{"name":""}`, ts.secret); code != 400 {
		t.Errorf("empty name: got %d, want 400", code)
	}
	ts.do(t, "POST", "/api/profiles", `{"name":"b","device":"COM1","group":"Lab"}`, ts.secret)
	if code, _ := ts.do(t, "DELETE", "/api/groups/Lab", "", ts.secret); code != 409 {
		t.Errorf("group in use: got %d, want 409", code)
	}
	ts.do(t, "DELETE", "/api/profiles/b", "", ts.secret)
	if code, _ := ts.do(t, "DELETE", "/api/groups/Lab", "", ts.secret); code != 200 {
		t.Errorf("empty group: got %d, want 200", code)
	}
	if _, body := ts.do(t, "GET", "/api/groups", "", ts.secret); !strings.Contains(body, `"groups":[]`) {
		t.Errorf("groups left: %s", body)
	}
}

func TestPingNeedsTheSecret(t *testing.T) {
	ts := newTestServer(t)
	if code, _ := ts.do(t, "GET", "/api/ping", "", ""); code != 401 {
		t.Errorf("no secret: got %d, want 401", code)
	}
	if code, _ := ts.do(t, "GET", "/api/ping", "", ts.secret); code != 200 {
		t.Errorf("with secret: got %d, want 200", code)
	}
}

func TestDeviceCharsetIsConvertedBothWays(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "charset": "iso-8859-1"}})
	waitFor(t, c, ctx, "state", "connected")
	master.Write([]byte("caf\xe9\r\n")) // é in Latin-1
	var got []byte
	for !strings.Contains(string(got), "café") {
		_, data, err := c.Read(ctx)
		if err != nil {
			t.Fatalf("read %q: %v", got, err)
		}
		got = append(got, data...)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "input", "data": "é€\r"})
	buf := make([]byte, 16)
	master.SetReadDeadline(time.Now().Add(5 * time.Second))
	n, err := master.Read(buf)
	if err != nil || string(buf[:n]) != "\xe9?\r" {
		t.Fatalf("device got %q (%v), want é in Latin-1 and ? for the euro sign", buf[:n], err)
	}
}

func TestExistingLogIsAskedAbout(t *testing.T) {
	ts := newTestServer(t)
	_, slave := newPty(t)
	logFile := filepath.Join(t.TempDir(), "run.log")
	os.WriteFile(logFile, []byte("old line\n"), 0o600)
	c, ctx := dialTerminal(t, ts)
	profile := map[string]any{"name": "t", "device": slave, "logPath": logFile, "logExisting": "ask"}
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": profile})
	if m := waitFor(t, c, ctx, "logexists", ""); m["path"] != logFile {
		t.Errorf("asked about %v", m["path"])
	}
	sendJSON(t, c, ctx, map[string]any{"type": "loganswer", "answer": "overwrite"})
	waitFor(t, c, ctx, "state", "connected")
	if data, _ := os.ReadFile(logFile); strings.Contains(string(data), "old line") {
		t.Errorf("the file should have been replaced: %q", data)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "close"})
	waitFor(t, c, ctx, "state", "closed")
	// answering "none" opens the port without a log, and leaves the file as it is
	os.WriteFile(logFile, []byte("keep me\n"), 0o600)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": profile})
	waitFor(t, c, ctx, "logexists", "")
	sendJSON(t, c, ctx, map[string]any{"type": "loganswer", "answer": "none"})
	waitFor(t, c, ctx, "state", "connected")
	if data, _ := os.ReadFile(logFile); string(data) != "keep me\n" {
		t.Errorf("file changed: %q", data)
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

func TestTheSamePortCannotBeOpenTwice(t *testing.T) {
	ts := newTestServer(t)
	_, slave := newPty(t)
	profile := map[string]any{"name": "t", "device": slave}
	a, actx := dialTerminal(t, ts)
	sendJSON(t, a, actx, map[string]any{"type": "open", "profile": profile})
	waitFor(t, a, actx, "state", "connected")

	b, bctx := dialTerminal(t, ts)
	sendJSON(t, b, bctx, map[string]any{"type": "open", "profile": profile})
	if state := readState(t, b, bctx); !strings.Contains(state, "already open in another tab") {
		t.Fatalf("second open: %q", state)
	}
	// the tab that has it can reopen it (settings reload), and once it lets go the other can take it
	sendJSON(t, a, actx, map[string]any{"type": "open", "profile": profile})
	waitFor(t, a, actx, "state", "connected")
	sendJSON(t, a, actx, map[string]any{"type": "close"})
	waitFor(t, a, actx, "state", "closed")
	sendJSON(t, b, bctx, map[string]any{"type": "open", "profile": profile})
	waitFor(t, b, bctx, "state", "connected")
}

func TestLoggingCanBeStartedAndStoppedWhileConnected(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	logFile := filepath.Join(t.TempDir(), "mid.log")
	c, ctx := dialTerminal(t, ts)
	// no log path: starting gives a notice
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "log", "value": true})
	if m := waitFor(t, c, ctx, "notice", ""); !strings.Contains(m["text"].(string), "set a log file") {
		t.Errorf("notice %v", m["text"])
	}
	// with a path the port opens without logging only when asked to; here it logs from the start, then stops, then starts again
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "logPath": logFile}})
	if m := waitFor(t, c, ctx, "logging", ""); m["on"] != true {
		t.Fatalf("logging %v", m)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "log", "value": false})
	if m := waitFor(t, c, ctx, "logging", ""); m["on"] != false {
		t.Fatalf("logging %v", m)
	}
	master.Write([]byte("not logged\r\n"))
	time.Sleep(200 * time.Millisecond)
	sendJSON(t, c, ctx, map[string]any{"type": "log", "value": true})
	if m := waitFor(t, c, ctx, "logging", ""); m["on"] != true {
		t.Fatalf("logging %v", m)
	}
	master.Write([]byte("logged\r\n"))
	deadline := time.Now().Add(5 * time.Second)
	for {
		data, _ := os.ReadFile(logFile)
		if strings.Contains(string(data), "logged\n") {
			if strings.Contains(string(data), "not logged") {
				t.Fatalf("what came while the log was off was written: %q", data)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("log was %q", data)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// readFrame returns the raw bytes and the text of the next frame of port output, skipping the text messages.
func readFrame(c *websocket.Conn, ctx context.Context) (raw, text []byte, err error) {
	for {
		kind, data, err := c.Read(ctx)
		if err != nil {
			return nil, nil, err
		}
		if kind == websocket.MessageBinary {
			n := int(binary.BigEndian.Uint32(data[8:12]))
			return data[12 : 12+n], data[12+n:], nil
		}
	}
}

// readBinary returns the text of the next frame of port output.
func readBinary(c *websocket.Conn, ctx context.Context) ([]byte, error) {
	_, text, err := readFrame(c, ctx)
	return text, err
}

// Every script, style sheet and file the page names must be served, or the page breaks in a real browser.
func TestEveryFileTheIndexPageUsesIsServed(t *testing.T) {
	ts := newTestServer(t)
	_, page := ts.do(t, "GET", "/", "", "")
	refs := regexp.MustCompile(`(?:src|href)="(/[^"]+)"`).FindAllStringSubmatch(page, -1)
	if len(refs) < 6 {
		t.Fatalf("expected the page to name several files, found %d", len(refs))
	}
	for _, ref := range refs {
		if code, _ := ts.do(t, "GET", ref[1], "", ""); code != 200 {
			t.Errorf("%s: got %d, want 200", ref[1], code)
		}
	}
}

func TestFramesCarryTheRawBytesAndTheTime(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "charset": "iso-8859-1"}})
	waitFor(t, c, ctx, "state", "connected")
	master.Write([]byte{'a', 0xe9})
	raw, text, err := readFrame(c, ctx)
	if err != nil || !bytes.Equal(raw, []byte{'a', 0xe9}) || string(text) != "aé" {
		t.Fatalf("raw %x text %q %v", raw, text, err)
	}
}

func readDevice(t *testing.T, master *os.File, want int) []byte {
	t.Helper()
	var got []byte
	buf := make([]byte, 64)
	master.SetReadDeadline(time.Now().Add(3 * time.Second))
	for len(got) < want {
		n, err := master.Read(buf)
		if err != nil {
			t.Fatalf("read %q: %v", got, err)
		}
		got = append(got, buf[:n]...)
	}
	return got
}

func TestRawBytesGoOutUnconverted(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "charset": "iso-8859-1"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "raw", "bytes": []byte{0x00, 0xe9, 0xff}})
	if got := readDevice(t, master, 3); !bytes.Equal(got, []byte{0x00, 0xe9, 0xff}) {
		t.Errorf("sent %x", got)
	}
}

func TestCountersFollowTheBytes(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave}})
	waitFor(t, c, ctx, "state", "connected")
	master.Write([]byte("12345"))
	sendJSON(t, c, ctx, map[string]any{"type": "raw", "bytes": []byte("ab")})
	for {
		m := waitFor(t, c, ctx, "counters", "")
		if m["rx"] == float64(5) && m["tx"] == float64(2) {
			break
		}
	}
	sendJSON(t, c, ctx, map[string]any{"type": "resetcounters"})
	for {
		if m := waitFor(t, c, ctx, "counters", ""); m["rx"] == float64(0) && m["tx"] == float64(0) {
			return
		}
	}
}

func TestCmdAndScriptStepsDriveLinesWaitAndSendBytes(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	if code, body := ts.do(t, "POST", "/api/scripts",
		`{"name":"s","commands":["@hex=41 42; wait=20","@@at","go"],"delayMs":0}`, ts.secret); code != 200 {
		t.Fatalf("save script: %d %s", code, body)
	}
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "enter": "lf"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "script", "name": "s"})
	waitFor(t, c, ctx, "script", "done")
	if got := readDevice(t, master, len("AB@at\ngo\n")); string(got) != "AB@at\ngo\n" {
		t.Errorf("sent %q", got)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "steps", "data": "dtr=0; rts=0; hex=0d"})
	if got := readDevice(t, master, 1); string(got) != "\r" {
		t.Errorf("sent %q", got)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "steps", "data": "nonsense"})
	waitFor(t, c, ctx, "notice", "")
}

func TestTriggerAnswersWhatTheDeviceSends(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "enter": "lf",
		"triggers": "login: => admin\n/Pass(word)?:/ => @hex=31 32; wait=1"}})
	waitFor(t, c, ctx, "state", "connected")
	master.Write([]byte("lo"))
	time.Sleep(50 * time.Millisecond)
	master.Write([]byte("gin: "))
	if got := readDevice(t, master, 6); string(got) != "admin\n" {
		t.Fatalf("sent %q", got)
	}
	master.Write([]byte("Password:"))
	if got := readDevice(t, master, 2); string(got) != "12" {
		t.Errorf("sent %q", got)
	}
}

func TestOpeningWithACustomBellSoundIsNotCutByTheMessageLimit(t *testing.T) {
	ts := newTestServer(t)
	_, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sound := "data:audio/wav;base64," + strings.Repeat("A", 300_000)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "bellSound": sound}})
	waitFor(t, c, ctx, "state", "connected")
}

func TestCmdNeedsAnOpenPortAndStopsWhenItCloses(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "steps", "data": "hex=41"})
	if m := waitFor(t, c, ctx, "notice", ""); !strings.Contains(m["text"].(string), "open the port") {
		t.Errorf("notice %v", m)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "steps", "data": "wait=300; hex=42"})
	sendJSON(t, c, ctx, map[string]any{"type": "close"})
	waitFor(t, c, ctx, "state", "closed")
	master.SetReadDeadline(time.Now().Add(600 * time.Millisecond))
	if n, _ := master.Read(make([]byte, 4)); n != 0 {
		t.Error("a step ran after the port was closed")
	}
}

// The pure helpers of the page have their own tests, run with Node when it is installed.
func TestWebFormatHelpers(t *testing.T) {
	node, err := exec.LookPath("node")
	if err != nil {
		t.Skip("node is not installed")
	}
	if out, err := exec.Command(node, "--test", "web/format_test.js").CombinedOutput(); err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
}

// The checks that need a DOM (lint and a run of the page in jsdom) run when `npm install` was done in tools/webtest.
func TestWebPageInJsdom(t *testing.T) {
	npm, err := exec.LookPath("npm")
	if err != nil {
		t.Skip("npm is not installed")
	}
	if _, err := os.Stat("tools/webtest/node_modules"); err != nil {
		t.Skip("run npm install in tools/webtest to include this test")
	}
	cmd := exec.Command(npm, "test")
	cmd.Dir = "tools/webtest"
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
}

func TestScriptsAreListedAndDeletedOverHTTP(t *testing.T) {
	ts := newTestServer(t)
	if code, _ := ts.do(t, "POST", "/api/scripts", `{"name":"s","commands":["a"]}`, ts.secret); code != 200 {
		t.Fatal("save")
	}
	if code, _ := ts.do(t, "POST", "/api/scripts", `{"name":"bad","commands":[]}`, ts.secret); code != 400 {
		t.Errorf("a script without commands: got %d, want 400", code)
	}
	if _, body := ts.do(t, "GET", "/api/scripts", "", ts.secret); !strings.Contains(body, `"name":"s"`) {
		t.Errorf("listed: %s", body)
	}
	if code, _ := ts.do(t, "DELETE", "/api/scripts/s", "", ts.secret); code != 200 {
		t.Error("delete")
	}
	if code, _ := ts.do(t, "DELETE", "/api/scripts/s", "", ts.secret); code != 404 {
		t.Errorf("delete twice: got %d, want 404", code)
	}
	if code, _ := ts.do(t, "DELETE", "/api/profiles/none", "", ts.secret); code != 404 {
		t.Errorf("delete a missing profile: got %d, want 404", code)
	}
	if code, _ := ts.do(t, "POST", "/api/profiles", `{not json`, ts.secret); code != 400 {
		t.Errorf("broken JSON: got %d, want 400", code)
	}
}

func TestTheVersionIsAvailableToTheLoggedInPage(t *testing.T) {
	ts := newTestServer(t)
	if code, body := ts.do(t, "GET", "/api/info", "", ts.secret); code != 200 || !strings.Contains(body, `"version"`) {
		t.Errorf("%d %s", code, body)
	}
	if code, _ := ts.do(t, "GET", "/api/info", "", ""); code != 401 {
		t.Errorf("without the login: got %d", code)
	}
}

func TestVirtualPortsAreListedApartOverHTTP(t *testing.T) {
	ts := newTestServer(t)
	_, virtual := ts.do(t, "GET", "/api/virtual", "", ts.secret)
	_, real := ts.do(t, "GET", "/api/ports", "", ts.secret)
	if !strings.Contains(virtual, "fake:shell") || strings.Contains(real, "fake:") {
		t.Errorf("virtual %s real %s", virtual, real)
	}
}

func TestLinesAndBreakWorkOnAVirtualPort(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:link-a"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "line", "line": "rts", "value": false})
	for {
		if m := waitFor(t, c, ctx, "lines", ""); m["rts"] == false {
			break
		}
	}
	sendJSON(t, c, ctx, map[string]any{"type": "break"})
	if m := waitFor(t, c, ctx, "break", ""); m["ok"] != true {
		t.Errorf("break %v", m)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "close"})
	waitFor(t, c, ctx, "state", "closed")
	sendJSON(t, c, ctx, map[string]any{"type": "break"})
	if m := waitFor(t, c, ctx, "break", ""); m["ok"] != false {
		t.Errorf("break on a closed port %v", m)
	}
}

// The program in a real browser (Chromium through Playwright), when its browser is installed:
// npm install in tools/webtest, then npx playwright install chromium.
func TestWebPageInChromium(t *testing.T) {
	npm, err := exec.LookPath("npm")
	if err != nil {
		t.Skip("npm is not installed")
	}
	cache, _ := os.UserCacheDir()
	if _, err := os.Stat("tools/webtest/node_modules/playwright"); err != nil {
		t.Skip("run npm install in tools/webtest to include this test")
	}
	if matches, _ := filepath.Glob(filepath.Join(cache, "ms-playwright", "chromium*")); len(matches) == 0 {
		t.Skip("run npx playwright install chromium in tools/webtest to include this test")
	}
	cmd := exec.Command(npm, "run", "e2e")
	cmd.Dir = "tools/webtest"
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
}

func TestOpeningAndClosingPortsManyTimesLeavesNoGoroutinesBehind(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	time.Sleep(100 * time.Millisecond)
	before := runtime.NumGoroutine()
	for i := 0; i < 40; i++ {
		sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:counter", "triggers": "x => y"}})
		waitFor(t, c, ctx, "state", "connected")
		sendJSON(t, c, ctx, map[string]any{"type": "close"})
		waitFor(t, c, ctx, "state", "closed")
	}
	var after int
	for wait := 0; wait < 30; wait++ { // the pollers notice that the port is gone within a poll interval
		time.Sleep(100 * time.Millisecond)
		if after = runtime.NumGoroutine(); after <= before+2 {
			return
		}
	}
	t.Errorf("goroutines: %d before, %d after 40 open and close cycles", before, after)
}

func TestAPortClosedWithTheConnectionCanBeOpenedAgain(t *testing.T) {
	ts := newTestServer(t)
	for i := 0; i < 3; i++ {
		c, ctx := dialTerminal(t, ts)
		sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:link-a"}})
		waitFor(t, c, ctx, "state", "connected")
		c.Close(websocket.StatusNormalClosure, "page closed")
		time.Sleep(100 * time.Millisecond)
	}
}

func TestALogThatFailsStopsAndTellsThePage(t *testing.T) {
	if _, err := os.Stat("/dev/full"); err != nil {
		t.Skip("no /dev/full")
	}
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo", "logPath": "/dev/full"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "raw", "bytes": []byte("hello\n")})
	if m := waitFor(t, c, ctx, "notice", ""); !strings.Contains(m["text"].(string), "log stopped") {
		t.Errorf("notice %v", m)
	}
}

func TestATriggerThatTriggersItselfIsPaused(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo", "enter": "lf", "triggers": "a => a"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "raw", "bytes": []byte("a")})
	if m := waitFor(t, c, ctx, "notice", ""); !strings.Contains(m["text"].(string), "triggers paused") {
		t.Errorf("notice %v", m)
	}
}

// readFor collects what the page receives from the device for a while.
func readFor(t *testing.T, c *websocket.Conn, ctx context.Context, d time.Duration) string {
	t.Helper()
	ctx, cancel := context.WithTimeout(ctx, d)
	defer cancel()
	var got []byte
	for {
		_, text, err := readFrame(c, ctx)
		if err != nil {
			return string(got)
		}
		got = append(got, text...)
	}
}

func TestSlowSendPutsTimeBetweenCharactersAndCanBeFlushed(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo", "charDelayMs": 40, "lineDelayMs": 100}})
	waitFor(t, c, ctx, "state", "connected")
	start := time.Now()
	sendJSON(t, c, ctx, map[string]any{"type": "raw", "bytes": []byte("ab\ncd")})
	var got string
	for len(got) < 5 && time.Since(start) < 3*time.Second {
		_, text, err := readFrame(c, ctx)
		if err != nil {
			break
		}
		got += string(text)
	}
	if got != "ab\ncd" {
		t.Fatalf("echoed %q", got)
	}
	if elapsed := time.Since(start); elapsed < 40*4+100-20 {
		t.Errorf("4 pauses of 40 ms and one of 100 ms expected, it took %v", elapsed)
	}

	// a long send is stopped by a flush: only the first characters get out
	sendJSON(t, c, ctx, map[string]any{"type": "raw", "bytes": []byte(strings.Repeat("x", 100))})
	time.Sleep(150 * time.Millisecond)
	sendJSON(t, c, ctx, map[string]any{"type": "flush"})
	got = readFor(t, c, ctx, 600*time.Millisecond)
	if len(got) == 0 || len(got) > 15 {
		t.Errorf("after a flush %d characters came out of 100", len(got))
	}
}

func TestStoppingAScriptDropsWhatIsWaitingToBeSentSlowly(t *testing.T) {
	ts := newTestServer(t)
	if code, _ := ts.do(t, "POST", "/api/scripts", `{"name":"long","commands":["`+strings.Repeat("y", 80)+`"],"delayMs":0,"loop":true}`, ts.secret); code != 200 {
		t.Fatal("save")
	}
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo", "charDelayMs": 20}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "script", "name": "long"})
	time.Sleep(200 * time.Millisecond)
	sendJSON(t, c, ctx, map[string]any{"type": "stop"})
	readFor(t, c, ctx, 300*time.Millisecond) // what was already on its way
	if rest := readFor(t, c, ctx, 500*time.Millisecond); len(rest) > 3 {
		t.Errorf("%d characters kept coming after Stop", len(rest))
	}
}

// states reads the "state" messages until the wanted one comes, and returns all of them.
func statesUntil(t *testing.T, c *websocket.Conn, ctx context.Context, want string, d time.Duration) []string {
	t.Helper()
	ctx, cancel := context.WithTimeout(ctx, d)
	defer cancel()
	var seen []string
	for {
		_, data, err := c.Read(ctx)
		if err != nil {
			t.Fatalf("waiting for state %q after %v: %v", want, seen, err)
		}
		var m map[string]string
		if json.Unmarshal(data, &m) == nil && m["type"] == "state" {
			seen = append(seen, m["state"])
			if m["state"] == want {
				return seen
			}
		}
	}
}

func TestAPortThatCameBackIsOpenedAgainWhenAskedTo(t *testing.T) {
	ts := newTestServer(t)
	resetFlaky()
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:flaky", "reconnect": true}})
	statesUntil(t, c, ctx, "connected", 2*time.Second)
	if seen := statesUntil(t, c, ctx, "reconnecting", 3*time.Second); seen[len(seen)-1] != "reconnecting" {
		t.Fatalf("states %v", seen)
	}
	if seen := statesUntil(t, c, ctx, "connected", 6*time.Second); len(seen) != 1 {
		t.Errorf("a quiet reconnect says nothing while it fails, but saw %v", seen)
	}
}

func TestWithoutReconnectAPortThatWentAwayStaysEnded(t *testing.T) {
	ts := newTestServer(t)
	resetFlaky()
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:flaky"}})
	statesUntil(t, c, ctx, "connected", 2*time.Second)
	statesUntil(t, c, ctx, "ended", 3*time.Second)
}

func TestClosingThePortEndsTheReconnecting(t *testing.T) {
	ts := newTestServer(t)
	resetFlaky()
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:flaky", "reconnect": true}})
	statesUntil(t, c, ctx, "reconnecting", 4*time.Second)
	sendJSON(t, c, ctx, map[string]any{"type": "close"})
	statesUntil(t, c, ctx, "closed", time.Second)
	cctx, cancel := context.WithTimeout(ctx, 3500*time.Millisecond)
	defer cancel()
	for {
		_, data, err := c.Read(cctx)
		if err != nil {
			return // nothing came: it stopped
		}
		if strings.Contains(string(data), `"connected"`) {
			t.Fatal("the port was opened again after it was closed")
		}
	}
}

func resetFlaky() {
	fakeMu.Lock()
	flakyBack = time.Time{}
	fakeMu.Unlock()
}

func TestTheServerSendsTheGraphPoints(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo", "graphSeries": "T | t=\nH | /h=([0-9.]+)%/"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "raw", "bytes": []byte("t=21.5 h=40%\r\nt=22")})
	m := waitFor(t, c, ctx, "points", "")
	vals, _ := m["values"].([]any)
	if len(vals) != 2 || m["t"] == nil {
		t.Fatalf("points %v", m)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "raw", "bytes": []byte(".5\r\n")}) // the rest of a line that was cut
	m = waitFor(t, c, ctx, "points", "")
	if v := m["values"].([]any)[0].(map[string]any); v["name"] != "T" || v["value"] != 22.5 {
		t.Errorf("the line cut in two gave %v", v)
	}
}

// portLink lets the XMODEM engine talk through a port that the test opened itself.
type portLink struct {
	port serial.Port
	in   chan byte
}

func newPortLink(port serial.Port) *portLink {
	l := &portLink{port: port, in: make(chan byte, 1<<16)}
	go func() {
		buf := make([]byte, 256)
		for {
			n, err := port.Read(buf)
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

func (l *portLink) write(data []byte) error { _, err := l.port.Write(data); return err }
func (l *portLink) read(ctx context.Context, timeout time.Duration) (byte, error) {
	select {
	case b := <-l.in:
		return b, nil
	case <-time.After(timeout):
		return 0, errXTimeout
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

func shortXmodemTimes(t *testing.T) {
	t.Helper()
	old := xmodemTimesInUse
	xmodemTimesInUse = fastTimes
	t.Cleanup(func() { xmodemTimesInUse = old })
}

func TestAFileIsSentWithXmodemThroughTheCable(t *testing.T) {
	shortXmodemTimes(t)
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:link-a"}})
	waitFor(t, c, ctx, "state", "connected")
	other, err := openFake("fake:link-b", true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	data := bytes.Repeat([]byte("firmware "), 300)
	received := make(chan []byte, 1)
	go func() {
		got, _ := xmodemReceive(context.Background(), newPortLink(other), fastTimes, func(int) {})
		received <- got
	}()
	sendJSON(t, c, ctx, map[string]any{"type": "xmodem", "action": "send", "name": "fw.bin", "bytes": data, "oneK": true})
	done := waitFor(t, c, ctx, "xfer", "done")
	if done["dir"] != "send" || done["done"] != float64(len(data)) {
		t.Errorf("done %v", done)
	}
	if got := <-received; !bytes.HasPrefix(got, data) {
		t.Errorf("the device received %d bytes that are not the file", len(got))
	}
}

func TestAFileIsReceivedWithXmodemAndGivenToThePage(t *testing.T) {
	shortXmodemTimes(t)
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:link-a"}})
	waitFor(t, c, ctx, "state", "connected")
	other, err := openFake("fake:link-b", true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	data := []byte("log of the device\n" + strings.Repeat("x", 500))
	sendJSON(t, c, ctx, map[string]any{"type": "xmodem", "action": "receive", "name": "log"})
	go xmodemSend(context.Background(), newPortLink(other), data, false, fastTimes, func(int) {})
	done := waitFor(t, c, ctx, "xfer", "done")
	got, _ := base64.StdEncoding.DecodeString(done["data"].(string))
	if !bytes.HasPrefix(got, data) || done["dir"] != "receive" {
		t.Errorf("received %d bytes: %v", len(got), done["dir"])
	}
	// the transfer's bytes did not reach the screen
	if text := readFor(t, c, ctx, 200*time.Millisecond); strings.Contains(text, "log of the device") {
		t.Errorf("the page saw the file: %q", text)
	}
}

func TestATransferCanBeCancelledAndNeedsAnOpenPort(t *testing.T) {
	shortXmodemTimes(t)
	xmodemTimesInUse.invite = 300 * time.Millisecond // it asks for ten times: a few seconds to cancel in
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "xmodem", "action": "send", "bytes": []byte("x")})
	if m := waitFor(t, c, ctx, "notice", ""); !strings.Contains(m["text"].(string), "open the port") {
		t.Errorf("notice %v", m)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "xmodem", "action": "receive", "name": "never"})
	waitFor(t, c, ctx, "xfer", "running")
	sendJSON(t, c, ctx, map[string]any{"type": "xmodem", "action": "cancel"})
	waitFor(t, c, ctx, "xfer", "cancelled")
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
	seen := map[string]string{}
	for len(seen) < 2 {
		m := waitFor(t, c, ctx, "tunnel", "")
		data, _ := base64.StdEncoding.DecodeString(m["data"].(string))
		seen[m["dir"].(string)] = string(data)
	}
	if seen["a2b"] != "from A\n" || seen["b2a"] != "from B\n" {
		t.Errorf("the page saw %v", seen)
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

func TestATunnelNeedsTwoDifferentPortsThatAreFree(t *testing.T) {
	ts := newTestServer(t)
	_, slave := newPty(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "peer": map[string]any{"device": slave}}})
	if state := readState(t, c, ctx); !strings.Contains(state, "two different ports") {
		t.Errorf("state %q", state)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "peer": map[string]any{"device": "/dev/nothing-here"}}})
	if state := readState(t, c, ctx); !strings.HasPrefix(state, "failed:") {
		t.Errorf("state %q", state)
	}
	// the first port was released by the failed attempt
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave}})
	waitFor(t, c, ctx, "state", "connected")
}

func TestAFileIsSentAndReceivedWithYmodemThroughTheCable(t *testing.T) {
	shortXmodemTimes(t)
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:link-a"}})
	waitFor(t, c, ctx, "state", "connected")
	other, err := openFake("fake:link-b", true, true)
	if err != nil {
		t.Fatal(err)
	}
	defer other.Close()
	link := newPortLink(other)                       // one reader of the port for the whole test
	data := bytes.Repeat([]byte("exact size "), 150) // 1650 bytes: not a multiple of a block
	got := make(chan []byte, 1)
	name := make(chan string, 1)
	go func() {
		n, d, _ := ymodemReceive(context.Background(), link, fastTimes, func(int) {})
		name <- n
		got <- d
	}()
	sendJSON(t, c, ctx, map[string]any{"type": "xmodem", "action": "send", "proto": "ymodem", "name": "exact.txt", "bytes": data})
	waitFor(t, c, ctx, "xfer", "done")
	if n, d := <-name, <-got; n != "exact.txt" || !bytes.Equal(d, data) {
		t.Errorf("the device got %q with %d bytes", n, len(d))
	}

	// and the other way: the device sends, the page is given the name and the exact bytes
	sendJSON(t, c, ctx, map[string]any{"type": "xmodem", "action": "receive", "proto": "ymodem"})
	go ymodemSend(context.Background(), link, "from-device.bin", data[:777], fastTimes, func(int) {})
	done := waitFor(t, c, ctx, "xfer", "done")
	raw, _ := base64.StdEncoding.DecodeString(done["data"].(string))
	if done["received"] != "from-device.bin" || !bytes.Equal(raw, data[:777]) {
		t.Errorf("received %v with %d bytes", done["received"], len(raw))
	}
}

// Accessibility of the page (axe-core, WCAG A and AA) in the light and dark themes, when Chromium is installed.
func TestWebPageAccessibility(t *testing.T) {
	npm, err := exec.LookPath("npm")
	if err != nil {
		t.Skip("npm is not installed")
	}
	cache, _ := os.UserCacheDir()
	if _, err := os.Stat("tools/webtest/node_modules/@axe-core"); err != nil {
		t.Skip("run npm install in tools/webtest to include this test")
	}
	if matches, _ := filepath.Glob(filepath.Join(cache, "ms-playwright", "chromium*")); len(matches) == 0 {
		t.Skip("run npx playwright install chromium in tools/webtest to include this test")
	}
	cmd := exec.Command(npm, "run", "a11y")
	cmd.Dir = "tools/webtest"
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("%v\n%s", err, out)
	}
}

// collectUntil gathers the text of the frames, and returns it with the first JSON message that has the type and state.
func collectUntil(t *testing.T, c *websocket.Conn, ctx context.Context, kind, state string) (string, map[string]any) {
	t.Helper()
	var text []byte
	for {
		mt, data, err := c.Read(ctx)
		if err != nil {
			t.Fatalf("waiting for %s/%s: %v (text so far %q)", kind, state, err, text)
		}
		if mt == websocket.MessageBinary {
			n := int(binary.BigEndian.Uint32(data[8:12]))
			text = append(text, data[12+n:]...)
			continue
		}
		var m map[string]any
		json.Unmarshal(data, &m)
		if m["type"] == kind && (state == "" || m["state"] == state) {
			return string(text), m
		}
	}
}

func TestAScriptCanWaitForWhatTheDeviceSays(t *testing.T) {
	ts := newTestServer(t)
	if code, body := ts.do(t, "POST", "/api/scripts", `{"name":"login","commands":["@expect=login: ","admin","@expect=Password:","admin","@expect=/admin# /","echo done-by-script"],"delayMs":0}`, ts.secret); code != 200 {
		t.Fatalf("save: %d %s", code, body)
	}
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:shell", "enter": "cr"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "script", "name": "login"})
	text, _ := collectUntil(t, c, ctx, "script", "done")
	time.Sleep(100 * time.Millisecond)
	more := readFor(t, c, ctx, 300*time.Millisecond)
	if !strings.Contains(text+more, "done-by-script") || !strings.Contains(text+more, "admin# ") {
		t.Errorf("the shell did not run the last line: %q", text+more)
	}
}

func TestAnExpectThatDoesNotComeStopsTheScriptWithAnError(t *testing.T) {
	ts := newTestServer(t)
	ts.do(t, "POST", "/api/scripts", `{"name":"wait","commands":["@timeout=100; expect=never-comes","after"],"delayMs":0}`, ts.secret)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "script", "name": "wait"})
	text, m := collectUntil(t, c, ctx, "script", "error")
	if !strings.Contains(m["message"].(string), "never-comes") || strings.Contains(text, "after") {
		t.Errorf("message %v, echoed %q", m, text)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "steps", "data": "timeout=50; expect=zzz"})
	if n := waitFor(t, c, ctx, "notice", ""); !strings.Contains(n["text"].(string), "zzz") {
		t.Errorf("notice %v", n)
	}
	if text := readFor(t, c, ctx, 200*time.Millisecond); strings.Contains(text, "after") {
		t.Error("the line after the failed expect was sent")
	}
}

// Messages of every kind with odd contents, in any order: the server must neither panic nor hang.
func TestRandomMessagesDoNotCrashTheServer(t *testing.T) {
	for seed := int64(1); seed <= 4; seed++ {
		t.Run(itoa(int(seed)), func(t *testing.T) { randomMessages(t, seed) })
	}
}

func randomMessages(t *testing.T, seed int64) {
	ts := newTestServer(t)
	rng := rand.New(rand.NewSource(seed))
	pick := func(list ...any) any { return list[rng.Intn(len(list))] }
	devices := []any{"fake:echo", "fake:shell", "fake:counter", "fake:binary", "fake:link-a", "fake:link-b", "fake:flaky", "nothing", "", "COM99"}
	profile := func() map[string]any {
		p := map[string]any{"name": "r", "device": pick(devices...), "baud": pick(9600, 0, -5, 115200, 99999999),
			"triggers": pick("", "a => b", "/(/ => x", "x => @wait=1; hex=41"), "graphSeries": pick("", "A | a=", "bad"), "charDelayMs": pick(0, 1, 5, -1),
			"rtsMode": pick("", "off", "high", "low", "sideways"), "reconnect": pick(true, false), "charset": pick("utf-8", "cp437", "nope"),
			"view": pick("text", "hex", "frames", "x"), "frameSpec": pick("", "size = 4", "bad"), "logPath": pick("", "/dev/null")}
		if rng.Intn(4) == 0 {
			p["peer"] = map[string]any{"device": pick(devices...)}
		}
		return p
	}
	messages := []func() map[string]any{
		func() map[string]any { return map[string]any{"type": "open", "profile": profile()} },
		func() map[string]any { return map[string]any{"type": "close"} },
		func() map[string]any { return map[string]any{"type": "input", "data": pick("", "hello\r", "\x00\xff")} },
		func() map[string]any {
			return map[string]any{"type": "raw", "bytes": []byte(pick("", "abc", "\x00\x01\x02").(string))}
		},
		func() map[string]any {
			return map[string]any{"type": "steps", "data": pick("dtr=1", "wait=5", "hex=zz", "expect=x; timeout=20", "", "rts=0;;")}
		},
		func() map[string]any { return map[string]any{"type": "break"} },
		func() map[string]any { return map[string]any{"type": "flush"} },
		func() map[string]any { return map[string]any{"type": "stop"} },
		func() map[string]any { return map[string]any{"type": "mark", "data": "m"} },
		func() map[string]any { return map[string]any{"type": "resetcounters"} },
		func() map[string]any { return map[string]any{"type": "script", "name": pick("none", "")} },
		func() map[string]any {
			return map[string]any{"type": "line", "line": pick("dtr", "rts", "x"), "value": rng.Intn(2) == 0}
		},
		func() map[string]any { return map[string]any{"type": "log", "value": rng.Intn(2) == 0} },
		func() map[string]any {
			return map[string]any{"type": "xmodem", "action": pick("send", "receive", "cancel", "x"), "proto": pick("xmodem", "ymodem", ""), "bytes": []byte("data")}
		},
		func() map[string]any {
			return map[string]any{"type": "loganswer", "answer": pick("append", "overwrite", "none", "x")}
		},
		func() map[string]any { return map[string]any{"type": pick("unknown", "", "tunnel", "points")} },
	}
	c, ctx := dialTerminal(t, ts)
	// read and discard everything the server says, so that it never waits for the reader
	go func() {
		for {
			if _, _, err := c.Read(ctx); err != nil {
				return
			}
		}
	}()
	for i := 0; i < 400; i++ {
		data, _ := json.Marshal(messages[rng.Intn(len(messages))]())
		if rng.Intn(25) == 0 {
			data = []byte(pick("{broken", "", "[]", "null", `{"type":5}`).(string))
		}
		kind := websocket.MessageText
		if rng.Intn(40) == 0 {
			kind = websocket.MessageBinary
		}
		if err := c.Write(ctx, kind, data); err != nil {
			t.Fatalf("message %d: %v", i, err)
		}
		if i%20 == 0 {
			time.Sleep(15 * time.Millisecond)
		}
	}
	time.Sleep(300 * time.Millisecond)
	if code, _ := ts.do(t, "GET", "/api/ping", "", ts.secret); code != 200 {
		t.Errorf("the server did not answer after the messages: %d", code)
	}
	c.Close(websocket.StatusNormalClosure, "done")
	time.Sleep(100 * time.Millisecond)
	if code, _ := ts.do(t, "GET", "/api/ping", "", ts.secret); code != 200 {
		t.Errorf("the server did not answer after the connection closed: %d", code)
	}
}
