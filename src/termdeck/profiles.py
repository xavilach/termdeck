"""Saved connection profiles, stored as JSON.

Every option carries metadata (label, help, choices, advanced) so each UI can
build its settings form from the same definition. See `describe_options`.
"""

import base64
import difflib
import hashlib
import json
import os
import re
from dataclasses import MISSING, asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Literal

from .config import atomic_write, config_dir, file_lock
from .errors import ProfileError, ProfileNotFound

ForwardKind = Literal["local", "remote", "dynamic"]


def option(default: Any, label: str, help: str = "", *, advanced: bool = False,
           choices: dict | None = None, widget: str = "", kinds: tuple[str, ...] | None = None,
           hidden: bool = False, min: float | None = None, max: float | None = None,
           wide: bool = False, group: str = "", when: tuple[str, tuple] | None = None) -> Any:
    """A profile option.

    `widget` refines the input type (path, secret, multiline, device, forwards).
    `kinds` lists the connection types the option applies to, so UIs hide the
    others; it defaults to the section's type (SSH options apply to SSH only).
    `hidden` options are stored and kept by every UI but never shown in forms.
    `min`/`max` bound numbers. `wide` asks forms for a full-width field.
    `group` puts related advanced options under one subheading. `when` is
    (other option, values): the field is shown only while that option has one of
    them, or, with values None, while that option is set at all.
    """
    meta = {"label": label, "help": help, "advanced": advanced, "choices": choices, "widget": widget,
            "kinds": kinds, "hidden": hidden, "min": min, "max": max, "wide": wide, "group": group,
            "when": when}
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
            raise ProfileError(f"invalid forward type {self.kind!r}")
        if not _is_int(self.listen_port) or not 0 <= self.listen_port < 65536:
            raise ProfileError(f"invalid forward listen port {self.listen_port!r}")
        check_host(self.listen_host, "forward listen host")
        if self.kind != "dynamic":
            if not self.dest_host:
                raise ProfileError(f"forward {self} needs a destination host")
            check_host(self.dest_host, "forward destination host")
            if not _is_int(self.dest_port) or not 0 < self.dest_port < 65536:
                raise ProfileError(f"invalid forward destination port {self.dest_port!r}")

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


def check_host(host: str, what: str) -> None:
    if not host or _BAD_HOST_CHARS.search(host):
        raise ProfileError(f"invalid {what} {host!r}")


_FORWARD_RE = re.compile(
    r"^(?:(?P<bind>\[[^\]]+\]|[^:]+):)?(?P<port>\d+)"
    r"(?::(?P<host>\[[^\]]+\]|[^:]+):(?P<hostport>\d+))?$"
)


def parse_forward(kind: ForwardKind, spec: str) -> Forward:
    """Parse OpenSSH syntax: [bind:]port:host:hostport, or [bind:]port for dynamic."""
    m = _FORWARD_RE.match(spec.strip())
    if not m or (kind == "dynamic") != (m["host"] is None):
        expected = "[bind:]port" if kind == "dynamic" else "[bind:]port:host:hostport"
        raise ProfileError(f"invalid {kind} forward {spec!r}, expected {expected}")
    fwd = Forward(kind, int(m["port"]), (m["bind"] or "localhost").strip("[]"))
    if m["host"]:
        fwd.dest_host = m["host"].strip("[]")
        fwd.dest_port = int(m["hostport"])
    return fwd


