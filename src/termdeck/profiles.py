"""Saved connection profiles, stored as JSON.

Every option carries metadata (label, help, choices, advanced) so each UI can
build its settings form from the same definition. See `describe_options`.
"""

import difflib
import json
import os
import re
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from .config import config_dir, open_private

ForwardKind = Literal["local", "remote", "dynamic"]


def option(default: Any, label: str, help: str = "", *, advanced: bool = False,
           choices: dict | None = None, widget: str = "", kinds: tuple[str, ...] = ("ssh", "serial"),
           hidden: bool = False) -> Any:
    """A profile option.

    `widget` refines the input type (path, secret, multiline). `kinds` lists the
    connection types the option applies to, so UIs hide the others. `hidden`
    options are stored and kept by every UI but never shown in forms.
    """
    meta = {"label": label, "help": help, "advanced": advanced, "choices": choices, "widget": widget,
            "kinds": kinds, "hidden": hidden}
    if isinstance(default, list):
        return field(default_factory=list, metadata=meta)
    return field(default=default, metadata=meta)


@dataclass
class Forward:
    """An SSH port forward. Dynamic forwards (SOCKS) have no destination."""

    kind: ForwardKind
    listen_port: int
    listen_host: str = "localhost"
    dest_host: str = ""
    dest_port: int = 0

    def validate(self) -> None:
        if self.kind not in ("local", "remote", "dynamic"):
            raise ValueError(f"invalid forward type {self.kind!r}")
        if not _is_int(self.listen_port) or not 0 <= self.listen_port < 65536:
            raise ValueError(f"invalid forward listen port {self.listen_port!r}")
        _check_host(self.listen_host, "forward listen host")
        if self.kind != "dynamic":
            if not self.dest_host:
                raise ValueError(f"forward {self} needs a destination host")
            _check_host(self.dest_host, "forward destination host")
            if not _is_int(self.dest_port) or not 0 < self.dest_port < 65536:
                raise ValueError(f"invalid forward destination port {self.dest_port!r}")

    @property
    def public(self) -> bool:
        """True when the listener accepts connections from other machines."""
        return self.listen_host not in LOOPBACK

    def __str__(self) -> str:
        flag = {"local": "L", "remote": "R", "dynamic": "D"}[self.kind]
        listen = f"{self.listen_host}:{self.listen_port}"
        if self.kind == "dynamic":
            return f"-{flag} {listen}"
        return f"-{flag} {listen}:{self.dest_host}:{self.dest_port}"


LOOPBACK = ("localhost", "127.0.0.1", "::1")
# Characters that would change the meaning of a known_hosts line or pattern
# (separators, wildcards, negation, comments, hashed entries, [host]:port, @markers).
_BAD_HOST_CHARS = re.compile(r"[\s,*?!#|\[\]]|^@")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_host(host: str, what: str) -> None:
    if not host or _BAD_HOST_CHARS.search(host):
        raise ValueError(f"invalid {what} {host!r}")


_FORWARD_RE = re.compile(
    r"^(?:(?P<bind>\[[^\]]+\]|[^:]+):)?(?P<port>\d+)"
    r"(?::(?P<host>\[[^\]]+\]|[^:]+):(?P<hostport>\d+))?$"
)


def parse_forward(kind: ForwardKind, spec: str) -> Forward:
    """Parse OpenSSH syntax: [bind:]port:host:hostport, or [bind:]port for dynamic."""
    m = _FORWARD_RE.match(spec.strip())
    if not m or (kind == "dynamic") != (m["host"] is None):
        expected = "[bind:]port" if kind == "dynamic" else "[bind:]port:host:hostport"
        raise ValueError(f"invalid {kind} forward {spec!r}, expected {expected}")
    fwd = Forward(kind, int(m["port"]), (m["bind"] or "localhost").strip("[]"))
    if m["host"]:
        fwd.dest_host = m["host"].strip("[]")
        fwd.dest_port = int(m["hostport"])
    return fwd


