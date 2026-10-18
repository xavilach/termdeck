"""Terminal emulator widget: pyte keeps the screen, Textual draws it."""

import asyncio

import pyte
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from ..profiles import Profile
from ..session import Session, describe_error, open_session

# Default foreground/background per profile theme. Matches the web UI themes.
THEMES = {
    "dark": ("#e4e7ee", "#0f1218"),
    "light": ("#1b1f27", "#ffffff"),
    "solarized": ("#839496", "#002b36"),
    "dracula": ("#f8f8f2", "#282a36"),
}

ESCAPE_KEY = "ctrl+right_square_bracket"
# pyte stores private modes shifted by 5 bits.
APP_CURSOR_MODE = 1 << 5
BRACKETED_PASTE = 2004 << 5

KEYS = {
    "enter": b"\r", "tab": b"\t", "shift+tab": b"\x1b[Z", "escape": b"\x1b", "backspace": b"\x7f",
    "ctrl+h": b"\x08", "delete": b"\x1b[3~", "insert": b"\x1b[2~",
    "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
    "home": b"\x1b[H", "end": b"\x1b[F", "pageup": b"\x1b[5~", "pagedown": b"\x1b[6~",
    "f1": b"\x1bOP", "f2": b"\x1bOQ", "f3": b"\x1bOR", "f4": b"\x1bOS",
    "f5": b"\x1b[15~", "f6": b"\x1b[17~", "f7": b"\x1b[18~", "f8": b"\x1b[19~",
    "f9": b"\x1b[20~", "f10": b"\x1b[21~", "f11": b"\x1b[23~", "f12": b"\x1b[24~",
    "ctrl+space": b"\x00", "ctrl+@": b"\x00", "ctrl+backslash": b"\x1c",
    "ctrl+underscore": b"\x1f", "ctrl+circumflex_accent": b"\x1e",
}
APP_CURSOR_KEYS = {"up": b"\x1bOA", "down": b"\x1bOB", "right": b"\x1bOC", "left": b"\x1bOD"}
MODIFIED_ARROWS = {"up": "A", "down": "B", "right": "C", "left": "D", "home": "H", "end": "F"}
MODIFIER_CODES = {"shift": 2, "alt": 3, "shift+alt": 4, "ctrl": 5, "ctrl+shift": 6}


def key_to_bytes(key: str, character: str | None, app_cursor: bool = False) -> bytes | None:
    """Translate a Textual key event to what an xterm would send."""
    if app_cursor and key in APP_CURSOR_KEYS:
        return APP_CURSOR_KEYS[key]
    if key in KEYS:
        return KEYS[key]
    modifiers, _, base = key.rpartition("+")
    if base in MODIFIED_ARROWS and modifiers in MODIFIER_CODES:
        return f"\x1b[1;{MODIFIER_CODES[modifiers]}{MODIFIED_ARROWS[base]}".encode()
    if modifiers == "ctrl" and len(base) == 1 and base.isalpha():
        return bytes([ord(base.lower()) - 96])
    if modifiers == "alt" and character:
        return b"\x1b" + character.encode()
    if character and character.isprintable():
        return character.encode("utf-8", "surrogatepass")
    return None


def _color(name: str, bright: bool = False) -> str | None:
    if name == "default":
        return None
    if len(name) == 6 and all(c in "0123456789abcdefABCDEF" for c in name):
        return f"#{name}"
    name = {"brown": "yellow"}.get(name, name)
    if name.startswith("bright"):
        return f"bright_{name[6:]}"
    return f"bright_{name}" if bright else name


class _Screen(pyte.HistoryScreen):
    """pyte screen that sends terminal replies (cursor reports, device attributes) back."""

    def __init__(self, columns, lines, history, reply):
        super().__init__(columns, lines, history=history, ratio=0.5)
        self._reply = reply

    def write_process_input(self, data: str) -> None:
        self._reply(data.encode())


