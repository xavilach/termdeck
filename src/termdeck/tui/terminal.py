"""Terminal emulator widget: pyte keeps the screen, Textual draws it."""

import asyncio
import sys

import pyte
from pyte.graphics import FG_BG_256
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget

from ..controller import SessionController, State
from ..profiles import Profile, ProfileStore
from ..session import Session, describe_error
from ..themes import ANSI, palette
from ..vt import BellLimiter, clean_paste, clean_title

FEED_SLICE = 16384  # output parsed between two chances for the UI to run (pyte is slow on big floods)

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


def windows_clipboard(text: str) -> bool:
    """Put `text` on the Windows clipboard directly (the console host ignores OSC 52). False elsewhere."""
    if sys.platform != "win32":
        return False
    import ctypes

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    data = text.encode("utf-16-le") + b"\0\0"
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not handle:
            return False
        ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT; on success the clipboard owns it
            kernel32.GlobalFree(handle)
            return False
        return True
    finally:
        user32.CloseClipboard()


# pyte gives 256-colour indices 0-15 as its own fixed hex values: they are the theme's 16 colours.
_ANSI_HEX = dict(zip(FG_BG_256[:16], ANSI))


def _colour(colours: dict, name: str, bright: bool = False) -> str | None:
    """A pyte colour (a name, or hex for 256 and true colours) as hex from the profile's palette."""
    if name == "default":
        return None
    if len(name) == 6 and all(c in "0123456789abcdefABCDEF" for c in name):
        return colours.get(_ANSI_HEX.get(name.lower(), ""), f"#{name}")
    if name.startswith("bright"):  # before brown -> yellow: pyte calls bright yellow "brightbrown"
        name, bright = name[6:].lstrip("_"), True
    name = {"brown": "yellow"}.get(name, name)
    key = f"bright{name.capitalize()}" if bright else name
    return colours.get(key) or colours.get(name)


