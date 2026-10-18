"""Proxies for the first hop of an SSH connection, like PuTTY's Proxy panel.

SOCKS 4/4a/5 and HTTP CONNECT are spoken here and handed to asyncssh as a
tunnel object; a local proxy command (ssh -o ProxyCommand) goes to asyncssh's
proxy_command. Proxy passwords are asked when needed, never stored.
"""

import asyncio
import base64
import collections
import fnmatch
import ipaddress
import re
import shlex
import socket
import struct
import subprocess
import sys

from .errors import ConnectError
from .profiles import SSHSettings

DEFAULT_PORTS = {"socks4": 1080, "socks5": 1080, "http": 3128, "telnet": 23}
LOCAL_NAMES = ("localhost", "127.0.0.1", "::1")


def uses_proxy(s: SSHSettings, host: str) -> bool:
    """Whether the connection to `host` goes through the profile's proxy (exclusions, local hosts)."""
    if s.proxy_type == "none":
        return False
    if host.lower() in LOCAL_NAMES or host.lower().endswith(".localhost"):
        return s.proxy_local
    patterns = [p.lower() for p in re.split(r"[\s,;]+", s.proxy_exclude) if p]  # as PuTTY: commas or spaces
    return not any(_excluded(host.lower(), pattern) for pattern in patterns)


def _excluded(host: str, pattern: str) -> bool:
    if "/" in pattern:  # a network, like 10.0.0.0/8
        try:
            return ipaddress.ip_address(host) in ipaddress.ip_network(pattern, strict=False)
        except ValueError:
            return False
    return fnmatch.fnmatch(host, pattern) or (pattern.startswith(".") and host.endswith(pattern))


def proxy_command(s: SSHSettings, host: str, port: int, username: str) -> list[str]:
    """The local command's arguments, with PuTTY's %host %port %user %proxyhost %proxyport replaced."""
    values = {"%host": host, "%port": str(port), "%user": username, "%proxyhost": s.proxy_host,
              "%proxyport": str(s.proxy_port or "")}
    windows = sys.platform == "win32"
    # On Windows backslashes are path separators: split without escapes, then drop the quotes.
    words = shlex.split(s.proxy_command, posix=not windows)
    if windows:
        words = [w[1:-1] if len(w) > 1 and w[0] == w[-1] and w[0] in "\"'" else w for w in words]
    out = []
    for word in words:
        word = word.replace("%%", "\0")
        for key in sorted(values, key=len, reverse=True):
            word = word.replace(key, values[key])
        out.append(word.replace("\0", "%"))
    return out


class CommandTunnel:
    """An asyncssh tunnel through a local command (ssh -o ProxyCommand), like asyncssh's own
    proxy_command but with the command's error output kept apart: it is shown when the
    command fails, instead of being fed into the SSH stream."""

    def __init__(self, argv: list[str]):
        self.argv = argv
        self.errors = collections.deque(maxlen=10)  # the last lines the command wrote to stderr

    def __str__(self) -> str:
        return f"proxy command {self.argv[0]}"

    def failure(self) -> str:
        said = " / ".join(self.errors)
        return f"{self} ended" + (f": {said}" if said else " without saying why")

    async def create_connection(self, protocol_factory, host: str, port: int):
        loop = asyncio.get_running_loop()
        conn = protocol_factory()
        # No console window for a console program started from the desktop window; its own process group elsewhere.
        extra = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {"start_new_session": True}
        try:
            _, pipe = await loop.subprocess_exec(lambda: _CommandPipe(conn, self), *self.argv, **extra)
        except OSError as exc:
            raise ConnectError(f"{self}: {exc.strerror or exc}") from None
        return pipe, conn


