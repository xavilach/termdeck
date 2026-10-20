package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"termdeck/internal/serial"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func itoa(n int) string { b, _ := json.Marshal(n); return string(b) }

type testServer struct {
	*httptest.Server
	srv    *Server
	secret string
}

func newTestServer(t *testing.T) *testServer {
	t.Helper()
	dir := t.TempDir()
	s := NewServer(dir)
	ts := httptest.NewServer(s.Handler())
	t.Cleanup(ts.Close)
	t.Cleanup(s.endSessions) // the sessions would outlive the test, and keep their ports
	return &testServer{Server: ts, srv: s, secret: s.auth.Login(s.auth.NewLink())}
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
	if code, _ := ts.do(t, "GET", "/api/devices", "", ""); code != 401 {
		t.Errorf("no secret: got %d, want 401", code)
	}
	if code, _ := ts.do(t, "GET", "/api/devices", "", "wrong"); code != 401 {
		t.Errorf("wrong secret: got %d, want 401", code)
	}
	if code, _ := ts.do(t, "GET", "/api/devices", "", ts.secret); code != 200 {
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
	req, _ := http.NewRequest("GET", ts.URL+"/api/devices", nil)
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

func TestTheLayersMakeTheSettingsOfAPort(t *testing.T) {
	ts := newTestServer(t)
	put := func(method, path, body string) {
		t.Helper()
		if code, out := ts.do(t, method, path, body, ts.secret); code != 200 {
			t.Fatalf("%s %s: %d %s", method, path, code, out)
		}
	}
	put("PUT", "/api/devices/Generic", `{"name":"Generic","settings":{"fontSize":16,"baud":9600}}`)
	put("POST", "/api/devices", `{"name":"Board","settings":{"baud":57600,"enter":"lf","rules":"button Hi => send hello"}}`)
	put("PUT", "/api/portentries", `{"port":"COM3","device":"Board","settings":{"flow":"rtscts"}}`)
	layers := func(query string) map[string]any {
		_, out := ts.do(t, "GET", "/api/layers?"+query, "", ts.secret)
		var m map[string]any
		json.Unmarshal([]byte(out), &m)
		return m
	}
	m := layers("port=COM3")
	p := m["profile"].(map[string]any)
	if p["name"] != "Board · COM3" || p["device"] != "COM3" || p["baud"] != float64(57600) || p["fontSize"] != float64(16) || p["flow"] != "rtscts" || p["enter"] != "lf" {
		t.Errorf("Generic, then the device, then the port: %v", p)
	}
	if p := layers("port=COM3&device=Generic")["profile"].(map[string]any); p["baud"] != float64(9600) || p["flow"] != "rtscts" || p["name"] != "COM3" {
		t.Errorf("the same port with another device keeps its own changes: %v", p)
	}
	if p := layers("port=COM7")["profile"].(map[string]any); p["baud"] != float64(9600) || p["flow"] != "none" {
		t.Errorf("a port never seen starts from Generic: %v", p)
	}
	for _, bad := range []string{`{"name":"X","settings":{"baud":1}}`, `{"name":"X","settings":{"colour":"red"}}`, `{"name":"X","settings":{"device":"COM1"}}`, `{"name":"","settings":{}}`, `{"name":"generic"}`} {
		if code, _ := ts.do(t, "POST", "/api/devices", bad, ts.secret); code != 400 {
			t.Errorf("%s: got %d, want 400", bad, code)
		}
	}
	if code, _ := ts.do(t, "PUT", "/api/portentries", `{"port":"COM4","device":"Nobody"}`, ts.secret); code != 400 {
		t.Errorf("a port with a device that does not exist: got %d", code)
	}
	if code, _ := ts.do(t, "DELETE", "/api/devices/Generic", "", ts.secret); code != 400 {
		t.Errorf("Generic cannot be deleted: %d", code)
	}
	_, list := ts.do(t, "GET", "/api/devices", "", ts.secret)
	if !strings.HasPrefix(list, `{"devices":[{"name":"Generic"`) || !strings.Contains(list, `"Board"`) {
		t.Errorf("Generic comes first: %s", list)
	}
}

func TestAPortIsFoundByItsAdapterOnAnotherPortName(t *testing.T) {
	ts := newTestServer(t)
	ts.do(t, "POST", "/api/devices", `{"name":"Board","settings":{"baud":57600}}`, ts.secret)
	ts.do(t, "PUT", "/api/portentries", `{"port":"/dev/ttyUSB0","serial":"A50285BI","device":"Board","settings":{"reconnect":true}}`, ts.secret)
	_, out := ts.do(t, "GET", "/api/layers?port=/dev/ttyUSB1&serial=A50285BI", "", ts.secret)
	if !strings.Contains(out, `"name":"Board · /dev/ttyUSB1"`) || !strings.Contains(out, `"reconnect":true`) {
		t.Errorf("the adapter moved to ttyUSB1 and keeps its settings: %s", out)
	}
	ts.do(t, "PUT", "/api/portentries", `{"port":"/dev/ttyUSB1","serial":"A50285BI","device":"Board","settings":{}}`, ts.secret)
	if _, list := ts.do(t, "GET", "/api/portentries", "", ts.secret); strings.Contains(list, "ttyUSB0") || !strings.Contains(list, "ttyUSB1") {
		t.Errorf("saving it under its new name forgets the old one: %s", list)
	}
}

func TestADeviceWithScriptsStaysAndARenameTakesItsPortsAndScripts(t *testing.T) {
	ts := newTestServer(t)
	ts.do(t, "POST", "/api/devices", `{"name":"Board","settings":{}}`, ts.secret)
	ts.do(t, "PUT", "/api/portentries", `{"port":"COM3","device":"Board"}`, ts.secret)
	ts.do(t, "POST", "/api/scripts", `{"name":"boot","device":"Board","commands":["x"]}`, ts.secret)
	if code, _ := ts.do(t, "DELETE", "/api/devices/Board", "", ts.secret); code != 409 {
		t.Errorf("a device with scripts: got %d, want 409", code)
	}
	if code, out := ts.do(t, "PUT", "/api/devices/Board", `{"name":"Bench","settings":{}}`, ts.secret); code != 200 {
		t.Fatalf("rename %d %s", code, out)
	}
	_, ports := ts.do(t, "GET", "/api/portentries", "", ts.secret)
	_, scripts := ts.do(t, "GET", "/api/scripts", "", ts.secret)
	if !strings.Contains(ports, `"device":"Bench"`) || !strings.Contains(scripts, `"device":"Bench"`) {
		t.Errorf("ports %s scripts %s", ports, scripts)
	}
	ts.do(t, "DELETE", "/api/scripts/boot", "", ts.secret)
	if code, _ := ts.do(t, "DELETE", "/api/devices/Bench", "", ts.secret); code != 200 {
		t.Errorf("delete %d", code)
	}
	if _, ports := ts.do(t, "GET", "/api/portentries", "", ts.secret); !strings.Contains(ports, `"device":""`) {
		t.Errorf("its ports go back to Generic: %s", ports)
	}
}

func dialTerminal(t *testing.T, ts *testServer) (*websocket.Conn, context.Context) {
	t.Helper()
	return dialTerminalID(t, ts, "")
}

// dialTerminalID attaches to the session of the tab id (a new one when it is empty or unknown).
func dialTerminalID(t *testing.T, ts *testServer, id string) (*websocket.Conn, context.Context) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	c, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(ts.URL, "http")+"/api/terminal?id="+id,
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
	_, slave := newLine(t)
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
	master, slave := newLine(t)
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
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "text", "end": "none", "data": "hi\r"})
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
	master, slave := newLine(t)
	if code, body := ts.do(t, "POST", "/api/scripts",
		`{"name":"boot","device":"","commands":["one","two"],"delayMs":50,"loop":true,"count":2}`, ts.secret); code != 200 {
		t.Fatalf("save script: %d %s", code, body)
	}
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "enter": "lf"}})
	waitFor(t, c, ctx, "state", "connected")
	start := time.Now()
	sendJSON(t, c, ctx, map[string]any{"type": "script", "name": "boot"})
	waitNote(t, c, ctx, "script boot done")
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
	_, slave := newLine(t)
	ts.do(t, "POST", "/api/scripts", `{"name":"forever","commands":["x"],"delayMs":20,"loop":true}`, ts.secret)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "script", "name": "forever"})
	waitNote(t, c, ctx, "script forever: the port is closed")
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "script", "name": "forever"})
	for m := waitFor(t, c, ctx, "jobs", ""); len(m["jobs"].([]any)) == 0; m = waitFor(t, c, ctx, "jobs", "") {
	}
	sendJSON(t, c, ctx, map[string]any{"type": "stop"})
	waitNote(t, c, ctx, "script forever stopped")
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
	master, slave := newLine(t)
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
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "text", "end": "none", "data": "é€\r"})
	buf := make([]byte, 16)
	master.SetReadDeadline(time.Now().Add(5 * time.Second))
	n, err := master.Read(buf)
	if err != nil || string(buf[:n]) != "\xe9?\r" {
		t.Fatalf("device got %q (%v), want é in Latin-1 and ? for the euro sign", buf[:n], err)
	}
}

