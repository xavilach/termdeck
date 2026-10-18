---
type: interface
title: Web UI server and front end
description: The local aiohttp server behind `termdeck web` and the desktop window — localhost-only Host/Origin checks, one-time start tokens exchanged for per-tab secrets, the JSON/binary terminal WebSocket protocol driven by SessionController, WebPrompter, profile and SFTP REST endpoints with single-use download tickets — and the xterm.js single-page front end.
tags: [web, aiohttp, websocket, xterm-js, authentication, sftp, rest-api]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-0b95c579db49820d8c7bba4f
    resource: repo://src/termdeck/web/server.py
  - id: openwiki-source-9882304e8bec403af4144bd8
    resource: repo://src/termdeck/web/static/app.js
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Web UI server and front end

`src/termdeck/web/server.py` serves a single-page UI from `web/static/` (`index.html`,
`app.js`, `app.css`, and vendored xterm.js 5.5.0 with the fit, search, unicode11 and web-links
addons). `termdeck web` runs it in the foreground; the [desktop GUI](desktop-gui.md) runs the
same server on a background thread. Requires the `web` extra (aiohttp).

## Starting and logging in

`start(port=0)` binds **127.0.0.1 only** (random port by default) and returns the start URL
`http://127.0.0.1:PORT/#token=…`. The token is a one-time start link valid for `LINK_TTL`
(10 minutes), carried in the URL fragment so it never reaches server logs.

`serve()` prints the URL and, unless `--no-browser`, opens it through `open_in_browser`: it
writes a private temporary HTML file that forwards to the link (`location.replace`) and opens
that file, so the token never appears on a browser command line other local users can read
(`/proc`, Task Manager). The file is deleted after the link's lifetime.

The page (`login()` in `app.js`) removes the fragment from history, `POST /api/login` with the
token, and stores the returned per-tab **secret** in `sessionStorage`. Every API call sends it
in the `X-Termdeck-Auth` header; the WebSocket sends it as a subprotocol `auth.<secret>` next to
`termdeck`. There is deliberately **no cookie**: cookies are shared by every port on
127.0.0.1. `POST /api/link` creates a fresh start link for opening another window.

`Auth` compares tokens, secrets and tickets with `hmac.compare_digest`.

## Request security (`security` middleware)

- The `Host` header must be `127.0.0.1`, `localhost` or `[::1]` (blocks DNS rebinding) → 403.
- An `Origin` header, if present, must equal `http://<host>` → 403.
- Everything except `/`, `/api/login`, `/static/…` and `/api/download/…` needs a valid
  secret → 401.
- `KeyError`/`ValueError`/`TypeError` from a handler become 400 JSON errors.
- Response headers: a strict Content-Security-Policy (`default-src 'self'`, inline styles only
  for xterm.js, `frame-ancestors 'none'`), `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: no-referrer`; `/api/` responses are `Cache-Control: no-store`.
- Request bodies are limited to 1 MB except multipart uploads, which stream without a limit.

## REST endpoints

| Route | Purpose |
|---|---|
| `GET /api/options` | `describe_options()`, section titles, defaults, `THEMES` — the page builds its forms from this |
| `GET /api/profiles` | saved profiles (each with `summary` and computed `palette`), store warnings and path; an unreadable file returns an `error` and the page still works for quick connect |
| `POST /api/profiles`, `PUT /api/profiles/{name}` | add / update (rename) via the store in a thread; `OptionError` → 400 with `field: "section.key"` so the page marks it; not found → 404 |
| `DELETE /api/profiles/{name}` | delete |
| `POST /api/target` | resolve quick-connect text with `resolve_target` (saved name or unsaved profile) |
| `GET /api/serial-ports` | `available_ports()` in a thread |
| `GET /api/terminal` | the terminal WebSocket |
| `/api/sessions/{sid}/sftp…` | SFTP on a connected tab (below) |

## Terminal WebSocket protocol