class _CommandPipe(asyncio.SubprocessProtocol):
    """The transport asyncssh writes to: the command's stdin and stdout; stderr is collected."""

    def __init__(self, conn, tunnel: CommandTunnel):
        self._conn, self._tunnel = conn, tunnel
        self._transport = self._stdin = None
        self._partial = b""

    def connection_made(self, transport) -> None:
        self._transport = transport
        self._stdin = transport.get_pipe_transport(0)
        self._conn.connection_made(self)

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if fd == 1:
            self._conn.data_received(data)
            return
        *lines, self._partial = (self._partial + data).split(b"\n")
        for line in lines:
            if text := line.decode("utf-8", "replace").strip():
                self._tunnel.errors.append(text)

    def pipe_connection_lost(self, fd: int, exc) -> None:
        if fd == 1:  # the command's output ended: so did the connection
            if self._partial.strip():
                self._tunnel.errors.append(self._partial.decode("utf-8", "replace").strip())
            self._conn.connection_lost(exc or ConnectError(self._tunnel.failure()))

    def get_extra_info(self, name: str, default=None):
        return self._transport.get_extra_info(name, default) if self._transport else default

    def write(self, data: bytes) -> None:
        if self._stdin:
            self._stdin.write(data)

    def is_closing(self) -> bool:
        return self._transport is None or self._transport.is_closing()

    def abort(self) -> None:
        self.close()

    def close(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None


class ProxyTunnel:
    """An asyncssh tunnel through a SOCKS or HTTP proxy."""

    def __init__(self, s: SSHSettings, ask_password):
        self.kind = s.proxy_type
        self.host, self.port = s.proxy_host, s.proxy_port or DEFAULT_PORTS[s.proxy_type]
        self.username = s.proxy_username
        # Auto, like PuTTY: SOCKS 4 servers often lack 4a, so names are resolved here for them.
        self.remote_dns = s.proxy_dns == "yes" or (s.proxy_dns == "auto" and s.proxy_type != "socks4")
        self.telnet_command = s.proxy_telnet_command
        self._ask_password = ask_password  # async () -> str | None
        self._password_given: str | None = None  # asked once per connect, also across a host key retry

    def __str__(self) -> str:
        label = {"socks4": "SOCKS 4", "socks5": "SOCKS 5", "http": "HTTP", "telnet": "Telnet"}[self.kind]
        return f"{label} proxy {self.host}:{self.port}"

    async def create_connection(self, protocol_factory, host: str, port: int):
        loop = asyncio.get_running_loop()
        sock = await self._connect(loop)
        try:
            await getattr(self, f"_{self.kind}")(loop, sock, host, port)
            return await loop.create_connection(protocol_factory, sock=sock)
        except BaseException:
            sock.close()
            raise

    async def _connect(self, loop) -> socket.socket:
        try:
            infos = await loop.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ConnectError(f"{self}: cannot resolve the proxy ({exc.strerror or exc})") from None
        last = None
        for family, kind, proto, _, address in infos:
            sock = socket.socket(family, kind, proto)
            sock.setblocking(False)
            try:
                await loop.sock_connect(sock, address)
                return sock
            except OSError as exc:
                sock.close()
                last = exc
        raise ConnectError(f"{self}: cannot connect to the proxy ({last.strerror if last else 'no address'})")

    async def _password(self) -> str:
        if self._password_given is None:
            password = await self._ask_password()
            if password is None:
                raise ConnectError(f"{self}: proxy password not given, connection cancelled")
            self._password_given = password
        return self._password_given

    async def _target(self, loop, host: str) -> tuple[str, bytes | None]:
        """("ip", packed address) or ("name", None) for a name the proxy resolves.
        A name resolved here prefers IPv4 for SOCKS 4, which has no IPv6."""
        try:
            return "ip", ipaddress.ip_address(host).packed
        except ValueError:
            pass
        if self.remote_dns:
            return "name", None
        try:
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ConnectError(f"cannot resolve {host} ({exc.strerror or exc}); "
                               "'Proxy looks up host names' would let the proxy do it") from None
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        if self.kind == "socks4":
            addresses.sort(key=lambda a: a.version != 4)
        return "ip", addresses[0].packed

    async def _socks4(self, loop, sock, host: str, port: int) -> None:
        kind, packed = await self._target(loop, host)
        if kind == "ip" and len(packed) != 4:
            raise ConnectError(f"{self}: SOCKS 4 cannot reach the IPv6 address {host}; use SOCKS 5")
        user = self.username.encode()
        if kind == "ip":
            request = struct.pack(">BBH", 4, 1, port) + packed + user + b"\0"
        else:  # SOCKS 4a: the proxy resolves the name
            request = struct.pack(">BBH", 4, 1, port) + b"\0\0\0\1" + user + b"\0" + host.encode("idna") + b"\0"
        await loop.sock_sendall(sock, request)
        reply = await _recv(loop, sock, 8, self)
        if reply[1] != 90:
            raise ConnectError(f"{self} refused the connection to {host}:{port} (SOCKS 4 code {reply[1]})")

    async def _socks5(self, loop, sock, host: str, port: int) -> None:
        methods = b"\0\2" if self.username else b"\0"
        await loop.sock_sendall(sock, bytes([5, len(methods)]) + methods)
        version, method = await _recv(loop, sock, 2, self)
        if version != 5 or method == 0xFF:
            hint = "" if self.username else " (it may need a login: set the proxy username)"
            raise ConnectError(f"{self}: the proxy accepts none of our login methods{hint}")
        if method == 2:
            user, password = self.username.encode(), (await self._password()).encode()
            if len(user) > 255 or len(password) > 255:
                raise ConnectError(f"{self}: SOCKS 5 user names and passwords are limited to 255 bytes")
            await loop.sock_sendall(sock, bytes([1, len(user)]) + user + bytes([len(password)]) + password)
            if (await _recv(loop, sock, 2, self))[1] != 0:
                raise ConnectError(f"{self}: proxy login failed for {self.username}")
        elif method != 0:
            raise ConnectError(f"{self}: the proxy asks for an unsupported login method ({method})")
        kind, packed = await self._target(loop, host)
        if kind == "name":
            name = host.encode("idna")
            if len(name) > 255:
                raise ConnectError(f"{self}: host name too long for SOCKS 5")
            address = bytes([3, len(name)]) + name
        else:
            address = bytes([1 if len(packed) == 4 else 4]) + packed
        await loop.sock_sendall(sock, b"\5\1\0" + address + struct.pack(">H", port))
        head = await _recv(loop, sock, 4, self)
        if head[1] != 0:
            reasons = {1: "general failure", 2: "not allowed by its rules", 3: "network unreachable",
                       4: "host unreachable", 5: "connection refused", 6: "TTL expired"}
            raise ConnectError(f"{self}: {host}:{port}: {reasons.get(head[1], f'error {head[1]}')}")
        size = {1: 4, 4: 16}.get(head[3]) or (await _recv(loop, sock, 1, self))[0]
        await _recv(loop, sock, size + 2, self)  # the address the proxy bound: not needed

    async def _http(self, loop, sock, host: str, port: int) -> None:
        kind, packed = await self._target(loop, host)
        if kind == "ip":  # an address given, or the name resolved here (Proxy looks up host names: no)
            host = str(ipaddress.ip_address(packed))
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
        if self.username:
            token = base64.b64encode(f"{self.username}:{await self._password()}".encode()).decode()
            lines.append(f"Proxy-Authorization: Basic {token}")
        await loop.sock_sendall(sock, ("\r\n".join(lines) + "\r\n\r\n").encode())
        reply = b""
        while b"\r\n\r\n" not in reply:
            chunk = await loop.sock_recv(sock, 1)  # byte by byte: what follows the headers is SSH
            if not chunk or len(reply) > 16384:
                raise ConnectError(f"{self}: the proxy closed the connection")
            reply += chunk
        status = reply.split(b"\r\n", 1)[0].decode("latin-1")
        parts = status.split(" ", 2)
        if len(parts) < 2 or parts[1] != "200":
            hint = " (set the proxy user name)" if len(parts) > 1 and parts[1] == "407" and not self.username else ""
            raise ConnectError(f"{self} refused {target}: {status}{hint}")


    async def _telnet(self, loop, sock, host: str, port: int) -> None:
        """PuTTY's Telnet proxy: send a command line, then the connection is the target's."""
        command = self.telnet_command
        values = {"%host": host, "%port": str(port), "%user": self.username}
        if "%pass" in command:
            values["%pass"] = await self._password()
        command = command.replace("%%", "\0")
        for key in sorted(values, key=len, reverse=True):
            command = command.replace(key, values[key])
        command = command.replace("\\n", "\n").replace("\\r", "\r").replace("\0", "%")
        await loop.sock_sendall(sock, command.encode())


async def _recv(loop, sock, size: int, proxy) -> bytes:
    data = b""
    while len(data) < size:
        chunk = await loop.sock_recv(sock, size - len(data))
        if not chunk:
            raise ConnectError(f"{proxy}: the proxy closed the connection")
        data += chunk
    return data
