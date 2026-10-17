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
