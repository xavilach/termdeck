package main

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

const (
	dirRx = 0
	dirTx = 1

	// A line that is not finished (a prompt) is written after this pause.
	logIdle = 300 * time.Millisecond
)

var unsafeName = regexp.MustCompile(`[<>:"/\\|?*\x00-\x1f]`)

// logPath replaces {name}, {date} and {time} in a profile's log path.
func logPath(p Profile, now time.Time) string {
	return strings.NewReplacer(
		"{name}", unsafeName.ReplaceAllString(p.Name, "_"),
		"{date}", now.Format("2006-01-02"),
		"{time}", now.Format("150405"),
	).Replace(p.LogPath)
}

// Logger writes what was received and sent as lines, each starting with its marker.
type Logger struct {
	mu        sync.Mutex
	f         *os.File
	markers   [2]string
	printable bool // leave out escape sequences and control characters
	lines     [2][]byte
	timers    [2]*time.Timer
	stamps    bool         // start each line with the date and time it began
	began     [2]time.Time // when the line in progress of each direction began
	err       error        // the first error writing the file (the disk is full, say)

	path     string // the file, to start a new one when it is full
	size     int64  // bytes in it
	maxBytes int64  // 0 means no limit
	keep     int    // how many full files are kept: path.1 is the newest
}

// OpenLog opens the profile's log. It returns nil, nil when the profile has none or `existing` is "none".
// existing says what to do with a file that is already there: "append" or "overwrite" ("ask" is
// settled by the caller before). Call it only once the port is open, so a port that fails to open
// leaves no file.
func OpenLog(p Profile, existing string) (*Logger, error) {
	if p.LogPath == "" || existing == "none" {
		return nil, nil
	}
	path := logPath(p, time.Now())
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	flags := os.O_CREATE | os.O_WRONLY
	if existing == "overwrite" {
		flags |= os.O_TRUNC
	} else {
		flags |= os.O_APPEND
	}
	f, err := os.OpenFile(path, flags, 0o600)
	if err != nil {
		return nil, err
	}
	l := &Logger{f: f, markers: [2]string{p.LogRxMarker, p.LogTxMarker}, printable: p.LogPrintable, stamps: p.LogTimestamps,
		path: path, maxBytes: int64(p.LogMaxMB) * 1024 * 1024, keep: max(p.LogKeep, 1)}
	if info, err := f.Stat(); err == nil {
		l.size = info.Size()
	}
	if p.LogHeader {
		fmt.Fprintf(f, "=~=~=~=~= termdeck log %s: %s %d baud =~=~=~=~=\n", time.Now().Format("2006.01.02 15:04:05"), p.Device, p.Baud)
	}
	return l, nil
}

// logExists reports whether the profile's log file is already there.
func logExists(p Profile) bool {
	if p.LogPath == "" {
		return false
	}
	_, err := os.Stat(logPath(p, time.Now()))
	return err == nil
}

// printableOnly drops escape sequences and control characters (a backspace erases the character before it).
func printableOnly(in []byte) []byte {
	out := make([]byte, 0, len(in))
	for i := 0; i < len(in); i++ {
		b := in[i]
		switch {
		case b == 0x1b && i+1 < len(in):
			switch in[i+1] {
			case '[': // CSI: parameters, then a final byte @ to ~
				i += 2
				for i < len(in) && (in[i] < 0x40 || in[i] > 0x7e) {
					i++
				}
			case ']': // OSC: until BEL or ESC \
				i += 2
				for i < len(in) && in[i] != 0x07 && !(in[i] == 0x1b && i+1 < len(in) && in[i+1] == '\\') {
					i++
				}
				if i < len(in) && in[i] == 0x1b {
					i++
				}
			case '(', ')', '*', '+': // character set selection takes one more byte
				i += 2
			default:
				i++
			}
		case b == 0x1b:
			// a lone ESC at the end
		case b == '\b':
			if n := len(out); n > 0 {
				_, size := utf8.DecodeLastRune(out)
				out = out[:n-size]
			}
		case b == '\t' || (b >= 0x20 && b != 0x7f):
			out = append(out, b)
		}
	}
	return out
}

