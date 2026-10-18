# Review findings

Merged from the reviewers (feat = features-reviewer, ui = interface-reviewer, arch = architecture-reviewer,
sec = security-reviewer, win = windows-reviewer, conv = conventions-reviewer). Severity: Critical, High,
Medium, Low. Size: S, M, L. Status: open, fixed (commit), won't fix (reason), needs owner (reason),
needs Windows (what to test by hand), proposed (improvement, not implemented).

PuTTY options are issues, not improvements: REQUIREMENTS.md asks for every PuTTY SSH and serial option.

## Pass 1

### Dialogs, focus and safety (web, TUI)

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-01 | issue | Critical | ui | web/static/index.html:92, app.js:376 | Enter in the password prompt submits the first button (Cancel): login is aborted. | Cancel is `type="button"`; form submit means OK. | S | fixed (b782d71) |
| P1-02 | issue | Critical | ui | index.html:80, app.js:816 | Enter in any profile editor field submits as Cancel and discards the edits. | Same pattern: Enter saves, Cancel is a plain button. | S | fixed (b782d71) |
| P1-03 | issue | High | sec, ui | app.js:395, 513-527 | Changed-host-key dialog focuses "Trust new key"; one stray Enter trusts an attacker's key. Message omits host:port. `activate()` steals focus from typing. | Focus Cancel on danger dialogs, delay enabling the danger button, show host:port. | S | fixed (b782d71) |
| P1-04 | issue | High | ui | ssh.py:184, session.py:33 | Host key prompt shows only the SHA256 fingerprint: no key type, no previous key, no file. | Pass algorithm and old fingerprint through the Prompter; show them in every UI. | M | fixed (218591c) |
| P1-05 | issue | High | ui, feat | app.js:581,600,854; tui/app.py:483-510; gui.py | Live sessions close without confirmation (tab close, Ctrl+Shift+W, TUI w/q/Ctrl+Q, window close). PuTTY WarnOnClose. | Confirm when a connected session would close, in every UI; `confirm_close=True` in the GUI. | M | open |
| P1-06 | issue | Medium | ui | app.js:337, tui/app.py:258 | Esc/Cancel discards profile edits without asking. | Compare with a snapshot; ask "Discard changes?". | S | open |
| P1-07 | issue | Medium | ui | app.js:638, tui/app.py:491 | Break is sent on one click and "Break sent" shows even on failure. | Report errors; TUI break needs a second key. | S | open |
| P1-08 | issue | Medium | sec, ui | ssh.py:76, app.js:512, session.py:20 | Server-supplied kbd-int prompts look like local ones (phishing a passphrase), bidi chars not stripped, prompt dialog steals focus from other tabs. | Prefix server prompts with (user@host), show name/instructions, strip bidi/format chars in `printable`. | S | fixed (218591c) |

### Profiles and store (core)

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-10 | issue | High | feat, sec | config.py:21-35, session.py:85 | Opening a log file chmods its existing parent folder to 0700 (home, shared folders; fails on /tmp). | Only tighten termdeck's own folders; create missing log folders 0700, never chmod existing ones. | S | fixed (a33c0b9) |
| P1-11 | issue | High | conv, feat, sec, win | session.py:84, profiles.py:153 | `log_file` uses `str.format`: `{x}` or a stray brace validates, then crashes the connection with a bare `'x'`. Windows reserved names (`con`, `nul`) not handled. | Substitute placeholders explicitly, validate in `Profile.validate`, sanitise reserved names. | S | fixed (a33c0b9) |
| P1-12 | issue | High | conv, ui | profiles.py:328; all UIs | Profiles that fail validation vanish from every UI with no message. | Core helper; CLI prints warnings, TUI notifies, web shows them. | M | fixed (a33c0b9) |
| P1-13 | issue | High | arch, ui | profiles.py:335, tui/app.py:355 | Corrupt profiles.json crashes the TUI and the CLI message has no path. | Raise a clear error with path and line; TUI starts with an error banner. | S | fixed (a33c0b9) |
| P1-14 | issue | High | arch | profiles.py:346 | Duplicate profile names: the earlier entry is silently deleted on the next save. | Keep later duplicates as invalid entries ("duplicate name"). | S | fixed (a33c0b9) |
| P1-15 | issue | High | arch | profiles.py:355, web/server.py:221, tui/app.py:248 | Create-vs-overwrite check is done by each UI and races; concurrent writers lose changes. | `store.create()` / `store.update()` in the core, with a cross-process file lock. | M | fixed (a33c0b9) |
| P1-16 | issue | Medium | arch, sec | ssh.py:31-40, profiles.py:374 | known_hosts rewritten in place (not atomic); `os.replace` fails on Windows when the target is open. | `config.atomic_write` with retry on Windows; use for profiles and known_hosts. | S | needs Windows (fixed in a33c0b9: save profiles from two termdeck processes at once; open profiles.json in another program while saving; check LF endings) |
| P1-17 | issue | Medium | conv, feat, ui | profiles.py:244-262 | Baud 0, font size 0, huge scrollback, names with newlines are accepted. | `min=`/`max=` in `option()`, exported to forms; reject control chars in name/group. | S | fixed (a33c0b9) |
| P1-18 | issue | Medium | sec | profiles.py:182, ssh.py:113 | Jump host is validated on a different string than the one used: known_hosts injection possible; `h:notaport` crashes. | One `parse_jump()` used by both; validate host and port. | S | fixed (a33c0b9) |
| P1-19 | issue | Medium | sec | ssh.py:120 | Agent and X11 forwarding, env and remote command are copied to the jump host. | Clear them in `jump_profile`. | S | fixed (a33c0b9) |
| P1-20 | issue | Low | arch | ssh.py:100-122 | `env` parsed only at connect time. | Parse in `Profile.validate`. | S | fixed (a33c0b9) |
| P1-21 | issue | Low | feat | profiles.py:19, 282 | Options in ssh/serial sections claim `kinds=("ssh","serial")`. | Derive `kinds` from the section in `describe_options`. | S | fixed (a33c0b9) |
| P1-22 | issue | Medium | conv | cli.py:63 | `set` can write the hidden `usb_id`. | Exclude hidden options. | S | fixed (a33c0b9) |
| P1-23 | improvement | Low | arch | profiles.py:373 | profiles.json has no format version. | Write `"version": 1`; refuse newer versions. | S | fixed (a33c0b9) |
| P1-24 | issue | Medium | win | profiles.py:303, serialport.py:90-116 | COM names not normalised: `com3` never matches `COM3`; `\\.\COM10` parses as an SSH host. | Normalise on validation; accept `\\.\COMn` in quick targets. | S | needs Windows (fixed in a33c0b9: com3, \\.\COM10, COM10@115200 resolve and open; redirected 'termdeck list > out.txt' with accented names) |
| P1-25 | issue | Low | sec | profiles.py:127, serialport.py:157 | Any pyserial URL is accepted (`spy://` logs keystrokes to a file). | Allow a short list of URL schemes. | S | fixed (a33c0b9) |

