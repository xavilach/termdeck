package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/coder/websocket"

	"termdeck/internal/serial"
)

var enterBytes = map[string]string{"cr": "\r", "lf": "\n", "crlf": "\r\n"}

type clientMessage struct {
	Type    string       `json:"type"`
	Profile Profile      `json:"profile"`
	View    viewSettings `json:"view"`
	Data    string       `json:"data"`
	Answer  string       `json:"answer"` // reply to a question about an existing log: append, overwrite or none
	Line    string       `json:"line"`   // control line to set: dtr or rts
	Value   bool         `json:"value"`  // asserted or not
	Name    string       `json:"name"`   // script to run
	Bytes   []byte       `json:"bytes"`  // raw bytes to send, as they are
	Action  string       `json:"action"` // xmodem: send, receive or cancel
	OneK    bool         `json:"oneK"`   // xmodem: blocks of 1024 bytes instead of 128
	Proto   string       `json:"proto"`  // xmodem (the default) or ymodem: the name and the exact size travel with the file

	Format    string `json:"format"`    // send: text, hex, dec, bin or cmd
	Check     string `json:"check"`     // send: the check added after the data
	End       string `json:"end"`       // send: none, cr, lf, crlf, enter or custom
	EndCustom string `json:"endCustom"` // send: the end when it is custom, written like text
	Ms        int    `json:"ms"`        // auto: the pause between two sends
	Job       int    `json:"job"`       // stopjob: which job (0: all of them)
}

// termSession is one tab: its port, its records and what it sends. It lives on the server, so a reload of the
// page attaches to it again and finds everything as it was. A session left without a page ends after a while.
type termSession struct {
	srv  *Server
	id   string
	ctx  context.Context // the life of the session
	stop context.CancelFunc

	connMu  sync.Mutex // guards the three below
	conn    *websocket.Conn
	connCtx context.Context
	orphan  *time.Timer // ends the session when no page comes back

	mu         sync.Mutex // guards everything below
	gen        int        // changes with each open and close, so a stale reader knows it is stale
	port       serial.Port
	profile    Profile
	hasProfile bool
	state      string // as last told to the page: connecting, connected, failed, closed, ended, reconnecting
	marks      int
	meters     meters
	dtr        bool // the lines as we last set them
	rts        bool
	log        *Logger
	jobs       map[int]*job // what runs over time: scripts, automatic sends, files, transfers
	jobSeq     int
	jobsSent   time.Time

	writeMu sync.Mutex // one command at a time reaches the port

	opMu          sync.Mutex      // one open at a time
	answers       chan string     // the page's answer about an existing log file
	codec         *Codec          // device character set; nil for UTF-8
	rx, tx        atomic.Int64    // bytes received and sent since the port opened
	rules         []rule          // what the session does by itself, from its settings
	framer        *frameParser    // the frames of what the device sends: counted, and for the rules on bad frames
	lastRx        time.Time       // when the device last sent something, for the idle rules
	idleFired     map[int]bool    // the idle rules that fired since then
	replyMu       sync.Mutex      // trigger replies go out one at a time, in order
	pending       atomic.Int32    // trigger replies waiting or running
	runCtx        context.Context // ends when the port closes: for steps typed in Cmd and trigger replies
	runStop       context.CancelFunc
	tun           *tunnel            // set while the session is a tunnel between two ports
	xfer          *xfer              // the file transfer that is running, if any
	reconnectStop context.CancelFunc // ends the loop that opens the port again
	outQ          chan outItem       // set when the profile has send delays: what the writer sends
	flushed       atomic.Int64       // changes when the waiting data is dropped

	expMu  sync.Mutex // guards expBuf
	expBuf []byte     // what the device sent since a script last sent a line, for the expect step

	trigMu         sync.Mutex // guards the two below: the replies of the last second, and whether they are paused
	replyTimes     []time.Time
	triggersPaused bool

	streamMu sync.Mutex // guards the records and the renderers; held while screen output is sent, to keep its order
	hist     history
	view     viewSettings
	viewSet  bool      // the page told its view; until then the view follows the settings
	screen   *renderer // the tab's view of the records
	lines    *renderer // the received lines as the session cuts them, for the graph and the rules
}

const (
	maxPendingReplies = 32
	maxMessage        = 16 << 20 // an XMODEM file travels in one message
)

// sessionGrace is how long a session waits for its page to come back (a reload) before it ends.
var sessionGrace = time.Minute

