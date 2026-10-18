import sys
import types
import urllib.request

import pytest

from termdeck import gui


def test_background_server_serves_and_stops():
    server = gui.BackgroundServer()
    url = server.start()
    assert url.startswith("http://127.0.0.1:") and "token=" in url
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    with opener.open(url) as response:  # token login sets the cookie and redirects to the page
        assert b"<title>termdeck</title>" in response.read()
    server.stop()
    assert not server._thread.is_alive()


def test_run_opens_window_and_stops_server(monkeypatch):
    calls = {}
    class Event(list):  # pywebview events take handlers with +=
        def __iadd__(self, handler):
            self.append(handler)
            return self

    window = types.SimpleNamespace(events=types.SimpleNamespace(closing=Event()), expose=lambda *functions: None)
    fake = types.SimpleNamespace(
        create_window=lambda title, url, **kw: calls.update(title=title, url=url, window=kw) or window,
        start=lambda **kw: calls.update(start=kw),
        settings={"ALLOW_DOWNLOADS": False},
    )
    monkeypatch.setitem(sys.modules, "webview", fake)
    gui.run()
    assert calls["title"] == "termdeck" and calls["url"].startswith("http://127.0.0.1:")
    assert calls["url"].endswith("&desktop=1")  # the page hides what a desktop window cannot do
    assert fake.settings["ALLOW_DOWNLOADS"] is True  # SFTP downloads (pywebview blocks them by default)
    [closing] = window.events.closing
    assert closing() is True  # no session open: closes without asking
    assert calls["start"]["private_mode"] is False


class _Event(list):  # pywebview events take handlers with +=
    def __iadd__(self, handler):
        self.append(handler)
        return self


def _fake_webview(calls, answer=True):
    window = types.SimpleNamespace(
        events=types.SimpleNamespace(closing=_Event()),
        create_confirmation_dialog=lambda title, text: calls.update(asked=(title, text)) or answer,
        expose=lambda *functions: None)
    return types.SimpleNamespace(create_window=lambda *a, **kw: window, start=lambda **kw: None,
                                 settings={}), window


@pytest.mark.skipif(sys.platform != "win32", reason="the registry is Windows only")
def test_has_webview2_returns_a_bool():
    assert isinstance(gui.has_webview2(), bool)


def test_run_on_windows_without_webview2_says_what_to_install(monkeypatch):
    fake, _ = _fake_webview({})
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(gui, "has_webview2", lambda: False)
    started = []
    monkeypatch.setattr(gui.BackgroundServer, "start", lambda self: started.append(1))
    with pytest.raises(ValueError, match="WebView2"):
        gui.run()
    assert started == []  # no server was left running


