"""File transfer over an SSH connection, for every UI (CLI, web, TUI).

Wraps asyncssh's SFTP client with what the UIs need: listings as plain data,
no silent overwrites (uploads go through a temporary name), wildcards, resumed
transfers, symbolic chmod modes, progress callbacks, and errors as SFTPFailure
messages naming the path.
"""

import asyncio
import posixpath
import re
import secrets
import stat
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import asyncssh

from .errors import SFTPFailure

CHUNK = 256 * 1024
Progress = Callable[[str, int, int], None]  # (name, bytes done, total bytes)
WILDCARDS = re.compile(r"[*?\[]")


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    is_link: bool
    size: int
    mtime: int
    mode: str  # "-rw-r--r--"


def join(directory: str, name: str) -> str:
    """A child path; `name` is a single file name (no directories, no ..)."""
    name = posixpath.basename(name.replace("\\", "/"))
    if name in ("", ".", ".."):
        raise SFTPFailure(f"invalid file name {name!r}")
    return posixpath.join(directory, name)


def parse_mode(spec: str, current: int = 0) -> int:
    """A chmod mode: octal (644) or symbolic like chmod and psftp (u+x, go-w, a=r, +t, u+s,g+s)."""
    if re.fullmatch(r"[0-7]{1,4}", spec):
        return int(spec, 8)
    mode = current & 0o7777
    shifts = (("u", 6), ("g", 3), ("o", 0))
    for clause in spec.split(","):
        m = re.fullmatch(r"([ugoa]*)([+=-])([rwxst]*)", clause)
        if not m:
            raise SFTPFailure(f"invalid mode {spec!r}: use octal (644) or symbolic (u+x, go-w)")
        who, op, what = m[1] or "a", m[2], m[3]
        if "a" in who:
            who = "ugo"
        bits = 0
        for person, shift in shifts:
            if person in who:
                bits |= sum(bit << shift for letter, bit in (("r", 4), ("w", 2), ("x", 1)) if letter in what)
        if "s" in what:
            bits |= (0o4000 if "u" in who else 0) | (0o2000 if "g" in who else 0)
        if "t" in what:
            bits |= 0o1000
        if op == "+":
            mode |= bits
        elif op == "-":
            mode &= ~bits
        else:
            mode = (mode & ~sum(0o7 << shift for person, shift in shifts if person in who)) | bits
    return mode


