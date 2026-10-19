# termdeck

Serial terminal in the browser. One Go program; the page is embedded.

## Run

Download a binary from the releases, or build:

```sh
go build -o termdeck .                                 # Linux, macOS
GOOS=windows GOARCH=amd64 go build -o termdeck.exe .   # Windows
./termdeck                                             # --port N, --no-browser
```

## Features

- Sessions in groups, saved as JSON. Tabs, or two panes (side by side or stacked).
- Baud, data bits, parity, stop bits, flow control (RTS/CTS, XON/XOFF, DSR/DTR on Windows), DTR/RTS, live CTS/DSR/DCD/RI.
- Scripts per group: commands with a delay, optional loop.
- Log per session: Rx/Tx line markers, append/overwrite/ask, printable only, start/stop while connected.
- Rx/Tx line markers on screen.
- Character sets, local line editing, local echo.
- Editing an open session reloads it.
- Under Advanced: answerback, key styles, colours, bell, paste and mouse options.

## Use

- Left column: left click selects, double click opens (or runs a script), right click edits. Drag a session or script onto a group to move it.
- Ctrl+Shift+C / V / L: copy, paste, clear. Alt+Shift+N / W / S / B: new session, close tab, layout, left column.

## Data

`profiles.json`, `scripts.json`, `groups.json` in `%APPDATA%\termdeck` or `~/.config/termdeck` (`TERMDECK_HOME` overrides).
The server listens on 127.0.0.1 only; the start link works once.

## Notes

`internal/serial` is go.bug.st/serial (BSD) plus flow control.
Not tested: real serial hardware, the Windows build, a real browser.

## Licence

MIT. xterm.js (MIT) in `web/vendor`.
