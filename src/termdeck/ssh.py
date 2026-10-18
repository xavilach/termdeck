"""SSH connections: interactive shell, port forwards and SFTP (asyncssh)."""

import asyncio
import signal as signal_module
import socket
from dataclasses import replace
from pathlib import Path

import asyncssh

from .config import atomic_write, config_dir, file_lock, open_private, user_path
from .errors import ConnectError
from .profiles import Profile, check_host, parse_env, parse_jump
from .session import HostKey, Prompter, Session, describe_error as _describe, notify, printable
from .sftp import SFTP

MAX_PASSWORD_TRIES = 3
ERRORS = (asyncssh.Error,)  # connection failures, as opposed to bugs (see session.is_expected)


def known_hosts_path() -> Path:
    return config_dir() / "known_hosts"


def _known_hosts_files() -> list[str]:
    ours = known_hosts_path()
    if not ours.exists():
        open_private(ours).close()
    openssh = Path.home() / ".ssh" / "known_hosts"
    return [str(f) for f in (ours, openssh) if f.is_file()]


def _host_pattern(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def remember_host_key(host: str, port: int, key: asyncssh.SSHKey, replaced: list[asyncssh.SSHKey] = ()) -> None:
    """Store `key` in termdeck's known_hosts, replacing any older key for this host.

    Keys it `replaced` are written as @revoked, so a copy still trusted elsewhere
    (~/.ssh/known_hosts, another entry for the same address) is refused too.
    """
    check_host(host, "host")  # never write a pattern that could match other hosts
    path = known_hosts_path()
    pattern = _host_pattern(host, port)
    algorithm, blob = key.export_public_key("openssh").decode().split()[:2]
    with file_lock(path):  # two tabs may accept keys at the same moment
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        # Drop the old entry, and any revocation of the key now trusted (a server restored from backup).
        lines = [line for line in lines
                 if line.split(" ", 1)[0] != pattern and line != f"@revoked {pattern} {algorithm} {blob}"]
        for old in replaced:
            old_algorithm, old_blob = old.export_public_key("openssh").decode().split()[:2]
            # Only the key this one replaces: the host's keys of other types stay valid.
            old_line = f"@revoked {pattern} {old_algorithm} {old_blob}"
            if old_algorithm == algorithm and old_blob != blob and old_line not in lines:
                lines.append(old_line)
        lines.append(f"{pattern} {algorithm} {blob}")
        atomic_write(path, "\n".join(lines) + "\n")


def forget_host(host: str, port: int = 22) -> int:
    """Remove every termdeck known_hosts line for host:port, revocations included. Returns how many."""
    path = known_hosts_path()
    pattern = _host_pattern(host, port)
    with file_lock(path):
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        keep = [line for line in lines if pattern not in line.split(" ")[:2]]  # "host ..." or "@revoked host ..."
        if len(keep) != len(lines):
            atomic_write(path, "\n".join(keep) + ("\n" if keep else ""))
    return len(lines) - len(keep)


class _Client(asyncssh.SSHClient):
    def __init__(self, prompter: Prompter, label: str):
        self._prompter = prompter
        self._label = label
        self._password_tries = 0
        self._password: str | None = None  # the last one typed, for a password change; never stored
        self.rejected_key: asyncssh.SSHKey | None = None
        self.known_keys: list[asyncssh.SSHKey] = []
        self.key_changed = False
        self.lost: Exception | None = None
        self._tasks: set[asyncio.Task] = set()

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        # Called only for keys missing from known_hosts. The handshake fails and
        # SSHConnection.connect asks the user before reconnecting.
        trusted, cas = asyncssh.read_known_hosts(_known_hosts_files()).match(host, addr, port)[:2]
        self.rejected_key = key
        # The same key can match both the name and the address: list it once.
        self.known_keys = list({k.export_public_key(): k for k in trusted}.values())
        self.key_changed = bool(trusted or cas)  # a host vouched for by a CA is not "unknown" either
        return False

    def auth_completed(self) -> None:
        self._password = None  # needed only for a password change during login

    def connection_lost(self, exc) -> None:
        self._password = None
        self.lost = exc  # why the server closed (its disconnect reason), for the end of the session

    def auth_banner_received(self, msg: str, lang: str) -> None:
        # Shown like PuTTY does, before the login prompts (printable keeps newlines).
        task = asyncio.ensure_future(notify(self._prompter, printable(msg.replace("\r\n", "\n")).strip()))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def password_auth_requested(self) -> str | None:
        if self._password_tries >= MAX_PASSWORD_TRIES:
            return None
        self._password_tries += 1
        self._password = await self._prompter.ask(f"{self._label} password: ")
        return self._password

    async def password_change_requested(self, prompt: str, lang: str):
        """The password expired: ask for a new one (twice) and change it, like ssh."""
        await notify(self._prompter, printable(prompt).strip() or f"The password for {self._label} has expired.")
        for _ in range(MAX_PASSWORD_TRIES):
            new = await self._prompter.ask(f"New password for {self._label}: ")
            if new is None:
                break
            if new == await self._prompter.ask(f"New password again for {self._label}: "):
                old, self._password = self._password, None
                return old or "", new
            await notify(self._prompter, "The passwords do not match.")
        return NotImplemented

    def kbdint_auth_requested(self) -> str:
        return ""

    async def kbdint_challenge_received(self, name, instructions, lang, prompts):
        if not prompts:
            return []
        if self._password_tries >= MAX_PASSWORD_TRIES:
            return None
        self._password_tries += 1
        # MFA and Duo flows explain themselves in name/instructions.
        await notify(self._prompter, printable("\n".join(t for t in (name, instructions) if t)))
        answers = []
        for prompt, echo in prompts:
            # Prefixed like OpenSSH: the text comes from the server, which could pretend to be termdeck.
            text = f"({self._label}) {printable(prompt).strip() or 'Response:'} "
            answer = await self._prompter.ask(text, secret=not echo)
            if answer is None:
                return None
            answers.append(answer)
        return answers


async def _load_key(path: str, prompter: Prompter) -> asyncssh.SSHKey:
    path = str(user_path(path))
    try:
        return await asyncio.to_thread(asyncssh.read_private_key, path)
    except asyncssh.KeyImportError as exc:
        if "passphrase" not in str(exc).lower():
            raise
    passphrase = await prompter.ask(f"Passphrase for {path}: ")
    if passphrase is None:
        raise asyncssh.KeyImportError("passphrase entry cancelled")
    # Decrypting a key takes most of a second: keep the event loop (other tabs) running.
    return await asyncio.to_thread(asyncssh.read_private_key, path, passphrase)


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def jump_profile(profile: Profile) -> Profile:
    """Profile for the jump host: [user@]host[:port], connection options inherited.

    Like ssh -J, the jump host only relays: it gets no agent or X11 forwarding,
    no environment, no command and no port forwards.
    """
    user, host, port = parse_jump(profile.ssh.jump_host)
    ssh = replace(profile.ssh, host=host, port=port, username=user or profile.ssh.username,
                  jump_host="", forwards=[], agent_forwarding=False, x11_forwarding=False,
                  env="", remote_command="")
    return replace(profile, name=f"{profile.name} (jump)", ssh=ssh)


class SSHConnection:
    def __init__(self, conn: asyncssh.SSHClientConnection, profile: Profile, jump: "SSHConnection | None" = None,
                 client: "_Client | None" = None):
        self.conn = conn
        self.client = client
        self.profile = profile
        self.jump = jump
        self.listeners: list[asyncssh.SSHListener] = []
        self._files: SFTP | None = None

    @classmethod
    async def connect(cls, profile: Profile, prompter: Prompter) -> "SSHConnection":
        s = profile.ssh
        jump = await cls.connect(jump_profile(profile), prompter) if s.jump_host else None
        try:
            return await cls._connect(profile, prompter, jump)
        except BaseException:
            if jump:
                jump.close()
            raise

    @classmethod
    async def _connect(cls, profile: Profile, prompter: Prompter, jump: "SSHConnection | None") -> "SSHConnection":
        s = profile.ssh
        username = s.username or await prompter.ask(f"login as ({s.host}): ", secret=False)
        if not username:
            raise ConnectError("No username given, connection cancelled")
        options = {
            "connect_timeout": s.connect_timeout or None,
            "keepalive_interval": s.keepalive,
            "compression_algs": ["zlib@openssh.com", "zlib", "none"] if s.compression else ["none"],
            "agent_forwarding": s.agent_forwarding,
            "family": {"ipv4": socket.AF_INET, "ipv6": socket.AF_INET6}.get(s.address_family, socket.AF_UNSPEC),
        }
        if jump:
            options["tunnel"] = jump.conn
        if not s.use_agent:
            options["agent_path"] = None
        if s.key_file:
            options["client_keys"] = [await _load_key(s.key_file, prompter)]
        if s.ciphers:
            options["encryption_algs"] = _csv(s.ciphers)
        if s.kex_algs:
            options["kex_algs"] = _csv(s.kex_algs)
        label = f"{username}@{s.host}" + (f":{s.port}" if s.port != 22 else "")

        for _ in range(2):
            client = _Client(prompter, label)
            try:
                conn = await asyncssh.connect(
                    s.host,
                    s.port,
                    username=username,
                    known_hosts=_known_hosts_files(),
                    client_factory=lambda: client,
                    **options,
                )
                return cls(conn, profile, jump, client)
            except asyncssh.HostKeyNotVerifiable:
                key = client.rejected_key
                if key is None:
                    raise
                info = HostKey(
                    host=s.host, port=s.port, key_type=key.get_algorithm(), fingerprint=key.get_fingerprint(),
                    changed=client.key_changed,
                    old_fingerprints=[f"{k.get_algorithm()} {k.get_fingerprint()}" for k in client.known_keys],
                    known_hosts=str(known_hosts_path()),
                )
                if not await prompter.confirm_host_key(info):
                    raise
                await asyncio.to_thread(remember_host_key, s.host, s.port, key, client.known_keys)
        raise asyncssh.HostKeyNotVerifiable("host key still not trusted after accepting it")

    async def start_forwards(self) -> list[str]:
        """Open the profile's port forwards. Returns messages for failures and public listeners."""
        errors = []
        for fwd in self.profile.ssh.forwards:
            if fwd.public:
                errors.append(f"warning: forward {fwd} accepts connections from other machines")
            try:
                if fwd.kind == "local":
                    listener = await self.conn.forward_local_port(
                        fwd.listen_host, fwd.listen_port, fwd.dest_host, fwd.dest_port
                    )
                elif fwd.kind == "remote":
                    listener = await self.conn.forward_remote_port(
                        fwd.listen_host, fwd.listen_port, fwd.dest_host, fwd.dest_port
                    )
                else:
                    listener = await self.conn.forward_socks(fwd.listen_host, fwd.listen_port)
                self.listeners.append(listener)
            except (OSError, asyncssh.Error) as exc:
                errors.append(f"forward {fwd} failed: {exc}")
        return errors

    async def open_shell(self, cols: int, rows: int, notices: list[str] = (), separate_stderr: bool = False) -> Session:
        """The shell, command or subsystem; with no_shell, a session that only keeps the forwards open.

        `separate_stderr` keeps the remote error output apart (read_stderr) when there is no
        terminal, for scripts; otherwise it is merged into the output, as a terminal shows it.
        """
        s = self.profile.ssh
        if s.no_shell:
            return SSHForwards(self, notices)
        process = await self.conn.create_process(
            None if s.subsystem else (s.remote_command or None),
            subsystem=s.subsystem or None,
            term_type=self.profile.terminal.term_type if s.pty else None,
            term_size=(cols, rows) if s.pty else None,
            env=parse_env(s.env),
            x11_forwarding=s.x11_forwarding,
            stderr=asyncssh.PIPE if separate_stderr and not s.pty else asyncssh.STDOUT,
            encoding=None,
        )
        return SSHShell(self, process, notices, separate_stderr=separate_stderr and not s.pty)

    async def open_stream(self, host: str, port: int) -> "SSHStream":
        """A TCP connection opened from the server to host:port (like ssh -W, plink -nc)."""
        reader, writer = await self.conn.open_connection(host, port)
        return SSHStream(self, reader, writer, f"{host}:{port}")

    def sftp(self) -> SFTP:
        """File transfer on this connection; the SFTP channel opens on first use."""
        if self._files is None:
            self._files = SFTP(self.conn)
        return self._files

    def close(self) -> None:
        if self._files:
            self._files.close()
        self.conn.close()
        if self.jump:
            self.jump.close()

    async def wait_closed(self) -> None:
        await self.conn.wait_closed()
        if self.jump:
            await self.jump.wait_closed()


class SSHShell(Session):
    """Interactive shell. Closing it closes the whole connection, forwards included."""

    def __init__(self, conn: SSHConnection, process: asyncssh.SSHClientProcess, notices,
                 separate_stderr: bool = False):
        self.connection = conn
        self._process = process
        self._separate_stderr = separate_stderr
        self.title = conn.profile.name
        self.exit_status: int | None = None
        self.exit_signal: str | None = None
        for text in notices:
            self.notice(text)

    async def read(self) -> bytes:
        try:
            if data := await self._process.stdout.read(65536):
                return data
        except (asyncssh.Error, OSError) as exc:
            self.end_reason = _describe(exc)
            return b""
        await self._ended()
        return b""

    async def _ended(self) -> None:
        """Record how the remote side ended: exit status, signal, or a lost connection."""
        try:
            await asyncio.wait_for(self._process.wait_closed(), 5)
        except (asyncio.TimeoutError, asyncssh.Error, OSError):
            pass
        self.exit_status = self._process.exit_status
        self.exit_signal = self._process.exit_signal[0] if self._process.exit_signal else None
        if self.exit_signal:  # like plink and shells: 128 + the signal number
            number = getattr(signal_module.Signals, f"SIG{self.exit_signal}", None)
            self.exit_status = 128 + (number.value if number else 0)
        lost = self.connection.client.lost if self.connection.client else None
        if self.exit_status is None and not self._process.exit_signal:
            # No exit status: the connection went away. asyncssh keeps the server's reason
            # unless it was a plain "by application" goodbye.
            self.end_reason = _describe(lost) if lost else "Connection closed by the server"
        elif signal := self._process.exit_signal:
            self.end_reason = f"Remote program killed by signal {printable(signal[0])}"
        elif self.exit_status not in (None, 0):
            self.end_reason = f"Remote program exited with status {self.exit_status}"

    async def read_stderr(self) -> bytes:
        if not self._separate_stderr:
            return b""
        try:
            return await self._process.stderr.read(65536)
        except (asyncssh.Error, OSError):
            return b""

    async def write(self, data: bytes) -> None:
        if not self._process.stdin.is_closing():
            self._process.stdin.write(data)
            await self._process.stdin.drain()  # wait while the server is not taking data (piped input)

    async def write_eof(self) -> bool:
        if not self._process.stdin.is_closing():
            self._process.stdin.write_eof()
        return True

    def resize(self, cols: int, rows: int) -> None:
        if self.connection.profile.ssh.pty:  # no terminal, no size
            self._process.change_terminal_size(cols, rows)

    def sftp(self) -> SFTP:
        return self.connection.sftp()

    async def close(self) -> None:
        self.connection.close()
        await self.connection.wait_closed()


class SSHForwards(Session):
    """A connection kept open for its port forwards only (no shell, like ssh -N)."""

    def __init__(self, conn: SSHConnection, notices):
        self.connection = conn
        self.title = conn.profile.name
        forwards = ", ".join(str(f) for f in conn.profile.ssh.forwards) or "none"
        self.notice(f"connected without a shell; port forwards: {forwards}")
        for text in notices:
            self.notice(text)

    async def read(self) -> bytes:
        await self.connection.conn.wait_closed()
        if lost := self.connection.client and self.connection.client.lost:
            self.end_reason = _describe(lost)
        return b""

    async def write(self, data: bytes) -> None:
        pass  # nothing to type into

    async def write_eof(self) -> bool:
        return True  # input ending (stdin from /dev/null) must not close the forwards

    def sftp(self) -> SFTP:
        return self.connection.sftp()

    async def close(self) -> None:
        self.connection.close()
        await self.connection.wait_closed()


class SSHStream(Session):
    """Raw bytes to and from a host:port reached through the server."""

    def __init__(self, conn: SSHConnection, reader, writer, title: str):
        self.connection = conn
        self._reader, self._writer = reader, writer
        self.title = title

    async def read(self) -> bytes:
        try:
            return await self._reader.read(65536)
        except (asyncssh.Error, OSError) as exc:
            self.end_reason = _describe(exc)
            return b""

    async def write(self, data: bytes) -> None:
        if not self._writer.is_closing():
            self._writer.write(data)
            await self._writer.drain()

    async def write_eof(self) -> bool:
        if not self._writer.is_closing():
            self._writer.write_eof()
        return True

    def sftp(self) -> SFTP:
        return self.connection.sftp()

    async def close(self) -> None:
        self.connection.close()
        await self.connection.wait_closed()


async def open_session(profile: Profile, prompter: Prompter, cols: int, rows: int,
                       scripted: bool = False) -> Session:
    """Connect, open the port forwards and start the shell (the transport entry point)."""
    conn = await SSHConnection.connect(profile, prompter)
    try:
        notices = await conn.start_forwards()
        return await conn.open_shell(cols, rows, notices, separate_stderr=scripted)
    except BaseException:
        conn.close()
        raise


def describe_error(exc: BaseException) -> str | None:
    """User message for an SSH error, or None if `exc` is not one."""
    if isinstance(exc, asyncssh.HostKeyNotVerifiable):
        if "revoked" in str(exc).lower():
            return ("This host key was revoked when a changed key was accepted; if the server really uses "
                    "it again, run termdeck forget-host HOST[:PORT] and connect to confirm it")
        return "Host key not trusted, connection aborted"
    if isinstance(exc, asyncssh.PermissionDenied):
        return "Authentication failed"
    if isinstance(exc, asyncssh.ConnectionLost):
        return f"Connection lost: {exc.reason}" if exc.reason else "Connection lost"
    if isinstance(exc, asyncssh.DisconnectError):
        return f"Disconnected: {exc.reason}"
    if isinstance(exc, asyncssh.KeyImportError):
        return f"Cannot load the private key: {exc}"
    return None