@dataclass
class SSHSettings:
    host: str = option("", "Host", "Host name or IP address", wide=True)
    port: int = option(22, "Port", min=1, max=65535)
    username: str = option("", "Username", "Asked at login when empty (see Username from environment)")
    key_file: str = option("", "Private key", "Uses the SSH agent and default keys when empty. "
                           "Encrypted default keys need to be named here to be offered", widget="path")
    forwards: list[Forward] = option([], "Port forwards", "Local (-L), remote (-R) and dynamic SOCKS (-D)",
                                     widget="forwards")

    # Session: what runs on the server
    remote_command: str = option("", "Remote command", "Run this instead of a login shell", advanced=True,
                                 wide=True, group="Session")
    subsystem: str = option("", "Subsystem", "Start this SSH subsystem instead of a shell or command, like ssh -s",
                            advanced=True, group="Session")
    no_shell: bool = option(False, "No shell", "Only open the port forwards, like ssh -N", advanced=True,
                            group="Session")
    pty: bool = option(True, "Allocate a terminal", "Off runs the command with plain pipes, like ssh -T",
                       advanced=True, group="Session")
    tty_modes: str = option("", "Terminal modes", "NAME=number per line, sent with the terminal request "
                            "(e.g. ERASE=127)", advanced=True, widget="multiline", group="Session",
                            when=("pty", (True,)))
    terminal_speed: int = option(38400, "Terminal speed (baud)", "Reported to the server", advanced=True,
                                 min=1, max=4_000_000, group="Session", when=("pty", (True,)))
    env: str = option("", "Environment", "NAME=value per line; the server must accept them", advanced=True,
                      widget="multiline", group="Session")

    # Connection
    jump_host: str = option("", "Jump host", "Connect through [user@]host[:port], like ssh -J", advanced=True,
                            wide=True, group="Connection")
    jump_command: str = option("", "Jump host command", "Reach the host by running this on the jump host instead "
                               "of a forwarded connection: e.g. nc %host %port, or subsystem:NAME (PuTTY's SSH "
                               "proxy that executes a command)", advanced=True, wide=True, group="Connection",
                               when=("jump_host", None))
    connect_timeout: int = option(15, "Connect timeout (s)", "0 waits as long as the system allows",
                                  advanced=True, min=0, max=3600, group="Connection")
    keepalive: int = option(0, "Keepalive (s)", "Seconds between SSH keepalives, 0 disables", advanced=True,
                            min=0, max=86400, group="Connection")
    tcp_keepalive: bool = option(False, "TCP keepalives", "Detect dead links at the network level too",
                                 advanced=True, group="Connection")
    address_family: str = option("any", "Address family", choices={"any": "Any", "ipv4": "IPv4", "ipv6": "IPv6"},
                                 advanced=True, group="Connection")
    compression: bool = option(False, "Compression", "Worth it on slow links only", advanced=True,
                               group="Connection")
    rekey_minutes: int = option(60, "Rekey after (minutes)", "0 never rekeys on time", advanced=True,
                                min=0, max=1440, group="Connection")
    rekey_mb: int = option(1024, "Rekey after (MB)", "0 never rekeys on data", advanced=True, min=0,
                           max=1_000_000, group="Connection")
    use_ssh_config: bool = option(False, "Read ~/.ssh/config", "Apply OpenSSH's settings (Host blocks) too. On "
                                  "Windows write paths in it with / or in quotes", advanced=True, group="Connection")
    share_connection: bool = option(False, "Share the connection", "New tabs to the same login reuse it, in "
                                    "the same termdeck window (like PuTTY connection sharing)", advanced=True,
                                    group="Connection")

    # Proxy (the first hop)
    proxy_type: str = option("none", "Proxy", choices={"none": "None", "socks4": "SOCKS 4", "socks5": "SOCKS 5",
                                                       "http": "HTTP CONNECT", "telnet": "Telnet (command)",
                                                       "command": "Local command"},
                             advanced=True, group="Proxy")
    proxy_host: str = option("", "Proxy host", advanced=True, group="Proxy",
                             when=("proxy_type", ("socks4", "socks5", "http", "telnet")))
    proxy_port: int = option(0, "Proxy port", "0: the usual port (1080 SOCKS, 3128 HTTP, 23 Telnet)", advanced=True,
                             min=0, max=65535, group="Proxy",
                             when=("proxy_type", ("socks4", "socks5", "http", "telnet")))
    proxy_username: str = option("", "Proxy username", "The proxy password is asked when needed, never saved",
                                 advanced=True, group="Proxy", when=("proxy_type", ("socks4", "socks5", "http")))
    proxy_dns: str = option("auto", "Proxy looks up host names", "Auto: yes for SOCKS 5 and HTTP, no for SOCKS 4",
                            choices={"auto": "Auto", "yes": "Yes", "no": "No"}, advanced=True, group="Proxy",
                            when=("proxy_type", ("socks4", "socks5", "http")))
    proxy_telnet_command: str = option("connect %host %port\\n", "Telnet proxy command",
                                       "Sent to the proxy; %host, %port, %user, %pass are replaced, \\n is a new "
                                       "line", advanced=True, wide=True, group="Proxy", when=("proxy_type", ("telnet",)))
    proxy_command: str = option("", "Proxy command", "Run this and talk SSH over it; %host, %port, %user, "
                                "%proxyhost, %proxyport are replaced", advanced=True, wide=True, group="Proxy",
                                when=("proxy_type", ("command",)))
    proxy_exclude: str = option("", "No proxy for", "Hosts, *.domain or networks (10.0.0.0/8), separated by "
                                "commas or spaces", advanced=True, group="Proxy",
                                when=("proxy_type", ("socks4", "socks5", "http", "telnet", "command")))
    proxy_local: bool = option(False, "Proxy local hosts too", "localhost and 127.0.0.1 normally connect directly",
                               advanced=True, group="Proxy",
                               when=("proxy_type", ("socks4", "socks5", "http", "telnet", "command")))

    # Authentication
    username_from_env: bool = option(False, "Username from environment", "Use your local user name when "
                                     "Username is empty, instead of asking", advanced=True, group="Authentication")
    certificate: str = option("", "Certificate", "OpenSSH certificate for the private key", advanced=True,
                              widget="path", group="Authentication")
    use_agent: bool = option(True, "Use SSH agent", advanced=True, group="Authentication")
    kbdint_auth: bool = option(True, "Keyboard-interactive login", "Answer the server's questions (one-time "
                               "codes, PAM)", advanced=True, group="Authentication")
    no_trivial_auth: bool = option(False, "Refuse trivial login", "Abort when the server lets you in without a "
                                   "password or key: it may be a fake (like plink -no-trivial-auth)", advanced=True,
                                   group="Authentication")
    gssapi_auth: bool = option(True, "GSSAPI/Kerberos login", "Single sign-on with a Kerberos ticket "
                               "(SSPI on Windows)", advanced=True, group="Authentication")
    gssapi_kex: bool = option(True, "GSSAPI key exchange", advanced=True, group="Authentication",
                              when=("gssapi_auth", (True,)))
    gssapi_delegate: bool = option(False, "Delegate Kerberos credentials", "Lets the server act as you; "
                                   "trust it first", advanced=True, group="Authentication",
                                   when=("gssapi_auth", (True,)))

    # Host keys
    trusted_keys: str = option("", "Trusted host keys", "When set, only these keys are accepted, without asking "
                               "and without known_hosts (like plink -hostkey). One per line: SHA256:..., MD5 hex, "
                               "or a public key line", advanced=True, widget="multiline", group="Host keys")
    logical_host: str = option("", "Logical host name", "Look up the host key under this name (a host reached "
                               "through a tunnel, or with a changing address)", advanced=True, group="Host keys")
    hostkey_algs: str = option("", "Host key types", "Comma-separated preference list (e.g. ssh-ed25519), "
                               "empty for defaults", advanced=True, group="Host keys")

    # Algorithms
    ciphers: str = option("", "Ciphers", "Comma-separated preference list, empty for defaults", advanced=True,
                          group="Algorithms")
    kex_algs: str = option("", "Key exchange", "Comma-separated preference list, empty for defaults", advanced=True,
                           group="Algorithms")
    mac_algs: str = option("", "MACs", "Comma-separated preference list, empty for defaults", advanced=True,
                           group="Algorithms")
    signature_algs: str = option("", "Login signature types", "Comma-separated, for keys and certificates; e.g. "
                                 "ssh-rsa for old servers that refuse rsa-sha2 certificates (OpenSSH 7.2-7.7)",
                                 advanced=True, group="Algorithms")

    # Forwarding to the server
    agent_forwarding: bool = option(False, "Forward agent", "Lets the server use your agent; trust it first. "
                                    "Needs 'Use SSH agent'", advanced=True, group="Agent and X11 forwarding")
    x11_forwarding: bool = option(False, "X11 forwarding", "Needs a local X server", advanced=True,
                                  group="Agent and X11 forwarding")
    x11_display: str = option("", "X display", "Local X display (empty: $DISPLAY; on Windows localhost:0 for "
                              "VcXsrv or X410)", advanced=True, group="Agent and X11 forwarding",
                              when=("x11_forwarding", (True,)))
    x11_auth_file: str = option("", "X authority file", "Empty: ~/.Xauthority", advanced=True, widget="path",
                                group="Agent and X11 forwarding", when=("x11_forwarding", (True,)))


