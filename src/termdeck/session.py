"""Common interface between connections and user interfaces."""

import asyncio
import codecs
import re
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .config import open_private, user_path
from .lineedit import LineEditor
from .profiles import Profile, TerminalSettings


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Output ending like this means the device waits for a secret (line editing hides it).
_SECRET_PROMPT = re.compile(r"(pass(word|phrase)?|pin|passwort|mot de passe)[^\n]{0,20}[:>]\s*$", re.I)


# Names Windows reserves for devices, with any extension: CON.log is still the console.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"{d}{i}" for d in ("COM", "LPT") for i in [*range(10), "¹", "²", "³"])}


def _file_part(text: str) -> str:
    """`text` made safe as part of a file name on every platform."""
    text = re.sub(r"[^\w.-]", "_", text).lstrip(".").rstrip(". ") or "session"
    return f"_{text}" if text.split(".")[0].upper() in _RESERVED else text


def log_path(template: str, profile: Profile, now: datetime | None = None) -> Path:
    """The log file for a session: {name}, {host} and {date} replaced, anything else kept as typed."""
    host = profile.ssh.host if profile.kind == "ssh" else profile.serial.device
    values = {"name": profile.name, "host": host, "date": (now or datetime.now()).strftime("%Y%m%d-%H%M%S")}
    path = template
    for key, value in values.items():
        path = path.replace("{" + key + "}", _file_part(value))
    return user_path(path)


def printable(text: str) -> str:
    """Drop control characters, so remote text cannot move the cursor or fake output."""
    return _CONTROL.sub("", text)


class Prompter(Protocol):
    """Questions a connection may ask the user. Each UI provides its own."""

    async def ask(self, prompt: str, secret: bool = True) -> str | None:
        """Return the answer, or None if the user cancels. `secret` hides the input."""

    async def confirm_host_key(self, host: str, port: int, fingerprint: str, changed: bool) -> bool:
        """Return True to trust and remember the key. `changed` means a different key was known."""


