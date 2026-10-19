# termdeck

Serial terminal in the browser. One Go program; the page is embedded.

## Run

Download a binary from the releases, or build:

```sh
go build -o termdeck .                                 # Linux, macOS
GOOS=windows GOARCH=amd64 go build -o termdeck.exe .   # Windows
./termdeck                                             # --port N, --no-browser
```

Press Help (left column, Alt+Shift+H) for the keys and the syntax of the settings.

## Sessions

- Saved as JSON, in groups, with scripts per group. Tabs, or two panes. The left column can be resized (drag its edge). Export / Import (left column) move them between computers.
- Baud, data bits, parity, stop bits, flow control (RTS/CTS, XON/XOFF, DSR/DTR on Windows), DTR/RTS, live CTS/DSR/DCD/RI, character sets.
- Reconnect when the port comes back. RS-485 direction by RTS. Slow send (pause per character and per line end). Note line changes. Duplicate (❐).
- A tab that gets data while another is shown is marked, and the browser tab title counts them: `(2) termdeck`.
- The port list follows plugging and unplugging. A reload brings the tabs back (the connected ones connect again).
- Virtual ports, to try things without hardware: `fake:echo`, `fake:shell` (admin/admin), `fake:counter`, `fake:binary`, `fake:stream` (600 KB/s), `fake:flaky` (drops and returns), `fake:link-a` / `fake:link-b` (a null-modem cable).

## Receive (the bar above the lines)

- View: Text, Hex, Decimal, Binary, Frames. New line at (Terminal, CR, LF, CR LF, Custom, None), filter (text, `/regex/`, `!hide`); under More: control characters, time (clock or since the last line), new line after a pause, Copy, Save. Changing them redraws the data kept (8 MB): nothing is lost.
- Frames: bytes cut into frames by a description (`sync`, `size`, `end`, `header`, `check`), one line each, OK or BAD.
- Highlights (`ERROR = red+bold`), Graph of numbers taken from lines (`Temp | Temperature: | degC`), Marks (Alt+Shift+M), Freeze, Copy, Save text / raw, Find (Ctrl+Shift+F), Rx counter.
- The lines are read-only.

## Send (the bar below)

- Text, Hex, Decimal, Binary or Cmd (steps: `dtr=1; wait=500; hex=0D 0A; expect=login:`; `expect` waits for what the device says, so scripts can log in or flash step by step). End, Check (sum, XOR, CRC), Auto, history, files, quick-send buttons.
- Transfer: XMODEM and YMODEM, send or receive (checked against lrzsz).
- Scripts, triggers (`login: => admin`, paused when they loop) and quick sends are rules, one per line.

## Tools

- Tunnel: join two ports, both directions shown and logged.
- Log per session: markers, times, a new file when it gets big, append / overwrite / ask, start / stop.

## Data

`profiles.json`, `scripts.json`, `groups.json` in `%APPDATA%\termdeck` or `~/.config/termdeck` (`--home DIR` or `TERMDECK_HOME` override). `tools/release.sh VERSION` builds for Linux, Windows and macOS.
The server listens on 127.0.0.1 only; the start link works once, and the login is kept for the life of the browser tab.

## Tests

`go test ./...` (also runs the Node tests). `tools/webtest`: lint, the page in jsdom, and the program in Chromium (`npm test`, `npm run e2e`). `LRZSZ=<folder> go test` checks XMODEM / YMODEM against the reference programs.

## Notes

`internal/serial` is go.bug.st/serial (BSD) plus flow control.

## Licence

MIT. xterm.js (MIT) in `web/vendor`.
