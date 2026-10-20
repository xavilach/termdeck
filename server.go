package main

import (
	"crypto/rand"
	"crypto/subtle"
	"embed"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"
)

//go:embed web
var webFiles embed.FS

const (
	authHeader   = "X-Termdeck-Auth"
	wsProtocol   = "termdeck"
	linkLifetime = 10 * time.Minute
	maxBody      = 1 << 20
)

func randomToken(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return base64.RawURLEncoding.EncodeToString(b)
}

// Auth holds one-time start links and the per-tab secrets they are exchanged for.
type Auth struct {
	mu      sync.Mutex
	links   map[string]time.Time
	secrets []string
}

func NewAuth() *Auth { return &Auth{links: map[string]time.Time{}} }

func (a *Auth) NewLink() string {
	token := randomToken(24)
	a.mu.Lock()
	a.links[token] = time.Now().Add(linkLifetime)
	a.mu.Unlock()
	return token
}

// Login consumes a start token and returns a new secret, or "".
func (a *Auth) Login(token string) string {
	a.mu.Lock()
	defer a.mu.Unlock()
	for link, expires := range a.links {
		if subtle.ConstantTimeCompare([]byte(link), []byte(token)) == 1 {
			delete(a.links, link)
			if time.Now().After(expires) {
				return ""
			}
			secret := randomToken(32)
			a.secrets = append(a.secrets, secret)
			return secret
		}
	}
	return ""
}

func (a *Auth) Check(secret string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	ok := false
	for _, s := range a.secrets {
		if subtle.ConstantTimeCompare([]byte(s), []byte(secret)) == 1 {
			ok = true
		}
	}
	return ok && secret != ""
}

type Server struct {
	sessionsMu sync.Mutex
	sessions   map[string]*termSession // the sessions of the tabs, by the tab's id

	portsMu     sync.Mutex
	ports       map[string]*termSession // ports that are open, by device, so no two tabs open the same one
	devices     *Store[Device]
	portEntries *Store[PortEntry]
	scripts     *Store[Script]
	auth        *Auth
}

// NewServer keeps its settings in dir: devices.json, ports.json and scripts.json.
func NewServer(dir string) *Server {
	return &Server{devices: newDeviceStore(filepath.Join(dir, "devices.json")), portEntries: newPortStore(filepath.Join(dir, "ports.json")),
		scripts: NewScriptStore(filepath.Join(dir, "scripts.json")), auth: NewAuth(), ports: map[string]*termSession{}, sessions: map[string]*termSession{}}
}

func wsSecret(r *http.Request) string {
	for _, header := range r.Header.Values("Sec-WebSocket-Protocol") {
		for _, p := range strings.Split(header, ",") {
			if p = strings.TrimSpace(p); strings.HasPrefix(p, "auth.") {
				return p[len("auth."):]
			}
		}
	}
	return ""
}

func isLocalHost(host string) bool {
	name, _, err := net.SplitHostPort(host)
	if err != nil {
		name = host
	}
	return name == "127.0.0.1" || name == "localhost" || name == "[::1]" || name == "::1"
}

// guard answers only localhost, refuses cross-origin requests and requires the secret on /api/ (except login).
func (s *Server) guard(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !isLocalHost(r.Host) {
			http.Error(w, "termdeck only answers on localhost", http.StatusForbidden)
			return
		}
		if origin := r.Header.Get("Origin"); origin != "" && origin != "http://"+r.Host {
			http.Error(w, "cross-origin request refused", http.StatusForbidden)
			return
		}
		h := w.Header()
		h.Set("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; media-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
		h.Set("X-Frame-Options", "DENY")
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("Referrer-Policy", "no-referrer")
		if strings.HasPrefix(r.URL.Path, "/api/") {
			h.Set("Cache-Control", "no-store")
			if r.URL.Path != "/api/login" {
				secret := r.Header.Get(authHeader)
				if secret == "" {
					secret = wsSecret(r)
				}
				if !s.auth.Check(secret) {
					writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "not logged in: open termdeck with the link it printed"})
					return
				}
			}
		}
		next.ServeHTTP(w, r)
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]string{"error": err.Error()})
}

func readJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	return readJSONLimit(w, r, v, maxBody)
}

