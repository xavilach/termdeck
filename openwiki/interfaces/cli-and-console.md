---
type: interface
title: Command line and connect console
description: The termdeck command line (profile management, connect, tunnel, sftp, ports, forget-host, and launching the TUI, web and desktop UIs), its console, batch and password-file prompters and exit codes, and console.py which runs `termdeck connect` interactively with a Ctrl+] escape or as a byte-exact pipe like ssh/plink.
tags: [cli, console, connect, plink, prompter, exit-codes, windows]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-40dcfdf1b085605444d2f636
    resource: repo://src/termdeck/cli.py
  - id: openwiki-source-05b605d22d3c21243f1c4e2a
    resource: repo://src/termdeck/console.py
  - id: openwiki-source-ff7529f692c58b456b32f1d7
    resource: repo://tests/test_connect.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Command line and connect console

`src/termdeck/cli.py` is the program entry point (`termdeck = "termdeck.cli:main"`, also
`python -m termdeck`). `src/termdeck/console.py` runs one session in the current terminal for
`termdeck connect`.

## Commands

`build_parser()` defines the subcommands; with no command `termdeck` opens the TUI.

| Command | What it does |
|---|---|
| `tui [TARGET]` (default) | Textual UI, optionally opening a profile or quick target ([TUI](tui.md)) |
| `connect TARGET [COMMAND…]` | one session here, like `ssh`/`plink` (below) |
| `tunnel NAME` | SSH forwards only, no shell; prints each forward, waits until the server closes |
| `sftp TARGET [COMMAND…]` | psftp-like file transfer ([SFTP](../transports/sftp.md)) |
| `web [--port] [--no-browser]` | serve the browser UI ([Web UI](web-ui.md)) |
| `gui` | desktop window ([Desktop GUI](desktop-gui.md)) |
| `list [--json]`, `show NAME` | list profiles, print one as JSON |
| `add ssh NAME HOST [-p -u -i -L -R -D -g --set]` / `add serial NAME DEVICE [-b --databits --parity --stopbits --flow -g --set]` | create a profile; serial defaults and choices come from the option declarations |
| `set NAME section.option=value… [-L/-R/-D] [--no-forwards]` | change options |
| `rename`, `rm` | rename or delete a profile |
| `options` | every option with values and default, for `set`/`--set` |
| `ports` | serial ports with their USB identity |
| `forget-host HOST[:PORT]` | remove a host's entries from termdeck's `known_hosts` |

Profile commands go through `ProfileStore` ([Profile storage](../core/profile-store.md)); option
text is converted by `apply_settings`/`_coerce` ([Option registry](../core/profiles-and-options.md)).
`resolve_profile` accepts a saved name or a quick target; if `profiles.json` is unreadable it
warns and still allows quick targets. Commands that need a transport import it lazily.

## `main` and exit codes

`main` reconfigures stdin/stdout/stderr to UTF-8 on Windows (stdin accepts a BOM), parses
arguments, runs the command (awaiting it with `asyncio.run` if it is a coroutine) and maps
outcomes:

