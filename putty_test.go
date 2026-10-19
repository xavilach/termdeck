//go:build linux

package main

import (
	"strings"
	"testing"
	"unicode/utf16"
)

func TestPuttySessionNamesAreUnescaped(t *testing.T) {
	for in, want := range map[string]string{"Router%20console": "Router console", "plain": "plain", "%2Fdev%2Ftty": "/dev/tty", "50%": "50%", "bad%zz": "bad%zz", "a%41": "aA"} {
		if got := unescapePuttyName(in); got != want {
			t.Errorf("%q: got %q, want %q", in, got, want)
		}
	}
}

const puttyFile = "Protocol=serial\nSerialLine=COM7\nSerialSpeed=57600\nSerialDataBits=7\nSerialStopHalfbits=4\nSerialParity=2\nSerialFlowControl=2\nHost=\n"

func TestAPuttySessionFileGivesASerialSession(t *testing.T) {
	got := parsePuttyFile("Bench%20board", []byte(puttyFile))
	if len(got) != 1 {
		t.Fatalf("%+v", got)
	}
	p := got[0]
	if p.Name != "Bench board" || p.Device != "COM7" || p.Baud != 57600 || p.DataBits != 7 || p.StopBits != "2" || p.Parity != "even" || p.Flow != "rtscts" || p.Group != "PuTTY" {
		t.Errorf("%+v", p)
	}
	if n := len(parsePuttyFile("ssh", []byte("Protocol=ssh\nHostName=example.invalid\n"))); n != 0 {
		t.Errorf("an ssh session gave %d sessions", n)
	}
	defaults := parsePuttyFile("d", []byte("Protocol=serial\nSerialLine=/dev/ttyUSB0\n"))
	if len(defaults) != 1 || defaults[0].Baud != 9600 || defaults[0].StopBits != "1" || defaults[0].Flow != "xonxoff" {
		t.Errorf("PuTTY's defaults: %+v", defaults)
	}
}

const puttyReg = "Windows Registry Editor Version 5.00\r\n\r\n" +
	"[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\Dev%20board]\r\n" +
	"\"Protocol\"=\"serial\"\r\n\"SerialLine\"=\"COM3\"\r\n\"SerialSpeed\"=dword:0001c200\r\n\"SerialDataBits\"=dword:00000008\r\n" +
	"\"SerialStopHalfbits\"=dword:00000003\r\n\"SerialParity\"=dword:00000000\r\n\"SerialFlowControl\"=dword:00000000\r\n\r\n" +
	"[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\Some%20server]\r\n\"Protocol\"=\"ssh\"\r\n\"HostName\"=\"x\"\r\n\r\n" +
	"[HKEY_CURRENT_USER\\Software\\SimonTatham\\PuTTY\\Sessions\\Default%20Settings]\r\n\"Protocol\"=\"ssh\"\r\n"

func TestAnExportFromRegeditGivesItsSerialSessions(t *testing.T) {
	for _, data := range [][]byte{[]byte(puttyReg), utf16le(puttyReg)} {
		got := parsePuttyFile("export.reg", data)
		if len(got) != 1 || got[0].Name != "Dev board" || got[0].Device != "COM3" || got[0].Baud != 115200 || got[0].StopBits != "1.5" || got[0].Flow != "none" {
			t.Errorf("%+v", got)
		}
	}
}

func utf16le(s string) []byte {
	out := []byte{0xff, 0xfe}
	for _, u := range utf16.Encode([]rune(s)) {
		out = append(out, byte(u), byte(u>>8))
	}
	return out
}

func TestSessionsAreReadFromAFolderAndOverHTTP(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, dir+"/Bench%20board", puttyFile)
	writeFile(t, dir+"/web", "Protocol=ssh\n")
	if got := puttySessionsFromFolder(dir); len(got) != 1 || got[0].Name != "Bench board" {
		t.Errorf("%+v", got)
	}
	if got := puttySessionsFromFolder(dir + "/none"); got != nil {
		t.Error("a missing folder gives nothing")
	}
}

func TestPuttyFileCanBePostedToTheServer(t *testing.T) {
	ts := newTestServer(t)
	body := `{"name":"x.reg","text":` + jsonString(puttyReg) + `}`
	code, out := ts.do(t, "POST", "/api/putty", body, ts.secret)
	if code != 200 || !strings.Contains(out, `"Dev board"`) {
		t.Errorf("%d %s", code, out)
	}
	if code, out := ts.do(t, "POST", "/api/putty", `{"name":"junk","text":"nothing here"}`, ts.secret); code != 200 || !strings.Contains(out, `"sessions":[]`) {
		t.Errorf("%d %s", code, out)
	}
}
