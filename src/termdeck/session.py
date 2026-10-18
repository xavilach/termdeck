"""Common interface between connections and user interfaces."""

import asyncio
import codecs
import importlib
import logging
import re
import sys
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .config import open_private, user_path
from .errors import ProfileError, TermdeckError
from .lineedit import LineEditor
from .profiles import TRANSPORTS, Profile, TerminalSettings
from .vt import PrintableLog, PrinterCapture, RemoteFilter, initial_modes, remap_keys

log = logging.getLogger(__name__)


# Control characters, and the Unicode format and separator characters that reorder,
# hide or break text (bidi overrides, zero-width, tags): remote text must not move
# the cursor or disguise itself. Newline and tab are kept.
_HIDDEN = ("Cc", "Cf", "Zl", "Zp")
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
    """Drop control and invisible characters, so remote text cannot move the cursor or fake output."""
    return "".join(c for c in text if c in "\n\t" or unicodedata.category(c) not in _HIDDEN)


@dataclass
class HostKey:
    """A server key that is not trusted yet, as shown to the user."""

    host: str
    port: int
    key_type: str                  # ssh-ed25519, ecdsa-sha2-nistp256...
    fingerprint: str               # SHA256:...
    changed: bool = False          # a different key was trusted for this host
    old_fingerprints: list[str] = field(default_factory=list)
    known_hosts: str = ""          # where the key will be saved


class Prompter(Protocol):
    """Questions a connection may ask the user. Each UI provides its own."""

    async def ask(self, prompt: str, secret: bool = True) -> str | None:
        """Return the answer, or None if the user cancels. `secret` hides the input."""

    async def confirm_host_key(self, key: HostKey) -> bool:
        """Return True to trust and remember the key."""

    async def notify(self, text: str) -> None:
        """Show a message from the connection (server banner, login instructions). Optional."""


async def notify(prompter: Prompter, text: str) -> None:
    """Pass a message to the prompter, if it shows messages."""
    if text.strip() and (show := getattr(prompter, "notify", None)):
        await show(text)


class Session(ABC):
    """A byte stream to a terminal on the other side."""

    title: str = ""
    can_break: bool = False
    # Why the session ended, for the UI, once read() has returned b"". None: closed normally.
    end_reason: str | None = None

    @abstractmethod
    async def read(self) -> bytes:
        """Return the next chunk of output, or b"" once the session has ended."""

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    async def write_eof(self) -> bool:
        """Tell the remote side input has ended. False if it cannot know (a serial port)."""
        return False

    def sftp(self):
        """File transfer on the same connection (sftp.SFTP), or None (serial)."""
        return None

    def resize(self, cols: int, rows: int) -> None:
        """Report the new terminal size. Ignored by sessions without one."""

    async def send_break(self) -> None:
        """Send a serial break. Only sessions with `can_break` support it."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None: ...

    def on_notice(self, show) -> None:
        """Send this session's own messages (not remote output, never logged) to `show`."""
        self._show = show
        for data in getattr(self, "_early_notices", []):
            show(data)
        self._early_notices = []

    def notice(self, text: str) -> None:
        """A message from termdeck about this session, shown in the terminal."""
        data = f"\r\n[termdeck] {text}\r\n".encode()
        if show := getattr(self, "_show", None):
            show(data)
        else:
            self._early_notices = [*getattr(self, "_early_notices", []), data]


ENTER = {"cr": b"\r", "crlf": b"\r\n", "lf": b"\n"}
OUTPUT_BACKLOG = 64  # chunks read ahead of the UI; beyond that the remote side waits
ENQ = b"\x05"