class SFTP:
    """One SFTP channel, opened on first use and shared by everything using the connection."""

    def __init__(self, conn: asyncssh.SSHClientConnection):
        self._conn = conn
        self._client: asyncssh.SFTPClient | None = None
        self._lock = asyncio.Lock()

    async def client(self) -> asyncssh.SFTPClient:
        async with self._lock:  # two first requests at once must not open two channels
            if self._client is None:
                try:
                    self._client = await self._conn.start_sftp_client()
                except asyncssh.Error as exc:
                    raise SFTPFailure(f"the server refused SFTP: {exc}") from None
            return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.exit()
            self._client = None

    async def _call(self, path: str, operation: str, *args, **kwargs):
        """Run an asyncssh SFTPClient method; errors become SFTPFailure naming `path`."""
        sftp = await self.client()
        try:
            return await getattr(sftp, operation)(*args, **kwargs)
        except asyncssh.SFTPError as exc:
            if exc.code in (asyncssh.FX_NO_CONNECTION, asyncssh.FX_CONNECTION_LOST):
                self._client = None  # the channel is gone: the next call opens a new one
            raise SFTPFailure(f"{path}: {exc.reason}") from None
        except asyncssh.Error as exc:  # the channel died while the connection lives: start a new one next time
            self._client = None
            raise SFTPFailure(f"{path}: SFTP channel closed ({exc})") from None

    async def realpath(self, path: str = ".") -> str:
        return await self._call(path, "realpath", path or ".")

    async def list(self, path: str = ".") -> tuple[str, "list[Entry]"]:  # quoted: `list` is this method
        """The absolute path and its entries, folders first, by name."""
        path = await self.realpath(path)
        entries = []
        for item in await self._call(path, "readdir", path):
            if item.filename in (".", ".."):
                continue
            mode = item.attrs.permissions or 0
            entries.append(Entry(item.filename, posixpath.join(path, item.filename), stat.S_ISDIR(mode),
                                 stat.S_ISLNK(mode), item.attrs.size or 0, item.attrs.mtime or 0,
                                 stat.filemode(mode) if mode else ""))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return path, entries

    async def glob(self, pattern: str) -> "list[str]":
        """Remote paths matching `pattern` (* ? [...]); the pattern itself when it has no wildcard."""
        if not WILDCARDS.search(pattern):
            return [pattern]
        return sorted(await self._call(pattern, "glob", pattern))  # asyncssh raises "No matches found"

    async def exists(self, path: str) -> bool:
        return await self._call(path, "exists", path)

    async def is_dir(self, path: str) -> bool:
        return await self._call(path, "isdir", path)

    async def mode(self, path: str) -> int:
        return (await self._call(path, "stat", path)).permissions or 0

    async def open_read(self, path: str):
        """(size, file) for a download; the file is an async context manager with read()."""
        if await self.is_dir(path):
            raise SFTPFailure(f"{path} is a folder")
        size = (await self._call(path, "stat", path)).size
        return size, await self._call(path, "open", path, "rb")

    async def write(self, path: str, chunks: AsyncIterator[bytes], *, overwrite: bool = False) -> int:
        """Write a remote file from `chunks`; refuses to replace one unless `overwrite`. Returns the size.

        The data goes to a temporary name first and replaces the target only when complete,
        so a cancelled or broken upload never leaves a half file or destroys the original.
        """
        if not overwrite and await self.exists(path):
            raise FileExistsError(path)
        folder, name = posixpath.split(path)
        temporary = posixpath.join(folder, f".{name}.{secrets.token_hex(4)}.part")
        done = 0
        try:
            async with await self._call(path, "open", temporary, "xb") as remote:
                async for chunk in chunks:
                    await remote.write(chunk)
                    done += len(chunk)
            if overwrite:
                await self._replace(temporary, path)
            elif await self.exists(path):  # it appeared during the upload
                raise FileExistsError(path)
            else:
                await self._call(path, "rename", temporary, path)
        except BaseException:
            try:
                await self._call(temporary, "remove", temporary)
            except (SFTPFailure, OSError):
                pass
            raise
        return done

    async def _replace(self, source: str, target: str) -> None:
        """Rename over an existing file: plain SFTP rename refuses, the POSIX extension replaces."""
        try:
            await self._call(target, "posix_rename", source, target)
        except SFTPFailure:
            if await self.exists(target):
                await self._call(target, "remove", target)
            await self._call(target, "rename", source, target)

    async def mkdir(self, path: str) -> None:
        await self._call(path, "mkdir", path)

    async def rmdir(self, path: str) -> None:
        await self._call(path, "rmdir", path)

    async def remove(self, path: str, recursive: bool = False) -> None:
        """Delete a file, or a folder and everything in it with `recursive`. A link is removed, never followed."""
        link = await self._call(path, "islink", path)
        if recursive and not link and await self.is_dir(path):
            await self._call(path, "rmtree", path)
        else:
            await self._call(path, "remove", path)

    async def rename(self, source: str, target: str, *, overwrite: bool = False) -> None:
        """Rename or move; a target that is a folder receives the source under its own name."""
        if await self.is_dir(target):
            target = posixpath.join(target, posixpath.basename(source.rstrip("/")))
        if not await self.exists(target):
            await self._call(source, "rename", source, target)
        elif not overwrite:
            raise FileExistsError(target)
        else:
            await self._replace(source, target)

    async def chmod(self, path: str, mode: int | str) -> None:
        """Set permissions: a number, or octal/symbolic text (symbolic applies to the current mode)."""
        if isinstance(mode, str):
            mode = parse_mode(mode, 0 if mode.isdigit() else await self.mode(path))
        if not 0 <= mode <= 0o7777:
            raise SFTPFailure(f"{path}: {mode:o} is not a permission mode (0 to 7777)")
        await self._call(path, "chmod", path, mode)

    async def get(self, remote: str, local: Path, *, recursive: bool = False, overwrite: bool = False,
                  progress: Progress | None = None) -> None:
        """Download to `local` (a file, or an existing folder to put it in)."""
        if local.is_dir():
            local = local / posixpath.basename(remote.rstrip("/"))
            if sys.platform == "win32" and _unsafe_on_windows(local.name):  # a:b would become a hidden stream
                raise SFTPFailure(f"{remote}: Windows cannot store the name {local.name!r}; give a local name")
        if local.exists() and not overwrite:
            raise FileExistsError(str(local))
        if sys.platform == "win32" and recursive and (bad := await self._unsafe_names(remote)):
            shown = ", ".join(bad[:5]) + (f" and {len(bad) - 5} more" if len(bad) > 5 else "")
            raise SFTPFailure(f"{remote}: names Windows cannot store ({shown}); rename them on the server first")
        await self._call(remote, "get", remote, str(local), recurse=recursive, preserve=True,
                         progress_handler=_handler(progress))

    async def put(self, local: Path, remote: str, *, recursive: bool = False, overwrite: bool = False,
                  progress: Progress | None = None) -> None:
        """Upload `local` to `remote` (a file name, or an existing folder to put it in)."""
        if not local.exists():
            raise SFTPFailure(f"{local}: no such file")
        if await self.is_dir(remote):
            remote = posixpath.join(remote, local.name)
        if await self.exists(remote) and not overwrite:
            raise FileExistsError(remote)
        await self._call(remote, "put", str(local), remote, recurse=recursive, preserve=True,
                         progress_handler=_handler(progress))

    async def reget(self, remote: str, local: Path, progress: Progress | None = None) -> None:
        """Continue an interrupted download: append what `local` lacks."""
        if local.is_dir():
            local = local / posixpath.basename(remote)
        have = local.stat().st_size if local.exists() else 0
        size, source = await self.open_read(remote)
        if have > (size or 0):
            await source.close()
            raise SFTPFailure(f"{local} is larger than {remote}: not a partial copy")
        async with source:
            await source.seek(have)
            with open(local, "ab") as target:
                done = have
                while chunk := await source.read(CHUNK):
                    target.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(local.name, done, size or done)

    async def reput(self, local: Path, remote: str, progress: Progress | None = None) -> None:
        """Continue an interrupted upload: append what `remote` lacks."""
        if await self.is_dir(remote):
            remote = posixpath.join(remote, local.name)
        have = (await self._call(remote, "stat", remote)).size if await self.exists(remote) else 0
        size = local.stat().st_size
        if have > size:
            raise SFTPFailure(f"{remote} is larger than {local}: not a partial copy")
        async with await self._call(remote, "open", remote, "ab") as target:
            with open(local, "rb") as source:
                source.seek(have)
                done = have
                while chunk := source.read(CHUNK):
                    await target.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(local.name, done, size)

    async def _unsafe_names(self, top: str) -> "list[str]":
        """Paths under `top` whose names Windows cannot store (a:b would even become a hidden stream)."""
        bad, folders = [], [top]
        if _unsafe_on_windows(posixpath.basename(top.rstrip("/"))):
            bad.append(top)
        while folders:
            folder = folders.pop()
            try:
                items = await self._call(folder, "readdir", folder)
            except SFTPFailure:
                continue  # not a folder
            for item in items:
                if item.filename in (".", ".."):
                    continue
                path = posixpath.join(folder, item.filename)
                if _unsafe_on_windows(item.filename):
                    bad.append(path)
                if stat.S_ISDIR(item.attrs.permissions or 0):
                    folders.append(path)
        return bad


_WINDOWS_BAD = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}


def _unsafe_on_windows(name: str) -> bool:
    return bool(_WINDOWS_BAD.search(name)) or name.split(".")[0].upper() in _WINDOWS_RESERVED or name.endswith((".", " "))


def _handler(progress: Progress | None):
    if progress is None:
        return None

    def handler(source: bytes, target: bytes, done: int, total: int) -> None:
        progress(posixpath.basename(source.decode("utf-8", "replace")), done, total)

    return handler
