---
type: subsystem
title: SFTP file transfer
description: The core SFTP class that shares one lazily opened channel per SSH connection and implements listing, safe uploads via temporary files, downloads without copying dangerous modes, resumable transfers, chmod parsing and Windows-unsafe name checks — and the three front ends built on it, the psftp-like `termdeck sftp` command, the TUI files panel and the web SFTP API.
tags: [sftp, file-transfer, psftp, ssh, windows]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-40dcfdf1b085605444d2f636
    resource: repo://src/termdeck/cli.py
  - id: openwiki-source-cb7f1de01bf167b93eb32524
    resource: repo://src/termdeck/sftp.py
  - id: openwiki-source-a1b0d5ac424a24d3717cfa4c
    resource: repo://tests/test_sftp.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# SFTP file transfer

`src/termdeck/sftp.py` holds all SFTP logic. UIs only call it: the
[CLI](../interfaces/cli-and-console.md) (`termdeck sftp`), the
[TUI](../interfaces/tui.md) files panel and the [web UI](../interfaces/web-ui.md) endpoints.

## One channel per connection

`SSHConnection.sftp()` creates one `SFTP` object per SSH connection; a session exposes it
through `Session.sftp()` (SSH shells have `has_sftp = True`, which the controller turns into
the `sftp` capability; serial sessions return `None`). The SFTP channel opens on first use
under a lock, so two concurrent first requests never open two channels. A server refusing the
subsystem gives `SFTPFailure("the server refused SFTP: …")`. Every call goes through `_call`,
which turns asyncssh errors into `SFTPFailure("<path>: <reason>")`; if the channel died
(connection lost, or another asyncssh error while the connection lives) it is dropped and the
next call opens a new one. `close()` is called when the connection closes. See
[SSH transport](ssh.md).

## Operations

| Method | Behaviour |
|---|---|
| `realpath`, `list(path)` | absolute path and entries (`Entry`: name, path, dir, link, size, mtime, `ls`-style mode), folders first, then by name; `.`/`..` skipped |
| `glob(pattern)` | remote matches for `* ? [..]`; the pattern itself without wildcards |
| `exists`, `is_dir`, `mode` | stat helpers |
| `open_read(path)` | `(size, file)` for streaming downloads; refuses folders |
| `write(path, chunks, overwrite=False)` | streaming upload (web UI) |
| `get(remote, local, recursive, overwrite, progress)` | download a file or tree |
| `put(local, remote, recursive, overwrite, progress)` | upload a file or tree |
| `reget`, `reput` | resume by appending what the other side lacks; refuse when the target is larger (not a partial copy) |
| `mkdir`, `rmdir`, `remove(path, recursive)`, `rename(src, dst, overwrite)`, `chmod(path, mode)` | file management |

Safety rules:
- **Never replace without asking**: `write`, `get`, `put` and `rename` raise `FileExistsError`
  unless `overwrite`. UIs turn it into "already exists (use -f to replace it)" or a 409 the page
  asks about.
- **Uploads are atomic**: `write` streams to `.<name>.<random>.part` (opened exclusively), then
  renames onto the target — with `posix-rename` when replacing, falling back to remove+rename —
  and removes the temporary file on any failure or cancellation, so a broken upload never
  leaves half a file or destroys the original. A file that appears during the upload is not
  overwritten.
- **Downloads keep the user's local permissions**: `preserve=False` (copying the remote mode
  could bring setuid or world-writable bits); only the modification time is kept.
- **Links**: `remove(recursive=True)` deletes a symbolic link itself, never the tree it points to.
- **Windows names**: when downloading on Windows, names with `<>:"|?*\`, control characters,
  reserved device names (`CON`, `COM1`…) or a trailing dot/space are refused (`a:b` would even
  become a hidden alternate data stream); recursive downloads check the whole tree first.
- `join(directory, name)` takes only the base name of an uploaded file name and rejects
  `""`, `.`, `..`, so a browser-supplied name cannot escape the target folder.
- `parse_mode` accepts octal (`644`) or symbolic clauses like chmod/psftp (`u+x`, `go-w`,
  `a=r`, `+t`, `u+s,g+s`); symbolic modes apply to the current mode; results outside 0–7777 are
  refused.

Progress callbacks receive `(name, bytes done, total)`.

## `termdeck sftp TARGET [COMMAND]`

Connects with `SSHConnection.connect` (saved profile or quick target) and then:
- with words after the target: runs one command (`--force` or a leading `-f` replaces files);
- with `-b FILE`, or stdin not a terminal: runs a script (`#` comments, blank lines skipped,
  quotes as in a shell, backslashes literal on Windows); stops at the first error with
  `line N:` unless `--keep-going` (psftp `-be`); `--echo` shows each line (psftp `-bc`); `quit`
  ends it;
- otherwise: an interactive shell with a `sftp <cwd>>` prompt on the terminal.

Commands follow psftp: `ls [DIR]` (alias `dir`), `cd`, `pwd`, `lcd`, `lpwd`,
`get [-r] [-f] REMOTE [LOCAL]` / `mget`, `put [-r] [-f] LOCAL [REMOTE]` / `mput`, `reget`,
`reput`, `mkdir`, `rmdir`, `rm [-r] [-f]` (alias `del`, `delete`), `mv [-f]` (alias `ren`,
`rename`), `chmod MODE PATH…`, `help`. Remote and local names may use wildcards; local paths
expand `~` and `$VAR`/`%VAR%`. In the interactive shell a recursive delete of a folder asks
first. Remote names are printed through `printable` so no escape sequence from the server
reaches the terminal. Short and long flags (`-rf`, `--force`, `--recursive`, `--`) are parsed
per command and unknown ones are rejected.

## Panels

- **TUI** (`tui/files.py`): browse, download with Enter to the Downloads folder, upload a local
  path, delete, refresh ([TUI](../interfaces/tui.md)).
- **Web/desktop** (`/api/sessions/{sid}/sftp…`): list, single-use download tickets, upload with
  a prior existence check, mkdir, rename, remove, rmdir and chmod; drag-and-drop upload in the
  page ([Web UI](../interfaces/web-ui.md)).

## Tests

`tests/test_sftp.py` runs against the fixture server's chrooted SFTP root: listings, overwrite
refusal, `.part` files on failure and cancellation, a file appearing during upload, `join`,
one channel for concurrent first requests, `parse_mode` and chmod, glob, resume, and channel
recovery. The `termdeck sftp` command, scripts and flags are tested in `tests/test_cli.py`.
