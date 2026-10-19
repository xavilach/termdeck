package main

import (
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"
)

// fakeShell is the device behind fake:shell: a login, then a prompt and a few commands.
type fakeShell struct {
	port  *fakePort
	state string // user, password, shell
	line  []byte
	user  string
	cr    bool // the last byte was CR, so an LF right after it is part of the same Enter
	mu    sync.Mutex
}

func newFakeShell(f *fakePort) *fakeShell {
	s := &fakeShell{port: f, state: "user"}
	f.feed([]byte("termdeck fake device\r\nlogin: "))
	return s
}

func (s *fakeShell) receive(_ *fakePort, data []byte) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, b := range data {
		if b != '\n' && b != '\r' {
			s.cr = false
		}
		switch {
		case b == '\n' && s.cr:
			s.cr = false
		case b == '\r' || b == '\n':
			s.cr = b == '\r'
			s.port.feed([]byte("\r\n"))
			s.enter(string(s.line))
			s.line = nil
		case b == 0x7f || b == '\b':
			if len(s.line) > 0 {
				s.line = s.line[:len(s.line)-1]
				if s.state != "password" {
					s.port.feed([]byte("\b \b"))
				}
			}
		case b == 0x03: // Ctrl+C
			s.line = nil
			s.port.feed([]byte("^C\r\n" + s.prompt()))
		case b >= 0x20:
			s.line = append(s.line, b)
			if s.state != "password" {
				s.port.feed([]byte{b})
			}
		}
	}
}

func (s *fakeShell) prompt() string {
	switch s.state {
	case "user":
		return "login: "
	case "password":
		return "Password: "
	}
	return s.user + "# "
}

func (s *fakeShell) enter(line string) {
	switch s.state {
	case "user":
		s.user, s.state = strings.TrimSpace(line), "password"
	case "password":
		if s.user == "admin" && line == "admin" {
			s.state = "shell"
			s.port.feed([]byte("Welcome. Type help.\r\n"))
		} else {
			s.state = "user"
			s.port.feed([]byte("Login incorrect\r\n"))
		}
	default:
		s.command(strings.TrimSpace(line))
	}
	s.port.feed([]byte(s.prompt()))
}

func (s *fakeShell) command(line string) {
	name, arg, _ := strings.Cut(line, " ")
	arg = strings.TrimSpace(arg)
	say := func(text string) { s.port.feed([]byte(strings.ReplaceAll(text, "\n", "\r\n") + "\r\n")) }
	switch name {
	case "":
	case "help":
		say("help            this list\necho TEXT       print TEXT\ntime            the time\nversion         the version\ncolors          ANSI colours\nbell            ring the bell\nbin             send bytes 0 to 255\nflood N         print N lines (up to 5000)\nslow            print dots slowly, without a line end\nlogout          back to the login")
	case "echo":
		say(arg)
	case "time":
		say(time.Now().Format("2006-01-02 15:04:05"))
	case "version":
		say("fake device 1.0")
	case "colors":
		var b strings.Builder
		for c := 30; c <= 37; c++ {
			fmt.Fprintf(&b, "\x1b[%dmcolour %d\x1b[0m ", c, c)
		}
		say(b.String())
	case "bell":
		s.port.feed([]byte("\a"))
	case "bin":
		all := make([]byte, 256)
		for i := range all {
			all[i] = byte(i)
		}
		s.port.feed(all)
		say("")
	case "flood":
		n, err := strconv.Atoi(arg)
		if err != nil || n < 1 || n > 5000 {
			say("flood: a number from 1 to 5000")
			return
		}
		var b strings.Builder
		for i := 1; i <= n; i++ {
			fmt.Fprintf(&b, "line %d of %d\r\n", i, n)
		}
		s.port.feed([]byte(b.String()))
	case "slow":
		go func() {
			for i := 0; i < 8; i++ {
				select {
				case <-s.port.stop:
					return
				case <-time.After(300 * time.Millisecond):
					s.port.feed([]byte("."))
				}
			}
			s.port.feed([]byte("\r\n"))
		}()
	case "logout":
		s.state = "user"
	default:
		say(name + ": command not found")
	}
}