### Sessions, transports, architecture

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-30 | issue | High | arch | session.py:77,99 | Unbounded output queue defeats flow control (124 MB queued in 0.5 s). | Bounded queue; local echo in a separate deque. | S | fixed (218591c) |
| P1-31 | issue | High | arch, ui | ssh.py:261, serialport.py:187, session.py:89, server.py:335 | The reason a session ended is lost; unplug and disconnect show "Session ended". | `Session.end_reason`, shown by every UI via `describe_error`. | M | fixed (218591c) |
| P1-32 | issue | High | arch | ssh.py:86,31; session.py:91; tui/app.py:251 | Blocking calls on the event loop (key decryption 0.7 s, log flush per chunk, known_hosts I/O, list_ports). | `asyncio.to_thread`; buffered log flush. | S | fixed (a33c0b9, 218591c: key decryption, known_hosts, USB scan in threads; buffered log) |
| P1-33 | issue | High | conv, arch, feat | server.py:347-457, cli.py:207, ssh.py:225 | SFTP logic duplicated in CLI and web, two SFTP entry points, cached client races and is never closed; web reaches `session.inner.connection`. | Core `sftp.py` service exposed as `Session.sftp()`, locked creation. | L | fixed (7b105ed) |
| P1-34 | issue | High | ui, arch | tui/app.py:272, app.js:560, server.py:253 | States are only connecting/connected/closed; failed vs ended indistinguishable; capabilities guessed from profile kind; reconnect differs per UI. | Core `SessionController` with states, capabilities, reconnect (re-reads profile). | M | open |
| P1-35 | issue | High | ui | ssh.py:190, server.py:318 | Port-forward results are only text in the terminal; no UI shows active forwards. | Core forward status list; shown in status bars. | M | open |
| P1-36 | issue | Medium | arch | session.py:153-190 | Hidden import cycle; `describe_error` imports asyncssh for serial errors; adding a transport edits session.py. | Transport registry. | M | fixed (218591c) |
| P1-37 | issue | Medium | arch | cli.py:344, server.py:169 | No core error types: bugs reported as user errors (400 / exit 2). | `errors.py` with `TermdeckError` subclasses. | M | fixed (218591c) |
| P1-38 | issue | Medium | arch, feat, sec | session.py:26, ssh.py:68, server.py:123 | Prompter has no notices; SSH banner and kbd-int instructions dropped; web answers not type-checked. | `Prompter.notify`, banner, type checks. | M | fixed (218591c) |
| P1-39 | issue | Medium | arch | serialport.py:186, tui/terminal.py:162 | Serial reads hold default-executor workers; TUI writes unordered via `ensure_future`. | Per-session reader thread and single-worker writer; TUI write queue. | M | fixed (218591c) |
| P1-40 | issue | Medium | conv, arch | profiles.py:20, tui/app.py:149, app.js:250, cli.py:63 | Option schema isn't the single source: `forwards`, wide fields, device picker, section titles hard-coded in UIs; stopbits typed from default. | Widget metadata (`device`, `forwards`, `wide`), section titles from the core. | M | open |
| P1-41 | issue | Medium | conv, arch, ui | app.js:404,607; tui/terminal.py:17 | JS copies of `parse_target` (drifted), `summary`, themes; TUI theme copy. | Server parses quick targets; themes in a core module served by `/api/options`. | M | open |
| P1-42 | issue | Medium | arch | cli.py:20 | `getpass` on the default executor: Ctrl+C at a prompt may hang shutdown. | Daemon thread resolving a future. | S | fixed (218591c) |
| P1-43 | issue | Low | arch | session.py:131 | Non-UTF-8 input decoded per chunk: split multibyte chars corrupted. | Incremental decoder for input. | S | fixed (218591c) |
| P1-44 | issue | Low | arch, conv | tui/terminal.py:162, console.py:198, server.py:309,335 | Cancelled tasks never awaited; fire-and-forget tasks unreferenced. | Keep references, gather after cancel. | S | fixed (218591c) |
| P1-45 | issue | Low | sec | serialport.py:173, server.py:307 | Cancelling a serial open leaves the port open with no owner. | Close the port if the open is cancelled. | S | fixed (218591c) |
| P1-46 | issue | Low | feat, arch | ssh.py:229 | Concurrent first SFTP requests open two clients. | Covered by P1-33 (lock). | S | fixed (7b105ed) |
| P1-47 | issue | Low | conv | server.py:495 | Uses private `site._server`. | `runner.addresses`. | S | fixed (7b105ed) |
| P1-48 | issue | Low | arch | tui/app.py:253 | TUI save catches only ValueError/TypeError; an OSError crashes the TUI. | Catch OSError. | S | fixed (a33c0b9: TUI save catches OSError) |
| P1-49 | issue | Low | arch | pyproject.toml, cli.py:239 | Default command needs the `tui` extra; without it: "No module named 'textual'". | Make the TUI deps core dependencies. | S | fixed (a33c0b9) |

### SSH security and host keys

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-50 | issue | Medium | sec | web/server.py:505 | The start token is in the browser's command line (/proc readable by other local users). | Open a private redirect file instead of the token URL. | M | open |
| P1-51 | issue | Low | sec | ssh.py:19-40 | After accepting a changed key, the old key stays trusted (other entries, OpenSSH file); CA matches show as "unknown". | Record old key as `@revoked`; treat CA matches as changed. | M | fixed (218591c: the @revoked line applies to every known_hosts file asyncssh reads) |
| P1-52 | issue | Low | sec | app.js:743 | A failed download navigates the app away. | Download through an `<a download>`. | S | fixed (b782d71) |
| P1-53 | issue | Low | sec | tui/terminal.py:190, app.js | Pasted text containing `ESC[201~` breaks out of bracketed paste. | Strip ESC from pastes. | S | open |
| P1-54 | improvement | Low | sec | server.py:421 | Uploads have no size or part limit. | Part count limit, 413. | S | proposed |
| P1-55 | improvement | Low | sec | server.py:63 | Per-tab secrets never expire; no logout. | Idle expiry, logout on pagehide. | S | proposed |
| P1-56 | issue | Low | sec | server.py:39 | No `Cache-Control: no-store` on API responses and downloads. | Add it. | S | fixed (7b105ed) |
| P1-57 | improvement | Low | sec | web/static/vendor | Vendored JS has no version/checksum record. | `vendor/VERSIONS` with sha256 and a test. | S | proposed |
| P1-58 | improvement | Low | sec | ssh.py:162 | Weak algorithms in cipher/kex lists accepted silently. | Validate names against asyncssh; warn on legacy. | S | fixed (e8b2e74) |