class TerminalSession(Session):
    """Applies a profile's terminal settings on top of a raw session.

    UIs always exchange UTF-8 with it; the remote side gets the profile's
    character set, key mapping, local echo, answerback and logging. A `raw`
    session (scripted use, data in pipes) passes bytes unchanged and only logs;
    its notices go wherever `on_notice` sends them.
    """

    def __init__(self, inner: Session, settings: TerminalSettings, log=None, raw: bool = False,
                 log_header: str = "", ssh_log=None):
        """`log` is a path, or a log already opened by open_session (with `ssh_log`, its SSH
        packet handler, started before the connection so the handshake is logged)."""
        self.inner = inner
        self._raw = raw
        self.title = inner.title
        self.can_break = inner.can_break
        self._settings = settings
        self._utf8 = settings.encoding.replace("_", "-").lower() in ("utf-8", "utf8")
        self._decoder = codecs.getincrementaldecoder(settings.encoding)("replace")
        self._input_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        # Output is bounded (so a flood waits at the remote side, by flow control);
        # local echo and notices are not, so typing never blocks.
        self._queue: asyncio.Queue[tuple[bytes, bool]] = asyncio.Queue()
        self._room = asyncio.Semaphore(OUTPUT_BACKLOG)
        self._pump: asyncio.Task | None = None
        self._failure: str | None = None
        self._editor = LineEditor() if settings.line_editing and not raw else None
        self._tail = ""  # end of the output, to spot password prompts
        self._log = open_log(log, settings, log_header) if isinstance(log, Path) else log
        self._ssh_log = ssh_log
        self._printable = PrintableLog() if settings.log_mode == "printable" else None
        self._flush_timer: asyncio.TimerHandle | None = None
        self._release_timer: asyncio.TimerHandle | None = None
        self._held = b""  # a CR at the end of a chunk, until we know whether LF follows
        self._filter = RemoteFilter(settings)
        try:
            self._printer = open_private(user_path(settings.printer), "ab", own_dir=False) if settings.printer else None
        except BaseException:  # nothing half set up stays behind (the log, the process-wide SSH handler)
            self._close_files()
            raise
        self._print = PrinterCapture(self._printed) if self._printer else None
        if not raw:
            inner.on_notice(self._local)
            if modes := initial_modes(settings):
                self._local(modes)

    def on_notice(self, show) -> None:
        self.inner.on_notice(show)

    @property
    def end_reason(self) -> str | None:
        return self.inner.end_reason or self._failure

    @property
    def exit_status(self) -> int | None:
        """The remote program's exit status, when the transport has one (SSH)."""
        return getattr(self.inner, "exit_status", None)

    async def read_stderr(self) -> bytes:
        """The remote error output when it is kept apart (SSH without a terminal), else b""."""
        read = getattr(self.inner, "read_stderr", None)
        return await read() if read else b""

    async def write_eof(self) -> bool:
        return await self.inner.write_eof()

    def sftp(self):
        return self.inner.sftp()

    def _local(self, data: bytes) -> None:
        """Show bytes that did not come from the remote side (echo, notices)."""
        self._queue.put_nowait((data, False))

    def _received(self, data: bytes) -> bytes:
        """Output as the UI must show it: UTF-8, with the implicit CR/LF settings applied."""
        s = self._settings
        if not self._utf8:
            data = self._decoder.decode(data).encode("utf-8")
        if self._print:
            data = self._print.feed(data)
        data = self._filter.feed(data)  # what the remote side may not do (Remote control options)
        if self._filter.holding and self._release_timer is None:  # a cut sequence: shown soon even if nothing follows
            self._release_timer = asyncio.get_running_loop().call_later(0.05, self._release_held)
        if s.implicit_cr or s.implicit_lf:
            data, self._held = self._held + data, b""
            if s.implicit_lf and data.endswith(b"\r"):
                data, self._held = data[:-1], b"\r"
                # A lone CR (a prompt, a progress line) shows soon even if nothing follows.
                asyncio.get_running_loop().call_later(0.05, self._release_cr)
            # One pass, like PuTTY: each CR or LF becomes one new line, never two.
            pattern = {(True, True): rb"\r\n|\r|\n", (True, False): rb"\r?\n", (False, True): rb"\r\n?"}
            data = re.sub(pattern[s.implicit_cr, s.implicit_lf], b"\r\n", data)
        return data

    def _release_held(self) -> None:
        self._release_timer = None
        if data := self._filter.flush():
            self._local(data)

    def _printed(self, data: bytes) -> None:
        """Remote printing: into the printer file, flushed at the end of each job."""
        if not self._printer:
            return
        try:
            self._printer.write(data)
            self._printer.flush()
        except OSError as exc:
            self._printer.close()
            self._printer = None
            self._local(f"\r\n[termdeck] printing stopped: {describe_error(exc)}\r\n".encode())

    def _close_files(self) -> None:
        if self._ssh_log:
            _end_ssh_logging(self._ssh_log)
            self._ssh_log = None
        for name in ("_log", "_printer"):
            if (file := getattr(self, name, None)) is not None:
                try:
                    file.close()
                except OSError:
                    pass
                setattr(self, name, None)

    def _release_cr(self) -> None:
        if self._held:
            self._held = b""
            self._local(b"\r\n")

    async def _run_pump(self) -> None:
        try:
            while data := await self.inner.read():
                if self._log:
                    # Buffered, and flushed within a second, so `tail -f` keeps up without a write per chunk.
                    try:
                        self._log.write(self._printable.feed(data) if self._printable else data)
                    except OSError as exc:
                        self._stop_logging(exc)
                    if self._log and self._flush_timer is None:
                        self._flush_timer = asyncio.get_running_loop().call_later(1, self._flush_log)
                if self._raw:
                    await self._room.acquire()
                    self._queue.put_nowait((data, True))
                    continue
                if ENQ in data and self._settings.answerback:
                    await self.inner.write(self._settings.answerback.encode(self._settings.encoding, "replace"))
                data = self._received(data)
                if self._editor:
                    self._tail = (self._tail + data.decode("utf-8", "replace"))[-200:]
                    self._editor.secret = bool(_SECRET_PROMPT.search(self._tail))
                if data:
                    await self._room.acquire()
                    self._queue.put_nowait((data, True))
        except Exception as exc:  # end the session and say why; a bug also goes to the log
            if not is_expected(exc):
                log.exception("session output failed")
            self._failure = describe_error(exc)
        finally:
            if rest := self._filter.flush():
                self._local(rest)
            if self._held:
                self._local(b"\r\n")  # the held CR, with its implicit LF
            self._flush_log()  # the session ended: its last output is on disk now
            self._queue.put_nowait((b"", False))

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
        self._local(f"\r\n[termdeck] logging stopped: {describe_error(exc)}\r\n".encode())

    async def read(self) -> bytes:
        if self._pump is None:
            self._pump = asyncio.create_task(self._run_pump())
        data, counted = await self._queue.get()
        if counted:
            self._room.release()
        if not data:
            self._queue.put_nowait((b"", False))  # keep reporting the end to later reads
        return data

    def _translate(self, data: bytes) -> bytes:
        s = self._settings
        if s.enter_sends != "cr":
            data = data.replace(b"\r", ENTER[s.enter_sends])
        if s.backspace_sends == "bs":
            data = data.replace(b"\x7f", b"\x08")
        return remap_keys(data, s)

    def _encode(self, data: bytes) -> bytes:
        """UTF-8 from the UI to the profile's character set (multibyte keys may span writes)."""
        if self._utf8:
            return data
        return self._input_decoder.decode(data).encode(self._settings.encoding, "replace")

    async def write(self, data: bytes) -> None:
        if self._raw:
            await self.inner.write(data)
            return
        if self._editor:
            echo, data = self._editor.feed(data)
            if echo:
                self._local(echo)
            if not data:
                return
        elif self._settings.local_echo:
            self._local(data.replace(b"\r", b"\r\n").replace(b"\x7f", b"\x08 \x08"))
        await self.inner.write(self._encode(self._translate(data)))

    def resize(self, cols: int, rows: int) -> None:
        self.inner.resize(cols, rows)

    async def send_break(self) -> None:
        await self.inner.send_break()

    async def close(self) -> None:
        if self._pump:
            self._pump.cancel()  # it may be waiting for room the UI will never make
        await self.inner.close()
        if self._pump:
            await asyncio.gather(self._pump, return_exceptions=True)
        if self._flush_timer:
            self._flush_timer.cancel()
            self._flush_timer = None
        if self._release_timer:
            self._release_timer.cancel()
            self._release_timer = None
        self._close_files()


