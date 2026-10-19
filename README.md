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
- The port list follows plugging and unplugging by itself. Virtual ports (own section in the left column, reduced at first) to try things without hardware: `fake:echo`, `fake:shell` (login admin/admin, type `help`), `fake:counter`, `fake:binary`, `fake:flaky` (drops and comes back, to try Reconnect), and `fake:link-a` / `fake:link-b` (a null-modem cable: open one in each tab).
- Freeze (keep the screen while data is still kept), Copy, Save text, Save raw (the bytes the device sent). Time can show the clock or the time since the previous line.
- Graph (Graph button): numbers the server takes from the received lines (Go regular expressions: a pattern cannot run away on a long line), set per session under Advanced as `name | prefix | suffix` or `name | /regex/`, drawn over time (windows from 10 s to all, each series on its own scale if wanted), Save CSV.
- Marks (Mark button, Alt+Shift+M): a line in the output and in the log; ◀ ▶ jump between them.
- Reconnect (session option): when the port goes away, it is opened again every second until it is back.
- Quick sends (Advanced): buttons above the send bar, one per line `name | type | data` (`| end=cr`, `| check=crc16modbus` to choose the end and a check).
- Check (send bar, byte types): add a sum, XOR, CRC-8, CRC-16 (Modbus, CCITT, XMODEM) or CRC-32 after the data.
- Slow send (Advanced): a pause after each character and after line ends, for devices that lose data; Stop and Cancel drop what is waiting.
- Highlights (Advanced): `ERROR = red+bold`, `/warn(ing)?/ = orange` colour matching lines. Filter (receive bar): show only lines matching text or /regex/, `!` hides them; the data is kept, so clearing the filter shows everything.
- Note line changes (session option): CTS, DSR, DCD and RI changes are written in the output. A session can be duplicated (❐ in the left column).
- Rx/Tx counters, Find (Ctrl+Shift+F, regex), triggers (`login: => admin`, `/regex/ => @hex=0D`; paused when they loop, e.g. over an echo).
- Export and Import (top of the left column): sessions, scripts and groups in one file; items with the same name are replaced, nothing is stored if one is invalid.
- Log per session: optional date and time on each line, a new file when it gets big (`name.log.1` is the newest full one, a number of them kept), Rx/Tx line markers, append/overwrite/ask, printable only, start/stop while connected.
- Rx/Tx line markers on screen.
- The lines are read-only; everything is sent from the send bar (and scripts, triggers). Local echo shows what is sent.
- Character sets.
- Editing an open session reloads it.
- Under Advanced: answerback, colours, bell, mouse and paste options, triggers.

## Use

- Left column: left click selects, double click opens (or runs a script), right click edits. Drag a session or script onto a group to move it.
- Tunnel (left column): join two ports, both directions are shown (A→B green, B→A cyan) and logged (`A>B`, `B>A`).
- XMODEM (send bar, XMODEM…): send a file to the device or receive one from it (CRC-16, 128 or 1K blocks); the transfer's data stays out of the output and the log; a received file is saved by the browser, padded with 1A to a block.
- Help (left column, Alt+Shift+H): shortcuts and the syntax of the one-rule-per-line settings.
- Ctrl+Shift+C / V / L: copy, paste, clear. Alt+Shift+N / W / S / B / M: new session, close tab, layout, left column, mark.

## Data

`profiles.json`, `scripts.json`, `groups.json` in `%APPDATA%\termdeck` or `~/.config/termdeck` (`TERMDECK_HOME` overrides).
The server listens on 127.0.0.1 only; the start link works once, and a reload of the page keeps the login and brings the tabs back (kept for the life of the browser tab): the connected ones connect again.

## Notes

`internal/serial` is go.bug.st/serial (BSD) plus flow control.

## Licence

MIT. xterm.js (MIT) in `web/vendor`.