func readJSONLimit(w http.ResponseWriter, r *http.Request, v any, limit int64) bool {
	if err := json.NewDecoder(io.LimitReader(r.Body, limit)).Decode(v); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return false
	}
	return true
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	static, _ := fs.Sub(webFiles, "web")
	files := http.FileServer(http.FS(static))
	mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-cache")
		files.ServeHTTP(w, r)
	})
	mux.Handle("GET /", files) // the page, its scripts, style sheet and vendor files
	mux.HandleFunc("POST /api/login", s.login)
	mux.HandleFunc("POST /api/link", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"url": "/#token=" + s.auth.NewLink()})
	})
	mux.HandleFunc("GET /api/ping", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
	})
	mux.HandleFunc("GET /api/ports", func(w http.ResponseWriter, r *http.Request) {
		ports, err := listPorts()
		if err != nil {
			writeError(w, http.StatusInternalServerError, err)
			return
		}
		writeJSON(w, http.StatusOK, ports)
	})
	mux.HandleFunc("GET /api/portinfo", func(w http.ResponseWriter, r *http.Request) {
		ports, _ := listPorts()
		writeJSON(w, http.StatusOK, portDetails(ports))
	})
	mux.HandleFunc("GET /api/virtual", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, virtualPorts())
	})
	mux.HandleFunc("GET /api/devices", s.listDevices)
	mux.HandleFunc("POST /api/devices", s.saveDevice)
	mux.HandleFunc("PUT /api/devices/{name}", s.saveDevice)
	mux.HandleFunc("DELETE /api/devices/{name}", s.deleteDevice)
	mux.HandleFunc("GET /api/portentries", s.listPortEntries)
	mux.HandleFunc("PUT /api/portentries", s.savePortEntry)
	mux.HandleFunc("DELETE /api/portentries/{port...}", s.deletePortEntry)
	mux.HandleFunc("GET /api/layers", s.getLayers)
	mux.HandleFunc("GET /api/presets", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, http.StatusOK, presets) })
	mux.HandleFunc("GET /api/scripts", s.listScripts)
	mux.HandleFunc("POST /api/scripts", s.saveScript)
	mux.HandleFunc("PUT /api/scripts/{name}", s.saveScript)
	mux.HandleFunc("DELETE /api/scripts/{name}", s.deleteScript)
	mux.HandleFunc("GET /api/info", func(w http.ResponseWriter, r *http.Request) {
		home, _ := configDir()
		writeJSON(w, http.StatusOK, map[string]string{"version": version, "os": runtime.GOOS, "home": home})
	})
	mux.HandleFunc("GET /api/putty", s.putty)
	mux.HandleFunc("POST /api/putty", s.putty)
	mux.HandleFunc("GET /api/export", s.export)
	mux.HandleFunc("POST /api/import", s.importSettings)
	mux.HandleFunc("GET /api/terminal", s.terminal)
	mux.HandleFunc("GET /api/sessions/{id}/raw", s.rawOutput)
	return s.guard(mux)
}

// rawOutput is what the device of a tab sent, as the bytes it sent (the records kept: the last 8 MB).
func (s *Server) rawOutput(w http.ResponseWriter, r *http.Request) {
	s.sessionsMu.Lock()
	t := s.sessions[r.PathValue("id")]
	s.sessionsMu.Unlock()
	if t == nil {
		writeError(w, http.StatusNotFound, fmt.Errorf("no such tab"))
		return
	}
	t.streamMu.Lock()
	data := t.hist.raw()
	t.streamMu.Unlock()
	w.Header().Set("Content-Type", "application/octet-stream")
	w.Write(data)
}

func (s *Server) login(w http.ResponseWriter, r *http.Request) {
	var body struct{ Token string }
	if !readJSON(w, r, &body) {
		return
	}
	secret := s.auth.Login(body.Token)
	if secret == "" {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "this link was already used or is not valid: open a new one from termdeck"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"secret": secret})
}

func (s *Server) listScripts(w http.ResponseWriter, r *http.Request) {
	scripts, err := s.scripts.List()
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"scripts": []Script{}, "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"scripts": scripts})
}

func (s *Server) saveScript(w http.ResponseWriter, r *http.Request) {
	var sc Script
	if !readJSON(w, r, &sc) {
		return
	}
	if err := s.scripts.Put(r.PathValue("name"), sc); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, sc)
}

func (s *Server) deleteScript(w http.ResponseWriter, r *http.Request) {
	if err := s.scripts.Delete(r.PathValue("name")); err != nil {
		writeError(w, http.StatusNotFound, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
}

// portKey names a device the same way however it is written (COM3, com3, \\.\COM3).
func portKey(device string) string {
	device = strings.TrimPrefix(device, `\\.\`)
	if runtime.GOOS == "windows" {
		device = strings.ToUpper(device)
	}
	return device
}

// claimPort reserves a device for a tab. It fails when another tab has it.
func (s *Server) claimPort(device string, t *termSession) bool {
	s.portsMu.Lock()
	defer s.portsMu.Unlock()
	key := portKey(device)
	if owner, taken := s.ports[key]; taken && owner != t {
		return false
	}
	s.ports[key] = t
	return true
}

func (s *Server) releasePort(device string, t *termSession) {
	s.portsMu.Lock()
	defer s.portsMu.Unlock()
	if key := portKey(device); s.ports[key] == t {
		delete(s.ports, key)
	}
}
