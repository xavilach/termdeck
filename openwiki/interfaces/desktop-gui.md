---
type: interface
title: Desktop GUI (pywebview)
description: How `termdeck gui` wraps the web UI in a pywebview window — the web server on a background thread, WebView2 detection on Windows, the minimal exposed JS API, close confirmation while sessions are connected, window size persistence in logical pixels and the system-theme background.
tags: [gui, pywebview, desktop, webview2, windows]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-69cc0e49919868a1c629913c
    resource: repo://src/termdeck/gui.py
  - id: openwiki-source-9882304e8bec403af4144bd8
    resource: repo://src/termdeck/web/static/app.js
  - id: openwiki-source-b603cff1c38be901a3172a4f
    resource: repo://tests/test_gui.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Desktop GUI (pywebview)

`src/termdeck/gui.py` is a thin desktop shell: it runs the [web UI](web-ui.md) server on a
background thread and shows its page in a pywebview window. All terminal and profile logic is
the web UI's. Requires the `gui` extra (`pywebview`, `aiohttp`); `termdeck gui` calls
`gui.run()`.

## Startup (`run`)

1. Import `webview`; if missing, raise `ValueError` suggesting `termdeck[gui]` or
   `termdeck web` (the CLI reports it with exit code 2).
2. On Windows, require the Edge **WebView2** runtime (`has_webview2` checks the Edge updater's
   registry keys, machine-wide and per user, for a `pv` version other than `0.0.0.0`) and force
   the `edgechromium` backend — otherwise pywebview would fall back to Internet Explorer, which
   cannot run the UI.
3. Allow downloads (SFTP downloads use WebView2's Save As dialog).
4. Start `BackgroundServer`, which runs `web.server.start()` with its own `asyncio.run` on a
   daemon thread named `termdeck-server`, waits up to 15 s for the URL, and re-raises a startup
   error. The start URL (with its one-time token) gets `&desktop=1`, which the page stores in
   `sessionStorage` to switch to desktop behaviour: Ctrl+Shift shortcuts instead of the
   browser's Alt+Shift ones, full-screen and always-on-top buttons, no "open in browser" footer.
5. Create the window at the saved size, with a background matching the system theme
   (`system_dark` reads Windows' `AppsUseLightTheme`; dark where it cannot be read) to avoid a
   white flash.
6. `webview.start(private_mode=False, storage_path=data_dir()/"webview")` so UI preferences
   persist; the folder is not tightened with `private_dir` because WebView2 manages its own
   access entries. `server.stop()` runs when the window loop ends.

## Exposed JS API

`desktop_functions(window)` returns plain functions given to `window.expose`:
`toggle_fullscreen()` and `set_on_top(on)`; the page calls them as
`window.pywebview.api.*`. A `js_api` object is deliberately avoided: pywebview resolves dotted
names on such an object with `getattr`, private attributes included, so page script could walk
from it to the window and on to `os.system`. Exposed functions are found by exact name only.

## Closing

Like PuTTY, the window asks before closing only while sessions are connected
(`server.sessions`, the web server's map of connected terminals). On Windows the WinForms
confirmation dialog runs inside the `closing` handler. On GTK and Cocoa the dialog is queued on
the GUI thread that is running the handler, so asking there would deadlock: the handler keeps
the window, asks from a helper thread, and destroys the window if confirmed.

## Window size

Only the size is saved (`window.json` in the config folder, via `atomic_write`), never the
position, which could be on a screen that is gone. It is saved on close only when the window
is in its normal state (maximize/minimize/restore events are tracked); a minimized size is
ignored. Sizes are converted to logical pixels with the Windows display scale factor, since
pywebview reports device pixels but sizes in logical ones. `load_size` clamps to
`MIN_SIZE` (900×560) and 10000, and falls back to 1280×800 on any bad file (including
`Infinity`). Saving errors never stop the window from closing.

## Tests

`tests/test_gui.py` uses a fake `webview` module: the background server serves and stops, the
missing-WebView2 message, close confirmation on both dialog paths, exposed functions rather
than an object, size round trips, clamping, logical pixels and the theme background.

## Related

- [Web UI server and front end](web-ui.md)
- [Profile storage and private files](../core/profile-store.md) (config and data folders)
- [Architecture overview](../architecture/overview.md)
