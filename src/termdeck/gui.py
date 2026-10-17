"""Desktop window: the web UI in a native webview (pywebview).

The server runs on its own thread and event loop; the GUI toolkit needs the main thread.
"""

import asyncio
import threading

from .config import config_dir, private_dir


class BackgroundServer:
    """The web server on a daemon thread. `url` is set once it listens."""

    def __init__(self):
        self.url: str | None = None
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
        from .web.server import start

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        try:
            runner, self.url = await start()
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            await runner.cleanup()


def run() -> None:
    try:
        import webview
    except ImportError:
        raise ValueError("the desktop window needs pywebview: install termdeck[gui], or use `termdeck web`") from None

    server = BackgroundServer()
    url = server.start()
    webview.create_window("termdeck", url, width=1280, height=800, min_size=(640, 400),
                          text_select=True, background_color="#0f1218")
    storage = private_dir(config_dir() / "webview")
    try:
        # Not private: keeps UI preferences (advanced toggle) between runs.
        webview.start(private_mode=False, storage_path=str(storage))
    finally:
        server.stop()