def hide_secrets(record) -> bool:
    """A logging filter: asyncssh logs environment variables with their values
    ("[chan=0]   Env: %s=%s"), which may be tokens."""
    if isinstance(record.msg, str) and "Env: %s=%s" in record.msg and record.args:
        record.args = (*record.args[:-1], "(hidden)")
    return True


class _LogHandler(logging.Handler):
    """SSH protocol messages into the session log (log mode "SSH packets")."""

    def __init__(self, log):
        super().__init__(logging.DEBUG)
        self._file = log
        self.addFilter(hide_secrets)
        self.setFormatter(logging.Formatter("%(asctime)s ssh: %(message)s"))

    def emit(self, record) -> None:
        try:
            self._file.write((self.format(record) + "\r\n").encode("utf-8", "replace"))
        except (OSError, ValueError):
            pass


_SSH_LOGS: list = []  # session logs receiving asyncssh's messages, and the level to restore after the last
_SAVED_LEVEL: list = []


def _ssh_logging(log):
    """Send asyncssh's messages to a session log. asyncssh logs process-wide: while such a log is
    open it also gets the other SSH connections of this termdeck (the option help says so)."""
    import asyncssh

    handler = _LogHandler(log)
    logger = logging.getLogger("asyncssh")
    if not _SSH_LOGS:
        _SAVED_LEVEL[:] = [logger.level]
    _SSH_LOGS.append(handler)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    asyncssh.set_debug_level(2)  # packets and algorithms; level 3 (raw data) could contain typed secrets
    return handler


