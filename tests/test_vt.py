"""Terminal options shared by every UI: themes, key mappings, initial modes, the remote-control
filter, paste cleaning, printing and logging (themes.py, vt.py, TerminalSession)."""

import logging
import os

import pytest

from termdeck.errors import ProfileError
from termdeck.profiles import Profile, SerialSettings, TerminalSettings
from termdeck.session import TerminalSession, open_session
from termdeck.themes import THEMES, palette, parse_colours
from termdeck.vt import PrintableLog, PrinterCapture, RemoteFilter, clean_paste, initial_modes, remap_keys

from .conftest import FakePrompter, read_until
from .test_session import FakeInner, _drain


# ---- themes ----

def test_palette_merges_overrides_over_the_theme():
    p = palette("dracula", "background=#112233\nred=#ABCDEF")
    assert p["background"] == "#112233" and p["red"] == "#abcdef"
    assert p["green"] == THEMES["dracula"]["green"]
    assert palette("dracula") == THEMES["dracula"]
    assert palette("nonexistent")["background"] == THEMES["dark"]["background"]


def test_palette_does_not_change_the_theme():
    palette("dark", "background=#010203")
    assert THEMES["dark"]["background"] != "#010203"


@pytest.mark.parametrize("text", ["brightBlue=#112233", "bright_blue=#112233", "Bright Blue = #112233",
                                  "bright-blue=#112233", "BRIGHTBLUE=#112233"])
def test_parse_colours_accepts_name_spellings(text):
    assert parse_colours(text) == {"brightBlue": "#112233"}


def test_parse_colours_skips_blank_lines_and_reads_several():
    assert parse_colours("\nforeground=#ffffff\n\n  \ncursor=#000000\n") == {
        "foreground": "#ffffff", "cursor": "#000000"}
    assert parse_colours("") == {}


@pytest.mark.parametrize("text", ["purple=#112233", "red=112233", "red=#12345", "red=#1234567", "red=#gggggg",
                                  "red", "red=", "=#112233", "red=#112233\nbogus=#000000", "red=blue"])
def test_parse_colours_rejects(text):
    with pytest.raises(ProfileError, match="invalid colour"):
        parse_colours(text)


def test_profile_validate_rejects_bad_colours():
    profile = Profile("p", "serial", serial=SerialSettings(device="loop://"))
    profile.terminal.colours = "red=#112233"
    profile.validate()
    profile.terminal.colours = "red=nope"
    with pytest.raises(ProfileError, match="invalid colour"):
        profile.validate()


# ---- key mappings ----

def _keys(**settings):
    return TerminalSettings(**settings)


@pytest.mark.parametrize("style, home, end", [
    ("standard", b"\x1b[1~", b"\x1b[4~"),
    ("rxvt", b"\x1b[7~", b"\x1b[8~"),
    ("xterm", b"\x1b[H", b"\x1b[F"),
])
def test_home_and_end_styles(style, home, end):
    s = _keys(home_end=style)
    # Both the normal and the application cursor key forms the UIs send.
    assert remap_keys(b"\x1b[H", s) == home and remap_keys(b"\x1bOH", s) == (home if style != "xterm" else b"\x1bOH")
    assert remap_keys(b"\x1b[F", s) == end and remap_keys(b"\x1bOF", s) == (end if style != "xterm" else b"\x1bOF")


@pytest.mark.parametrize("style, mapped", [
    ("xterm", [b"\x1bOP", b"\x1bOQ", b"\x1bOR", b"\x1bOS", b"\x1b[15~", b"\x1b[17~", b"\x1b[18~", b"\x1b[19~",
               b"\x1b[20~", b"\x1b[21~", b"\x1b[23~", b"\x1b[24~"]),
    ("vt400", [b"\x1b[11~", b"\x1b[12~", b"\x1b[13~", b"\x1b[14~"] + [None] * 8),
    ("linux", [b"\x1b[[A", b"\x1b[[B", b"\x1b[[C", b"\x1b[[D", b"\x1b[[E"] + [None] * 7),
    ("vt100", [b"\x1bO" + bytes([c]) for c in b"PQRSTUVWXYZ["]),
    ("sco", [b"\x1b[" + bytes([c]) for c in b"MNOPQRSTUVWX"]),
])
def test_function_key_styles(style, mapped):
    xterm = [b"\x1bOP", b"\x1bOQ", b"\x1bOR", b"\x1bOS", b"\x1b[15~", b"\x1b[17~", b"\x1b[18~", b"\x1b[19~",
             b"\x1b[20~", b"\x1b[21~", b"\x1b[23~", b"\x1b[24~"]
    s = _keys(function_keys=style)
    for key, expected in zip(xterm, mapped):
        assert remap_keys(key, s) == (expected or key)  # keys the style has no change for stay as sent