class Session(ABC):
    """A byte stream to a terminal on the other side."""

    title: str = ""
    can_break: bool = False

    @abstractmethod
    async def read(self) -> bytes:
        """Return the next chunk of output, or b"" once the session has ended."""

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    def resize(self, cols: int, rows: int) -> None:
        """Report the new terminal size. Ignored by sessions without one."""

    async def send_break(self) -> None:
        """Send a serial break. Only sessions with `can_break` support it."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None: ...


ENTER = {"cr": b"\r", "crlf": b"\r\n", "lf": b"\n"}


class TerminalSession(Session):
    """Applies a profile's terminal settings on top of a raw session.

    UIs always exchange UTF-8 with it; the remote side gets the profile's
    character set, key mapping, local echo and logging.
    """

    def __init__(self, inner: Session, settings: TerminalSettings, log: Path | None = None):
        self.inner = inner
        self.title = inner.title
        self.can_break = inner.can_break
        self._settings = settings
        self._utf8 = settings.encoding.replace("_", "-").lower() in ("utf-8", "utf8")
        self._decoder = codecs.getincrementaldecoder(settings.encoding)("replace")
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._pump: asyncio.Task | None = None
        self._editor = LineEditor() if settings.line_editing else None
        self._tail = ""  # end of the output, to spot password prompts
        # The log's folder belongs to the user: create it if needed, never change its permissions.
        self._log = open_private(log, "ab", own_dir=False) if log else None
        self._flush_timer: asyncio.TimerHandle | None = None

    async def _run_pump(self) -> None:
        try:
            while data := await self.inner.read():
                if self._log:
                    # Buffered, and flushed within a second, so `tail -f` keeps up without a write per chunk.
                    try:
                        self._log.write(data)
                    except OSError as exc:
                        self._stop_logging(exc)
                    if self._log and self._flush_timer is None:
                        self._flush_timer = asyncio.get_running_loop().call_later(1, self._flush_log)
                if not self._utf8:
                    data = self._decoder.decode(data).encode("utf-8")
                if self._editor:
                    self._tail = (self._tail + data.decode("utf-8", "replace"))[-200:]
                    self._editor.secret = bool(_SECRET_PROMPT.search(self._tail))
                if data:
                    self._queue.put_nowait(data)
        finally:
            self._flush_log()  # the session ended: its last output is on disk now
            self._queue.put_nowait(b"")

    def _flush_log(self) -> None:
        self._flush_timer = None
        if self._log:
            try:
                self._log.flush()
            except OSError as exc:
                self._stop_logging(exc)

    def _stop_logging(self, exc: OSError) -> None:
        """Disk full, drive gone: stop logging and say so, but keep the session."""
        try:
            self._log.close()
        except OSError:
            pass
        self._log = None
        self._queue.put_nowait(f"\r\n[termdeck] logging stopped: {describe_error(exc)}\r\n".encode())

    async def read(self) -> bytes:
        if self._pump is None:
            self._pump = asyncio.create_task(self._run_pump())
        data = await self._queue.get()
        if not data:
            self._queue.put_nowait(b"")  # keep reporting the end to later reads
        return data

    def _translate(self, data: bytes) -> bytes:
        s = self._settings
        if s.enter_sends != "cr":
            data = data.replace(b"\r", ENTER[s.enter_sends])
        if s.backspace_sends == "bs":
            data = data.replace(b"\x7f", b"\x08")
        return data

    async def write(self, data: bytes) -> None:
        if self._editor:
            echo, data = self._editor.feed(data)
            if echo:
                self._queue.put_nowait(echo)
            if not data:
                return
        elif self._settings.local_echo:
            self._queue.put_nowait(data.replace(b"\r", b"\r\n").replace(b"\x7f", b"\x08 \x08"))
        data = self._translate(data)
        if not self._utf8:
            data = data.decode("utf-8", "replace").encode(self._settings.encoding, "replace")
        await self.inner.write(data)

    def resize(self, cols: int, rows: int) -> None:
        self.inner.resize(cols, rows)

    async def send_break(self) -> None:
        await self.inner.send_break()

    async def close(self) -> None:
        await self.inner.close()
        if self._pump:
            await asyncio.gather(self._pump, return_exceptions=True)
        if self._flush_timer:
            self._flush_timer.cancel()
            self._flush_timer = None
        if self._log:
            self._log.close()
            self._log = None


def describe_error(exc: BaseException) -> str:
    """One-line message for a failed connection, suitable for any UI."""
    return printable(_describe_error(exc))


def _describe_error(exc: BaseException) -> str:
    import asyncssh
    import serial

    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        return "Host key not trusted, connection aborted"
    if isinstance(exc, asyncssh.PermissionDenied):
        return "Authentication failed"
    if isinstance(exc, asyncssh.DisconnectError):
        return f"Disconnected: {exc.reason}"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "Connection timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "Connection refused"
    if isinstance(exc, serial.SerialException):
        return f"Serial port error: {exc}"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or type(exc).__name__


async def open_raw_session(profile: Profile, prompter: Prompter, cols: int, rows: int) -> Session:
    if profile.kind == "ssh":
        from .ssh import SSHConnection

        conn = await SSHConnection.connect(profile, prompter)
        try:
            notices = await conn.start_forwards()
            return await conn.open_shell(cols, rows, notices)
        except BaseException:
            conn.close()
            raise
    if profile.kind == "serial":
        from .serialport import SerialSession

        return await SerialSession.open(profile)
    raise ValueError(f"unknown profile type {profile.kind!r}")


async def open_session(profile: Profile, prompter: Prompter, cols: int = 80, rows: int = 24) -> Session:
    """Connect to `profile` and return an interactive session with its terminal settings applied."""
    profile.validate()
    raw = await open_raw_session(profile, prompter, cols, rows)
    try:
        log = log_path(profile.terminal.log_file, profile) if profile.terminal.log_file else None
        return TerminalSession(raw, profile.terminal, log)
    except BaseException:
        await raw.close()
        raise
