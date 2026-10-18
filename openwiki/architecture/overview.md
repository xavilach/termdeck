---
type: architecture
title: Architecture overview
description: termdeck is one asyncio core (profiles, sessions, transports, a per-tab controller) with four interfaces on top — CLI/connect console, Textual TUI, local web UI and a pywebview desktop window. This page maps the layers, the dependency direction and the flow from a profile to bytes on screen.
tags: [architecture, layering, asyncio, core, interfaces]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-40dcfdf1b085605444d2f636
    resource: repo://src/termdeck/cli.py
  - id: openwiki-source-69cc0e49919868a1c629913c
    resource: repo://src/termdeck/gui.py
  - id: openwiki-source-4003fa8570daad2a67aaf127
    resource: repo://src/termdeck/profiles.py
  - id: openwiki-source-46cb3961e7da265beeba0507
    resource: repo://src/termdeck/session.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Architecture overview

termdeck is an SSH and serial terminal with saved profiles. Everything that is not
presentation lives in a UI-free core under `src/termdeck/`; four interfaces reuse it:

| Entry | Module | What it is |
|---|---|---|
| `termdeck` (no command) | `tui/app.py` | Full-screen Textual UI with tabs |
| `termdeck connect NAME` | `cli.py` + `console.py` | One session in the current terminal, like `ssh`/`plink` |
| `termdeck web` | `web/server.py` + `web/static/` | aiohttp server on 127.0.0.1 with an xterm.js page |
| `termdeck gui` | `gui.py` | pywebview window around the web UI, server on a background thread |

The console script `termdeck = "termdeck.cli:main"` (`pyproject.toml`) is the single
entry point; `cli.main` imports each UI lazily (`cmd_web`, `cmd_gui`, `cmd_tui`), so the
CLI never loads Textual, aiohttp or pywebview unless asked.

## Layers

```
 UIs        cli.py/console.py   tui/   web/server.py   gui.py (→ web)
              │                  │         │
 per tab      └──── controller.SessionController ─────┘   (TUI and web; connect uses session directly)
                                  │
 core         session.open_session → TerminalSession(inner)   ← vt.py, lineedit.py
                                  │
 transports   ssh.py (+ proxy.py, sftp.py)        serialport.py
                                  │
 data         profiles.py (options, Profile, ProfileStore) ← config.py, themes.py, errors.py
```

- **Data** — `profiles.py` declares every option once with `option()` and stores profiles
  through `ProfileStore`; `config.py` locates the settings folder and writes private files.
  See [Profiles and the option registry](../core/profiles-and-options.md) and
  [Profile storage](../core/profile-store.md).
- **Core session** — `session.py` defines the `Session` ABC, the `Prompter` protocol each UI
  implements, and `TerminalSession`, which applies a profile's terminal settings (charset,
  CR/LF, key mapping, filtering, logging, line editing) on top of a transport session.
  See [Sessions](../core/sessions.md) and [Terminal processing](../core/terminal-processing.md).
- **Per-tab controller** — `controller.SessionController` tracks one tab's state
  (connecting/connected/failed/ended), capabilities and reconnects. See
  [SessionController](../core/session-controller.md).
- **Transports** — `ssh.py` and `serialport.py`, selected through the `TRANSPORTS` map.
  See [SSH](../transports/ssh.md), [Proxies](../transports/proxies.md),
  [Serial](../transports/serial.md), [SFTP](../transports/sftp.md).

Core modules never import a UI. UIs import core modules; UI-to-UI reuse is limited to the
desktop GUI starting the web server.

## Lazy transport loading

`profiles.TRANSPORTS` maps a profile kind to a module name
(`{"ssh": "termdeck.ssh", "serial": "termdeck.serialport"}`). `session.open_raw_session`
imports the module with `importlib` on first use, so serial-only use never loads asyncssh.
Each transport module exposes the same three things: `open_session(profile, prompter, cols,
rows, scripted)`, `describe_error(exc)` and `ERRORS` (exception types that mean "the
connection failed", not "termdeck has a bug"). Error classification only consults
transports already present in `sys.modules`.

## From a profile to bytes on screen

1. A UI resolves a profile: a saved name (`ProfileStore.get`) or a quick target such as
   `bob@host:2222` or `COM3@9600` (`profiles.resolve_target`).
2. TUI and web create a `SessionController(profile, prompter, store, saved=...)`; for a saved
   profile each `connect()` re-reads it from the store, so a reconnect uses the latest saved
   options. `termdeck connect` calls `session.open_session` directly.
3. `open_session` validates the profile, opens the session log first (so an SSH packet log
   sees the handshake), imports the transport and wraps its session in a `TerminalSession`.
4. The transport asks the user through the UI's `Prompter` (passwords, host keys, banners).
5. The UI loops on `read()`; `TerminalSession` runs a pump task that reads the transport,
   logs, converts to UTF-8, filters remote escape sequences and queues output with a bounded
   backlog. `b""` means the session ended; `end_reason` says why.
6. Keystrokes go through `write()`: line editor or local echo, Enter/Backspace mapping, key
   remapping, then the profile's character set.

## Async model

Everything is asyncio. Each UI owns the event loop: `asyncio.run` in `cli.main`, Textual's
loop in the TUI, aiohttp's in the web server, and a dedicated loop on a daemon thread in the
desktop GUI (`gui.BackgroundServer`). Blocking work goes to threads: the profile store's file
lock (`asyncio.to_thread(self.store.get, ...)` in the controller), serial port opening and
device resolution, and the serial reader/writer threads (Windows ports cannot be polled by
the event loop). Backpressure is explicit: `TerminalSession` lets at most `OUTPUT_BACKLOG`
(64) chunks run ahead of the UI, the serial reader at most `BACKLOG` (256).

## Errors

`errors.py` defines `TermdeckError` and subclasses (`ProfileError`, `OptionError`,
`ProfileNotFound`, `ConnectError`, `SFTPFailure`) whose message is shown as is.
`session.is_expected` treats `TermdeckError`, `OSError`, timeouts, `UnicodeError` and the
transports' `ERRORS` as expected; anything else is a bug. `describe_error` gives every UI the
same one-line message (passed through `printable` so remote text cannot inject control
characters). The CLI maps this to exit codes: 2 for `KeyError`/`ValueError` (unknown profile,
invalid input), 1 for expected failures, 70 with a traceback for bugs, 130 for Ctrl+C.

## Related

- [Command line and connect console](../interfaces/cli-and-console.md)
- [Textual TUI](../interfaces/tui.md)
- [Web UI](../interfaces/web-ui.md) and [Desktop GUI](../interfaces/desktop-gui.md)
- [Security model](../security/model.md)
