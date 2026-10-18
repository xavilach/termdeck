---
type: interface
title: Textual TUI
description: The default `termdeck` interface — a Textual app with a profile sidebar and quick connect, terminal tabs each driven by a SessionController and rendered with pyte, a profile editor generated from describe_options(), modal prompter dialogs, menu keys behind a Ctrl+] escape, and an SFTP file panel.
tags: [tui, textual, pyte, terminal-emulation, profile-editor]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-faaa686a5bbf335a2292ef9b
    resource: repo://src/termdeck/tui/app.py
  - id: openwiki-source-f9719ac14ee153ecb2aef8e3
    resource: repo://src/termdeck/tui/terminal.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Textual TUI

`src/termdeck/tui/` is what `termdeck` opens (`cli.cmd_tui` → `tui.app.run`). It has three
modules: `app.py` (application, dialogs, profile editor), `terminal.py` (the terminal widget)
and `files.py` (the SFTP panel).

## Layout and keys

`TermdeckTUI` composes a sidebar (title, quick-connect `Input`, profile `ListView` grouped by
profile group) and a main area (toolbar, `TabbedContent` with a Welcome pane, footer).

In a terminal **every key goes to the remote side**. Ctrl+] leaves the terminal for the menus
and Esc (or Ctrl+] again) returns. Menu keys: `n` new, `e` edit, `c` duplicate, Delete delete
profile; `w` close tab (asks while connected, per `warn_on_close`), `r` reconnect, `/` quick
connect; `f` find in scrollback, `t` files (SFTP), `s` toggle sidebar; `b b` sends a serial
break (the second B within `BREAK_AGAIN` = 3 s, so a stray key cannot reset a device); `q`
quit. Ctrl+Q quits from the menus and asks while sessions are connected; inside a terminal
Ctrl+Q goes to the device. `check_action` enables session actions only when they apply
(break and files need a connected session with that capability; reconnect needs a session
that is not live).

Profile file problems never stop the app: an unreadable `profiles.json` is reported and the
list starts empty; skipped entries are notified once each.

## Tabs: `TerminalView`

Each tab holds a `TerminalView` widget that owns a
[`SessionController`](../core/session-controller.md) (saved profiles are re-read on each
connect). Its `_run` task connects with the widget's size, then reads output and feeds it to a
pyte `HistoryScreen` subclass in `FEED_SLICE` (16 KB) slices, yielding to the event loop
between slices so a flood does not freeze other tabs or the keyboard. On failure or end it
prints the message and "Press Enter to reconnect"; if `controller.closes_tab`, it posts
`CloseRequested`.

- **Input**: `key_to_bytes` turns Textual keys into xterm bytes (application cursor mode
  respected, modifier-coded arrows). Keys and terminal replies (cursor reports, device
  attributes from pyte) go out through one queue and writer task, in order. Forwards-only
  connections (`capabilities.typing` false) ignore typing. A write failure shows a notice and
  keeps the tab usable.
- **Paste**: `vt.clean_paste` unless `paste_controls`; newlines become CR; wrapped in bracketed
  paste markers when the remote side enabled mode 2004.
- **Rendering**: colours from `themes.palette(theme, colours)` (re-applied on connect since the
  saved profile may have changed), one cached Rich `Style` per cell look.
- **Scrollback**: Shift+PgUp/PgDn and the mouse wheel; the view stays on the lines being read
  as new output arrives unless `scroll_on_output`; `scroll_on_key` returns to the bottom.
- **Selection** by dragging, copied on release through OSC 52 (`app.copy_to_clipboard`) and,
  on Windows, directly to the clipboard (the console host ignores OSC 52). Find searches the
  history.
- **Bell**: `vt.BellLimiter`, then sound, a 0.15 s visual flash, or both; a bell in a hidden tab
  marks the tab. **Title**: `vt.clean_title` before it reaches the tab label.
- **Notices** (banners, `[termdeck]` messages) are shown in grey inside the terminal.

See [Terminal processing](../core/terminal-processing.md) for the helpers.

## Prompter: `TUIPrompter`

Implements the [Prompter protocol](../core/sessions.md) with modal screens: `InputDialog`
(secret input hidden) for `ask`, `ConfirmDialog` for host keys. Unknown keys show the
fingerprint; changed keys get a warning, the old fingerprints and a red button. Cancel is the
default button for both, because the dialog may pop up while the user is typing. The last
`notify` text is repeated in the next question and shown in the terminal.

## Profile editor

`ProfileEditor` is generated from `describe_options()` (never hand-coded fields; see
[Option registry](../core/profiles-and-options.md)): `Select` for choices, `Switch` for
booleans, `Input` (password mode for `secret` widgets) or `TextArea` for multiline, a device
field with a port picker (`PortPicker`, from `serialport.available_ports`). Both SSH and serial
sections are built; CSS shows only the selected kind's, and advanced options are hidden until
the **Advanced** switch is on (remembered for the session). Related advanced options appear
under their `group` subheading. Forwards are edited as one comma-separated text
(`L 8080:db:5432, R …, D 1080`). On save, an `OptionError` is mapped to the field id
`section--key`; the editor turns Advanced on if needed, marks the field invalid and focuses it.
Cancel asks only if values changed. Renaming a saved profile calls `controller.rename` on its
open tabs.

## Escaping

All remote or user text is shown with `markup=False` (dialog titles, prompts, bodies, help,
notifications, group names), following the project rule that such text never goes into Textual
markup unescaped.

## Files panel (`files.py`)

`FilesScreen` is a modal SFTP browser on the tab's connection (`session.sftp()`): list,
type a path, Enter to open a folder or download a file (to the Downloads folder —
`download_dir` asks Windows where it is — or the home folder), Backspace for the parent, `u`
to upload a local path, Delete to delete (confirmed), F5 to refresh; transfers show progress.
`InputDialog`/`ConfirmDialog` are reused from `app.py`; errors come through `describe_error`.
Rename and new folder are offered by the web panel, not here. See
[SFTP](../transports/sftp.md).

## Tests

`tests/test_tui.py` drives the app with Textual's pilot: profile list and editor (generated
fields, kinds, advanced, error focus), tabs and reconnect, key mapping, paste cleaning,
selection, scrollback, bell, prompts and the files panel.

## Related

- [SessionController](../core/session-controller.md)
- [Profiles and the option registry](../core/profiles-and-options.md)
- [Web UI](web-ui.md) — the other tabbed interface, with the same controller
