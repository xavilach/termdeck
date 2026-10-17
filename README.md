# termdeck

SSH and serial terminal with saved profiles. One core, four interfaces:

| Command | Interface |
|---|---|
| `termdeck` | Interactive full-screen UI in the terminal (TUI) |
| `termdeck connect NAME` | One session straight in this terminal, like `ssh` |
| `termdeck gui` | Desktop window |
| `termdeck web` | Same UI in your browser (localhost only) |

## Install

```sh
uv sync --all-extras        # or: pip install ".[tui,web,gui]"
uv run termdeck --help
```

Extras: `tui` (Textual), `web` (aiohttp), `gui` (pywebview + web). The core and the CLI need only `asyncssh`, `bcrypt` and `pyserial`.

## Features

- **SSH**: password, keyboard-interactive, keys (with passphrase), agent; jump host; keepalive, compression, agent and X11 forwarding, environment, cipher and key-exchange lists, remote command.
- **Port forwards**: local `-L`, remote `-R`, SOCKS `-D`. Forwards listening on a non-loopback address are flagged.
- **SFTP**: file panel in the web and desktop UIs (browse, upload by drag and drop, download, rename, delete, new folder); `termdeck sftp NAME ls|get|put` on the command line.
- **Serial**: any pyserial device or URL (`COM3`, `/dev/ttyUSB0`, `rfc2217://host:port`); speed, data bits, parity, stop bits, flow control, DTR/RTS, break.
- **Follow the device**: a serial profile remembers its USB adapter (VID:PID, serial number, USB socket) and finds it again when its port name changes. It never guesses between adapters it cannot tell apart, and refuses to connect if the old port name now belongs to a different adapter.
- **Local line editing**: for devices without a line editor. Edit the line here (arrows, Home/End, Ctrl+U/K/W, history with Up/Down) and send it on Enter. After a prompt such as `Password:` the line is not echoed and not kept in history.
- **Terminal**: character set, Enter and Backspace mapping, local echo, scrollback, session log file, font size, themes.
- **Host keys**: unknown keys are always confirmed, changed keys are flagged. Kept in termdeck's own `known_hosts`; `~/.ssh/known_hosts` is also read.

Settings forms show only the options of the selected connection type. Less common options are behind the **Advanced** switch.

## Command line

```sh
termdeck add ssh web example.com -u bob -L 8080:db:5432 -g Prod
termdeck add serial board COM3 -b 9600 --parity E
termdeck set web ssh.keepalive=30 terminal.theme=dracula   # any option: section.option=value
termdeck list
termdeck connect web                 # or a quick target: bob@host:2222, COM3@9600
termdeck tunnel web                  # forwards only, no shell
termdeck sftp web get /var/log/syslog
termdeck ports                       # serial ports with their USB identity
```

`termdeck show NAME` prints a profile as JSON, which is also where the option names for `set` come from.

## Keys

| Where | Key | Action |
|---|---|---|
| `connect` | Ctrl+] then q | Quit |
| `connect` | Ctrl+] then b | Send serial break |
| `connect` | Ctrl+] then ] | Send Ctrl+] |
| TUI | Ctrl+] | Leave the terminal for the menus (Esc returns) |
| TUI menus | n, e, c, Delete | New, edit, copy, delete profile |
| TUI menus | w, r, b, / | Close tab, reconnect, break, quick connect |
| TUI | Ctrl+Q | Quit (inside a terminal Ctrl+Q goes to the device) |
| TUI | Shift+PgUp/PgDn, wheel | Scrollback |
| Web, desktop | select / right click | Copy / paste |
| Web, desktop | Ctrl+Shift+C/V/F | Copy, paste, find |
| Web, desktop | Ctrl+Shift+N, Ctrl+Shift+W | New profile, close tab |

## Data

Profiles, `known_hosts` and window storage live in `%APPDATA%\termdeck` (Windows), `~/Library/Application Support/termdeck` (macOS) or `~/.config/termdeck` (Linux). `TERMDECK_HOME` overrides the location. Files are created readable by the owner only. Passwords are never stored.

## Web UI security

The server listens on 127.0.0.1 only and checks the Host and Origin headers. The start link holds a one-time token, valid for 10 minutes, that the page exchanges for a per-tab secret. The secret is sent as a header, never as a cookie. Downloads use single-use links. Uploads have no size limit.

## Development

```sh
uv run pytest -q
```

The tests run without network or hardware: a local asyncssh server and pyserial's `loop://` stand in for real hosts and devices.

Tested on Linux only (WSL2, Python 3.11). Not yet tested: Windows (console mode for `connect`, COM ports, WebView2), macOS, a real desktop window, real serial hardware.

## License

MIT. Bundles xterm.js 5.5.0 and addons (MIT), see `src/termdeck/web/static/vendor/xterm-LICENSE.txt`.