# pyserial URL schemes allowed as a device. Others are refused: spy:// writes
# everything typed (passwords included) to a file, hwgrep:// may pick any port.
SERIAL_URL_SCHEMES = ("rfc2217", "socket", "loop", "cp2110")


@dataclass
class SerialSettings:
    device: str = option("", "Device", "COM3, /dev/ttyUSB0, or rfc2217://host:port or socket://host:port "
                         "(network ports are not encrypted)", widget="device", wide=True)
    baudrate: int = option(115200, "Speed (baud)", min=1, max=100_000_000)
    follow_device: bool = option(True, "Follow the device",
                                 "Find the same USB adapter again when its port name changes (COM3 becomes COM7)")
    usb_id: str = option("", "USB identity", "Recorded when the profile is saved with the adapter plugged in",
                         hidden=True)
    bytesize: int = option(8, "Data bits", choices={5: "5", 6: "6", 7: "7", 8: "8"})
    parity: str = option("N", "Parity", choices={"N": "None", "E": "Even", "O": "Odd", "M": "Mark", "S": "Space"})
    stopbits: float = option(1, "Stop bits", choices={1: "1", 1.5: "1.5", 2: "2"})
    flow: str = option("none", "Flow control", "DSR/DTR works on Windows only",
                       choices={"none": "None", "rtscts": "RTS/CTS", "dsrdtr": "DSR/DTR", "xonxoff": "XON/XOFF"})
    dtr: bool = option(True, "Assert DTR", "Some boards reset when DTR changes. The system or the driver may "
                       "still raise DTR briefly while opening the port, and on Windows many drivers drop it "
                       "on close", advanced=True)
    rts: bool = option(True, "Assert RTS", advanced=True)


