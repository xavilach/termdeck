"""Location of user data (profiles, known hosts) and safe file writing."""

import contextlib
import os
import stat
import sys
import threading
import time
from pathlib import Path


def config_dir() -> Path:
    """Return the termdeck settings directory. TERMDECK_HOME overrides it."""
    if env := os.environ.get("TERMDECK_HOME"):
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "termdeck"


def data_dir() -> Path:
    """Directory for caches that must not roam with the user profile (the webview's storage)."""
    if os.environ.get("TERMDECK_HOME"):
        return config_dir()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "termdeck"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "termdeck"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "termdeck"


def user_path(text: str) -> Path:
    """A path typed or pasted by the user: surrounding quotes ("Copy as path"), ~ and $VAR/%VAR%."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    return Path(os.path.expandvars(text)).expanduser()


def owner_only_acl(path: Path) -> None:
    """Windows: give `path` an access list of its own allowing only the current user, without the
    inherited entries (chmod does nothing there; a TERMDECK_HOME or log folder outside the user
    profile would otherwise let other users read it). Nothing elsewhere, or without pywin32."""
    if sys.platform != "win32":
        return
    try:
        import ntsecuritycon
        import pywintypes
        import win32api
        import win32security
    except ImportError:
        return
    try:
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        user = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION,
                                   win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE,
                                   ntsecuritycon.FILE_ALL_ACCESS, user)
        win32security.SetNamedSecurityInfo(
            str(path), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, dacl, None)
    except pywintypes.error:
        pass  # FAT/exFAT and some network shares have no access lists: nothing more can be done there


_PRIVATE_DIRS: set[Path] = set()  # folders already made private by this process (Windows: an ACL write each)


def private_dir(path: Path) -> Path:
    """Create `path` (and parents) readable by the owner only. For termdeck's own folders."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)  # also tighten a directory that already existed
    elif path not in _PRIVATE_DIRS:  # files created inside inherit it
        owner_only_acl(path)
        _PRIVATE_DIRS.add(path)
    return path


def open_private(path: Path, mode: str = "w", *, own_dir: bool = True):
    """Open a file that only the owner can read. Modes: "w", "a", "ab"...

    `own_dir` is for termdeck's own files: the folder is tightened too, and an
    existing file is made private again. With own_dir=False (logs, in the user's
    folders, maybe shared ones like /tmp) existing permissions are left alone, and
    symlinks, hard links and files owned by someone else are refused.
    """
    if own_dir:
        private_dir(path.parent)
    else:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Outside our folders the file is emptied ("w") only after the checks below.
    truncate = not own_dir and "a" not in mode
    flags = os.O_WRONLY | (os.O_APPEND if "a" in mode else 0 if truncate else os.O_TRUNC) | getattr(os, "O_BINARY", 0)
    if not own_dir:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd, created = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600), True
    except FileExistsError:
        fd, created = os.open(path, flags), False
    try:
        if os.name == "posix":
            if not own_dir and not created:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink > 1:
                    raise PermissionError(f"refusing to write to {path}: not a plain file of yours")
            if own_dir or created:
                os.fchmod(fd, 0o600)
        elif created and not own_dir:  # Windows: a new file outside our folders would inherit the folder's list
            owner_only_acl(path)
        if truncate:
            os.ftruncate(fd, 0)
        # Text files keep LF line endings on every platform, as other tools expect.
        return os.fdopen(fd, mode, **({} if "b" in mode else {"encoding": "utf-8", "newline": "\n"}))
    except BaseException:
        os.close(fd)
        raise


def atomic_write(path: Path, text: str) -> None:
    """Replace `path` with `text` in one step, so a crash never leaves half a file."""
    # Unique per process and thread: two threads writing at once must not share it.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open_private(tmp) as f:
            f.write(text)
    except BaseException:
        tmp.unlink(missing_ok=True)  # disk full: leave no stray temp file
        raise
    for attempt in range(10):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            # Windows refuses while another program (antivirus, backup, another
            # termdeck) has the file open; that lasts milliseconds.
            if sys.platform != "win32" or attempt == 9:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


@contextlib.contextmanager
def file_lock(path: Path):
    """Hold an exclusive lock on `path`.lock across processes, for read-modify-write."""
    lock = path.with_name(path.name + ".lock")
    with open_private(lock, "a") as f:
        if sys.platform == "win32":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)  # retries for about 10 s, then raises OSError
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
