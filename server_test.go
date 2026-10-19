//go:build linux

package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
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

// readBinary returns the next frame of port output, skipping the text messages.
func readBinary(c *websocket.Conn, ctx context.Context) ([]byte, error) {
	for {
		kind, data, err := c.Read(ctx)
		if err != nil || kind == websocket.MessageBinary {
			return data, err
		}
	}
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
