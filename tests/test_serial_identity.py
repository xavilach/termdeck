from types import SimpleNamespace

import pytest

from termdeck import serialport
from termdeck.profiles import Profile, SerialSettings
from termdeck.serialport import find_device, port_identity, resolve_device, update_identity


def port(device, vid=0x0403, pid=0x6001, serial_number=None, location=None):
    return SimpleNamespace(device=device, vid=vid, pid=pid, serial_number=serial_number, location=location,
                           description="USB Serial")


FTDI_A = port("COM3", serial_number="A1", location="1-1.2")
FTDI_B = port("COM4", serial_number="B2", location="1-1.3")
CH340 = port("COM5", 0x1A86, 0x7523, location="1-4")
BUILTIN = port("COM1", None, None)


def test_identity_strings():
    assert port_identity(FTDI_A) == "0403:6001 serial=A1 location=1-1.2"
    assert port_identity(CH340) == "1a86:7523 location=1-4"
    assert port_identity(BUILTIN) == ""


@pytest.mark.parametrize("identity, ports, current, expected", [
    # Serial number wins, whatever the socket and port name.
    ("0403:6001 serial=A1 location=1-1.2", [port("COM9", serial_number="A1", location="2-1"), FTDI_B], "COM3", "COM9"),
    # Same model, other serial number: a different adapter, never picked.
    ("0403:6001 serial=A1 location=1-1.2", [port("COM9", serial_number="ZZ", location="1-1.2")], "COM3", None),
    # Serial recorded, the only port of the model has none: a different adapter.
    ("0403:6001 serial=A1", [port("COM9")], "COM3", None),
    # Clones sharing a serial number: keep the current port if it is one of them...
    ("10c4:ea60 serial=0001 location=1-2", [port("COM4", 0x10C4, 0xEA60, "0001", "1-1"),
                                            port("COM5", 0x10C4, 0xEA60, "0001", "1-2")], "COM5", "COM5"),
    # ...otherwise the recorded socket decides...
    ("10c4:ea60 serial=0001 location=1-2", [port("COM4", 0x10C4, 0xEA60, "0001", "1-1"),
                                            port("COM6", 0x10C4, 0xEA60, "0001", "1-2")], "COM9", "COM6"),
    # ...and with no way to tell them apart, nothing is picked.
    ("10c4:ea60 serial=0001 location=1-2", [port("COM4", 0x10C4, 0xEA60, "0001", "2-1"),
                                            port("COM6", 0x10C4, 0xEA60, "0001", "2-2")], "COM9", None),
    # No serial number: same model on the same socket.
    ("1a86:7523 location=1-4", [port("COM8", 0x1A86, 0x7523, location="1-4"),
                                 port("COM6", 0x1A86, 0x7523, location="1-5")], "COM3", "COM8"),
    # Moved to another socket, but it is the only one of its kind.
    ("1a86:7523 location=1-4", [port("COM8", 0x1A86, 0x7523, location="2-2"), FTDI_A], "COM3", "COM8"),
    # Two identical adapters, both moved: ambiguous, give up.
    ("1a86:7523 location=1-4", [port("COM8", 0x1A86, 0x7523, location="2-2"),
                                 port("COM6", 0x1A86, 0x7523, location="2-3")], "COM3", None),
    ("", [FTDI_A], "COM3", None),
])
def test_find_device(identity, ports, current, expected):
    assert find_device(identity, ports, current) == expected


def test_refuses_when_port_now_holds_another_adapter(monkeypatch):
    from termdeck.serialport import WrongDeviceError

    monkeypatch.setattr(serialport.list_ports, "comports", lambda: [port("COM3", serial_number="OTHER")])
    with pytest.raises(WrongDeviceError):
        resolve_device(_profile(usb_id="0403:6001 serial=A1"))
    monkeypatch.setattr(serialport.list_ports, "comports", lambda: [])
    assert resolve_device(_profile(usb_id="0403:6001 serial=A1")) == ("COM3", None)  # absent: normal open error


def _profile(device="COM3", usb_id="", follow=True):
    return Profile("board", "serial", serial=SerialSettings(device=device, usb_id=usb_id, follow_device=follow))


def test_update_and_resolve(monkeypatch):
    monkeypatch.setattr(serialport.list_ports, "comports", lambda: [FTDI_A, CH340])
    profile = _profile()
    update_identity(profile)
    assert profile.serial.usb_id == port_identity(FTDI_A)

    monkeypatch.setattr(serialport.list_ports, "comports", lambda: [port("COM7", serial_number="A1"), CH340])
    assert resolve_device(profile) == ("COM7", f"adapter {profile.serial.usb_id} found on COM7 (was COM3)")

    profile.serial.follow_device = False
    assert resolve_device(profile) == ("COM3", None)


def test_changing_device_drops_old_identity(monkeypatch):
    monkeypatch.setattr(serialport.list_ports, "comports", lambda: [])
    old = _profile(usb_id=port_identity(FTDI_A))
    new = _profile(device="COM12", usb_id=old.serial.usb_id)
    update_identity(new, previous=old)
    assert new.serial.usb_id == ""
    same = _profile(usb_id=old.serial.usb_id)
    update_identity(same, previous=old)  # unplugged but unchanged: keep it
    assert same.serial.usb_id == old.serial.usb_id


async def test_session_reports_moved_device(monkeypatch):
    from termdeck.session import open_session

    from .conftest import FakePrompter, read_until

    loop_port = SimpleNamespace(device="loop://", vid=0x0403, pid=0x6001, serial_number="A1", location=None,
                                description="")
    monkeypatch.setattr(serialport.list_ports, "comports", lambda: [loop_port])
    profile = _profile(device="COM3", usb_id="0403:6001 serial=A1")
    session = await open_session(profile, FakePrompter())
    try:
        assert b"found on loop:// (was COM3)" in await read_until(session, b"was COM3")
    finally:
        await session.close()


def test_identity_is_hidden_from_forms():
    from termdeck.profiles import describe_options

    keys = [o["key"] for o in describe_options()["serial"]]
    assert "follow_device" in keys and "usb_id" not in keys


@pytest.mark.parametrize("number", ["5&2A3B1C&0&1", "7&1&0&2"])
def test_windows_instance_ids_are_not_serial_numbers(number):
    anonymous = port("COM3", serial_number=number, location="1-1.2")
    assert serialport.serial_number(anonymous) is None
    assert port_identity(anonymous) == "0403:6001 location=1-1.2"


def test_real_serial_numbers_are_kept_and_empty_ones_are_none():
    assert serialport.serial_number(port("COM3", serial_number="A1")) == "A1"
    assert serialport.serial_number(port("COM3", serial_number="")) is None
    assert serialport.serial_number(port("COM3", serial_number=None)) is None


def test_adapter_with_a_windows_instance_id_is_found_again_on_another_socket():
    # Recorded without a serial number: the instance id (which changes with the socket) must not count.
    recorded = port_identity(port("COM3", serial_number="5&AAA&0&1", location="1-1"))
    moved = port("COM8", serial_number="5&BBB&0&2", location="1-2")
    assert find_device(recorded, [moved], "COM3") == "COM8"


def test_identity_stored_with_a_windows_instance_id_matches_the_model_on_another_port():
    stored = "0403:6001 serial=5&2B3C&0&2"
    other_port = port("COM9", serial_number="5&99FF&0&1", location="3-2")
    assert find_device(stored, [other_port, CH340], "COM3") == "COM9"
    # A real serial number on the same model is a different adapter.
    assert find_device(stored, [FTDI_A], "COM3") is None
