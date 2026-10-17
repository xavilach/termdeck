import sys
import types
import urllib.request

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
    fake = types.SimpleNamespace(
        create_window=lambda title, url, **kw: calls.update(title=title, url=url),
        start=lambda **kw: calls.update(start=kw),
    )
    monkeypatch.setitem(sys.modules, "webview", fake)
    gui.run()
    assert calls["title"] == "termdeck" and calls["url"].startswith("http://127.0.0.1:")
    assert calls["start"]["private_mode"] is False