### SSH features (PuTTY parity)

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-60 | issue | High | feat | ssh.py | Proxy settings missing (SOCKS4/5, HTTP CONNECT, local proxy command, exclude list, proxy DNS). | `ssh.proxy_*` options; asyncssh `tunnel=` connector / `proxy_command`. | M | fixed (e8b2e74) |
| P1-61 | issue | High | feat | cli.py:173, console.py:153 | Plink use impossible: `connect` refuses piped stdin, has no command argument, always exits 0, no batch mode. | Non-interactive mode, command arg, `-t/-T`, `--batch`, `-N`, remote exit status. | M | fixed (48bc35c) |
| P1-62 | issue | Medium | feat | ssh.py:43 | Expired password cannot be changed. | `password_change_requested` via the Prompter. | S | fixed (218591c) |
| P1-63 | issue | Low | feat | ssh.py:154 | `use_agent=False` silently disables agent forwarding. | Reject the combination in validate. | S | fixed (a33c0b9) |
| P1-64 | issue | Low | feat | ssh.py:160 | Encrypted default keys skipped (password asked instead of passphrase). | Document in the key-file help. | S | fixed (218591c) |
| P1-65 | issue | Low | feat | ssh.py:171 | `~/.ssh/config` applied silently. | `ssh.use_ssh_config` option. | S | fixed (e8b2e74) |
| P1-66 | issue | Medium | feat | ssh.py | `NoPTY` and `SshNoShell` not reachable from TUI/web (forwards-only session). | `ssh.allocate_pty`, `ssh.no_shell`. | S | fixed (48bc35c) |
| P1-67 | issue | Low | feat | ssh.py | Connection sharing missing. | `ssh.share_connection`. | L | fixed (e8b2e74: within one termdeck process — tabs of the TUI, web or desktop window; not across separate termdeck processes, which PuTTY also does) |
| P1-68 | issue | Low | feat | ssh.py | Rekey time/bytes missing. | `ssh.rekey_*` → asyncssh. | S | fixed (e8b2e74) |
| P1-69 | issue | Medium | feat | ssh.py | Host-key algorithm list, manually trusted fingerprints (Plink `-hostkey`), CA missing. | `ssh.hostkey_algs`, `ssh.hostkey_fingerprints`. | S | fixed (e8b2e74) |
| P1-70 | issue | Medium | feat | ssh.py | GSSAPI/Kerberos missing. | `ssh.gssapi_*` → asyncssh gss_*. | M | needs Windows (fixed in e8b2e74: Kerberos login on a domain PC with pywin32; on Linux needs the gssapi extra) |
| P1-71 | issue | Low | feat | ssh.py | Auth options missing: no-trivial-auth, keyboard-interactive off, certificate file, skip auth. | `ssh.*` advanced options. | S | fixed (e8b2e74) |
| P1-72 | issue | Low | feat | ssh.py | TTY modes missing. | `ssh.tty_modes`. | S | fixed (e8b2e74) |
| P1-73 | issue | Low | feat | ssh.py | X11 display / auth file missing. | `ssh.x11_display`, `ssh.x11_auth_file`. | S | needs Windows (fixed in e8b2e74: X11 with VcXsrv on localhost:0 and with ':0') |
| P1-74 | issue | Low | feat, ui | app.js:351 | Forward rows with port 0 dropped silently; empty numbers become 0; no per-forward IPv4/IPv6. | Send null for empty fields; no silent filtering. | S | fixed (b782d71) |
| P1-75 | issue | Low | feat | ssh.py | Bug workarounds (16 `Bug*` keys) not available. | asyncssh exposes none of these. | M | needs owner (drops part of 'every PuTTY option'. asyncssh has no hook for 5 of PuTTY's 16 server-bug workarounds (BugHMAC2, BugDeriveKey2, BugRSAPad2, BugPKSessID2, BugDropStart: servers from ~2005); supporting them means patching the SSH library. Covered: BugRekey2 (rekey 0), BugFilterKexinit/BugOldGex2 (algorithm lists), BugRSASHA2CertUserauth (signature_algs, P1-339). Not applicable: BugIgnore2, BugWinadj, BugChanReq (asyncssh never sends those), BugMaxPkt2. SSH-1 only (BugIgnore1, BugPlainPW1, BugRSA1): termdeck has no SSH-1 — recommend listing SSH-1 as out of scope in REQUIREMENTS.md) |
| P1-76 | issue | Low | feat | ssh.py | TCP keepalives, address family, logical host name, username from environment, terminal speed missing. | `ssh.*` → asyncssh `tcp_keepalive`, `family`. | S | fixed (e8b2e74) |
| P1-77 | issue | Medium | feat | cli.py | Plink options `-v`, `-m`, `-s`, `-nc`, `-pwfile`, `-sshlog`, `-4/-6` missing; no event log. | `-v` to stderr, per-session event log. | M | fixed (48bc35c) |

### Serial

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-80 | issue | High | ui, feat | tui/app.py:181, app.js:258 | No port picker in the TUI; web list fetched once; recorded USB identity never shown. | Picker from `available_ports()` with rescan, in both UIs; show the recorded adapter. | M | open |
| P1-81 | issue | Medium | feat | profiles.py:136 | DSR/DTR flow control missing. | Add `dsrdtr` choice. | S | fixed (218591c) |
| P1-82 | issue | Low | feat | serialport.py:157 | No write timeout: a held-off flow-controlled port blocks a thread forever. | `write_timeout`, reported in the terminal. | S | needs Windows (fixed in 218591c: hold a port off with RTS/CTS and type; 'write' notice after 5 s, no frozen UI) |
| P1-83 | issue | Medium | win | serialport.py:116-148 | DTR/RTS on Windows: drivers assert on open, drop on close; comment promises too much. | Document; fix the comment and option help. | S | open |
| P1-84 | issue | Low | win | serialport.py | Windows-generated serial numbers (contain `&`) depend on the USB socket. | Treat them as absent. | S | needs Windows (fixed in 218591c: adapter without serial number, moved between USB sockets, still followed) |
| P1-85 | issue | Low | win | serialport.py | "Access denied" for a port in use is reported raw. | Map to "port in use by another program". | S | needs Windows (fixed in 218591c: open a port held by PuTTY -> 'in use'; COM99 -> 'no such port'; unplug mid-session -> 'device stopped responding') |
| P1-86 | improvement | High | ui | serialport.py | Reopen automatically when an unplugged adapter returns. | Option "Reopen when the device returns". | M | proposed |

### Terminal options (PuTTY parity)

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-90 | issue | High | feat | session.py:87 | Implicit CR in every LF / LF in every CR missing (serial staircase). | `terminal.implicit_cr`, `terminal.implicit_lf` in the core. | S | fixed (218591c) |
| P1-91 | issue | Medium | feat | session.py | Logging: log type (output/all/SSH packets), file clash (append/overwrite), header missing. | `terminal.log_mode`, `terminal.log_clash`. | M | open |
| P1-92 | issue | Low | feat | | Initial modes (auto wrap, origin, BCE, blink text) missing. | Advanced terminal options. | M | open |
| P1-93 | issue | Low | feat | session.py | Answerback to ENQ missing. | `terminal.answerback` in the core. | S | fixed (218591c) |
| P1-94 | issue | Low | feat | | Printer passthrough missing. | `terminal.printer`. | M | open |
| P1-95 | issue | Low | feat | tui/terminal.py:29 | Keyboard options (Home/End, function key style, app cursor/keypad) missing. | `terminal.function_keys`, `terminal.home_end`, ... | M | open |
| P1-96 | issue | Medium | feat | app.js, tui | Bell ignored in every UI. | `terminal.bell` {none, visual, sound}. | S | open |
| P1-97 | issue | Low | feat | | "Features" switches (disable app cursor, mouse, remote resize, alt screen, title, bracketed paste...) missing. | Advanced terminal options. | M | open |
| P1-98 | issue | Low | feat | app.js, tui | Remote window title ignored. | Show OSC title in the tab. | S | open |
| P1-99 | issue | Low | feat | | Scroll on key/output, erase to scrollback fixed. | Advanced terminal options. | S | open |
| P1-100 | issue | Medium | feat | app.js:443 | Font family, cursor style/blink missing. | `terminal.font_family`, `terminal.cursor_style`, `terminal.cursor_blink`. | S | open |
| P1-101 | issue | Low | feat | gui.py | Always on top, full screen. | App setting. | S | open |
| P1-102 | issue | Low | feat | | CJK ambiguous width. | `terminal.cjk_ambiguous_wide`. | S | open |
| P1-103 | issue | Low | feat | app.js:470 | Selection options fixed (rect select, word chars, paste controls). | Advanced terminal options. | S | open |
| P1-104 | issue | Medium | feat, conv | tui/terminal.py:17, app.js:85 | Custom colours, bold style missing; TUI applies only fg/bg of the theme. | Core palettes, `terminal.palette`, TUI maps the palette. | M | open |
| P1-105 | issue | Low | feat, conv | profiles.py:153 | `font_size` shown in the TUI form but ignored there. | Declare per-UI applicability or document. | S | open |
| P1-106 | issue | Low | feat | | Close window on exit (always/clean/never). | `terminal.close_on_exit`. | S | open |

### SFTP

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-110 | issue | High | ui | server.py:421, app.js:764 | Uploads silently overwrite, no progress, no cancel. | Conflict check (409) and Replace/Skip; XHR progress and abort. | M | fixed (7b105ed) |
| P1-111 | issue | Medium | feat | cli.py:320 | CLI sftp has only ls/get/put; no mkdir/rm/rmdir/mv/chmod, no interactive mode. | Commands on the core service; interactive shell. | M | fixed (7b105ed) |
| P1-112 | issue | Medium | feat | tui | TUI has no SFTP. | File panel or transfer dialog. | M | open |
| P1-113 | issue | Medium | ui | cli.py:215 | `sftp get` overwrites local files silently, no progress. | `--force`; progress on stderr. | S | fixed (7b105ed) |
| P1-114 | improvement | Low | ui | index.html:46 | SFTP panel: fixed width, mouse only, no multi-select or breadcrumb. | Splitter, keyboard, multi-select. | M | proposed |

### Web and desktop UI

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-120 | issue | High | win, arch, ui | gui.py:62 | pywebview blocks downloads (`ALLOW_DOWNLOADS` false): SFTP download does nothing in the desktop app. | Enable it. | S | needs Windows (fixed in 7b105ed: termdeck gui: SFTP download opens Save As; a machine without WebView2 prints the install message; closing with a live session asks, without one closes) |
| P1-121 | issue | High | win | gui.py:62 | Without WebView2, pywebview falls back to IE: broken blank window. | Force edgechromium; clear error. | S | needs Windows (fixed in 7b105ed: termdeck gui: SFTP download opens Save As; a machine without WebView2 prints the install message; closing with a live session asks, without one closes) |
| P1-122 | issue | High | ui | app.js:125 | Profile list not keyboard reachable; double click only. | Listbox with roving tabindex, Enter connects, F2 edit, Del delete. | M | open |
| P1-123 | issue | High | ui | app.css:275 | Switch checkboxes `display:none`: Tab skips every boolean option. | Visually hidden but focusable, focus ring. | S | open |
| P1-124 | issue | High | ui | app.css:291, gui.py:63 | Below 720 px the sidebar disappears with no way back. | Collapsible sidebar toggle. | S | open |
| P1-125 | issue | Medium | ui | app.js:502 | Renamed profile: open tabs reconnect with the old name and fail. | Update open tabs after save. | S | open |
| P1-126 | issue | Medium | ui | app.js:850 | Ctrl+Shift+N/W are browser shortcuts in `termdeck web`. | Use Alt+Shift in the browser. | S | open |
| P1-127 | issue | Medium | ui | app.css:111-141 | Contrast failures (warn 2.1:1, ok 2.3:1, input borders 1.4:1). | New tokens. | S | open |
| P1-128 | issue | Medium | ui | app.css, index.html:60 | Looks like a generic AI web page (hero, cards, blur, pulse, pill). | Desktop-tool visual system; recent-sessions list. | M | open |
| P1-129 | issue | Medium | ui | app.js:366 | Editor errors are one line at the bottom; the field isn't marked. | Core `OptionError(section, key)`; mark and focus the field. | M | open |
| P1-130 | issue | Low | ui | app.css:179, app.js:133 | "COM" badge overflows and is used for /dev and URLs. | 32 px column, "SER". | S | open |
| P1-131 | issue | Low | ui | index.html:15,37,53 | Full-width ＋ and emoji render as boxes or in colour. | Inline SVG icons. | S | open |
| P1-132 | issue | Low | ui | app.js:246 | Boolean fields squeezed into half a column. | Full width. | S | fixed (e8b2e74) |
| P1-133 | issue | Low | ui | index.html:79, tui/app.py:39 | macOS button order; Windows expects OK then Cancel on the right. | Right-aligned primary first. | S | fixed (b782d71) |
| P1-134 | issue | Low | conv, ui | app.js:555 | Reconnect-on-Enter listener leaks, can reconnect twice. | Dispose in `openSocket`. | S | fixed (b782d71) |
| P1-135 | issue | Low | ui | app.js:185, tui/app.py:320 | Native `confirm()` for delete; "Copy" vs "Duplicate"; "Folder" vs `--group`. | One dialog, one term. | S | open |
| P1-136 | issue | Low | ui | gui.py:63 | Fixed dark window background; window geometry not saved. | Theme background; save geometry. | S | open |
| P1-137 | issue | Medium | win | gui.py:69 | WebView2 cache in roaming %APPDATA%. | `data_dir()` → %LOCALAPPDATA%. | S | fixed (a33c0b9) |
| P1-138 | issue | Low | conv | app.js:289 | Dead expression. | `f[key] ?? ""`. | S | fixed (b782d71) |
| P1-139 | issue | Medium | win | gui.py, pyproject | "Open another window" uses `window.open` which does nothing in pywebview. | Hide it in the desktop app. | S | needs Windows (fixed in 7b105ed: the desktop window shows no 'Open another window') |

### TUI

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-140 | issue | Critical | ui | tui/app.py:212 | Number fields ignore unparsable input: clearing "22" saves port 2. | Keep raw text, parse on save with a clear error. | S | fixed (b782d71) |
| P1-141 | issue | High | arch | tui/terminal.py:133,225 | pyte at ~0.46 MB/s on the loop, a Style per cell: big output freezes everything. | Feed in slices, cache styles. | M | open |
| P1-142 | issue | High | ui | tui/app.py:357 | `termdeck tui nosuch` crashes with a traceback and exits 0. | Notify and continue. | S | fixed (b782d71) |
| P1-143 | issue | Medium | ui | tui/terminal.py:151 | Output scrolls back to the bottom: scrollback unreadable while a device streams. | Scroll on key only; indicator. | S | open |
| P1-144 | issue | Medium | ui | tui/app.py:300 | Editor clipped at 80x24 (Save cut off). | Responsive width/height. | S | open |
| P1-145 | issue | Medium | ui | tui/terminal.py | No text selection/copy in the TUI terminal. | Selection + OSC 52 copy. | M | open |
| P1-146 | issue | Medium | ui | tui/app.py:284 | Fixed 34-column sidebar. | Toggle. | M | open |
| P1-147 | issue | Low | ui | tui/app.py:316 | Footer shows "Back to terminal" while in the terminal. | "Menus". | S | open |
| P1-148 | issue | Low | ui | tui/app.py:339 | Quick-connect placeholder truncated. | Shorter. | S | open |
| P1-149 | issue | Low | feat | tui | No scrollback search, no tunnel-only session in the TUI. | Search; covered by P1-66 for no-shell. | M | open |

### CLI and console

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-150 | issue | High | ui | cli.py:163, profiles.py:286 | A mistyped profile name becomes an SSH host (password may go to the wrong machine). | No bare-word SSH target; "did you mean". | S | fixed (b782d71) |
| P1-151 | issue | Medium | conv | cli.py:284 | CLI redefines defaults and choices of serial options. | Derive from the option metadata. | M | fixed (a33c0b9) |
| P1-152 | issue | Medium | conv | console.py | console.py is UI code listed as core. | Document it as part of the CLI. | S | fixed (48bc35c) |
| P1-153 | issue | Medium | ui | cli.py:50 | `--set serial.baudrate=abc` error lacks the option name. | Re-raise with the key. | S | fixed (a33c0b9) |
| P1-154 | issue | Medium | ui | cli.py:173, session.py:155 | `connect` silent until connected; errors lack the target. | "connecting to ..." on stderr; target in errors. | S | fixed (48bc35c) |
| P1-155 | issue | Medium | ui | ssh.py:147 | Username asked before connecting; empty answer reported as "Authentication failed". | Clear message. | S | fixed (218591c) |
| P1-156 | issue | Low | ui | cli.py:78 | Empty `list`/`ports` print nothing; no way to list option names. | Messages; `termdeck options`. | S | fixed (a33c0b9) |
| P1-157 | issue | Medium | win | console.py:87-110 | Console mode return values unchecked; handles truncated without argtypes; stdout not checked. | Check, declare argtypes, require a console. | S | needs Windows (fixed in 48bc35c: termdeck connect in Windows Terminal, conhost and PowerShell 5: console mode and code page restored after Ctrl+] q, after Ctrl+Break and after closing the window; no key lost at the shell; resize reaches the remote; é€😀 output without U+FFFD) |
| P1-158 | issue | Medium | win, arch | console.py:107, 137 | Windows stdin reader thread never stops (eats a key after quit); `call_soon_threadsafe` on a closed loop. | Cancellable read; guard the call. | M | needs Windows (fixed in 48bc35c: termdeck connect in Windows Terminal, conhost and PowerShell 5: console mode and code page restored after Ctrl+] q, after Ctrl+Break and after closing the window; no key lost at the shell; resize reaches the remote; é€😀 output without U+FFFD) |
| P1-159 | issue | Medium | win | console.py:165 | UTF-8 split across writes renders as U+FFFD on conhost. | Hold back partial sequences. | S | needs Windows (fixed in 48bc35c: termdeck connect in Windows Terminal, conhost and PowerShell 5: console mode and code page restored after Ctrl+] q, after Ctrl+Break and after closing the window; no key lost at the shell; resize reaches the remote; é€😀 output without U+FFFD) |
| P1-160 | issue | Medium | win | console.py:87 | Console code page/mode not restored on hard exit or Ctrl+Break. | atexit restore, ignore SIGBREAK. | S | needs Windows (fixed in 48bc35c: termdeck connect in Windows Terminal, conhost and PowerShell 5: console mode and code page restored after Ctrl+] q, after Ctrl+Break and after closing the window; no key lost at the shell; resize reaches the remote; é€😀 output without U+FFFD) |
| P1-161 | issue | Medium | win | console.py:112, cli.py:149 | `shutil.get_terminal_size` uses COLUMNS/LINES first. | `os.get_terminal_size`. | S | needs Windows (fixed in 48bc35c: termdeck connect in Windows Terminal, conhost and PowerShell 5: console mode and code page restored after Ctrl+] q, after Ctrl+Break and after closing the window; no key lost at the shell; resize reaches the remote; é€😀 output without U+FFFD) |
| P1-162 | issue | Low | win | cli.py | Redirected output on Windows uses the ANSI code page: UnicodeEncodeError. | Reconfigure stdout/stderr to UTF-8. | S | needs Windows (fixed in a33c0b9: com3, \\.\COM10, COM10@115200 resolve and open; redirected 'termdeck list > out.txt' with accented names) |

