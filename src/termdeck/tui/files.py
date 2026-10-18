"""File transfer for an SSH tab (core: sftp.py): browse the remote side, download, upload, delete."""

import sys
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView

from ..config import user_path
from ..errors import SFTPFailure
from ..sftp import SFTP, Entry
from ..session import describe_error, printable


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def download_dir() -> Path:
    """Where downloads go: the user's Downloads folder (wherever Windows has moved it), or the home folder."""
    downloads = _windows_downloads() or Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def _windows_downloads() -> Path | None:
    """The Downloads known folder (it may be redirected to OneDrive or another drive)."""
    if sys.platform != "win32":
        return None
    import ctypes
    import uuid
    from ctypes import wintypes

    folder = uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}")  # FOLDERID_Downloads
    guid = (ctypes.c_byte * 16).from_buffer_copy(folder.bytes_le)
    path = wintypes.LPWSTR()
    try:
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path)) != 0:
            return None
        return Path(path.value)
    except OSError:
        return None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(path)


class EntryItem(ListItem):
    def __init__(self, entry: Entry):
        mark = "/" if entry.is_dir else "@" if entry.is_link else ""
        size = "" if entry.is_dir else _size(entry.size)
        # Remote names are shown as plain text, without control characters.
        super().__init__(Label(Text.assemble((printable(entry.name) + mark, "bold" if entry.is_dir else ""),
                                             "  ", (size, "dim"))))
        self.entry = entry


class FilesScreen(ModalScreen[None]):
    """Enter opens a folder or downloads a file; U uploads; Backspace goes up; Del deletes."""

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("backspace", "up", "Parent folder"),
        Binding("u", "upload", "Upload"),
        Binding("delete", "delete", "Delete"),
        Binding("f5", "refresh", "Refresh"),
    ]

    def __init__(self, sftp: SFTP, name: str):
        super().__init__()
        self.sftp, self._name = sftp, name
        self.path = "."

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog files"):
            yield Label(f"Files on {self._name}", classes="dialog-title", markup=False)
            yield Input(id="files-path", placeholder="Remote folder")
            yield ListView(id="files-list")
            yield Label("", id="files-status")
            with Horizontal(classes="buttons"):
                yield Button("Upload…", id="upload")
                yield Button("Close", id="close")

    def on_mount(self) -> None:
        self.load(".")

    def _status(self, text: str, error: bool = False) -> None:
        self.query_one("#files-status", Label).update(Text(text, style="red" if error else "dim"))

    @work(exclusive=True)
    async def load(self, path: str) -> None:
        self._status("Loading…")
        try:
            self.path, entries = await self.sftp.list(path)
        except (SFTPFailure, OSError) as exc:
            self._status(describe_error(exc), error=True)
            return
        self.query_one("#files-path", Input).value = self.path
        view = self.query_one("#files-list", ListView)
        await view.clear()
        await view.extend(EntryItem(e) for e in entries)
        view.index = 0 if entries else None
        self._status(f"{len(entries)} items. Enter: open or download to {download_dir()}  U: upload  Del: delete")
        view.focus()

    @on(Input.Submitted, "#files-path")
    def go(self, event: Input.Submitted) -> None:
        self.load(event.value or ".")

    def action_up(self) -> None:
        if not isinstance(self.focused, Input):
            self.load(self.path.rstrip("/").rpartition("/")[0] or "/")

    def action_refresh(self) -> None:
        self.load(self.path)

    @on(ListView.Selected, "#files-list")
    def open_entry(self, event: ListView.Selected) -> None:
        entry = event.item.entry
        if entry.is_dir:
            self.load(entry.path)
        else:
            self.transfer(entry.path, download_dir(), download=True)

    @work(exclusive=False)
    async def transfer(self, remote: str, local: Path, download: bool) -> None:
        verb = "Downloading" if download else "Uploading"
        self._status(f"{verb} {printable(remote if download else local.name)}…")
        def progress(name, done: int, total: int) -> None:
            self._status(f"{verb} {printable(str(name))}: {_size(done)} of {_size(total)}")

        try:
            if download:
                await self.sftp.get(remote, local, progress=progress)
            else:
                await self.sftp.put(local, remote, progress=progress)
        except FileExistsError as exc:
            self._status(f"{exc}: already exists, nothing replaced", error=True)
            return
        except (SFTPFailure, OSError) as exc:
            self._status(describe_error(exc), error=True)
            return
        self._status(f"Downloaded to {local}" if download else f"Uploaded {local.name}")
        if not download:
            self.load(self.path)

    @on(Button.Pressed, "#upload")
    def action_upload(self) -> None:
        from .app import InputDialog

        def chosen(text: str | None) -> None:
            if text:
                self.transfer(self.path, user_path(text.strip()), download=False)

        self.app.push_screen(InputDialog("Upload", f"Local file to upload into {self.path}:"), callback=chosen)

    def action_delete(self) -> None:
        from .app import ConfirmDialog

        item = self.query_one("#files-list", ListView).highlighted_child
        if not isinstance(item, EntryItem):
            return
        entry = item.entry
        what = f"the folder {entry.name} and everything in it" if entry.is_dir else entry.name

        async def done(confirmed: bool) -> None:
            if confirmed:
                try:
                    await self.sftp.remove(entry.path, recursive=entry.is_dir)
                except (SFTPFailure, OSError) as exc:
                    self._status(describe_error(exc), error=True)
                    return
                self.load(self.path)

        self.app.push_screen(ConfirmDialog("Delete", f"Delete {printable(what)}? This cannot be undone.", "Delete",
                                           danger=True), callback=done)

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        self.dismiss(None)