@dataclass
class TerminalSettings:
    term_type: str = option("xterm-256color", "Terminal type", "Sent to the server as $TERM", advanced=True, kinds=("ssh",))
    encoding: str = option("utf-8", "Character set", advanced=True)
    enter_sends: str = option("cr", "Enter sends", choices={"cr": "CR", "crlf": "CR LF", "lf": "LF"}, advanced=True)
    backspace_sends: str = option("del", "Backspace sends", choices={"del": "DEL (^?)", "bs": "BS (^H)"}, advanced=True)
    implicit_cr: bool = option(False, "Implicit CR in every LF",
                               "For devices that end lines with LF only (output shifts right line after line)")
    implicit_lf: bool = option(False, "Implicit LF in every CR",
                               "For devices that end lines with CR only (each line overwrites the last)")
    answerback: str = option("", "Answerback", "Sent when the remote side asks with ENQ (Ctrl+E); empty sends nothing",
                             advanced=True)
    local_echo: bool = option(False, "Local echo", "Show typed characters yourself, for devices that do not echo", advanced=True)
    line_editing: bool = option(False, "Local line editing",
                                "Edit lines here (arrows, history) and send them on Enter; for devices without a line editor. Turn the device echo off")
    scrollback: int = option(10000, "Scrollback lines", advanced=True, min=0, max=1_000_000)
    log_file: str = option("", "Log file", "Append session output to this file; {name}, {host} and {date} are "
                           "replaced", advanced=True, widget="path")
    font_size: int = option(14, "Font size", min=6, max=72)
    theme: str = option("dark", "Color theme", choices={"dark": "Dark", "light": "Light", "solarized": "Solarized", "dracula": "Dracula"})


KINDS = {"ssh": "SSH", "serial": "Serial"}
# The module implementing each kind (see session.py for what it provides).
TRANSPORTS = {"ssh": "termdeck.ssh", "serial": "termdeck.serialport"}
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def parse_jump(text: str) -> tuple[str, str, int]:
    """Split a jump host, [user@]host[:port] or [user@][v6addr][:port], into (user, host, port)."""
    user, _, hostport = text.rpartition("@")
    if hostport.startswith("["):
        host, closed, rest = hostport[1:].partition("]")
        if not closed or (rest and not rest.startswith(":")):
            raise ProfileError(f"invalid jump host {text!r}")
        port = rest[1:] if rest else "22"
    elif hostport.count(":") > 1:  # a bare IPv6 address; a port needs [addr]:port
        host, port = hostport, "22"
    elif ":" in hostport:
        host, _, port = hostport.partition(":")
    else:
        host, port = hostport, "22"
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ProfileError(f"invalid jump host port in {text!r}")
    check_host(host, "jump host")
    return user, host, int(port)


def parse_env(text: str) -> dict[str, str]:
    """Environment variables from NAME=value lines."""
    env = {}
    for line in text.splitlines():
        if line.strip():
            name, sep, value = line.partition("=")
            if not sep or not name.strip():
                raise ProfileError(f"invalid environment line {line!r}, expected NAME=value")
            env[name.strip()] = value
    return env


