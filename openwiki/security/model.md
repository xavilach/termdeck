---
type: concept
title: Security model
description: Cross-cutting security rules in termdeck — remote text treated as hostile (printable, RemoteFilter, title and paste cleaning, no unescaped markup), host key trust and pinning with revocation, secrets never stored and kept out of logs, owner-only files and safe log creation, the localhost web server's authentication, public forwards and SFTP overwrite protections.
tags: [security, threat-model, host-keys, secrets, web-auth, escaping, file-permissions]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-4003fa8570daad2a67aaf127
    resource: repo://src/termdeck/profiles.py
  - id: openwiki-source-46cb3961e7da265beeba0507
    resource: repo://src/termdeck/session.py
  - id: openwiki-source-cb7f1de01bf167b93eb32524
    resource: repo://src/termdeck/sftp.py
  - id: openwiki-source-29d66090aef4ef6e672b9810
    resource: repo://src/termdeck/ssh.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Security model

termdeck connects to machines it does not control and, in its web UI, runs a local server.
Its security rules are spread across modules; this page gathers them and links to where each
lives. The `security-reviewer` agent checks changes in these areas
([conventions](../development/conventions.md)).

## Remote output and remote text are hostile

- **Messages**: `session.printable` drops control characters and Unicode format/separator
  characters (bidi overrides, zero-width, tags) so remote text cannot move the cursor or
  disguise itself. `describe_error` passes every error message through it; SSH banners,
  keyboard-interactive names, instructions and prompts are filtered too
  ([Sessions](../core/sessions.md)).
- **Server prompts cannot pretend to be termdeck**: keyboard-interactive prompts are prefixed
  `(user@host) `, like OpenSSH ([SSH](../transports/ssh.md)).
- **Escape sequences**: `vt.RemoteFilter` enforces the "Remote may …" options (mouse,
  alternate screen, title, resize, clear scrollback, charsets, application keys, bracketed
  paste); unfinished sequences are held and dropped rather than leaked
  ([Terminal processing](../core/terminal-processing.md)).
- **Titles**: `clean_title` (and its JS twin) strips control and direction characters and
  shortens titles before they reach a tab label.
- **Paste**: control characters are stripped unless `paste_controls`, since an ESC could end
  bracketed paste early and run commands.
- **Answerback** may not contain control characters (an ENQ would make it type a command).
- **UI rendering**: remote or user text never goes into Textual markup or `innerHTML`
  unescaped — the TUI uses `markup=False`, the web page builds DOM with text nodes and
  `textContent` ([TUI](../interfaces/tui.md), [Web UI](../interfaces/web-ui.md)).
- **`connect` console**: the Ctrl+] escape is recognized in kitty/modifyOtherKeys encodings so a
  remote program cannot disable it, and terminal modes are reset on exit
  ([CLI](../interfaces/cli-and-console.md)).

## Host keys

- Unknown keys are always confirmed with key type and SHA-256 fingerprint; changed keys are
  flagged with the old fingerprints. Keys are saved in termdeck's own `known_hosts`;
  `~/.ssh/known_hosts` is read too. A host vouched for by a CA counts as known.
- A replaced key is appended as `@revoked`, so a copy still trusted elsewhere is refused; a
  revocation of a key trusted again is removed. `termdeck forget-host` clears a host's entries.
- Host names that could change the meaning of a `known_hosts` line (whitespace, `,*?!#|[]`,
  leading `@`) are rejected by `check_host` at validation and before writing.
- **Pinned keys** (`ssh.trusted_keys`, `connect --hostkey`) bypass `known_hosts` entirely:
  anything else is refused without asking, and nothing is saved.
- Host-key questions in the CLI are asked on the terminal, never answered from piped stdin;
  `--batch` refuses unknown keys; the TUI and web dialogs default to Cancel (the web one with
  an arm delay) because they can pop up while the user is typing.

## Secrets

- Passwords are never stored in profiles. The last typed password is kept in memory only for
  an expired-password change and cleared after authentication or disconnect.
- `--password-file` answers only the target host's own first hidden password prompt, once —
  never a jump host, a key passphrase or a server-written question.
- Logs: SSH packet logging uses asyncssh debug level 2, not 3 (raw data could contain typed
  secrets); `hide_secrets` replaces environment variable values in log records. While line
  editing, input after a password-like prompt is not echoed and not kept in history.

## Files on disk

`config.py` ([Profile storage](../core/profile-store.md)):
- termdeck's folder and files are owner-only (`0o700`/`0o600`; on Windows a protected
  owner-only access list).
- Session logs and printer files live in user-chosen folders, possibly shared: they are opened
  with `O_NOFOLLOW`, and existing files that are not regular, not owned by the user, or have
  several links are refused before truncation; on Windows new ones are created owner-only from
  the start. Log file name placeholders are sanitized (no path separators, no reserved device
  names).
- Writes are atomic and locked, so concurrent windows never lose data or leave half files.

## Local web server

([Web UI](../interfaces/web-ui.md))
- Listens on 127.0.0.1 only; the Host header must be local (DNS rebinding) and any Origin must
  match.
- A one-time start token in the URL fragment (10 minutes), exchanged for a per-tab secret sent
  as a header or WebSocket subprotocol — never a cookie, since cookies are shared across all
  127.0.0.1 ports. Comparisons are constant-time.
- `termdeck web` opens the browser through a private forwarding file so the token is not on a
  command line visible to other users.
- Strict CSP, `X-Frame-Options: DENY`, `no-referrer`, `no-store` for API responses.
- Downloads use single-use tickets valid 60 s, so no secret lands in download history.
- The desktop window exposes only two plain functions to page script, not an object whose
  attributes could be walked to `os.system` ([Desktop GUI](../interfaces/desktop-gui.md)).

## Network exposure and transfers

- Port forwards listening on a non-loopback address are flagged in the UIs and warned about
  when opened; an empty listen host means `localhost`, never every interface.
- Weak algorithms explicitly allowed in a profile trigger a warning notice.
- Quick connect refuses bare words as hosts so a mistyped profile name never sends a password
  to whatever host it resolves to.
- SFTP never replaces files without force/`-f`, uploads go to a temporary name first, downloads
  do not copy remote setuid or world-writable modes, recursive removal never follows links,
  and on Windows names that would become alternate data streams or device names are refused
  ([SFTP](../transports/sftp.md)).
- Serial: network serial URLs are limited to known schemes; a break from the TUI needs two key
  presses; DTR/RTS are applied before opening ([Serial](../transports/serial.md)).
