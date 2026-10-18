---
type: subsystem
title: Terminal byte processing, line editing and themes
description: The shared terminal helpers in vt.py (key remapping, initial modes, the RemoteFilter that enforces "remote may …" options, printer capture, bell limiting, title and paste cleaning, printable logs), the local LineEditor for devices without a line editor, and the colour themes in themes.py.
tags: [terminal, escape-sequences, vt, line-editing, themes, putty-features]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-fa460805ad5d829f7bb6a062
    resource: repo://src/termdeck/lineedit.py
  - id: openwiki-source-0733578a02057076c3f6f398
    resource: repo://src/termdeck/themes.py
  - id: openwiki-source-f9719ac14ee153ecb2aef8e3
    resource: repo://src/termdeck/tui/terminal.py
  - id: openwiki-source-c8f8f2eae68aba2a3c283c10
    resource: repo://src/termdeck/vt.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Terminal byte processing, line editing and themes

Byte-level terminal behaviour shared by every UI lives in three small, pure modules. They are
driven mainly by [TerminalSession](sessions.md); the TUI and web front end use a few helpers
directly. UIs send keys as xterm does; the profile decides what the remote side receives.

## `vt.py`

### Keys sent to the remote side

- `remap_keys(data, settings)` rewrites xterm Home/End and F1–F12 sequences for the profile's
  `home_end` (`xterm`, `standard` → `ESC[1~`/`ESC[4~`, `rxvt` → `ESC[7~`/`ESC[8~`) and
  `function_keys` style (`xterm`, `vt400`, `linux`, `vt100`, `sco`). With xterm styles it is a
  no-op.
- `initial_modes(settings)` returns escape sequences shown **locally** (not sent) at session
  start: auto-wrap off (`ESC[?7l`), DEC origin mode, application cursor keys, application
  keypad.

### `RemoteFilter`: what the remote side may do

The equivalent of PuTTY's Features panel. Each `terminal.allow_*` option that is off removes
the matching sequences from remote output:

| Option | Removed |
|---|---|
| `allow_app_keys` | private modes 1 and 66, `ESC =` / `ESC >` |
| `allow_mouse` | mouse modes 9, 1000–1003, 1005, 1006, 1015, 1016 |
| `allow_alt_screen` | modes 47, 1047, 1048, 1049 |
| `allow_bracketed_paste` | mode 2004 |
| `allow_resize` | `CSI … t` window operations |
| `allow_clear_scrollback` | `CSI 3 J` |
| `allow_title` | OSC 0, 1, 2 |
| `allow_charset` | `ESC ( ) * +` designations, SI/SO |

Only blocked modes are dropped from a combined `CSI ? a;b h` sequence. CSI with more than 64
parameter bytes is dropped as garbage. Both 7-bit (`ESC [`, `ESC ]`) and 8-bit C1 introducers
(UTF-8 encoded U+009B/U+009D/U+009C) are recognized, since xterm.js and pyte accept both.

When all options allow everything the filter is inactive and passes data through. Otherwise a
sequence cut at a chunk boundary is held until the rest arrives; one still unfinished after
`HOLD_LIMIT` (4096) bytes is dropped, never let through. `flush()` releases held data (output
quiet for 50 ms, or the session ended), dropping an unfinished sequence.

### Other helpers

- `PrinterCapture` diverts text between `ESC[5i` and `ESC[4i` (remote-controlled printing) to
  the printer file instead of the screen, handling markers split across chunks.
- `BellLimiter` (used by the TUI) mimics PuTTY's bell overload: at most one bell per 0.25 s,
  and 5 s of silence after more than 5 bells within 2 s.
- `clean_title` makes a remote window title fit for a tab: one line, control and bidi/zero-width
  characters removed, at most 60 characters. The web front end has a JS equivalent.
- `clean_paste(text, allow_controls)` strips control characters from a paste (except tab and
  line ends) unless `terminal.paste_controls` is on: an ESC in a paste could end bracketed paste
  early and run commands. The web front end applies the same regex in `app.js`.
- `PrintableLog` produces "printable text" session logs: escape sequences and control
  characters removed, sequences cut between chunks kept for the next, CR and CRLF turned into LF.

## `lineedit.py`: local line editing

`LineEditor` serves `terminal.line_editing`, for devices with no line editor of their own.
`feed(data) -> (echo, send)`: keys are edited locally and only a complete line is sent on Enter
(`line + "\r"`).

- Editing: Left/Right, Home/End (also Ctrl+A/Ctrl+E), Backspace, Delete, Ctrl+U (kill line),
  Ctrl+K (kill to end), Ctrl+W (kill word), Up/Down history (200 lines, consecutive duplicates
  and blank lines not stored; the draft line is restored after browsing). Echo is minimal
  redraw output (cursor moves, rewrite from the first changed character, `ESC[K`).
- Other control keys (Ctrl+C, Ctrl+D, Ctrl+Z…) are sent at once; Ctrl+C also drops the local
  line and echoes `^C`. Unhandled escape sequences (function keys) are dropped; terminal
  replies in OSC/DCS strings are parsed to their terminator; sequences longer than 4096
  characters are discarded. CR LF from a paste counts as one Enter.
- `secret`: set by `TerminalSession` when output ends like a password prompt. While set, typed
  characters are not echoed and the line is not kept in history; Enter clears it.

## `themes.py`

`THEMES` holds xterm.js-style palettes (`foreground`, `background`, `cursor`,
`selectionBackground` and the 16 ANSI colours) for `dark`, `light`, `solarized`, `dracula` and
`putty`; `THEME_NAMES` provides the choices of the `terminal.theme` option. `parse_colours`
validates `name=#rrggbb` override lines (names matched case-insensitively, ignoring `_`, `-`
and spaces) and `palette(theme, colours)` merges overrides into a theme (unknown theme → dark).
The web server sends `THEMES` with `/api/options` and each profile's `palette`; the TUI's
`TerminalView` uses `palette` for its pyte rendering.

## Tests

`tests/test_vt.py` (key remapping, filter rules and split sequences, printer capture, bells,
titles, paste, printable logs) and `tests/test_lineedit.py`.

## Related

- [Sessions and TerminalSession](sessions.md)
- [Textual TUI](../interfaces/tui.md), [Web UI](../interfaces/web-ui.md)
- [Security model](../security/model.md)