func newSession(srv *Server, id string) *termSession {
	ctx, stop := context.WithCancel(context.Background())
	t := &termSession{srv: srv, id: id, ctx: ctx, stop: stop, answers: make(chan string, 1), state: "closed"}
	t.resetMeters()
	t.view = viewOf(Profile{})
	t.screen = t.newScreen()
	t.lines = t.newLines()
	go t.tick()
	return t
}

// sessionFor finds the session of a tab, or starts one.
func (s *Server) sessionFor(id string) *termSession {
	s.sessionsMu.Lock()
	defer s.sessionsMu.Unlock()
	if t := s.sessions[id]; t != nil {
		return t
	}
	t := newSession(s, id)
	s.sessions[id] = t
	return t
}

// endSessions ends every session: termdeck is stopping.
func (s *Server) endSessions() {
	s.sessionsMu.Lock()
	all := make([]*termSession, 0, len(s.sessions))
	for _, t := range s.sessions {
		all = append(all, t)
	}
	s.sessionsMu.Unlock()
	for _, t := range all {
		t.end()
	}
}

// end closes the port and forgets the session.
func (t *termSession) end() {
	t.srv.sessionsMu.Lock()
	if t.srv.sessions[t.id] == t {
		delete(t.srv.sessions, t.id)
	}
	t.srv.sessionsMu.Unlock()
	t.close()
	t.stop()
	t.connMu.Lock()
	conn := t.conn
	t.conn = nil
	t.connMu.Unlock()
	if conn != nil {
		conn.Close(websocket.StatusNormalClosure, "the tab was closed")
	}
}

// attach makes c the page of the session; a page that had it before is let go.
func (t *termSession) attach(c *websocket.Conn, ctx context.Context) {
	t.connMu.Lock()
	old := t.conn
	t.conn, t.connCtx = c, ctx
	if t.orphan != nil {
		t.orphan.Stop()
		t.orphan = nil
	}
	t.connMu.Unlock()
	if old != nil {
		old.Close(websocket.StatusPolicyViolation, "the tab was opened again in another page")
	}
}

// detach notes that c is gone; the session ends if no page comes back in time.
func (t *termSession) detach(c *websocket.Conn) {
	t.connMu.Lock()
	defer t.connMu.Unlock()
	if t.conn != c {
		return
	}
	t.conn = nil
	t.orphan = time.AfterFunc(sessionGrace, func() {
		t.connMu.Lock()
		alone := t.conn == nil
		t.connMu.Unlock()
		if alone {
			t.end()
		}
	})
}

// write sends a message to the page, when there is one.
func (t *termSession) send(kind websocket.MessageType, data []byte) {
	t.connMu.Lock()
	c, ctx := t.conn, t.connCtx
	t.connMu.Unlock()
	if c != nil {
		c.Write(ctx, kind, data)
	}
}

func (t *termSession) sendJSON(v any) {
	data, _ := json.Marshal(v)
	t.send(websocket.MessageText, data)
}

// sendState tells the page the state of the port, and notes it in the stream.
func (t *termSession) sendState(state, message string) {
	t.mu.Lock()
	was := t.state
	t.state = state
	profile := t.profile
	t.mu.Unlock()
	defer func() {
		if was == "connected" && state != "connected" {
			t.fire("disconnect", "", message)
		}
	}()
	t.sendJSON(map[string]string{"type": "state", "state": state, "message": message})
	switch state {
	case "failed", "ended":
		if message == "" {
			message = state
		}
		t.note(message)
	case "reconnecting":
		t.note(message + ": opening the port again as soon as it is back")
	case "connected":
		t.note("connected to " + summary(profile))
	case "closed":
		t.note("port closed")
	}
}

// summary is the port and its settings in a few words: COM3 115200 8N1.
func summary(p Profile) string {
	text := p.Device
	if p.Peer != nil {
		text += " ⇄ " + p.Peer.Device
	}
	parity := "N"
	if p.Parity != "" {
		parity = string(p.Parity[0] - 'a' + 'A')
	}
	text += fmt.Sprintf(" %d %d%s%s", p.Baud, p.DataBits, parity, p.StopBits)
	switch p.Flow {
	case "rtscts":
		text += " RTS/CTS"
	case "xonxoff":
		text += " XON/XOFF"
	case "dsrdtr":
		text += " DSR/DTR"
	}
	return text
}

// notice tells the page something went wrong or happened, in the stream.
func (t *termSession) notice(text string) { t.note(text) }

