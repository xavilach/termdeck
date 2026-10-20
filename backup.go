package main

import (
	"fmt"
	"net/http"
	"strings"
)

// Export and import of the devices, ports and scripts, to move them to another computer or keep a copy.

type backup struct {
	Version int         `json:"version"`
	Devices []Device    `json:"devices"`
	Ports   []PortEntry `json:"ports"`
	Scripts []Script    `json:"scripts"`
}

const backupVersion = 2

// hasParentSegment says whether a path climbs out of its folder with "..": a file from elsewhere must not
// make a session write over files outside the place it names.
func hasParentSegment(path string) bool {
	for _, part := range strings.FieldsFunc(path, func(r rune) bool { return r == '/' || r == '\\' }) {
		if part == ".." {
			return true
		}
	}
	return false
}

func (s *Server) export(w http.ResponseWriter, r *http.Request) {
	devices, err1 := s.devices.List()
	ports, err2 := s.portEntries.List()
	scripts, err3 := s.scripts.List()
	for _, err := range []error{err1, err2, err3} {
		if err != nil {
			writeError(w, http.StatusInternalServerError, err)
			return
		}
	}
	w.Header().Set("Content-Disposition", `attachment; filename="termdeck-settings.json"`)
	writeJSON(w, http.StatusOK, backup{backupVersion, devices, ports, scripts})
}

// checkBackup says what is wrong with the first item that is not valid, or nil.
func checkBackup(in *backup) error {
	if in.Version != backupVersion {
		return fmt.Errorf("this is not a termdeck settings file of this version (version %d, not %d)", in.Version, backupVersion)
	}
	logPath := func(layer map[string]any) bool { path, _ := layer["logPath"].(string); return hasParentSegment(path) }
	for i := range in.Devices {
		if err := newDeviceStore("").normalize(&in.Devices[i]); err != nil {
			return err
		}
		if logPath(in.Devices[i].Settings) {
			return fmt.Errorf("device %q: a log path with .. in it is not imported", in.Devices[i].Name)
		}
	}
	for i := range in.Ports {
		if err := newPortStore("").normalize(&in.Ports[i]); err != nil {
			return err
		}
		if logPath(in.Ports[i].Settings) {
			return fmt.Errorf("port %q: a log path with .. in it is not imported", in.Ports[i].Port)
		}
	}
	for i := range in.Scripts {
		if err := in.Scripts[i].Normalize(); err != nil {
			return fmt.Errorf("script %q: %w", in.Scripts[i].Name, err)
		}
	}
	return nil
}

// importSettings adds what the file holds, and replaces the items that have the same name. Nothing is
// stored unless every item is valid.
func (s *Server) importSettings(w http.ResponseWriter, r *http.Request) {
	var in backup
	if !readJSONLimit(w, r, &in, 32<<20) { // settings can carry bell sounds of 300 KB each
		return
	}
	if err := checkBackup(&in); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	replaced := 0
	count := func(r bool, err error) error {
		if r {
			replaced++
		}
		return err
	}
	for _, d := range in.Devices {
		if err := count(s.devices.Upsert(d)); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
	}
	for _, p := range in.Ports {
		if err := count(s.portEntries.Upsert(p)); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
	}
	for _, sc := range in.Scripts {
		if err := count(s.scripts.Upsert(sc)); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]int{"devices": len(in.Devices), "ports": len(in.Ports), "scripts": len(in.Scripts), "replaced": replaced})
}