func TestExistingLogIsAskedAbout(t *testing.T) {
	ts := newTestServer(t)
	_, slave := newLine(t)
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

func TestTheSamePortCannotBeOpenTwice(t *testing.T) {
	ts := newTestServer(t)
	_, slave := newLine(t)
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
	master, slave := newLine(t)
	logFile := filepath.Join(t.TempDir(), "mid.log")
	c, ctx := dialTerminal(t, ts)
	// no log path: starting gives a notice
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "log", "value": true})
	if n := waitNote(t, c, ctx, "log"); !strings.Contains(n, "set a log file") {
		t.Errorf("notice %v", n)
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

var noteLine = regexp.MustCompile(`\x1b\[90m\[termdeck\] (.*?)\x1b\[0m\r\n`)

// splitScreen separates the notes of termdeck from the rest of what the screen shows.
func splitScreen(data []byte) (text string, notes []string) {
	for _, m := range noteLine.FindAllSubmatch(data, -1) {
		notes = append(notes, string(m[1]))
	}
	return noteLine.ReplaceAllString(string(data), ""), notes
}

// readBinary returns what the screen shows next from the device, without the notes, skipping the text messages.
func readBinary(c *websocket.Conn, ctx context.Context) ([]byte, error) {
	for {
		kind, data, err := c.Read(ctx)
		if err != nil {
			return nil, err
		}
		if kind == websocket.MessageBinary {
			if text, _ := splitScreen(data); text != "" {
				return []byte(text), nil
			}
		}
	}
}

// waitNote reads messages until the screen shows a note of termdeck that contains the text.
func waitNote(t *testing.T, c *websocket.Conn, ctx context.Context, contains string) string {
	t.Helper()
	for {
		kind, data, err := c.Read(ctx)
		if err != nil {
			t.Fatalf("waiting for a note with %q: %v", contains, err)
		}
		if kind != websocket.MessageBinary {
			continue
		}
		_, notes := splitScreen(data)
		for _, n := range notes {
			if strings.Contains(n, contains) {
				return n
			}
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

func TestTheRawBytesTheDeviceSentCanBeDownloaded(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newLine(t)
	c, ctx := dialTerminalID(t, ts, "tab-raw")
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "charset": "iso-8859-1"}})
	waitFor(t, c, ctx, "state", "connected")
	master.Write([]byte{'a', 0xe9})
	if text, err := readBinary(c, ctx); err != nil || string(text) != "aé" {
		t.Fatalf("screen %q %v", text, err)
	}
	if code, body := ts.do(t, "GET", "/api/sessions/tab-raw/raw", "", ts.secret); code != 200 || body != "a\xe9" {
		t.Errorf("raw: %d %q", code, body)
	}
	if code, _ := ts.do(t, "GET", "/api/sessions/nobody/raw", "", ts.secret); code != 404 {
		t.Errorf("unknown session: %d", code)
	}
}

func readDevice(t *testing.T, master line, want int) []byte {
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
	master, slave := newLine(t)
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
	master, slave := newLine(t)
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
	master, slave := newLine(t)
	if code, body := ts.do(t, "POST", "/api/scripts",
		`{"name":"s","commands":["@hex=41 42; wait=20","@@at","go"],"delayMs":0}`, ts.secret); code != 200 {
		t.Fatalf("save script: %d %s", code, body)
	}
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "enter": "lf"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "script", "name": "s"})
	waitNote(t, c, ctx, "script s done")
	if got := readDevice(t, master, len("AB@at\ngo\n")); string(got) != "AB@at\ngo\n" {
		t.Errorf("sent %q", got)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "cmd", "data": "dtr=0; rts=0; hex=0d"})
	if got := readDevice(t, master, 1); string(got) != "\r" {
		t.Errorf("sent %q", got)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "cmd", "data": "nonsense"})
	waitNote(t, c, ctx, "nonsense")
}

