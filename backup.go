package main

import (
	"fmt"
	"net/http"
)

// Export and import of the sessions, scripts and groups, to move them to another computer or keep a copy.

type backup struct {
	Version  int       `json:"version"`
	Profiles []Profile `json:"profiles"`
	Scripts  []Script  `json:"scripts"`
	Groups   []string  `json:"groups"`
}

func (s *Server) export(w http.ResponseWriter, r *http.Request) {
	profiles, err1 := s.store.List()
	scripts, err2 := s.scripts.List()
	groups, err3 := s.groups.List()
	for _, err := range []error{err1, err2, err3} {
		if err != nil {
			writeError(w, http.StatusInternalServerError, err)
			return
		}
	}
	w.Header().Set("Content-Disposition", `attachment; filename="termdeck-settings.json"`)
	writeJSON(w, http.StatusOK, backup{1, profiles, scripts, groups})
}

// importSettings adds what the file holds, and replaces the items that have the same name. Nothing is
// stored unless every item is valid.
func (s *Server) importSettings(w http.ResponseWriter, r *http.Request) {
	var in backup
	if !readJSON(w, r, &in) {
		return
	}
	if in.Version != 1 {
		writeError(w, http.StatusBadRequest, fmt.Errorf("this is not a termdeck settings file (version %d)", in.Version))
		return
	}
	for i := range in.Profiles { // all valid first
		if err := in.Profiles[i].Normalize(); err != nil {
			writeError(w, http.StatusBadRequest, fmt.Errorf("session %q: %w", in.Profiles[i].Name, err))
			return
		}
	}
	for i := range in.Scripts {
		if err := in.Scripts[i].Normalize(); err != nil {
			writeError(w, http.StatusBadRequest, fmt.Errorf("script %q: %w", in.Scripts[i].Name, err))
			return
		}
	}
	replaced := 0
	for _, p := range in.Profiles {
		if r, err := s.store.Upsert(p); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		} else if r {
			replaced++
		}
	}
	for _, sc := range in.Scripts {
		if r, err := s.scripts.Upsert(sc); err != nil {
			writeError(w, http.StatusBadRequest, err)
			return
		} else if r {
			replaced++
		}
	}
	for _, g := range in.Groups {
		s.groups.Upsert(g) // a group that is already there is fine; "General" is refused and skipped
	}
	writeJSON(w, http.StatusOK, map[string]int{"sessions": len(in.Profiles), "scripts": len(in.Scripts), "groups": len(in.Groups), "replaced": replaced})
}
