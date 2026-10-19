package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"net/http"
	"sync"
	"sync/atomic"
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
	Bytes   []byte  `json:"bytes"`  // raw bytes to send, as they are
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

	opMu          sync.Mutex   // one open at a time
	answers       chan string  // the page's answer about an existing log file
	codec         *Codec       // device character set; nil for UTF-8
	rx, tx        atomic.Int64 // bytes received and sent since the port opened
	triggers      []trigger
	replyMu       sync.Mutex      // trigger replies go out one at a time, in order
	pending       atomic.Int32    // trigger replies waiting or running
	runCtx        context.Context // ends when the port closes: for steps typed in Cmd and trigger replies
	runStop       context.CancelFunc
	reconnectStop context.CancelFunc // ends the loop that opens the port again
	outQ          chan outItem       // set when the profile has send delays: what the writer sends
	flushed       atomic.Int64       // changes when the waiting data is dropped

	trigMu         sync.Mutex // guards the two below: the replies of the last second, and whether they are paused
	replyTimes     []time.Time
	triggersPaused bool
}

const (
	maxPendingReplies = 32
	maxMessage        = 1 << 20
)

// frame is what the page gets for data from the device: the time in milliseconds (8 bytes), the
// length of the raw bytes (4), the raw bytes, then the same data converted to UTF-8.
func frame(at time.Time, raw, text []byte) []byte {
	out := make([]byte, 12, 12+len(raw)+len(text))
	binary.BigEndian.PutUint64(out, uint64(at.UnixMilli()))
	binary.BigEndian.PutUint32(out[8:], uint32(len(raw)))
	return append(append(out, raw...), text...)
}

func (t *termSession) sendJSON(v any) {
	data, _ := json.Marshal(v)
	t.c.Write(t.ctx, websocket.MessageText, data)
}

func (t *termSession) sendState(state, message string) {
	t.sendJSON(map[string]string{"type": "state", "state": state, "message": message})
}

// open (re)opens the port of profile.
func (t *termSession) open(profile Profile) { t.openPort(profile, false) }