def tty_mode_known(name: str) -> bool:
    """A terminal mode name asyncssh can send (with or without RFC 4254's V: ERASE or VERASE)."""
    import asyncssh

    return hasattr(asyncssh, f"PTY_{name}") or hasattr(asyncssh, f"PTY_V{name}")


def supported_algorithms() -> dict[str, list[str]]:
    """Algorithm names asyncssh supports, by kind (ciphers, key exchange, MACs, host key types)."""
    from asyncssh.encryption import get_encryption_algs
    from asyncssh.kex import get_kex_algs
    from asyncssh.mac import get_mac_algs
    from asyncssh.public_key import get_certificate_algs, get_public_key_algs

    def names(*lists) -> list[str]:
        return [a.decode() for algs in lists for a in algs]

    return {"cipher": names(get_encryption_algs()), "key exchange": names(get_kex_algs()),
            "MAC": names(get_mac_algs()), "host key type": names(get_public_key_algs(), get_certificate_algs()),
            "signature type": names(get_public_key_algs(), get_certificate_algs())}


def check_algorithms(text: str, what: str) -> list[str]:
    """A comma-separated preference list, checked against what asyncssh supports."""
    wanted = [v.strip() for v in text.split(",") if v.strip()]
    supported = supported_algorithms()[what]
    if unknown := [name for name in wanted if name not in supported]:
        kind = f"{what}s" if len(unknown) > 1 else what
        raise ProfileError(f"unknown {kind} {', '.join(unknown)}; supported: {', '.join(supported)}")
    return wanted


def normalize_fingerprint(text: str) -> str:
    """A host key fingerprint as asyncssh writes it: SHA256 without padding, MD5 in lower case.

    Also accepted, as PuTTY does: MD5 hex without "MD5:", and a public key's base64 blob.
    """
    text = text.strip()
    if m := re.fullmatch(r"SHA256:([A-Za-z0-9+/]{43})=?", text):
        return f"SHA256:{m[1]}"
    if m := re.fullmatch(r"(?:MD5:)?((?:[0-9a-fA-F]{2}:){15}[0-9a-fA-F]{2})", text):
        return f"MD5:{m[1].lower()}"
    if re.fullmatch(r"AAAA[A-Za-z0-9+/]{40,}={0,2}", text):  # a public key (the blob of "ssh-ed25519 AAAA...")
        digest = hashlib.sha256(base64.b64decode(text)).digest()
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")
    raise ProfileError(f"trusted host keys: {text!r} is not a SHA256:... or MD5:... fingerprint")


def parse_tty_modes(text: str) -> dict[str, int]:
    """Terminal modes from NAME=value lines (names as in RFC 4254: ECHO, ERASE, VINTR...)."""
    modes = {}
    for line in text.splitlines():
        if line.strip():
            name, sep, value = line.partition("=")
            if not sep or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name.strip()) \
                    or not re.fullmatch(r"[0-9]{1,10}", value.strip()) or int(value) > 2**32 - 1:
                raise ProfileError(f"invalid terminal mode {line!r}, expected NAME=number like ERASE=127")
            modes[name.strip().upper()] = int(value)
    return modes


def normalize_device(device: str) -> str:
    """Canonical serial device name: COM ports upper case and without the \\\\.\\ prefix."""
    device = device.strip()
    # pyserial adds \\\\.\\ back for COM ports only; other Windows devices (com0com's CNCA0) keep it.
    if m := re.fullmatch(r"(?:\\\\\.\\)?(com\d+)", device, re.I):
        device = m[1].upper()
    return device


