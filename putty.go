package main

import (
	"bufio"
	"encoding/binary"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"unicode/utf16"
)

// Import of the serial sessions of PuTTY: from the Registry (Windows), from ~/.putty/sessions (Unix), or
// from a file the user chooses (a .reg export, or a session file). Only serial sessions are taken.

var (
	puttyParity = map[int]string{0: "none", 1: "odd", 2: "even", 3: "mark", 4: "space"}
	puttyFlow   = map[int]string{0: "none", 1: "xonxoff", 2: "rtscts", 3: "dsrdtr"}
	puttyStop   = map[int]string{2: "1", 3: "1.5", 4: "2"} // PuTTY counts half bits
)

// unescapePuttyName turns the %XX escapes of a saved session name into the characters.
func unescapePuttyName(in string) string {
	var out []byte
	for i := 0; i < len(in); i++ {
		if in[i] == '%' && i+2 < len(in) {
			if n, err := strconv.ParseUint(in[i+1:i+3], 16, 8); err == nil {
				out = append(out, byte(n))
				i += 2
				continue
			}
		}
		out = append(out, in[i])
	}
	return string(out)
}

// puttyProfile makes a profile of the settings of a PuTTY session; ok is false for a session that is not serial.
func puttyProfile(name string, settings map[string]string) (Profile, bool) {
	if !strings.EqualFold(settings["Protocol"], "serial") || settings["SerialLine"] == "" {
		return Profile{}, false
	}
	number := func(key string, def int) int {
		if n, err := strconv.Atoi(settings[key]); err == nil {
			return n
		}
		return def
	}
	p := Profile{
		Name:     unescapePuttyName(name),
		Device:   settings["SerialLine"],
		Baud:     number("SerialSpeed", 9600),
		DataBits: number("SerialDataBits", 8),
		Parity:   puttyParity[number("SerialParity", 0)],
		StopBits: puttyStop[number("SerialStopHalfbits", 2)],
		Flow:     puttyFlow[number("SerialFlowControl", 1)],
	}
	if p.Normalize() != nil {
		return Profile{}, false
	}
	return p, true
}

// parsePuttyFile reads a PuTTY session file (Key=Value lines) or a .reg export (sections of "Key"=value lines,
// possibly UTF-16), and gives the serial sessions in it. A session file has no name inside: name is used then.
func parsePuttyFile(name string, data []byte) []Profile {
	text := decodeText(data)
	if strings.Contains(text, "[HKEY_") {
		return parsePuttyReg(text)
	}
	settings := map[string]string{}
	scanner := bufio.NewScanner(strings.NewReader(text))
	for scanner.Scan() {
		if key, value, ok := strings.Cut(strings.TrimSpace(scanner.Text()), "="); ok {
			settings[key] = value
		}
	}
	if p, ok := puttyProfile(name, settings); ok {
		return []Profile{p}
	}
	return nil
}

func parsePuttyReg(text string) []Profile {
	var out []Profile
	var name string
	var settings map[string]string
	flush := func() {
		if settings != nil {
			if p, ok := puttyProfile(name, settings); ok {
				out = append(out, p)
			}
		}
		settings = nil
	}
	scanner := bufio.NewScanner(strings.NewReader(text))
	scanner.Buffer(make([]byte, 0, 64*1024), 1<<20)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		switch {
		case strings.HasPrefix(line, "["):
			flush()
			const marker = `\PuTTY\Sessions\`
			if at := strings.Index(line, marker); at >= 0 {
				name, settings = strings.TrimSuffix(line[at+len(marker):], "]"), map[string]string{}
			}
		case settings != nil && strings.HasPrefix(line, `"`):
			key, value, ok := strings.Cut(line, `"=`)
			if !ok {
				continue
			}
			key = strings.TrimPrefix(key, `"`)
			if v, found := strings.CutPrefix(value, "dword:"); found {
				if n, err := strconv.ParseUint(v, 16, 32); err == nil {
					value = strconv.FormatUint(n, 10)
				}
			} else {
				value = strings.ReplaceAll(strings.Trim(value, `"`), `\\`, `\`)
			}
			settings[key] = value
		}
	}
	flush()
	return out
}

// decodeText turns the bytes of a file into text: UTF-16 with a byte order mark (what regedit writes), or as is.
func decodeText(data []byte) string {
	if len(data) >= 2 && (data[0] == 0xff && data[1] == 0xfe || data[0] == 0xfe && data[1] == 0xff) {
		order := binary.ByteOrder(binary.LittleEndian)
		if data[0] == 0xfe {
			order = binary.BigEndian
		}
		units := make([]uint16, 0, len(data)/2)
		for i := 2; i+1 < len(data); i += 2 {
			units = append(units, order.Uint16(data[i:]))
		}
		return string(utf16.Decode(units))
	}
	return strings.TrimPrefix(string(data), "\uFEFF")
}

// puttySessionsFromFolder reads the session files of a folder (Unix PuTTY keeps them in ~/.putty/sessions).
func puttySessionsFromFolder(dir string) []Profile {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	var out []Profile
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		if data, err := os.ReadFile(filepath.Join(dir, e.Name())); err == nil {
			out = append(out, parsePuttyFile(e.Name(), data)...)
		}
	}
	sort.Slice(out, func(i, j int) bool { return strings.ToLower(out[i].Name) < strings.ToLower(out[j].Name) })
	return out
}

// foundPuttySessions are the serial sessions PuTTY has on this computer.
func foundPuttySessions() []Profile {
	if sessions := puttySessionsFromRegistry(); len(sessions) > 0 {
		return sessions
	}
	if home, err := os.UserHomeDir(); err == nil {
		return puttySessionsFromFolder(filepath.Join(home, ".putty", "sessions"))
	}
	return nil
}

// puttyBackup makes settings to import of PuTTY sessions: each session is a device (its name and its line
// settings), and its port is remembered with that device.
func puttyBackup(sessions []Profile) backup {
	out := backup{Version: backupVersion, Devices: []Device{}, Ports: []PortEntry{}, Scripts: []Script{}}
	for _, p := range sessions {
		out.Devices = append(out.Devices, Device{Name: p.Name, Settings: map[string]any{"baud": p.Baud, "dataBits": p.DataBits,
			"parity": p.Parity, "stopBits": p.StopBits, "flow": p.Flow}})
		out.Ports = append(out.Ports, PortEntry{Port: p.Device, Device: p.Name, Settings: map[string]any{}})
	}
	return out
}

// putty finds the serial sessions of PuTTY on this computer (GET), or in a file the page sends (POST), as
// settings the page can import.
func (s *Server) putty(w http.ResponseWriter, r *http.Request) {
	sessions := foundPuttySessions()
	if r.Method == http.MethodPost {
		var in struct{ Name, Text string }
		if !readJSON(w, r, &in) {
			return
		}
		sessions = parsePuttyFile(in.Name, []byte(in.Text))
	}
	writeJSON(w, http.StatusOK, puttyBackup(sessions))
}
