//go:build linux

package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"
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