def test_remap_leaves_other_input_alone():
    s = _keys(home_end="rxvt", function_keys="sco")
    for data in (b"hello", "héllo 世界".encode(), b"\x1b[A", b"\x1b[3~", b"\x1b[5~", b"\x1b[1;5D", b"\x1bb", b"\x1b",
                 b"\x1b[", b"\x03\r\x7f"):
        assert remap_keys(data, s) == data


def test_remap_changes_keys_inside_other_input_only():
    s = _keys(home_end="standard", function_keys="vt400")
    assert remap_keys(b"ab\x1b[Hcd\x1bOPe\x1b[A", s) == b"ab\x1b[1~cd\x1b[11~e\x1b[A"


def test_default_settings_send_keys_as_xterm():
    """$TERM is xterm-256color, so by default Home, End and F-keys go out as an xterm sends them."""
    keys = b"\x1b[H\x1b[F\x1bOP\x1b[15~"
    assert remap_keys(keys, TerminalSettings()) == keys


async def test_session_write_sends_the_mapped_keys():
    inner = FakeInner()
    session = TerminalSession(inner, _keys(home_end="rxvt", function_keys="vt400"))
    await session.write(b"\x1b[H")
    await session.write(b"\x1bOP")
    await session.write(b"x")
    assert inner.writes == [b"\x1b[7~", b"\x1b[11~", b"x"]
    await session.close()


async def test_session_write_maps_keys_after_enter_and_backspace_settings():
    inner = FakeInner()
    session = TerminalSession(inner, _keys(enter_sends="crlf", backspace_sends="bs", function_keys="sco"))
    await session.write(b"\r\x7f\x1b[15~")
    assert inner.writes == [b"\r\n\x08\x1b[Q"]
    await session.close()


# ---- initial modes ----

def test_initial_modes():
    assert initial_modes(TerminalSettings()) == b""
    assert initial_modes(_keys(auto_wrap=False)) == b"\x1b[?7l"
    assert initial_modes(_keys(origin_mode=True)) == b"\x1b[?6h"
    assert initial_modes(_keys(app_cursor_keys=True)) == b"\x1b[?1h"
    assert initial_modes(_keys(app_keypad=True)) == b"\x1b="
    assert initial_modes(_keys(auto_wrap=False, origin_mode=True, app_cursor_keys=True, app_keypad=True)) == (
        b"\x1b[?7l\x1b[?6h\x1b[?1h\x1b=")


@pytest.mark.parametrize("option, value, sequence", [
    ("auto_wrap", False, b"\x1b[?7l"), ("origin_mode", True, b"\x1b[?6h"),
    ("app_cursor_keys", True, b"\x1b[?1h"), ("app_keypad", True, b"\x1b="),
])
async def test_session_shows_initial_modes_first_and_sends_nothing(option, value, sequence):
    inner = FakeInner([b"hello"])
    session = TerminalSession(inner, _keys(**{option: value}))
    first = await session.read()
    assert sequence in first and first.startswith(sequence)
    assert (first + await _drain(session)).endswith(b"hello")
    assert inner.writes == []
    await session.close()


async def test_session_without_initial_modes_shows_only_the_output():
    session = TerminalSession(FakeInner([b"hello"]), TerminalSettings())
    assert await _drain(session) == b"hello"
    await session.close()


async def test_raw_session_gets_no_initial_modes():
    session = TerminalSession(FakeInner([b"x"]), _keys(auto_wrap=False), raw=True)
    assert await _drain(session) == b"x"
    await session.close()