// Write records data seen in direction dir (dirRx or dirTx). Safe on a nil Logger.
func (l *Logger) Write(dir int, data []byte) {
	if l == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	for _, b := range data {
		switch {
		case b == '\n' && (dir == dirRx || len(l.lines[dir]) > 0), b == '\r' && dir == dirTx:
			l.endLine(dir)
		case b == '\n' || b == '\r':
			// the CR of a received CR LF, or the LF of a sent CR LF
		default:
			if len(l.lines[dir]) == 0 {
				l.began[dir] = time.Now()
			}
			l.lines[dir] = append(l.lines[dir], b)
		}
	}
	if len(l.lines[dir]) > 0 {
		if l.timers[dir] != nil {
			l.timers[dir].Stop()
		}
		l.timers[dir] = time.AfterFunc(logIdle, func() {
			l.mu.Lock()
			defer l.mu.Unlock()
			l.flushPartial(dir)
		})
	}
}

// Mark writes a line that is neither received nor sent, such as a bookmark, after the lines in progress.
// Safe on a nil Logger.
func (l *Logger) Mark(text string) {
	if l == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.flushPartial(dirRx)
	l.flushPartial(dirTx)
	if l.f == nil {
		return
	}
	if _, err := fmt.Fprintf(l.f, "---- %s ----\n", text); err != nil && l.err == nil {
		l.err = err
	}
}

// endLine writes the current line of dir, even when it is empty (a blank line was sent or received).
func (l *Logger) endLine(dir int) {
	if l.f == nil {
		return
	}
	text := l.lines[dir]
	if l.printable {
		text = printableOnly(text)
	}
	line := append([]byte(l.head(dir)), text...)
	n, err := l.f.Write(append(line, '\n'))
	if err != nil && l.err == nil {
		l.err = err
	}
	l.size += int64(n)
	l.lines[dir] = l.lines[dir][:0]
	if l.maxBytes > 0 && l.size >= l.maxBytes {
		l.rotate()
	}
}

// rotate closes the full file, keeps it as path.1 (the older ones move up, the oldest goes) and starts a new one.
func (l *Logger) rotate() {
	l.f.Close()
	os.Remove(fmt.Sprintf("%s.%d", l.path, l.keep))
	for i := l.keep - 1; i >= 1; i-- {
		os.Rename(fmt.Sprintf("%s.%d", l.path, i), fmt.Sprintf("%s.%d", l.path, i+1))
	}
	os.Rename(l.path, l.path+".1")
	f, err := os.OpenFile(l.path, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, 0o600)
	if err != nil {
		if l.err == nil {
			l.err = err
		}
		l.f = nil
		return
	}
	l.f, l.size = f, 0
}

// head is what starts a line of dir: the time when asked for, then the marker.
func (l *Logger) head(dir int) string {
	at := l.began[dir]
	l.began[dir] = time.Time{}
	if !l.stamps {
		return l.markers[dir]
	}
	if at.IsZero() {
		at = time.Now() // an empty line
	}
	return at.Format("2006-01-02 15:04:05.000 ") + l.markers[dir]
}

// flushPartial writes a line that was not finished, if there is one.
func (l *Logger) flushPartial(dir int) {
	if len(l.lines[dir]) > 0 {
		l.endLine(dir)
	}
}

// Err is the first error writing the file, or nil. Safe on a nil Logger.
func (l *Logger) Err() error {
	if l == nil {
		return nil
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.err
}

func (l *Logger) Close() {
	if l == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.flushPartial(dirRx)
	l.flushPartial(dirTx)
	for _, t := range l.timers {
		if t != nil {
			t.Stop()
		}
	}
	l.f.Close()
	l.f = nil
}
