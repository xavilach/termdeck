# Review findings

Merged from the reviewers (feat = features-reviewer, ui = interface-reviewer, arch = architecture-reviewer,
sec = security-reviewer, win = windows-reviewer, conv = conventions-reviewer). Severity: Critical, High,
Medium, Low. Size: S, M, L. Status: open, won't fix (reason), needs owner (reason),
needs Windows (what to test by hand), proposed (improvement, not implemented).

Fixed findings are removed from this list once their fix is committed; the fix commits and the
earlier versions of this file are in the git history.

PuTTY options are issues, not improvements: REQUIREMENTS.md asks for every PuTTY SSH and serial option.

## Pass 1

### Profiles and store (core)

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-16 | issue | Medium | arch, sec | ssh.py:31-40, profiles.py:374 | known_hosts rewritten in place (not atomic); `os.replace` fails on Windows when the target is open. | `config.atomic_write` with retry on Windows; use for profiles and known_hosts. | S | needs Windows (fixed in a33c0b9: save profiles from two termdeck processes at once; open profiles.json in another program while saving; check LF endings) |
| P1-24 | issue | Medium | win | profiles.py:303, serialport.py:90-116 | COM names not normalised: `com3` never matches `COM3`; `\\.\COM10` parses as an SSH host. | Normalise on validation; accept `\\.\COMn` in quick targets. | S | needs Windows (fixed in a33c0b9: com3, \\.\COM10, COM10@115200 resolve and open; redirected 'termdeck list > out.txt' with accented names) |

### SSH security and host keys

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-54 | improvement | Low | sec | server.py:421 | Uploads have no size or part limit. | Part count limit, 413. | S | proposed |
| P1-55 | improvement | Low | sec | server.py:63 | Per-tab secrets never expire; no logout. | Idle expiry, logout on pagehide. | S | proposed |
| P1-57 | improvement | Low | sec | web/static/vendor | Vendored JS has no version/checksum record. | `vendor/VERSIONS` with sha256 and a test. | S | proposed |