@dataclass
class SSHSettings:
    host: str = option("", "Host", "Host name or IP address")
    port: int = option(22, "Port")
    username: str = option("", "Username", "Asked at login when empty")
    key_file: str = option("", "Private key", "Uses the SSH agent and default keys when empty", widget="path")
    forwards: list[Forward] = option([], "Port forwards", "Local (-L), remote (-R) and dynamic SOCKS (-D)")
    remote_command: str = option("", "Remote command", "Run this instead of a login shell", advanced=True)
    jump_host: str = option("", "Jump host", "Connect through [user@]host[:port], like ssh -J", advanced=True)
    keepalive: int = option(0, "Keepalive (s)", "Seconds between keepalives, 0 disables", advanced=True)
    connect_timeout: int = option(15, "Connect timeout (s)", advanced=True)
    compression: bool = option(False, "Compression", "Worth it on slow links only", advanced=True)
    use_agent: bool = option(True, "Use SSH agent", advanced=True)
    agent_forwarding: bool = option(False, "Forward agent", "Lets the server use your agent; trust it first", advanced=True)
    x11_forwarding: bool = option(False, "X11 forwarding", "Needs a local X server", advanced=True)
    env: str = option("", "Environment", "NAME=value per line; the server must accept them", advanced=True, widget="multiline")
    ciphers: str = option("", "Ciphers", "Comma-separated preference list, empty for defaults", advanced=True)
    kex_algs: str = option("", "Key exchange", "Comma-separated preference list, empty for defaults", advanced=True)


@dataclass
class SerialSettings:
    device: str = option("", "Device", "COM3, /dev/ttyUSB0, or a pyserial URL such as rfc2217://host:port")
    baudrate: int = option(115200, "Speed (baud)")
    follow_device: bool = option(True, "Follow the device",
                                 "Find the same USB adapter again when its port name changes (COM3 becomes COM7)")
    usb_id: str = option("", "USB identity", "Recorded when the profile is saved with the adapter plugged in",
                         hidden=True)
    bytesize: int = option(8, "Data bits", choices={5: "5", 6: "6", 7: "7", 8: "8"})
    parity: str = option("N", "Parity", choices={"N": "None", "E": "Even", "O": "Odd", "M": "Mark", "S": "Space"})
    stopbits: float = option(1, "Stop bits", choices={1: "1", 1.5: "1.5", 2: "2"})
    flow: str = option("none", "Flow control", choices={"none": "None", "rtscts": "RTS/CTS", "xonxoff": "XON/XOFF"})
    dtr: bool = option(True, "Assert DTR", "Some boards reset when DTR changes. On Linux and macOS the system "
                       "raises DTR briefly while opening the port, so a reset at connect may still happen", advanced=True)
    rts: bool = option(True, "Assert RTS", advanced=True)


@dataclass
class TerminalSettings:
    term_type: str = option("xterm-256color", "Terminal type", "Sent to the server as $TERM", advanced=True, kinds=("ssh",))
    encoding: str = option("utf-8", "Character set", advanced=True)
    enter_sends: str = option("cr", "Enter sends", choices={"cr": "CR", "crlf": "CR LF", "lf": "LF"}, advanced=True)
    backspace_sends: str = option("del", "Backspace sends", choices={"del": "DEL (^?)", "bs": "BS (^H)"}, advanced=True)
    local_echo: bool = option(False, "Local echo", "Show typed characters yourself, for devices that do not echo", advanced=True)
    line_editing: bool = option(False, "Local line editing",
                                "Edit lines here (arrows, history) and send them on Enter; for devices without a line editor. Turn the device echo off")
    scrollback: int = option(10000, "Scrollback lines", advanced=True)
    log_file: str = option("", "Log file", "Append session output to this file; {name} and {date} are replaced", advanced=True, widget="path")
    font_size: int = option(14, "Font size")
    theme: str = option("dark", "Color theme", choices={"dark": "Dark", "light": "Light", "solarized": "Solarized", "dracula": "Dracula"})


KINDS = {"ssh": "SSH", "serial": "Serial"}


