//go:build linux

package main

import (
	"strings"
	"testing"
)

func TestSettingsCanBeExportedAndImportedElsewhere(t *testing.T) {
	a := newTestServer(t)
	a.do(t, "POST", "/api/devices", `{"name":"Board","settings":{"rules":"button Hi => send hello"}}`, a.secret)
	a.do(t, "PUT", "/api/portentries", `{"port":"COM3","device":"Board","settings":{"baud":9600}}`, a.secret)
	a.do(t, "POST", "/api/scripts", `{"name":"boot","device":"Board","commands":["one","@wait=5"]}`, a.secret)
	code, exported := a.do(t, "GET", "/api/export", "", a.secret)
	if code != 200 || !strings.Contains(exported, `"Board"`) || !strings.Contains(exported, `"boot"`) || !strings.Contains(exported, `"COM3"`) {
		t.Fatalf("%d %s", code, exported)
	}

	b := newTestServer(t)
	b.do(t, "POST", "/api/devices", `{"name":"board","settings":{}}`, b.secret) // the same name in another case
	code, result := b.do(t, "POST", "/api/import", exported, b.secret)
	if code != 200 || !strings.Contains(result, `"devices":1`) || !strings.Contains(result, `"replaced":1`) {
		t.Fatalf("import: %d %s", code, result)
	}
	_, devices := b.do(t, "GET", "/api/devices", "", b.secret)
	if strings.Count(devices, `"name"`) != 2 || !strings.Contains(devices, "hello") {
		t.Errorf("the imported device should have replaced the old one (Generic is the other): %s", devices)
	}
	if _, layers := b.do(t, "GET", "/api/layers?port=COM3", "", b.secret); !strings.Contains(layers, `"baud":9600`) || !strings.Contains(layers, "Board · COM3") {
		t.Errorf("the port came with its device and its changes: %s", layers)
	}
	if _, scripts := b.do(t, "GET", "/api/scripts", "", b.secret); !strings.Contains(scripts, `"boot"`) {
		t.Errorf("scripts %s", scripts)
	}
}

func TestAnImportWithABadItemStoresNothing(t *testing.T) {
	ts := newTestServer(t)
	body := `{"version":2,"devices":[{"name":"ok","settings":{}},{"name":"bad","settings":{"baud":1}}],"ports":[],"scripts":[]}`
	if code, _ := ts.do(t, "POST", "/api/import", body, ts.secret); code != 400 {
		t.Errorf("got %d, want 400", code)
	}
	if _, devices := ts.do(t, "GET", "/api/devices", "", ts.secret); strings.Contains(devices, `"ok"`) {
		t.Errorf("the valid device was stored although another was not: %s", devices)
	}
	if code, _ := ts.do(t, "POST", "/api/import", `{"version":1}`, ts.secret); code != 400 {
		t.Errorf("another version: got %d, want 400", code)
	}
	if code, _ := ts.do(t, "GET", "/api/export", "", ""); code != 401 {
		t.Errorf("an export needs the login: got %d", code)
	}
}

func TestAnImportedSessionCannotClimbOutOfItsLogFolder(t *testing.T) {
	ts := newTestServer(t)
	body := `{"version":2,"devices":[],"ports":[{"port":"COM1","settings":{"logPath":"logs/../../../etc/x.log"}}],"scripts":[]}`
	if code, _ := ts.do(t, "POST", "/api/import", body, ts.secret); code != 400 {
		t.Errorf("got %d, want 400", code)
	}
	if hasParentSegment("logs/{name}.log") || hasParentSegment("C:\\logs\\a..b.log") || !hasParentSegment("..\\x") {
		t.Error("only a .. segment counts")
	}
}
