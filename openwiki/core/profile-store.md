---
type: subsystem
title: Profile storage and private files
description: How ProfileStore keeps profiles in one versioned JSON file with cross-process locking, atomic replacement and lossless handling of invalid entries, and how config.py places termdeck's data and creates owner-only files on POSIX and Windows.
tags: [persistence, profiles, config, file-locking, permissions, windows]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-d47bafb87025777e901ce17b
    resource: repo://src/termdeck/config.py
  - id: openwiki-source-4003fa8570daad2a67aaf127
    resource: repo://src/termdeck/profiles.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Profile storage and private files

Profiles live in `profiles.json` in termdeck's settings folder. Two modules share the job:
`profiles.ProfileStore` owns the file format and the read-modify-write cycle, and
`config.py` owns folder locations, private file creation, atomic writes and locks. The same
`config.py` helpers are reused for `known_hosts` (see [SSH transport](../transports/ssh.md)),
session logs and the desktop window's size file.

## Locations

`config.config_dir()` returns `TERMDECK_HOME` when set, otherwise
`%APPDATA%\termdeck` (Windows), `~/Library/Application Support/termdeck` (macOS) or
`$XDG_CONFIG_HOME/termdeck` (default `~/.config/termdeck`). `config.data_dir()` is for caches
that must not roam (the webview's storage): `%LOCALAPPDATA%`, `~/Library/Caches` or
`$XDG_DATA_HOME`; with `TERMDECK_HOME` set it is the same folder. Tests rely on
`TERMDECK_HOME` to isolate every run (see [Test suite](../testing/test-suite.md)).

`config.user_path` interprets paths the user typed or pasted: surrounding quotes (Windows
"Copy as path") are removed, then `$VAR`/`%VAR%` and `~` are expanded.

## The profile file

```json
{"version": 1, "profiles": [ {"name": ..., "kind": "ssh", "group": ..., "ssh": {...}, "terminal": {...}}, ... ]}
```

- `Profile.to_dict` stores only the active connection section (`ssh` *or* `serial`) plus
  `terminal`; the other section comes back as defaults. See
  [Profiles and the option registry](profiles-and-options.md).
- `FORMAT_VERSION = 1`. A file with a higher or non-integer `version` is refused with
  `ProfileFileError` ("written by a newer termdeck") and left untouched.
- Invalid JSON, non-UTF-8 content or the wrong shape also raise `ProfileFileError`, naming the
  line and column. Because every write first reads the file, **nothing is saved until the
  user fixes or moves the file** — a corrupt file is never overwritten.
- Entries are written sorted by name, case-insensitively.

### Invalid and duplicate entries are kept

`_read` validates each entry with `Profile.from_dict`. An entry that fails, or that repeats an
earlier name, is *skipped*: `load()` leaves it out and records `(name, error)` in
`store.invalid`; `warnings()` turns these into lines the UIs show. Skipped entries are still
written back verbatim after the valid ones, so a hand edit gone wrong never loses data.

- `get(name)` explains when the name exists but is invalid, and suggests `termdeck set` or
  `termdeck rm`; otherwise it raises `ProfileNotFound`.
- `get_raw(name)` returns even an invalid entry (`from_dict(validate=False)`) so an editor can
  repair it; `update(name, profile)` accepts a skipped entry's name and replaces it.
- `delete(name)` removes every entry with that name, valid or skipped.

### Writes

`add`, `update`, `save` and `delete` validate first, then hold `file_lock(path)` while they
re-read the file, change their own copy and write it back, so two windows, threads or
processes saving at once never drop each other's profiles.

- `add` refuses a name already used by a valid or skipped entry.
- `update` renames when `profile.name` differs, refuses to rename onto a taken name, and
  raises `ProfileNotFound` if the profile was deleted or renamed meanwhile.
- `add` and `update` call `_record_identity`, which for serial profiles records the USB
  adapter currently on the device (`serialport.update_identity`; a changed device name drops
  the old identity). `save` is for scripts and tests and skips that and the name checks. See
  [Serial transport](../transports/serial.md).

The store is synchronous; async callers move it to a thread (the controller does
`asyncio.to_thread(self.store.get, ...)` because of the lock).

## Private files, atomic writes and locks (`config.py`)

- `private_dir(path)` creates the folder with mode `0o700` and on POSIX re-applies it to an
  existing folder. On Windows, where `chmod` does nothing, it sets an owner-only, protected
  (non-inherited) access list once per process (`owner_only_acl`, needs pywin32; silently
  skipped on filesystems without ACLs).
- `open_private(path, mode, own_dir=True)` opens a file only the owner can read.
  - `own_dir=True` (termdeck's own files): the parent is made private and an existing file
    is set back to `0o600`.
  - `own_dir=False` (session logs and printer files, possibly in shared folders like `/tmp`):
    the folder's permissions are left alone; `O_NOFOLLOW` is used, and an existing file that
    is not a regular file owned by the current user with a single link is refused
    (`PermissionError`) — this blocks symlink and hard-link tricks. Truncation happens only
    after these checks. On Windows a new file is created owner-only from the start via
    `create_owner_only` (`CreateFile` with a security descriptor) so nobody can open it in
    between.
  - Text files always use UTF-8 and LF line endings.
- `atomic_write(path, text)` writes a temp file unique per process and thread
  (`.{name}.{pid}.{thread}.tmp`), then `replace`s the target. On Windows it retries for up to
  ~0.5 s when another program (antivirus, backup) briefly holds the file.
- `file_lock(path)` locks `path.lock` exclusively: `fcntl.flock` on POSIX,
  `msvcrt.locking` on Windows (which gives up after about 10 s with `OSError`).

## Tests

`tests/test_store.py` covers corrupt and newer files being left untouched, duplicates kept
and reported, concurrent adds losing nothing, repairing invalid entries through `update`,
`open_private` refusing symlinks and hard links, and log-file permissions.

## Related

- [Profiles and the option registry](profiles-and-options.md)
- [Security model](../security/model.md)
- [Serial transport and USB identity](../transports/serial.md)
