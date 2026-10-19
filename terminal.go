package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"time"

	"github.com/coder/websocket"

	"termdeck/internal/serial"
)

var enterBytes = map[string]string{"cr": "\r", "lf": "\n", "crlf": "\r\n"}

type clientMessage struct {
	Type    string  `json:"type"`
	Profile Profile `json:"profile"`
	Data    string  `json:"data"`
	Answer  string  `json:"answer"` // reply to a question about an existing log: append, overwrite or none
	Line    string  `json:"line"`   // control line to set: dtr or rts
	Value   bool    `json:"value"`  // asserted or not
	Name    string  `json:"name"`   // script to run
}

// termSession is one tab: a WebSocket that can open and close its port as often as the user wants.
type termSession struct {
	srv *Server
	c   *websocket.Conn
	ctx context.Context

	mu      sync.Mutex // guards everything below
	gen     int        // changes with each open and close, so a stale reader knows it is stale
	port    serial.Port
	profile Profile
	dtr     bool // the lines as we last set them
	rts     bool
	log     *Logger
	stopRun context.CancelFunc

	writeMu sync.Mutex // one command at a time reaches the port

	opMu    sync.Mutex  // one open at a time
	answers chan string // the page's answer about an existing log file
	codec   *Codec      // device character set; nil for UTF-8
}

func (t *termSession) sendJSON(v any) {
	data, _ := json.Marshal(v)
	t.c.Write(t.ctx, websocket.MessageText, data)
}

func (t *termSession) sendState(state, message string) {
	t.sendJSON(map[string]string{"type": "state", "state": state, "message": message})
}

// open (re)opens the port of profile.
func (t *termSession) open(profile Profile) {
	t.close()
	if err := profile.Normalize(); err != nil {
		t.sendState("failed", err.Error())
		return
	}
	t.sendState("connecting", "")
	if !t.srv.claimPort(profile.Device, t) {
		t.sendState("failed", profile.Device+" is already open in another tab")
		return
	}
	port, err := openPort(profile)
	if err != nil {
		t.srv.releasePort(profile.Device, t)
		t.sendState("failed", err.Error())
		return
	}
	// The log is created only now, so a port that does not open leaves no file behind.
	existing := profile.LogExisting
	if existing == "ask" {
		existing = "append"
		if logExists(profile) {
			existing = t.askAboutLog(profile)
		}
	}
	log, logErr := OpenLog(profile, existing)
	t.mu.Lock()
	t.gen++
	gen := t.gen
	t.port, t.profile, t.log, t.codec = port, profile, log, NewCodec(profile.Charset)
	t.dtr, t.rts = asserted(profile.DTR), asserted(profile.RTS)
	t.mu.Unlock()
	t.sendState("connected", "")
	t.sendJSON(map[string]any{"type": "logging", "on": log != nil})
	if logErr != nil {
		t.sendJSON(map[string]string{"type": "notice", "text": "log not written: " + logErr.Error()})
	}
	go t.read(gen, port, t.codec)
	go t.watchLines(gen, port, asserted(profile.DTR), asserted(profile.RTS))
}

// watchLines reports the control lines (the ones we drive and the ones the device drives) when they change.
func (t *termSession) watchLines(gen int, port serial.Port, dtr, rts bool) {
	var last map[string]any
	for ; ; time.Sleep(400 * time.Millisecond) {
		t.mu.Lock()
		current := t.gen == gen
		if current {
			dtr, rts = t.dtr, t.rts
		}
		t.mu.Unlock()
		if !current {
			return
		}
		bits, err := port.GetModemStatusBits()
		if err != nil {
			return // not every port has status lines (a pty, some adapters)
		}
		now := map[string]any{"type": "lines", "cts": bits.CTS, "dsr": bits.DSR, "dcd": bits.DCD, "ri": bits.RI, "dtr": dtr, "rts": rts}
		if fmt.Sprint(now) != fmt.Sprint(last) {
			last = now
			t.sendJSON(now)
		}
	}
}

