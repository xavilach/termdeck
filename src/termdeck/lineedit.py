"""Local line editing for devices without their own line editor.

The line is edited and echoed locally; only complete lines (and control
keys such as Ctrl+C) are sent. Redraws use relative cursor moves, so they
work after any prompt the device printed. Lines longer than the terminal
width are not handled specially.
"""

import codecs

HISTORY_SIZE = 200
MAX_ESCAPE = 4096  # longer escape sequences are garbage: dropped

# Key sequences as sent by xterm-compatible UIs (normal and application cursor mode).
LEFT = {"\x1b[D", "\x1bOD", "\x02"}
RIGHT = {"\x1b[C", "\x1bOC", "\x06"}
UP = {"\x1b[A", "\x1bOA", "\x10"}
DOWN = {"\x1b[B", "\x1bOB", "\x0e"}
HOME = {"\x1b[H", "\x1bOH", "\x1b[1~", "\x1b[7~", "\x01"}
END = {"\x1b[F", "\x1bOF", "\x1b[4~", "\x1b[8~", "\x05"}
DELETE = {"\x1b[3~"}
BACKSPACE = {"\x7f", "\x08"}
ENTER = {"\r", "\n"}
KILL_LINE, KILL_END, KILL_WORD = "\x15", "\x0b", "\x17"


def _left(n: int) -> str:
    return f"\x1b[{n}D" if n else ""


class LineEditor:
    def __init__(self, history_size: int = HISTORY_SIZE):
        self.line = ""
        self.cursor = 0
        self.history: list[str] = []
        self._history_size = history_size
        self._browse: int | None = None  # index into history while browsing
        self._draft = ""                 # line being typed before browsing started
        self._escape = ""                # incomplete escape sequence
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._after_cr = False
        # Set while the device shows a password prompt: no local echo, no history.
        self.secret = False

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        """Process key input. Returns (bytes to display locally, bytes to send)."""
        echo, send = [], []
        secret = self.secret  # Enter clears it, but this input was typed under it
        for ch in self._decoder.decode(data):
            if self._escape and ch < " " and not self._in_string() and ch != "\x1b":
                self._escape = ""  # a control key interrupts an unfinished sequence
            if self._escape or ch == "\x1b":
                self._escape += ch
                if len(self._escape) > MAX_ESCAPE:
                    self._escape = ""
                    continue
                if not self._escape_complete():
                    continue
                key, self._escape = self._escape, ""
            else:
                key = ch
            self._key(key, echo, send)
        if secret:
            echo = [e for e in echo if e == "\r\n"]
        return "".join(echo).encode(), "".join(send).encode()

    def _in_string(self) -> bool:
        """Inside OSC/DCS/APC/PM/SOS, which end with BEL or ESC \\ (terminal replies)."""
        return len(self._escape) > 1 and self._escape[1] in "]P_^X"

    def _escape_complete(self) -> bool:
        seq = self._escape
        if len(seq) == 1:
            return False
        if self._in_string():
            return seq.endswith(("\x07", "\x1b\\"))
        if seq[1] == "[":
            return len(seq) > 2 and ("@" <= seq[-1] <= "~")
        if seq[1] == "O":
            return len(seq) == 3
        return True  # Alt+key: ESC followed by one character

    def _key(self, key: str, echo: list, send: list) -> None:
        # CR LF from a paste counts as one Enter.
        if key == "\n" and self._after_cr:
            self._after_cr = False
            return
        self._after_cr = key == "\r"

        before = (self.line, self.cursor)
        if key in ENTER:
            echo.append("\r\n")
            send.append(self.line + "\r")
            if not self.secret:
                self._remember(self.line)
            self.line, self.cursor, self._browse = "", 0, None
            self.secret = False
            return
        if key in LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key in RIGHT:
            self.cursor = min(len(self.line), self.cursor + 1)
        elif key in HOME:
            self.cursor = 0
        elif key in END:
            self.cursor = len(self.line)
        elif key in BACKSPACE:
            if self.cursor:
                self.line = self.line[: self.cursor - 1] + self.line[self.cursor:]
                self.cursor -= 1
        elif key in DELETE:
            self.line = self.line[: self.cursor] + self.line[self.cursor + 1:]
        elif key == KILL_LINE:
            self.line, self.cursor = "", 0
        elif key == KILL_END:
            self.line = self.line[: self.cursor]
        elif key == KILL_WORD:
            start = self.line[: self.cursor].rstrip(" ").rfind(" ") + 1
            self.line, self.cursor = self.line[:start] + self.line[self.cursor:], start
        elif key in UP:
            self._history_step(-1)
        elif key in DOWN:
            self._history_step(1)
        elif len(key) == 1 and (key.isprintable() or key == "\t"):
            self.line = self.line[: self.cursor] + key + self.line[self.cursor:]
            self.cursor += 1
        elif len(key) == 1:
            # Other control keys (Ctrl+C, Ctrl+D, Ctrl+Z...) go to the device now.
            # Ctrl+C also drops the local line, as a shell would.
            send.append(key)
            if key == "\x03":
                echo.append("^C\r\n")
                self.line, self.cursor, self._browse = "", 0, None
            return
        else:
            return  # unhandled escape sequence (F-keys...) is dropped
        echo.append(self._redraw(*before))

    def _redraw(self, old_line: str, old_cursor: int) -> str:
        if old_line == self.line:
            delta = self.cursor - old_cursor
            return f"\x1b[{delta}C" if delta > 0 else _left(-delta)
        # Rewrite from the first changed character, clear leftovers, park the cursor.
        same = 0
        limit = min(len(old_line), len(self.line))
        while same < limit and old_line[same] == self.line[same]:
            same += 1
        move = same - old_cursor
        out = f"\x1b[{move}C" if move > 0 else _left(-move)
        out += self.line[same:]
        if len(self.line) < len(old_line):
            out += "\x1b[K"
        return out + _left(len(self.line) - self.cursor)

    def _remember(self, line: str) -> None:
        if line.strip() and (not self.history or self.history[-1] != line):
            self.history.append(line)
            del self.history[: -self._history_size]

    def _history_step(self, step: int) -> None:
        if not self.history:
            return
        if self._browse is None:
            if step > 0:
                return
            self._draft = self.line
            index = len(self.history) - 1
        else:
            index = self._browse + step
        if index < 0:
            return
        if index >= len(self.history):
            self._browse, self.line = None, self._draft
        else:
            self._browse, self.line = index, self.history[index]
        self.cursor = len(self.line)