@dataclass
class Profile:
    name: str = option("", "Name")
    kind: str = option("ssh", "Type", choices=KINDS)
    group: str = option("", "Folder", "Groups profiles in the sidebar")
    ssh: SSHSettings = field(default_factory=SSHSettings)
    serial: SerialSettings = field(default_factory=SerialSettings)
    terminal: TerminalSettings = field(default_factory=TerminalSettings)

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("profile name is empty")
        if self.kind not in KINDS:
            raise ValueError(f"unknown profile type {self.kind!r}")
        for section in (self, self.ssh, self.serial, self.terminal):
            for f in fields(section):
                if "label" in f.metadata:
                    _check_option(f, getattr(section, f.name))
        if self.kind == "ssh":
            if not self.ssh.host:
                raise ValueError("ssh profile needs a host")
            _check_host(self.ssh.host, "host")
            if self.ssh.jump_host:
                hostport = self.ssh.jump_host.rpartition("@")[2]
                host = hostport[1:].partition("]")[0] if hostport.startswith("[") else hostport.partition(":")[0]
                _check_host(host, "jump host")
            if not 0 < self.ssh.port < 65536:
                raise ValueError(f"invalid port {self.ssh.port}")
            for fwd in self.ssh.forwards:
                fwd.validate()
        elif not self.serial.device:
            raise ValueError("serial profile needs a device")
        try:
            "".encode(self.terminal.encoding)
        except LookupError:
            raise ValueError(f"unknown character set {self.terminal.encoding!r}") from None

    def summary(self) -> str:
        if self.kind == "ssh":
            user = f"{self.ssh.username}@" if self.ssh.username else ""
            return f"ssh {user}{self.ssh.host}:{self.ssh.port}"
        s = self.serial
        return f"serial {s.device} {s.baudrate} {s.bytesize}{s.parity}{s.stopbits:g}"

    def to_dict(self) -> dict:
        """Only the active connection section is stored, the other one is defaults."""
        return {
            "name": self.name,
            "kind": self.kind,
            "group": self.group,
            self.kind: asdict(getattr(self, self.kind)),
            "terminal": asdict(self.terminal),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Profile":
        """Build and validate a profile. Unknown keys are ignored, missing ones get defaults."""
        if not isinstance(data, dict):
            raise ValueError("profile must be an object")
        for key in ("name", "kind", "group"):
            if not isinstance(data.get(key, ""), str):
                raise ValueError(f"profile {key} must be text")
        sections = {k: data.get(k, {}) for k in ("ssh", "serial", "terminal")}
        if not all(isinstance(v, dict) for v in sections.values()):
            raise ValueError("profile sections must be objects")
        ssh = _known(SSHSettings, sections["ssh"])
        forwards = ssh.get("forwards", [])
        if not isinstance(forwards, list) or not all(isinstance(f, dict) for f in forwards):
            raise ValueError("forwards must be a list of objects")
        ssh["forwards"] = [Forward(**_known(Forward, f)) for f in forwards]
        for fwd in ssh["forwards"]:
            fwd.listen_host = fwd.listen_host or "localhost"  # empty would mean every interface
        profile = cls(
            name=data.get("name", ""),
            kind=data.get("kind", "ssh"),
            group=data.get("group", ""),
            ssh=SSHSettings(**ssh),
            serial=SerialSettings(**_known(SerialSettings, sections["serial"])),
            terminal=TerminalSettings(**_known(TerminalSettings, sections["terminal"])),
        )
        profile.validate()
        return profile


def _check_option(f, value) -> None:
    label = f.metadata["label"].lower()
    choices = f.metadata["choices"]
    if choices:
        if value not in choices:
            raise ValueError(f"invalid {label} {value!r}")
        return
    default = f.default if f.default is not MISSING else f.default_factory()
    expected = type(default)
    if expected is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected is int:
        ok = _is_int(value)
    else:
        ok = isinstance(value, expected)
    if not ok:
        raise ValueError(f"{label} must be {'a number' if expected in (int, float) else expected.__name__}, got {value!r}")
    if expected is int and value < 0:
        raise ValueError(f"{label} cannot be negative")


def _known(cls, data: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def describe_options() -> dict:
    """JSON-ready description of every option, grouped by section, for building forms."""

    def describe(cls) -> list[dict]:
        out = []
        for f in fields(cls):
            if "label" not in f.metadata or f.metadata["hidden"]:
                continue
            default = f.default if f.default is not MISSING else f.default_factory()
            choices = f.metadata["choices"]
            out.append({
                "key": f.name,
                "type": "forwards" if f.name == "forwards" else type(default).__name__,
                "default": default,
                "label": f.metadata["label"],
                "help": f.metadata["help"],
                "advanced": f.metadata["advanced"],
                "widget": f.metadata["widget"],
                "choices": [[k, v] for k, v in choices.items()] if choices else None,
                "kinds": list(f.metadata["kinds"]),
            })
        return out

    return {
        "profile": describe(Profile),
        "ssh": describe(SSHSettings),
        "serial": describe(SerialSettings),
        "terminal": describe(TerminalSettings),
    }


def parse_target(text: str) -> Profile | None:
    """Unsaved profile from user@host[:port] (SSH) or DEVICE[@baud] (serial).

    A bare word is not a target: it is far more likely a mistyped profile name,
    and connecting to it would send a password to whatever host it resolves to.
    SSH targets need a user (bob@host), a port (host:22), or a dotted name or address.
    """
    if m := re.match(r"^(COM\d+|/dev/\S+|\w+://\S*)(?:@(\d+))?$", text, re.I):
        p = Profile(text, "serial")
        p.serial.device = m[1]
        if m[2]:
            p.serial.baudrate = int(m[2])
        return p
    m = re.match(r"^(?:([^@\s]+)@)?(\[[^\]]+\]|[^:\s/]+)(?::(\d+))?$", text)
    if m and (m[1] or m[3] or "." in m[2] or m[2].startswith("[")):
        p = Profile(text, "ssh")
        p.ssh.username = m[1] or ""
        p.ssh.host = m[2].strip("[]")
        if m[3]:
            p.ssh.port = int(m[3])
        return p
    return None


def unknown_target(text: str, names) -> str:
    """Message for a quick-connect text that is neither a profile nor a target."""
    message = f"{text!r} is not a profile"
    if close := difflib.get_close_matches(text, list(names), n=1):
        message += f"; did you mean {close[0]!r}?"
    elif re.fullmatch(r"[\w-]+", text):
        message += f"; for a host without a dot use {text}:22 or user@{text}"
    else:
        message += "; use user@host, host:port, or a serial device like COM3@9600"
    return message


def resolve_target(text: str, profiles: dict[str, "Profile"]) -> "Profile":
    """A saved profile by name, else a quick target such as bob@host or COM3@9600.

    Raises KeyError with a message that suggests what the user may have meant.
    """
    text = text.strip()
    if text in profiles:
        return profiles[text]
    if profile := parse_target(text):
        return profile
    raise KeyError(unknown_target(text, profiles))


class ProfileStore:
    """Profiles kept in one JSON file, written atomically and readable by the owner only.

    An entry that fails validation is skipped by `load` (see `invalid`) but kept
    in the file, so a hand edit gone wrong never loses data.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "profiles.json"
        self.invalid: list[tuple[str, str]] = []  # (name, error) from the last load
        self._invalid_raw: list[dict] = []

    def load(self) -> dict[str, Profile]:
        self.invalid, self._invalid_raw = [], []
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("profiles", []), list):
            raise ValueError(f"{self.path} is not a termdeck profile file")
        profiles = {}
        for raw in data.get("profiles", []):
            try:
                profile = Profile.from_dict(raw)
            except (ValueError, TypeError) as exc:
                self.invalid.append((str(raw.get("name", "?")) if isinstance(raw, dict) else "?", str(exc)))
                self._invalid_raw.append(raw)
                continue
            profiles[profile.name] = profile
        return profiles

    def get(self, name: str) -> Profile:
        try:
            return self.load()[name]
        except KeyError:
            raise KeyError(f"no profile named {name!r}") from None

    def save(self, profile: Profile, replace: str | None = None) -> None:
        """Add or update a profile. `replace` is the old name when renaming."""
        profile.validate()
        profiles = self.load()
        if profile.name != (replace or profile.name) and profile.name in profiles:
            raise ValueError(f"a profile named {profile.name!r} already exists")
        profiles.pop(replace or profile.name, None)
        profiles[profile.name] = profile
        self._write(profiles)

    def delete(self, name: str) -> None:
        profiles = self.load()
        if profiles.pop(name, None) is None:
            raise KeyError(f"no profile named {name!r}")
        self._write(profiles)

    def _write(self, profiles: dict[str, Profile]) -> None:
        entries = [p.to_dict() for p in sorted(profiles.values(), key=lambda p: p.name.lower())]
        data = {"profiles": entries + self._invalid_raw}
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        with open_private(tmp) as f:
            f.write(json.dumps(data, indent=2) + "\n")
        tmp.replace(self.path)
