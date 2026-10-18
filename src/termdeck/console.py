"""Run a session in the current terminal, like ssh: raw keys in, output straight out.

Ctrl+] starts an escape: then q (or .) quits, b sends a serial break,
] sends a literal Ctrl+]. Any other key cancels the escape.
"""

import asyncio
import os
import shutil
import sys

from .session import Session

ESCAPE = 0x1D  # Ctrl+]
# Ctrl+] as sent by terminals in kitty keyboard / modifyOtherKeys mode, which a
# remote program can switch on. Without these the escape key could be disabled.
ESCAPE_ALIASES = (b"\x1b[93;5u", b"\x1b[27;5;93~")
# Undo modes a remote program may have left on: kitty keys, modifyOtherKeys,
# mouse reporting, bracketed paste, alternate screen; then show the cursor.
RESET_MODES = b"\x1b[<u\x1b[>4;0m\x1b[?1000;1002;1003;1006;2004l\x1b[?1049l\x1b[?25h"
ESCAPE_HELP = "Ctrl+] then q: quit, b: break, ]: send Ctrl+]"


class EscapeFilter:
    """Splits keyboard input into bytes for the session and escape commands."""

    def __init__(self):
        self._pending = False

    def feed(self, data: bytes) -> tuple[bytes, list[str]]:
        for alias in ESCAPE_ALIASES:
            data = data.replace(alias, bytes([ESCAPE]))
        out, commands = bytearray(), []
        for byte in data:
            if self._pending:
                self._pending = False
                if byte in b"q.":
                    commands.append("quit")
                elif byte in b"bB":
                    commands.append("break")
                elif byte in (ESCAPE, ord("]")):
                    out.append(ESCAPE)
            elif byte == ESCAPE:
                self._pending = True
            else:
                out.append(byte)
        return bytes(out), commands


class _PosixConsole:
    def __init__(self):
        self.fd = sys.stdin.fileno()

    def __enter__(self):
        import termios
        import tty

        self._saved = termios.tcgetattr(self.fd)
        tty.setraw(self.fd, termios.TCSADRAIN)  # keep typeahead (TCSAFLUSH would drop it)
        return self

    def __exit__(self, *exc):
        import termios

        termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)

    def start(self, loop, on_input, on_resize) -> None:
        import signal

        def read():
            try:
                data = os.read(self.fd, 4096)
            except OSError:
                data = b""
            on_input(data)  # b"" (end of input, hangup) means quit

        loop.add_reader(self.fd, read)
        loop.add_signal_handler(signal.SIGWINCH, on_resize)

    def stop(self, loop) -> None:
        import signal

        loop.remove_reader(self.fd)
        loop.remove_signal_handler(signal.SIGWINCH)


class _WindowsConsole:
    """Console in VT mode: keys arrive as xterm sequences, output escapes are rendered."""

    ENABLE_PROCESSED_INPUT, ENABLE_LINE_INPUT, ENABLE_ECHO_INPUT = 0x1, 0x2, 0x4
    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x200
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x4

    def __enter__(self):
        import ctypes

        self.k32 = ctypes.windll.kernel32
        self.h_in, self.h_out = self.k32.GetStdHandle(-10), self.k32.GetStdHandle(-11)
        self._modes = []
        for handle in (self.h_in, self.h_out):
            mode = ctypes.c_uint32()
            self.k32.GetConsoleMode(handle, ctypes.byref(mode))
            self._modes.append(mode.value)
        self._cp = (self.k32.GetConsoleCP(), self.k32.GetConsoleOutputCP())
        in_mode = self._modes[0] & ~(self.ENABLE_PROCESSED_INPUT | self.ENABLE_LINE_INPUT | self.ENABLE_ECHO_INPUT)
        self.k32.SetConsoleMode(self.h_in, in_mode | self.ENABLE_VIRTUAL_TERMINAL_INPUT)
        self.k32.SetConsoleMode(self.h_out, self._modes[1] | self.ENABLE_VIRTUAL_TERMINAL_PROCESSING)
        self.k32.SetConsoleCP(65001)
        self.k32.SetConsoleOutputCP(65001)
        return self

    def __exit__(self, *exc):
        self.k32.SetConsoleMode(self.h_in, self._modes[0])
        self.k32.SetConsoleMode(self.h_out, self._modes[1])
        self.k32.SetConsoleCP(self._cp[0])
        self.k32.SetConsoleOutputCP(self._cp[1])

    def start(self, loop, on_input, on_resize) -> None:
        import threading

        def read():
            try:
                while data := os.read(sys.stdin.fileno(), 4096):
                    loop.call_soon_threadsafe(on_input, data)
            except OSError:
                pass
            loop.call_soon_threadsafe(on_input, b"")

        async def watch_size():
            size = shutil.get_terminal_size()
            while True:
                await asyncio.sleep(0.5)
                if (new := shutil.get_terminal_size()) != size:
                    size = new
                    on_resize()

        threading.Thread(target=read, daemon=True).start()
        self._watcher = loop.create_task(watch_size())

    def stop(self, loop) -> None:
        self._watcher.cancel()


def _console():
    return _WindowsConsole() if sys.platform == "win32" else _PosixConsole()


async def interact(session: Session, banner: str = "") -> None:
    """Connect the current terminal to `session` until it ends or the user quits.

    `banner` goes to stderr once raw mode is on, so keys typed after it are never lost.
    """
    if not sys.stdin.isatty():
        raise ValueError("connect needs an interactive terminal")
    loop = asyncio.get_running_loop()
    escape = EscapeFilter()
    inputs: asyncio.Queue[bytes | str] = asyncio.Queue()
    out = sys.stdout.buffer

    def on_input(data: bytes) -> None:
        if not data:
            inputs.put_nowait("quit")
            return
        payload, commands = escape.feed(data)
        if payload:
            inputs.put_nowait(payload)
        for command in commands:
            inputs.put_nowait(command)

    def on_resize() -> None:
        size = shutil.get_terminal_size()
        session.resize(size.columns, size.lines)

    async def forward_input() -> None:
        # One writer keeps keystrokes in order, even for sessions that write from a thread.
        while True:
            item = await inputs.get()
            if item == "quit":
                return
            if item == "break":
                if session.can_break:
                    await session.send_break()
            else:
                await session.write(item)

    async def forward_output() -> None:
        while data := await session.read():
            out.write(data)
            out.flush()

    console = _console()
    with console:
        console.start(loop, on_input, on_resize)
        if banner:
            print(f"{banner}\r", file=sys.stderr, flush=True)
        try:
            tasks = {asyncio.create_task(forward_input()), asyncio.create_task(forward_output())}
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            console.stop(loop)
            out.write(RESET_MODES)
            out.flush()
