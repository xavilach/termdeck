---
type: subsystem
title: Serial transport and USB device identity
description: How termdeck opens serial ports with pyserial (any device or rfc2217/socket/loop/cp2110 URL), runs reader and writer threads with backpressure and write timeouts, sends breaks and keeps DTR/RTS stable, and how a profile records a USB adapter's identity to follow it when its COM port name changes without ever guessing between adapters.
tags: [serial, pyserial, usb, com-ports, windows, flow-control, device-identity]
verified:
  - by: openwiki/0.7.2
    at: 2026-10-18T13:56:19.871Z
sources:
  - id: openwiki-source-eb862138b8e98bab9e37074d
    resource: repo://src/termdeck/serialport.py
generated: { by: "claude-code", at: "2026-10-18T13:56:19.871Z" }
---

# Serial transport and USB device identity

`src/termdeck/serialport.py` is the transport for `kind == "serial"` profiles (imported lazily
through `TRANSPORTS`; it never loads asyncssh). Entry points follow the transport contract
([Sessions](../core/sessions.md)): `open_session` (serial ports ask nothing and have no size),
`describe_error` and `ERRORS = (serial.SerialException,)`.

## Opening a port

`SerialSession.open(profile)`:

1. `resolve_device(profile)` in a thread picks the port name (see USB identity below) and may
   produce a notice ("adapter … found on COM7 (was COM3)").
2. `serial.serial_for_url(device, …, do_not_open=True)` with the profile's speed, data bits,
   parity, stop bits and flow control (`rtscts`, `xonxoff`, `dsrdtr`), a 0.2 s read timeout and
   a 5 s write timeout. Devices may be names (`COM3`, `/dev/ttyUSB0`) or URLs; validation limits
   schemes to `rfc2217`, `socket`, `loop`, `cp2110` ([Option registry](../core/profiles-and-options.md)).
3. DTR and RTS are set **before** opening so the line state is applied at open (some boards
   reset when DTR changes). With RTS/CTS flow the driver owns RTS. DSR/DTR flow control exists
   only on Windows, where the driver owns DTR; elsewhere the profile's DTR applies and a notice
   says no flow control is used.
4. `port.open()` runs in a thread; if the open is cancelled, termdeck waits for the thread and
   closes the port so it is never left open.
5. On POSIX `HUPCL` is cleared so closing the port does not drop DTR and reset the board.

## `SerialSession`

- **Reading**: one daemon reader thread per session (Windows ports cannot be polled by the
  event loop) reads what is waiting and hands chunks to the loop with
  `call_soon_threadsafe`. At most `BACKLOG` (256) chunks run ahead of the UI (a
  `threading.Semaphore`); beyond that the reader waits and the port's buffer, then flow
  control, holds the data. A read failure after opening sets `end_reason` to "the device
  stopped responding (unplugged, or the driver reset)" and ends the session.
- **Writing**: a single-thread executor keeps keystrokes in order. A write that times out
  (flow control holding the line, or the device not reading) shows one notice and then drops
  typing for `WRITE_TIMEOUT` (5 s) per attempt so it cannot pile up; the session continues.
  Other write errors are left for the reader to report.
- `can_break = True`; `send_break()` runs `port.send_break` on the writer thread. Serial ports
  cannot signal end of input (`write_eof` is False), so piped `connect` closes them after a
  short drain ([CLI](../interfaces/cli-and-console.md)).
- `close()` cancels the pending read, joins the reader (up to 1 s), closes the port in a
  thread and shuts the writer down.

## USB identity: following the device

A USB adapter's COM number can change (COM3 becomes COM7). With `follow_device` on (default),
a profile records the adapter in the hidden option `serial.usb_id`:

- `port_identity(port)` = `"VID:PID [serial=…] [location=…]"`, empty for non-USB ports.
  Windows invents serial numbers containing `&` for adapters that have none, and they change
  with the USB socket, so such numbers are treated as absent.
- `update_identity(profile, previous)` runs when a profile is added or updated through the
  store ([Profile storage](../core/profile-store.md)): if the device is plugged in now, its
  identity is recorded; if the device name changed, the old identity is dropped first so the
  profile never follows an adapter the user moved away from.
- `find_device(identity, ports, current)` **never guesses** — sending to the wrong board is
  worse than not connecting. In order: the current port if it is still that adapter; the only
  port with this model and serial number; among several (clones sharing a serial, or adapters
  without one) the one on the recorded USB socket; the only port of this model for adapters
  without a serial number. Otherwise `None`.
- `resolve_device(profile)`: without following or a recorded identity, use the device as is.
  If the adapter is found, use its current name (with a notice if it moved). If not found but
  the profile's port name now belongs to a different device, raise `WrongDeviceError` asking
  to plug the adapter in or turn off "Follow the device". If not found and the name is free,
  try the recorded name.

`available_ports()` lists ports with device, description and USB identity, for the TUI and web
device pickers and `termdeck ports`.

## Error messages

`describe_error` turns pyserial messages into actions: a port in use or without permission
(only when the error happened while *opening*; once open, Windows reports an unplugged adapter
as "Access is denied"), no such port (suggesting `termdeck ports`), a device that stopped
responding (unplugged, stale Bluetooth port), or the `WrongDeviceError` text as is.

## Tests

`tests/test_serial_identity.py` (identity strings, `find_device` choices, Windows-invented
serial numbers, moved and wrong devices) and the serial parts of `tests/test_session.py`
(write order, close, cancelled open, write timeouts, flow control and DTR handling, errors),
using `loop://` and the `patch_port` fixture ([Test suite](../testing/test-suite.md)).
