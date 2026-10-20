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

var unsafeName = regexp.MustCompile(`[<>:"/\\|?*\x00-\x1f]`)

// logPath replaces {name}, {date} and {time} in a profile's log path.
func logPath(p Profile, now time.Time) string {
	return strings.NewReplacer(
		"{name}", unsafeName.ReplaceAllString(p.Name, "_"),
		"{date}", now.Format("2006-01-02"),
		"{time}", now.Format("150405"),
	).Replace(p.LogPath)
}

// Logger writes the records of a session to its log file, through a renderer with the log's format (markers,
// times, printable text), and starts a new file when the file is full.
type Logger struct {
	mu  sync.Mutex
	f   *os.File
	r   *renderer
	err error // the first error writing the file (the disk is full, say)

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
	l := &Logger{f: f, r: newRenderer(logFormat(p)), path: path, maxBytes: int64(p.LogMaxMB) * 1024 * 1024, keep: max(p.LogKeep, 1)}
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

// Record writes a record. Safe on a nil Logger.
func (l *Logger) Record(rec record) {
	if l == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.r.add(rec)
	l.write()
}

// idle writes a line that has waited long enough for its end (a prompt). Safe on a nil Logger.
func (l *Logger) idle(now time.Time) {
	if l == nil {
		return
	}
	l.mu.Lock()
	defer l.mu.Unlock()
	l.r.idle(now)
	l.write()
}

// write puts what the renderer made into the file, and starts a new file when it is full.
func (l *Logger) write() {
	out := l.r.take()
	if l.f == nil || len(out) == 0 {
		return
	}
	n, err := l.f.Write(out)
	if err != nil && l.err == nil {
		l.err = err
	}
	l.size += int64(n)
	if l.maxBytes > 0 && l.size >= l.maxBytes && out[len(out)-1] == '\n' {
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
	l.r.flushPending()
	l.r.flushHex()
	l.write()
	l.f.Close()
	l.f = nil
}
