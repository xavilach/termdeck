"""Location of user data (profiles, known hosts)."""

import os
import sys
from pathlib import Path


def config_dir() -> Path:
    """Return the termdeck data directory. TERMDECK_HOME overrides it."""
    if env := os.environ.get("TERMDECK_HOME"):
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "termdeck"


def private_dir(path: Path) -> Path:
    """Create `path` (and parents) readable by the owner only."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)  # also tighten a directory that already existed
    return path


def open_private(path: Path, mode: str = "w"):
    """Open a file that only the owner can read. Modes: "w", "a", "ab"..."""
    private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if "a" in mode else os.O_TRUNC)
    fd = os.open(path, flags | getattr(os, "O_BINARY", 0), 0o600)
    if os.name == "posix":
        os.fchmod(fd, 0o600)  # the mode above only applies to new files
    return os.fdopen(fd, mode, **({} if "b" in mode else {"encoding": "utf-8"}))