# ---- the remote-control filter ----

def _filter(data, chunks=None, **settings):
    f = RemoteFilter(_keys(**settings))
    if chunks:
        return b"".join(f.feed(c) for c in chunks) + f.feed(b"", final=True)
    return f.feed(data)


def test_mouse_modes_are_dropped_and_other_modes_kept():
    assert _filter(b"a\x1b[?1000;1006hb", allow_mouse=False) == b"ab"
    assert _filter(b"\x1b[?1000;25h", allow_mouse=False) == b"\x1b[?25h"
    assert _filter(b"\x1b[?25;1002l", allow_mouse=False) == b"\x1b[?25l"
    assert _filter(b"\x1b[?1000h", allow_mouse=True, allow_title=False) == b"\x1b[?1000h"


@pytest.mark.parametrize("terminator", [b"\x07", b"\x1b\\"])
@pytest.mark.parametrize("code", [b"0", b"1", b"2"])
def test_titles_are_dropped(code, terminator):
    data = b"a\x1b]" + code + b";my title" + terminator + b"b"
    assert _filter(data, allow_title=False) == b"ab"
    assert _filter(data, allow_title=True, allow_mouse=False) == data


def test_other_osc_sequences_are_kept_when_titles_are_blocked():
    data = b"\x1b]52;c;aGk=\x07"
    assert _filter(data, allow_title=False) == data


@pytest.mark.parametrize("option, sequence", [
    ("allow_alt_screen", b"\x1b[?1049h"), ("allow_bracketed_paste", b"\x1b[?2004h"),
    ("allow_clear_scrollback", b"\x1b[3J"), ("allow_resize", b"\x1b[8;40;120t"),
    ("allow_charset", b"\x1b(0"), ("allow_app_keys", b"\x1b[?1h"),
])
def test_each_switch_drops_its_sequence_only(option, sequence):
    assert _filter(b"x" + sequence + b"y", **{option: False}) == b"xy"
    assert _filter(b"x" + sequence + b"y", **{option: True, "allow_mouse": False}) == b"x" + sequence + b"y"
    others = b"\x1b[2J\x1b[5n\x1b[?25h\x1b[31m"  # covered by no switch
    assert _filter(others, **{option: False}) == others


def test_charset_shifts_and_designations():
    assert _filter(b"a\x0eb\x0fc\x1b(0d\x1b)Be", allow_charset=False) == b"abcde"
    assert _filter(b"a\x0eb", allow_charset=True, allow_mouse=False) == b"a\x0eb"


def test_application_keypad_sequences():
    assert _filter(b"\x1b=x\x1b>", allow_app_keys=False) == b"x"
    assert _filter(b"\x1b=x\x1b>", allow_app_keys=True, allow_mouse=False) == b"\x1b=x\x1b>"
    assert _filter(b"\x1b[?66h\x1b[?1l", allow_app_keys=False) == b""


@pytest.mark.parametrize("cut", range(1, 16))
def test_a_sequence_split_across_chunks_is_still_filtered(cut):
    data = b"A\x1b[?1000;1006hB"
    assert _filter(None, chunks=[data[:cut], data[cut:]], allow_mouse=False) == b"AB"


@pytest.mark.parametrize("cut", range(1, 12))
def test_a_title_split_across_chunks_is_still_dropped(cut):
    data = b"A\x1b]0;title\x07B"
    assert _filter(None, chunks=[data[:cut], data[cut:]], allow_title=False) == b"AB"


def test_a_sequence_that_is_not_ours_is_not_held_back():
    f = RemoteFilter(_keys(allow_mouse=False))
    assert f.feed(b"abc\x1b[31m") == b"abc\x1b[31m"
    assert f.feed(b"x") == b"x"


def test_unfinished_sequence_is_released_at_the_end():
    f = RemoteFilter(_keys(allow_mouse=False))
    assert f.feed(b"a\x1b[?10") == b"a"
    assert f.feed(b"", final=True) == b"\x1b[?10"


