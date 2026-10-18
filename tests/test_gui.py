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