// setLine drives DTR or RTS.
func (t *termSession) setLine(line string, value bool) {
	t.mu.Lock()
	port := t.port
	t.mu.Unlock()
	if port == nil {
		return
	}
	var err error
	switch line {
	case "dtr":
		if err = port.SetDTR(value); err == nil {
			t.mu.Lock()
			t.dtr = value
			t.mu.Unlock()
		}
	case "rts":
		if err = port.SetRTS(value); err == nil {
			t.mu.Lock()
			t.rts = value
			t.mu.Unlock()
		}
	}
	if err != nil {
		t.sendJSON(map[string]string{"type": "notice", "text": "cannot set " + line + ": " + err.Error()})
	}
}

// askAboutLog asks the page what to do with the log file that is already there.
func (t *termSession) askAboutLog(profile Profile) string {
	select { // an old answer must not count
	case <-t.answers:
	default:
	}
	t.sendJSON(map[string]string{"type": "logexists", "path": logPath(profile, time.Now())})
	select {
	case answer := <-t.answers:
		if answer == "overwrite" || answer == "append" {
			return answer
		}
	case <-t.ctx.Done():
	case <-time.After(5 * time.Minute):
	}
	return "none"
}

func (t *termSession) read(gen int, port serial.Port, codec *Codec) {
	buf := make([]byte, 4096)
	for {
		n, err := port.Read(buf)
		if n > 0 {
			text := codec.FromDevice(buf[:n])
			t.logger().Write(dirRx, text)
			if t.c.Write(t.ctx, websocket.MessageBinary, text) != nil {
				return
			}
		}
		if err != nil || n == 0 {
			t.mu.Lock()
			current := t.gen == gen
			t.mu.Unlock()
			if current {
				t.close()
				message := "the port was closed"
				if err != nil {
					message = err.Error()
				}
				t.sendState("ended", message)
			}
			return
		}
	}
}

func (t *termSession) logger() *Logger {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.log
}

// setLogging starts or stops the log of the open port, whatever it was when the port opened.
func (t *termSession) setLogging(on bool) {
	t.mu.Lock()
	port, profile, current := t.port, t.profile, t.log
	t.mu.Unlock()
	switch {
	case port == nil:
		t.sendJSON(map[string]string{"type": "notice", "text": "open the port before logging"})
	case !on:
		t.mu.Lock()
		t.log = nil
		t.mu.Unlock()
		current.Close()
		t.sendJSON(map[string]any{"type": "logging", "on": false})
	case current != nil:
		// already logging
	case profile.LogPath == "":
		t.sendJSON(map[string]string{"type": "notice", "text": "set a log file in the settings of this session first"})
	default:
		existing := profile.LogExisting
		if existing == "ask" {
			existing = "append"
			if logExists(profile) {
				existing = t.askAboutLog(profile)
			}
		}
		log, err := OpenLog(profile, existing)
		if err != nil {
			t.sendJSON(map[string]string{"type": "notice", "text": "log not written: " + err.Error()})
			return
		}
		t.mu.Lock()
		t.log = log
		t.mu.Unlock()
		t.sendJSON(map[string]any{"type": "logging", "on": log != nil})
	}
}

// close stops any script and closes the port and the log. It is safe to call when nothing is open.
func (t *termSession) close() bool {
	t.mu.Lock()
	port, log, stop, device := t.port, t.log, t.stopRun, t.profile.Device
	t.port, t.log, t.stopRun = nil, nil, nil
	if port != nil {
		t.gen++
	}
	t.mu.Unlock()
	if stop != nil {
		stop()
	}
	if port != nil {
		port.Close()
		t.srv.releasePort(device, t)
	}
	log.Close()
	return port != nil
}

// write sends data to the device and records it as sent.
func (t *termSession) write(data []byte) {
	t.mu.Lock()
	port, log, codec := t.port, t.log, t.codec
	t.mu.Unlock()
	if port == nil {
		return
	}
	t.writeMu.Lock()
	defer t.writeMu.Unlock()
	if _, err := port.Write(codec.ToDevice(data)); err == nil {
		log.Write(dirTx, data)
	}
}