### SSH features (PuTTY parity)

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-70 | issue | Medium | feat | ssh.py | GSSAPI/Kerberos missing. | `ssh.gssapi_*` → asyncssh gss_*. | M | needs Windows (fixed in e8b2e74: Kerberos login on a domain PC with pywin32; on Linux needs the gssapi extra) |
| P1-73 | issue | Low | feat | ssh.py | X11 display / auth file missing. | `ssh.x11_display`, `ssh.x11_auth_file`. | S | needs Windows (fixed in e8b2e74: X11 with VcXsrv on localhost:0 and with ':0') |
| P1-75 | issue | Low | feat | ssh.py | Bug workarounds (16 `Bug*` keys) not available. | asyncssh exposes none of these. | M | needs owner (drops part of 'every PuTTY option'. asyncssh has no hook for 5 of PuTTY's 16 server-bug workarounds (BugHMAC2, BugDeriveKey2, BugRSAPad2, BugPKSessID2, BugDropStart: servers from ~2005); supporting them means patching the SSH library. Covered: BugRekey2 (rekey 0), BugFilterKexinit/BugOldGex2 (algorithm lists), BugRSASHA2CertUserauth (signature_algs, P1-339). Not applicable: BugIgnore2, BugWinadj, BugChanReq (asyncssh never sends those), BugMaxPkt2. SSH-1 only (BugIgnore1, BugPlainPW1, BugRSA1): termdeck has no SSH-1 — recommend listing SSH-1 as out of scope in REQUIREMENTS.md) |

### Serial

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-82 | issue | Low | feat | serialport.py:157 | No write timeout: a held-off flow-controlled port blocks a thread forever. | `write_timeout`, reported in the terminal. | S | needs Windows (fixed in 218591c: hold a port off with RTS/CTS and type; 'write' notice after 5 s, no frozen UI) |
| P1-83 | issue | Medium | win | serialport.py:116-148 | DTR/RTS on Windows: drivers assert on open, drop on close; comment promises too much. | Document; fix the comment and option help. | S | needs Windows (comment and option help fixed in an earlier batch: check DTR/RTS on open and close with FTDI, CH340, Prolific) |
| P1-84 | issue | Low | win | serialport.py | Windows-generated serial numbers (contain `&`) depend on the USB socket. | Treat them as absent. | S | needs Windows (fixed in 218591c: adapter without serial number, moved between USB sockets, still followed) |
| P1-85 | issue | Low | win | serialport.py | "Access denied" for a port in use is reported raw. | Map to "port in use by another program". | S | needs Windows (fixed in 218591c: open a port held by PuTTY -> 'in use'; COM99 -> 'no such port'; unplug mid-session -> 'device stopped responding') |
| P1-86 | improvement | High | ui | serialport.py | Reopen automatically when an unplugged adapter returns. | Option "Reopen when the device returns". | M | proposed |

### Terminal options (PuTTY parity)

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-102 | issue | Low | feat | | CJK ambiguous width. | `terminal.cjk_ambiguous_wide`. | S | needs owner (xterm.js and pyte have no ambiguous-width setting; needs a different terminal engine) |

### SFTP

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-114 | improvement | Low | ui | index.html:46 | SFTP panel: fixed width, mouse only, no multi-select or breadcrumb. | Splitter, keyboard, multi-select. | M | proposed |

### Web and desktop UI

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-120 | issue | High | win, arch, ui | gui.py:62 | pywebview blocks downloads (`ALLOW_DOWNLOADS` false): SFTP download does nothing in the desktop app. | Enable it. | S | needs Windows (fixed in 7b105ed: termdeck gui: SFTP download opens Save As; a machine without WebView2 prints the install message; closing with a live session asks, without one closes) |
| P1-121 | issue | High | win | gui.py:62 | Without WebView2, pywebview falls back to IE: broken blank window. | Force edgechromium; clear error. | S | needs Windows (fixed in 7b105ed: termdeck gui: SFTP download opens Save As; a machine without WebView2 prints the install message; closing with a live session asks, without one closes) |
| P1-139 | issue | Medium | win | gui.py, pyproject | "Open another window" uses `window.open` which does nothing in pywebview. | Hide it in the desktop app. | S | needs Windows (fixed in 7b105ed: the desktop window shows no 'Open another window') |

### CLI and console

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-157 | issue | Medium | win | console.py:87-110 | Console mode return values unchecked; handles truncated without argtypes; stdout not checked. | Check, declare argtypes, require a console. | S | needs Windows (fixed in 48bc35c: termdeck connect in Windows Terminal, conhost and PowerShell 5: console mode and code page restored after Ctrl+] q, after Ctrl+Break and after closing the window; no key lost at the shell; resize reaches the remote; é€😀 output without U+FFFD) |
| P1-158 | issue | Medium | win, arch | console.py:107, 137 | Windows stdin reader thread never stops (eats a key after quit); `call_soon_threadsafe` on a closed loop. | Cancellable read; guard the call. | M | needs Windows (fixed in 48bc35c: termdeck connect in Windows Terminal, conhost and PowerShell 5: console mode and code page restored after Ctrl+] q, after Ctrl+Break and after closing the window; no key lost at the shell; resize reaches the remote; é€😀 output without U+FFFD) |
| P1-159 | issue | Medium | win | console.py:165 | UTF-8 split across writes renders as U+FFFD on conhost. | Hold back partial sequences. | S | needs Windows (fixed in 48bc35c: termdeck connect in Windows Terminal, conhost and PowerShell 5: console mode and code page restored after Ctrl+] q, after Ctrl+Break and after closing the window; no key lost at the shell; resize reaches the remote; é€😀 output without U+FFFD) |
| P1-160 | issue | Medium | win | console.py:87 | Console code page/mode not restored on hard exit or Ctrl+Break. | atexit restore, ignore SIGBREAK. | S | needs Windows (fixed in 48bc35c: termdeck connect in Windows Terminal, conhost and PowerShell 5: console mode and code page restored after Ctrl+] q, after Ctrl+Break and after closing the window; no key lost at the shell; resize reaches the remote; é€😀 output without U+FFFD) |
| P1-161 | issue | Medium | win | console.py:112, cli.py:149 | `shutil.get_terminal_size` uses COLUMNS/LINES first. | `os.get_terminal_size`. | S | needs Windows (fixed in 48bc35c: termdeck connect in Windows Terminal, conhost and PowerShell 5: console mode and code page restored after Ctrl+] q, after Ctrl+Break and after closing the window; no key lost at the shell; resize reaches the remote; é€😀 output without U+FFFD) |
| P1-162 | issue | Low | win | cli.py | Redirected output on Windows uses the ANSI code page: UnicodeEncodeError. | Reconfigure stdout/stderr to UTF-8. | S | needs Windows (fixed in a33c0b9: com3, \\.\COM10, COM10@115200 resolve and open; redirected 'termdeck list > out.txt' with accented names) |

### Windows platform