class TerminalView(Widget, can_focus=True):
    """One connection rendered as a terminal."""

    DEFAULT_CSS = """
    TerminalView { width: 1fr; height: 1fr; }
    """

    class StatusChanged(Message):
        def __init__(self, view: "TerminalView", status: str):
            super().__init__()
            self.view = view
            self.status = status

    class EscapePressed(Message):
        pass

    def __init__(self, profile: Profile, prompter, **kwargs):
        super().__init__(**kwargs)
        self.profile = profile
        self.prompter = prompter
        self.session: Session | None = None
        self.status = "connecting"
        fg, bg = THEMES.get(profile.terminal.theme, THEMES["dark"])
        self._default_style = Style(color=fg, bgcolor=bg)
        self._vt = _Screen(80, 24, profile.terminal.scrollback, self._send_nowait)
        self._vt_stream = pyte.ByteStream(self._vt)
        self._conn_task: asyncio.Task | None = None
        # Keys and terminal replies go out through one queue, in the order they were made.
        self._writes: asyncio.Queue[bytes] = asyncio.Queue()
        self._writer: asyncio.Task | None = None

    # ---- connection ----

    def connect(self) -> None:
        if self._conn_task and not self._conn_task.done():
            return
        self._conn_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        self._set_status("connecting")
        cols, rows = max(self.size.width, 2), max(self.size.height, 2)
        try:
            self.session = await open_session(self.profile, self.prompter, cols, rows)
        except Exception as exc:
            self._feed(f"\r\n\x1b[31m{describe_error(exc)}\x1b[0m\r\n".encode())
            self._feed(b"Press Enter to reconnect.\r\n")
            self._set_status("closed")
            return
        self._set_status("connected")
        self._writes = asyncio.Queue()  # nothing typed for an earlier connection goes to this one
        self._writer = asyncio.create_task(self._write_loop(self.session))
        reason = None
        try:
            while data := await self.session.read():
                self._feed(data)
            reason = self.session.end_reason
        finally:
            self._writer.cancel()
            await asyncio.gather(self._writer, return_exceptions=True)
            await self.session.close()
            self.session = None
            ended = f"Session ended: {reason}" if reason else "Session ended"
            self._feed(f"\r\n\x1b[33m{ended}. Press Enter to reconnect.\x1b[0m\r\n".encode())
            self._set_status("closed")

    async def _write_loop(self, session: Session) -> None:
        while True:
            data = await self._writes.get()
            try:
                await session.write(data)
            except Exception as exc:  # report it and keep typing possible; the output side ends the session
                self.show_notice(f"[termdeck] input not sent: {describe_error(exc)}")

    def show_notice(self, text: str) -> None:
        """A message from the connection (server banner, login instructions), in grey (pyte has no faint)."""
        self._feed(b"\r\n\x1b[90m" + text.replace("\n", "\r\n").encode() + b"\x1b[0m\r\n")

    async def disconnect(self) -> None:
        if self.session:
            await self.session.close()
        if self._conn_task:
            self._conn_task.cancel()
            await asyncio.gather(self._conn_task, return_exceptions=True)

    def _set_status(self, status: str) -> None:
        self.status = status
        self.post_message(self.StatusChanged(self, status))

    def _feed(self, data: bytes) -> None:
        self._scroll_to_bottom()
        self._vt_stream.feed(data)
        self.refresh()

    async def send(self, data: bytes) -> None:
        self._send_nowait(data)

    def _send_nowait(self, data: bytes) -> None:
        if self.session:
            self._writes.put_nowait(data)

    async def send_break(self) -> None:
        if self.session and self.session.can_break:
            await self.session.send_break()

    # ---- input ----

    async def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        if event.key == ESCAPE_KEY:
            self.post_message(self.EscapePressed())
            return
        if event.key in ("shift+pageup", "shift+pagedown"):
            (self._vt.prev_page if event.key == "shift+pageup" else self._vt.next_page)()
            self.refresh()
            return
        if self.status == "closed":
            if event.key == "enter":
                self.connect()
            return
        app_cursor = APP_CURSOR_MODE in self._vt.mode
        data = key_to_bytes(event.key, event.character, app_cursor)
        if data:
            self._scroll_to_bottom()
            await self.send(data)

    async def on_paste(self, event: events.Paste) -> None:
        event.stop()
        data = event.text.replace("\r\n", "\r").replace("\n", "\r").encode()
        if BRACKETED_PASTE in self._vt.mode:
            data = b"\x1b[200~" + data + b"\x1b[201~"
        await self.send(data)

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._vt.prev_page()
        self.refresh()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._vt.next_page()
        self.refresh()

    def _scroll_to_bottom(self) -> None:
        history = self._vt.history
        while history.position < history.size and history.bottom:
            self._vt.next_page()

    # ---- size and drawing ----

    def on_resize(self, event: events.Resize) -> None:
        cols, rows = max(event.size.width, 2), max(event.size.height, 2)
        self._vt.resize(rows, cols)
        if self.session:
            self.session.resize(cols, rows)
        self.refresh()

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    def render_line(self, y: int) -> Strip:
        screen = self._vt
        width = self.size.width
        if y >= screen.lines:
            return Strip([Segment(" " * width, self._default_style)], width)
        row = screen.buffer[y]
        cursor = screen.cursor
        show_cursor = self.has_focus and not cursor.hidden and cursor.y == y
        segments = []
        for x in range(min(screen.columns, width)):
            char = row[x]
            fg, bg = _color(char.fg, char.bold), _color(char.bg)
            if char.reverse:
                fg, bg = bg or self._default_style.bgcolor.name, fg or self._default_style.color.name
            style = self._default_style + Style(
                color=fg, bgcolor=bg, bold=char.bold, italic=char.italics,
                underline=char.underscore, strike=char.strikethrough, blink=char.blink,
            )
            if show_cursor and x == cursor.x:
                style += Style(reverse=True)
            segments.append(Segment(char.data or " ", style))
        if width > screen.columns:
            segments.append(Segment(" " * (width - screen.columns), self._default_style))
        return Strip(Segment.simplify(segments), width)
