import asyncio
import subprocess
import sys
import threading

import pytest
import serial

from termdeck.errors import ConnectError, ProfileError, ProfileNotFound
from termdeck.profiles import Profile, SerialSettings, TerminalSettings
from termdeck.serialport import SerialSession
from termdeck.session import OUTPUT_BACKLOG, Session, TerminalSession, describe_error, open_session, printable

from .conftest import FakePrompter, read_until


class FakeInner(Session):
    """A raw session that produces `chunks` (forever when `endless`) and records what is written to it."""

    def __init__(self, chunks=(), endless=False, end_reason=None):
        self.chunks = list(chunks)
        self.endless = endless
        self.pulled = 0
        self.writes: list[bytes] = []
        self.closed = False
        self.end_reason = end_reason

    async def read(self) -> bytes:
        await asyncio.sleep(0)  # a real transport yields to the loop
        if self.closed:
            return b""
        if self.endless:
            self.pulled += 1
            return b"data"
        self.pulled += 1
        return self.chunks.pop(0) if self.chunks else b""

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def close(self) -> None:
        self.closed = True


async def _drain(session) -> bytes:
    out = b""
    while chunk := await asyncio.wait_for(session.read(), 5):
        out += chunk
    return out


async def _settle(inner, minimum):
    """Wait until `inner` was read `minimum` times, then let the pump run until it blocks."""
    async def reached():
        while inner.pulled < minimum:
            await asyncio.sleep(0)

    await asyncio.wait_for(reached(), 5)
    for _ in range(200):
        await asyncio.sleep(0)


# ---- backpressure ----

async def test_unread_output_is_bounded_and_local_echo_still_appears():
    inner = FakeInner(endless=True)
    session = TerminalSession(inner, TerminalSettings(local_echo=True))
    assert await session.read() == b"data"  # starts the pump; nobody reads after this
    await _settle(inner, OUTPUT_BACKLOG)
    pulled = inner.pulled
    await _settle(inner, pulled)
    assert OUTPUT_BACKLOG <= inner.pulled <= OUTPUT_BACKLOG + 3  # the remote side is made to wait

    await asyncio.wait_for(session.write(b"x"), 2)  # typing never blocks on the full backlog
    assert inner.writes == [b"x"]
    seen = []
    for _ in range(OUTPUT_BACKLOG + 5):
        seen.append(await asyncio.wait_for(session.read(), 2))
        if b"x" in seen:
            break
    assert b"x" in seen
    inner.closed = True
    await _drain(session)  # reading makes room, so the pump sees the end
    await session.close()


async def test_close_does_not_hang_when_the_output_backlog_is_full():
    inner = FakeInner(endless=True)
    session = TerminalSession(inner, TerminalSettings())
    await session.read()
    await _settle(inner, OUTPUT_BACKLOG)
    await asyncio.wait_for(session.close(), 2)


# ---- end_reason ----

async def test_end_reason_of_the_inner_session_is_passed_on():
    session = TerminalSession(FakeInner([b"bye"], end_reason="Remote program exited with status 3"), TerminalSettings())
    assert await _drain(session) == b"bye"
    assert session.end_reason == "Remote program exited with status 3"
    await session.close()


async def test_unexpected_error_in_the_session_ends_it_with_a_reason():
    class Broken(FakeInner):
        async def read(self):
            raise serial.SerialException("device vanished")

    session = TerminalSession(Broken(), TerminalSettings())
    assert await _drain(session) == b""
    assert "device vanished" in session.end_reason
    await session.close()


async def test_serial_port_gone_mid_session_ends_with_the_error(patch_port):
    gone = threading.Event()

    def read(real, size=1):
        if gone.wait(0.02):
            raise serial.SerialException("device reports readiness to read but returned no data")
        return real(size)

    patch_port(read=read)
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    session = await open_session(profile, FakePrompter())
    try:
        await session.write(b"hi")
        assert await read_until(session, b"hi") == b"hi"
        assert session.end_reason is None
        gone.set()
        assert await asyncio.wait_for(session.read(), 5) == b""
        assert session.end_reason.startswith("loop: ")
        assert "stopped responding" in session.end_reason
    finally:
        await session.close()


# ---- error messages and exception classes ----