func TestTriggerAnswersWhatTheDeviceSends(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newLine(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "enter": "lf",
		"rules": "login: => send admin\n/Pass(word)?:/ => send @hex=31 32; wait=1"}})
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
	_, slave := newLine(t)
	c, ctx := dialTerminal(t, ts)
	sound := "data:audio/wav;base64," + strings.Repeat("A", 300_000)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave, "bellSound": sound}})
	waitFor(t, c, ctx, "state", "connected")
}

func TestCmdNeedsAnOpenPortAndStopsWhenItCloses(t *testing.T) {
	ts := newTestServer(t)
	master, slave := newLine(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "cmd", "data": "hex=41"})
	waitNote(t, c, ctx, "open the port")
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": slave}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "cmd", "data": "wait=300; hex=42"})
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
	if code, _ := ts.do(t, "DELETE", "/api/devices/none", "", ts.secret); code != 404 {
		t.Errorf("delete a missing device: got %d, want 404", code)
	}
	if code, _ := ts.do(t, "POST", "/api/devices", `{not json`, ts.secret); code != 400 {
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
	if n := waitNote(t, c, ctx, "break"); n != "break sent" {
		t.Errorf("break %q", n)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "close"})
	waitFor(t, c, ctx, "state", "closed")
	sendJSON(t, c, ctx, map[string]any{"type": "break"})
	if n := waitNote(t, c, ctx, "break"); !strings.Contains(n, "failed") {
		t.Errorf("break on a closed port %q", n)
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
		sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:counter", "rules": "x => send y"}})
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

func TestAPortLeftByItsPageIsClosedAfterAWhile(t *testing.T) {
	old := sessionGrace
	sessionGrace = 50 * time.Millisecond
	t.Cleanup(func() { sessionGrace = old })
	ts := newTestServer(t)
	for i := 0; i < 3; i++ {
		c, ctx := dialTerminal(t, ts)
		sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:link-a"}})
		waitFor(t, c, ctx, "state", "connected")
		c.Close(websocket.StatusNormalClosure, "page closed")
		time.Sleep(200 * time.Millisecond)
	}
}

func TestAReloadedPageFindsItsSessionAsItWas(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminalID(t, ts, "tab-1")
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "text", "end": "none", "data": "before reload\n"})
	if text := readFor(t, c, ctx, 300*time.Millisecond); !strings.Contains(text, "before reload") {
		t.Fatalf("echo %q", text)
	}
	c.Close(websocket.StatusNormalClosure, "reload")

	c, ctx = dialTerminalID(t, ts, "tab-1")
	sendJSON(t, c, ctx, map[string]any{"type": "hello", "profile": map[string]any{"name": "t", "device": "fake:echo"}, "view": map[string]any{"mode": "text"}})
	m := waitFor(t, c, ctx, "session", "")
	if m["state"] != "connected" {
		t.Errorf("state after the reload %v", m["state"])
	}
	waitFor(t, c, ctx, "reset", "")
	if text, _ := collectUntil(t, c, ctx, "replayed", ""); !strings.Contains(text, "before reload") {
		t.Errorf("the records were not shown again: %q", text)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "text", "end": "none", "data": "after\n"})
	if text := readFor(t, c, ctx, 300*time.Millisecond); !strings.Contains(text, "after") {
		t.Errorf("the port is not open any more: %q", text)
	}
	// another page with the same tab takes it over: the first one is let go
	other, octx := dialTerminalID(t, ts, "tab-1")
	sendJSON(t, other, octx, map[string]any{"type": "hello", "view": map[string]any{"mode": "hex"}})
	waitFor(t, other, octx, "session", "")
	if _, _, err := c.Read(ctx); err == nil {
		for {
			if _, _, err := c.Read(ctx); err != nil {
				break
			}
		}
	}
	// the tab is closed: the session ends and the port is free for another tab
	sendJSON(t, other, octx, map[string]any{"type": "end"})
	c3, ctx3 := dialTerminal(t, ts)
	time.Sleep(100 * time.Millisecond)
	sendJSON(t, c3, ctx3, map[string]any{"type": "open", "profile": map[string]any{"name": "t2", "device": "fake:echo"}})
	waitFor(t, c3, ctx3, "state", "connected")
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
	if n := waitNote(t, c, ctx, "log"); !strings.Contains(n, "log stopped") {
		t.Errorf("notice %v", n)
	}
}

