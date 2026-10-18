import asyncio

import asyncssh
import pytest

from termdeck.profiles import Profile, SSHSettings

USER, PASSWORD = "alice", "secret"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep tests away from the real user config, keys, agent and ssh config."""
    monkeypatch.setenv("TERMDECK_HOME", str(tmp_path / "termdeck"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    (tmp_path / "home").mkdir()


class FakePrompter:
    def __init__(self, answers=None, trust=True):
        self.answers = list(answers or [PASSWORD])
        self.trust = trust
        self.asked: list[str] = []
        self.host_keys: list[tuple] = []
        self.keys: list = []
        self.notices: list[str] = []

    async def ask(self, prompt, secret=True):
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else None

    async def confirm_host_key(self, key):
        self.host_keys.append((key.host, key.port, key.fingerprint, key.changed))
        self.keys.append(key)
        return self.trust

    async def notify(self, text):
        self.notices.append(text)


class _Server(asyncssh.SSHServer):
    """Behaviour is chosen per test through the owning SSHServer (banner, kbdint, expired)."""

    def __init__(self, owner):
        self.owner = owner

    def connection_made(self, conn):
        self.conn = conn

    def server_requested_listener(self, listen_host, listen_port):
        return True  # remote forwards (ssh -R)

    def begin_auth(self, username):
        if self.owner.banner:
            self.conn.send_auth_banner(self.owner.banner)
        return True

    def password_auth_supported(self):
        return not self.owner.kbdint

    def validate_password(self, username, password):
        if self.owner.expired:
            raise asyncssh.PasswordChangeRequired("Your password has expired")
        return username == USER and password == PASSWORD

    def change_password(self, username, old_password, new_password):
        self.owner.changes.append((old_password, new_password))
        return True

    def kbdint_auth_supported(self):
        return self.owner.kbdint

    def get_kbdint_challenge(self, username, lang, submethods):
        return "MFA", "Enter the code from your phone", "", [("Code: ", False)]

    def validate_kbdint_response(self, username, responses):
        return responses == [PASSWORD]

    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return True

    def server_requested(self, listen_host, listen_port):
        return True


async def _shell(process: asyncssh.SSHServerProcess):
    if process.command == "exit3":
        process.exit(3)
        return
    if process.command == "abort":  # the connection drops without a word
        process.channel.get_connection().abort()
        return
    if process.command == "disconnect":  # the server says goodbye with a reason
        # Not DISC_BY_APPLICATION: asyncssh treats that as a normal close and drops the reason.
        process.channel.get_connection().disconnect(asyncssh.DISC_SERVICE_NOT_AVAILABLE, "bye bye")
        return
    if process.subsystem:
        process.stdout.write(f"subsystem {process.subsystem}\r\n")
        process.exit(0)
        return
    if process.command == "kill":  # the remote program dies of a signal
        process.exit_with_signal("KILL")
        return
    if process.command == "enq":  # asks for the answerback, then reports the first thing it receives
        process.stdout.write("\x05")
        process.stdout.write(f"got {await process.stdin.read(1024)!r}\r\n")
        process.exit(0)
        return
    if process.command == "err":  # one line on each stream
        process.stdout.write("to-stdout\n")
        process.stderr.write("to-stderr\n")
        process.exit(0)
        return
    if process.command:
        process.stdout.write(f"ran {process.command} FOO={process.env.get('FOO')}\r\n")
        process.exit(0)
        return
    cols, rows = process.get_terminal_size()[:2]
    process.stdout.write(f"hello {process.get_terminal_type()} {cols}x{rows}\r\n")
    while True:
        try:
            data = await process.stdin.read(1024)
        except asyncssh.TerminalSizeChanged as exc:
            process.stdout.write(f"resized {exc.width}x{exc.height}\r\n")
            continue
        if not data or data.strip() == "exit":
            break
        process.stdout.write(data)
    process.exit(0)


class SSHServer:
    def __init__(self, root):
        self.root = root
        self.key = asyncssh.generate_private_key("ssh-ed25519")
        self.banner = ""        # sent before authentication
        self.kbdint = False     # offer keyboard-interactive (one prompt) instead of passwords
        self.expired = False    # passwords are accepted only through a password change
        self.changes: list[tuple[str, str]] = []  # (old, new) password changes received

    async def start(self, port=0):
        root = str(self.root)
        self.listener = await asyncssh.listen(
            "127.0.0.1",
            port,
            server_factory=lambda: _Server(self),
            server_host_keys=[self.key],
            process_factory=_shell,
            sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=root),
        )
        self.port = self.listener.get_port()
        return self

    def profile(self, name="test", **ssh):
        return Profile(name, "ssh", ssh=SSHSettings(host="127.0.0.1", port=self.port, username=USER, **ssh))


@pytest.fixture
async def ssh_server(tmp_path):
    root = tmp_path / "sftp-root"
    root.mkdir()
    server = await SSHServer(root).start()
    yield server
    server.listener.close()


@pytest.fixture
def patch_port(monkeypatch):
    """patch_port(**attrs): every serial port opened from now on gets `attrs` set on it (a failing
    read, a blocking open...). The functions get the port's original method as first argument.
    Returns the list of ports created and the keyword arguments they were created with."""
    import serial

    original = serial.serial_for_url
    created: list[tuple] = []

    def install(**attrs):
        def factory(url, *args, **kwargs):
            port = original(url, *args, **kwargs)
            for name, fn in attrs.items():
                setattr(port, name, (lambda fn, real: lambda *a, **k: fn(real, *a, **k))(fn, getattr(port, name)))
            created.append((port, kwargs))
            return port

        monkeypatch.setattr(serial, "serial_for_url", factory)
        return created

    return install


async def read_until(session, marker: bytes, timeout=5.0) -> bytes:
    buf = b""

    async def loop():
        nonlocal buf
        while marker not in buf:
            chunk = await session.read()
            if not chunk:
                break
            buf += chunk

    await asyncio.wait_for(loop(), timeout)
    return buf
