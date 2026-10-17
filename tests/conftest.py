import asyncio
import socket

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

    async def ask(self, prompt, secret=True):
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else None

    async def confirm_host_key(self, host, port, fingerprint, changed):
        self.host_keys.append((host, port, fingerprint, changed))
        return self.trust


class _Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == USER and password == PASSWORD

    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return True

    def server_requested(self, listen_host, listen_port):
        return True


async def _shell(process: asyncssh.SSHServerProcess):
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

    async def start(self):
        root = str(self.root)
        self.listener = await asyncssh.listen(
            "127.0.0.1",
            0,
            server_factory=_Server,
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
