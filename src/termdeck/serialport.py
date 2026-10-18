"""Serial console sessions (pyserial, so any device name or pyserial URL works)."""

import asyncio
import concurrent.futures
import re
import sys
import threading
import time

import serial
from serial.tools import list_ports

from .profiles import Profile
from .session import Session

READ_TIMEOUT = 0.2
ERRORS = (serial.SerialException,)  # device failures, as opposed to bugs (see session.is_expected)


def port_identity(port) -> str:
    """Stable description of a USB serial adapter: "VID:PID [serial=...] [location=...]".

    Empty for ports that are not USB (built-in UARTs, Bluetooth...).
    """
    if port.vid is None or port.pid is None:
        return ""
    parts = [f"{port.vid:04x}:{port.pid:04x}"]
    if serial_number(port):
        parts.append(f"serial={serial_number(port)}")
    if port.location:
        parts.append(f"location={port.location}")
    return " ".join(parts)


def serial_number(port) -> str | None:
    """The adapter's own serial number. Windows invents one (with "&") for adapters
    without, and it changes with the USB socket: treated as none."""
    number = port.serial_number or None
    return None if number and "&" in number else number


def _parse_identity(identity: str) -> dict:
    head, *rest = identity.split()
    fields = dict(item.split("=", 1) for item in rest if "=" in item)
    number = fields.get("serial")
    return {"usb": head.lower(), "serial": None if number and "&" in number else number,
            "location": fields.get("location")}


def _matches(port, want: dict) -> bool:
    """Same model, and the same serial number when one was recorded (absent on both sides otherwise)."""
    return (port_identity(port).split(" ")[0] == want["usb"]
            and serial_number(port) == want["serial"])


def find_device(identity: str, ports, current: str = "") -> str | None:
    """Current port name of the adapter described by `identity`, or None.

    Never guesses between candidates: sending to the wrong board is worse than
    not connecting. In order:
    1. the port at `current` if it is still that adapter;
    2. the only port with this model and serial number (any USB socket);
    3. among several such ports (clones sharing a serial number, or adapters
       without one), the one on the recorded USB socket;
    4. the only port of this model, for adapters without a serial number.
    """
    if not identity:
        return None
    want = _parse_identity(identity)
    candidates = [p for p in ports if _matches(p, want)]
    for p in candidates:
        if p.device == current:
            return p.device
    if want["serial"] and len(candidates) == 1:
        return candidates[0].device
    if want["location"]:
        on_socket = [p for p in candidates if p.location == want["location"]]
        if len(on_socket) == 1:
            return on_socket[0].device
    if not want["serial"] and len(candidates) == 1:
        return candidates[0].device
    return None


class WrongDeviceError(serial.SerialException):
    """The profile's port now belongs to another adapter than the recorded one."""


def update_identity(profile: Profile, previous: Profile | None = None) -> None:
    """Record the USB identity of the profile's device if it is plugged in now.

    A device name changed from `previous` drops the old identity, so the profile
    never keeps following an adapter the user moved away from.
    """
    s = profile.serial
    if profile.kind != "serial" or not s.follow_device:
        return
    if previous is not None and previous.serial.device != s.device:
        s.usb_id = ""
    for port in list_ports.comports():
        if port.device == s.device:
            s.usb_id = port_identity(port) or s.usb_id
            return


def resolve_device(profile: Profile) -> tuple[str, str | None]:
    """Port to open, and a notice when the adapter was found under another name.

    Raises WrongDeviceError when the recorded adapter is missing and its old
    port name is now used by a different device.
    """
    s = profile.serial
    if not (s.follow_device and s.usb_id):
        return s.device, None
    ports = list_ports.comports()
    found = find_device(s.usb_id, ports, s.device)
    if found:
        if found == s.device:
            return found, None
        return found, f"adapter {s.usb_id} found on {found} (was {s.device})"
    if any(p.device == s.device for p in ports):
        raise WrongDeviceError(
            f"{s.device} is now a different device; the adapter recorded for this profile ({s.usb_id}) "
            "is not connected or cannot be told apart from another one. "
            "Plug it in, or turn off 'Follow the device' to use whatever is on this port."
        )
    return s.device, None


def available_ports() -> list[dict]:
    """Serial ports present on this machine, for device pickers."""
    return [
        {
            "device": p.device,
            "description": p.description if p.description != "n/a" else "",
            "usb_id": port_identity(p),
        }
        for p in sorted(list_ports.comports(), key=lambda p: p.device)
    ]


def _keep_lines_on_close(port) -> None:
    """Clear HUPCL so closing the port does not drop DTR (and reset the board) on POSIX."""
    fd = getattr(port, "fd", None)
    if fd is None:
        return
    try:
        import termios
    except ImportError:
        return
    try:
        attrs = termios.tcgetattr(fd)
        attrs[2] &= ~termios.HUPCL
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        pass


WRITE_TIMEOUT = 5  # seconds a write may wait for flow control before it is reported
BACKLOG = 256  # chunks read ahead of the UI