class _Screen(pyte.HistoryScreen):
    """pyte screen that sends terminal replies (cursor reports, device attributes) back,
    and reports the bell and the window title."""

    def __init__(self, columns, lines, history, reply, on_bell, on_title):
        super().__init__(columns, lines, history=history, ratio=0.5)
        self._reply, self._on_bell, self._on_title = reply, on_bell, on_title
        self.pushed = 0  # lines that went into the scrollback so far

    def index(self) -> None:
        top, bottom = self.margins or (0, self.lines - 1)
        if self.cursor.y == bottom:  # the top line goes into the history
            self.pushed += 1
        super().index()

    def write_process_input(self, data: str) -> None:
        self._reply(data.encode())

    def bell(self, *args) -> None:
        self._on_bell()

    def set_title(self, param: str) -> None:
        super().set_title(param)
        self._on_title(param)


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

    class CloseRequested(Message):
        """The session ended and the profile closes the tab then (Close tab on exit)."""

        def __init__(self, view: "TerminalView"):
            super().__init__()
            self.view = view

    class TitleChanged(Message):
        """The remote title changed, or the bell rang in a tab not shown."""

        def __init__(self, view: "TerminalView"):
            super().__init__()
            self.view = view

    def __init__(self, profile: Profile, prompter, store: ProfileStore | None = None, saved: bool = False, **kwargs):
        """`saved`: a profile of `store`, read again on each connect (a reconnect uses the saved options)."""
        super().__init__(**kwargs)
        self.prompter = prompter
        self.controller = SessionController(profile, prompter, store, saved, on_change=self._changed)
        t = profile.terminal
        self._apply_colours()
        self.remote_title = ""
        self.bell = False  # rang while the tab was not shown
        self._bells = BellLimiter()
        self._flash = False
        self._offset = 0  # lines scrolled back into the history (0: the live screen)
        self._selection: tuple[tuple[int, int], tuple[int, int]] | None = None  # (line, column) from, to
        self._found: tuple[str, int] | None = None  # the last search and the line it found
        self.last_find = ""
        self._vt = _Screen(80, 24, t.scrollback, self._send_nowait, self._ring, self._title)
        self._vt_stream = pyte.ByteStream(self._vt)
        self._conn_task: asyncio.Task | None = None
        # Keys and terminal replies go out through one queue, in the order they were made.
        self._writes: asyncio.Queue[bytes] = asyncio.Queue()
        self._writer: asyncio.Task | None = None

    # ---- connection ----

    @property
    def profile(self) -> Profile:
        return self.controller.profile

    @property
    def session(self) -> Session | None:
        return self.controller.session

    @property
    def status(self) -> str:
        """connecting, connected, failed or ended (controller.State)."""
        return str(self.controller.state)

    @property
    def live(self) -> bool:
        return self.controller.state in (State.CONNECTING, State.CONNECTED)

    def connect(self) -> None:
        if self._conn_task and not self._conn_task.done():
            return
        self._conn_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        self.remote_title, self.bell = "", False  # from an earlier connection
        c = self.controller
        cols, rows = max(self.size.width, 2), max(self.size.height, 2)
        if not await c.connect(cols, rows):
            self._feed(f"\r\n\x1b[31m{c.message}\x1b[0m\r\nPress Enter to reconnect.\r\n".encode())
            return
        self._writes = asyncio.Queue()  # nothing typed for an earlier connection goes to this one
        self._writer = asyncio.create_task(self._write_loop(c.session))
        try:
            while data := await c.read():
                for start in range(0, len(data), FEED_SLICE):
                    self._feed(data[start:start + FEED_SLICE])
                    await asyncio.sleep(0)  # a flood must not freeze the other tabs and the keyboard
        finally:
            self._writer.cancel()
            await asyncio.gather(self._writer, return_exceptions=True)
            await c.close()
            self._feed(f"\r\n\x1b[33m{c.message}. Press Enter to reconnect.\x1b[0m\r\n".encode())
            if c.closes_tab:
                self.post_message(self.CloseRequested(self))

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
        await self.controller.close()
        if self._conn_task:
            self._conn_task.cancel()
            await asyncio.gather(self._conn_task, return_exceptions=True)

    def _changed(self, controller: SessionController) -> None:
        if controller.state is State.CONNECTED:  # the saved profile may have new colours
            self._apply_colours()
        self.post_message(self.StatusChanged(self, self.status))

    def _apply_colours(self) -> None:
        t = self.profile.terminal
        self._colours = palette(t.theme, t.colours)
        self._default_style = Style(color=self._colours["foreground"], bgcolor=self._colours["background"])
        self._styles: dict[tuple, Style] = {}  # a Style per cell look, built once
        if self.is_mounted:
            self.refresh()

    def _feed(self, data: bytes) -> None:
        pushed = self._vt.pushed
        self._vt_stream.feed(data)
        if self.profile.terminal.scroll_on_output:
            self._offset = 0
        elif self._offset:  # keep the lines the user is reading in place
            self._offset = min(self._offset + self._vt.pushed - pushed, len(self._vt.history.top))
        self.refresh()

    def _ring(self) -> None:
        style = self.profile.terminal.bell
        if style == "none" or not self._bells.allow():
            return
        if style in ("sound", "both"):
            self.app.bell()
        if style in ("visual", "both"):
            self._flash = True
            self.refresh()
            self.set_timer(0.15, self._unflash)
        if not self.is_on_screen and not self.bell:
            self.bell = True
            self.post_message(self.TitleChanged(self))  # the tab shows the bell

    def _unflash(self) -> None:
        self._flash = False
        self.refresh()

    def _title(self, title: str) -> None:
        if (title := clean_title(title)) != self.remote_title:
            self.remote_title = title
            self.post_message(self.TitleChanged(self))

    @property
    def is_on_screen(self) -> bool:
        return self.is_mounted and self.display and self.parent is not None and self.parent.display

    async def send(self, data: bytes) -> None:
        self._send_nowait(data)

    def _send_nowait(self, data: bytes) -> None:
        if self.session:
            self._writes.put_nowait(data)

    async def send_break(self) -> tuple[bool, str]:
        return await self.controller.send_break()

    # ---- input ----

    async def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        if event.key == ESCAPE_KEY:
            self.post_message(self.EscapePressed())
            return
        if event.key in ("shift+pageup", "shift+pagedown"):
            self._page(up=event.key == "shift+pageup")
            self.refresh()
            return
        if not self.live:
            if event.key == "enter":
                self.connect()
            return
        if not self.controller.capabilities.typing:  # a forwards-only connection: nothing to type into
            return
        app_cursor = APP_CURSOR_MODE in self._vt.mode
        data = key_to_bytes(event.key, event.character, app_cursor)
        if data:
            self._selection = self._found = None
            if self.profile.terminal.scroll_on_key:
                self._scroll_to_bottom()
            await self.send(data)

    async def on_paste(self, event: events.Paste) -> None:
        event.stop()
        if not self.controller.capabilities.typing:
            return
        text = clean_paste(event.text, self.profile.terminal.paste_controls)
        data = text.replace("\r\n", "\r").replace("\n", "\r").encode()
        if BRACKETED_PASTE in self._vt.mode:
            data = b"\x1b[200~" + data + b"\x1b[201~"
        await self.send(data)

    # ---- selection (drag; copied on release, through the terminal: OSC 52) and search ----

    def _line_at(self, y: int) -> int:
        """The line under row `y` of the view, counted from the top of the history."""
        return len(self._vt.history.top) - self._offset + y

    def _lines(self) -> list:
        return [*self._vt.history.top, *(self._vt.buffer[y] for y in range(self._vt.lines))]

    def _text(self, row, start: int = 0, end: int | None = None) -> str:
        return "".join(row[x].data for x in range(start, min(end or self._vt.columns, self._vt.columns)))

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1:
            point = (self._line_at(event.y), event.x)
            self._selection = (point, point)
            self.capture_mouse()
            self.refresh()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._selection and self.app.mouse_captured is self:
            self._selection = (self._selection[0], (self._line_at(max(0, min(event.y, self.size.height - 1))),
                                                    max(0, event.x)))
            self.refresh()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self.app.mouse_captured is self:
            self.release_mouse()
        if self._selection and (text := self.selected_text()):
            self.app.copy_to_clipboard(text)  # OSC 52: the terminal puts it on the clipboard (conhost does not)
            windows_clipboard(text)
            self.notify(f"Copied {len(text)} characters", timeout=2)
        else:
            self._selection = None
        self.refresh()

    def selected_text(self) -> str:
        if not self._selection:
            return ""
        (l0, c0), (l1, c1) = sorted(self._selection)
        if (l0, c0) == (l1, c1):
            return ""
        lines = self._lines()
        out = []
        for line in range(max(l0, 0), min(l1, len(lines) - 1) + 1):
            text = self._text(lines[line], c0 if line == l0 else 0, c1 + 1 if line == l1 else None)
            out.append(text.rstrip())
        return "\n".join(out)

    def _selected(self, line: int) -> tuple[int, int] | None:
        """The columns selected on `line` (from, to excluded)."""
        if not self._selection:
            return None
        (l0, c0), (l1, c1) = sorted(self._selection)
        if not l0 <= line <= l1 or (l0, c0) == (l1, c1):
            return None
        return (c0 if line == l0 else 0, c1 + 1 if line == l1 else self._vt.columns)

    def find(self, text: str) -> bool:
        """Show the next line above that contains `text` (case ignored); again for the one above."""
        self.last_find = text
        lines, history = self._lines(), len(self._vt.history.top)
        start = len(lines) - 1
        if self._found and self._found[0].lower() == text.lower():
            start = self._found[1] - 1
        wanted = text.lower()
        for line in range(min(start, len(lines) - 1), -1, -1):
            if wanted in self._text(lines[line]).lower():
                self._found = (text, line)
                self._offset = max(0, history - line)  # the line at the top (or the live screen)
                cols = self._text(lines[line]).lower().index(wanted)
                self._selection = ((line, cols), (line, cols + len(text) - 1))
                self.refresh()
                return True
        self._found = None
        return False

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        self._page(up=True)
        self.refresh()

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        self._page(up=False)
        self.refresh()

    def _page(self, up: bool) -> None:
        step = max(1, self._vt.lines // 2)
        offset = min(self._offset + (step if up else -step), len(self._vt.history.top))
        # Paging down close to the live screen goes all the way (new output may have moved it a little).
        self._offset = 0 if offset < (0 if up else step // 2) else offset

    def _scroll_to_bottom(self) -> None:
        self._offset = 0

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
        if self._offset and y == screen.lines - 1:  # while reading the scrollback, say so
            note = f" Scrolled back {self._offset} lines: a key or Shift+PgDn returns ".ljust(width)[:width]
            return Strip([Segment(note, self._default_style + Style(reverse=True))], width)
        if self._offset:  # the history above the screen, then the screen's top lines
            top = screen.history.top
            index = y - self._offset
            row = top[len(top) + index] if index < 0 else screen.buffer[index]
        else:
            row = screen.buffer[y]
        selected = self._selected(self._line_at(y))
        cursor = screen.cursor
        show_cursor = self.has_focus and not cursor.hidden and cursor.y == y and not self._offset
        t = self.profile.terminal
        segments = []
        for x in range(min(screen.columns, width)):
            char = row[x]
            look = (char.fg, char.bg, char.bold, char.italics, char.underscore, char.strikethrough,
                    char.blink and t.blink_text, char.reverse, show_cursor and x == cursor.x,
                    self._flash != bool(selected and selected[0] <= x < selected[1]))
            if (style := self._styles.get(look)) is None:
                if len(self._styles) > 4096:  # true-colour gradients would grow it without end
                    self._styles.clear()
                style = self._styles[look] = self._style(*look)
            segments.append(Segment(char.data or " ", style))
        if width > screen.columns:
            segments.append(Segment(" " * (width - screen.columns), self._default_style))
        return Strip(Segment.simplify(segments), width)

    def _style(self, fg, bg, bold, italics, underscore, strike, blink, reverse, cursor, flash) -> Style:
        """`flash`: shown inverted (a visual bell, or selected text)."""
        t = self.profile.terminal
        bright = bold and t.bold_style in ("colour", "both")  # PuTTY: bold as a brighter colour, a bold font, or both
        fg, bg = _colour(self._colours, fg, bright), _colour(self._colours, bg)
        if reverse != flash:  # a visual bell inverts the screen for a moment
            fg, bg = bg or self._colours["background"], fg or self._colours["foreground"]
        style = self._default_style + Style(color=fg, bgcolor=bg, bold=bold and t.bold_style != "colour",
                                            italic=italics, underline=underscore, strike=strike, blink=blink)
        if cursor:  # a terminal cell cannot draw a bar: underline and bar both underline
            style += Style(reverse=True) if t.cursor_style == "block" else Style(underline=True)
        return style