| Code | When |
|---|---|
| 0 | success |
| 1 | expected failure (connection, file) — `describe_error` message |
| 2 | `KeyError` / `ValueError`: unknown profile or option, invalid value |
| 70 | unexpected exception: message plus traceback to report |
| 130 | Ctrl+C (the terminal's saved termios state is restored, e.g. after a password prompt) |
| remote status | `connect` returns the remote program's exit status |

For `connect`, `_split_command` parses arguments like plink: options may appear anywhere up to
the first plain word after the target; that word and everything after it (or after `--`) is
the remote command.

## Prompters

- `ConsolePrompter` asks on the controlling terminal (`/dev/tty` or `CONIN$`), never on stdin
  — a pipe is session data, and a `y` in it must not trust a host key. Prompts go to stderr.
  Reads happen on a daemon thread so Ctrl+C at a prompt can exit immediately. Without a
  terminal (cron) a question fails with `ConnectError`. Host key questions print the new and
  old fingerprints and warn loudly for a changed key.
- `BatchPrompter` (`--batch`, like `plink -batch`) never asks: questions fail and host keys are
  refused with an explanation on stderr.
- `PasswordFilePrompter` (`--password-file`) answers only the target's first password prompt
  (or a hidden keyboard-interactive prompt prefixed with the target) from the file's first line,
  once; jump hosts, key passphrases and other questions go to the inner prompter.

## `termdeck connect`

`_connect_profile` resolves the target, applies `--set`, then the ssh/plink-style flags (SSH
only; refused for serial): remote command (arguments or `-m FILE`), `-N`, `-s SUBSYSTEM`,
`--nc HOST:PORT`, `-4/-6`, `-L/-R/-D`, `--hostkey` (pinned fingerprints), `--proxycmd`,
`--loghost`, `--no-trivial-auth`, `--share/--no-share`, `-t/-T`. Like ssh, a command, a
subsystem or piped stdin gets no remote terminal unless `-t`. `-v`/`-vv` and `--ssh-log FILE`
send asyncssh logging to stderr or a private file, with environment values hidden.

`cmd_connect` then chooses a mode:

- **Interactive** (stdin and stdout are terminals, a remote terminal is allocated, no `--nc`):
  `open_session(...)` with full terminal settings and `console.interact`.
- **Scripted** (anything else): `open_session(..., raw=True)` — bytes pass unchanged — and
  `console.pipe`; termdeck's notices go to stderr so they never mix with stdout data.
- **`--nc`**: `SSHConnection.open_stream(host, port)` (like `ssh -W`), piped.

It returns the remote exit status when there is one, else 1 if the session ended with a
reason, else 0. In interactive mode (or when a script's session ended abnormally without a
status) it prints `[termdeck] Session ended[: reason]` on stderr.

## `console.py`

### Interactive: `interact(session, banner)`

The terminal is put in raw mode (`_PosixConsole` with `tty.setraw(TCSADRAIN)` to keep typeahead;
`_WindowsConsole` uses VT input/output modes). The banner is printed only once raw mode is on,
so keys typed after it are not lost. Input is read with `loop.add_reader` on POSIX, resize via
`SIGWINCH`. One task forwards input in order, another copies output to stdout.

`EscapeFilter` implements **Ctrl+]** escapes: then `q` or `.` quits, `b` sends a serial break
(a refusal is reported and the session continues), `]` or Ctrl+] sends a literal Ctrl+]; any
other key cancels. The kitty-keyboard and modifyOtherKeys encodings of Ctrl+] are recognized
too, so a remote program cannot disable the escape. On exit `RESET_MODES` undoes modes the
remote side may have left on (kitty keys, mouse, bracketed paste, alternate screen, cursor
keys, origin mode, no-wrap) and shows the cursor.

### Scripted: `pipe(session)`

stdin is read on a daemon thread in 64 KB chunks, at most `PIPE_BACKLOG` (4) ahead of what the
session has accepted. Output goes to stdout byte for byte; SSH sessions without a terminal also
copy remote stderr to local stderr. At the end of stdin the session gets `write_eof()`; sessions
that cannot signal EOF (serial) are closed `DRAIN` (0.5 s) later. On a Windows console stdout,
`_ConsoleOutput` enables VT processing and writes whole UTF-8 characters.

## Tests

`tests/test_cli.py` (commands, settings, prompters, exit codes, sftp command) and
`tests/test_connect.py` (piped connect against the local SSH server: exit statuses including
signals, EOF, stderr kept apart, `--batch`, password files never answering jump hosts or
passphrases, `--nc`, `--hostkey`, plink-style argument order, logs hiding secrets, stdin read
only a few chunks ahead, serial drain).

## Related

- [Sessions and TerminalSession](../core/sessions.md)
- [SSH transport](../transports/ssh.md)
- [SFTP file transfer](../transports/sftp.md)