// openPort opens the port of profile and says whether it did. A reconnecting attempt is quiet: it says
// nothing while it fails, and does not stop the reconnecting loop it belongs to.
func (t *termSession) openPort(profile Profile, reconnecting bool) bool {
	if !reconnecting {
		t.close()
	}
	if err := profile.Normalize(); err != nil {
		t.sendState("failed", err.Error())
		return false
	}
	if !reconnecting {
		t.sendState("connecting", "")
	}
	if !t.srv.claimPort(profile.Device, t) {
		if !reconnecting {
			t.sendState("failed", profile.Device+" is already open in another tab")
		}
		return false
	}
	port, err := openPort(profile)
	if err != nil {
		t.srv.releasePort(profile.Device, t)
		if !reconnecting {
			t.sendState("failed", err.Error())
		}
		return false
	}
	// The log is created only now, so a port that does not open leaves no file behind.
	existing := profile.LogExisting
	if existing == "ask" {
		existing = "append"
		if logExists(profile) && !reconnecting { // no question to answer while the page is not waiting
			existing = t.askAboutLog(profile)
		}
	}
	log, logErr := OpenLog(profile, existing)
	t.mu.Lock()
	t.gen++
	gen := t.gen
	t.port, t.profile, t.log, t.codec = port, profile, log, NewCodec(profile.Charset)
	t.triggers, _ = parseTriggers(profile.Triggers) // checked by Normalize
	t.runCtx, t.runStop = context.WithCancel(t.ctx)
	t.startWriter(profile, t.runCtx)
	t.trigMu.Lock()
	t.replyTimes, t.triggersPaused = nil, false
	t.trigMu.Unlock()
	t.rx.Store(0)
	t.tx.Store(0)
	t.dtr, t.rts = asserted(profile.DTR), asserted(profile.RTS)
	t.mu.Unlock()
	t.sendState("connected", "")
	t.sendJSON(map[string]any{"type": "logging", "on": log != nil})
	if logErr != nil {
		t.sendJSON(map[string]string{"type": "notice", "text": "log not written: " + logErr.Error()})
	}
	graph, _ := parseGraphSeries(profile.GraphSeries) // checked by Normalize
	go t.read(gen, port, t.codec, t.triggers, graph, t.runCtx)
	go t.watchLines(gen, port, asserted(profile.DTR), asserted(profile.RTS))
	go t.watchCounters(gen)
	return true
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

func (t *termSession) read(gen int, port serial.Port, codec *Codec, triggers []trigger, graph []graphSpec, ctx context.Context) {
	buf := make([]byte, 4096)
	var lines lineSplitter // the lines of the data, for the graph
	var window []byte      // the end of what the device sent, to match triggers across chunks
	for {
		n, err := port.Read(buf)
		if n > 0 {
			at := time.Now()
			raw := append([]byte(nil), buf[:n]...)
			text := codec.FromDevice(raw)
			t.rx.Add(int64(n))
			t.logger().Write(dirRx, text)
			t.stopLogOnError()
			if t.c.Write(t.ctx, websocket.MessageBinary, frame(at, raw, text)) != nil {
				return
			}
			window = t.match(ctx, triggers, window, text)
			if len(graph) > 0 {
				t.sendPoints(at, lines.feed(text), graph)
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
				t.mu.Lock()
				profile := t.profile
				t.mu.Unlock()
				if profile.Reconnect {
					t.sendState("reconnecting", message)
					t.startReconnect(profile)
				} else {
					t.sendState("ended", message)
				}
			}
			return
		}
	}
}

// stopLogOnError closes a log that cannot be written any more and says so, once.
func (t *termSession) stopLogOnError() {
	log := t.logger()
	err := log.Err()
	if err == nil {
		return
	}
	t.mu.Lock()
	if t.log != log { // already stopped
		t.mu.Unlock()
		return
	}
	t.log = nil
	t.mu.Unlock()
	log.Close()
	t.sendJSON(map[string]any{"type": "logging", "on": false})
	t.sendJSON(map[string]string{"type": "notice", "text": "log stopped: " + err.Error()})
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
	port, log, stop, device, runStop, reconnect := t.port, t.log, t.stopRun, t.profile.Device, t.runStop, t.reconnectStop
	t.port, t.log, t.stopRun, t.runStop, t.reconnectStop = nil, nil, nil, nil, nil
	if port != nil {
		t.gen++
	}
	t.mu.Unlock()
	if stop != nil {
		stop()
	}
	if runStop != nil {
		runStop()
	}
	if reconnect != nil {
		reconnect()
	}
	if port != nil {
		port.Close()
		t.srv.releasePort(device, t)
	}
	log.Close()
	return port != nil
}

// write sends text to the device, in its character set, and records it as sent.
func (t *termSession) write(data []byte) { t.send(data, false) }

// writeRaw sends bytes to the device as they are.
func (t *termSession) writeRaw(data []byte) { t.send(data, true) }

func (t *termSession) send(data []byte, raw bool) {
	t.mu.Lock()
	port, log, codec := t.port, t.log, t.codec
	t.mu.Unlock()
	if port == nil {
		return
	}
	out := data
	if !raw {
		out = codec.ToDevice(data)
	}
	t.mu.Lock()
	queue, ctx := t.outQ, t.runCtx
	t.mu.Unlock()
	if queue != nil { // slow send: the writer sends it
		select {
		case queue <- outItem{out, data}:
		case <-ctx.Done():
		}
		return
	}
	t.writeMu.Lock()
	defer t.writeMu.Unlock()
	if n, err := port.Write(out); err == nil {
		t.tx.Add(int64(n))
		log.Write(dirTx, data)
	}
}

// terminal serves one tab. The client sends JSON: open, close, input, raw, steps, flush, mark, break, script (by name), stop.
// The server sends binary frames of port output (see frame) and JSON: state (connecting, connected, failed,
// closed, ended), tx (a line a script sent), script (running, done, stopped, error), break, notice, counters, trigger.
// The port can be closed and opened again over the same connection, so the tab keeps its screen.
func (s *Server) terminal(w http.ResponseWriter, r *http.Request) {
	c, err := websocket.Accept(w, r, &websocket.AcceptOptions{Subprotocols: []string{wsProtocol}})
	if err != nil {
		return
	}
	defer c.CloseNow()
	c.SetReadLimit(maxMessage) // an "open" message carries the whole profile, with a custom bell sound
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
		case "raw":
			t.writeRaw(m.Bytes)
		case "steps":
			t.runCmd(m.Data)
		case "resetcounters":
			t.resetCounters()
		case "break":
			t.sendBreak()
		case "script":
			t.runScript(m.Name)
		case "stop":
			t.stopScript()
		case "flush":
			t.flushOut()
		case "mark":
			t.logger().Mark(m.Data)
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

// sendPoints tells the page the numbers the lines of a chunk gave, for its graph.
func (t *termSession) sendPoints(at time.Time, lines []string, graph []graphSpec) {
	var values []graphValue
	for _, line := range lines {
		values = append(values, extractValues(line, graph)...)
	}
	if len(values) > 0 {
		t.sendJSON(map[string]any{"type": "points", "t": at.UnixMilli(), "values": values})
	}
}
