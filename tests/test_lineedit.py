import pytest

from termdeck.lineedit import LineEditor
from termdeck.profiles import Profile, SerialSettings
from termdeck.session import open_session

from .conftest import FakePrompter, read_until

LEFT, RIGHT, UP, DOWN, HOME, END, DEL = b"\x1b[D", b"\x1b[C", b"\x1b[A", b"\x1b[B", b"\x1b[H", b"\x1b[F", b"\x1b[3~"


def typed(*keys: bytes) -> tuple[LineEditor, bytes]:
    editor = LineEditor()
    sent = b""
    for key in keys:
        sent += editor.feed(key)[1]
    return editor, sent


def test_nothing_sent_until_enter():
    editor, sent = typed(b"show ver")
    assert sent == b"" and editor.line == "show ver"
    assert typed(b"show ver", b"\r")[1] == b"show ver\r"


def test_cursor_editing():
    # "helo" -> left -> insert "l" -> home -> delete -> end -> backspace
    editor, sent = typed(b"helo", LEFT, b"l", HOME, DEL, END, b"\x7f", b"\r")
    assert sent == b"ell\r"


def test_kill_keys():
    assert typed(b"abc def  ", b"\x17", b"\r")[1] == b"abc \r"   # Ctrl+W
    assert typed(b"abc", b"\x15", b"x\r")[1] == b"x\r"            # Ctrl+U
    assert typed(b"abcdef", LEFT, LEFT, b"\x0b", b"\r")[1] == b"abcd\r"  # Ctrl+K


def test_history_browse_and_draft_restore():
    editor, _ = typed(b"one\r", b"two\r", b"two\r", b"   \r")
    assert editor.history == ["one", "two"]  # no duplicates, no blank lines
    editor.feed(b"dra")
    editor.feed(UP)
    assert editor.line == "two"
    editor.feed(UP)
    editor.feed(UP)  # stays on the oldest
    assert editor.line == "one"
    editor.feed(DOWN)
    editor.feed(DOWN)
    assert editor.line == "dra"  # back to what was being typed
    assert editor.feed(UP + b"\r")[1] == b"two\r"


def test_control_keys_pass_through_and_ctrl_c_clears():
    editor = LineEditor()
    editor.feed(b"partial")
    echo, sent = editor.feed(b"\x03")
    assert sent == b"\x03" and editor.line == "" and echo == b"^C\r\n"
    assert editor.feed(b"\x04")[1] == b"\x04"


def test_split_escape_sequence_and_utf8():
    editor = LineEditor()
    editor.feed(b"ab\x1b")
    editor.feed(b"[D")  # Left, split across reads
    editor.feed("é".encode()[:1])
    editor.feed("é".encode()[1:] + b"\r")
    assert editor.history == ["aéb"]


def test_paste_with_crlf_sends_each_line_once():
    assert typed(b"a\r\nb\r\n")[1] == b"a\rb\r"


def test_echo_redraws_only_what_changed():
    editor = LineEditor()
    assert editor.feed(b"ab")[0] == b"ab"  # plain typing echoes just the characters
    assert editor.feed(LEFT)[0] == b"\x1b[1D"
    assert editor.feed(b"X")[0] == b"Xb\x1b[1D"  # insert: rewrite the tail, step back
    assert editor.feed(HOME)[0] == b"\x1b[2D"
    assert editor.feed(DEL)[0] == b"Xb\x1b[K\x1b[2D"  # "aXb" -> "Xb": shorter, so clear
    editor.feed(b"\x15")
    assert editor.line == ""


async def test_session_line_mode_with_crlf_enter():
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    profile.terminal.line_editing = True
    profile.terminal.enter_sends = "crlf"
    session = await open_session(profile, FakePrompter())
    try:
        await session.write(b"AT+X")
        await session.write(b"\x7fY\r")
        # Local echo of the edits, then the device loops back the whole line.
        out = await read_until(session, b"AT+Y\r\n")
        assert out.endswith(b"\r\nAT+Y\r\n")
    finally:
        await session.close()


def test_osc_reply_is_dropped_and_control_key_interrupts_escape():
    editor = LineEditor()
    echo, sent = editor.feed(b"\x1b]11;rgb:0000/0000/0000\x07ab")
    assert sent == b"" and editor.line == "ab"
    editor = LineEditor()
    assert editor.feed(b"\x1b[\rls\r")[1] == b"\r" + b"ls\r"


def test_secret_line_is_not_echoed_or_remembered():
    editor = LineEditor()
    editor.secret = True
    echo, sent = editor.feed(b"hunter2\r")
    assert sent == b"hunter2\r" and echo == b"\r\n"
    assert editor.history == [] and editor.secret is False


async def test_session_hides_input_after_password_prompt():
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    profile.terminal.line_editing = True
    session = await open_session(profile, FakePrompter())
    try:
        await session.write(b"Password:")  # stands in for the device prompt, looped back
        await session.write(b"\r")
        await read_until(session, b"\nPassword:\r")  # the loopback, after the local echo's CR LF
        assert session._editor.secret
        await session.write(b"s3cret")
        await session.write(b"\r")
        out = await read_until(session, b"s3cret\r")
        assert out.count(b"s3cret") == 1  # only the loopback, no local echo
        assert "s3cret" not in session._editor.history
    finally:
        await session.close()