### Windows platform

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-170 | issue | Medium | win, sec | config.py:21-36 | chmod is a no-op on Windows: files outside the profile keep inherited ACLs. | Owner-only ACL on Windows. | M | open |
| P1-171 | improvement | Low | win | ssh.py:84 | Paths: quotes from "Copy as path", `%VAR%` not expanded. | Strip quotes, expandvars. | S | needs Windows (fixed in a33c0b9: log with {name}=con/nul/COM1; key and log paths pasted with quotes or %USERPROFILE%) |
| P1-172 | improvement | Low | win | ssh.py:35 | known_hosts written with CRLF on Windows. | `newline="\n"`. | S | fixed (a33c0b9) |
| P1-173 | improvement | Low | win | ssh.py:158 | No message when the Windows agent pipe is missing. | Document; notice. | S | proposed |
| P1-174 | improvement | Low | win | gui.py, pyproject | No app icon/AppUserModelID; no gui-script; DPI awareness. | Icon, `gui-scripts`. | M | proposed |
| P1-175 | improvement | Medium | feat, win | | No Windows installer or frozen build. | PyInstaller/MSIX build. | L | proposed |

### Tests

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-180 | issue | Medium | conv, feat, arch | tests | Untested: ciphers/kex/agent/X11/timeout options, serial line settings, remote and SOCKS forwards, `tunnel`, web upload/op, `ports`, unplug, disconnect reason, reconnect, corrupt store. | Add tests. | M | open |
| P1-181 | issue | Medium | feat, arch | tests | 875 lines of JS untested (the Critical dialog bugs went unseen). | Playwright smoke test of editor, prompts, connect. | M | open |
| P1-182 | issue | Low | arch | conftest.py:105, test_tui.py | `free_port()` race; fixed `pilot.pause(0.5)` sleeps. | Port 0 and `get_port()`; wait helpers. | S | open |

