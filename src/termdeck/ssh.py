"""SSH connections: interactive shell, port forwards and SFTP (asyncssh)."""

from dataclasses import replace
from pathlib import Path

import asyncssh

from .config import config_dir, open_private
from .profiles import Profile
from .session import Prompter, Session, printable

MAX_PASSWORD_TRIES = 3


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


def remember_host_key(host: str, port: int, key: asyncssh.SSHKey) -> None:
    """Store `key` in termdeck's known_hosts, replacing any older key for this host."""
    path = known_hosts_path()
    pattern = _host_pattern(host, port)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if line.split(" ", 1)[0] != pattern]
    algorithm, blob = key.export_public_key("openssh").decode().split()[:2]
    lines.append(f"{pattern} {algorithm} {blob}")
    with open_private(path) as f:
        f.write("\n".join(lines) + "\n")


class _Client(asyncssh.SSHClient):
    def __init__(self, prompter: Prompter, label: str):
        self._prompter = prompter
        self._label = label
        self._password_tries = 0
        self.rejected_key: asyncssh.SSHKey | None = None
        self.key_changed = False

    def validate_host_public_key(self, host, addr, port, key) -> bool:
        # Called only for keys missing from known_hosts. The handshake fails and
        # SSHConnection.connect asks the user before reconnecting.
        known = asyncssh.read_known_hosts(_known_hosts_files()).match(host, addr, port)[0]
        self.rejected_key = key
        self.key_changed = bool(known)
        return False

    async def password_auth_requested(self) -> str | None:
        if self._password_tries >= MAX_PASSWORD_TRIES:
            return None
        self._password_tries += 1
        return await self._prompter.ask(f"{self._label} password: ")

    def kbdint_auth_requested(self) -> str:
        return ""

    async def kbdint_challenge_received(self, name, instructions, lang, prompts):
        if not prompts:
            return []
        if self._password_tries >= MAX_PASSWORD_TRIES:
            return None
        self._password_tries += 1
        answers = []
        for prompt, echo in prompts:
            answer = await self._prompter.ask(printable(prompt) or "Response: ", secret=not echo)
            if answer is None:
                return None
            answers.append(answer)
        return answers


async def _load_key(path: str, prompter: Prompter) -> asyncssh.SSHKey:
    path = str(Path(path).expanduser())
    try:
        return asyncssh.read_private_key(path)
    except asyncssh.KeyImportError as exc:
        if "passphrase" not in str(exc).lower():
            raise
    passphrase = await prompter.ask(f"Passphrase for {path}: ")
    if passphrase is None:
        raise asyncssh.KeyImportError("passphrase entry cancelled")
    return asyncssh.read_private_key(path, passphrase)


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _parse_env(text: str) -> dict[str, str]:
    env = {}
    for line in text.splitlines():
        if line.strip():
            name, sep, value = line.partition("=")
            if not sep:
                raise ValueError(f"invalid environment line {line!r}, expected NAME=value")
            env[name.strip()] = value
    return env


def jump_profile(profile: Profile) -> Profile:
    """Profile for the jump host: [user@]host[:port], other options inherited."""
    user, _, hostport = profile.ssh.jump_host.rpartition("@")
    host, port = hostport, 22
    if hostport.startswith("["):
        host, _, rest = hostport[1:].partition("]")
        port = int(rest[1:]) if rest.startswith(":") else 22
    elif hostport.count(":") == 1:
        host, port = hostport.split(":")
    ssh = replace(profile.ssh, host=host, port=int(port), username=user or profile.ssh.username,
                  jump_host="", forwards=[])
    return replace(profile, name=f"{profile.name} (jump)", ssh=ssh)


class SSHConnection:
    def __init__(self, conn: asyncssh.SSHClientConnection, profile: Profile, jump: "SSHConnection | None" = None):
        self.conn = conn
        self.profile = profile
        self.jump = jump
        self.listeners: list[asyncssh.SSHListener] = []
        self._sftp: asyncssh.SFTPClient | None = None

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
            raise asyncssh.PermissionDenied("no username given")
        options = {
            "connect_timeout": s.connect_timeout or None,
            "keepalive_interval": s.keepalive,
            "compression_algs": ["zlib@openssh.com", "zlib", "none"] if s.compression else ["none"],
            "agent_forwarding": s.agent_forwarding,
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
        label = f"{username}@{s.host}"

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
                return cls(conn, profile, jump)
            except asyncssh.HostKeyNotVerifiable:
                key = client.rejected_key
                if key is None:
                    raise
                fingerprint = key.get_fingerprint()
                if not await prompter.confirm_host_key(s.host, s.port, fingerprint, client.key_changed):
                    raise
                remember_host_key(s.host, s.port, key)
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

    async def open_shell(self, cols: int, rows: int, notices: list[str] = ()) -> "SSHShell":
        s = self.profile.ssh
        process = await self.conn.create_process(
            s.remote_command or None,
            term_type=self.profile.terminal.term_type,
            term_size=(cols, rows),
            env=_parse_env(s.env),
            x11_forwarding=s.x11_forwarding,
            stderr=asyncssh.STDOUT,
            encoding=None,
        )
        return SSHShell(self, process, notices)

    def sftp(self):
        """Return an async context manager yielding an asyncssh SFTPClient."""
        return self.conn.start_sftp_client()

    async def sftp_client(self) -> asyncssh.SFTPClient:
        """SFTP client shared by the UI for this connection, opened on first use."""
        if self._sftp is None:
            self._sftp = await self.conn.start_sftp_client()
        return self._sftp

    def close(self) -> None:
        self.conn.close()
        if self.jump:
            self.jump.close()

    async def wait_closed(self) -> None:
        await self.conn.wait_closed()
        if self.jump:
            await self.jump.wait_closed()


class SSHShell(Session):
    """Interactive shell. Closing it closes the whole connection, forwards included."""

    def __init__(self, conn: SSHConnection, process: asyncssh.SSHClientProcess, notices):
        self.connection = conn
        self._process = process
        self._pending = "".join(f"\r\n[termdeck] {n}\r\n" for n in notices).encode()
        self.title = conn.profile.name

    async def read(self) -> bytes:
        if self._pending:
            data, self._pending = self._pending, b""
            return data
        try:
            return await self._process.stdout.read(65536)
        except (asyncssh.Error, OSError):
            return b""

    async def write(self, data: bytes) -> None:
        if not self._process.stdin.is_closing():
            self._process.stdin.write(data)

    def resize(self, cols: int, rows: int) -> None:
        self._process.change_terminal_size(cols, rows)

    async def close(self) -> None:
        self.connection.close()
        await self.connection.wait_closed()