| ID | Type | Sev | Found by | Location | Problem | Fix | Size | Status |
|---|---|---|---|---|---|---|---|---|
| P1-170 | issue | Medium | win, sec | config.py:21-36 | chmod is a no-op on Windows: files outside the profile keep inherited ACLs. | Owner-only ACL on Windows. | M | needs Windows (fixed in 8792025: TERMDECK_HOME on NTFS outside the profile, a log in a shared folder, FAT/USB drive) |
| P1-171 | improvement | Low | win | ssh.py:84 | Paths: quotes from "Copy as path", `%VAR%` not expanded. | Strip quotes, expandvars. | S | needs Windows (fixed in a33c0b9: log with {name}=con/nul/COM1; key and log paths pasted with quotes or %USERPROFILE%) |
| P1-173 | improvement | Low | win | ssh.py:158 | No message when the Windows agent pipe is missing. | Document; notice. | S | proposed |
| P1-174 | improvement | Low | win | gui.py, pyproject | No app icon/AppUserModelID; no gui-script; DPI awareness. | Icon, `gui-scripts`. | M | proposed |
| P1-175 | improvement | Medium | feat, win | | No Windows installer or frozen build. | PyInstaller/MSIX build. | L | proposed |

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
| P1-220 | issue | Low | win | gui.py | Old WebView2 cache in %APPDATA% orphaned after moving to %LOCALAPPDATA%. | — | S | won't fix (new app: no existing installs to migrate) |
| P1-221 | issue | Low | win | config.py file_lock | Windows lock could wait 30 s and the comment said 10 s. | One LK_LOCK call (~10 s); store calls run in threads. | S | needs Windows (fixed in a33c0b9: save profiles from two termdeck processes at once; open profiles.json in another program while saving; check LF endings) |
| P1-222 | issue | Low | win | session.py | Superscript COM¹/LPT¹ device names not reserved. | Added. | S | needs Windows (fixed in a33c0b9: log with {name}=con/nul/COM1; key and log paths pasted with quotes or %USERPROFILE%) |
| P1-244 | issue | Low | win | cli.py ConsolePrompter | A cancelled ask leaves a reading thread (Ctrl+C at a prompt). | Covered by P1-158 (cancellable console read). | S | won't fix (only Ctrl+C cancels a prompt, and the program then exits; the daemon thread cannot outlive it) |
| P1-291 | issue | High | win | cli.py sftp shell | shlex ate backslashes in Windows paths. | No escapes on Windows. | S | needs Windows (fixed in 7b105ed: in the sftp shell, lcd C:\Users\me, put "C:\My Docs\a.txt"; get -r of a folder with a:b or con refused; piped UTF-8 script with accents) |
| P1-293 | issue | Low | win, conv | sftp.py get | Remote names Windows cannot store (a:b → alternate stream, con, x.) in downloads. | Refused with the list (single files too). | S | needs Windows (fixed in 7b105ed: in the sftp shell, lcd C:\Users\me, put "C:\My Docs\a.txt"; get -r of a folder with a:b or con refused; piped UTF-8 script with accents) |
| P1-294 | issue | Low | win, feat | cli.py | Piped sftp scripts decoded with the ANSI code page. | stdin UTF-8 (BOM allowed). | S | needs Windows (fixed in 7b105ed: in the sftp shell, lcd C:\Users\me, put "C:\My Docs\a.txt"; get -r of a folder with a:b or con refused; piped UTF-8 script with accents) |
| P1-317 | issue | High | win | proxy.py | Quoted program paths in a proxy command kept their quotes on Windows. | Strip quotes after a non-POSIX split. | S | needs Windows (fixed in e8b2e74: proxy command "C:\Program Files\...\connect.exe" %host %port connects; no console window from the desktop window) |
| P1-318 | issue | Medium | win | ssh.py | X11 display ":0" uses a unix socket the Windows loop lacks. | ":N" becomes "localhost:N" on Windows; clear error without a display. | S | needs Windows (fixed in e8b2e74: X11 with VcXsrv on localhost:0 and with ':0') |
| P1-319 | issue | Medium | win, feat | pyproject.toml | GSSAPI options did nothing: SSPI needs pywin32 (Windows), gssapi on POSIX. | pywin32 on Windows; `gssapi` extra elsewhere. | S | needs Windows (fixed in e8b2e74: Kerberos login on a domain PC with pywin32; on Linux needs the gssapi extra) |
| P1-363 | issue | Medium | win, sec | config.py owner_only_acl | pywin32 errors escaped as non-OSError; DACL rewritten on every call. | Swallowed (no ACL on FAT/shares); once per folder per process. | S | needs Windows (fixed in 8792025: save 100 profiles on NTFS and on a FAT USB drive) |
| P1-364 | issue | Low | win | gui.py | Saved window size grew with display scaling; minimised size saved; WebView2 folder ACL rewritten. | Divide by scale, skip tiny sizes, plain mkdir for WebView2. | S | needs Windows (fixed in 8792025: 150% scaling, open/close three times) |
| P1-365 | issue | Low | win | tui/files.py | Downloads ignored a redirected Downloads known folder. | SHGetKnownFolderPath. | S | needs Windows (fixed in 8792025: OneDrive-redirected Downloads) |
| P1-384 | issue | Minor | ui, win | tui/terminal.py | "Copied" shown though conhost ignores OSC 52. | Windows clipboard API on win32, or honest wording. | S | needs Windows (fixed in 0bac117: in conhost, drag-select in the TUI and paste elsewhere) |
| P1-387 | issue | Minor | ui, win | gui.py save_size | Maximised state not kept; maximised size saved as the normal size. | Track maximized/restored events; save the state. | S | needs Windows (fixed in 0bac117: maximise, close, reopen: the normal size comes back) |
| P1-390 | issue | Low | sec, win | config.py open_private | Windows: a new log is created with the folder's ACL and restricted after: another user can open it in between. | Create it with the security descriptor (win32file.CreateFile). | M | needs Windows (fixed in 3a95a45: a new log in a folder another user can read is never readable by them) |
| P1-391 | issue | Low | win | app.js shortcut | Alt+Shift switches keyboard layout on Windows with two layouts. | Test; if it clashes, pick another chord or make it configurable. | S | needs Windows (Alt+Shift+N/W/B in Edge and Chrome with two layouts installed) |
