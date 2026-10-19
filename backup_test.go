//go:build linux

package main

import (
	"strings"
	"testing"
)

func TestSettingsCanBeExportedAndImportedElsewhere(t *testing.T) {
	a := newTestServer(t)
	a.do(t, "POST", "/api/groups", `{"name":"Lab"}`, a.secret)
	a.do(t, "POST", "/api/profiles", `{"name":"Board","device":"COM3","group":"Lab","quickSends":"Hi | text | hello"}`, a.secret)
	a.do(t, "POST", "/api/scripts", `{"name":"boot","group":"Lab","commands":["one","@wait=5"]}`, a.secret)
	code, exported := a.do(t, "GET", "/api/export", "", a.secret)
	if code != 200 || !strings.Contains(exported, `"Board"`) || !strings.Contains(exported, `"boot"`) || !strings.Contains(exported, `"Lab"`) {
		t.Fatalf("%d %s", code, exported)
	}

	b := newTestServer(t)
	b.do(t, "POST", "/api/profiles", `{"name":"board","device":"COM9"}`, b.secret) // the same name in another case
	code, result := b.do(t, "POST", "/api/import", exported, b.secret)
	if code != 200 || !strings.Contains(result, `"sessions":1`) || !strings.Contains(result, `"replaced":1`) {
		t.Fatalf("import: %d %s", code, result)
	}
	_, profiles := b.do(t, "GET", "/api/profiles", "", b.secret)
	if strings.Count(profiles, `"name"`) != 1 || !strings.Contains(profiles, `"COM3"`) || !strings.Contains(profiles, "hello") {
		t.Errorf("the imported session should have replaced the old one: %s", profiles)
	}
	if _, scripts := b.do(t, "GET", "/api/scripts", "", b.secret); !strings.Contains(scripts, `"boot"`) {
		t.Errorf("scripts %s", scripts)
	}
	if _, groups := b.do(t, "GET", "/api/groups", "", b.secret); !strings.Contains(groups, "Lab") {
		t.Errorf("groups %s", groups)
	}
}

func TestAnImportWithABadItemStoresNothing(t *testing.T) {
	ts := newTestServer(t)
	body := `{"version":1,"profiles":[{"name":"ok","device":"COM1"},{"name":"bad","device":"COM2","baud":1}],"scripts":[],"groups":[]}`
	if code, _ := ts.do(t, "POST", "/api/import", body, ts.secret); code != 400 {
		t.Errorf("got %d, want 400", code)
	}
	if _, profiles := ts.do(t, "GET", "/api/profiles", "", ts.secret); strings.Contains(profiles, `"ok"`) {
		t.Errorf("the valid session was stored although another was not: %s", profiles)
	}
	if code, _ := ts.do(t, "POST", "/api/import", `{"version":2}`, ts.secret); code != 400 {
		t.Errorf("another version: got %d, want 400", code)
	}
	if code, _ := ts.do(t, "GET", "/api/export", "", ""); code != 401 {
		t.Errorf("an export needs the login: got %d", code)
	}
}
