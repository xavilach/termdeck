"""Terminal byte handling shared by every UI: key mappings, initial modes, the
remote-control filter (PuTTY's Features panel), paste cleaning and printable logs.

UIs send keys as an xterm does; the profile decides what the remote side receives.
"""

import re
import time

from .profiles import TerminalSettings

# Keys as the UIs send them (xterm) -> what each Home/End and function key style sends.
_HOME_END = {
    "standard": {b"\x1b[H": b"\x1b[1~", b"\x1b[F": b"\x1b[4~", b"\x1bOH": b"\x1b[1~", b"\x1bOF": b"\x1b[4~"},
    "rxvt": {b"\x1b[H": b"\x1b[7~", b"\x1b[F": b"\x1b[8~", b"\x1bOH": b"\x1b[7~", b"\x1bOF": b"\x1b[8~"},
    "xterm": {},
}
_XTERM_F = [b"\x1bOP", b"\x1bOQ", b"\x1bOR", b"\x1bOS", b"\x1b[15~", b"\x1b[17~", b"\x1b[18~", b"\x1b[19~",
            b"\x1b[20~", b"\x1b[21~", b"\x1b[23~", b"\x1b[24~"]
_FUNCTION_KEYS = {
    "xterm": {},
    "vt400": dict(zip(_XTERM_F[:4], [b"\x1b[11~", b"\x1b[12~", b"\x1b[13~", b"\x1b[14~"])),
    "linux": dict(zip(_XTERM_F[:5], [b"\x1b[[A", b"\x1b[[B", b"\x1b[[C", b"\x1b[[D", b"\x1b[[E"])),
    "vt100": dict(zip(_XTERM_F, [b"\x1bO" + bytes([c]) for c in b"PQRSTUVWXYZ["])),
    "sco": dict(zip(_XTERM_F, [b"\x1b[" + bytes([c]) for c in b"MNOPQRSTUVWX"])),
}
_KEY = re.compile(rb"\x1b(?:\[[0-9;]*[~A-Z]|O[A-Z])")


def remap_keys(data: bytes, settings: TerminalSettings) -> bytes:
    """Translate the UI's xterm key sequences to the profile's Home/End and function key styles."""
    table = {**_HOME_END[settings.home_end], **_FUNCTION_KEYS[settings.function_keys]}
    if not table or b"\x1b" not in data:
        return data
    return _KEY.sub(lambda m: table.get(m[0], m[0]), data)


def initial_modes(settings: TerminalSettings) -> bytes:
    """Escape sequences putting the UI's terminal in the profile's initial modes (shown locally, not sent)."""
    out = b""
    if not settings.auto_wrap:
        out += b"\x1b[?7l"
    if settings.origin_mode:
        out += b"\x1b[?6h"
    if settings.app_cursor_keys:
        out += b"\x1b[?1h"
    if settings.app_keypad:
        out += b"\x1b="
    return out


# Private modes (CSI ? n h/l) each remote-control switch covers.
_MODES = {
    "allow_app_keys": {1, 66},
    "allow_mouse": {9, 1000, 1001, 1002, 1003, 1005, 1006, 1015, 1016},
    "allow_alt_screen": {47, 1047, 1048, 1049},
    "allow_bracketed_paste": {2004},
}
HOLD_LIMIT = 4096  # an unfinished sequence longer than this is dropped, never let through
# Sequences as ECMA-48 defines them, with 7-bit (ESC [, ESC ]) and 8-bit (CSI U+009B, OSC U+009D,
# ST U+009C, as UTF-8) introducers: xterm.js and pyte accept both.
_CSI = rb"(?:\x1b\[|\xc2\x9b)([0-?]*)([ -/]*)([@-~])"
_OSC = rb"(?:\x1b\]|\xc2\x9d)([0-9]*)(?:;[^\x07\x1b\xc2]*|(?:\xc2(?!\x9c)[\x80-\xbf])*)*(?:\x07|\x1b\\|\xc2\x9c)"
_SEQUENCE = re.compile(rb"%s|%s|\x1b[()*+][ -~]|[\x0e\x0f]|\x1b[=>]" % (_CSI, _OSC))
_START = re.compile(rb"\x1b|\xc2[\x9b\x9d]")