def test_closing_asks_only_while_sessions_are_open(monkeypatch):
    calls = {}
    fake, window = _fake_webview(calls, answer=False)
    monkeypatch.setitem(sys.modules, "webview", fake)
    servers = []

    class Server(gui.BackgroundServer):
        def __init__(self):
            super().__init__()
            servers.append(self)

        def start(self, timeout=15):
            return "http://127.0.0.1:1/?token=t"

        def stop(self, timeout=5):
            pass

    monkeypatch.setattr(gui, "BackgroundServer", Server)
    monkeypatch.setattr(gui, "has_webview2", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")  # the dialog answers right in the handler there
    gui.run()
    [closing] = window.events.closing
    [server] = servers
    assert closing() is True and "asked" not in calls
    server.sessions["a"] = object()
    assert closing() is False  # the user said no: the window stays
    assert "1 session is" in calls["asked"][1]
    server.sessions["b"] = object()
    calls["asked"] = None
    assert closing() is False and "2 sessions are" in calls["asked"][1]
    window.create_confirmation_dialog = lambda title, text: True
    assert closing() is True


def test_closing_elsewhere_asks_from_a_thread_then_closes(monkeypatch):
    """On GTK and Cocoa the dialog must not be awaited in the closing handler (it would deadlock)."""
    import threading

    calls = {}
    fake, window = _fake_webview(calls, answer=True)
    destroyed = threading.Event()
    window.destroy = destroyed.set
    monkeypatch.setitem(sys.modules, "webview", fake)
    servers = []

    class Server(gui.BackgroundServer):
        def __init__(self):
            super().__init__()
            servers.append(self)

        def start(self, timeout=15):
            return "http://127.0.0.1:1/?token=t"

        def stop(self, timeout=5):
            pass

    monkeypatch.setattr(gui, "BackgroundServer", Server)
    monkeypatch.setattr(sys, "platform", "linux")
    gui.run()
    [closing] = window.events.closing
    servers[0].sessions["a"] = object()
    assert closing() is False  # the window stays while the question is asked
    assert destroyed.wait(5)  # the user said yes: closed from the helper thread
    assert closing() is True  # and the close that follows is not asked again


class _Window:
    on_top = False

    def __init__(self):
        self.toggled = 0

    def toggle_fullscreen(self):
        self.toggled += 1


def test_desktop_functions_toggle_fullscreen_and_pin_the_window():
    class Window:
        on_top = False
        toggled = 0

        def toggle_fullscreen(self):
            self.toggled += 1

    window = Window()
    toggle_fullscreen, set_on_top = gui.desktop_functions(window)
    toggle_fullscreen()
    toggle_fullscreen()
    assert window.toggled == 2
    assert set_on_top(True) is True and window.on_top is True
    assert set_on_top(0) is False and window.on_top is False


def test_run_exposes_functions_not_an_object(monkeypatch):
    """No js_api object: pywebview would let page script walk its attributes (even private ones)."""
    calls = {}
    fake, window = _fake_webview(calls)
    window.toggle_fullscreen = lambda: calls.update(toggled=True)
    window.expose = lambda *functions: calls.update(exposed=functions)
    fake.create_window = lambda title, url, **kw: calls.update(kw=kw) or window
    monkeypatch.setitem(sys.modules, "webview", fake)
    gui.run()
    assert "js_api" not in calls["kw"]
    assert [f.__name__ for f in calls["exposed"]] == ["toggle_fullscreen", "set_on_top"]
    calls["exposed"][0]()
    assert calls["toggled"] is True




# ---- window size, system theme ----

def test_load_size_defaults_without_a_saved_file():
    assert gui.load_size() == (1280, 800)


def test_save_size_then_load_size_round_trips(tmp_path):
    gui.save_size(types.SimpleNamespace(width=1500, height=900))
    assert gui.load_size() == (1500, 900)
    assert (tmp_path / "termdeck" / gui.GEOMETRY_FILE).is_file()


def _write_geometry(tmp_path, text):
    folder = tmp_path / "termdeck"
    folder.mkdir(exist_ok=True)
    (folder / gui.GEOMETRY_FILE).write_text(text)


def test_a_saved_size_is_clamped_to_the_minimum_and_a_sane_maximum(tmp_path):
    _write_geometry(tmp_path, '{"width": 100, "height": 50}')
    assert gui.load_size() == gui.MIN_SIZE
    _write_geometry(tmp_path, '{"width": 10000000, "height": 10000000}')
    assert gui.load_size() == (10000, 10000)


def test_a_minimised_window_does_not_overwrite_the_last_real_size():
    gui.save_size(types.SimpleNamespace(width=1500, height=900))
    gui.save_size(types.SimpleNamespace(width=0, height=0))
    gui.save_size(types.SimpleNamespace(width=160, height=28))
    assert gui.load_size() == (1500, 900)


def test_the_size_is_saved_in_logical_pixels(monkeypatch):
    """pywebview reports device pixels on a scaled display but creates the window in logical ones."""
    monkeypatch.setattr(gui, "_scale", lambda: 1.5)
    gui.save_size(types.SimpleNamespace(width=1800, height=1200))
    assert gui.load_size() == (1200, 800)


@pytest.mark.parametrize("content", [
    "{oops", "[]", "null", "{}", '{"width": "wide", "height": 800}', '{"width": null, "height": 5}',
    "\xff",
])
def test_a_bad_geometry_file_gives_the_default_size(tmp_path, content):
    folder = tmp_path / "termdeck"
    folder.mkdir()
    (folder / gui.GEOMETRY_FILE).write_bytes(content.encode("latin-1"))
    assert gui.load_size() == (1280, 800)


def test_an_infinite_size_in_the_geometry_file_gives_the_default_size(tmp_path):
    _write_geometry(tmp_path, '{"width": 1e999, "height": 800}')
    assert gui.load_size() == (1280, 800)


def test_save_size_never_stops_the_window_closing(tmp_path):
    gui.save_size(types.SimpleNamespace())  # no width
    gui.save_size(types.SimpleNamespace(width=None, height=None))
    gui.save_size(types.SimpleNamespace(width="x", height=1))
    assert not (tmp_path / "termdeck" / gui.GEOMETRY_FILE).exists()


def test_save_size_survives_an_unwritable_folder(tmp_path):
    (tmp_path / "termdeck").write_text("a file where the folder should be")
    gui.save_size(types.SimpleNamespace(width=1000, height=700))  # no exception


def test_run_opens_the_window_at_the_saved_size_and_saves_it_on_close(monkeypatch):
    gui.save_size(types.SimpleNamespace(width=1111, height=777))
    calls = {}
    fake, window = _fake_webview(calls)
    window.width, window.height = 1234, 888
    fake.create_window = lambda title, url, **kw: calls.update(kw=kw) or window
    monkeypatch.setitem(sys.modules, "webview", fake)
    gui.run()
    assert (calls["kw"]["width"], calls["kw"]["height"]) == (1111, 777)
    assert calls["kw"]["min_size"] == gui.MIN_SIZE
    [closing] = window.events.closing
    assert closing() is True
    assert gui.load_size() == (1234, 888)


def test_run_does_not_save_the_size_when_the_user_keeps_the_window(monkeypatch):
    calls = {}
    fake, window = _fake_webview(calls, answer=False)
    window.width, window.height = 1234, 888
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(gui, "has_webview2", lambda: True)
    servers = []

    class Server(gui.BackgroundServer):
        def __init__(self):
            super().__init__()
            servers.append(self)

        def start(self, timeout=15):
            return "http://127.0.0.1:1/?token=t"

        def stop(self, timeout=5):
            pass

    monkeypatch.setattr(gui, "BackgroundServer", Server)
    gui.run()
    [closing] = window.events.closing
    servers[0].sessions["a"] = object()  # a session is open, so the user is asked
    assert closing() is False
    assert gui.load_size() == (1280, 800)  # nothing saved
    window.create_confirmation_dialog = lambda title, text: True
    assert closing() is True
    assert gui.load_size() == (1234, 888)


@pytest.mark.skipif(sys.platform == "win32", reason="the registry is read on Windows")
def test_system_dark_is_true_where_it_cannot_be_read():
    assert gui.system_dark() is True


@pytest.mark.parametrize("light, dark", [(0, True), (1, False)])
def test_system_dark_reads_the_apps_theme(monkeypatch, light, dark):
    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    fake = types.SimpleNamespace(HKEY_CURRENT_USER=object(), OpenKey=lambda *a: Key(),
                                 QueryValueEx=lambda key, name: (light, 4))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert gui.system_dark() is dark


def test_system_dark_is_false_when_the_registry_has_no_app_theme(monkeypatch):
    """Windows 10 before 1809 has no dark mode for apps: light."""
    def missing(*a):
        raise FileNotFoundError("no such key")

    monkeypatch.setitem(sys.modules, "winreg", types.SimpleNamespace(HKEY_CURRENT_USER=object(), OpenKey=missing))
    assert gui.system_dark() is False


def test_the_window_background_follows_the_system_theme(monkeypatch):
    for dark in (True, False):
        calls = {}
        fake, window = _fake_webview(calls)
        fake.create_window = lambda title, url, **kw: calls.update(kw=kw) or window
        monkeypatch.setitem(sys.modules, "webview", fake)
        monkeypatch.setattr(gui, "system_dark", lambda dark=dark: dark)
        gui.run()
        assert calls["kw"]["background_color"] == gui.BACKGROUND[dark]
