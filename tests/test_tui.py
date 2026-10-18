import asyncio

import pytest
from textual.widgets import Button, Input, Label, ListView, Select, Switch

from termdeck.profiles import Profile, ProfileStore, SerialSettings
from termdeck.tui.app import ConfirmDialog, InputDialog, ProfileEditor, TermdeckTUI, text_to_forwards
from termdeck.tui.terminal import TerminalView, key_to_bytes


@pytest.mark.parametrize("key, char, app_cursor, expected", [
    ("a", "a", False, b"a"),
    ("enter", "\r", False, b"\r"),
    ("ctrl+c", None, False, b"\x03"),
    ("up", None, False, b"\x1b[A"),
    ("up", None, True, b"\x1bOA"),
    ("ctrl+left", None, False, b"\x1b[1;5D"),
    ("alt+b", "b", False, b"\x1bb"),
    ("f5", None, False, b"\x1b[15~"),
    ("eacute", "é", False, "é".encode()),
])
def test_key_to_bytes(key, char, app_cursor, expected):
    assert key_to_bytes(key, char, app_cursor) == expected


def test_text_to_forwards():
    fwds = text_to_forwards("L 8080:db:5432, D 1080")
    assert [f.kind for f in fwds] == ["local", "dynamic"]
    with pytest.raises(ValueError):
        text_to_forwards("X 1")


