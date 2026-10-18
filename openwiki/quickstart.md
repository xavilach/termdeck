---
type: overview
title: Quickstart
description: Entry point to the termdeck wiki — what termdeck is (an SSH and serial terminal with saved profiles, one asyncio core and four interfaces), how to install, run and test it, where its data lives, and which page to read for each kind of task.
tags: [quickstart, overview, onboarding, navigation]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-05ccef8d4cf1698187f20464
    resource: repo://pyproject.toml
  - id: openwiki-source-23775c3de52f3ab95a13cb8b
    resource: repo://README.md
  - id: openwiki-source-40dcfdf1b085605444d2f636
    resource: repo://src/termdeck/cli.py
  - id: openwiki-source-d47bafb87025777e901ce17b
    resource: repo://src/termdeck/config.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Quickstart

termdeck is an SSH and serial terminal with saved profiles, meant to cover what PuTTY offers
for SSH and serial (Windows first). One UI-free core drives four interfaces:

| Command | Interface |
|---|---|
| `termdeck` | full-screen terminal UI (Textual TUI) |
| `termdeck connect NAME` | one session in the current terminal, like `ssh`/`plink` |
| `termdeck web` | the same UI in a browser, served on 127.0.0.1 |
| `termdeck gui` | desktop window (pywebview around the web UI) |

## Install, run, test

```sh
uv sync --all-extras          # or: pip install ".[web,gui]"
uv run termdeck --help
uv run pytest -q              # no network, no hardware needed
uv run playwright install chromium   # once, for browser tests and web screenshots
```

Python ≥ 3.11. Core, CLI and TUI need `asyncssh`, `bcrypt`, `pyserial`, `textual`, `pyte`
(and `pywin32` on Windows). Extras: `web` (aiohttp), `gui` (pywebview + aiohttp), `gssapi`
(Kerberos on Linux/macOS). Entry point: `termdeck.cli:main`.

Data — profiles (`profiles.json`), `known_hosts`, window size — lives in `%APPDATA%\termdeck`,
`~/Library/Application Support/termdeck` or `~/.config/termdeck`; `TERMDECK_HOME` overrides it.
Files are owner-only and passwords are never stored.

## Where to look

| Task | Read |
|---|---|
| Understand the layers and the flow from profile to screen | [Architecture overview](architecture/overview.md) |
| Add or change a profile option, validation, quick-connect syntax | [Profiles and the option registry](core/profiles-and-options.md) |
| Profile file format, locking, private files, config folders | [Profile storage and private files](core/profile-store.md) |
| Terminal settings applied to a session, logging, error messages | [Sessions and TerminalSession](core/sessions.md) |
| Tab state, reconnect, close-on-exit, what UIs display | [SessionController](core/session-controller.md) |
| Escape-sequence filtering, key mapping, line editing, themes | [Terminal processing](core/terminal-processing.md) |
| SSH auth, host keys, forwards, jump hosts, connection sharing | [SSH transport](transports/ssh.md) |
| SOCKS/HTTP/Telnet proxies, proxy and jump commands | [Proxies](transports/proxies.md) |
| Serial ports, flow control, following a USB adapter | [Serial transport](transports/serial.md) |
| File transfer, `termdeck sftp`, file panels | [SFTP](transports/sftp.md) |
| CLI commands, exit codes, `connect` console and pipes | [Command line and connect console](interfaces/cli-and-console.md) |
| The Textual UI, its editor and terminal widget | [Textual TUI](interfaces/tui.md) |
| Web server, auth, WebSocket protocol, front end | [Web UI](interfaces/web-ui.md) |
| Desktop window, WebView2, window size | [Desktop GUI](interfaces/desktop-gui.md) |
| What protects users from hostile servers, pages and files | [Security model](security/model.md) |
| Fixtures, test map, screenshots | [Test suite](testing/test-suite.md) |
| Project rules, requirements, review agents | [Development conventions](development/conventions.md) |

## Rules to know before changing code

- Declare every profile option once with `option()` in `profiles.py`; UIs build forms from
  `describe_options()`.
- Behaviour shared by all UIs goes in the core, not in a UI.
- Remote or user text never goes into Textual markup or `innerHTML` unescaped.
- Tests use a local asyncssh server and `loop://`, never network or hardware; every fixed bug
  gets a test.
- Owner requests go in `REQUIREMENTS.md`.

Tested on Linux (WSL2) only; Windows console mode, COM ports, WebView2, macOS and real serial
hardware are not yet tested.