class RemoteFilter:
    """Drops what the profile does not let the remote side do (PuTTY's Features panel).

    A sequence cut at the end of a chunk is held, from its start, until the rest arrives;
    one that never ends within HOLD_LIMIT bytes is dropped. `flush` releases what is held
    (output went quiet, or the session ended)."""

    def __init__(self, settings: TerminalSettings):
        self._blocked = set().union(*(modes for key, modes in _MODES.items() if not getattr(settings, key)))
        self._s = settings
        self.active = bool(self._blocked) or not all(
            (settings.allow_resize, settings.allow_title, settings.allow_clear_scrollback, settings.allow_charset,
             settings.allow_app_keys))
        self._tail = b""

    @property
    def holding(self) -> bool:
        return bool(self._tail)

    def feed(self, data: bytes, final: bool = False) -> bytes:
        if not self.active:
            return data
        data, self._tail = self._tail + data, b""
        if not final:
            data, self._tail = self._split(data)
        return _SEQUENCE.sub(self._keep, data)

    def flush(self) -> bytes:
        """What is held, filtered; an unfinished sequence itself is dropped."""
        data, self._tail = self._tail, b""
        if not data:
            return b""
        if _START.match(data) and not _SEQUENCE.match(data):
            return b""  # never finished: not ours to show
        return _SEQUENCE.sub(self._keep, data)

    def _split(self, data: bytes) -> tuple[bytes, bytes]:
        """(what can be filtered now, the unfinished sequence at the end to keep for later)."""
        pos = 0
        while (m := _START.search(data, pos)) is not None:
            if found := _SEQUENCE.match(data, m.start()):
                pos = found.end()
                continue
            if _unfinished(data[m.start():]):
                tail = data[m.start():]
                return data[:m.start()], (tail if len(tail) <= HOLD_LIMIT else b"")
            pos = m.start() + 1  # not a sequence we filter: pass it on
        return data, b""

    def _keep(self, m: re.Match) -> bytes:
        s, text = self._s, m[0]
        if m[3] is not None:  # CSI
            params, intermediates, final = m[1], m[2], m[3]
            if len(params) > 64:
                return b""  # absurd parameters: nothing a terminal needs
            if params.startswith(b"?") and final in b"hl" and not intermediates:
                modes = [p for p in params[1:].split(b";") if p and int(p.split(b":")[0] or 0) not in self._blocked]
                return b"\x1b[?" + b";".join(modes) + final if modes else b""
            if final == b"t" and not intermediates:
                return text if s.allow_resize else b""
            if params == b"3" and final == b"J":
                return text if s.allow_clear_scrollback else b""
            return text
        if m[4] is not None:  # OSC
            return text if s.allow_title or (m[4] or b"0").lstrip(b"0") not in (b"", b"1", b"2") else b""
        if text in (b"\x1b=", b"\x1b>"):
            return text if s.allow_app_keys else b""
        return text if s.allow_charset else b""


def _unfinished(tail: bytes) -> bool:
    """Whether `tail` (from an ESC or C1) could still become a sequence we filter."""
    if tail in (b"\x1b", b"\xc2"):
        return True
    if re.fullmatch(rb"(?:\x1b\[|\xc2\x9b)[0-?]*[ -/]*", tail):
        return True
    if re.match(rb"(?:\x1b\]|\xc2\x9d)", tail):
        return not re.search(rb"\x07|\x1b\\|\xc2\x9c", tail)
    return tail[:1] == b"\x1b" and len(tail) == 2 and tail[1:2] in b"()*+"


class PrinterCapture:
    """Remote-controlled printing (ESC [5i ... ESC [4i): the text goes to `write`, not the screen."""

    START, STOP = b"\x1b[5i", b"\x1b[4i"

    def __init__(self, write):
        self._write = write
        self._printing = False
        self._tail = b""

    def feed(self, data: bytes) -> bytes:
        data, self._tail, shown = self._tail + data, b"", b""
        while data:
            marker = self.STOP if self._printing else self.START
            at = data.find(marker)
            if at < 0:
                # Keep what could be the start of a marker split across chunks.
                keep = next((n for n in range(len(marker) - 1, 0, -1) if data.endswith(marker[:n])), 0)
                part, self._tail = (data[:-keep], data[-keep:]) if keep else (data, b"")
                if self._printing:
                    self._write(part)
                else:
                    shown += part
                break
            if self._printing:
                self._write(data[:at])
            else:
                shown += data[:at]
            self._printing, data = not self._printing, data[at + len(marker):]
        return shown


class BellLimiter:
    """Like PuTTY's bell overload: at most one bell per BELL_GAP, and silence for BELL_QUIET
    after a burst (garbage at the wrong speed, a binary file shown by mistake)."""

    BELL_GAP, BURST, BURST_TIME, BELL_QUIET = 0.25, 5, 2.0, 5.0

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._last = -1e9
        self._recent: list[float] = []
        self._quiet_until = 0.0

    def allow(self) -> bool:
        now = self._clock()
        self._recent = [t for t in self._recent if now - t < self.BURST_TIME] + [now]
        if len(self._recent) > self.BURST:
            self._quiet_until = now + self.BELL_QUIET
        if now < self._quiet_until or now - self._last < self.BELL_GAP:
            return False
        self._last = now
        return True


TITLE_LENGTH = 60
_TITLE_DROP = re.compile("[\x00-\x1f\x7f-\x9f\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def clean_title(title: str) -> str:
    """A remote window title fit for a tab: one line, no control or direction characters, short."""
    title = " ".join(_TITLE_DROP.sub(" ", title).split())
    return title if len(title) <= TITLE_LENGTH else title[:TITLE_LENGTH - 1] + "…"


_CONTROLS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def clean_paste(text: str, allow_controls: bool = False) -> str:
    """Pasted text without control characters (unless allowed): an ESC in a paste could end
    bracketed paste early and run commands. Tabs and line ends stay."""
    return text if allow_controls else _CONTROLS.sub("", text)


_ESCAPES = re.compile(rb"%s|%s|\x1b[()*+].|\x1b[^\[\]]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]" % (_CSI, _OSC))


class PrintableLog:
    """Output for a "printable text" log: escape sequences and control characters removed,
    a sequence cut between two chunks kept for the next, a lone CR (a redrawn line) as a new line."""

    def __init__(self):
        self._tail = b""

    def feed(self, data: bytes) -> bytes:
        data, self._tail = self._tail + data, b""
        if (cut := data.rfind(b"\x1b")) >= 0 and _unfinished(data[cut:]) and len(data) - cut < HOLD_LIMIT:
            data, self._tail = data[:cut], data[cut:]
        data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        return _ESCAPES.sub(b"", data)