def _end_ssh_logging(handler) -> None:
    import asyncssh

    logger = logging.getLogger("asyncssh")
    logger.removeHandler(handler)
    if handler in _SSH_LOGS:
        _SSH_LOGS.remove(handler)
    if not _SSH_LOGS and _SAVED_LEVEL:  # the last one: back to how logging was
        logger.setLevel(_SAVED_LEVEL.pop())
        asyncssh.set_debug_level(1)


# Each transport module (TRANSPORTS, in profiles.py) provides
# `async open_session(profile, prompter, cols, rows, scripted) -> Session`,
# `describe_error(exc) -> str | None` and `ERRORS`, the exception types that mean
# "the connection failed" rather than "termdeck has a bug". Imported on first
# use: the serial tools never load asyncssh.


def _loaded_transports():
    return [module for name in TRANSPORTS.values() if (module := sys.modules.get(name))]


def is_expected(exc: BaseException) -> bool:
    """True for failures of the network, device, files or input; False for bugs."""
    expected = (TermdeckError, OSError, TimeoutError, asyncio.TimeoutError, UnicodeError)
    return isinstance(exc, expected) or any(isinstance(exc, m.ERRORS) for m in _loaded_transports())


def describe_error(exc: BaseException) -> str:
    """One-line message for a failed connection, suitable for any UI."""
    return printable(_describe_error(exc))


def _describe_error(exc: BaseException) -> str:
    for module in _loaded_transports():
        if text := module.describe_error(exc):
            return text
    if isinstance(exc, TermdeckError):
        return str(exc)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "Connection timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "Connection refused"
    if isinstance(exc, OSError) and exc.strerror:
        return f"{exc.strerror}: {exc.filename}" if exc.filename else exc.strerror
    if not is_expected(exc):
        return f"Internal error ({type(exc).__name__}: {exc}); please report it"
    return str(exc) or type(exc).__name__


async def open_raw_session(profile: Profile, prompter: Prompter, cols: int, rows: int,
                           scripted: bool = False) -> Session:
    """The transport's session. `scripted`: data in pipes (remote stderr kept apart, see SSHShell)."""
    if profile.kind not in TRANSPORTS:
        raise ProfileError(f"unknown profile type {profile.kind!r}")
    transport = importlib.import_module(TRANSPORTS[profile.kind])
    return await transport.open_session(profile, prompter, cols, rows, scripted)


def open_log(path: Path, settings: TerminalSettings, header: str):
    """The session log: the folder belongs to the user (created if needed, permissions left alone)."""
    log = open_private(path, "wb" if settings.log_exists == "overwrite" else "ab", own_dir=False)
    if settings.log_header:
        log.write(f"=~=~=~= termdeck log {datetime.now():%Y-%m-%d %H:%M:%S} {header} =~=~=~=\r\n".encode())
    return log


async def open_session(profile: Profile, prompter: Prompter, cols: int = 80, rows: int = 24,
                       raw: bool = False) -> Session:
    """Connect to `profile` and return a session with its terminal settings applied (`raw`: logging only)."""
    profile.validate()
    t = profile.terminal
    # The log is opened first: the SSH packet log must see the key exchange and the login.
    log = open_log(log_path(t.log_file, profile), t, f"{profile.name} ({profile.summary()})") if t.log_file else None
    ssh_log = _ssh_logging(log) if log and t.log_mode == "ssh" and profile.kind == "ssh" else None
    try:
        inner = await open_raw_session(profile, prompter, cols, rows, scripted=raw)
    except BaseException:
        if ssh_log:
            _end_ssh_logging(ssh_log)
        if log:
            log.close()
        raise
    try:
        return TerminalSession(inner, t, log, raw=raw, ssh_log=ssh_log)
    except BaseException:
        await inner.close()
        raise