// note puts a message of termdeck in the stream.
func (t *termSession) note(text string) {
	t.emit(record{at: time.Now(), src: srcNote, text: []byte(text)})
}

// ---------------------------------------------------------------- the stream

// emit keeps a record, writes it to the log and shows it on the page.
func (t *termSession) emit(rec record) {
	t.streamMu.Lock()
	defer t.streamMu.Unlock()
	t.hist.add(rec)
	t.logger().Record(rec)
	t.lines.add(rec)
	t.lines.take()
	t.screen.add(rec)
	t.flushScreen()
}

// flushScreen sends what the screen renderer made. The caller holds streamMu.
func (t *termSession) flushScreen() {
	if out := t.screen.take(); len(out) > 0 {
		t.send(websocket.MessageBinary, out)
	}
}

// tick shows the lines that waited long enough for their end (a prompt), on the screen and in the log.
func (t *termSession) tick() {
	for {
		select {
		case <-t.ctx.Done():
			return
		case now := <-time.After(100 * time.Millisecond):
			t.idleRules(now)
			t.streamMu.Lock()
			t.screen.idle(now)
			t.lines.idle(now)
			t.lines.take()
			t.flushScreen()
			t.streamMu.Unlock()
			t.logger().idle(now)
		}
	}
}

func (t *termSession) newScreen() *renderer {
	t.mu.Lock()
	profile := t.profile
	t.mu.Unlock()
	if !t.viewSet {
		t.view = viewOf(profile)
	}
	r := newRenderer(screenFormat(profile, t.view))
	return r
}

func (t *termSession) newLines() *renderer {
	t.mu.Lock()
	profile := t.profile
	t.mu.Unlock()
	rules, _ := parseRules(profile.Rules) // checked by Normalize
	r := newRenderer(linesFormat(profile))
	r.onLine = func(src, line string, at time.Time) {
		if src != srcRx || r.replaying {
			return
		}
		if values := graphValues(rules, line); len(values) > 0 {
			t.sendJSON(map[string]any{"type": "points", "t": at.UnixMilli(), "values": values})
		}
		t.lineRules(rules, line)
	}
	return r
}

// maxReplay is how much of the screen a redraw sends at most: more than the terminal keeps anyway.
const maxReplay = 4 << 20

// redraw shows the records again from the start, with the current view and settings.
func (t *termSession) redraw() {
	t.streamMu.Lock()
	defer t.streamMu.Unlock()
	t.screen = t.newScreen()
	t.lines = t.newLines()
	t.screen.replaying, t.lines.replaying = true, true
	for _, rec := range t.hist.records {
		t.screen.add(rec)
		t.lines.add(rec)
	}
	t.lines.take()
	t.lines.replaying = false
	t.screen.idle(time.Now())
	t.screen.replaying = false
	out := t.screen.take()
	if len(out) > maxReplay {
		out = out[len(out)-maxReplay:]
		for i, b := range out {
			if b == '\n' {
				out = append([]byte("\x1b[0m"), out[i+1:]...)
				break
			}
		}
	}
	t.sendJSON(map[string]string{"type": "reset"})
	if len(out) > 0 {
		t.send(websocket.MessageBinary, out)
	}
	t.sendJSON(map[string]string{"type": "replayed"})
}

// clearRecords forgets the records and the counters: the page cleared its screen.
func (t *termSession) clearRecords() {
	t.streamMu.Lock()
	t.hist.clear()
	t.screen = t.newScreen()
	t.streamMu.Unlock()
	t.resetCounters()
}

// setProfile changes the settings that show the records (and that the next open uses).
func (t *termSession) setProfile(p Profile) {
	if err := p.Normalize(); err != nil {
		t.notice("settings not applied: " + err.Error())
		return
	}
	t.mu.Lock()
	t.profile, t.hasProfile = p, true
	t.mu.Unlock()
	t.sendRuleNames()
}

// sendRuleNames tells the page the buttons and the graph series the rules of the settings make.
func (t *termSession) sendRuleNames() {
	t.mu.Lock()
	rules, _ := parseRules(t.profile.Rules) // checked by Normalize
	t.mu.Unlock()
	buttons, keys, series := ruleNames(rules)
	t.sendJSON(map[string]any{"type": "rules", "buttons": append([]string{}, buttons...), "keys": append([]string{}, keys...), "series": append([]string{}, series...)})
}

