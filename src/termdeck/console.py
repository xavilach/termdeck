"""The `termdeck connect` front end: a session in the current terminal, like ssh.

Interactive (`interact`): raw keys in, output straight out. Ctrl+] starts an
escape: then q (or .) quits, b sends a serial break, ] sends a literal Ctrl+].
Any other key cancels the escape.

Scripted (`pipe`): stdin, stdout and stderr are passed through unchanged, like
plink or ssh with a pipe, and the remote exit status becomes ours.
"""

import asyncio
import atexit
import codecs
import os
import signal
import sys
import threading

from .session import Session

ESCAPE = 0x1D  # Ctrl+]
# Ctrl+] as sent by terminals in kitty keyboard / modifyOtherKeys mode, which a
# remote program can switch on. Without these the escape key could be disabled.
ESCAPE_ALIASES = (b"\x1b[93;5u", b"\x1b[27;5;93~")
# Undo modes a remote program or the profile's start modes may have left on: kitty keys,
# modifyOtherKeys, mouse reporting, bracketed paste, alternate screen, cursor keys and
# keypad, origin mode and no-wrap; then show the cursor.
RESET_MODES = (b"\x1b[<u\x1b[>4;0m\x1b[?1000;1002;1003;1006;2004l\x1b[?1049l\x1b[?1l\x1b>\x1b[?6l\x1b[?7h"
               b"\x1b[?25h")
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

    def size(self) -> os.terminal_size:
        return os.get_terminal_size(sys.stdout.fileno())

    def start(self, loop, on_input, on_resize) -> None:
        def read():
            try:
                data = os.read(self.fd, 4096)
            except OSError:
                data = b""
            on_input(data)  # b"" (end of input, hangup) means quit

        loop.add_reader(self.fd, read)
        loop.add_signal_handler(signal.SIGWINCH, on_resize)

    def stop(self, loop) -> None:
        loop.remove_reader(self.fd)
        loop.remove_signal_handler(signal.SIGWINCH)

    def write(self, data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


class _WindowsConsole:
    """Console in VT mode: keys arrive as xterm sequences, output escapes are rendered."""

    ENABLE_PROCESSED_INPUT, ENABLE_LINE_INPUT, ENABLE_ECHO_INPUT = 0x1, 0x2, 0x4
    ENABLE_VIRTUAL_TERMINAL_INPUT = 0x200
    ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x4
    STD_INPUT_HANDLE, STD_OUTPUT_HANDLE = -10, -11

    def __enter__(self):
        import ctypes
        from ctypes import wintypes

        k32 = self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Declared, so 64-bit handles are not truncated to C int.
        k32.GetStdHandle.argtypes, k32.GetStdHandle.restype = [wintypes.DWORD], wintypes.HANDLE
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        for name in ("GetConsoleMode", "SetConsoleMode", "SetConsoleCP", "SetConsoleOutputCP"):
            getattr(k32, name).restype = wintypes.BOOL
        self.h_in = k32.GetStdHandle(wintypes.DWORD(self.STD_INPUT_HANDLE & 0xFFFFFFFF))
        self.h_out = k32.GetStdHandle(wintypes.DWORD(self.STD_OUTPUT_HANDLE & 0xFFFFFFFF))
        self._modes = []
        for handle in (self.h_in, self.h_out):
            mode = wintypes.DWORD()
            if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
                raise ValueError("connect needs a console window (not a redirected or emulated one)")
            self._modes.append(mode.value)
        self._cp = (k32.GetConsoleCP(), k32.GetConsoleOutputCP())
        in_mode = self._modes[0] & ~(self.ENABLE_PROCESSED_INPUT | self.ENABLE_LINE_INPUT | self.ENABLE_ECHO_INPUT)
        if not (k32.SetConsoleMode(self.h_in, in_mode | self.ENABLE_VIRTUAL_TERMINAL_INPUT)
                and k32.SetConsoleMode(self.h_out, self._modes[1] | self.ENABLE_VIRTUAL_TERMINAL_PROCESSING)):
            self._restore()
            raise ValueError("this console has no VT mode: connect needs Windows 10 1809 or later "
                             "(Windows Terminal works everywhere)")
        k32.SetConsoleCP(65001)
        k32.SetConsoleOutputCP(65001)
        # Restored even if the process is ended abruptly from here on, or by Ctrl+Break.
        atexit.register(self._restore)
        self._break = signal.signal(signal.SIGBREAK, signal.SIG_IGN)  # Ctrl+Break goes nowhere while connected
        self._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        return self

    def _restore(self) -> None:
        k32 = self.k32
        k32.SetConsoleMode(self.h_in, self._modes[0])
        k32.SetConsoleMode(self.h_out, self._modes[1])
        if hasattr(self, "_cp"):
            k32.SetConsoleCP(self._cp[0])
            k32.SetConsoleOutputCP(self._cp[1])

    def __exit__(self, *exc):
        self._restore()
        atexit.unregister(self._restore)
        signal.signal(signal.SIGBREAK, self._break)

    def size(self) -> os.terminal_size:
        # Not shutil.get_terminal_size: it prefers COLUMNS/LINES, which would hide resizes.
        return os.get_terminal_size(sys.stdout.fileno())

    def start(self, loop, on_input, on_resize) -> None:
        import msvcrt

        self._stopped = threading.Event()

        def deliver(data: bytes) -> None:
            try:
                loop.call_soon_threadsafe(on_input, data)
            except RuntimeError:
                pass  # the loop is gone: the session is over

        def read():
            # Wait with a timeout and read only when a key is ready (kbhit also drops
            # focus and mouse events), so the thread stops when the session ends
            # instead of swallowing the next key typed at the shell.
            try:
                while not self._stopped.is_set():
                    if self.k32.WaitForSingleObject(self.h_in, 100) == 0 and msvcrt.kbhit():
                        if not (data := os.read(sys.stdin.fileno(), 4096)):
                            break
                        deliver(data)
            except OSError:
                pass
            if not self._stopped.is_set():
                deliver(b"")

        async def watch_size():
            size = self.size()
            while True:
                await asyncio.sleep(0.5)
                try:
                    new = self.size()
                except OSError:  # console detached: keep the last size
                    continue
                if new != size:
                    size = new
                    on_resize()

        self._reader = threading.Thread(target=read, daemon=True)
        self._reader.start()
        self._watcher = loop.create_task(watch_size())

    def stop(self, loop) -> None:
        self._stopped.set()
        self._watcher.cancel()
        self._reader.join(0.5)

    def write(self, data: bytes) -> None:
        # Whole characters only: conhost shows a UTF-8 sequence split across two
        # writes as two replacement characters.
        text = self._utf8.decode(data)
        if text:
            sys.stdout.buffer.write(text.encode("utf-8"))
            sys.stdout.buffer.flush()


def _console():
    return _WindowsConsole() if sys.platform == "win32" else _PosixConsole()


def terminal_size() -> os.terminal_size:
    """The size of the terminal on stdout (not COLUMNS/LINES), 80x24 if there is none."""
    try:
        return os.get_terminal_size(sys.stdout.fileno())
    except (OSError, ValueError):
        return os.terminal_size((80, 24))


async def interact(session: Session, banner: str = "") -> None:
    """Connect the current terminal to `session` until it ends or the user quits.

    `banner` goes to stderr once raw mode is on, so keys typed after it are never lost.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("interactive connect needs a terminal; with pipes it runs in scripted mode")
    loop = asyncio.get_running_loop()
    escape = EscapeFilter()
    inputs: asyncio.Queue[bytes | str] = asyncio.Queue()

    def on_input(data: bytes) -> None:
        if not data:
            inputs.put_nowait("quit")
            return
        payload, commands = escape.feed(data)
        if payload:
            inputs.put_nowait(payload)
        for command in commands:
            inputs.put_nowait(command)

    console = _console()

    def on_resize() -> None:
        size = console.size()
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
            console.write(data)

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
            sys.stdout.buffer.write(RESET_MODES)
            sys.stdout.buffer.flush()


class _ConsoleOutput:
    """stdout when it is a Windows console in scripted mode: escape sequences rendered
    (VT processing on, best effort) and whole UTF-8 characters per write."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetStdHandle.argtypes, k32.GetStdHandle.restype = [wintypes.DWORD], wintypes.HANDLE
        k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle, mode = k32.GetStdHandle(wintypes.DWORD(0xFFFFFFF5)), wintypes.DWORD()  # STD_OUTPUT_HANDLE
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(handle, mode.value | _WindowsConsole.ENABLE_VIRTUAL_TERMINAL_PROCESSING)
            atexit.register(k32.SetConsoleMode, handle, mode.value)

    def write(self, data: bytes) -> None:
        if text := self._decoder.decode(data):
            sys.stdout.buffer.write(text.encode("utf-8"))

    def flush(self) -> None:
        sys.stdout.buffer.flush()


PIPE_BACKLOG = 4  # 64 KB chunks read ahead of the network
DRAIN = 0.5  # seconds of output still shown after stdin ends, for sessions that never end by themselves


async def pipe(session: Session) -> None:
    """Pass stdin to `session` and its output to stdout (and stderr), byte for byte.

    At the end of stdin the remote side gets an end of file. Sessions that have no
    such thing (serial ports) end DRAIN seconds later.
    """
    loop = asyncio.get_running_loop()
    inputs: asyncio.Queue[bytes] = asyncio.Queue()
    room = threading.Semaphore(PIPE_BACKLOG)  # stdin is read only as fast as the remote side takes it

    def read_stdin() -> None:
        try:
            while room.acquire() and (data := os.read(sys.stdin.fileno(), 65536)):
                loop.call_soon_threadsafe(inputs.put_nowait, data)
        except (OSError, RuntimeError):
            pass
        try:
            loop.call_soon_threadsafe(inputs.put_nowait, b"")
        except RuntimeError:
            pass

    async def forward_input() -> None:
        while data := await inputs.get():
            await session.write(data)  # waits while the network is not taking data
            room.release()
        if not await session.write_eof():
            await asyncio.sleep(DRAIN)
            await session.close()

    async def copy(read, stream) -> None:
        while data := await read():
            stream.write(data)
            stream.flush()

    threading.Thread(target=read_stdin, name="termdeck-stdin", daemon=True).start()
    feeder = asyncio.create_task(forward_input())
    stdout = _ConsoleOutput() if sys.platform == "win32" and sys.stdout.isatty() else sys.stdout.buffer
    outputs = [copy(session.read, stdout)]
    if read_stderr := getattr(session, "read_stderr", None):
        outputs.append(copy(read_stderr, sys.stderr.buffer))
    try:
        await asyncio.gather(*outputs)
    finally:
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
