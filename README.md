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
- Scripts per group: commands with a delay, optional loop. A line starting with `@` is steps: `@dtr=1; wait=500; dtr=0; hex=0D 0A` (`@@` for a literal `@`).
- Send bar: text (`\r \n \t \xNN`), hex, decimal, binary or Cmd (steps); End (None, CR, LF, CR LF, Custom), Auto (send again every N ms), history, send a file. Alt+T/D/H/B switch the type. The input is emptied after a send.
- View: text, hex, decimal or binary rows; New line at (Terminal, CR, LF, CR LF, Custom, None: other CR/LF are dropped, or shown as `<CR>` `<LF>` with control characters on); control characters as `<ESC>`...; time at the start of each line; new line after a pause. Changing the view redraws the kept data (last 8 MB), so switching back restores it.
- The port list follows plugging and unplugging by itself. Virtual ports (own section in the left column, reduced at first) to try things without hardware: `fake:echo`, `fake:shell` (login admin/admin, type `help`), `fake:counter`, `fake:binary`, and `fake:link-a` / `fake:link-b` (a null-modem cable: open one in each tab).
- Freeze (keep the screen while data is still kept), Copy, Save text, Save raw (the bytes the device sent). Time can show the clock or the time since the previous line.
- Rx/Tx counters, Find (Ctrl+Shift+F, regex), triggers (`login: => admin`, `/regex/ => @hex=0D`; paused when they loop, e.g. over an echo).
- Log per session: Rx/Tx line markers, append/overwrite/ask, printable only, start/stop while connected.
- Rx/Tx line markers on screen.
- The lines are read-only; everything is sent from the send bar (and scripts, triggers). Local echo shows what is sent.
- Character sets.
- Editing an open session reloads it.
- Under Advanced: answerback, colours, bell, mouse and paste options, triggers.

## Use

- Left column: left click selects, double click opens (or runs a script), right click edits. Drag a session or script onto a group to move it.
- Ctrl+Shift+C / V / L: copy, paste, clear. Alt+Shift+N / W / S / B: new session, close tab, layout, left column.

## Data

`profiles.json`, `scripts.json`, `groups.json` in `%APPDATA%\termdeck` or `~/.config/termdeck` (`TERMDECK_HOME` overrides).
The server listens on 127.0.0.1 only; the start link works once, and a reload of the page keeps the login (kept for the life of the browser tab).

## Notes

`internal/serial` is go.bug.st/serial (BSD) plus flow control.

## Licence

MIT. xterm.js (MIT) in `web/vendor`.