// sentText is how a line that was sent shows: the text, without its line end, with control characters escaped.
func sentText(data []byte) []byte {
	for len(data) > 0 && (data[len(data)-1] == '\n' || data[len(data)-1] == '\r') {
		data = data[:len(data)-1]
	}
	return []byte(escapeText(data))
}

// recordSent puts what went out in the stream, with how it shows.
func (t *termSession) recordSent(out, shown []byte) {
	t.emit(record{at: time.Now(), src: srcTx, raw: out, text: shown})
}

// ---------------------------------------------------------------- the port

// open (re)opens the port of profile.
func (t *termSession) open(profile Profile) { t.openPort(profile, false) }

// openPort opens the port of profile and says whether it did. A reconnecting attempt is quiet: it says
// nothing while it fails, and does not stop the reconnecting loop it belongs to.
func (t *termSession) openPort(profile Profile, reconnecting bool) bool {
	if profile.Peer != nil {
		return t.openTunnel(profile, reconnecting)
	}
	if !reconnecting {
		t.close()
	}
	if err := profile.Normalize(); err != nil {
		t.sendState("failed", err.Error())
		return false
	}
	if !reconnecting {
		t.useProfile(profile)
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
	if profile.RTSMode == "high" || profile.RTSMode == "low" {
		port.SetRTS(rs485Idle(profile.RTSMode)) // listening until something is sent
	}
	t.rules, _ = parseRules(profile.Rules) // checked by Normalize
	t.framer, t.lastRx, t.idleFired = nil, time.Now(), map[int]bool{}
	if spec, err := parseFrameSpec(profile.FrameSpec); err == nil && spec != nil {
		t.framer = &frameParser{spec: spec} // the frames are counted, and the rules on bad frames fire
	}
	t.resetMeters()
	t.runCtx, t.runStop = context.WithCancel(t.ctx)
	t.startWriter(profile, t.runCtx)
	t.trigMu.Lock()
	t.replyTimes, t.triggersPaused = nil, false
	t.trigMu.Unlock()
	t.rx.Store(0)
	t.tx.Store(0)
	t.expectClear()
	t.dtr, t.rts = asserted(profile.DTR), asserted(profile.RTS)
	t.mu.Unlock()
	t.sendState("connected", "")
	t.sendJSON(map[string]any{"type": "logging", "on": log != nil})
	if logErr != nil {
		t.notice("log not written: " + logErr.Error())
	}
	go t.read(gen, port, t.codec, t.runCtx)
	go t.watchLines(gen, port, asserted(profile.DTR), asserted(profile.RTS))
	go t.watchCounters(gen)
	t.startRules()
	return true
}

// useProfile takes the settings of an open, and shows the records again when they show differently now.
func (t *termSession) useProfile(p Profile) {
	t.mu.Lock()
	before, _ := json.Marshal(t.profile)
	t.profile, t.hasProfile = p, true
	t.mu.Unlock()
	t.sendRuleNames()
	if after, _ := json.Marshal(p); string(after) != string(before) {
		t.redraw()
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

func (t *termSession) read(gen int, port serial.Port, codec *Codec, ctx context.Context) {
	buf := make([]byte, 4096)
	windows := map[int][]byte{} // the end of what the device sent, to match the rules across chunks
	for {
		n, err := port.Read(buf)
		if n > 0 && t.feedTransfer(buf[:n]) {
			t.rx.Add(int64(n)) // a file transfer is reading: the data is not for the page
		} else if n > 0 {
			raw := append([]byte(nil), buf[:n]...)
			text := codec.FromDevice(raw)
			t.rx.Add(int64(n))
			t.expectFeed(text)
			t.emit(record{at: time.Now(), src: srcRx, raw: raw, text: text})
			t.stopLogOnError()
			t.matchRules(ctx, windows, text)
			t.frameRules(raw)
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
	t.notice("log stopped: " + err.Error())
}

func (t *termSession) logger() *Logger {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.log
}

// setLogging starts or stops the log of the open port, whatever it was when the port opened.
func (t *termSession) setLogging(on bool) {
	t.mu.Lock()
	open, profile, current := t.port != nil || t.tun != nil, t.profile, t.log
	t.mu.Unlock()
	switch {
	case !open:
		t.notice("open the port before logging")
	case !on:
		t.mu.Lock()
		t.log = nil
		t.mu.Unlock()
		current.Close()
		t.sendJSON(map[string]any{"type": "logging", "on": false})
	case current != nil:
		// already logging
	case profile.LogPath == "":
		t.notice("set a log file in the settings of this session first")
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
			t.notice("log not written: " + err.Error())
			return
		}
		t.mu.Lock()
		t.log = log
		t.mu.Unlock()
		t.sendJSON(map[string]any{"type": "logging", "on": log != nil})
	}
}

// close stops the jobs and closes the port and the log. It is safe to call when nothing is open.
func (t *termSession) close() bool {
	t.mu.Lock()
	port, log, device, runStop, reconnect, tun := t.port, t.log, t.profile.Device, t.runStop, t.reconnectStop, t.tun
	t.port, t.log, t.runStop, t.reconnectStop, t.tun = nil, nil, nil, nil, nil
	if port != nil || tun != nil {
		t.gen++
	}
	t.mu.Unlock()
	if runStop != nil { // the jobs and the replies end with the port
		runStop()
	}
	if reconnect != nil {
		reconnect()
	}
	if port != nil {
		port.Close()
		t.srv.releasePort(device, t)
	}
	if tun != nil {
		t.closeTunnel(tun)
	}
	log.Close()
	return port != nil || tun != nil
}

// write sends text to the device, in its character set, and records it as sent.
func (t *termSession) write(text []byte) {
	t.mu.Lock()
	codec := t.codec
	t.mu.Unlock()
	t.sendData(codec.ToDevice(text), sentText(text))
}

// writeRaw sends bytes to the device as they are.
func (t *termSession) writeRaw(data []byte) { t.sendData(data, []byte(hexText(data))) }

// sendData sends bytes to the device (through the slow writer when the session has delays), and records
// them as sent, shown as shown (nil: not recorded).
func (t *termSession) sendData(out, shown []byte) { t.sendDataWait(out, shown, false) }

// sendDataWait is sendData that can wait until a slow writer has written the bytes (or dropped them).
func (t *termSession) sendDataWait(out, shown []byte, wait bool) {
	t.mu.Lock()
	port, queue, ctx := t.port, t.outQ, t.runCtx
	t.mu.Unlock()
	if port == nil {
		return
	}
	if queue != nil { // slow send: the writer sends it
		item := outItem{out: out, shown: shown}
		if wait {
			item.done = make(chan struct{})
		}
		select {
		case queue <- item:
		case <-ctx.Done():
			return
		}
		if wait {
			select {
			case <-item.done:
			case <-ctx.Done():
			}
		}
		return
	}
	t.writeMu.Lock()
	mode, delay := t.rs485()
	n, err := transmit(port, mode, delay, func() (int, error) { return port.Write(out) })
	t.writeMu.Unlock()
	if err == nil {
		t.tx.Add(int64(n))
		if shown != nil { // a file is not shown, a note says it was sent
			t.recordSent(out, shown)
		}
	}
}

// terminal serves the page of one tab: GET /api/terminal?id=<the tab's id>. The page sends JSON: hello (its
// settings and view: the session sends its state and shows its records again), view, profile, open, close,
// send (the send bar: format, data, check, end), button (of a rule, by name), auto (the send bar, every ms),
// file (a file to send), raw (bytes), flush, mark, clear, note, xmodem, break, script, stop (every job),
// stopjob, log, line, end (the tab is closed).
// The session sends its screen as binary messages (text for the terminal) and JSON: session, state, reset and
// replayed (around a redraw), logging, logexists, lines, counters, points, notify, jobs, xfer.
func (s *Server) terminal(w http.ResponseWriter, r *http.Request) {
	c, err := websocket.Accept(w, r, &websocket.AcceptOptions{Subprotocols: []string{wsProtocol}})
	if err != nil {
		return
	}
	defer c.CloseNow()
	c.SetReadLimit(maxMessage) // an "open" message carries the whole profile, with a custom bell sound
	id := r.URL.Query().Get("id")
	if id == "" {
		id = randomToken(12)
	}
	t := s.sessionFor(id)
	t.attach(c, r.Context())
	defer t.detach(c)
	for {
		_, data, err := c.Read(r.Context())
		if err != nil {
			return
		}
		var m clientMessage
		if json.Unmarshal(data, &m) != nil {
			continue
		}
		if handle := messageHandlers[m.Type]; handle != nil {
			handle(t, m)
		}
	}
}

// hello answers a page that attached: the settings and view it brings (unless the port is open with others),
// the state of the session, and its records.
func (t *termSession) hello(m clientMessage) {
	t.mu.Lock()
	busy := t.port != nil || t.tun != nil || t.reconnectStop != nil
	keep := busy || (t.hasProfile && m.Profile.Device == "")
	t.mu.Unlock()
	if !keep {
		t.setProfile(m.Profile)
	}
	t.streamMu.Lock()
	t.view, t.viewSet = m.View, true
	t.streamMu.Unlock()
	t.mu.Lock()
	info := map[string]any{"type": "session", "state": t.state, "logging": t.log != nil, "profile": t.profile}
	t.mu.Unlock()
	info["jobs"] = t.listJobs()
	// the counters go first: the page opens the port when it reads the session, and the counters of the
	// port that opens must not be overtaken by these
	t.sendJSON(t.meterMessage(0, 0))
	t.sendJSON(info)
	t.sendRuleNames()
	t.redraw()
}

// mark puts a numbered mark in the stream, to find a moment again.
func (t *termSession) mark() { t.markWith("") }

// markWith puts a numbered mark in the stream, with a few words after it when there are some.
func (t *termSession) markWith(words string) {
	t.mu.Lock()
	t.marks++
	n := t.marks
	t.mu.Unlock()
	text := fmt.Sprintf("mark %d at %s", n, time.Now().Format("15:04:05"))
	if words != "" {
		text += ": " + words
	}
	t.note(text)
}

// rs485 is the direction control of the open port, and its delay.
func (t *termSession) rs485() (string, time.Duration) {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.profile.RTSMode, time.Duration(t.profile.RTSDelayMs) * time.Millisecond
}

// What the session does for each kind of message from the page.
var messageHandlers = map[string]func(t *termSession, m clientMessage){
	"hello": func(t *termSession, m clientMessage) { t.hello(m) },
	"view": func(t *termSession, m clientMessage) {
		t.streamMu.Lock()
		t.view, t.viewSet = m.View, true
		t.streamMu.Unlock()
		t.redraw()
	},
	"profile": func(t *termSession, m clientMessage) { t.setProfile(m.Profile); t.redraw() },
	"open": func(t *termSession, m clientMessage) {
		go func() { // in the background: the page may have to answer a question first
			t.opMu.Lock()
			defer t.opMu.Unlock()
			t.open(m.Profile)
		}()
	},
	"loganswer": func(t *termSession, m clientMessage) {
		select {
		case t.answers <- m.Answer:
		default:
		}
	},
	"close": func(t *termSession, m clientMessage) {
		t.close()
		t.sendState("closed", "port closed")
	},
	"end":           func(t *termSession, m clientMessage) { go t.end() },
	"send":          func(t *termSession, m clientMessage) { t.sendBar(m) },
	"button":        func(t *termSession, m clientMessage) { t.pressButton(m.Name) },
	"key":           func(t *termSession, m clientMessage) { t.fire("key", normalizeKey(m.Name), "") },
	"raw":           func(t *termSession, m clientMessage) { t.writeRaw(m.Bytes) },
	"resetcounters": func(t *termSession, m clientMessage) { t.resetCounters() },
	"clear":         func(t *termSession, m clientMessage) { t.clearRecords() },
	"note":          func(t *termSession, m clientMessage) { t.note(m.Data) },
	"break":         func(t *termSession, m clientMessage) { t.sendBreak(250 * time.Millisecond) },
	"script":        func(t *termSession, m clientMessage) { t.runScript(m.Name) },
	"stop":          func(t *termSession, m clientMessage) { t.stopJob(0) },
	"stopjob":       func(t *termSession, m clientMessage) { t.stopJob(m.Job) },
	"auto":          func(t *termSession, m clientMessage) { t.startAuto(m) },
	"file":          func(t *termSession, m clientMessage) { t.startFile(m.Name, m.Bytes) },
	"flush":         func(t *termSession, m clientMessage) { t.flushOut() },
	"xmodem": func(t *termSession, m clientMessage) {
		if m.Action == "cancel" {
			t.cancelTransfer()
		} else {
			t.startXmodem(m, xmodemTimesInUse)
		}
	},
	"mark": func(t *termSession, m clientMessage) { t.mark() },
	"log": func(t *termSession, m clientMessage) {
		go func() {
			t.opMu.Lock()
			defer t.opMu.Unlock()
			t.setLogging(m.Value)
		}()
	},
	"line": func(t *termSession, m clientMessage) { t.setLine(m.Line, m.Value) },
}