def test_describe_error_for_serial_does_not_load_asyncssh():
    code = (
        "import sys, serial\n"
        "import termdeck.session, termdeck.serialport\n"
        "from termdeck.session import describe_error\n"
        "text = describe_error(serial.SerialException('x'))\n"
        "assert text == 'Serial port error: x', text\n"
        "assert 'asyncssh' not in sys.modules\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_access_denied_on_a_serial_port_says_it_is_in_use():
    from termdeck import serialport  # noqa: F401  (describe_error consults the loaded transports)

    text = describe_error(serial.SerialException("could not open port 'COM3': PermissionError(13, 'Access is denied.')"))
    assert "in use by another program" in text
    assert text.startswith("COM3: ")


def test_profile_errors_are_catchable_as_builtin_errors():
    assert issubclass(ProfileError, ValueError)
    assert issubclass(ProfileNotFound, KeyError)
    error = ProfileNotFound("no profile named 'x'")
    assert str(error) == "no profile named 'x'"  # KeyError alone would quote it
    assert describe_error(error) == "no profile named 'x'"
    assert str(ProfileNotFound()) == ""
    with pytest.raises(ValueError):
        Profile("", "ssh").validate()


def test_connect_error_message_is_shown_as_is():
    assert describe_error(ConnectError("No username given, connection cancelled")) == "No username given, connection cancelled"


def test_store_lookup_failure_is_a_profile_not_found(tmp_path):
    from termdeck.profiles import ProfileStore

    with pytest.raises(ProfileNotFound) as exc:
        ProfileStore(tmp_path / "p.json").get("zz")
    assert str(exc.value) == "no profile named 'zz'"


def test_printable_strips_bidi_and_zero_width_characters():
    assert printable("a‮b​c⁦d") == "abcd"
    assert printable("ok\x1b[31m\x07") == "ok[31m"
    assert printable("é漢字🙂") == "é漢字🙂"
    assert describe_error(ConnectError("evil\u202etxt")) == "eviltxt"  # messages are cleaned too


# ---- terminal settings applied by TerminalSession ----

async def test_implicit_cr_and_lf_are_applied_to_output():
    cr = TerminalSession(FakeInner([b"a\nb"]), TerminalSettings(implicit_cr=True))
    assert await _drain(cr) == b"a\r\nb"
    lf = TerminalSession(FakeInner([b"a\rb"]), TerminalSettings(implicit_lf=True))
    assert await _drain(lf) == b"a\r\nb"
    for session in (cr, lf):
        await session.close()


async def test_answerback_is_sent_on_enq_in_the_profile_charset():
    inner = FakeInner([b"hi\x05there"])
    session = TerminalSession(inner, TerminalSettings(answerback="é-1", encoding="latin-1"))
    assert await _drain(session) == b"hi\x05there"
    assert inner.writes == ["é-1".encode("latin-1")]
    await session.close()


async def test_no_answerback_sends_nothing():
    inner = FakeInner([b"hi\x05there"])
    session = TerminalSession(inner, TerminalSettings())
    await _drain(session)
    assert inner.writes == []
    await session.close()


async def test_wide_and_combining_characters_split_across_chunks_pass_through():
    inner = FakeInner(["漢字🙂é".encode()[:4], "漢字🙂é".encode()[4:]])  # split inside a character
    session = TerminalSession(inner, TerminalSettings())
    assert (await _drain(session)).decode() == "漢字🙂é"
    await session.close()


# ---- serial transport ----

async def test_serial_writes_arrive_in_order():
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    session = await open_session(profile, FakePrompter())
    try:
        expected = b"".join(b"%03d," % i for i in range(200))
        for i in range(200):
            await session.write(b"%03d," % i)
        assert await read_until(session, b"199,") == expected
    finally:
        await session.close()


async def test_serial_close_returns_promptly():
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    session = await open_session(profile, FakePrompter())
    await asyncio.wait_for(session.close(), 2)
    assert await session.read() == b""
    await asyncio.wait_for(session.close(), 2)  # closing twice is harmless


async def test_cancelling_open_leaves_the_port_closed(patch_port):
    entered, release = threading.Event(), threading.Event()

    def slow_open(real):
        entered.set()
        release.wait(5)
        real()

    ports = patch_port(open=slow_open)
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    task = asyncio.create_task(SerialSession.open(profile))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    [(port, _)] = ports
    assert not port.is_open


async def test_write_timeout_is_reported_and_the_session_continues(patch_port, monkeypatch):
    from termdeck import serialport

    monkeypatch.setattr(serialport, "WRITE_TIMEOUT", 0.05)  # typing is dropped this long after a timeout
    calls = []

    def write(real, data):
        calls.append(data)
        if len(calls) == 1:
            raise serial.SerialTimeoutException("Write timeout")
        return real(data)

    patch_port(write=write)
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    session = await open_session(profile, FakePrompter())
    try:
        await session.write(b"lost")
        notice = await read_until(session, b"typing is dropped")
        assert b"input not sent" in notice
        assert session.end_reason is None

        async def retry():  # writes are refused until the stall period is over
            while True:
                await session.write(b"ok")
                try:
                    return await asyncio.wait_for(read_until(session, b"ok"), 0.05)
                except asyncio.TimeoutError:
                    pass

        assert b"ok" in await asyncio.wait_for(retry(), 5)
    finally:
        await session.close()


async def test_typing_is_dropped_while_a_write_is_stalled(patch_port):
    calls = []

    def write(real, data):
        calls.append(data)
        raise serial.SerialTimeoutException("Write timeout")

    patch_port(write=write)
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    session = await open_session(profile, FakePrompter())
    try:
        for key in (b"a", b"b", b"c"):
            await session.write(key)
        assert calls == [b"a"]  # no pile-up of blocked writes behind the first
        assert (await read_until(session, b"again)")).count(b"input not sent") == 1
    finally:
        await session.close()


@pytest.mark.parametrize("flow, rtscts, dsrdtr", [("dsrdtr", False, True), ("rtscts", True, False), ("none", False, False)])
async def test_flow_control_reaches_pyserial(patch_port, flow, rtscts, dsrdtr):
    ports = patch_port()
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://", flow=flow))
    profile.validate()  # "dsrdtr" is an accepted value
    session = await open_session(profile, FakePrompter())
    try:
        [(_, kwargs)] = ports
        assert (kwargs["rtscts"], kwargs["dsrdtr"]) == (rtscts, dsrdtr)
    finally:
        await session.close()


async def test_dsrdtr_leaves_dtr_to_the_windows_driver(patch_port, monkeypatch):
    ports = patch_port()
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://", flow="dsrdtr", dtr=False))
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", "win32")
        session = await open_session(profile, FakePrompter())
    try:
        [(port, _)] = ports
        assert port.dtr is True  # the profile's "assert DTR: off" is not forced on top of the handshake
        assert port.is_open
    finally:
        await session.close()


async def test_dsrdtr_elsewhere_applies_dtr_and_says_it_is_unavailable(patch_port, monkeypatch):
    ports = patch_port()
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://", flow="dsrdtr", dtr=False))
    with monkeypatch.context() as m:
        m.setattr(sys, "platform", "linux")
        session = await open_session(profile, FakePrompter())
    try:
        [(port, _)] = ports
        assert port.dtr is False
        assert b"DSR/DTR flow control is not available" in await read_until(session, b"used")
    finally:
        await session.close()


# ---- batch 3 ----

def test_printable_strips_invisible_format_and_separator_characters_but_keeps_layout():
    assert printable("a⁠b­c d\U000e0041e") == "abcde"
    assert printable("line1\nA\tB") == "line1\nA\tB"


def test_answerback_with_control_characters_is_invalid():
    for bad in ("ls\r", "a\x05b", "x\x00", "y\x7f"):
        profile = Profile("p", "serial", serial=SerialSettings(device="loop://"),
                          terminal=TerminalSettings(answerback=bad))
        with pytest.raises(ProfileError, match="control characters"):
            profile.validate()
    Profile("p", "serial", serial=SerialSettings(device="loop://"),
            terminal=TerminalSettings(answerback="termdeck é")).validate()


def test_is_expected_separates_connection_failures_from_bugs():
    import asyncssh

    from termdeck import serialport, ssh  # noqa: F401  (is_expected consults the loaded transports)
    from termdeck.session import is_expected

    for expected in (OSError("x"), ConnectionRefusedError(), ProfileError("x"), asyncssh.PermissionDenied("x"),
                     serial.SerialException("x"), TimeoutError()):
        assert is_expected(expected), expected
    for bug in (RuntimeError("x"), AttributeError("x"), KeyError("x")):
        assert not is_expected(bug), bug
    assert describe_error(RuntimeError("x")).startswith("Internal error")


def test_every_transport_provides_the_entry_points():
    import importlib

    from termdeck.profiles import TRANSPORTS

    for name in TRANSPORTS.values():
        module = importlib.import_module(name)
        assert callable(module.open_session) and callable(module.describe_error), name
        assert module.ERRORS and all(issubclass(e, Exception) for e in module.ERRORS), name


def test_opening_a_serial_session_does_not_load_asyncssh():
    code = (
        "import asyncio, sys\n"
        "from termdeck.profiles import Profile, SerialSettings\n"
        "from termdeck.session import open_session\n"
        "async def main():\n"
        "    s = await open_session(Profile('l', 'serial', serial=SerialSettings(device='loop://')), None)\n"
        "    await s.close()\n"
        "asyncio.run(main())\n"
        "assert 'asyncssh' not in sys.modules\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


def test_missing_serial_port_is_reported_as_no_such_port():
    from termdeck import serialport  # noqa: F401

    text = describe_error(serial.SerialException(
        "could not open port 'COM9': FileNotFoundError(2, 'The system cannot find the file specified.')"))
    assert text.startswith("COM9: ") and "no such port" in text


def test_read_time_access_denied_is_not_reported_as_in_use():
    from termdeck import serialport  # noqa: F401

    text = describe_error(serial.SerialException(
        "ClearCommError failed (PermissionError(13, 'Access is denied.', None, 5))"))
    assert "in use" not in text and "ClearCommError" in text


@pytest.mark.parametrize("chunks, expected", [
    ([b"a\nb\r\nc\r", b"\nd"], b"a\r\nb\r\nc\r\nd"),         # CR at the end of a chunk, LF at the start of the next
    ([b"a\r", b"\r\nb"], b"a\r\n\r\nb"),                      # CR, then CRLF: two line ends
    ([b"a\r\n", b"\n"], b"a\r\n\r\n"),
    ([b"x\r"], b"x\r\n"),                                     # a CR that ends the session still ends its line
])
async def test_implicit_cr_and_lf_together_never_double_a_newline(chunks, expected):
    session = TerminalSession(FakeInner(chunks), TerminalSettings(implicit_cr=True, implicit_lf=True))
    assert await _drain(session) == expected
    await session.close()
