"""Serial console sessions (pyserial, so any device name or pyserial URL works)."""

import asyncio

import serial
from serial.tools import list_ports

from .profiles import Profile
from .session import Session

READ_TIMEOUT = 0.2


def port_identity(port) -> str:
    """Stable description of a USB serial adapter: "VID:PID [serial=...] [location=...]".

    Empty for ports that are not USB (built-in UARTs, Bluetooth...).
    """
    if port.vid is None or port.pid is None:
        return ""
    parts = [f"{port.vid:04x}:{port.pid:04x}"]
    if port.serial_number:
        parts.append(f"serial={port.serial_number}")
    if port.location:
        parts.append(f"location={port.location}")
    return " ".join(parts)


def _parse_identity(identity: str) -> dict:
    head, *rest = identity.split()
    fields = dict(item.split("=", 1) for item in rest if "=" in item)
    return {"usb": head.lower(), "serial": fields.get("serial"), "location": fields.get("location")}


def _matches(port, want: dict) -> bool:
    """Same model, and the same serial number when one was recorded (absent on both sides otherwise)."""
    return (port_identity(port).split(" ")[0] == want["usb"]
            and (port.serial_number or None) == want["serial"])


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


class SerialSession(Session):
    can_break = True

    def __init__(self, port: serial.SerialBase, title: str, notice: str | None = None):
        self._port = port
        self._closed = False
        self._pending = f"\r\n[termdeck] {notice}\r\n".encode() if notice else b""
        self.title = title

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
            timeout=READ_TIMEOUT,
            do_not_open=True,
        )
        # Applied at open. Windows honours them from the start; on POSIX the tty
        # driver raises DTR during open() before pyserial can lower it.
        port.dtr = s.dtr
        if s.flow != "rtscts":
            port.rts = s.rts
        await asyncio.to_thread(port.open)
        _keep_lines_on_close(port)
        return cls(port, profile.name, notice)

    def _read_blocking(self) -> bytes:
        return self._port.read(self._port.in_waiting or 1)

    async def read(self) -> bytes:
        if self._pending:
            data, self._pending = self._pending, b""
            return data
        while not self._closed:
            try:
                data = await asyncio.to_thread(self._read_blocking)
            except (serial.SerialException, OSError, TypeError):
                # TypeError: pyserial on POSIX when the port is closed during a read.
                break
            if data:
                return data
        return b""

    async def write(self, data: bytes) -> None:
        if not self._closed:
            await asyncio.to_thread(self._port.write, data)

    async def send_break(self) -> None:
        await asyncio.to_thread(self._port.send_break)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._port.cancel_read()
        except (AttributeError, NotImplementedError):
            pass
        await asyncio.to_thread(self._port.close)