class SerialSession(Session):
    """A serial port. A reader thread per session (Windows ports cannot be polled by
    the event loop) and one writer thread, so keystrokes go out in order."""

    can_break = True

    def __init__(self, port: serial.SerialBase, title: str, notice: str | None = None, flow: str = "none"):
        self._port = port
        self._closed = False
        self.title = title
        self._flow = flow
        self._stalled = 0.0  # when a write last timed out; 0 while writes go through
        self._loop = asyncio.get_running_loop()
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue()
        # Chunks read ahead of the UI. Beyond that the reader waits and the port's own
        # buffer (then flow control, if any) holds the data.
        self._room = threading.Semaphore(BACKLOG)
        if notice:
            self.notice(notice)
        self._writer = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix=f"serial-write-{title}")
        self._reader = threading.Thread(target=self._read_loop, name=f"serial-read-{title}", daemon=True)
        self._reader.start()

    @classmethod
    async def open(cls, profile: Profile) -> "SerialSession":
        s = profile.serial
        device, notice = await asyncio.to_thread(resolve_device, profile)
        port = serial.serial_for_url(
            device,
            baudrate=s.baudrate,
            bytesize=s.bytesize,
            parity=s.parity,
            stopbits=s.stopbits,
            rtscts=s.flow == "rtscts",
            xonxoff=s.flow == "xonxoff",
            dsrdtr=s.flow == "dsrdtr",
            timeout=READ_TIMEOUT,
            write_timeout=WRITE_TIMEOUT,
            do_not_open=True,
        )
        # Applied at open. The driver or the system may still pulse DTR/RTS while
        # opening (see the option help); on POSIX HUPCL is cleared below.
        # With DSR/DTR flow control the Windows driver owns DTR. Other systems have no
        # DSR/DTR handshake: the profile's DTR applies, and the user is told.
        dsrdtr = s.flow == "dsrdtr" and sys.platform == "win32"
        if not dsrdtr:
            port.dtr = s.dtr
        if s.flow == "dsrdtr" and not dsrdtr:
            missing = "DSR/DTR flow control is not available on this system: no flow control is used"
            notice = f"{notice}; {missing}" if notice else missing
        if s.flow != "rtscts":
            port.rts = s.rts
        opening = asyncio.ensure_future(asyncio.to_thread(port.open))
        try:
            await asyncio.shield(opening)
        except asyncio.CancelledError:
            # The thread cannot be stopped: wait for it, then release the port.
            await asyncio.gather(opening, return_exceptions=True)
            port.close()
            raise
        _keep_lines_on_close(port)
        return cls(port, profile.name, notice, s.flow)

    def _read_loop(self) -> None:
        """Runs in the reader thread until the port closes or fails."""
        while not self._closed:
            try:
                data = self._port.read(self._port.in_waiting or 1)
            except (serial.SerialException, OSError, TypeError) as exc:
                # TypeError: pyserial on POSIX when the port is closed during a read.
                # Open errors are explained by describe_error; once open, a failing read means the
                # adapter went away (Windows reports that as "Access is denied", not as in use).
                if not self._closed:
                    self.end_reason = f"{self.title}: the device stopped responding (unplugged, or the driver reset)"
                break
            if data:
                while not self._room.acquire(timeout=READ_TIMEOUT):
                    if self._closed:
                        return self._deliver(b"")
                self._deliver(data)
        self._deliver(b"")

    def _deliver(self, data: bytes) -> None:
        try:
            self._loop.call_soon_threadsafe(self._chunks.put_nowait, data)
        except RuntimeError:
            pass  # the event loop is gone (program exiting)

    async def read(self) -> bytes:
        data = await self._chunks.get()
        if not data:
            self._chunks.put_nowait(b"")  # keep reporting the end
        else:
            self._room.release()
        return data

    async def write(self, data: bytes) -> None:
        if self._closed:
            return
        if self._stalled and time.monotonic() - self._stalled < WRITE_TIMEOUT:
            return  # one try per timeout while the line is held, so typing cannot pile up
        try:
            await self._loop.run_in_executor(self._writer, self._port.write, data)
            self._stalled = 0.0
        except serial.SerialTimeoutException:
            first = not self._stalled
            self._stalled = time.monotonic()
            if first:
                held = "flow control is holding the line" if self._flow != "none" else "the device is not reading"
                self.notice(f"input not sent: {held} (typing is dropped until it accepts data again)")
        except (serial.SerialException, OSError):
            pass  # the reader reports the failure and ends the session

    async def send_break(self) -> None:
        await self._loop.run_in_executor(self._writer, self._port.send_break)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._port.cancel_read()
        except (AttributeError, NotImplementedError):
            pass
        await asyncio.to_thread(self._reader.join, READ_TIMEOUT * 5)
        await asyncio.to_thread(self._port.close)
        self._writer.shutdown(wait=False)


async def open_session(profile: Profile, prompter, cols: int, rows: int, scripted: bool = False) -> SerialSession:
    """The transport entry point. Serial ports have no size and ask nothing."""
    return await SerialSession.open(profile)


def describe_error(exc: BaseException) -> str | None:
    """User message for a serial port error, or None if `exc` is not one."""
    if not isinstance(exc, serial.SerialException):
        return None
    text = str(exc)
    if isinstance(exc, WrongDeviceError):
        return text
    # "could not open port 'COM3': ..." (Windows), "[Errno 2] could not open port /dev/x: ..." (POSIX).
    # Once open, Windows says "Access is denied" for an unplugged adapter: that is not "in use".
    opening = re.search(r"could not open port '?([^':]+)'?", text)
    port = opening[1].strip() if opening else text.split(":")[0]
    busy = ("PermissionError", "Access is denied", "Permission denied", "Device or resource busy",
            "Errno 13]", "Errno 16]")
    if opening and any(m in text for m in busy):
        return f"{port}: the port is in use by another program, or you lack permission"
    if any(m in text for m in ("cannot find the file", "No such file", "does not exist", "Errno 2]")):
        return f"{port}: no such port; check the name (termdeck ports lists them) or plug the adapter in"
    if any(m in text for m in ("not functioning", "semaphore timeout", "Input/output error", "Errno 5]")):
        return f"{port}: the device stopped responding (unplugged, or a stale Bluetooth port)"
    return f"Serial port error: {text}"
