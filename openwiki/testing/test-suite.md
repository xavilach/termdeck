---
type: guide
title: Test suite
description: How termdeck's pytest suite runs without network or hardware — an isolated TERMDECK_HOME and HOME per test, an in-process asyncssh server with scripted commands and an SFTP root, pyserial's loop:// device and a port-patching fixture, a fake prompter — plus a map of the test modules and the screenshots tool for interface reviews.
tags: [testing, pytest, asyncssh, fixtures, playwright, screenshots]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-f0a6e7dc03522b2682f88655
    resource: repo://tests/conftest.py
  - id: openwiki-source-dd15e7e50d0c4ab46bbb9920
    resource: repo://tools/screenshots.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Test suite

Run everything with `uv run pytest -q` (`uv` may live at `~/.local/bin/uv`). Configuration
is in `pyproject.toml`: `testpaths = ["tests"]`, `asyncio_mode = "auto"` (async tests and
fixtures need no marker) with a function-scoped loop. Dev dependencies: `pytest`,
`pytest-asyncio`, `playwright`, `cairosvg`.

Project rules ([conventions](../development/conventions.md)): **no network and no hardware**,
and **every fixed bug gets a test**.

## Fixtures (`tests/conftest.py`)

- **`isolated_home`** (autouse): sets `TERMDECK_HOME`, `HOME` and `USERPROFILE` to a temporary
  folder and removes `SSH_AUTH_SOCK`, so tests never touch the real profiles, `known_hosts`,
  keys, agent or `~/.ssh/config`.
- **`ssh_server`**: a real asyncssh server on 127.0.0.1 with a random port and a fresh ed25519
  host key, an SFTP subsystem chrooted to `tmp_path/sftp-root`, and permission for local and
  remote forwards. User `alice`, password `secret`. Switches on the `SSHServer` object:
  `banner` (sent before auth), `kbdint` (one keyboard-interactive prompt "Code: " instead of
  passwords), `expired` (passwords only through a password change, recorded in `changes`).
  `server.profile(name, **ssh)` builds a matching SSH profile.
- **Scripted remote commands** (`_shell`): `exit3`, `abort` (drop the connection),
  `disconnect` (with a reason), `kill` (exit by signal), `enq` (asks for the answerback),
  `err` (one line each on stdout and stderr), any other command (`ran CMD FOO=…`, showing the
  environment), subsystems, and an interactive echo shell that reports the terminal type and
  size and `resized WxH` on window changes.
- **`FakePrompter`**: answers prompts from a list (default the password), trusts or refuses
  host keys, and records what was asked, host keys and notices.
- **`patch_port(**attrs)`**: wraps `serial.serial_for_url` so every port opened gets the given
  methods replaced (a failing read, a blocking open…), and returns the ports created with their
  keyword arguments. Serial tests otherwise use pyserial's `loop://` device.
- **`read_until(session, marker)`**: reads a session until a marker, with a timeout.

## Test modules

| Module | Covers |
|---|---|
| `test_core.py` | profiles, forwards, quick targets, option metadata, store basics, terminal mapping, private files |
| `test_store.py` | profile store robustness, concurrency, log paths and log file safety, `known_hosts` writing |
| `test_session.py` | `TerminalSession` (backlog, end reasons, CR/LF, answerback), serial sessions, error classification |
| `test_controller.py` | `SessionController` states, reconnect, close, capabilities, snapshot |
| `test_vt.py`, `test_lineedit.py` | escape filtering, key mapping, printer, bell, titles, paste, printable logs; line editor |
| `test_ssh.py`, `test_ssh_options.py` | SSH auth, host keys, forwards, sharing, jump hosts, proxies, every SSH option |
| `test_sftp.py` | the core `SFTP` class and the `termdeck sftp` command |
| `test_serial_identity.py` | USB identity recording and following a moved adapter |
| `test_cli.py`, `test_connect.py` | CLI commands and exit codes; `termdeck connect` in pipe mode |
| `test_tui.py` | the Textual app via Textual's test pilot |
| `test_web.py` | the aiohttp server: auth, headers, profiles API, WebSocket, SFTP |
| `test_web_ui.py` | the web page in headless Chromium (Playwright); skipped if unavailable |
| `test_gui.py` | the desktop window with a fake `webview` module |

Interactive console mode, Windows-only paths (COM ports, console APIs, WebView2, ACLs) and real
hardware are not exercised; Windows helpers are tested for doing nothing elsewhere or through
monkeypatching.

## Screenshots tool

`tools/screenshots.py` renders the TUI (Textual SVG, converted to PNG with cairosvg when
available) and the web UI (Playwright) with sample profiles — two SSH, two `loop://` serial —
in a temporary `TERMDECK_HOME`, for interface reviews:

```sh
uv run python tools/screenshots.py [--tui] [--web] [--out screenshots]
uv run playwright install chromium   # once, for --web and test_web_ui.py
```

## Related

- [Development conventions](../development/conventions.md)
- [Quickstart](../quickstart.md)