One WebSocket per tab (`terminal` handler), heartbeat 30 s.

**Client → server** (JSON text): `open` first — `{name}` for a saved profile (read from the
store at connect time, so always the saved version) or `{profile}` for an unsaved one, plus
`cols`/`rows` (clamped to 1–1000) — then `input` (`data` string, UTF-8 encoded), `resize`,
`answer` (`id`, `value`) and `break`.

**Server → client**: binary frames of UTF-8 terminal output, and JSON text messages:
`state` (the [controller snapshot](../core/session-controller.md); on `connected` also the
session id `session` and the profile as connected, which the page applies), `prompt` and
`hostkey` (with an `id` to answer), `notice`, and `break` (`ok`, `message`).

The handler creates a `SessionController` with `on_change` queuing state messages in an
outbox that is flushed before the output that follows, so the page always sees state changes in
order. When connected, the session is registered in `app[sessions_key][sid]` *before* the page
hears of it (an open SFTP panel asks at once). If the browser goes away while connecting, the
connect task is cancelled. Malformed messages are ignored; the session continues. On exit the
session is unregistered and the controller closed.

`WebPrompter` sends `prompt`/`hostkey` requests with increasing ids and awaits the matching
`answer`; any non-string answer to a prompt is a cancel, only `true` trusts a host key, and all
pending questions are cancelled when the socket closes.

## SFTP endpoints

All work on the SSH connection of an open tab (`_sftp` looks up `sessions[sid].sftp()`, 404
otherwise), through the core `SFTP` class ([SFTP](../transports/sftp.md)):

- `GET …/sftp?path=` lists a folder.
- `POST …/sftp/ticket {path}` opens the file first (so errors show in the page rather than the
  browser's download UI), then returns a single-use `/api/download/{ticket}` URL valid for 60 s.
  Downloads therefore need no secret and leave none in the download history.
  `Content-Disposition` is built with an ASCII fallback and RFC 5987 `filename*`, control
  characters removed.
- `POST …/sftp/check {path, names}` says which upload names already exist, so the page asks
  first; `POST …/sftp/upload?path=&overwrite=1` streams multipart parts and without `overwrite`
  stops with 409 at the first existing file.
- `POST …/sftp/op` runs `mkdir`, `rename`, `remove` (optionally recursive), `rmdir` or `chmod`
  (octal or symbolic, parsed by the core).

## Front end (`app.js`)

Plain JavaScript, no framework. DOM is built with the `el()` helper, which appends children as
text nodes and sets attributes, and `textContent`, never `innerHTML` with remote or user text.
Main parts: login and the `api()` wrapper; sidebar with grouped profiles, recent sessions and
quick connect; a profile editor rendered from `/api/options` (only the selected kind's options,
advanced ones behind a switch and grouped, a forwards editor, server-side errors focused by
`field`); tabs with an xterm.js terminal each (theme from the profile's palette, bell and title
handling with a JS `cleanTitle`, paste stripped of control characters unless `paste_controls`);
modal prompts (host key dialogs use an arm delay because they may open while the user types);
an SFTP side panel with drag-and-drop upload; status bar with forwards summary. Shortcuts use
Alt+Shift in a browser (Ctrl+Shift+N/W belong to the browser) and Ctrl+Shift in the desktop
window. Closing a tab of a connected session asks first (`warn_on_close`); `close_tab` from the
server closes the tab automatically.

## Tests

`tests/test_web.py` exercises the server with aiohttp's test client (auth and tokens, Host and
Origin checks, profiles API, WebSocket protocol against the local SSH server and `loop://`,
SFTP endpoints and tickets). `tests/test_web_ui.py` drives the page in headless Chromium with
Playwright and skips when Playwright or the browser is missing (`uv run playwright install
chromium`).

## Related

- [Security model](../security/model.md)
- [SessionController](../core/session-controller.md)
- [Desktop GUI](desktop-gui.md)