### Improvements (proposed, not implemented)

| ID | Type | Sev | Found by | Location | Idea | Size | Status |
|---|---|---|---|---|---|---|---|
| P1-190 | improvement | Medium | feat | profiles.py | Default settings for new profiles (PuTTY "Default Settings"). | M | proposed |
| P1-191 | improvement | Medium | feat | | Import PuTTY saved sessions (registry, ~/.putty/sessions). | M | proposed |
| P1-192 | improvement | Low | feat | | Change settings during a session; SSH special commands (signals, IGNORE, rekey). | M | proposed |
| P1-193 | improvement | Medium | arch | session.py:42 | `specials()` / `send_special(id)` instead of `can_break`. | M | proposed |
| P1-194 | improvement | Low | arch | | Keep imports lazy for start-up time. | S | proposed |
| P1-195 | improvement | Low | arch | server.py:201 | `/api/options` `defaults` duplicates option defaults. | S | proposed |
| P1-196 | improvement | Medium | ui | app.css:266 | Show advanced fields that differ from the default even with Advanced off; reset per field. | S | proposed |
| P1-197 | improvement | Medium | ui | app.js:613 | Rich status bar segments (8N1, DTR/RTS, line edit, log, forwards), clickable toggles. | M | proposed |
| P1-198 | improvement | Medium | ui | app.js:477 | Confirm multi-line pastes. | S | proposed |
| P1-199 | improvement | Medium | ui | session.py:76 | Runtime line-editing toggle and "input hidden" indicator. | M | proposed |
| P1-200 | improvement | Low | ui | app.css:129 | App appearance setting; terminal theme "auto". | S | proposed |
| P1-201 | improvement | Low | ui | app.js:801 | Save a quick-connect tab as a profile. | S | proposed |
| P1-202 | improvement | Low | feat | | Duplicate profile from the CLI; known-hosts management UI; export/import profiles. | M | proposed |
| P1-203 | improvement | Low | ui | | Undo after deleting a profile. | S | proposed |