def test_everything_allowed_changes_nothing_and_is_inactive():
    f = RemoteFilter(TerminalSettings())
    assert not f.active
    data = b"\x1b[?1000h\x1b]0;t\x07\x1b[3J\x1b[8;1;1t\x1b(0\x0e\x1b=\x1b[?1049h\x1b[?2004h\x1b["
    assert f.feed(data) == data
    assert RemoteFilter(_keys(allow_title=False)).active
    assert RemoteFilter(_keys(allow_mouse=False)).active


async def test_session_filters_remote_output():
    inner = FakeInner([b"a\x1b[?1000;25hb\x1b]2;t\x07c"])
    session = TerminalSession(inner, _keys(allow_mouse=False, allow_title=False))
    assert await _drain(session) == b"a\x1b[?25hbc"
    await session.close()


# ---- paste ----

def test_clean_paste_removes_controls_but_keeps_layout():
    assert clean_paste("a\x1b[201~rm -rf\x00\x07\x08\x7f\x9b b\tc\r\nd\ne") == "a[201~rm -rf b\tc\r\nd\ne"
    assert clean_paste("héllo 世界 🙂") == "héllo 世界 🙂"
    assert clean_paste("") == ""


def test_clean_paste_allowed_keeps_everything():
    text = "a\x1b[201~b\x00\x07"
    assert clean_paste(text, allow_controls=True) == text


# ---- printing ----

def _printed(chunks):
    printed = []
    capture = PrinterCapture(printed.append)
    shown = b"".join(capture.feed(c) for c in chunks)
    return shown, b"".join(printed)


def test_printer_text_does_not_reach_the_screen():
    assert _printed([b"before\x1b[5iprinted\x1b[4iafter"]) == (b"beforeafter", b"printed")


def test_printer_markers_split_across_chunks():
    data = b"before\x1b[5iprinted\x1b[4iafter"
    for cut in range(1, len(data)):
        shown, printed = _printed([data[:cut], data[cut:]])
        assert (shown, printed) == (b"beforeafter", b"printed"), cut


def test_printer_markers_one_byte_at_a_time():
    data = b"a\x1b[5ibc\x1b[4id\x1b[5ie\x1b[4i"
    assert _printed([bytes([b]) for b in data]) == (b"ad", b"bce")


def test_other_escapes_split_near_printer_markers_are_shown():
    shown, printed = _printed([b"a\x1b[", b"31mb"])
    assert (shown, printed) == (b"a\x1b[31mb", b"")


async def test_printer_file_receives_the_text_on_a_serial_session(tmp_path):
    path = tmp_path / "out" / "print.txt"
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    profile.terminal.printer = str(path)
    session = await open_session(profile, FakePrompter())
    try:
        await session.write(b"A\x1b[5ito paper\x1b[4iB")
        shown = await read_until(session, b"B")
        assert b"to paper" not in shown and b"A" in shown
    finally:
        await session.close()
    assert path.read_bytes() == b"to paper"


# ---- logging ----

def test_printable_log_strips_escapes_and_turns_a_lone_cr_into_a_new_line():
    data = b"\x1b[31mred\x1b[0m\r\nline\x1b]0;title\x07 two\x08\x00\x7f\r"
    assert PrintableLog().feed(data) == b"red\nline two\n"
    assert PrintableLog().feed("漢字 é\ttab".encode()) == "漢字 é\ttab".encode()


def test_printable_log_keeps_a_sequence_cut_between_chunks():
    log = PrintableLog()
    assert log.feed(b"a\x1b[38;5") + log.feed(b";196mb") == b"ab"


async def test_log_mode_printable_writes_clean_text(tmp_path):
    log = tmp_path / "p.log"
    inner = FakeInner([b"\x1b[1mbold\x1b[0m\r\n", b"next\r\n"])
    session = TerminalSession(inner, _keys(log_mode="printable", log_header=False), log=log)
    shown = await _drain(session)
    await session.close()
    assert shown == b"\x1b[1mbold\x1b[0m\r\nnext\r\n"  # the screen still gets everything
    assert log.read_bytes() == b"bold\nnext\n"


