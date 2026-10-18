---
type: subsystem
title: Sessions and TerminalSession
description: The Session byte-stream ABC and Prompter protocol every UI implements, open_session, and TerminalSession — the layer that applies a profile's terminal settings (charset, CR/LF, remote filtering, printer capture, line editing, echo, answerback), bounded output backpressure, session and SSH packet logs, and error classification.
tags: [session, terminal, prompter, logging, errors, backpressure]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-46cb3961e7da265beeba0507
    resource: repo://src/termdeck/session.py
  - id: openwiki-source-c15dab6c7f96c1dd80c5dc4f
    resource: repo://tests/test_session.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Sessions and TerminalSession

`src/termdeck/session.py` is the contract between transports and UIs. Transports produce a
raw `Session`; `open_session` wraps it in a `TerminalSession` that applies the profile's
`terminal` settings, so every UI gets the same behaviour. UIs talk only to the wrapped session
(directly in `termdeck connect`, through the [SessionController](session-controller.md) in the
TUI and web UI).

## `Session` (ABC)

A byte stream to a terminal on the other side.

| Member | Meaning |
|---|---|
| `read()` | next chunk of output; `b""` once ended (abstract) |
| `write(data)`, `close()` | abstract |
| `write_eof()` | tell the remote side input ended; `False` if it cannot (serial) |
| `resize(cols, rows)` | report the terminal size; ignored by default |
| `send_break()` | only when `can_break` |
| `sftp()` | an `sftp.SFTP` on the same connection, or `None` |
| `forwards()` | list of `ForwardStatus` (spec, kind, public, active, port, error) |
| `title`, `can_break`, `has_sftp`, `interactive` | flags the controller turns into capabilities |
| `end_reason` | why it ended, set before `read()` returns `b""`; `None` = normal end |
| `notice(text)` / `on_notice(show)` | termdeck's own messages (`[termdeck] …`), never logged; notices issued before a UI subscribes are buffered and replayed |

## `Prompter` (protocol)

Each UI implements `ask(prompt, secret=True) -> str | None` (None = cancel),
`confirm_host_key(HostKey) -> bool` and optionally `notify(text)` for banners and login
instructions (the module-level `notify()` helper skips prompters without it and blank text).
`HostKey` carries host, port, key type, fingerprint, `changed`, `old_fingerprints` and the
`known_hosts` path. Implementations: `cli.ConsolePrompter`/`BatchPrompter`/
`PasswordFilePrompter`, `tui.app.TUIPrompter`, `web.server.WebPrompter`.

## Opening a session

`open_session(profile, prompter, cols=80, rows=24, raw=False)`:

1. `profile.validate()`;
2. if `terminal.log_file` is set, opens the log **before connecting** (`open_log`, with the
   path from `log_path`), and for log mode `ssh` on an SSH profile attaches asyncssh's logger
   to it, so the key exchange and login are logged;
3. `open_raw_session` imports the transport from `TRANSPORTS` and calls its
   `open_session(..., scripted=raw)`;
4. wraps the result in `TerminalSession`. Any failure closes the log, the SSH log handler and
   the inner session, so nothing half set up stays behind.

`raw=True` is for scripted use (`termdeck connect` with pipes): bytes pass unchanged and are
only logged; SSH then keeps remote stderr apart.

## TerminalSession pipeline

UIs always exchange **UTF-8** with it.

**Output** — a pump task (started by the first `read()`) reads the inner session:

1. writes the raw bytes to the log (or a printable version in log mode `printable`, via
   `vt.PrintableLog`), buffered and flushed within one second;
2. on `ENQ` (0x05) with an answerback configured, writes the answerback back;
3. `_received`: decode the profile's character set to UTF-8 → `PrinterCapture` (remote
   printing to the printer file) → `RemoteFilter` (drops escape sequences the "remote may …"
   options forbid; a cut sequence is released after 50 ms) → implicit CR/LF in one pass, each
   CR or LF becoming exactly one new line, with a trailing CR held 50 ms to see whether LF
   follows;
4. with line editing on, keeps the last 200 characters and switches the editor to secret mode
   when they end like a password prompt (`_SECRET_PROMPT`: password, passphrase, PIN,
   Passwort, mot de passe);
5. queues the chunk.

Backpressure: at most `OUTPUT_BACKLOG = 64` remote chunks may wait for the UI (an
`asyncio.Semaphore`); beyond that the pump stops reading and the remote side is held back by
flow control. Local echo and notices bypass the limit so typing never blocks. When the inner
session ends, the pump flushes the filter, the held CR and the log, then queues `b""`; later
reads keep returning `b""`. An exception in the pump ends the session with `describe_error`
as `end_reason` (logged with a traceback only if unexpected).

**Input** — `write(data)`: with line editing, `LineEditor.feed` returns echo and the
completed line (if any); otherwise optional local echo. Then `_translate` (Enter → CR/CRLF/LF,
Backspace DEL → BS, Home/End and function key remapping via `vt.remap_keys`) and `_encode`
(UTF-8 → the profile's charset, incremental so multibyte keys may span writes).

At construction, non-raw sessions also emit `vt.initial_modes` (application cursor keys /
keypad, origin mode, auto-wrap) as local output. Details of the vt helpers and the line editor:
[Terminal processing](terminal-processing.md).

Logging and printer failures (disk full, drive gone) stop only the log/printer and show a
`[termdeck] logging stopped: …` notice; the session continues.

## Session logs

- `log_path(template, profile)` replaces `{name}`, `{host}` (SSH host or serial device) and
  `{date}` (`%Y%m%d-%H%M%S`) with file-name-safe values (Windows reserved names like `CON` or
  `COM1` get a `_` prefix); other text, including unknown placeholders, is kept.
- `open_log` opens via `config.open_private(..., own_dir=False)` — the log folder belongs to
  the user, symlinks and foreign files are refused — in append or overwrite mode, with an
  optional `=~=~=~= termdeck log … =~=~=~=` header.
- SSH packet logging: asyncssh logs process-wide, so while any such log is open it receives
  every SSH connection of this process. `_ssh_logging` raises asyncssh to debug level 2 (not 3,
  which could contain typed secrets); the last handler removed restores the previous level.
  The `hide_secrets` filter replaces environment variable values in `Env:` records.

## Error classification

- `is_expected(exc)`: `TermdeckError`, `OSError`, timeouts, `UnicodeError`, or a loaded
  transport's `ERRORS` → a failure of the network, device, files or input; anything else is a
  bug.
- `describe_error(exc)`: asks each loaded transport's `describe_error`, then maps
  `TermdeckError` (message as is), timeouts ("Connection timed out"), refused connections,
  `OSError` with `strerror`, and unexpected errors ("Internal error (…); please report it").
  The result goes through `printable()`, which drops control and Unicode format/separator
  characters so remote text cannot move the cursor or disguise itself.
- `end_message(reason)` and `send_break(session)` give every UI the same wording.

## Tests

`tests/test_session.py` covers the bounded backlog (local echo still appears, close never
hangs), end reasons, unexpected pump errors, implicit CR/LF never doubling a newline,
answerback, characters split across chunks, `is_expected`, every transport providing the
entry points, and serial use not loading asyncssh. `tests/test_core.py` covers Enter/Backspace
mapping, charset conversion, local echo and logs; `tests/test_store.py` covers log paths and
log file safety.

## Related

- [Architecture overview](../architecture/overview.md)
- [SSH transport](../transports/ssh.md), [Serial transport](../transports/serial.md)
- [Security model](../security/model.md)
