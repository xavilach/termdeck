"""Desktop window: the web UI in a native webview (pywebview).

The server runs on its own thread and event loop; the GUI toolkit needs the main thread.
"""

import asyncio
import sys
import threading

from .config import data_dir, private_dir


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
    window = webview.create_window("termdeck", url, width=1280, height=800, min_size=(900, 560),
                                   text_select=True, background_color="#0f1218")

    confirmed = threading.Event()

    def question() -> tuple[str, str]:
        count = len(server.sessions)
        return ("Close termdeck?", f"{count} session{'s are' if count > 1 else ' is'} still connected. "
                                   "Click OK to close them and quit.")

    def ask_then_close() -> None:
        if window.create_confirmation_dialog(*question()):
            confirmed.set()
            window.destroy()

    def closing() -> bool:
        """Like PuTTY: ask before closing only while sessions are connected. False keeps the window."""
        if confirmed.is_set() or not server.sessions:
            return True
        if sys.platform == "win32":  # the WinForms dialog runs right here
            return window.create_confirmation_dialog(*question())
        # GTK and Cocoa queue the dialog on the GUI thread, which is running this handler:
        # asking from here would wait forever. Keep the window, ask from a helper thread.
        threading.Thread(target=ask_then_close, daemon=True).start()
        return False

    window.events.closing += closing
    storage = private_dir(data_dir() / "webview")
    try:
        # Not private: keeps UI preferences (advanced toggle) between runs.
        webview.start(private_mode=False, storage_path=str(storage), **options)
    finally:
        server.stop()