### Found while fixing (pass 1)

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-210 | issue | Medium | sec | app.js hostkey prompt | Unknown-host dialog still defaulted to "Trust and connect" with no delay. | `armDelay` for every host-key prompt: Cancel focused, OK armed after 1.5 s. | S | fixed (b782d71) |
| P1-211 | issue | Medium | sec, ui | server.py sftp_ticket, app.js download | With `<a download>`, SFTP errors were hidden in the browser's download UI. | Stat the path when the ticket is made; errors become a toast. | S | fixed (b782d71) |
| P1-212 | issue | Low | ui | app.css | Disabled buttons looked enabled. | `button:disabled` style. | S | fixed (b782d71) |
| P1-213 | issue | Low | ui | app.js | The arming delay also slowed every SFTP delete. | Delay only via `armDelay` (host keys). | S | fixed (b782d71) |
| P1-214 | issue | Low | ui | app.js saveEditor | Enter pressed twice saved twice; half-filled forward rows were dropped silently. | In-flight guard; incomplete rows are reported. | S | fixed (b782d71) |
| P1-215 | issue | Low | ui | tui/app.py | A number error didn't lead to the field (hidden when advanced). | Turn Advanced on and focus the field. | S | fixed (b782d71) |
| P1-216 | issue | Low | conv, ui, sec | tui/app.py, app.js, cli.py | Did-you-mean only in the CLI; quick-connect messages didn't explain host:22; web didn't accept a saved name. | Core `resolve_target`/`unknown_target`; web uses `POST /api/target`. | S | fixed (b782d71) |
| P1-217 | issue | Low | conv, ui | cli.py | `tui NAME` help outdated. | Help text. | S | fixed (b782d71) |
| P1-218 | issue | Low | sec | app.js forwardsEditor | New forward rows showed "0" ports. | Empty for 0. | S | fixed (b782d71) |
| P1-220 | issue | Low | win | gui.py | Old WebView2 cache in %APPDATA% orphaned after moving to %LOCALAPPDATA%. | — | S | won't fix (new app: no existing installs to migrate) |
| P1-221 | issue | Low | win | config.py file_lock | Windows lock could wait 30 s and the comment said 10 s. | One LK_LOCK call (~10 s); store calls run in threads. | S | needs Windows (fixed in a33c0b9: save profiles from two termdeck processes at once; open profiles.json in another program while saving; check LF endings) |
| P1-222 | issue | Low | win | session.py | Superscript COM¹/LPT¹ device names not reserved. | Added. | S | needs Windows (fixed in a33c0b9: log with {name}=con/nul/COM1; key and log paths pasted with quotes or %USERPROFILE%) |
| P1-223 | issue | Major | ui, feat | server.py list_profiles, app.js | Corrupt profiles.json broke the web UI start; missing #profile-problems element. | Return `error` with 200; persistent sidebar notice. | S | fixed (a33c0b9) |
| P1-224 | issue | Major | ui | cli.py resolve_profile | Corrupt profiles.json blocked CLI quick connect. | Warn and continue with no profiles. | S | fixed (a33c0b9) |
| P1-225 | issue | Minor | ui | profiles.py, cli.py | A skipped profile could not be repaired or removed from the CLI. | `get_raw` for `set`; `rm` removes skipped entries. | S | fixed (a33c0b9) |
| P1-226 | issue | Minor | ui | profiles.py update | Rename onto the name of a skipped profile allowed. | Check skipped names. | S | fixed (a33c0b9) |
| P1-227 | issue | Minor | ui | tui/app.py, app.js | Skipped-profile warnings repeated on every reload; web showed them as an error toast after saves. | Notify once (TUI); sidebar details (web). | S | fixed (a33c0b9) |
| P1-228 | issue | Minor | ui | cli.py, profiles.py | `--set` range/choice errors lacked the key; choices not listed; "a number" for 22.5. | Check per option; list choices; "whole number". | S | fixed (a33c0b9) |
| P1-229 | issue | Minor | ui | cli.py options | `options` output showed Python reprs, no ranges, unaligned. | true/false, ranges, (empty), aligned. | S | fixed (a33c0b9) |
| P1-230 | issue | Cosmetic | ui | profiles.py, cli.py | Messages didn't use form labels; duplicate warning lacked the entry; path repeated. | Reworded. | S | fixed (a33c0b9) |
| P1-231 | issue | Medium | sec, feat | ssh.py, config.py | Concurrent host-key saves lost keys (shared temp file, no lock). | Lock in remember_host_key; temp name per thread. | S | fixed (a33c0b9) |
| P1-232 | issue | Medium | sec | profiles.py | Store kept skipped entries on the shared object: a concurrent load during a save dropped them from the file. | Per-call state (`_read`). | S | fixed (a33c0b9) |
| P1-233 | issue | Low | sec, feat | server.py, tui/app.py | Delete took the file lock on the event loop. | `asyncio.to_thread`. | S | fixed (a33c0b9) |
| P1-234 | issue | Medium | sec | config.py open_private | Logs in shared folders followed symlinks / took over others' files; fchmod on existing logs. | O_NOFOLLOW, owner/regular/nlink check, chmod only new files, close on error. | S | fixed (a33c0b9) |
| P1-235 | issue | Medium | sec, feat | session.py | Buffered log not flushed while idle or after the session ended. | 1 s flush timer; flush when the pump ends. | S | fixed (a33c0b9) |
| P1-236 | issue | Medium | feat | profiles.py normalize_device | `\\.\` stripped from non-COM devices (com0com CNCA0) which then fail to open. | Strip only for COMn. | S | fixed (a33c0b9) |
| P1-237 | issue | Medium | feat | profiles.py delete | Deleting a duplicated name resurrected the other copy. | Delete removes every entry with the name. | S | fixed (a33c0b9) |
| P1-238 | issue | Low | feat | profiles.py | Bare IPv6 jump host refused; cp2110:// refused. | Accept both. | S | fixed (a33c0b9) |
| P1-239 | issue | Low | conv | server.py save/delete | OSError and ProfileFileError gave bare 500s. | JSON errors. | S | fixed (a33c0b9) |
| P1-240 | issue | Low | conv | config.py | open_private("w", own_dir=False) truncated before checks; atomic_write left temp files on failure; log flush errors silent. | Append-only outside own dirs; unlink temp; stop logging with a notice. | S | fixed (a33c0b9) |
| P1-241 | issue | Medium | test-writer | server.py quick_target | Web quick connect failed (500) with a corrupt profiles.json. | Fall back to no profiles. | S | fixed (a33c0b9) |
| P1-242 | issue | Low | win | serialport.py _parse_identity | A stored identity with a Windows instance-id serial no longer matched. | Treat "&" serials as none when parsing too. | S | fixed (218591c) |
| P1-243 | issue | Low | win | serialport.py describe_error | Missing/stale port errors shown raw. | "no such port", "device stopped responding". | S | fixed (218591c) |
| P1-244 | issue | Low | win | cli.py ConsolePrompter | A cancelled ask leaves a reading thread (Ctrl+C at a prompt). | Covered by P1-158 (cancellable console read). | S | won't fix (only Ctrl+C cancels a prompt, and the program then exits; the daemon thread cannot outlive it) |
| P1-245 | issue | Major | ui, arch | serialport.py _read_loop | Windows unplug ("ClearCommError ... Access is denied") reported as "port in use". | Read failures say "device stopped responding"; "in use" only for open errors. | S | fixed (218591c) |
| P1-246 | issue | Minor | ui | session.py | Implicit CR + LF both on doubled line spacing; CR at chunk end. | Single regex pass, CR held across chunks. | S | fixed (218591c) |
| P1-247 | issue | Minor | ui | tui/terminal.py, app.js | Notices "dimmed" with SGR 2, which pyte ignores. | Grey (SGR 90) in both. | S | fixed (218591c) |
| P1-248 | issue | Minor | ui | tui/app.py, app.js | Login instructions and "passwords do not match" hidden behind the next dialog. | The last notice is shown in the next prompt. | S | fixed (218591c) |
| P1-249 | issue | Minor | ui | tui/app.py | Unknown-host dialog red like a changed key. | `cancel_default` separate from `danger`. | S | fixed (218591c) |
| P1-250 | issue | Minor | ui | serialport.py | Write-timeout notice wrong for flow none, repeated per key, logged as device output. | Wording by flow, once per stall, typing dropped while stalled, `Session.notice` channel not logged. | S | fixed (218591c) |
| P1-251 | issue | Cosmetic | ui | cli.py | CLI wording differed from TUI/web (Was:, Session ended); old keys without type. | Same words; "type fingerprint". | S | fixed (218591c) |
| P1-252 | issue | Critical | arch, test-writer | session.py close | close() hung while the output backlog was full (the UI stopped reading). | Cancel the pump first. | S | fixed (218591c) |
| P1-253 | issue | Major | arch | cli.py, server.py, session.py | Error types not used by the UIs: bugs reported as user errors. | `is_expected`, transports export ERRORS; CLI exit 70 + traceback for bugs; web no longer maps AttributeError to 400; pump logs bugs. | M | fixed (218591c) |
| P1-254 | issue | Major | arch, sec | ssh.py remember_host_key | @revoked lines were never removed: a host whose old key came back was locked out with a misleading message; keys of other types revoked too. | Re-trusting removes the revocation; only same-type keys revoked; message names `termdeck forget-host`; new command. | S | fixed (218591c) |
| P1-255 | issue | Minor | arch | cli.py | Ctrl+C at a getpass prompt left echo off (daemon thread). | Save and restore the tty in main. | S | fixed (218591c) |
| P1-256 | issue | Minor | arch | session.py, profiles.py | Transport table separate from KINDS; unknown type a plain ValueError. | TRANSPORTS next to KINDS; ProfileError. | S | fixed (218591c) |
| P1-257 | issue | Minor | arch | tui/terminal.py | A failed write silently stopped all further typing. | Report and keep the writer running. | S | fixed (218591c) |
| P1-258 | issue | Low | arch | profiles.py get | Invalid profile raised "not found". | ProfileError. | S | fixed (218591c) |
| P1-259 | issue | Low | arch | server.py notify | notify on a closed socket left an unretrieved task exception. | Ignore. | S | fixed (218591c) |
| P1-260 | issue | Medium | sec | serialport.py | DSR/DTR does nothing on POSIX and skipped the DTR setting. | Windows only; elsewhere DTR applies and a notice says so. | S | fixed (218591c) |
| P1-261 | issue | Low | sec | ssh.py | Typed password kept for the whole connection. | Cleared on auth_completed / connection_lost. | S | fixed (218591c) |
| P1-262 | issue | Low | sec | ssh.py | Remote signal name not cleaned. | printable. | S | fixed (218591c) |
| P1-263 | issue | Low | sec | tui/app.py | TUI host-key dialog could be accepted by Tab, Enter typed in another tab. | OK armed after 1.5 s. | S | fixed (218591c) |
| P1-264 | issue | Low | sec | profiles.py answerback | Answerback with CR could run a command on any ENQ. | Reject control characters. | S | fixed (218591c) |
| P1-265 | issue | Low | sec | session.py printable | Missed some invisible characters (U+2060.., soft hyphen, tags). | Strip Unicode categories Cc, Cf, Zl, Zp (keep \n, \t). | S | fixed (218591c) |
| P1-266 | issue | Low | test-writer | ssh.py | Server disconnect reason lost; duplicate old fingerprints. | connection_lost recorded; "Connection closed by the server"; dedupe. | S | fixed (218591c) |
| P1-267 | issue | Medium | conv | serialport.py describe_error | POSIX open errors kept the "[Errno n]" prefix; permission denied unmapped; "Errno 2" matched 20/22. | Regex for the port; Errno n] markers. | S | fixed (218591c) |
| P1-268 | issue | Medium | conv | README.md | Exit codes, "Session ended", forget-host undocumented. | Documented. | S | fixed (218591c) |
| P1-269 | issue | Low | conv | session.py | A lone CR under implicit LF waited for more output. | Released after 50 ms. | S | fixed (218591c) |
| P1-270 | issue | Low | conv | serialport.py | Serial output had no backlog bound. | Reader waits on a semaphore (256 chunks). | S | fixed (218591c) |
| P1-271 | issue | Low | win | cli.py | --password-file / -m: UTF-16 (PowerShell 5) crashed, BOM kept, empty file IndexError. | utf-8-sig, clear errors. | S | fixed (48bc35c) |
| P1-272 | issue | Low | win | console.py pipe | Scripted output to a Windows console: no VT processing, split UTF-8 shown as U+FFFD. | _ConsoleOutput. | S | fixed (48bc35c) |
| P1-273 | issue | Low | win | console.py | watch_size died on OSError. | Keep the last size. | S | fixed (48bc35c) |
| P1-274 | issue | High | sec | cli.py ConsolePrompter | In scripted mode host-key and login questions were answered from piped stdin ("y" trusted a changed key) and ate the data. | Ask only on /dev/tty or CONIN$; no terminal = fail. | S | fixed (48bc35c) |
| P1-275 | issue | Medium | sec | cli.py PasswordFilePrompter | --password-file answered the jump host and any prompt containing "password". | Target's password prompt only (and its kbd-int one). | S | fixed (48bc35c) |
| P1-276 | issue | Medium | sec | session.py, cli.py | Pipe mode applied terminal settings (answerback injection, echo of piped secrets, notices on stdout). | Raw TerminalSession, notices to stderr. | S | fixed (48bc35c) |
| P1-277 | issue | Low | sec | cli.py BatchPrompter | --batch did not flag a changed key. | WARNING with old fingerprint. | S | fixed (48bc35c) |
| P1-278 | issue | Low | sec | cli.py logging | --ssh-log/-vv logged environment values. | Filter hides them; help says commands are logged. | S | fixed (48bc35c) |
| P1-279 | issue | Low | sec | ssh.py SSHForwards | -N with stdin from /dev/null closed after 0.5 s. | write_eof ignored. | S | fixed (48bc35c) |
| P1-280 | issue | High | feat | cli.py | Options after the target became the remote command (regression of --set). | Split like plink at the first plain word. | S | fixed (48bc35c) |
| P1-281 | issue | High | feat | console.py, ssh.py | Piped input unbounded (400 MB in memory for a slow remote). | Bounded stdin reader; drain after writes. | S | fixed (48bc35c) |
| P1-282 | issue | Medium | feat | ssh.py | Without a PTY, stderr was lost outside pipe mode (TUI, web, interactive). | Separate only for scripts; merged otherwise. | S | fixed (48bc35c) |
| P1-283 | issue | Medium | feat | cli.py | A command, -m or -s got a PTY when stdin was a terminal. | No PTY unless -t, like plink. | S | fixed (48bc35c) |
| P1-284 | issue | Medium | feat | cli.py | --password-file ignored keyboard-interactive password prompts. | Answer the target's hidden kbd-int prompt once. | S | fixed (48bc35c) |
| P1-285 | issue | Low | feat | ssh.py | Remote signal gave exit 255. | 128 + signal number. | S | fixed (48bc35c) |
| P1-286 | issue | Low | feat | cli.py | Scripts got "[termdeck] Session ended" on stderr for a plain non-zero exit. | Quiet unless lost or killed. | S | fixed (48bc35c) |
| P1-287 | issue | Low | conv | profiles.py, cli.py | no_shell/subsystem/command and --nc with -N/-L/command silently overrode each other. | Rejected. | S | fixed (48bc35c) |
| P1-288 | issue | Low | conv | cli.py | In a terminal, a command without a PTY ran in raw interactive mode (no echo, key mapping). | Runs like `ssh host cmd`: terminal untouched, data passed unchanged. | S | fixed (48bc35c) |
| P1-289 | issue | Low | test-writer | cli.py | The environment-value log filter never matched (channel prefix) and sat on the parent logger. | Match "Env: %s=%s" anywhere; filter on the handlers. | S | fixed (48bc35c) |
| P1-290 | issue | Low | test-writer | cli.py PasswordFilePrompter | A jump host with the same user@host as the target (another port) would get the file's password. | Include the port in the prompt label. | S | fixed (7b105ed) |
| P1-291 | issue | High | win | cli.py sftp shell | shlex ate backslashes in Windows paths. | No escapes on Windows. | S | needs Windows (fixed in 7b105ed: in the sftp shell, lcd C:\Users\me, put "C:\My Docs\a.txt"; get -r of a folder with a:b or con refused; piped UTF-8 script with accents) |
| P1-292 | issue | Medium | win | cli.py sftp | ~ not expanded in local paths after lcd. | user_path per argument. | S | fixed (7b105ed) |
| P1-293 | issue | Low | win, conv | sftp.py get | Remote names Windows cannot store (a:b → alternate stream, con, x.) in downloads. | Refused with the list (single files too). | S | needs Windows (fixed in 7b105ed: in the sftp shell, lcd C:\Users\me, put "C:\My Docs\a.txt"; get -r of a folder with a:b or con refused; piped UTF-8 script with accents) |
| P1-294 | issue | Low | win, feat | cli.py | Piped sftp scripts decoded with the ANSI code page. | stdin UTF-8 (BOM allowed). | S | needs Windows (fixed in 7b105ed: in the sftp shell, lcd C:\Users\me, put "C:\My Docs\a.txt"; get -r of a folder with a:b or con refused; piped UTF-8 script with accents) |
| P1-295 | issue | Critical | ui, sec, feat, test-writer | sftp.py | `-> list[str]` inside class SFTP (method `list`) crashed the import: every SSH path broken. | Quoted annotations. | S | fixed (7b105ed) |
| P1-296 | issue | Major | ui | app.js | Refreshing the SFTP listing wiped upload progress and Cancel. | #sftp-info and #sftp-transfer. | S | fixed (7b105ed) |
| P1-297 | issue | Major | ui, sec, feat, conv | cli.py | --force after the command ignored; -rf dropped; unknown flags dropped; no `--`. | Leading option parser per command. | S | fixed (7b105ed) |
| P1-298 | issue | Minor | ui | app.js | Drops during an upload vanished; progress numbers disagreed; no "finishing" state. | Toast; e.total; "Finishing upload". | S | fixed (7b105ed) |
| P1-299 | issue | Minor | ui | app.js, cli.py, sftp.py | Recursive delete: no feedback (web), no question (shell); link to folder failed. | "Deleting…"; shell asks; links unlinked. | S | fixed (7b105ed) |
| P1-300 | issue | Minor | ui | cli.py | Progress line wrapped; raw byte counts; ls without dates; script errors without line. | Fitted line, sizes and rate, mtime, "line N:". | S | fixed (7b105ed) |
| P1-301 | issue | Cosmetic | ui | gui.py, app.js | Close prompt wording; empty sidebar strip in the desktop window. | Reworded; footer hidden. | S | fixed (7b105ed) |
| P1-302 | issue | Medium | sec, conv | cli.py | Remote names and paths printed without cleaning (ls, pwd, prompt, progress, cd error). | printable. | S | fixed (7b105ed) |
| P1-303 | issue | Low | sec, conv | sftp.py write | exists-then-"wb" race could replace a file. | Temporary name, then rename. | S | fixed (7b105ed) |
| P1-304 | issue | Low | sec, conv | server.py, cli.py | Bad chmod modes gave 500 / ended the shell; non-object JSON bodies gave 500. | Range checked in the core; 400. | S | fixed (7b105ed) |
| P1-305 | issue | High | feat | cli.py | Extra arguments silently dropped (rm a b c removed a). | Every argument handled; usage errors. | S | fixed (7b105ed) |
| P1-306 | issue | Medium | feat | sftp.py rename | mv FILE DIR failed. | Moves into the folder; several sources. | S | fixed (7b105ed) |
| P1-307 | issue | Medium | feat | sftp.py, cli.py | No wildcards, mget/mput. | glob, mget/mput. | M | fixed (7b105ed) |
| P1-308 | issue | Medium | feat | sftp.py write | A cancelled upload left a half file / destroyed the original on Replace. | Temporary name, rename when complete. | S | fixed (7b105ed) |
| P1-309 | issue | Medium | feat | gui.py | Close confirmation would deadlock on GTK/Cocoa (dialog waits on the GUI thread). | Ask from a helper thread, then destroy. | S | fixed (7b105ed) |
| P1-310 | issue | Medium | feat | cli.py | psftp aliases (dir, del, ren...) and # comments missing: psftp -b scripts failed. | Aliases; comments skipped. | S | fixed (7b105ed) |
| P1-311 | issue | Low | feat | sftp.py | No reget/reput. | Added. | S | fixed (7b105ed) |
| P1-312 | issue | Low | feat | sftp.py | Only octal chmod modes. | Symbolic modes. | S | fixed (7b105ed) |
| P1-313 | issue | Low | feat | cli.py | No -b FILE / -bc / -be. | -b, --echo, --keep-going. | S | fixed (7b105ed) |
| P1-314 | issue | Low | feat | sftp.py | A dead SFTP channel stayed cached. | Dropped on asyncssh errors. | S | fixed (7b105ed) |
| P1-315 | issue | Low | feat | README.md | sftp docs out of date. | Updated. | S | fixed (7b105ed) |
| P1-316 | improvement | Low | feat | app.js | chmod not reachable from the web panel. | Permissions… menu item. | S | fixed (7b105ed) |
| P1-317 | issue | High | win | proxy.py | Quoted program paths in a proxy command kept their quotes on Windows. | Strip quotes after a non-POSIX split. | S | needs Windows (fixed in e8b2e74: proxy command "C:\Program Files\...\connect.exe" %host %port connects; no console window from the desktop window) |
| P1-318 | issue | Medium | win | ssh.py | X11 display ":0" uses a unix socket the Windows loop lacks. | ":N" becomes "localhost:N" on Windows; clear error without a display. | S | needs Windows (fixed in e8b2e74: X11 with VcXsrv on localhost:0 and with ':0') |
| P1-319 | issue | Medium | win, feat | pyproject.toml | GSSAPI options did nothing: SSPI needs pywin32 (Windows), gssapi on POSIX. | pywin32 on Windows; `gssapi` extra elsewhere. | S | needs Windows (fixed in e8b2e74: Kerberos login on a domain PC with pywin32; on Linux needs the gssapi extra) |
| P1-320 | issue | Low | win | profiles.py | ~/.ssh/config paths with backslashes break on Windows. | Help text. | S | fixed (e8b2e74: documented in the option help) |
| P1-321 | issue | Low | conv, feat | profiles.py | Trusted keys: padded SHA256 never matched; MD5 upper case / without prefix, pasted "type bits SHA256" lines and key blobs refused. | Normalised; all accepted. | S | fixed (e8b2e74) |
| P1-322 | issue | Low | conv | proxy.py | Over-long SOCKS 5 user/password/host raised a raw ValueError. | ConnectError. | S | fixed (e8b2e74) |
| P1-323 | issue | Major | ui, sec, feat | ssh.py | A shared connection ran the first profile's command, settings and title; agent forwarding of one profile leaked to another; share key too narrow; empty username shared another login. | Each tab's own profile; key from every connection-wide setting; no sharing without a username. | S | fixed (e8b2e74) |
| P1-324 | issue | Major | ui | profiles.py, tui, web | 43 advanced SSH fields in one flat list. | Groups with subheadings. | M | fixed (e8b2e74) |
| P1-325 | issue | Minor | ui | profiles.py, tui, web | Proxy, X11, GSSAPI, PTY sub-options shown when unused. | `when` conditions. | S | fixed (e8b2e74) |
| P1-326 | issue | Major | ui | proxy.py | A proxy command's stderr went into the SSH stream; its errors were lost; console window on Windows. | CommandTunnel: stderr apart, shown on failure, no window. | S | fixed (e8b2e74) |
| P1-327 | issue | Minor | ui | profiles.py | Algorithm and terminal-mode typos found only at connect. | Checked when saving; plurals. | S | fixed (e8b2e74) |
| P1-328 | issue | Minor | ui | proxy.py | Proxy messages used internal names; no login hint for SOCKS 5; prompt style. | Labels, hint, "user@proxy proxy password:". | S | fixed (e8b2e74) |
| P1-329 | issue | Minor | ui | tui/app.py, app.css | TUI help cut off; web switches squeezed with "adv" badges repeated. | Wrapping help; one-line switches, full width; no badge in advanced sections. | S | fixed (e8b2e74) |
| P1-330 | issue | Medium | sec | ssh.py | The jump host inherited logical_host (and trusted keys). | Cleared. | S | fixed (e8b2e74) |
| P1-331 | issue | Low | sec | profiles.py | Terminal mode values: non-ASCII digits, > 32 bits. | Strict check. | S | fixed (e8b2e74) |
| P1-332 | issue | Low | sec | ssh.py | Certificate host key types refused; sha1/rsa1024 variants not flagged weak. | Added. | S | fixed (e8b2e74) |
| P1-333 | issue | Medium | feat | ssh.py | Trusted host keys did not pin the key (other keys still accepted from known_hosts). | Pinning: only listed keys, no known_hosts, no prompt. | S | fixed (e8b2e74) |
| P1-334 | issue | Medium | feat | ssh.py | With ~/.ssh/config on, its User and most settings were overridden. | Defaults not passed; username left to the config. | S | fixed (e8b2e74) |
| P1-335 | issue | Medium | feat | ssh.py | PuTTY's automatic ERASE and IUTF8 terminal modes not sent. | From Backspace and character set settings. | S | fixed (e8b2e74) |
| P1-336 | issue | Low | feat | profiles.py, proxy.py | SOCKS 4 defaulted to remote DNS (4a); proxy password asked twice; exclusions not split on spaces. | proxy_dns auto/yes/no; cached; spaces. | S | fixed (e8b2e74) |
| P1-337 | issue | Low | feat | proxy.py | Telnet proxy and proxy diagnostics missing. | Telnet proxy command; "connecting through" notice. | S | fixed (e8b2e74) |
| P1-338 | issue | Low | feat | cli.py | plink -proxycmd, -loghost, -no-trivial-auth, -share missing. | Flags on connect. | S | fixed (e8b2e74) |
| P1-339 | issue | Low | feat | profiles.py | BugRSASHA2CertUserauth (OpenSSH 7.2-7.7) not possible. | signature_algs option. | S | fixed (e8b2e74) |
| P1-340 | issue | Low | feat | proxy | PuTTY proxy types "SSH to proxy and execute a command" and "SSH subsystem" missing ("SSH to proxy and use port forwarding" is the jump host). | — | M | fixed (e8b2e74) |
| P1-341 | issue | Low | test-writer | proxy.py | SOCKS 4 failed for names resolving to IPv6 first; HTTP ignored proxy_dns=no. | Prefer IPv4; HTTP sends the resolved address. | S | fixed (e8b2e74) |
