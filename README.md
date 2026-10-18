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
uv sync --all-extras        # or: pip install ".[web,gui]"
uv run termdeck --help
```

The core, CLI and TUI need `asyncssh`, `bcrypt`, `pyserial`, `textual` and `pyte`. Extras: `web` (aiohttp), `gui` (pywebview + web).

## Features

- **SSH**: password, keyboard-interactive, keys (with passphrase) and certificates, agent, GSSAPI/Kerberos (`gssapi` extra on Linux and macOS); jump host; proxies (SOCKS 4/5, HTTP, Telnet, local command); keepalives, compression, rekeying; agent and X11 forwarding; environment, terminal modes; cipher, key-exchange, MAC and host-key lists; pinned host keys; connection sharing between tabs; remote command, subsystem or no shell. Like PuTTY, `~/.ssh/config` is not read unless the profile asks for it.
- **Port forwards**: local `-L`, remote `-R`, SOCKS `-D`. Forwards listening on a non-loopback address are flagged.
- **SFTP**: file panel in the web and desktop UIs (browse, upload by drag and drop, download, rename, delete, new folder); `termdeck sftp TARGET [COMMAND]` on the command line: the psftp commands (ls, cd, get, put, mget, mput, reget, reput, mkdir, rm, rmdir, mv, chmod, lcd...) with wildcards, never replacing files without `-f`.
- **Serial**: any pyserial device or URL (`COM3`, `/dev/ttyUSB0`, `rfc2217://host:port`); speed, data bits, parity, stop bits, flow control, DTR/RTS, break.
- **Follow the device**: a serial profile remembers its USB adapter (VID:PID, serial number, USB socket) and finds it again when its port name changes. It never guesses between adapters it cannot tell apart, and refuses to connect if the old port name now belongs to a different adapter.
- **Local line editing**: for devices without a line editor. Edit the line here (arrows, Home/End, Ctrl+U/K/W, history with Up/Down) and send it on Enter. After a prompt such as `Password:` the line is not echoed and not kept in history.
- **Terminal**: character set, Enter and Backspace mapping, local echo, scrollback, session log file (`{name}`, `{host}`, `{date}` in the path), font size, themes.
- **Host keys**: unknown keys are always confirmed (key type and fingerprint shown), changed keys are flagged with the old fingerprint. Kept in termdeck's own `known_hosts`; `~/.ssh/known_hosts` is also read. A replaced key is marked `@revoked`; `termdeck forget-host` clears a host's entries.

Settings forms show only the options of the selected connection type. Less common options are behind the **Advanced** switch.

## Command line

```sh
termdeck add ssh web example.com -u bob -L 8080:db:5432 -g Prod
termdeck add serial board COM3 -b 9600 --parity E
termdeck set web ssh.keepalive=30 terminal.theme=dracula   # any option: section.option=value
termdeck list
termdeck options                     # every option name for set/--set
termdeck connect web                 # or a quick target: bob@host:2222, COM3@9600
termdeck connect web uptime          # run a command; its exit status is ours (like plink)
tar c dir | termdeck connect web "tar x"   # with pipes: stdin/stdout pass through, no terminal
termdeck connect --batch -N -L 8080:db:5432 web   # never prompt; forwards only
termdeck tunnel web                  # forwards only, no shell
termdeck sftp web get /var/log/syslog   # one command, or no command for a psftp-like shell
termdeck sftp web -b script.txt         # a script (also from stdin); stops at the first error
termdeck ports                       # serial ports with their USB identity
termdeck forget-host host.example     # forget a host's key after a server was reinstalled
```

Exit codes: 0 success, 1 connection or file error, 2 invalid input or unknown profile, 70 internal error (with a traceback to report), 130 interrupted. `connect` returns the remote program's exit status (128 + n when it was killed by signal n), interactive or not, and in a terminal ends with `Session ended[: reason]` on stderr.

`termdeck show NAME` prints a profile as JSON; `termdeck options` lists every option with its values and default.

## Keys

| Where | Key | Action |
|---|---|---|
| `connect` | Ctrl+] then q | Quit |
| `connect` | Ctrl+] then b | Send serial break |
| `connect` | Ctrl+] then ] | Send Ctrl+] |
| TUI | Ctrl+] | Leave the terminal for the menus (Esc returns) |
| TUI menus | n, e, c, Delete | New, edit, duplicate, delete profile |
| TUI menus | w, r, / | Close tab (asks while connected), reconnect, quick connect |
| TUI menus | b b | Send serial break (B twice, so a stray key cannot reset a device) |
| TUI menus | f, t, s | Find in the scrollback, files (SFTP), show or hide the sidebar |
| TUI | Ctrl+Q | Quit (asks while sessions are connected; inside a terminal Ctrl+Q goes to the device) |
| TUI | Shift+PgUp/PgDn, wheel | Scrollback |
| TUI | drag | Select; copied on release |
| Web, desktop | select / right click | Copy / paste |
| Web, desktop | Ctrl+Shift+C/V/F | Copy, paste, find |
| Desktop | Ctrl+Shift+N, W, B, PgUp/PgDn | New profile, close tab, sidebar, previous/next tab |
| Web (browser) | Alt+Shift+N, W, B, PgUp/PgDn | The same (the browser keeps Ctrl+Shift+N/W for itself) |
| Web, desktop | profile list: arrows, Enter, F2, Delete | Move, connect, edit, delete |

## Data

Profiles, `known_hosts` and window storage live in `%APPDATA%\termdeck` (Windows), `~/Library/Application Support/termdeck` (macOS) or `~/.config/termdeck` (Linux). `TERMDECK_HOME` overrides the location. Files are created readable by the owner only (on Windows, with an access list of their own). The desktop window remembers its size. Passwords are never stored.

## Web UI security

The server listens on 127.0.0.1 only and checks the Host and Origin headers. The start link holds a one-time token, valid for 10 minutes, that the page exchanges for a per-tab secret. `termdeck web` opens the browser through a private file that forwards to the link, so the token never appears on a command line other users can read. The secret is sent as a header, never as a cookie. Downloads use single-use links. Uploads have no size limit.

## Development

```sh
uv run pytest -q
```

The tests run without network or hardware: a local asyncssh server and pyserial's `loop://` stand in for real hosts and devices.

Tested on Linux only (WSL2, Python 3.11). Not yet tested: Windows (console mode for `connect`, COM ports, WebView2), macOS, a real desktop window, real serial hardware.

## License

MIT. Bundles xterm.js 5.5.0 and addons (MIT), see `src/termdeck/web/static/vendor/xterm-LICENSE.txt`.
