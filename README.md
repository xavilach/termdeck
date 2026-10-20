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

## Ports and devices

- Settings come in layers: **Generic** (what every port starts from), then a **device** (what a kind of device speaks: speed, new line, rules, frames, scripts...), then the **port**'s own changes. Double-click a port to open it with its device; choose another device next to the port (or New device…, made of the port's changes); right-click a port for its own settings. The dialog shows each value with where it comes from, ↺ to go back to it, and Move these changes to the device. Presets fill in the settings of common devices.
- A port is recognised by its USB adapter (serial number) when it has one, so its settings follow the adapter to another port name.
- Tabs, or two panes. The left column can be resized (drag its edge). Export / Import (left column) move devices, ports and scripts between computers; PuTTY adds your serial PuTTY sessions as devices (found in the Registry or `~/.putty/sessions`, or from a `.reg` export or session file you choose).
- Baud, data bits, parity, stop bits, flow control (RTS/CTS, XON/XOFF, DSR/DTR on Windows), DTR/RTS, live CTS/DSR/DCD/RI, character sets.
- Reconnect when the port comes back. RS-485 direction by RTS. Slow send (pause per character and per line end). Note line changes. Copy a device (❐).
- A tab that gets data while another is shown is marked, and the browser tab title counts them: `(2) termdeck`.
- A tab lives on the server: a reload of the page finds it as it was (the port open, the lines, a running script). A tab left without its page ends after a minute.
- The port list follows plugging and unplugging, and tells USB adapters apart (product, vendor:product, serial number: from sysfs on Linux, the Registry on Windows). A reload brings the tabs back (the connected ones connect again).
- Virtual ports, to try things without hardware: `fake:echo`, `fake:shell` (admin/admin), `fake:counter`, `fake:binary`, `fake:stream` (600 KB/s), `fake:flaky` (drops and returns), `fake:link-a` / `fake:link-b` (a null-modem cable).

## Receive (the bar above the lines)

- View: Text, Hex, Decimal, Binary, Frames. New line at (Terminal, CR, LF, CR LF, Custom, None), filter (text, `/regex/`, `!hide`); under More: control characters, time (clock or since the last line), new line after a pause, Copy, Save. Changing them redraws the data kept (8 MB, on the server): nothing is lost. The screen and the log are drawn the same way.
- Frames: bytes cut into frames by a description (`sync`, `size`, `end`, `header`, `check`), one line each, OK or BAD.
- Highlights (`ERROR = red+bold`; add `notify` for a desktop notification while the tab is out of sight), Graph of numbers taken from lines (`Temp | Temperature: | degC`), Marks (Alt+Shift+M), Freeze, Copy, Save text / raw, Find (Ctrl+Shift+F), Rx counter.
- The lines are read-only.

## Send (the bar below)

- Text, Hex, Decimal, Binary or Cmd (steps: `dtr=1; wait=500; hex=0D 0A; expect=login:`; `expect` waits for what the device says, so scripts can log in or flash step by step). End, Check (sum, XOR, CRC), Auto, history, files, quick-send buttons.
- All tabs: send the same data to every connected tab (a test bench); Open all (▶▶) opens every port of a device.
- Transfer: XMODEM and YMODEM, send or receive (checked against lrzsz).
- Scripts, triggers (`login: => admin`, paused when they loop) and quick sends are rules, one per line. A pattern is the same everywhere: text in any case, `/regex/` as written, `/regex/i`.

## Tools

- Tunnel: join two ports, both directions shown and logged.
- Log per session: markers, times, a new file when it gets big, append / overwrite / ask, start / stop.

## Data

`devices.json`, `ports.json`, `scripts.json` in `%APPDATA%\termdeck` or `~/.config/termdeck` (`--home DIR` or `TERMDECK_HOME` override). `tools/release.sh VERSION` builds for Linux, Windows and macOS.
The server listens on 127.0.0.1 only; the start link works once, and the login is kept for the life of the browser tab.

## Tests

`go test ./...` (also runs the Node tests). `tools/webtest`: lint, the page in jsdom, and the program in Chromium (`npm test`, `npm run e2e`). `LRZSZ=<folder> go test` checks XMODEM / YMODEM against the reference programs.

## Notes

`internal/serial` is go.bug.st/serial (BSD) plus flow control.

## Licence

MIT. xterm.js (MIT) in `web/vendor`.
