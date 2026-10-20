package main

import (
	"bytes"
	"fmt"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

// The renderer turns the records of a session (see stream.go) into text: for the screen of a tab, with
// colours, as its view says, and for the log file, plain, as the log settings say. It is one renderer with
// a format, so the screen and the log cut lines, put times and markers, and show bytes the same way.

// viewSettings is how a tab shows the records; the receive bar changes it.
type viewSettings struct {
	Mode     string `json:"mode"`     // text, hex, dec, bin or frames
	Stamps   bool   `json:"stamps"`   // each line starts with the time
	TimeMode string `json:"timeMode"` // clock or delta (since the line before)
	Pause    int    `json:"pause"`    // a new line after this many milliseconds without data; 0 never
	Ctrl     bool   `json:"ctrl"`     // control characters shown as <CR>, <LF>...
	Filter   string `json:"filter"`   // show only the lines that match; ! in front hides them
	NL       string `json:"nl"`       // what ends a line: terminal, cr, lf, crlf, custom or none
	NLCustom string `json:"nlCustom"` // the sequence for custom, written with \r \n \t \xNN
}

// viewOf is the view a session starts with.
func viewOf(p Profile) viewSettings {
	return viewSettings{Mode: p.View, Stamps: p.Timestamps, TimeMode: p.TimeMode, Pause: p.LinePauseMs, Ctrl: p.ShowControl,
		Filter: p.Filter, NL: p.NewLine, NLCustom: p.NewLineCustom}
}

// format is everything the renderer needs to know besides the records.
type format struct {
	view       viewSettings
	ansi       bool              // the screen: colours and CR LF; otherwise plain text and LF (the log)
	fullStamp  bool              // the time with its date
	markers    map[string]string // what starts the lines of each source
	showTx     bool              // what was sent is shown (local echo)
	printable  bool              // escape sequences and control characters are left out of the text
	implicitLF bool              // a CR received also moves down a line
	noMouse    bool              // the device cannot switch on mouse reports
	highlights []highlight
	filter     *lineFilter
	frames     *frameSpec
	framesErr  error
}

const lineIdle = 300 * time.Millisecond // an unfinished line (a prompt) is shown after this pause

// screenFormat is how a tab with the given view shows the records of a session.
func screenFormat(p Profile, view viewSettings) format {
	if view.Mode == "" {
		view.Mode = "text"
	}
	f := format{view: view, ansi: true, showTx: p.LocalEcho, implicitLF: p.ImplicitLF, noMouse: p.NoMouseReport,
		markers: map[string]string{srcRx: p.RxMarker, srcTx: p.TxMarker, srcA2B: "\x1b[32mA→B \x1b[0m", srcB2A: "\x1b[36mB→A \x1b[0m"}}
	f.highlights, _ = parseHighlights(p.Highlights) // checked by Normalize
	f.filter, _ = parseFilter(view.Filter)
	f.frames, f.framesErr = parseFrameSpec(p.FrameSpec)
	return f
}

// logFormat is how the log of a session writes the records.
func logFormat(p Profile) format {
	a2b, b2a := p.LogRxMarker, p.LogTxMarker
	if a2b == "" && b2a == "" {
		a2b, b2a = "A>B ", "B>A "
	}
	return format{view: viewSettings{Mode: "text", Stamps: p.LogTimestamps, NL: p.NewLine, NLCustom: p.NewLineCustom},
		fullStamp: true, showTx: true, printable: p.LogPrintable, implicitLF: p.ImplicitLF,
		markers: map[string]string{srcRx: p.LogRxMarker, srcTx: p.LogTxMarker, srcA2B: a2b, srcB2A: b2a}}
}

// linesFormat cuts the received text into lines as the session says, with nothing added: for the graph.
func linesFormat(p Profile) format {
	return format{view: viewSettings{Mode: "text", NL: p.NewLine, NLCustom: p.NewLineCustom}, implicitLF: p.ImplicitLF, markers: map[string]string{}}
}

// hexRow is a row of a number view that is not full yet.
type hexRow struct {
	src   string
	n     int
	chars []byte
	at    time.Time
}

type renderer struct {
	f   format
	out bytes.Buffer

	lineStart bool
	lastSrc   string
	lastRx    time.Time
	lastHead  time.Time
	afterCR   bool
	hex       *hexRow
	shaper    lineShaper
	pending   []byte // the start of a line not finished yet (line mode)
	pendingAt time.Time
	frames    *frameParser
	warned    bool
	replaying bool

	onLine   func(src, line string, at time.Time) // a received line is complete (line mode)
	onNotify func(line string)                    // a line matched a highlight that asks for a notification
}

func newRenderer(f format) *renderer {
	r := &renderer{f: f, lineStart: true, shaper: newLineShaper(f.view, f.ansi)}
	if f.frames != nil {
		r.frames = &frameParser{spec: f.frames}
	}
	return r
}

func (r *renderer) eol() string {
	if r.f.ansi {
		return "\r\n"
	}
	return "\n"
}

// take returns what was rendered since the last call.
func (r *renderer) take() []byte {
	out := append([]byte(nil), r.out.Bytes()...)
	r.out.Reset()
	return out
}

// add renders one record.
func (r *renderer) add(rec record) {
	switch rec.src {
	case srcRx, srcA2B, srcB2A:
		r.rx(rec)
	case srcTx:
		if r.f.showTx {
			r.tx(rec)
		}
	default:
		r.note(rec.text, rec.at)
	}
}

// idle finishes a line that has waited long enough for its end: a prompt shows, the log writes it.
func (r *renderer) idle(now time.Time) {
	if len(r.pending) > 0 && now.Sub(r.pendingAt) >= lineIdle {
		r.flushPending()
	}
}

// lineMode: lines are handled whole (filter, highlights, printable text, the lines of the graph).
func (r *renderer) lineMode() bool {
	return !r.f.ansi || r.f.filter != nil || len(r.f.highlights) > 0
}

func (r *renderer) head(src string, at time.Time) string {
	head := ""
	if r.f.view.Stamps {
		var text string
		switch {
		case r.f.view.TimeMode == "delta":
			delta := time.Duration(0)
			if !r.lastHead.IsZero() {
				delta = at.Sub(r.lastHead)
			}
			text = formatDelta(delta)
		case r.f.fullStamp:
			text = at.Format("2006-01-02 15:04:05.000")
		default:
			text = at.Format("15:04:05.000")
		}
		switch {
		case !r.f.ansi && r.f.fullStamp:
			head = text + " "
		case !r.f.ansi:
			head = "[" + text + "] "
		default:
			head = "\x1b[90m[" + text + "]\x1b[39m "
		}
		r.lastHead = at
	}
	return head + r.f.markers[src]
}

// formatDelta is the time since the line before, short: +87ms, +1.250s, +2m03.4s
func formatDelta(d time.Duration) string {
	ms := float64(d) / float64(time.Millisecond)
	switch {
	case ms < 1000:
		return fmt.Sprintf("+%dms", int(ms+0.5))
	case ms < 60000:
		return fmt.Sprintf("+%.3fs", ms/1000)
	}
	minutes := int(ms / 60000)
	return fmt.Sprintf("+%dm%04.1fs", minutes, (ms-float64(minutes)*60000)/1000)
}

// ---- numbers: each byte as a number, in rows, then the text of the row

type numberLook struct {
	width, per int
	show       func(byte) string
}

var numberLooks = map[string]numberLook{
	"hex": {3, 16, func(b byte) string { return fmt.Sprintf("%02x", b) }},
	"dec": {4, 16, func(b byte) string { return fmt.Sprintf("%03d", b) }},
	"bin": {9, 8, func(b byte) string { return fmt.Sprintf("%08b", b) }},
}

func (r *renderer) look() numberLook {
	if l, ok := numberLooks[r.f.view.Mode]; ok {
		return l
	}
	return numberLooks["hex"] // frames without a description
}

func (r *renderer) endRow() {
	l := r.look()
	r.out.WriteString(strings.Repeat(" ", (l.per-r.hex.n)*l.width) + " |" + string(r.hex.chars) + "|" + r.eol())
	r.hex = nil
	r.lineStart = true
}

// flushHex finishes a row that is not full before anything else is written.
func (r *renderer) flushHex() {
	if r.hex != nil {
		r.endRow()
	}
}

func (r *renderer) hexWrite(data []byte, src string, at time.Time) {
	l := r.look()
	if r.hex != nil && (r.hex.src != src || (r.f.view.Pause > 0 && at.Sub(r.hex.at) > time.Duration(r.f.view.Pause)*time.Millisecond)) {
		r.endRow()
	}
	for _, b := range data {
		if r.hex == nil {
			if !r.lineStart {
				r.out.WriteString(r.eol())
			}
			r.out.WriteString(r.head(src, at))
			r.hex = &hexRow{src: src, at: at}
			r.lineStart = false
		}
		text := l.show(b)
		r.out.WriteString(text + strings.Repeat(" ", l.width-len(text)))
		c := byte('.')
		if b >= 0x20 && b < 0x7f {
			c = b
		}
		r.hex.chars = append(r.hex.chars, c)
		if r.hex.n++; r.hex.n == l.per {
			r.endRow()
		}
	}
	if r.hex != nil {
		r.hex.at = at
	}
}

// ---- text

var mouseModes = regexp.MustCompile(`\x1b\[\?(?:9|100[0-6]|1015|1016)[hl]`)

// prepare is the received text before it is cut into lines: mouse modes the session refuses are removed,
// a CR gets its LF, and the "New line at" rule is applied.
func (r *renderer) prepare(text []byte) []byte {
	if r.f.noMouse && bytes.IndexByte(text, 0x1b) >= 0 {
		text = mouseModes.ReplaceAll(text, nil)
	}
	if r.f.implicitLF && r.shaper.terminal() && !r.f.view.Ctrl {
		text = r.addImplicitLF(text)
	}
	return r.shaper.shape(text)
}

// addImplicitLF: a CR that is not followed by LF becomes CR LF, across chunks too.
func (r *renderer) addImplicitLF(data []byte) []byte {
	out := make([]byte, 0, len(data)+4)
	if r.afterCR && len(data) > 0 && data[0] != '\n' {
		out = append(out, '\n')
	}
	r.afterCR = false
	for i, b := range data {
		out = append(out, b)
		if b == '\r' {
			if i+1 < len(data) {
				if data[i+1] != '\n' {
					out = append(out, '\n')
				}
			} else {
				r.afterCR = true
			}
		}
	}
	return out
}

// writeLines writes text where every line starts with its head.
func (r *renderer) writeLines(data []byte, src string, at time.Time) {
	marker := r.f.markers[src]
	if marker == "" && !r.f.view.Stamps {
		r.out.Write(data)
		if last := bytes.LastIndexByte(data, '\n'); last >= 0 {
			r.lineStart = last == len(data)-1
		} else if len(data) > 0 {
			r.lineStart = false
		}
		return
	}
	start := 0
	for i, b := range data {
		if r.lineStart {
			r.out.Write(data[start:i])
			start = i
			r.out.WriteString(r.head(src, at))
			r.lineStart = false
		}
		if b == '\n' {
			r.out.Write(data[start : i+1])
			start = i + 1
			r.lineStart = true
		}
	}
	r.out.Write(data[start:])
}

func (r *renderer) rx(rec record) {
	switch {
	case r.f.view.Mode == "frames":
		r.showFrames(rec)
	case r.f.view.Mode != "text":
		r.hexWrite(rec.raw, rec.src, rec.at)
	default:
		if r.lastSrc != "" && rec.src != r.lastSrc && !r.lineStart { // the other way: a line of its own
			r.flushPending()
			if !r.lineStart {
				r.out.WriteString(r.eol())
				r.lineStart = true
			}
		}
		r.lastSrc = rec.src
		text := r.prepare(rec.text)
		if pause := time.Duration(r.f.view.Pause) * time.Millisecond; pause > 0 && !r.lastRx.IsZero() && rec.at.Sub(r.lastRx) > pause {
			r.flushPending()
			if !r.lineStart {
				r.out.WriteString(r.eol()) // nothing came for a while: a new line
				r.lineStart = true
			}
		}
		if r.lineMode() {
			r.byLines(text, rec.src, rec.at)
		} else {
			r.writeLines(text, rec.src, rec.at)
		}
	}
	r.lastRx = rec.at
}

func (r *renderer) showFrames(rec record) {
	if r.frames == nil {
		if !r.warned {
			r.warned = true
			why := "no frame description"
			if r.f.framesErr != nil {
				why = r.f.framesErr.Error()
			}
			r.note([]byte("Frames view: "+why+" (set it on the Receive page, Frames, in the session settings)"), rec.at)
		}
		r.hexWrite(rec.raw, rec.src, rec.at)
		return
	}
	for _, row := range r.frames.feed(rec.raw) {
		line := frameLine(r.f.frames, row)
		switch {
		case !r.f.ansi:
		case row.noise:
			line = "\x1b[90m" + line + "\x1b[0m"
		case row.checked && !row.ok:
			line = "\x1b[31m" + line + "\x1b[0m"
		}
		r.writeLines([]byte(line+r.eol()), rec.src, rec.at)
	}
}

// byLines holds text until its line is complete (or has waited lineIdle, see idle), then shows each line.
func (r *renderer) byLines(text []byte, src string, at time.Time) {
	if len(r.pending) > 0 && at.Sub(r.pendingAt) >= lineIdle {
		r.flushPending()
	}
	r.pending = append(r.pending, text...)
	r.pendingAt = at
	for {
		end := bytes.IndexByte(r.pending, '\n')
		if end < 0 {
			break
		}
		line := append([]byte(nil), r.pending[:end+1]...)
		r.pending = r.pending[end+1:]
		r.line(line, src, at, true)
	}
}

func (r *renderer) flushPending() {
	if len(r.pending) == 0 {
		return
	}
	line := r.pending
	r.pending = nil
	r.line(line, r.lastSrc, r.pendingAt, false)
}

// line shows one line (or the start of one, waiting too long for its end) through the filter and the highlights.
func (r *renderer) line(line []byte, src string, at time.Time, complete bool) {
	if src == "" {
		src = srcRx
	}
	plain := plainLine(line)
	if complete && r.onLine != nil {
		r.onLine(src, plain, at)
	}
	if !r.f.filter.passes(plain) {
		return
	}
	if !r.f.ansi {
		body, end := splitLineEnd(line)
		if r.f.printable {
			body = printableOnly(body)
		}
		if !complete && len(end) == 0 {
			end = []byte(r.eol()) // the log writes a line that waited too long as a line
		}
		r.writeLines(append(body, end...), src, at)
		return
	}
	if !r.replaying && r.onNotify != nil && notifies(r.f.highlights, plain) {
		r.onNotify(plain)
	}
	sgr := styleOf(r.f.highlights, plain)
	if sgr == "" {
		r.writeLines(line, src, at)
		return
	}
	body, end := splitLineEnd(line)
	coloured := append([]byte("\x1b["+sgr+"m"), body...)
	coloured = append(append(coloured, "\x1b[0m"...), end...)
	r.writeLines(coloured, src, at)
}

// splitLineEnd cuts the CR and LF off the end of a line.
func splitLineEnd(line []byte) (body, end []byte) {
	n := len(line)
	for n > 0 && (line[n-1] == '\n' || line[n-1] == '\r') {
		n--
	}
	return append([]byte(nil), line[:n]...), line[n:]
}

var sgrSequence = regexp.MustCompile(`\x1b\[[0-9;?]*[ -/]*[@-~]`)

// plainLine is what a line looks like to the rules: without its colours and its line end, cut short.
func plainLine(line []byte) string {
	text := sgrSequence.ReplaceAllString(string(line), "")
	text = strings.TrimRight(text, "\r\n")
	if len(text) > 4096 {
		text = text[:4096]
		for !utf8.ValidString(text) {
			text = text[:len(text)-1]
		}
	}
	return text
}

// tx shows a line that was sent, as text or as numbers.
func (r *renderer) tx(rec record) {
	r.flushPending()
	if r.f.view.Mode != "text" {
		r.hexWrite(rec.raw, srcTx, rec.at)
		r.flushHex()
		return
	}
	if !r.lineStart {
		r.out.WriteString(r.eol())
	}
	r.out.WriteString(r.head(srcTx, rec.at))
	r.out.Write(rec.text)
	r.out.WriteString(r.eol())
	r.lineStart = true
}

// note writes a line that is neither received nor sent: a mark, a message of termdeck.
func (r *renderer) note(text []byte, at time.Time) {
	r.flushHex()
	r.flushPending()
	if !r.lineStart {
		r.out.WriteString(r.eol())
	}
	if r.f.ansi {
		r.out.WriteString("\x1b[90m[termdeck] " + string(text) + "\x1b[0m\r\n")
	} else {
		stamp := ""
		if r.f.view.Stamps {
			stamp = at.Format("2006-01-02 15:04:05.000 ")
		}
		r.out.WriteString(stamp + "---- " + string(text) + " ----\n")
	}
	r.lineStart = true
}