def _store(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.save(Profile("loop", "serial", group="Lab", serial=SerialSettings(device="loop://")))
    return store


async def test_connect_serial_and_type(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_tab(app.store.get("loop"))
        await _until(pilot, lambda: app.query(TerminalView) and app.query_one(TerminalView).status == "connected")
        view = app.query_one(TerminalView)
        assert view.status == "connected"
        assert app.focused is view
        await pilot.press("h", "i", "q")  # q must reach the terminal, not quit
        await _until(pilot, lambda: "hiq" in view._vt.display[0])
        assert "hiq" in view._vt.display[0]
        await pilot.press("ctrl+right_square_bracket")
        assert app.focused.id == "profiles"
        await pilot.press("w")  # close tab from menu mode: connected, so it asks first
        await _until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        assert isinstance(app.screen, ConfirmDialog)
        assert app.screen.focused.id == "cancel"  # Enter alone does not close a live session
        await pilot.click("#ok")
        await _until(pilot, lambda: not app.query(TerminalView))
        assert not app.query(TerminalView)


async def test_editor_shows_only_selected_kind(tmp_path):
    store = _store(tmp_path)
    app = TermdeckTUI(store=store)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, ProfileEditor)
        def ids():
            """Ids of the inputs the user can currently see."""
            return {w.id for w in editor.query("Input, Select, Switch, TextArea")
                    if w.id and all(n.display for n in w.ancestors_with_self)}
        assert "ssh--host" in ids() and "serial--device" not in ids()
        editor.query_one("#profile--kind", Select).value = "serial"
        await _until(pilot, lambda: "serial--device" in ids())
        assert "serial--device" in ids() and "ssh--host" not in ids()
        assert "terminal--term_type" not in ids()  # SSH-only option hidden for serial
        dtr = editor.query_one("#serial--dtr").parent
        assert not dtr.display  # advanced, hidden by default
        editor.query_one("#advanced", Switch).value = True
        await pilot.pause()
        assert dtr.display
        editor.query_one("#profile--name", Input).value = "board"
        editor.query_one("#serial--device", Input).value = "/dev/ttyACM0"
        await pilot.pause()
        await pilot.click("#save")
        await _until(pilot, lambda: not isinstance(app.screen, ProfileEditor))
        assert store.get("board").serial.device == "/dev/ttyACM0"


async def test_ctrl_q_quits_from_quick_connect_but_not_from_terminal(tmp_path):
    app = TermdeckTUI(store=ProfileStore(tmp_path / "empty.json"))
    async with app.run_test() as pilot:
        assert app.focused.id == "quick"  # first run: ready to type a target
        await pilot.press("q")
        assert app.query_one("#quick", Input).value == "q"
        await pilot.press("ctrl+q")
        await pilot.pause()
    assert app.return_code == 0


async def test_ctrl_q_goes_to_the_device(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_tab(app.store.get("loop"))
        await _until(pilot, lambda: app.query(TerminalView) and app.query_one(TerminalView).status == "connected")
        await pilot.press("ctrl+q")
        await pilot.pause(0.2)
        assert app.is_running and app.query(TerminalView)


async def test_markup_in_remote_and_profile_text_is_shown_literally(tmp_path):
    from termdeck.tui.app import InputDialog

    store = ProfileStore(tmp_path / "p.json")
    store.save(Profile("[/x] evil", "serial", serial=SerialSettings(device="loop://")))
    store.save(Profile("[RED]shown trail\\", "serial", serial=SerialSettings(device="loop://")))
    app = TermdeckTUI(store=store)
    async with app.run_test(size=(120, 40)) as pilot:
        app.push_screen(InputDialog("[/x]", "Password [/x] [link=http://x]click[/]:", secret=True))
        await pilot.pause()
        assert "[link=http://x]" in str(app.screen.query("Label")[1].render())
        await pilot.press("escape")
        app.open_tab(store.get("[/x] evil"))
        await _until(pilot, lambda: app.query(TerminalView))
        assert app.query(TerminalView)
        labels = " ".join(str(item.query_one(Label).render()) for item in app.query("ProfileItem"))
        assert "[RED]shown trail\\" in labels and "[/x] evil" in labels


async def test_editor_reports_a_cleared_number_and_saves_a_typed_one(tmp_path):
    store = _store(tmp_path)
    app = TermdeckTUI(store=store)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        editor = app.screen
        editor.query_one("#profile--name", Input).value = "box"
        editor.query_one("#ssh--host", Input).value = "box.example"
        port = editor.query_one("#ssh--port", Input)
        port.value = ""
        await pilot.pause()
        await pilot.click("#save")
        await pilot.pause(0.3)
        assert app.screen is editor
        assert "Port: enter a number" in str(editor.query_one("#error", Label).render())
        assert "box" not in store.load()
        port.value = "2222"
        await pilot.pause()
        await pilot.click("#save")
        await _until(pilot, lambda: not isinstance(app.screen, ProfileEditor))
        assert store.get("box").ssh.port == 2222
        assert isinstance(store.get("box").ssh.port, int)


async def test_quick_connect_with_a_mistyped_name_suggests_and_opens_no_tab(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    notes = []
    app.notify = lambda message, **kw: notes.append((str(message), kw.get("severity")))
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#quick", Input).focus()
        await pilot.press(*"lop", "enter")
        await pilot.pause(0.2)
        assert any("did you mean 'loop'?" in m and sev == "error" for m, sev in notes)
        assert not app.query(TerminalView)


async def test_empty_quick_connect_does_nothing(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    notes = []
    app.notify = lambda message, **kw: notes.append(str(message))
    async with app.run_test(size=(120, 40)) as pilot:
        app.query_one("#quick", Input).focus()
        await pilot.press("enter")
        await pilot.press(*"  ", "enter")
        await pilot.pause(0.2)
        assert notes == []
        assert not app.query(TerminalView)


@pytest.mark.parametrize("field", ["ssh--keepalive", "ssh--connect_timeout"])
async def test_editor_shows_advanced_and_focuses_a_cleared_advanced_number(tmp_path, field):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        editor = app.screen
        editor.query_one("#advanced", Switch).value = False
        editor.query_one("#profile--name", Input).value = "box"
        editor.query_one("#ssh--host", Input).value = "box.example"
        editor.query_one(f"#{field}", Input).value = ""
        await pilot.pause()
        await pilot.click("#save")
        await pilot.pause(0.3)
        assert app.screen is editor
        assert editor.query_one("#advanced", Switch).value is True
        assert app.focused is editor.query_one(f"#{field}", Input)
        assert "enter a number" in str(editor.query_one("#error", Label).render())
        assert "box" not in app.store.load()


def _record_notifications(app):
    """Collect what the app tells the user through notify()."""
    seen = []
    original = app.notify

    def notify(message, **kw):
        seen.append((str(message), kw.get("severity", "information")))
        return original(message, **kw)

    app.notify = notify
    return seen


async def test_corrupt_profiles_file_is_reported_not_fatal(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("{oops")
    app = TermdeckTUI(store=ProfileStore(path))
    seen = _record_notifications(app)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert any(str(path) in m and s == "error" for m, s in seen)
        assert app.focused.id == "quick"  # starts as with no profiles
    assert path.read_text() == "{oops"


async def test_invalid_entries_are_reported_in_the_tui(tmp_path):
    import json

    path = tmp_path / "p.json"
    path.write_text(json.dumps({"profiles": [
        {"name": "good", "kind": "serial", "serial": {"device": "d"}},
        {"name": "bad", "kind": "serial", "serial": {"device": "d", "parity": "Q"}},
    ]}))
    app = TermdeckTUI(store=ProfileStore(path))
    seen = _record_notifications(app)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert any("bad" in m and s == "warning" for m, s in seen)


async def test_skipped_entry_warning_is_not_repeated_after_a_reload(tmp_path):
    import json

    path = tmp_path / "p.json"
    path.write_text(json.dumps({"profiles": [
        {"name": "bad", "kind": "serial", "serial": {"device": "d", "parity": "Q"}},
    ]}))
    app = TermdeckTUI(store=ProfileStore(path))
    seen = _record_notifications(app)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await app.reload_profiles()
        app.store.add(Profile("fine", "serial", serial=SerialSettings(device="loop://")))
        await app.reload_profiles()
        await pilot.pause()
        assert [m for m, s in seen if "bad" in m and s == "warning"] != []
        assert len([m for m, s in seen if "bad" in m and s == "warning"]) == 1


def _screen_text(view):
    """The screen with rows joined, so a message wrapped over two rows is still found."""
    lines = ("".join(segment.text for segment in view.render_line(y)) for y in range(view._vt.lines))
    return "".join(line.rstrip() + " " for line in lines)  # what the view draws, scrollback included


async def _wait_for_screen(pilot, view, text, timeout=5.0):
    async def poll():
        while text not in _screen_text(view):
            await pilot.pause(0.02)

    await asyncio.wait_for(poll(), timeout)


async def test_many_quick_key_presses_arrive_in_order(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_tab(app.store.get("loop"))
        await _until(pilot, lambda: app.query(TerminalView) and app.query_one(TerminalView).status == "connected")
        view = app.query_one(TerminalView)
        assert view.status == "connected"
        text = "abcdefghijklmnopqrstuvwxyz0123456789ZYXWVUTSRQPONMLKJIHGFEDCBA"
        await pilot.press(*text)
        await _wait_for_screen(pilot, view, text)


async def test_ended_session_says_why(tmp_path, patch_port):
    import threading

    import serial

    gone = threading.Event()

    def read(real, size=1):
        if gone.wait(0.02):
            raise serial.SerialException("cable pulled")  # reported in the user's words
        return real(size)

    patch_port(read=read)
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_tab(app.store.get("loop"))
        await _until(pilot, lambda: app.query(TerminalView) and app.query_one(TerminalView).status == "connected")
        view = app.query_one(TerminalView)
        assert view.status == "connected"
        gone.set()
        await _wait_for_screen(pilot, view, "Session ended: ")
        screen = _screen_text(view)
        assert "Session ended: loop: " in screen and "Enter to reconnect" in screen
        assert view.status == "ended"


async def test_normal_end_has_no_reason(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        profile = app.store.get("loop")
        profile.terminal.close_on_exit = "never"  # the default closes the tab on a clean end
        app.open_tab(profile)
        await _until(pilot, lambda: app.query(TerminalView) and app.query_one(TerminalView).status == "connected")
        view = app.query_one(TerminalView)
        await view.session.close()
        await _wait_for_screen(pilot, view, "Session ended")
        assert "Session ended:" not in _screen_text(view)


async def _host_key_dialog(app, pilot, changed):
    from termdeck.session import HostKey
    from termdeck.tui.app import ConfirmDialog, TUIPrompter

    key = HostKey("example.com", 22, "ssh-ed25519", "SHA256:abc", changed=changed,
                  old_fingerprints=["ssh-ed25519 SHA256:old"] if changed else [], known_hosts="/tmp/kh")
    answer = asyncio.create_task(TUIPrompter(app, "srv").confirm_host_key(key))
    await pilot.pause()
    assert isinstance(app.screen, ConfirmDialog)
    return answer


@pytest.mark.parametrize("changed, variant", [(False, "primary"), (True, "error")])
async def test_host_key_dialog_starts_on_cancel_and_enables_ok_later(tmp_path, changed, variant):
    from textual.widgets import Button

    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        answer = await _host_key_dialog(app, pilot, changed)
        cancel, ok = app.screen.query_one("#cancel", Button), app.screen.query_one("#ok", Button)
        assert cancel.has_focus and ok.disabled
        assert ok.variant == variant

        async def enabled():
            while ok.disabled:
                await pilot.pause(0.05)

        await asyncio.wait_for(enabled(), 5)
        assert cancel.has_focus  # focus did not move to the button that accepts
        await pilot.press("escape")
        assert await asyncio.wait_for(answer, 5) is False


async def test_failed_write_is_reported_and_later_keys_still_go_out(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_tab(app.store.get("loop"))
        await _until(pilot, lambda: app.query(TerminalView) and app.query_one(TerminalView).status == "connected")
        view = app.query_one(TerminalView)
        assert view.status == "connected"
        real, calls = view.session.write, []

        async def flaky(data):
            calls.append(data)
            if len(calls) == 1:
                raise OSError("pipe closed")
            await real(data)

        view.session.write = flaky
        await pilot.press("a")
        await _wait_for_screen(pilot, view, "input not sent")
        await pilot.press("b", "c")
        await _wait_for_screen(pilot, view, "bc")
        assert calls == [b"a", b"b", b"c"] and view.status == "connected"


async def test_editor_hides_the_proxy_host_until_a_proxy_is_chosen(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        editor = app.screen
        editor.query_one("#advanced", Switch).value = True
        await pilot.pause()
        field = editor.query_one("#field-ssh--proxy_host")
        assert field.has_class("unused")
        editor.query_one("#ssh--proxy_type", Select).value = "socks5"
        await pilot.pause()
        assert not field.has_class("unused")
        editor.query_one("#ssh--proxy_type", Select).value = "command"  # its own field, not the host
        await pilot.pause()
        assert field.has_class("unused")
        assert not editor.query_one("#field-ssh--proxy_command").has_class("unused")


async def test_editor_shows_the_jump_command_only_with_a_jump_host(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await pilot.pause()
        editor = app.screen
        editor.query_one("#advanced", Switch).value = True
        await pilot.pause()
        field = editor.query_one("#field-ssh--jump_command")
        assert field.has_class("unused")
        editor.query_one("#ssh--jump_host", Input).value = "bastion.example"
        await pilot.pause()
        assert not field.has_class("unused")
        editor.query_one("#ssh--jump_host", Input).value = ""
        await pilot.pause()
        assert field.has_class("unused")


# ---- terminal options: bell, title, scrolling, closing, colours, cursor, paste ----

def _loop_profile(**terminal):
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    for key, value in terminal.items():
        setattr(profile.terminal, key, value)
    return profile


async def _open(app, pilot, profile=None):
    app.open_tab(profile or app.store.get("loop"))
    await _until(pilot, lambda: app.query(TerminalView) and list(app.query(TerminalView))[-1].status == "connected")
    return list(app.query(TerminalView))[-1]


async def _until(pilot, condition, timeout=5.0):
    async def poll():
        while not condition():
            await pilot.pause(0.02)

    await asyncio.wait_for(poll(), timeout)


def _tab_label(app, view):
    return str(app.query_one("#tabs").get_tab(view.parent.id).label)


async def test_visual_bell_flashes_then_clears(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    rung = []
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(bell="visual"))
        app.bell = lambda: rung.append(1)
        await view.send(b"\x07")
        await _until(pilot, lambda: view._flash)
        await _until(pilot, lambda: not view._flash)
        assert rung == []


async def test_sound_bell_beeps_without_flashing(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    rung = []
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(bell="sound"))
        app.bell = lambda: rung.append(1)
        await view.send(b"\x07")
        await _until(pilot, lambda: rung)
        assert not view._flash


async def test_no_bell_does_nothing(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    rung = []
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(bell="none"))
        app.bell = lambda: rung.append(1)
        await view.send(b"\x07x")
        await _wait_for_screen(pilot, view, "x")
        assert rung == [] and not view._flash and not view.bell


async def test_bell_in_a_hidden_tab_marks_its_label_until_activated(tmp_path):
    from textual.widgets import TabbedContent

    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        first = await _open(app, pilot, _loop_profile(bell="visual"))
        second = await _open(app, pilot, _loop_profile(bell="visual"))
        tabs = app.query_one("#tabs", TabbedContent)
        assert tabs.active == second.parent.id
        await first.send(b"\x07")
        await _until(pilot, lambda: " !" in _tab_label(app, first))
        assert " !" not in _tab_label(app, second)
        tabs.active = first.parent.id
        await _until(pilot, lambda: " !" not in _tab_label(app, first))
        assert not first.bell


async def test_bell_in_the_shown_tab_does_not_mark_it(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(bell="visual"))
        await view.send(b"\x07")
        await _until(pilot, lambda: view._flash)
        assert " !" not in _tab_label(app, view)


async def test_remote_title_shows_in_the_tab_label_and_is_not_markup(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile())
        await view.send(b"\x1b]2;user@host: [bold]~/src\x07")
        await _until(pilot, lambda: "~/src" in _tab_label(app, view))
        assert "loop: user@host: [bold]~/src" in _tab_label(app, view)


async def _fill_scrollback(app, pilot, **terminal):
    view = await _open(app, pilot, _loop_profile(bell="sound", **terminal))
    await view.send(b"".join(b"line%03d\r\n" % i for i in range(120)))
    await _wait_for_screen(pilot, view, "line119")
    return view


async def _processed(app, pilot, view):
    """Wait until what was sent before has been drawn: a bell sent after it rings."""
    rung = []
    app.bell = lambda: rung.append(1)
    await view.send(b"\x07")
    await _until(pilot, lambda: rung)


@pytest.mark.parametrize("scroll_on_output", [False, True])
async def test_scroll_on_output(tmp_path, scroll_on_output):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _fill_scrollback(app, pilot, scroll_on_output=scroll_on_output)
        await pilot.press("shift+pageup")
        assert "line119" not in _screen_text(view)
        await view.send(b"newer output\r\n")
        await _processed(app, pilot, view)
        if scroll_on_output:
            assert "newer output" in _screen_text(view)
        else:
            assert "newer output" not in _screen_text(view) and "line119" not in _screen_text(view)
            await pilot.press("shift+pagedown")
            assert "newer output" in _screen_text(view)


@pytest.mark.parametrize("on_key", [True, False])
async def test_scroll_on_key_returns_to_the_bottom_unless_turned_off(tmp_path, on_key):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _fill_scrollback(app, pilot, scroll_on_key=on_key)
        await pilot.press("shift+pageup")
        await pilot.press("z")
        await _processed(app, pilot, view)
        assert ("line119" in _screen_text(view)) == on_key


async def test_close_on_exit_never_keeps_the_tab_after_a_clean_end(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(close_on_exit="never"))
        await view.session.close()
        await _wait_for_screen(pilot, view, "Session ended")
        await pilot.pause(0.3)
        assert app.query(TerminalView) and view.status == "ended"


@pytest.mark.parametrize("when", ["clean", "always"])
async def test_close_on_exit_closes_the_tab_after_a_clean_end(tmp_path, when):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(close_on_exit=when))
        await view.session.close()
        await _until(pilot, lambda: not app.query(TerminalView))


def _pull_the_cable(patch_port):
    import threading

    import serial

    gone = threading.Event()

    def read(real, size=1):
        if gone.wait(0.02):
            raise serial.SerialException("cable pulled")
        return real(size)

    patch_port(read=read)
    return gone


@pytest.mark.parametrize("when, closes", [("never", False), ("clean", False), ("always", True)])
async def test_close_on_exit_after_an_error_end(tmp_path, patch_port, when, closes):
    gone = _pull_the_cable(patch_port)
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(close_on_exit=when))
        gone.set()
        if closes:
            await _until(pilot, lambda: not app.query(TerminalView))
        else:
            await _wait_for_screen(pilot, view, "Session ended: ")  # the reason stays readable
            await pilot.pause(0.3)
            assert app.query(TerminalView) and view.status == "ended"


async def _cell_style(view, text):
    """The style of the first cell showing `text` in the view's first rendered rows."""
    for y in range(view._vt.lines):
        x = view._vt.display[y].find(text)
        if x >= 0:
            offset = 0
            for segment in view.render_line(y):
                if offset <= x < offset + len(segment.text):
                    return segment.style
                offset += len(segment.text)
    raise AssertionError(f"{text!r} is not on the screen")


@pytest.mark.parametrize("theme, red", [("dark", "#cd0000"), ("dracula", "#ff5555"), ("putty", "#bb0000")])
async def test_palette_colours_a_red_sgr_as_the_themes_red(tmp_path, theme, red):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(theme=theme))
        await view.send(b"\x1b[31mRED\x1b[0m plain")
        await _wait_for_screen(pilot, view, "plain")
        style = await _cell_style(view, "RED")
        assert style.color.get_truecolor().hex == red
        plain = await _cell_style(view, "plain")
        assert plain.color.get_truecolor().hex != red


async def test_colour_overrides_apply_to_the_tui(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(colours="red=#123456\nbackground=#010203"))
        await view.send(b"\x1b[31mRED\x1b[0m")
        await _wait_for_screen(pilot, view, "RED")
        assert (await _cell_style(view, "RED")).color.get_truecolor().hex == "#123456"
        assert (await _cell_style(view, "RED")).bgcolor.get_truecolor().hex == "#010203"


async def _cursor_style(view):
    cursor = view._vt.cursor
    offset = 0
    for segment in view.render_line(cursor.y):
        if offset <= cursor.x < offset + len(segment.text):
            return segment.style
        offset += len(segment.text)


@pytest.mark.parametrize("cursor_style, reverse, underline", [("block", True, False), ("underline", False, True),
                                                              ("bar", False, True)])
async def test_cursor_style_renders_in_the_cursor_cell(tmp_path, cursor_style, reverse, underline):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(cursor_style=cursor_style))
        await view.send(b"ab")
        await _wait_for_screen(pilot, view, "ab")
        assert app.focused is view
        style = await _cursor_style(view)
        assert bool(style.reverse) is reverse and bool(style.underline) is underline


async def test_paste_cannot_end_a_bracketed_paste_early(tmp_path):
    from textual import events

    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(local_echo=False))
        sent = []
        real = view.session.write

        async def record(data):
            sent.append(data)
            await real(data)

        view.session.write = record
        await view.on_paste(events.Paste("ok\x1b[201~rm -rf /\ttab\nnext"))
        await _until(pilot, lambda: sent)
        assert b"\x1b" not in b"".join(sent)
        assert b"".join(sent) == b"ok[201~rm -rf /\ttab\rnext"


async def test_paste_keeps_controls_when_the_profile_allows_it(tmp_path):
    from textual import events

    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot, _loop_profile(paste_controls=True))
        sent = []

        async def record(data):
            sent.append(data)

        view.session.write = record
        await view.on_paste(events.Paste("a\x1bb"))
        await _until(pilot, lambda: sent)
        assert sent == [b"a\x1bb"]


# ---- batch 8: closing and quitting, break, editor, sidebar, footer, find, selection, ports, files ----

def _store_with(tmp_path, *profiles):
    store = ProfileStore(tmp_path / "p.json")
    for profile in profiles:
        store.save(profile)
    return store


async def _to_menus(pilot, app):
    await pilot.press("ctrl+right_square_bracket")
    await _until(pilot, lambda: app.focused is not None and app.focused.id == "profiles")


async def test_closing_the_tab_of_a_connected_session_asks_and_cancel_keeps_it(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot)
        await _to_menus(pilot, app)
        await pilot.press("w")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        await pilot.click("#cancel")
        await _until(pilot, lambda: not isinstance(app.screen, ConfirmDialog))
        assert app.query(TerminalView) and view.status == "connected"
        await pilot.press("w")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        await pilot.press("escape")  # Esc cancels too
        await _until(pilot, lambda: not isinstance(app.screen, ConfirmDialog))
        assert app.query(TerminalView) and view.status == "connected"


async def test_closing_the_tab_without_the_warning_closes_at_once(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile(warn_on_close=False)))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(app, pilot)
        await _to_menus(pilot, app)
        await pilot.press("w")
        await _until(pilot, lambda: not app.query(TerminalView))
        assert not isinstance(app.screen, ConfirmDialog)


async def test_closing_an_ended_tab_does_not_ask(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile(close_on_exit="never")))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot)
        await view.session.close()
        await _until(pilot, lambda: view.status == "ended")
        await _to_menus(pilot, app)
        await pilot.press("w")
        await _until(pilot, lambda: not app.query(TerminalView))
        assert not isinstance(app.screen, ConfirmDialog)


async def test_quitting_with_a_connected_session_asks(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot)
        await _to_menus(pilot, app)
        await pilot.press("q")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        assert "Still connected: loop" in " ".join(str(w.render()) for w in app.screen.query("Static"))
        await pilot.press("escape")  # keeps running
        await _until(pilot, lambda: not isinstance(app.screen, ConfirmDialog))
        assert app.is_running and view.status == "connected"
        await pilot.press("q")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        await pilot.click("#ok")
    assert app.return_code == 0


async def test_quitting_without_a_connected_session_does_not_ask(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("q")
    assert app.return_code == 0


def _record_breaks(view):
    sent = []
    real = view.session.send_break

    async def record():
        sent.append(1)
        await real()

    view.session.send_break = record
    return sent


async def test_break_needs_two_presses(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    seen = _record_notifications(app)
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot)
        sent = _record_breaks(view)
        await _to_menus(pilot, app)
        await pilot.press("b")
        await _until(pilot, lambda: any("Press B again" in m for m, _ in seen))
        assert sent == []
        await pilot.press("b")
        await _until(pilot, lambda: any(m == "Break sent" for m, _ in seen))
        assert sent == [1]
        assert app.focused is view  # back in the terminal after the break


async def test_a_late_second_break_press_only_arms_again(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    app.BREAK_AGAIN = 0.0  # the window for the second press is already over
    seen = _record_notifications(app)
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot)
        sent = _record_breaks(view)
        await _to_menus(pilot, app)
        await pilot.press("b", "b")
        await _until(pilot, lambda: len([m for m, _ in seen if "Press B again" in m]) == 2)
        assert sent == []


async def test_break_in_a_session_that_cannot_send_one_says_nothing(tmp_path, ssh_server):
    from termdeck.ssh import remember_host_key

    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    app = TermdeckTUI(store=_store_with(tmp_path))
    seen = _record_notifications(app)
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_tab(ssh_server.profile())
        await _until(pilot, lambda: isinstance(app.screen, InputDialog))
        await pilot.press(*"secret", "enter")
        view = app.query_one(TerminalView)
        await _until(pilot, lambda: view.status == "connected")
        await _to_menus(pilot, app)
        await pilot.press("b", "b")
        await pilot.pause()
        assert not any("break" in m.lower() for m, _ in seen)


async def test_a_failed_break_is_reported_as_an_error(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    seen = _record_notifications(app)
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot)

        async def refuse():
            raise OSError("device vanished")

        view.session.send_break = refuse
        await _to_menus(pilot, app)
        await pilot.press("b", "b")
        await _until(pilot, lambda: any(m.startswith("Break not sent") for m, _ in seen))
        assert [s for m, s in seen if m.startswith("Break not sent")] == ["error"]
        assert "device vanished" in [m for m, _ in seen if m.startswith("Break not sent")][0]


async def test_editor_cancel_without_a_change_closes_at_once(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await _until(pilot, lambda: isinstance(app.screen, ProfileEditor))
        await pilot.click("#cancel")
        await _until(pilot, lambda: not isinstance(app.screen, ProfileEditor))
        assert not isinstance(app.screen, ConfirmDialog)


async def test_editor_cancel_after_a_change_asks_before_discarding(tmp_path):
    store = _store_with(tmp_path, _loop_profile())
    app = TermdeckTUI(store=store)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await _until(pilot, lambda: isinstance(app.screen, ProfileEditor))
        editor = app.screen
        editor.query_one("#profile--name", Input).value = "typed"
        await pilot.click("#cancel")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        assert "Discard changes?" in str(app.screen.query_one(".dialog-title").render())
        await pilot.press("escape")  # keep editing
        await _until(pilot, lambda: app.screen is editor)
        assert editor.query_one("#profile--name", Input).value == "typed"
        await pilot.press("escape")  # Esc is Cancel in the editor
        await _until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        await pilot.click("#ok")
        await _until(pilot, lambda: not isinstance(app.screen, (ProfileEditor, ConfirmDialog)))
        assert list(store.load()) == ["loop"]


async def test_editor_change_that_is_undone_is_no_change(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await _until(pilot, lambda: isinstance(app.screen, ProfileEditor))
        name = app.screen.query_one("#profile--name", Input)
        name.value = "x"
        name.value = ""
        await pilot.click("#cancel")
        await _until(pilot, lambda: not isinstance(app.screen, ProfileEditor))
        assert not isinstance(app.screen, ConfirmDialog)


async def test_saving_with_an_empty_host_focuses_and_marks_the_host_field(tmp_path):
    store = _store_with(tmp_path, _loop_profile())
    app = TermdeckTUI(store=store)
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await _until(pilot, lambda: isinstance(app.screen, ProfileEditor))
        editor = app.screen
        editor.query_one("#profile--name", Input).value = "box"
        await pilot.click("#save")
        await _until(pilot, lambda: app.focused is not None and app.focused.id == "ssh--host")
        assert editor.query_one("#field-ssh--host").has_class("invalid")
        assert "host" in str(editor.query_one("#error", Label).render())
        assert "box" not in store.load()
        # Typing a host and saving again clears the mark and saves.
        editor.query_one("#ssh--host", Input).value = "box.example"
        await pilot.click("#save")
        await _until(pilot, lambda: not isinstance(app.screen, ProfileEditor))
        assert store.get("box").ssh.host == "box.example"


async def test_a_mark_moves_to_the_next_invalid_field(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await _until(pilot, lambda: isinstance(app.screen, ProfileEditor))
        editor = app.screen
        editor.query_one("#profile--name", Input).value = "box"
        await pilot.click("#save")
        await _until(pilot, lambda: editor.query_one("#field-ssh--host").has_class("invalid"))
        editor.query_one("#ssh--host", Input).value = "box.example"
        editor.query_one("#advanced", Switch).value = True
        await pilot.pause()
        editor.query_one("#ssh--ciphers", Input).value = "rot13"
        await pilot.click("#save")
        await _until(pilot, lambda: editor.query_one("#field-ssh--ciphers").has_class("invalid"))
        assert not editor.query_one("#field-ssh--host").has_class("invalid")
        assert app.focused is editor.query_one("#ssh--ciphers", Input)
        assert "rot13" in str(editor.query_one("#error", Label).render())


async def test_an_invalid_advanced_option_opens_the_advanced_section(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await _until(pilot, lambda: isinstance(app.screen, ProfileEditor))
        editor = app.screen
        editor.query_one("#advanced", Switch).value = True
        await pilot.pause()
        editor.query_one("#profile--name", Input).value = "box"
        editor.query_one("#ssh--host", Input).value = "box.example"
        editor.query_one("#ssh--ciphers", Input).value = "rot13"
        editor.query_one("#advanced", Switch).value = False
        await pilot.pause()
        await pilot.click("#save")
        await _until(pilot, lambda: app.focused is not None and app.focused.id == "ssh--ciphers")
        assert editor.query_one("#advanced", Switch).value is True


async def test_an_invalid_serial_device_marks_its_field(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 50)) as pilot:
        await pilot.press("n")
        await _until(pilot, lambda: isinstance(app.screen, ProfileEditor))
        editor = app.screen
        await _until(pilot, lambda: editor.query("#profile--kind SelectOverlay"))
        editor.query_one("#profile--kind", Select).value = "serial"
        editor.query_one("#profile--name", Input).value = "board"
        editor.query_one("#serial--device", Input).value = "http://nope"
        await pilot.pause()
        await pilot.click("#save")
        await _until(pilot, lambda: app.focused is not None and app.focused.id == "serial--device")
        assert editor.query_one("#field-serial--device").has_class("invalid")


async def test_renaming_a_saved_profile_updates_the_open_tabs(tmp_path):
    store = _store_with(tmp_path, _loop_profile())
    app = TermdeckTUI(store=store)
    async with app.run_test(size=(120, 50)) as pilot:
        view = await _open(app, pilot, store.get("loop"))
        view.controller.saved = True  # as opened from the list
        await _to_menus(pilot, app)
        await pilot.press("e")
        await _until(pilot, lambda: isinstance(app.screen, ProfileEditor))
        app.screen.query_one("#profile--name", Input).value = "renamed"
        await pilot.click("#save")
        await _until(pilot, lambda: not isinstance(app.screen, ProfileEditor))
        assert view.controller.profile.name == "renamed"
        assert "renamed" in _tab_label(app, view)
        assert list(store.load()) == ["renamed"]
        # A reconnect finds the profile under its new name.
        await view.disconnect()
        view.connect()
        await _until(pilot, lambda: view.status == "connected")
        assert view.profile.name == "renamed"


async def test_renaming_does_not_touch_tabs_of_other_profiles(tmp_path):
    a, b = _loop_profile(), _loop_profile()
    a.name, b.name = "a", "b"
    store = _store_with(tmp_path, a, b)
    app = TermdeckTUI(store=store)
    async with app.run_test(size=(120, 50)) as pilot:
        other = await _open(app, pilot, store.get("b"))
        other.controller.saved = True
        await _to_menus(pilot, app)
        app.main.query_one("#profiles").index = 0  # "a" (profiles are sorted by name)
        await pilot.press("e")
        await _until(pilot, lambda: isinstance(app.screen, ProfileEditor))
        app.screen.query_one("#profile--name", Input).value = "a2"
        await pilot.click("#save")
        await _until(pilot, lambda: not isinstance(app.screen, ProfileEditor))
        assert other.controller.profile.name == "b"


async def test_s_hides_and_shows_the_sidebar(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 40)) as pilot:
        sidebar = app.query_one("#sidebar")
        assert not sidebar.has_class("hidden") and sidebar.display
        await pilot.press("s")
        await _until(pilot, lambda: sidebar.has_class("hidden"))
        assert not sidebar.display
        await pilot.press("s")
        await _until(pilot, lambda: not sidebar.has_class("hidden"))
        assert sidebar.display and app.focused.id == "profiles"


async def test_hiding_the_sidebar_puts_the_keyboard_in_the_terminal(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot)
        await _to_menus(pilot, app)
        await pilot.press("s")
        await _until(pilot, lambda: app.query_one("#sidebar").has_class("hidden"))
        assert app.focused is view
        await pilot.press("s")  # typed into the terminal, not a shortcut any more
        await _until(pilot, lambda: "s" in view._vt.display[0])
        assert app.query_one("#sidebar").has_class("hidden")  # still hidden: the key went to the device


def _footer_keys(app):
    return {str(key.description) for key in app.query("FooterKey") if key.display}


async def test_footer_offers_menus_in_the_terminal_and_terminal_in_the_menus(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 40)) as pilot:
        await _open(app, pilot)
        await _until(pilot, lambda: "Menus" in _footer_keys(app))
        assert "Terminal" not in _footer_keys(app)
        await _to_menus(pilot, app)
        await _until(pilot, lambda: "Terminal" in _footer_keys(app))
        assert "Menus" not in _footer_keys(app)


async def test_find_shows_the_line_above_and_again_the_one_further_up(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(80, 12)) as pilot:
        view = await _open(app, pilot)
        for n in range(40):
            view._feed(f"line {n} {'marker' if n in (5, 20) else ''}\r\n".encode())
        assert view.find("MARKER")  # case ignored
        first = view._offset
        assert first > 0 and "line 20 marker" in view._text(view._lines()[view._line_at(0)])
        assert view.find("marker")
        assert view._offset > first and "line 5 marker" in view._text(view._lines()[view._line_at(0)])
        assert not view.find("marker")  # nothing further up
        assert not view.find("absent")


async def test_find_key_prefills_the_last_search(tmp_path):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot)
        view.last_find = "error"
        await _to_menus(pilot, app)
        await pilot.press("f")
        await _until(pilot, lambda: isinstance(app.screen, InputDialog))
        assert app.screen.query_one("#answer", Input).value == "error"


async def test_dragging_selects_text_and_copies_it(tmp_path, monkeypatch):
    app = TermdeckTUI(store=_store_with(tmp_path, _loop_profile()))
    copied = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test(size=(120, 40)) as pilot:
        view = await _open(app, pilot)
        view._feed(b"hello world\r\nsecond line\r\n")
        top = view._line_at(0)
        view._selection = ((top, 6), (top + 1, 5))
        assert view.selected_text() == "world\nsecond"
        assert view._selected(top) == (6, view._vt.columns) and view._selected(top + 1) == (0, 6)
        await pilot.press("x")  # a key clears the selection
        assert view._selection is None
        view._selection = ((top, 0), (top, 4))
        view.on_mouse_up(None)
        assert copied == ["hello"]


async def test_port_picker_lists_the_ports_and_enter_fills_the_device(tmp_path, monkeypatch):
    import termdeck.tui.app as tui_app

    ports = [{"device": "/dev/ttyUSB7", "description": "CP2102", "usb_id": "10c4:ea60 serial=abc"}]
    monkeypatch.setattr(tui_app, "available_ports", lambda: ports)
    app = TermdeckTUI(store=_store_with(tmp_path))
    async with app.run_test(size=(120, 50)) as pilot:
        await app.action_new_profile()  # no profiles: focus is in quick connect, where n is typed
        await _until(pilot, lambda: isinstance(app.screen, ProfileEditor))
        editor = app.screen
        await _until(pilot, lambda: editor.query("#profile--kind SelectOverlay"))
        editor.query_one("#profile--kind", Select).value = "serial"
        await pilot.pause()
        editor.query_one("#pick--serial--device", Button).press()
        await _until(pilot, lambda: isinstance(app.screen, tui_app.PortPicker) and app.screen.query("ListItem"))
        assert "CP2102" in str(app.screen.query_one("ListItem Label").render())
        await pilot.press("enter")
        await _until(pilot, lambda: app.screen is editor)
        assert editor.query_one("#serial--device", Input).value == "/dev/ttyUSB7"


async def test_files_panel_lists_and_downloads(tmp_path, ssh_server, monkeypatch):
    from termdeck.controller import SessionController
    from termdeck.tui import files

    from .conftest import FakePrompter

    (ssh_server.root / "notes.txt").write_text("hi")
    (ssh_server.root / "sub").mkdir()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    monkeypatch.setattr(files, "download_dir", lambda: downloads)
    controller = SessionController(ssh_server.profile(), FakePrompter())
    assert await controller.connect()
    app = TermdeckTUI(store=_store_with(tmp_path))
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            app.push_screen(files.FilesScreen(controller.session.sftp(), "test"))
            await _until(pilot, lambda: len(app.screen.query(files.EntryItem)) == 2)
            names = [item.entry.name for item in app.screen.query(files.EntryItem)]
            assert names == ["sub", "notes.txt"]  # folders first
            view = app.screen.query_one("#files-list", ListView)
            view.index = 1
            await pilot.press("enter")
            await _until(pilot, lambda: (downloads / "notes.txt").exists())
            assert (downloads / "notes.txt").read_text() == "hi"
    finally:
        await controller.close()
