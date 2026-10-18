import asyncio

import pytest
from textual.widgets import Input, Label, Select, Switch

from termdeck.profiles import Profile, ProfileStore, SerialSettings
from termdeck.tui.app import ProfileEditor, TermdeckTUI, text_to_forwards
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
        await pilot.pause(0.5)
        view = app.query_one(TerminalView)
        assert view.status == "connected"
        assert app.focused is view
        await pilot.press("h", "i", "q")  # q must reach the terminal, not quit
        await pilot.pause(0.5)
        assert "hiq" in view._vt.display[0]
        await pilot.press("ctrl+right_square_bracket")
        assert app.focused.id == "profiles"
        await pilot.press("w")  # close tab from menu mode
        await pilot.pause(0.2)
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
        await pilot.pause(0.3)
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
        await pilot.pause(0.3)
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
        await pilot.pause(0.5)
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
        await pilot.pause(0.3)
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
        await pilot.pause(0.3)
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
    return "".join(line.rstrip() + " " for line in view._vt.display)


async def _wait_for_screen(pilot, view, text, timeout=5.0):
    async def poll():
        while text not in _screen_text(view):
            await pilot.pause(0.02)

    await asyncio.wait_for(poll(), timeout)


async def test_many_quick_key_presses_arrive_in_order(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_tab(app.store.get("loop"))
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
        view = app.query_one(TerminalView)
        assert view.status == "connected"
        gone.set()
        await _wait_for_screen(pilot, view, "Session ended: ")
        screen = _screen_text(view)
        assert "Session ended: loop: " in screen and "Enter to reconnect" in screen
        assert view.status == "closed"


async def test_normal_end_has_no_reason(tmp_path):
    app = TermdeckTUI(store=_store(tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        app.open_tab(app.store.get("loop"))
        await pilot.pause(0.5)
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
        await pilot.pause(0.5)
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
