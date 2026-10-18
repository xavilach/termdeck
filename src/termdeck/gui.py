"""Desktop window: the web UI in a native webview (pywebview).

The server runs on its own thread and event loop; the GUI toolkit needs the main thread.
"""

import asyncio
import json
import sys
import threading

from .config import atomic_write, config_dir, data_dir

GEOMETRY_FILE = "window.json"
MIN_SIZE = (900, 560)
# The page's background in each system theme (app.css --bg), shown before the page draws.
BACKGROUND = {True: "#0f1218", False: "#ffffff"}


class BackgroundServer:
    """The web server on a daemon thread. `url` is set once it listens."""

    def __init__(self):
        self.url: str | None = None
        self.sessions: dict = {}  # the server's open terminals (read from the GUI thread)
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, name="termdeck-server", daemon=True)

    def start(self, timeout: float = 15) -> str:
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("web server did not start")
        if self._error:
            raise self._error
        return self.url

    def stop(self, timeout: float = 5) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout)

    def _run(self) -> None:
        asyncio.run(self._main())

    async def _main(self) -> None:
        from .web.server import sessions_key, start

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        try:
            runner, self.url = await start()
            self.sessions = runner.app[sessions_key]
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            await runner.cleanup()


# The WebView2 runtime's id in the Edge updater's registry keys.
WEBVIEW2 = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def has_webview2() -> bool:
    """True when the Edge WebView2 runtime is installed (machine-wide or for this user)."""
    import winreg

    places = [(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\{WEBVIEW2}"),
              (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\{WEBVIEW2}"),
              (winreg.HKEY_CURRENT_USER, rf"Software\{WEBVIEW2}")]
    for root, key in places:
        try:
            with winreg.OpenKey(root, key) as handle:
                version, _ = winreg.QueryValueEx(handle, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def system_dark() -> bool:
    """Whether the system uses a dark theme for apps (Windows); dark elsewhere, where it cannot be read."""
    try:
        import winreg
    except ImportError:  # not Windows
        return True
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except OSError:  # Windows 10 before 1809 has no app theme: light
        return False
    return not light


def load_size() -> tuple[int, int]:
    """The window size saved when it last closed. Only the size: the position could be on a screen
    that is no longer there."""
    try:
        data = json.loads((config_dir() / GEOMETRY_FILE).read_text(encoding="utf-8"))
        width, height = int(data["width"]), int(data["height"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError):  # OverflowError: int(Infinity)
        return 1280, 800
    return max(MIN_SIZE[0], min(width, 10000)), max(MIN_SIZE[1], min(height, 10000))


def _scale() -> float:
    """Windows display scaling: pywebview reports the window in device pixels but sizes it in logical ones."""
    try:
        import ctypes

        return ctypes.windll.shcore.GetScaleFactorForDevice(0) / 100 or 1.0
    except (AttributeError, OSError):
        return 1.0


def save_size(window) -> None:
    try:
        scale = _scale()
        width, height = int(window.width / scale), int(window.height / scale)
        if width < MIN_SIZE[0] or height < MIN_SIZE[1]:
            return  # minimised: keep the last real size
        atomic_write(config_dir() / GEOMETRY_FILE, json.dumps({"width": width, "height": height}))
    except (AttributeError, TypeError, ValueError, ZeroDivisionError, OSError):
        pass  # a nicety: never stops the window from closing


def desktop_functions(window) -> list:
    """What the page may ask of the desktop window (window.pywebview.api in app.js).

    Plain functions given to window.expose, not a js_api object: pywebview resolves dotted
    names on a js_api object with getattr, private ones included, so page script could walk
    from it to the window and on to os.system. Exposed functions are found by exact name only.
    """

    def toggle_fullscreen() -> None:
        window.toggle_fullscreen()

    def set_on_top(on: bool) -> bool:
        window.on_top = bool(on)
        return bool(window.on_top)

    return [toggle_fullscreen, set_on_top]


def run() -> None:
    try:
        import webview
    except ImportError:
        raise ValueError("the desktop window needs pywebview: install termdeck[gui], or use `termdeck web`") from None
    options = {}
    if sys.platform == "win32":
        # Without it pywebview falls back to Internet Explorer, which cannot run the UI.
        if not has_webview2():
            raise ValueError("the desktop window needs the Microsoft Edge WebView2 Runtime "
                             "(https://go.microsoft.com/fwlink/p/?LinkId=2124703), or use `termdeck web`")
        options["gui"] = "edgechromium"

    webview.settings["ALLOW_DOWNLOADS"] = True  # SFTP downloads; WebView2 shows its Save As dialog
    server = BackgroundServer()
    url = server.start() + "&desktop=1"
    width, height = load_size()
    window = webview.create_window("termdeck", url, width=width, height=height, min_size=MIN_SIZE,
                                   text_select=True, background_color=BACKGROUND[system_dark()])
    window.expose(*desktop_functions(window))

    confirmed = threading.Event()

    def question() -> tuple[str, str]:
        count = len(server.sessions)
        return ("Close termdeck?", f"{count} session{'s are' if count > 1 else ' is'} still connected. "
                                   "Click OK to close them and quit.")

    def ask_then_close() -> None:
        if window.create_confirmation_dialog(*question()):
            confirmed.set()
            save_size(window)
            window.destroy()

    def closing() -> bool:
        """Like PuTTY: ask before closing only while sessions are connected. False keeps the window."""
        if confirmed.is_set() or not server.sessions:
            save_size(window)
            return True
        if sys.platform == "win32":  # the WinForms dialog runs right here
            if window.create_confirmation_dialog(*question()):
                save_size(window)
                return True
            return False
        # GTK and Cocoa queue the dialog on the GUI thread, which is running this handler:
        # asking from here would wait forever. Keep the window, ask from a helper thread.
        threading.Thread(target=ask_then_close, daemon=True).start()
        return False

    window.events.closing += closing
    # Not private_dir: WebView2 sets its own access entries in its folder (under the per-user LOCALAPPDATA).
    storage = data_dir() / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        # Not private: keeps UI preferences (advanced toggle) between runs.
        webview.start(private_mode=False, storage_path=str(storage), **options)
    finally:
        server.stop()