func TestARuleThatFiresItselfIsPaused(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo", "enter": "lf", "rules": "a => send a"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "raw", "bytes": []byte("a")})
	if n := waitNote(t, c, ctx, "rules paused"); !strings.Contains(n, "rules paused") {
		t.Errorf("notice %v", n)
	}
}

// readFor collects what the page receives from the device for a while.
func readFor(t *testing.T, c *websocket.Conn, ctx context.Context, d time.Duration) string {
	t.Helper()
	ctx, cancel := context.WithTimeout(ctx, d)
	defer cancel()
	var got []byte
	for {
		text, err := readBinary(c, ctx)
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
		text, err := readBinary(c, ctx)
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
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo", "rules": "t= => graph T\n/h=([0-9.]+)%/ => graph H"}})
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
	waitNote(t, c, ctx, "open the port")
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "t", "device": "fake:echo"}})
	waitFor(t, c, ctx, "state", "connected")
	sendJSON(t, c, ctx, map[string]any{"type": "xmodem", "action": "receive", "name": "never"})
	waitFor(t, c, ctx, "xfer", "running")
	sendJSON(t, c, ctx, map[string]any{"type": "xmodem", "action": "cancel"})
	waitFor(t, c, ctx, "xfer", "cancelled")
}

func TestATunnelNeedsTwoDifferentPortsThatAreFree(t *testing.T) {
	ts := newTestServer(t)
	_, slave := newLine(t)
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

// collectUntilNote gathers what the screen shows until a note of termdeck that contains the text.
func collectUntilNote(t *testing.T, c *websocket.Conn, ctx context.Context, contains string) string {
	t.Helper()
	var text string
	for {
		kind, data, err := c.Read(ctx)
		if err != nil {
			t.Fatalf("waiting for a note with %q: %v (text so far %q)", contains, err, text)
		}
		if kind != websocket.MessageBinary {
			continue
		}
		screen, notes := splitScreen(data)
		text += screen
		for _, n := range notes {
			if strings.Contains(n, contains) {
				return text
			}
		}
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
			screen, _ := splitScreen(data)
			text = append(text, screen...)
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
	text := collectUntilNote(t, c, ctx, "script login done")
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
	text := collectUntilNote(t, c, ctx, "never-comes")
	if strings.Contains(text, "after") {
		t.Errorf("echoed %q", text)
	}
	sendJSON(t, c, ctx, map[string]any{"type": "send", "format": "cmd", "data": "timeout=50; expect=zzz"})
	waitNote(t, c, ctx, "zzz")
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
			"rules": pick("", "a => send b", "/(/ => send x", "x => send @wait=1; hex=41", "a= => graph A", "bad", "every 10ms => send @hex=41", "on cts => note"), "charDelayMs": pick(0, 1, 5, -1),
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
		func() map[string]any {
			return map[string]any{"type": "send", "format": "text", "end": "none", "data": pick("", "hello\r", "\x00\xff")}
		},
		func() map[string]any {
			return map[string]any{"type": "raw", "bytes": []byte(pick("", "abc", "\x00\x01\x02").(string))}
		},
		func() map[string]any {
			return map[string]any{"type": "send", "format": "cmd", "data": pick("dtr=1", "wait=5", "hex=zz", "expect=x; timeout=20", "", "rts=0;;")}
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

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func jsonString(s string) string { b, _ := json.Marshal(s); return string(b) }

func TestATunnelReconnectsWhenAPortComesBack(t *testing.T) {
	ts := newTestServer(t)
	resetFlaky()
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "open", "profile": map[string]any{"name": "tun", "device": "fake:flaky", "reconnect": true, "peer": map[string]any{"device": "fake:echo"}}})
	statesUntil(t, c, ctx, "connected", 2*time.Second)
	statesUntil(t, c, ctx, "reconnecting", 3*time.Second)
	if seen := statesUntil(t, c, ctx, "connected", 6*time.Second); len(seen) != 1 {
		t.Errorf("a quiet reconnect says nothing while it fails, but saw %v", seen)
	}
}

// The page must offer the choices the server accepts (Profile.Normalize, the steps, the checks).
func TestTheFormOffersTheChoicesTheServerAccepts(t *testing.T) {
	page, _ := webFiles.ReadFile("web/index.html")
	selects := regexp.MustCompile(`(?s)<select ([^>]*)>(.*?)</select>`).FindAllSubmatch(page, -1)
	option := regexp.MustCompile(`<option(?: value="([^"]*)")?[^>]*>([^<]*)<`)
	ints := func(list []int) (out []string) {
		for _, n := range list {
			out = append(out, strconv.Itoa(n))
		}
		return
	}
	want := map[string][]string{`name="dataBits"`: ints(dataBitsS), `name="parity"`: parities, `name="stopBits"`: stopBits, `name="flow"`: flows,
		`name="enter"`: enters, `name="rtsMode"`: rtsModes, `name="view"`: views, `name="newLine"`: newLines, `name="timeMode"`: timeModes,
		`name="logExisting"`: existings, `name="bell"`: bells, `name="theme"`: themes, `name="cursor"`: cursors, `name="rightClick"`: rightClicks,
		`name="bold"`: bolds, "data-view": views, "data-nl": newLines, "data-time-mode": timeModes, "data-send-type": sendFormats,
		"data-send-check": checkNames(), "data-send-end": {"none", "cr", "lf", "crlf", "custom"}}
	seen := map[string]bool{}
	for _, sel := range selects {
		for key, choices := range want {
			if !strings.Contains(string(sel[1]), key) {
				continue
			}
			seen[key] = true
			var got []string
			for _, m := range option.FindAllSubmatch(sel[2], -1) {
				if strings.Contains(string(m[0]), "value=") {
					got = append(got, string(m[1]))
				} else {
					got = append(got, string(m[2])) // <option>8</option>: the value is the text
				}
			}
			if fmt.Sprint(sortedCopy(got)) != fmt.Sprint(sortedCopy(choices)) {
				t.Errorf("%s offers %v, the server accepts %v", key, got, choices)
			}
		}
	}
	for key := range want {
		if !seen[key] {
			t.Errorf("no select %s in the page", key)
		}
	}
}

func sortedCopy(list []string) []string {
	out := append([]string(nil), list...)
	sort.Strings(out)
	return out
}

// The defaults the server gives a new device must be values the form accepts, or the form cannot be saved.
func TestTheDefaultsFitTheLimitsOfTheForm(t *testing.T) {
	page, _ := webFiles.ReadFile("web/index.html")
	defaults := defaultSettings()
	attr := func(tag, name string) (float64, bool) {
		m := regexp.MustCompile(name + `="(-?[0-9.]+)"`).FindStringSubmatch(tag)
		if m == nil {
			return 0, false
		}
		v, err := strconv.ParseFloat(m[1], 64)
		return v, err == nil
	}
	checked := 0
	for _, tag := range regexp.MustCompile(`<input [^>]*type="number"[^>]*>`).FindAllString(string(page), -1) {
		name := regexp.MustCompile(`name="(\w+)"`).FindStringSubmatch(tag)
		if name == nil {
			continue
		}
		value, ok := defaults[name[1]].(float64)
		if !ok {
			continue
		}
		checked++
		if min, ok := attr(tag, "min"); ok && value < min {
			t.Errorf("%s: the default %v is below the form's min %v", name[1], value, min)
		}
		if max, ok := attr(tag, "max"); ok && value > max {
			t.Errorf("%s: the default %v is above the form's max %v", name[1], value, max)
		}
	}
	if checked < 8 {
		t.Errorf("only %d number fields checked", checked)
	}
}

// The page opens the port as soon as it reads the session: the counters of hello must come before it, or
// they can come after the counters of the port that opened, and a quiet port then shows 0 for good.
func TestTheCountersOfHelloComeBeforeTheSession(t *testing.T) {
	ts := newTestServer(t)
	c, ctx := dialTerminal(t, ts)
	sendJSON(t, c, ctx, map[string]any{"type": "hello", "profile": map[string]any{"name": "t", "device": "fake:shell"}, "view": map[string]any{"mode": "text"}})
	var order []string
	for len(order) < 2 {
		kind, data, err := c.Read(ctx)
		if err != nil {
			t.Fatal(err)
		}
		var m map[string]any
		if kind == websocket.MessageText && json.Unmarshal(data, &m) == nil && (m["type"] == "counters" || m["type"] == "session") {
			order = append(order, m["type"].(string))
		}
	}
	if order[0] != "counters" {
		t.Errorf("order %v", order)
	}
}

func TestThePortsOfTheCommandLineAreGivenOnce(t *testing.T) {
	ts := newTestServer(t)
	ts.srv.startOpen = []string{"COM3@115200", "fake:echo"}
	if _, out := ts.do(t, "POST", "/api/startopen", "", ts.secret); out != `{"open":["COM3@115200","fake:echo"]}`+"\n" {
		t.Errorf("first: %q", out)
	}
	if _, out := ts.do(t, "POST", "/api/startopen", "", ts.secret); out != `{"open":[]}`+"\n" {
		t.Errorf("second: %q", out)
	}
}
