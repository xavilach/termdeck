---
type: subsystem
title: "SessionController: tab connection lifecycle"
description: controller.SessionController is one tab's connection in the TUI and web UI — its connecting/connected/failed/ended state, capabilities (break, SFTP, typing), reconnects that re-read the saved profile, epoch-based cancellation of stale connects, close-on-exit and the JSON snapshot every UI renders.
tags: [controller, lifecycle, state-machine, reconnect, tabs]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-9ece4cae043d1d4b8f324f00
    resource: repo://src/termdeck/controller.py
  - id: openwiki-source-0b95c579db49820d8c7bba4f
    resource: repo://src/termdeck/web/server.py
  - id: openwiki-source-be32c02a60396a3ba808253c
    resource: repo://tests/test_controller.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# SessionController: tab connection lifecycle

`src/termdeck/controller.py` holds what a tab needs to know about its connection, so the TUI
and the web UI behave the same way. It sits between a UI and
[`session.open_session`](sessions.md). (`termdeck connect` does not use it; it has one session
and exits with it — see [CLI](../interfaces/cli-and-console.md).)

## States

```
           connect()                       read() returns b""
 ┌──────────────┐  ok   ┌───────────┐ ──────────────────────▶ ┌───────┐
 │  CONNECTING  │ ────▶ │ CONNECTED │                         │ ENDED │
 └──────────────┘       └───────────┘ ── close() ───────────▶ └───────┘ ("Disconnected", closed_locally)
        │ error / cancel / close()
        ▼
   ┌────────┐
   │ FAILED │   never connected: refused, cancelled, bad key, deleted profile...
   └────────┘
```

`State` is a `StrEnum` (`connecting`, `connected`, `failed`, `ended`); `STATUS_TEXT` gives the
label shown in every UI. `message` holds why it failed or ended, built by
`session.describe_error` or `session.end_message` ("Session ended[: reason]"). `on_change(self)`
is called after every state change; UIs re-render from it.

- `clean` — set when the session ends with no `end_reason` (the remote program ended normally;
  an SSH non-zero exit status gives a reason, so it is not clean).
- `closed_locally` — the user closed it; such a tab never closes itself.

## Connecting and reconnecting

`connect(cols, rows)`:
1. closes a previous session, if any (reconnect is just `connect()` again);
2. resets `clean`, `closed_locally` and `capabilities`, and enters `CONNECTING`;
3. if `saved`, re-reads the profile from the store **in a thread** (the store takes a file
   lock), so a reconnect uses the options as last saved;
4. calls `open_session(profile, prompter, cols, rows)`;
5. on success, records `Capabilities(send_break=session.can_break, sftp=session.has_sftp,
   typing=session.interactive)` and enters `CONNECTED`; returns `True`.

Failures return `False` with state `FAILED`: `ProfileNotFound` says the profile was deleted or
renamed in another window; anything else goes through `describe_error`. Cancellation sets
`FAILED`/"Cancelled" and re-raises.

`saved` is only honoured when a store is given. `rename(old, new)` lets a UI follow a rename of
the saved profile so the next connect reads the new name; unsaved profiles are left alone.

### Epochs: closing while connecting

`close()` bumps `_epoch`. A `connect()` remembers the epoch it started in; if it differs when
`open_session` returns, the user no longer wants the connection: the new session is closed and
discarded, and the state set by `close()` is kept. The same check stops a cancelled connect
from overwriting the state after a close.

## Reading and ending

`read()` returns the session's next output. When it returns `b""` while `CONNECTED`, the
controller sets `clean = end_reason is None` and moves to `ENDED` with `end_message(reason)`.

`close()` (tab closed, or before a reconnect) changes the state *first* — `ENDED` if there was
a session, else `FAILED`, both with "Disconnected" and `closed_locally = True` — and only then
closes the session, so output ending meanwhile is not mistaken for the remote side ending. A
state that is already `FAILED` or `ENDED` is not overwritten.

`closes_tab` implements the `terminal.close_on_exit` option: true only when `ENDED`, not closed
locally, and either `always` or (`clean` and the session ended cleanly). A failed connection
never closes its tab.

## Capabilities and forwards

- `send_break()` returns `(sent, message)` and refuses when not connected or not capable;
  otherwise delegates to `session.send_break`, which never raises for a refused break.
- `forwards()`, `forwards_failed()` and `forwards_summary()` ("2 forwards",
  "1 of 2 forwards failed") come from `Session.forwards()` (`ForwardStatus` entries).

## Snapshot

`snapshot()` is the JSON-ready state every UI shows: `state`, `status`, `message`, `live`
(connecting or connected); when connected also `title`, `capabilities`, `forwards` (each with
a `text` from `ForwardStatus.describe()`), `forwards_summary`, `forwards_failed`; when ended
`clean` and `close_tab`. The web server sends it as a `state` message on the WebSocket (adding
the session id and the profile as connected); the TUI's `TerminalView` reads it for tab titles
and the status bar. See [Web UI](../interfaces/web-ui.md) and [TUI](../interfaces/tui.md).

## Tests

`tests/test_controller.py` covers the state sequence, clean vs unclean ends, re-reading saved
profiles, deleted profiles, rename, `close_on_exit`, break and SFTP capabilities per transport,
forwards summaries, closing while reading or connecting, a second connect closing the first,
cancellation, and the snapshot contents.