func (t *termSession) sendBreak() {
	t.mu.Lock()
	port := t.port
	t.mu.Unlock()
	reply := map[string]any{"type": "break", "ok": port != nil}
	if port == nil {
		reply["message"] = "the port is closed"
	} else if err := port.Break(250 * time.Millisecond); err != nil {
		reply["ok"], reply["message"] = false, err.Error()
	}
	t.sendJSON(reply)
}

// runScript sends the commands of the named script, pausing after each, in the background.
func (t *termSession) runScript(name string) {
	script, err := t.srv.scripts.Get(name)
	t.mu.Lock()
	open := t.port != nil
	if err == nil && open && t.stopRun != nil {
		t.stopRun()
	}
	var ctx context.Context
	if err == nil && open {
		var cancel context.CancelFunc
		ctx, cancel = context.WithCancel(t.ctx)
		t.stopRun = cancel
	}
	enter := enterBytes[t.profile.Enter]
	t.mu.Unlock()
	switch {
	case err != nil:
		t.sendJSON(map[string]string{"type": "script", "state": "error", "name": name, "message": err.Error()})
		return
	case !open:
		t.sendJSON(map[string]string{"type": "script", "state": "error", "name": name, "message": "the port is closed"})
		return
	}
	t.sendJSON(map[string]string{"type": "script", "state": "running", "name": name})
	go func() {
		defer func() {
			state := "done"
			if ctx.Err() != nil {
				state = "stopped"
			}
			t.sendJSON(map[string]string{"type": "script", "state": state, "name": name})
		}()
		pause := func() bool {
			select {
			case <-ctx.Done():
				return false
			case <-time.After(time.Duration(script.DelayMs) * time.Millisecond):
				return true
			}
		}
		for round := 1; ; round++ {
			for i, command := range script.Commands {
				if ctx.Err() != nil {
					return
				}
				t.write([]byte(command + enter))
				t.sendJSON(map[string]string{"type": "tx", "data": command})
				last := i == len(script.Commands)-1 && (!script.Loop || (script.Count > 0 && round >= script.Count))
				if last || !pause() {
					return
				}
			}
		}
	}()
}

func (t *termSession) stopScript() {
	t.mu.Lock()
	stop := t.stopRun
	t.mu.Unlock()
	if stop != nil {
		stop()
	}
}

// terminal serves one tab. The client sends JSON: open, close, input, break, script (by name), stop.
// The server sends binary frames of port output and JSON: state (connecting, connected, failed,
// closed, ended), tx (a line a script sent), script (running, done, stopped, error), break, notice.
// The port can be closed and opened again over the same connection, so the tab keeps its screen.
func (s *Server) terminal(w http.ResponseWriter, r *http.Request) {
	c, err := websocket.Accept(w, r, &websocket.AcceptOptions{Subprotocols: []string{wsProtocol}})
	if err != nil {
		return
	}
	defer c.CloseNow()
	t := &termSession{srv: s, c: c, ctx: r.Context(), answers: make(chan string, 1)}
	defer t.close()

	for {
		_, data, err := c.Read(t.ctx)
		if err != nil {
			return
		}
		var m clientMessage
		if json.Unmarshal(data, &m) != nil {
			continue
		}
		switch m.Type {
		case "open":
			go func() { // in the background: the page may have to answer a question first
				t.opMu.Lock()
				defer t.opMu.Unlock()
				t.open(m.Profile)
			}()
		case "loganswer":
			select {
			case t.answers <- m.Answer:
			default:
			}
		case "close":
			t.close()
			t.sendState("closed", "port closed")
		case "input":
			t.write([]byte(m.Data))
		case "break":
			t.sendBreak()
		case "script":
			t.runScript(m.Name)
		case "stop":
			t.stopScript()
		case "log":
			go func() {
				t.opMu.Lock()
				defer t.opMu.Unlock()
				t.setLogging(m.Value)
			}()
		case "line":
			t.setLine(m.Line, m.Value)
		}
	}
}