async def test_log_exists_overwrite_truncates_and_append_keeps(tmp_path):
    log = tmp_path / "o.log"
    log.write_bytes(b"old content\n")
    for mode, expected in (("append", b"old content\nnew"), ("overwrite", b"new")):
        log.write_bytes(b"old content\n")
        session = TerminalSession(FakeInner([b"new"]), _keys(log_exists=mode, log_header=False), log=log)
        await _drain(session)
        await session.close()
        assert log.read_bytes() == expected


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_log_overwrite_still_refuses_symlinks(tmp_path):
    target = tmp_path / "precious"
    target.write_bytes(b"keep me")
    link = tmp_path / "link.log"
    link.symlink_to(target)
    with pytest.raises(OSError):
        TerminalSession(FakeInner([b"x"]), _keys(log_exists="overwrite"), log=link)
    assert target.read_bytes() == b"keep me"


async def test_log_overwrite_refuses_a_non_regular_file_without_emptying_it(tmp_path):
    folder = tmp_path / "dir.log"
    folder.mkdir()
    with pytest.raises(OSError):
        TerminalSession(FakeInner(), _keys(log_exists="overwrite"), log=folder)


async def test_log_header_names_the_profile(tmp_path):
    log = tmp_path / "{name}.log"
    profile = Profile("my board", "serial", serial=SerialSettings(device="loop://"))
    profile.terminal.log_file = str(log)
    session = await open_session(profile, FakePrompter())
    try:
        await session.write(b"zz")
        await read_until(session, b"zz")
    finally:
        await session.close()
    [written] = list(tmp_path.glob("*.log"))
    first, rest = written.read_bytes().split(b"\r\n", 1)
    assert first.startswith(b"=~=~=~= termdeck log ") and b"my board" in first and first.endswith(b"=~=~=~=")
    assert rest == b"zz"


async def test_log_header_can_be_turned_off(tmp_path):
    log = tmp_path / "n.log"
    session = TerminalSession(FakeInner([b"x"]), _keys(log_header=False), log=log)
    await _drain(session)
    await session.close()
    assert log.read_bytes() == b"x"


def _ssh_handlers():
    return [h for h in logging.getLogger("asyncssh").handlers if type(h).__name__ == "_LogHandler"]


async def test_log_mode_ssh_writes_protocol_lines_and_removes_its_handler(tmp_path, ssh_server):
    import asyncssh

    log = tmp_path / "ssh.log"
    profile = ssh_server.profile()
    profile.terminal.log_file = str(log)
    profile.terminal.log_mode = "ssh"
    logger = logging.getLogger("asyncssh")
    level = logger.level
    try:
        session = await open_session(profile, FakePrompter())
        assert len(_ssh_handlers()) == 1
        try:
            await session.write(b"ping\r")
            await read_until(session, b"ping")
        finally:
            await session.close()
        assert _ssh_handlers() == []
        assert logger.level == level  # asyncssh's logging is back to how it was
    finally:
        logger.setLevel(level)
        asyncssh.set_debug_level(1)
    text = log.read_text(errors="replace")
    assert text.startswith("=~=~=~= termdeck log")
    assert " ssh: " in text and "ping" in text  # protocol lines, and the output
    assert "secret" not in text.replace("(hidden)", "")  # the password is not logged


async def test_log_mode_output_installs_no_ssh_handler(tmp_path, ssh_server):
    profile = ssh_server.profile()
    profile.terminal.log_file = str(tmp_path / "o.log")
    session = await open_session(profile, FakePrompter())
    try:
        assert _ssh_handlers() == []
    finally:
        await session.close()


async def test_log_mode_ssh_keeps_logging_until_the_last_session_closes(tmp_path, ssh_server):
    logger = logging.getLogger("asyncssh")
    level = logger.level
    sessions = []
    try:
        for n in (1, 2):
            profile = ssh_server.profile()
            profile.terminal.log_file = str(tmp_path / f"s{n}.log")
            profile.terminal.log_mode = "ssh"
            sessions.append(await open_session(profile, FakePrompter()))
        assert len(_ssh_handlers()) == 2
        await sessions[0].close()
        assert len(_ssh_handlers()) == 1 and logger.level == logging.DEBUG  # the other still logs
        await sessions[1].close()
        assert _ssh_handlers() == [] and logger.level == level
    finally:
        for session in sessions:
            await session.close()
        logger.setLevel(level)