@dataclass
class Profile:
    name: str = option("", "Name")
    kind: str = option("ssh", "Type", choices=KINDS)
    group: str = option("", "Folder", "Groups profiles in the sidebar")
    ssh: SSHSettings = field(default_factory=SSHSettings)
    serial: SerialSettings = field(default_factory=SerialSettings)
    terminal: TerminalSettings = field(default_factory=TerminalSettings)

    def validate(self) -> None:
        """Check every option, and bring values to their canonical form (device names)."""
        if not self.name.strip():
            raise ProfileError("profile name is empty")
        if _CONTROL_CHARS.search(self.name) or _CONTROL_CHARS.search(self.group):
            raise ProfileError("profile name and folder cannot contain control characters")
        if self.kind not in KINDS:
            raise ProfileError(f"unknown profile type {self.kind!r}")
        for section in (self, self.ssh, self.serial, self.terminal):
            for f in fields(section):
                if "label" in f.metadata:
                    check_option(f, getattr(section, f.name))
        if self.kind == "ssh":
            s = self.ssh
            if not s.host:
                raise ProfileError("ssh profile needs a host")
            check_host(s.host, "host")
            if s.jump_host:
                parse_jump(s.jump_host)
            for fwd in s.forwards:
                fwd.validate()
            parse_env(s.env)
            chosen = [name for name, used in (("No shell", s.no_shell), ("Subsystem", s.subsystem),
                                              ("Remote command", s.remote_command)) if used]
            if len(chosen) > 1:
                raise ProfileError(f"choose one of {', '.join(chosen)}: they replace each other")
            parse_tty_modes(s.tty_modes)
            # Lines may be pasted as shown: "ssh-ed25519 255 SHA256:..." (the type and size are skipped).
            tokens = [t for t in s.trusted_keys.split() if not re.fullmatch(r"(ssh-|ecdsa-|sk-|rsa-)[\w@.-]+|\d+", t)]
            s.trusted_keys = "\n".join(dict.fromkeys(normalize_fingerprint(t) for t in tokens))
            for name in parse_tty_modes(s.tty_modes):
                if not tty_mode_known(name):
                    raise ProfileError(f"unknown terminal mode {name}")
            for text, what in ((s.ciphers, "cipher"), (s.kex_algs, "key exchange"), (s.mac_algs, "MAC"),
                               (s.hostkey_algs, "host key type"), (s.signature_algs, "signature type")):
                if text.strip():
                    check_algorithms(text, what)
            if s.proxy_type in ("socks4", "socks5", "http", "telnet"):
                if not s.proxy_host:
                    raise ProfileError("the proxy needs a proxy host")
                check_host(s.proxy_host, "proxy host")
            if s.proxy_type == "command" and not s.proxy_command.strip():
                raise ProfileError("the proxy needs a proxy command")
            if s.agent_forwarding and not s.use_agent:
                raise ProfileError("'Forward agent' needs 'Use SSH agent' (ssh.use_agent) turned on")
        else:
            self.serial.device = normalize_device(self.serial.device)
            device = self.serial.device
            if not device:
                raise ProfileError("serial profile needs a device")
            scheme, sep, _ = device.partition("://")
            if sep and scheme.lower() not in SERIAL_URL_SCHEMES:
                raise ProfileError(f"unsupported device URL {scheme}://, use one of: "
                                 + ", ".join(f"{s}://" for s in SERIAL_URL_SCHEMES))
        if _CONTROL_CHARS.search(self.terminal.answerback):
            raise ProfileError("answerback cannot contain control characters: an ENQ in the output "
                               "would make it type a command")
        try:
            "".encode(self.terminal.encoding)
        except LookupError:
            raise ProfileError(f"unknown character set {self.terminal.encoding!r}") from None

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
    def from_dict(cls, data: dict, validate: bool = True) -> "Profile":
        """Build and validate a profile. Unknown keys are ignored, missing ones get defaults.

        `validate=False` only checks the structure, to repair a profile that fails validation.
        """
        if not isinstance(data, dict):
            raise ProfileError("profile must be an object")
        for key in ("name", "kind", "group"):
            if not isinstance(data.get(key, ""), str):
                raise ProfileError(f"profile {key} must be text")
        sections = {k: data.get(k, {}) for k in ("ssh", "serial", "terminal")}
        if not all(isinstance(v, dict) for v in sections.values()):
            raise ProfileError("profile sections must be objects")
        ssh = _known(SSHSettings, sections["ssh"])
        forwards = ssh.get("forwards", [])
        if not isinstance(forwards, list) or not all(isinstance(f, dict) for f in forwards):
            raise ProfileError("forwards must be a list of objects")
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
        if validate:
            profile.validate()
        return profile


def check_option(f, value) -> None:
    meta = f.metadata
    label = meta["label"].lower()
    choices = meta["choices"]
    if choices:
        if value not in choices:
            raise ProfileError(f"invalid {label} {value!r}, expected one of: {', '.join(map(str, choices))}")
        return
    expected = f.type if isinstance(f.type, type) else list
    if expected is float:
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected is int:
        ok = _is_int(value)
    else:
        ok = isinstance(value, expected)
    if not ok:
        kind = {int: "a whole number", float: "a number", bool: "true or false", str: "text"}.get(expected, expected.__name__)
        raise ProfileError(f"{label} must be {kind}, got {value!r}")
    if expected in (int, float):
        low, high = meta["min"], meta["max"]
        if low is None and value < 0:
            raise ProfileError(f"{label} cannot be negative")
        if (low is not None and value < low) or (high is not None and value > high):
            raise ProfileError(f"{label} must be between {low} and {high}, got {value}")


def _known(cls, data: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


SECTION_KINDS = {"profile": ("ssh", "serial"), "ssh": ("ssh",), "serial": ("serial",), "terminal": ("ssh", "serial")}
SECTIONS = {"profile": Profile, "ssh": SSHSettings, "serial": SerialSettings, "terminal": TerminalSettings}


def option_fields(section: str, include_hidden: bool = False) -> list:
    """The dataclass fields declared with `option()` in a section."""
    return [f for f in fields(SECTIONS[section])
            if "label" in f.metadata and (include_hidden or not f.metadata["hidden"])]


def describe_options() -> dict:
    """JSON-ready description of every option, grouped by section, for building forms."""

    def describe(section: str) -> list[dict]:
        out = []
        for f in option_fields(section):
            meta = f.metadata
            default = f.default if f.default is not MISSING else f.default_factory()
            choices = meta["choices"]
            out.append({
                "key": f.name,
                "type": meta["widget"] if meta["widget"] == "forwards" else f.type.__name__,
                "default": default,
                "label": meta["label"],
                "help": meta["help"],
                "advanced": meta["advanced"],
                "widget": meta["widget"],
                "wide": meta["wide"] or meta["widget"] in ("path", "multiline", "device"),
                "min": meta["min"],
                "max": meta["max"],
                "choices": [[k, v] for k, v in choices.items()] if choices else None,
                "kinds": list(meta["kinds"] or SECTION_KINDS[section]),
                "group": meta["group"],
                # values None: shown while the other option is set (not empty, not off)
                "when": {"key": meta["when"][0], "values": None if meta["when"][1] is None else list(meta["when"][1])}
                if meta["when"] else None,
            })
        return out

    return {section: describe(section) for section in SECTIONS}


def parse_target(text: str) -> Profile | None:
    """Unsaved profile from user@host[:port] (SSH) or DEVICE[@baud] (serial).

    A bare word is not a target: it is far more likely a mistyped profile name,
    and connecting to it would send a password to whatever host it resolves to.
    SSH targets need a user (bob@host), a port (host:22), or a dotted name or address.
    """
    if m := re.match(r"^((?:\\\\\.\\)?COM\d+|/dev/\S+|\w+://\S*)(?:@(\d+))?$", text, re.I):
        p = Profile(text, "serial")
        p.serial.device = normalize_device(m[1])
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
    raise ProfileNotFound(unknown_target(text, profiles))


FORMAT_VERSION = 1


class ProfileFileError(ProfileError):
    """profiles.json cannot be read. Nothing is written until the user fixes it."""


class ProfileStore:
    """Profiles kept in one JSON file, written atomically and readable by the owner only.

    An entry that fails validation, or repeats an earlier name, is skipped by
    `load` (see `invalid` and `warnings`) but kept in the file, so a hand edit
    gone wrong never loses data. Changes hold a file lock and work on their own
    copy of the file's contents, so two windows, threads or processes saving at
    once never drop each other's profiles.
    """

    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "profiles.json"
        self.invalid: list[tuple[str, str]] = []  # (name, error) from the last load, for display

    def load(self) -> dict[str, Profile]:
        profiles, skipped = self._read()
        self.invalid = [(name, error) for name, error, _ in skipped]
        return profiles

    def _read(self) -> tuple[dict[str, Profile], list[tuple[str, str, Any]]]:
        """The valid profiles, and the skipped entries as (name, error, raw entry)."""
        if not self.path.exists():
            return {}, []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProfileFileError(f"{self.path} is not valid JSON (line {exc.lineno}, column {exc.colno}): "
                                   "fix it or move it away; nothing is saved until then") from None
        except UnicodeDecodeError:
            raise ProfileFileError(f"{self.path} is not UTF-8 text: fix it or move it away") from None
        if not isinstance(data, dict) or not isinstance(data.get("profiles", []), list):
            raise ProfileFileError(f"{self.path} is not a termdeck profile file")
        version = data.get("version", FORMAT_VERSION)
        if not _is_int(version) or version > FORMAT_VERSION:
            raise ProfileFileError(f"{self.path} was written by a newer termdeck (format {version!r})")
        profiles, skipped = {}, []
        for number, raw in enumerate(data.get("profiles", []), 1):
            name = str(raw.get("name", "?")) if isinstance(raw, dict) else "?"
            try:
                profile = Profile.from_dict(raw)
                if profile.name in profiles:
                    raise ProfileError(f"entry {number} repeats the name of an earlier profile")
            except (ValueError, TypeError) as exc:
                skipped.append((name, str(exc), raw))
                continue
            profiles[profile.name] = profile
        return profiles, skipped

    def warnings(self) -> list[str]:
        """One line per profile the last `load` skipped (in `path`), for the UIs to show."""
        return [f"profile {name!r} skipped: {error}" for name, error in self.invalid]

    def get(self, name: str) -> Profile:
        profiles = self.load()
        if name in profiles:
            return profiles[name]
        for bad, error in self.invalid:
            if bad == name:
                raise ProfileError(f"profile {name!r} is invalid: {error} (in {self.path}); "
                               f"fix it with termdeck set, or remove it with termdeck rm")
        raise ProfileNotFound(f"no profile named {name!r}")

    def get_raw(self, name: str) -> Profile:
        """A profile even if it fails validation (unchecked), so the user can repair it."""
        profiles, skipped = self._read()
        if name in profiles:
            return profiles[name]
        for bad, _, raw in skipped:
            if bad == name and isinstance(raw, dict):
                return Profile.from_dict(raw, validate=False)
        raise ProfileNotFound(f"no profile named {name!r}")

    def add(self, profile: Profile) -> None:
        """Save a new profile. Raises ValueError if the name is taken."""
        profile.validate()
        with file_lock(self.path):
            profiles, skipped = self._read()
            if profile.name in profiles or _named(skipped, profile.name):
                raise ProfileError(f"a profile named {profile.name!r} already exists")
            _record_identity(profile, None)
            profiles[profile.name] = profile
            self._write(profiles, skipped)

    def update(self, name: str, profile: Profile) -> None:
        """Replace the profile called `name` (renaming it if `profile.name` differs).

        A skipped entry called `name` can be updated too: that is how it gets repaired.
        Skipped duplicates of a valid `name` are left alone.
        """
        profile.validate()
        with file_lock(self.path):
            profiles, skipped = self._read()
            previous = profiles.pop(name, None)
            if previous is None:
                if not _named(skipped, name):
                    raise ProfileNotFound(f"no profile named {name!r}: it was deleted or renamed meanwhile")
                skipped = [entry for entry in skipped if entry[0] != name]
            if profile.name != name and (profile.name in profiles or _named(skipped, profile.name)):
                raise ProfileError(f"a profile named {profile.name!r} already exists")
            _record_identity(profile, previous)
            profiles[profile.name] = profile
            self._write(profiles, skipped)

    def save(self, profile: Profile) -> None:
        """Add `profile`, or replace the one with the same name. For scripts and tests:
        unlike add/update it records no USB identity and checks no names."""
        profile.validate()
        with file_lock(self.path):
            profiles, skipped = self._read()
            profiles[profile.name] = profile
            self._write(profiles, skipped)

    def delete(self, name: str) -> None:
        """Delete every entry called `name`, valid or skipped."""
        with file_lock(self.path):
            profiles, skipped = self._read()
            found = profiles.pop(name, None) is not None or _named(skipped, name)
            if not found:
                raise ProfileNotFound(f"no profile named {name!r}")
            self._write(profiles, [entry for entry in skipped if entry[0] != name])

    def _write(self, profiles: dict[str, Profile], skipped: list) -> None:
        entries = [p.to_dict() for p in sorted(profiles.values(), key=lambda p: p.name.lower())]
        data = {"version": FORMAT_VERSION, "profiles": entries + [raw for _, _, raw in skipped]}
        atomic_write(self.path, json.dumps(data, indent=2) + "\n")


def _named(skipped: list, name: str) -> bool:
    return any(entry[0] == name for entry in skipped)


def _record_identity(profile: Profile, previous: Profile | None) -> None:
    """Remember which USB adapter a serial profile uses (see serialport.update_identity)."""
    if profile.kind == "serial":
        from .serialport import update_identity  # serialport imports this module

        update_identity(profile, previous)
