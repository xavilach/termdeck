"""Batch 6 SSH options: proxies, trusted keys, algorithm lists, terminal modes, ssh config, sharing..."""

import asyncio
import getpass
import shutil
import socket
import struct
import sys
import types

import asyncssh
import pytest

from termdeck.errors import ConnectError, ProfileError
from termdeck.profiles import Profile, SSHSettings
from termdeck.proxy import proxy_command, uses_proxy
from termdeck.session import open_session
from termdeck.ssh import SSHConnection, known_hosts_path, remember_host_key

from .conftest import PASSWORD, USER, FakePrompter, read_until

# ---------------------------------------------------------------- helpers


class OptServer:
    """A small SSH server whose behaviour tests choose: counts logins and connections, records the pty."""

    def __init__(self, trivial=False, **listen):
        self.trivial = trivial
        self.binary = listen.get("encoding", "utf-8") is None  # byte streams, for the relay commands
        self.target: tuple | None = None  # where the "relay" subsystem connects to
        self.commands: list[str] = []
        self.subsystems: list[str] = []
        self.listen = listen
        self.key = asyncssh.generate_private_key("ssh-ed25519")
        self.logins = 0
        self.open = 0
        self.modes: dict = {}
        self.pty_seen = asyncio.Event()
        self.closed = asyncio.Event()

    async def start(self):
        owner = self

        class Server(asyncssh.SSHServer):
            def connection_made(self, conn):
                owner.open += 1

            def connection_lost(self, exc):
                owner.open -= 1
                owner.closed.set()

            def begin_auth(self, username):
                return not owner.trivial

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                owner.logins += 1
                return username == USER and password == PASSWORD

        async def relay(process, host, port):
            up_r, up_w = await asyncio.open_connection(host, port)

            async def down():
                while data := await up_r.read(65536):
                    process.stdout.write(data)
                    await process.stdout.drain()
                process.exit(0)

            task = asyncio.ensure_future(down())
            try:
                while data := await process.stdin.read(65536):
                    up_w.write(data)
                    await up_w.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                up_w.close()
                task.cancel()

        async def shell(process):
            if process.subsystem:  # "relay": a connection to the target, like "nc HOST PORT"
                owner.subsystems.append(process.subsystem)
                if process.subsystem == "relay":
                    await relay(process, *owner.target)
                else:
                    process.exit(1)
                return
            if process.command:
                owner.commands.append(process.command)
                verb, *args = process.command.split()
                if verb == "relay":  # relay HOST PORT
                    await relay(process, args[0], int(args[1]))
                else:  # a failing program
                    process.stderr.write(b"nc: refused\n" if owner.binary else "nc: refused\n")
                    process.exit(1)
                return
            owner.modes = {"erase": process.get_terminal_mode(asyncssh.PTY_VERASE),
                           "echo": process.get_terminal_mode(asyncssh.PTY_ECHO),
                           "all": dict(process.term_modes)}
            owner.pty_seen.set()
            process.stdout.write(b"ready\r\n" if owner.binary else "ready\r\n")
            while True:
                try:
                    data = await process.stdin.read(1024)
                except asyncssh.TerminalSizeChanged:
                    continue
                if not data:
                    break
                process.stdout.write(data)
            process.exit(0)

        self.listener = await asyncssh.listen("127.0.0.1", 0, server_factory=Server, server_host_keys=[self.key],
                                              process_factory=shell, **self.listen)
        self.port = self.listener.get_port()
        return self

    def profile(self, **ssh):
        ssh.setdefault("host", "127.0.0.1")
        return Profile("opt", "ssh", ssh=SSHSettings(port=self.port, username=USER, **ssh))

    def trust(self, host="127.0.0.1"):
        remember_host_key(host, self.port, self.key)


@pytest.fixture
async def opt_server():
    servers = []

    async def make(**kw):
        servers.append(await OptServer(**kw).start())
        return servers[-1]

    yield make
    for s in servers:
        s.listener.close()


async def _pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        writer.close()


async def _relay(reader, writer, port):
    up_r, up_w = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))


class Proxy:
    def __init__(self, handler):
        self.handler = handler
        self.seen: list = []  # what the proxy was asked for

    async def start(self, target_port):
        self.target = target_port

        async def run(reader, writer):
            try:
                await self.handler(self, reader, writer)
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                writer.close()

        self.server = await asyncio.start_server(run, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self


@pytest.fixture
async def proxy_for():
    proxies = []

    async def make(handler, target_port):
        proxies.append(await Proxy(handler).start(target_port))
        return proxies[-1]

    yield make
    for p in proxies:
        p.server.close()


def socks5(users=None):
    async def handler(p, r, w):
        n = (await r.readexactly(2))[1]
        methods = await r.readexactly(n)
        if users is None:
            w.write(b"\5\0")
        else:
            assert 2 in methods
            w.write(b"\5\2")
            await r.readexactly(1)
            user = await r.readexactly((await r.readexactly(1))[0])
            password = await r.readexactly((await r.readexactly(1))[0])
            ok = users.get(user.decode()) == password.decode()
            w.write(b"\1\0" if ok else b"\1\1")
            p.seen.append(("auth", user.decode(), password.decode()))
            if not ok:
                await w.drain()
                w.close()
                return
        _, cmd, _, atyp = await r.readexactly(4)
        if atyp == 3:
            host = (await r.readexactly((await r.readexactly(1))[0])).decode()
        else:
            host = (await r.readexactly(4 if atyp == 1 else 16)).hex()
        await r.readexactly(2)
        p.seen.append(("connect", atyp, host))
        w.write(b"\5\0\0\1" + bytes(6))
        await _relay(r, w, p.target)
    return handler


def socks4():
    async def handler(p, r, w):
        _, _, _port = struct.unpack(">BBH", await r.readexactly(4))
        ip = await r.readexactly(4)
        user = (await r.readuntil(b"\0"))[:-1].decode()
        name = None
        if ip[:3] == b"\0\0\0" and ip[3]:
            name = (await r.readuntil(b"\0"))[:-1].decode()
        p.seen.append(("connect", name or ".".join(map(str, ip)), user))
        w.write(b"\0\x5a" + bytes(6))
        await _relay(r, w, p.target)
    return handler


def http(credentials=None, status=200):
    async def handler(p, r, w):
        head = (await r.readuntil(b"\r\n\r\n")).decode()
        p.seen.append(head)
        auth_ok = credentials is None or f"Proxy-Authorization: Basic {credentials}" in head
        if not auth_ok or status != 200:
            w.write(f"HTTP/1.1 {407 if not auth_ok else status} Nope\r\n\r\n".encode())
            await w.drain()
            w.close()
            return
        w.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await _relay(r, w, p.target)
    return handler


def named(server, host, **ssh):
    """The server's profile, reached under another host name."""
    profile = server.profile(**ssh)
    profile.ssh.host = host
    return profile


async def shell_works(session, text=b"ping"):
    await read_until(session, b"\n")
    await session.write(text)
    assert text in await read_until(session, text)


# ---------------------------------------------------------------- proxies


async def test_socks5_proxy_without_auth(ssh_server, proxy_for):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(socks5(), ssh_server.port)
    prompter = FakePrompter()
    profile = ssh_server.profile(proxy_type="socks5", proxy_host="127.0.0.1", proxy_port=proxy.port, proxy_local=True)
    session = await open_session(profile, prompter, 80, 24)
    try:
        await shell_works(session)
    finally:
        await session.close()
    assert ("connect", 1, "7f000001") in proxy.seen
    assert len(prompter.asked) == 1 and "password" in prompter.asked[0]  # only the SSH one


async def test_local_host_bypasses_the_proxy_unless_proxy_local(ssh_server, proxy_for):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(socks5(), ssh_server.port)
    profile = ssh_server.profile(proxy_type="socks5", proxy_host="127.0.0.1", proxy_port=proxy.port)
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()
    assert proxy.seen == []


async def test_socks5_proxy_password_is_asked_and_sent(ssh_server, proxy_for):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(socks5({"bob": "pw"}), ssh_server.port)
    prompter = FakePrompter(answers=["pw", PASSWORD])
    profile = ssh_server.profile(proxy_type="socks5", proxy_host="127.0.0.1", proxy_port=proxy.port,
                                 proxy_local=True, proxy_username="bob")
    conn = await SSHConnection.connect(profile, prompter)
    conn.close()
    assert prompter.asked[0].startswith("bob@127.0.0.1:") and prompter.asked[0].endswith(" proxy password: ")
    assert ("auth", "bob", "pw") in proxy.seen


async def test_socks5_wrong_proxy_password_is_a_clear_error(ssh_server, proxy_for):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(socks5({"bob": "pw"}), ssh_server.port)
    prompter = FakePrompter(answers=["wrong", PASSWORD])
    profile = ssh_server.profile(proxy_type="socks5", proxy_host="127.0.0.1", proxy_port=proxy.port,
                                 proxy_local=True, proxy_username="bob")
    with pytest.raises(ConnectError, match="proxy login failed for bob"):
        await SSHConnection.connect(profile, prompter)
    assert len(prompter.asked) == 1  # no SSH password asked


async def test_cancelled_proxy_password_is_a_connect_error(ssh_server, proxy_for):
    proxy = await proxy_for(socks5({"bob": "pw"}), ssh_server.port)
    profile = ssh_server.profile(proxy_type="socks5", proxy_host="127.0.0.1", proxy_port=proxy.port,
                                 proxy_local=True, proxy_username="bob")
    prompter = FakePrompter()
    prompter.answers = []  # the user cancels
    with pytest.raises(ConnectError, match="proxy password not given"):
        await SSHConnection.connect(profile, prompter)


async def test_proxy_that_is_not_listening_is_a_connect_error(ssh_server):
    with socket.socket() as bound:  # bound but not listening: refused, and no one else can take the port
        bound.bind(("127.0.0.1", 0))
        profile = ssh_server.profile(proxy_type="socks5", proxy_host="127.0.0.1",
                                     proxy_port=bound.getsockname()[1], proxy_local=True)
        with pytest.raises(ConnectError, match="cannot connect to the proxy"):
            await SSHConnection.connect(profile, FakePrompter())


async def test_socks4_proxy_sends_ip_and_user(ssh_server, proxy_for):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(socks4(), ssh_server.port)
    profile = ssh_server.profile(proxy_type="socks4", proxy_host="127.0.0.1", proxy_port=proxy.port,
                                 proxy_local=True, proxy_username="carol")
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()
    assert proxy.seen == [("connect", "127.0.0.1", "carol")]


async def test_socks4a_proxy_gets_the_name(ssh_server, proxy_for):
    remember_host_key("ssh.example.test", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(socks4(), ssh_server.port)
    profile = named(ssh_server, "ssh.example.test", proxy_type="socks4", proxy_host="127.0.0.1",
                                 proxy_port=proxy.port, proxy_dns="yes")  # auto resolves here for SOCKS 4
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()
    assert proxy.seen[0][1] == "ssh.example.test"


async def test_socks5_proxy_resolves_names_unless_proxy_dns_is_off(ssh_server, proxy_for):
    remember_host_key("ssh.example.test", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(socks5(), ssh_server.port)
    profile = named(ssh_server, "ssh.example.test", proxy_type="socks5", proxy_host="127.0.0.1",
                                 proxy_port=proxy.port)
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()
    assert proxy.seen == [("connect", 3, "ssh.example.test")]


async def test_proxy_dns_off_resolves_locally_and_sends_an_address(ssh_server, proxy_for):
    remember_host_key("localhost", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(socks5(), ssh_server.port)
    profile = named(ssh_server, "localhost", proxy_type="socks5", proxy_host="127.0.0.1",
                                 proxy_port=proxy.port, proxy_local=True, proxy_dns=False)
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()
    [(_, atyp, _)] = proxy.seen
    assert atyp in (1, 4)  # an IP, not a name


async def test_proxy_dns_off_with_an_unresolvable_name_says_so(ssh_server, proxy_for):
    proxy = await proxy_for(socks5(), ssh_server.port)
    profile = named(ssh_server, "no-such-host.invalid", proxy_type="socks5", proxy_host="127.0.0.1",
                                 proxy_port=proxy.port, proxy_dns=False)
    with pytest.raises(ConnectError, match="cannot resolve no-such-host.invalid"):
        await SSHConnection.connect(profile, FakePrompter())


async def test_http_connect_proxy(ssh_server, proxy_for):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(http(), ssh_server.port)
    profile = ssh_server.profile(proxy_type="http", proxy_host="127.0.0.1", proxy_port=proxy.port, proxy_local=True)
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()
    assert proxy.seen[0].startswith(f"CONNECT 127.0.0.1:{ssh_server.port} HTTP/1.1")


async def test_http_proxy_basic_auth_asks_for_the_password(ssh_server, proxy_for):
    import base64

    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(http(base64.b64encode(b"dave:pw").decode()), ssh_server.port)
    prompter = FakePrompter(answers=["pw", PASSWORD])
    profile = ssh_server.profile(proxy_type="http", proxy_host="127.0.0.1", proxy_port=proxy.port,
                                 proxy_local=True, proxy_username="dave")
    conn = await SSHConnection.connect(profile, prompter)
    conn.close()
    assert prompter.asked[0].startswith("dave@127.0.0.1:") and prompter.asked[0].endswith(" proxy password: ")


async def test_http_proxy_407_without_user_mentions_the_proxy_user(ssh_server, proxy_for):
    proxy = await proxy_for(http("x"), ssh_server.port)
    profile = ssh_server.profile(proxy_type="http", proxy_host="127.0.0.1", proxy_port=proxy.port, proxy_local=True)
    with pytest.raises(ConnectError, match=r"407.*proxy user"):
        await SSHConnection.connect(profile, FakePrompter())


async def test_http_proxy_refusal_shows_the_status(ssh_server, proxy_for):
    proxy = await proxy_for(http(status=403), ssh_server.port)
    profile = ssh_server.profile(proxy_type="http", proxy_host="127.0.0.1", proxy_port=proxy.port, proxy_local=True)
    with pytest.raises(ConnectError, match="403"):
        await SSHConnection.connect(profile, FakePrompter())


RELAY = """
import socket, sys, threading
s = socket.create_connection((sys.argv[1], int(sys.argv[2])))
def up():
    while d := sys.stdin.buffer.read1(65536):
        s.sendall(d)
    s.shutdown(socket.SHUT_WR)
threading.Thread(target=up, daemon=True).start()
while d := s.recv(65536):
    sys.stdout.buffer.write(d)
    sys.stdout.buffer.flush()
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX quoting")
async def test_proxy_command_relays_the_connection(ssh_server, tmp_path):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    script = tmp_path / "relay.py"
    script.write_text(RELAY)
    profile = ssh_server.profile(proxy_type="command", proxy_local=True,
                                 proxy_command=f"'{sys.executable}' '{script}' %host %port")
    session = await open_session(profile, FakePrompter(), 80, 24)
    try:
        await shell_works(session)
    finally:
        await session.close()


@pytest.mark.skipif(shutil.which("nc") is None, reason="needs nc")
async def test_proxy_command_with_nc(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    profile = ssh_server.profile(proxy_type="command", proxy_local=True, proxy_command="nc %host %port")
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()


def test_proxy_command_substitution():
    s = SSHSettings(proxy_type="command", proxy_host="px", proxy_port=8080,
                    proxy_command="relay %host %port %user 100%% %proxyhost:%proxyport")
    assert proxy_command(s, "h.example", 2222, "al ice") == ["relay", "h.example", "2222", "al ice", "100%", "px:8080"]


def test_proxy_command_percent_percent_is_not_substituted_twice():
    s = SSHSettings(proxy_type="command", proxy_command="echo %%host %host")
    assert proxy_command(s, "h", 22, "u") == ["echo", "%host", "h"]


def test_uses_proxy_rules():
    def uses(host, **kw):
        return uses_proxy(SSHSettings(proxy_type="socks5", proxy_host="p", **kw), host)

    assert not uses_proxy(SSHSettings(), "example.com")  # no proxy configured
    assert uses("example.com")
    assert not uses("localhost") and not uses("127.0.0.1") and not uses("::1") and not uses("a.localhost")
    assert uses("localhost", proxy_local=True) and uses("127.0.0.1", proxy_local=True)
    assert not uses("box.lan", proxy_exclude="*.lan") and uses("box.lan.example.com", proxy_exclude="*.lan")
    assert not uses("Exact.Example.com", proxy_exclude="exact.example.com")
    assert uses("other.example.com", proxy_exclude="exact.example.com")
    assert not uses("10.1.2.3", proxy_exclude="10.0.0.0/8") and uses("11.1.2.3", proxy_exclude="10.0.0.0/8")
    assert uses("somehost", proxy_exclude="10.0.0.0/8")  # a name is not in a network
    assert not uses("db.corp", proxy_exclude=".corp") and uses("corp", proxy_exclude="x.corp")
    assert not uses("a.example.com", proxy_exclude="x.lan; a.example.com")
    assert not uses("a.example.com", proxy_exclude="x.lan, a.example.com")


@pytest.mark.parametrize("kind", ["socks4", "socks5", "http"])
def test_proxy_host_is_required(kind):
    profile = Profile("p", "ssh", ssh=SSHSettings(host="h", username="u", proxy_type=kind))
    with pytest.raises(ProfileError, match="proxy host"):
        profile.validate()
    profile.ssh.proxy_host = "proxy.example.com"
    profile.validate()


def test_proxy_command_is_required_for_command_proxy():
    profile = Profile("p", "ssh", ssh=SSHSettings(host="h", username="u", proxy_type="command", proxy_command="  "))
    with pytest.raises(ProfileError, match="proxy command"):
        profile.validate()
    profile.ssh.proxy_command = "nc %host %port"
    profile.validate()


def test_invalid_option_values_in_json_are_profile_errors():
    base = {"name": "p", "kind": "ssh", "ssh": {"host": "h", "username": "u"}}
    for bad in ({"proxy_type": "ftp"}, {"rekey_minutes": -1}, {"rekey_mb": "lots"}, {"terminal_speed": 0},
                {"proxy_port": 70000}, {"kbdint_auth": "yes"}):
        data = {**base, "ssh": {**base["ssh"], **bad}}
        with pytest.raises(ProfileError):
            Profile.from_dict(data)


# ---------------------------------------------------------------- trusted keys


async def test_trusted_fingerprint_connects_without_prompt_or_known_hosts(ssh_server):
    prompter = FakePrompter()
    profile = ssh_server.profile(trusted_keys=ssh_server.key.get_fingerprint("sha256"))
    conn = await SSHConnection.connect(profile, prompter)
    conn.close()
    assert prompter.host_keys == []
    assert not known_hosts_path().exists() or "ssh-ed25519" not in known_hosts_path().read_text()


async def test_trusted_md5_fingerprint_and_several_lines(ssh_server):
    other = asyncssh.generate_private_key("ssh-ed25519").get_fingerprint("sha256")
    prompter = FakePrompter()
    profile = ssh_server.profile(trusted_keys=f"{other}\n{ssh_server.key.get_fingerprint('md5')}\n")
    conn = await SSHConnection.connect(profile, prompter)
    conn.close()
    assert prompter.host_keys == []


async def test_other_key_than_the_trusted_ones_is_refused_without_asking(ssh_server):
    """Trusted host keys pin the key, as in PuTTY: known_hosts is not consulted, nothing is asked."""
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)  # even a known_hosts entry does not help
    other = asyncssh.generate_private_key("ssh-ed25519").get_fingerprint("sha256")
    prompter = FakePrompter()
    with pytest.raises(ConnectError, match="not one of the profile's Trusted host keys"):
        await SSHConnection.connect(ssh_server.profile(trusted_keys=other), prompter)
    assert prompter.host_keys == []


@pytest.mark.parametrize("line", ["SHA256:short", "sha256:" + "A" * 43, "not a fingerprint", "MD5:12:34",
                                  "SHA256:" + "A" * 43 + "\nbogus"])
def test_invalid_trusted_key_line_is_a_profile_error(line):
    profile = Profile("p", "ssh", ssh=SSHSettings(host="h", username="u", trusted_keys=line))
    with pytest.raises(ProfileError, match="fingerprint"):
        profile.validate()


# ---------------------------------------------------------------- algorithms


async def test_unknown_cipher_lists_the_supported_ones(ssh_server):
    with pytest.raises(ProfileError, match=r"unknown cipher rot13.*supported:.*aes128-ctr"):
        await SSHConnection.connect(ssh_server.profile(ciphers="aes128-ctr, rot13"), FakePrompter())


async def test_unknown_algorithm_of_other_kinds_is_reported(ssh_server):
    for field, what in (("kex_algs", "key exchange"), ("mac_algs", "MAC"), ("hostkey_algs", "host key type")):
        with pytest.raises(ProfileError, match=f"unknown {what} bogus"):
            await SSHConnection.connect(ssh_server.profile(**{field: "bogus"}), FakePrompter())


async def test_weak_cipher_is_noticed(opt_server):
    server = await opt_server(encryption_algs=["aes128-cbc", "aes128-ctr"])
    prompter = FakePrompter()
    conn = await SSHConnection.connect(server.profile(ciphers="aes128-cbc"), prompter)
    conn.close()
    assert any("weak cipher" in n and "aes128-cbc" in n for n in prompter.notices)


async def test_strong_cipher_gives_no_notice(ssh_server):
    prompter = FakePrompter()
    conn = await SSHConnection.connect(ssh_server.profile(ciphers="aes256-ctr"), prompter)
    conn.close()
    assert not any("weak" in n for n in prompter.notices)


async def test_mac_hostkey_and_kex_algorithms_reach_asyncssh(ssh_server, monkeypatch):
    captured = {}
    real = asyncssh.connect

    def spy(*a, **kw):
        captured.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(asyncssh, "connect", spy)
    profile = ssh_server.profile(mac_algs="hmac-sha2-256", hostkey_algs="ssh-ed25519", ciphers="aes256-ctr")
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()
    assert captured["mac_algs"] == ["hmac-sha2-256"]
    assert captured["server_host_key_algs"] == ["ssh-ed25519"]
    assert captured["encryption_algs"] == ["aes256-ctr"]


async def test_host_key_algorithm_the_server_lacks_fails(ssh_server):
    profile = ssh_server.profile(hostkey_algs="ecdsa-sha2-nistp256")
    with pytest.raises(asyncssh.Error):
        await SSHConnection.connect(profile, FakePrompter())


# ---------------------------------------------------------------- terminal modes


async def test_tty_modes_and_speed_reach_the_server(opt_server):
    server = await opt_server()
    server.trust()
    profile = server.profile(tty_modes="ERASE=127\nECHO=0\n\n", terminal_speed=9600)
    session = await open_session(profile, FakePrompter(), 80, 24)
    try:
        await asyncio.wait_for(server.pty_seen.wait(), 5)
    finally:
        await session.close()
    assert server.modes["erase"] == 127 and server.modes["echo"] == 0
    assert server.modes["all"][asyncssh.PTY_OP_ISPEED] == 9600
    assert server.modes["all"][asyncssh.PTY_OP_OSPEED] == 9600


def test_term_modes_maps_names_to_opcodes():
    from termdeck.ssh import term_modes

    profile = Profile("p", "ssh", ssh=SSHSettings(tty_modes="ERASE=8\nvintr=3", terminal_speed=19200))
    modes = term_modes(profile)
    assert modes[asyncssh.PTY_VERASE] == 8 and modes[asyncssh.PTY_VINTR] == 3
    assert modes[asyncssh.PTY_OP_ISPEED] == 19200 == modes[asyncssh.PTY_OP_OSPEED]


def test_erase_and_iutf8_follow_the_terminal_settings():
    """PuTTY's "Auto": ERASE from the Backspace key setting, IUTF8 from the character set."""
    from termdeck.ssh import term_modes

    profile = Profile("p", "ssh")
    assert term_modes(profile)[asyncssh.PTY_VERASE] == 127 and term_modes(profile)[asyncssh.PTY_IUTF8] == 1
    profile.terminal.backspace_sends, profile.terminal.encoding = "bs", "latin-1"
    assert term_modes(profile)[asyncssh.PTY_VERASE] == 8 and term_modes(profile)[asyncssh.PTY_IUTF8] == 0


def test_malformed_tty_mode_line_is_a_profile_error():
    for text in ("ERASE", "ERASE=", "ERASE=abc", "=3", "1X=3", "ERASE=-1"):
        profile = Profile("p", "ssh", ssh=SSHSettings(host="h", username="u", tty_modes=text))
        with pytest.raises(ProfileError, match="terminal mode"):
            profile.validate()


async def test_unknown_tty_mode_name_is_a_connect_error(ssh_server):
    with pytest.raises(ProfileError, match="unknown terminal mode NOSUCH"):
        await open_session(ssh_server.profile(tty_modes="NOSUCH=1"), FakePrompter(), 80, 24)


# ---------------------------------------------------------------- username, ssh config, auth


async def test_username_from_env_does_not_ask(opt_server, monkeypatch):
    server = await opt_server()
    server.trust()
    monkeypatch.setattr(getpass, "getuser", lambda: USER)
    prompter = FakePrompter()
    profile = server.profile(username_from_env=True)
    profile.ssh.username = ""
    conn = await SSHConnection.connect(profile, prompter)
    conn.close()
    assert len(prompter.asked) == 1 and "login as" not in prompter.asked[0]


async def test_username_from_env_off_still_asks(opt_server, monkeypatch):
    server = await opt_server()
    server.trust()
    monkeypatch.setattr(getpass, "getuser", lambda: "someone-else")
    prompter = FakePrompter(answers=[USER, PASSWORD])
    profile = server.profile()
    profile.ssh.username = ""
    conn = await SSHConnection.connect(profile, prompter)
    conn.close()
    assert prompter.asked[0] == "login as (127.0.0.1): "


def _ssh_config(tmp_path, server):
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "config").write_text(f"Host termdeck-alias\n  Port {server.port}\n  HostName 127.0.0.1\n")


async def test_use_ssh_config_applies_host_blocks_only_when_on(opt_server, tmp_path):
    server = await opt_server()
    server.trust()  # the key is looked up under the HostName the config resolves to
    _ssh_config(tmp_path, server)
    conn = await SSHConnection.connect(server.profile(host="termdeck-alias", use_ssh_config=True), FakePrompter())
    conn.close()
    with pytest.raises(OSError):  # the alias does not resolve without the config
        await SSHConnection.connect(server.profile(host="termdeck-alias"), FakePrompter())


async def test_use_ssh_config_alias_host_key_can_be_accepted_at_the_prompt(opt_server, tmp_path):
    server = await opt_server()
    _ssh_config(tmp_path, server)
    conn = await SSHConnection.connect(server.profile(host="termdeck-alias", use_ssh_config=True), FakePrompter())
    conn.close()


async def test_use_ssh_config_port_applies_when_the_profile_has_the_default_port(opt_server, tmp_path):
    server = await opt_server()
    server.trust()
    _ssh_config(tmp_path, server)
    profile = server.profile(host="termdeck-alias", use_ssh_config=True)
    profile.ssh.port = 22
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()


async def test_no_trivial_auth_refuses_a_server_that_lets_anyone_in(opt_server):
    server = await opt_server(trivial=True)
    server.trust()
    with pytest.raises(asyncssh.Error):
        await SSHConnection.connect(server.profile(no_trivial_auth=True), FakePrompter())
    conn = await SSHConnection.connect(server.profile(no_trivial_auth=False), FakePrompter())
    conn.close()


async def test_no_trivial_auth_does_not_hurt_a_normal_server(opt_server):
    server = await opt_server()
    server.trust()
    conn = await SSHConnection.connect(server.profile(no_trivial_auth=True), FakePrompter())
    conn.close()


async def test_kbdint_auth_off_fails_against_a_kbdint_only_server(ssh_server):
    ssh_server.kbdint = True
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    prompter = FakePrompter()
    with pytest.raises(asyncssh.PermissionDenied):
        await SSHConnection.connect(ssh_server.profile(kbdint_auth=False), prompter)
    assert prompter.asked == []
    conn = await SSHConnection.connect(ssh_server.profile(kbdint_auth=True), FakePrompter())
    conn.close()


# ---------------------------------------------------------------- rekey


@pytest.mark.parametrize(("minutes", "mb", "seconds", "size"), [(5, 2, 300, 2 * 2**20), (0, 0, None, None)])
async def test_rekey_limits_reach_asyncssh(ssh_server, monkeypatch, minutes, mb, seconds, size):
    captured = {}
    real = asyncssh.connect

    def spy(*a, **kw):
        captured.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(asyncssh, "connect", spy)
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    conn = await SSHConnection.connect(ssh_server.profile(rekey_minutes=minutes, rekey_mb=mb), FakePrompter())
    conn.close()
    if seconds is None:  # 0 means never: a limit nobody reaches
        assert captured["rekey_seconds"] > 10**12 and captured["rekey_bytes"] > 10**15
    else:
        assert (captured["rekey_seconds"], captured["rekey_bytes"]) == (seconds, size)


# ---------------------------------------------------------------- sharing, logical host


async def test_share_connection_logs_in_once_and_keeps_the_other_tab(opt_server):
    server = await opt_server()
    server.trust()
    prompter = FakePrompter(answers=[PASSWORD, PASSWORD])
    profile = server.profile(share_connection=True)
    one = await open_session(profile, prompter, 80, 24)
    two = await open_session(profile, prompter, 80, 24)
    try:
        assert server.logins == 1 and server.open == 1 and len(prompter.asked) == 1
        await one.write(b"first")
        assert b"first" in await read_until(one, b"first")
        await two.write(b"second")
        out = await read_until(two, b"second")
        assert b"second" in out and b"first" not in out
        await one.close()
        await two.write(b"still")
        assert b"still" in await read_until(two, b"still")
        assert server.open == 1
    finally:
        await one.close()
        await two.close()
    await asyncio.wait_for(server.closed.wait(), 5)
    assert server.open == 0


async def test_share_connection_reconnects_after_all_tabs_closed(opt_server):
    server = await opt_server()
    server.trust()
    profile = server.profile(share_connection=True)
    first = await open_session(profile, FakePrompter(), 80, 24)
    await first.close()
    second = await open_session(profile, FakePrompter(), 80, 24)
    try:
        await shell_works(second)
        assert server.logins == 2
    finally:
        await second.close()


async def test_without_share_connection_each_tab_logs_in(opt_server):
    server = await opt_server()
    server.trust()
    prompter = FakePrompter(answers=[PASSWORD, PASSWORD])
    profile = server.profile()
    one = await open_session(profile, prompter, 80, 24)
    two = await open_session(profile, prompter, 80, 24)
    try:
        assert server.logins == 2 and server.open == 2
        await one.close()
        await shell_works(two)
    finally:
        await one.close()
        await two.close()


async def test_logical_host_looks_up_the_key_under_that_name(ssh_server):
    remember_host_key("alias", ssh_server.port, ssh_server.key)
    prompter = FakePrompter()
    conn = await SSHConnection.connect(ssh_server.profile(logical_host="alias"), prompter)
    conn.close()
    assert prompter.host_keys == []
    # without the alias the key of 127.0.0.1 is unknown
    prompter = FakePrompter()
    conn = await SSHConnection.connect(ssh_server.profile(), prompter)
    conn.close()
    assert len(prompter.host_keys) == 1


# ---------------------------------------------------------------- batch 6 review: proxy command, telnet, dns


# Writes to stderr in the middle of the SSH exchange, where stray bytes in the stream would break it.
STDERR_RELAY = RELAY.replace(
    "while d := s.recv(65536):\n",
    "while d := s.recv(65536):\n    sys.stderr.write('proxy says hello\\nsecond line\\n'); sys.stderr.flush()\n")


def _command_profile(server, tmp_path, source, args="%host %port"):
    script = tmp_path / "proxy.py"
    script.write_text(source)
    return server.profile(proxy_type="command", proxy_local=True,
                          proxy_command=f"'{sys.executable}' '{script}' {args}")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX quoting")
async def test_proxy_command_stderr_stays_out_of_the_ssh_stream(ssh_server, tmp_path):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    profile = _command_profile(ssh_server, tmp_path, STDERR_RELAY)
    session = await open_session(profile, FakePrompter(), 80, 24)  # the handshake would fail on stray text
    try:
        await shell_works(session)
    finally:
        await session.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX quoting")
async def test_proxy_command_that_fails_shows_its_error_output(ssh_server, tmp_path):
    from termdeck.session import describe_error

    profile = _command_profile(ssh_server, tmp_path, "import sys\nsys.stderr.write('boom: refused\\n')\nsys.exit(1)\n")
    with pytest.raises(Exception) as caught:
        await SSHConnection.connect(profile, FakePrompter())
    assert "boom: refused" in describe_error(caught.value)
    assert "proxy command" in describe_error(caught.value)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX quoting")
async def test_proxy_command_that_fails_silently_says_so(ssh_server, tmp_path):
    from termdeck.session import describe_error

    profile = _command_profile(ssh_server, tmp_path, "import sys\nsys.exit(1)\n")
    with pytest.raises(Exception) as caught:
        await SSHConnection.connect(profile, FakePrompter())
    assert "ended without saying why" in describe_error(caught.value)


async def test_missing_proxy_program_is_a_clear_error(ssh_server, tmp_path):
    missing = tmp_path / "no-such-program"
    profile = ssh_server.profile(proxy_type="command", proxy_local=True, proxy_command=f'"{missing}" %host %port')
    with pytest.raises(ConnectError, match=r"proxy command .*no-such-program: .*[Nn]o such file"):
        await SSHConnection.connect(profile, FakePrompter())


def telnet_proxy():
    async def handler(p, r, w):
        p.seen.append((await r.readuntil(b"\n")).decode())
        await _relay(r, w, p.target)
    return handler


async def test_telnet_proxy_sends_the_connect_line_and_relays(ssh_server, proxy_for):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(telnet_proxy(), ssh_server.port)
    prompter = FakePrompter()
    profile = ssh_server.profile(proxy_type="telnet", proxy_host="127.0.0.1", proxy_port=proxy.port, proxy_local=True)
    session = await open_session(profile, prompter, 80, 24)
    try:
        await shell_works(session)
    finally:
        await session.close()
    assert proxy.seen == [f"connect 127.0.0.1 {ssh_server.port}\n"]
    assert len(prompter.asked) == 1 and "proxy password" not in prompter.asked[0]  # no %pass: nothing asked
    assert any("Telnet proxy 127.0.0.1:" in n for n in prompter.notices)


async def test_telnet_proxy_substitutes_user_and_asks_for_the_password_when_used(ssh_server, proxy_for):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(telnet_proxy(), ssh_server.port)
    prompter = FakePrompter(answers=["tpw", PASSWORD])
    profile = ssh_server.profile(proxy_type="telnet", proxy_host="127.0.0.1", proxy_port=proxy.port, proxy_local=True,
                                 proxy_username="tom", proxy_telnet_command="open %host:%port as %user/%pass 100%%\\n")
    conn = await SSHConnection.connect(profile, prompter)
    conn.close()
    assert proxy.seen == [f"open 127.0.0.1:{ssh_server.port} as tom/tpw 100%\n"]
    assert prompter.asked[0].startswith("tom@127.0.0.1:") and prompter.asked[0].endswith(" proxy password: ")


@pytest.mark.parametrize(("kind", "dns", "name_sent"), [
    ("socks4", "auto", False), ("socks5", "auto", True), ("http", "auto", True),
    ("socks4", "yes", True), ("socks5", "no", False)])
async def test_proxy_dns_decides_who_resolves_the_name(ssh_server, proxy_for, monkeypatch, kind, dns, name_sent):
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **kw: real("127.0.0.1" if host == "ssh.example.test" else host, *a, **kw))
    remember_host_key("ssh.example.test", ssh_server.port, ssh_server.key)
    handler = {"socks4": socks4, "socks5": socks5, "http": http}[kind]()
    proxy = await proxy_for(handler, ssh_server.port)
    profile = named(ssh_server, "ssh.example.test", proxy_type=kind, proxy_host="127.0.0.1", proxy_port=proxy.port,
                    proxy_dns=dns)
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()
    [seen] = proxy.seen
    if kind == "http":
        assert seen.startswith(f"CONNECT ssh.example.test:{ssh_server.port} ")
    elif kind == "socks4":
        assert seen[1] == ("ssh.example.test" if name_sent else "127.0.0.1")
    else:
        assert seen == (("connect", 3, "ssh.example.test") if name_sent else ("connect", 1, "7f000001"))


async def test_proxy_password_is_asked_once_across_the_host_key_retry(ssh_server, proxy_for):
    proxy = await proxy_for(socks5({"bob": "pw"}), ssh_server.port)
    prompter = FakePrompter(answers=["pw", PASSWORD])  # the host key is unknown: two attempts
    profile = ssh_server.profile(proxy_type="socks5", proxy_host="127.0.0.1", proxy_port=proxy.port,
                                 proxy_local=True, proxy_username="bob")
    conn = await SSHConnection.connect(profile, prompter)
    conn.close()
    assert len(prompter.host_keys) == 1
    assert [p for p in prompter.asked if p.endswith("proxy password: ")] == [prompter.asked[0]]
    assert len(prompter.asked) == 2 and prompter.asked[1].endswith("password: ")
    assert proxy.seen.count(("auth", "bob", "pw")) == 2  # both attempts logged in to the proxy


def test_proxy_exclusion_list_splits_on_runs_of_spaces():
    def uses(host):
        return uses_proxy(SSHSettings(proxy_type="socks5", proxy_host="p", proxy_exclude="a.lan  b.lan"), host)

    assert not uses("a.lan") and not uses("b.lan")
    assert uses("c.lan") and uses("a.lan b.lan")


async def test_proxy_notice_names_the_proxy_and_port(ssh_server, proxy_for):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(socks5(), ssh_server.port)
    prompter = FakePrompter()
    profile = ssh_server.profile(proxy_type="socks5", proxy_host="127.0.0.1", proxy_port=proxy.port, proxy_local=True)
    conn = await SSHConnection.connect(profile, prompter)
    conn.close()
    assert f"connecting through the SOCKS 5 proxy 127.0.0.1:{proxy.port}" in prompter.notices


# ---------------------------------------------------------------- trusted keys, normalised


def _blob(key):
    return key.export_public_key("openssh").decode().split()[1]


async def test_trusted_key_lines_are_normalised_and_still_pin(ssh_server):
    key = ssh_server.key
    sha, md5 = key.get_fingerprint("sha256"), key.get_fingerprint("md5")
    pasted = f"ssh-ed25519 255 {sha}"  # as PuTTY and ssh-keygen -l show it
    bare_md5 = md5.removeprefix("MD5:").upper()
    pubkey = f"ssh-ed25519 {_blob(key)}"
    for line in (pasted, bare_md5, pubkey):
        profile = ssh_server.profile(trusted_keys=line)
        profile.validate()
        assert profile.ssh.trusted_keys in (sha, md5)
        prompter = FakePrompter()
        conn = await SSHConnection.connect(profile, prompter)  # the normalised form is what pins
        conn.close()
        assert prompter.host_keys == []
    assert ssh_server.profile(trusted_keys=bare_md5).ssh.trusted_keys == bare_md5  # untouched before validate


def test_trusted_keys_normalised_form_and_duplicates_removed():
    key = asyncssh.generate_private_key("ssh-ed25519")
    sha, md5 = key.get_fingerprint("sha256"), key.get_fingerprint("md5")
    text = f"{sha}\nssh-ed25519 256 {sha}\n{md5.removeprefix('MD5:').upper()}\nssh-ed25519 {_blob(key)}\n{md5}"
    profile = Profile("p", "ssh", ssh=SSHSettings(host="h", username="u", trusted_keys=text))
    profile.validate()
    assert profile.ssh.trusted_keys.split("\n") == [sha, md5]
    profile.validate()  # idempotent
    assert profile.ssh.trusted_keys.split("\n") == [sha, md5]


# ---------------------------------------------------------------- captured asyncssh arguments


class Stop(Exception):
    """Raised by the connect stub once the arguments are captured."""


@pytest.fixture
def connect_args(monkeypatch):
    """The (args, kwargs) the next asyncssh.connect gets; the connection is not made."""
    captured = {}

    async def stub(*args, **kwargs):
        captured["args"], captured["kwargs"] = args, kwargs
        raise Stop

    monkeypatch.setattr(asyncssh, "connect", stub)

    async def run(profile):
        with pytest.raises(Stop):
            await SSHConnection.connect(profile, FakePrompter())
        return captured["args"], captured["kwargs"]

    return run


def _write_ssh_config(tmp_path, body):
    ssh_dir = tmp_path / "home" / ".ssh"
    ssh_dir.mkdir(exist_ok=True)
    (ssh_dir / "config").write_text(body)


async def test_ssh_config_user_compression_and_keepalive_apply_at_profile_defaults(ssh_server, tmp_path, connect_args):
    profile = ssh_server.profile(use_ssh_config=True)
    profile.ssh.username = ""
    _args, kw = await connect_args(profile)
    assert kw["username"] == () and kw["config"] == ()
    assert not {"compression_algs", "keepalive_interval", "agent_forwarding", "tcp_keepalive"} & kw.keys()


async def test_ssh_config_user_is_used_for_the_login_when_the_profile_has_none(ssh_server, tmp_path):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    _write_ssh_config(tmp_path, f"Host alias\n  HostName 127.0.0.1\n  Port {ssh_server.port}\n  User {USER}\n"
                                "  Compression yes\n  ServerAliveInterval 7\n")
    profile = named(ssh_server, "alias", use_ssh_config=True)
    profile.ssh.username, profile.ssh.port = "", 22
    prompter = FakePrompter()
    conn = await SSHConnection.connect(profile, prompter)  # the login name comes from the config
    conn.close()
    assert not any("login as" in p for p in prompter.asked)


async def test_profile_values_beat_the_ssh_config(ssh_server, connect_args):
    profile = ssh_server.profile(use_ssh_config=True, keepalive=3, compression=True, tcp_keepalive=True)
    _args, kw = await connect_args(profile)
    assert kw["username"] == USER
    assert kw["keepalive_interval"] == 3 and kw["tcp_keepalive"] is True
    assert "zlib" in kw["compression_algs"]
    profile = ssh_server.profile(use_ssh_config=False)
    _args, kw = await connect_args(profile)  # without the config every value is passed
    assert kw["config"] is None and kw["keepalive_interval"] == 0 and kw["compression_algs"] == ["none"]


async def test_gssapi_keepalive_and_certificate_values_reach_asyncssh(ssh_server, tmp_path, connect_args):
    cert = tmp_path / "id-cert.pub"
    profile = ssh_server.profile(gssapi_auth=False, gssapi_kex=False, gssapi_delegate=True, tcp_keepalive=True,
                                 certificate=str(cert))
    _args, kw = await connect_args(profile)
    assert (kw["gss_auth"], kw["gss_kex"], kw["gss_delegate_creds"]) == (False, False, True)
    assert kw["tcp_keepalive"] is True
    assert kw["client_certs"] == [str(cert)]
    _args, kw = await connect_args(ssh_server.profile())
    assert (kw["gss_auth"], kw["gss_kex"], kw["gss_delegate_creds"]) == (True, True, False)
    assert "client_certs" not in kw


async def test_signature_algs_reach_asyncssh(ssh_server, connect_args):
    _args, kw = await connect_args(ssh_server.profile(signature_algs="ssh-rsa, rsa-sha2-256"))
    assert kw["signature_algs"] == ["ssh-rsa", "rsa-sha2-256"]
    _args, kw = await connect_args(ssh_server.profile())
    assert "signature_algs" not in kw


def test_unknown_signature_alg_is_a_profile_error():
    profile = Profile("p", "ssh", ssh=SSHSettings(host="h", username="u", signature_algs="rsa-sha2-256, bogus-sig"))
    with pytest.raises(ProfileError, match="unknown signature type bogus-sig"):
        profile.validate()


def test_algorithm_errors_are_pluralised():
    for field, plural, single in (("ciphers", "ciphers", "cipher"), ("kex_algs", "key exchanges", "key exchange"),
                                  ("mac_algs", "MACs", "MAC")):
        profile = Profile("p", "ssh", ssh=SSHSettings(host="h", username="u", **{field: "a, b"}))
        with pytest.raises(ProfileError, match=f"unknown {plural} a, b;"):
            profile.validate()
        profile.ssh = SSHSettings(host="h", username="u", **{field: "a"})
        with pytest.raises(ProfileError, match=f"unknown {single} a;"):
            profile.validate()


@pytest.mark.parametrize("value", ["²", "99999999999", "4294967296", "-1", "١٢"])
def test_tty_mode_values_must_be_plain_32_bit_numbers(value):
    profile = Profile("p", "ssh", ssh=SSHSettings(host="h", username="u", tty_modes=f"ERASE={value}"))
    with pytest.raises(ProfileError, match="terminal mode"):
        profile.validate()
    profile.ssh.tty_modes = "ERASE=4294967295"
    profile.validate()


# ---------------------------------------------------------------- X11 arguments


@pytest.fixture
def process_args(ssh_server, monkeypatch):
    """The keyword arguments open_shell gives create_process; no process is created."""

    async def run(profile, platform=None):
        remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
        conn = await SSHConnection.connect(profile, FakePrompter())
        captured = {}

        async def stub(*args, **kwargs):
            captured.update(kwargs)
            raise Stop

        monkeypatch.setattr(conn.conn, "create_process", stub)
        if platform:
            monkeypatch.setattr("termdeck.ssh.sys", types.SimpleNamespace(platform=platform))
        try:
            with pytest.raises(Stop):
                await conn.open_shell(80, 24)
        finally:
            conn.close()
        return captured

    return run


async def test_x11_display_and_auth_file_reach_create_process(ssh_server, tmp_path, monkeypatch, process_args):
    monkeypatch.setenv("DISPLAY", ":7")
    auth = tmp_path / "xauth"
    kw = await process_args(ssh_server.profile(x11_forwarding=True, x11_display="box:2", x11_auth_file=str(auth)))
    assert kw["x11_display"] == "box:2" and kw["x11_auth_path"] == str(auth) and kw["x11_forwarding"] is True
    kw = await process_args(ssh_server.profile())  # no profile value: $DISPLAY, asyncssh's own authority file
    assert kw["x11_display"] == ":7" and "x11_auth_path" not in kw


@pytest.mark.parametrize(("display", "expected"), [(":0", "localhost:0"), (":10.1", "localhost:10.1"),
                                                  ("localhost:0", "localhost:0"), ("box:0", "box:0")])
async def test_x11_display_on_windows_uses_tcp(ssh_server, monkeypatch, process_args, display, expected):
    monkeypatch.delenv("DISPLAY", raising=False)
    kw = await process_args(ssh_server.profile(x11_display=display), platform="win32")
    assert kw["x11_display"] == expected


async def test_x11_forwarding_on_windows_without_a_display_is_a_connect_error(ssh_server, monkeypatch):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    monkeypatch.delenv("DISPLAY", raising=False)
    conn = await SSHConnection.connect(ssh_server.profile(x11_forwarding=True), FakePrompter())
    monkeypatch.setattr("termdeck.ssh.sys", types.SimpleNamespace(platform="win32"))
    try:
        with pytest.raises(ConnectError, match="local X server"):
            await conn.open_shell(80, 24)
    finally:
        conn.close()


# ---------------------------------------------------------------- shared connections


async def test_shared_tabs_with_different_commands_run_their_own_and_keep_their_titles(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    prompter = FakePrompter(answers=[PASSWORD, PASSWORD])
    first = ssh_server.profile("first", share_connection=True, remote_command="one")
    second = ssh_server.profile("second", share_connection=True, remote_command="two")
    a = await open_session(first, prompter, 80, 24)
    b = await open_session(second, prompter, 80, 24)
    try:
        assert len(prompter.asked) == 1  # one login
        assert b"ran one" in await read_until(a, b"ran one")
        assert b"ran two" in await read_until(b, b"ran two")
        assert (a.title, b.title) == ("first", "second")
    finally:
        await a.close()
        await b.close()


def _forward():
    from termdeck.profiles import Forward

    return Forward("dynamic", 0, "127.0.0.1")


@pytest.mark.parametrize("differs", [{"agent_forwarding": True}, {"forwards": "one"}, {"ciphers": "aes256-ctr"}])
async def test_shared_tabs_differing_in_connection_settings_get_their_own_connection(opt_server, differs):
    server = await opt_server()
    server.trust()
    if differs.get("forwards"):
        differs = {"forwards": [_forward()]}
    prompter = FakePrompter(answers=[PASSWORD, PASSWORD])
    a = await open_session(server.profile(share_connection=True), prompter, 80, 24)
    b = await open_session(server.profile(share_connection=True, **differs), prompter, 80, 24)
    try:
        assert server.logins == 2 and server.open == 2
    finally:
        await a.close()
        await b.close()


async def test_empty_username_is_never_shared(opt_server):
    server = await opt_server()
    server.trust()
    prompter = FakePrompter(answers=[USER, PASSWORD, USER, PASSWORD])
    profile = server.profile(share_connection=True)
    profile.ssh.username = ""
    a = await open_session(profile, prompter, 80, 24)
    b = await open_session(profile, prompter, 80, 24)
    try:
        assert server.logins == 2 and server.open == 2
        assert [p for p in prompter.asked if p.startswith("login as")] == ["login as (127.0.0.1): "] * 2
    finally:
        await a.close()
        await b.close()


def test_jump_profile_clears_what_belongs_to_the_target():
    from termdeck.ssh import jump_profile

    key = asyncssh.generate_private_key("ssh-ed25519").get_fingerprint("sha256")
    profile = Profile("web", "ssh", ssh=SSHSettings(
        host="target", username="u", jump_host="bob@jump:2222", logical_host="alias", trusted_keys=key,
        subsystem="sftp", no_shell=True, remote_command="ls", env="A=1", agent_forwarding=True,
        x11_forwarding=True, forwards=[_forward()], ciphers="aes256-ctr"))
    jump = jump_profile(profile).ssh
    assert (jump.host, jump.port, jump.username) == ("jump", 2222, "bob")
    assert jump.logical_host == "" and jump.trusted_keys == "" and jump.subsystem == "" and jump.no_shell is False
    assert jump.remote_command == "" and jump.env == "" and jump.forwards == [] and jump.jump_host == ""
    assert not jump.agent_forwarding and not jump.x11_forwarding
    assert jump.ciphers == "aes256-ctr"  # connection options are inherited
    assert profile.ssh.logical_host == "alias" and profile.ssh.trusted_keys == key  # the original is untouched


# ---------------------------------------------------------------- connect flags


def _connect_args(*argv):
    from termdeck import cli

    return cli.build_parser().parse_args(["connect", *argv])


def test_connect_flags_map_onto_the_profile():
    from termdeck import cli

    args = _connect_args("--proxycmd", "nc %host %port", "--loghost", "alias", "--no-trivial-auth", "--share",
                         "--hostkey", "SHA256:" + "A" * 43, "bob@host.example")
    profile = cli._connect_profile(args, cli.ProfileStore())
    s = profile.ssh
    assert (s.proxy_type, s.proxy_command) == ("command", "nc %host %port")
    assert s.logical_host == "alias" and s.no_trivial_auth is True and s.share_connection is True
    assert s.trusted_keys == "SHA256:" + "A" * 43


def test_share_flags_override_the_saved_profile_and_default_to_leaving_it():
    from termdeck import cli

    store = cli.ProfileStore()
    store.save(Profile("web", "ssh", ssh=SSHSettings(host="h", username="u", share_connection=True)))
    assert cli._connect_profile(_connect_args("web"), store).ssh.share_connection is True
    assert cli._connect_profile(_connect_args("--no-share", "web"), store).ssh.share_connection is False
    with pytest.raises(SystemExit):  # the two flags exclude each other
        _connect_args("--share", "--no-share", "web")
    store.save(Profile("plain", "ssh", ssh=SSHSettings(host="h", username="u")))
    profile = cli._connect_profile(_connect_args("plain"), store)
    assert profile.ssh.share_connection is False and profile.ssh.no_trivial_auth is False
    assert profile.ssh.proxy_type == "none" and profile.ssh.logical_host == ""


def test_no_trivial_auth_flag_does_not_turn_a_saved_setting_off():
    from termdeck import cli

    store = cli.ProfileStore()
    store.save(Profile("web", "ssh", ssh=SSHSettings(host="h", username="u", no_trivial_auth=True)))
    assert cli._connect_profile(_connect_args("web"), store).ssh.no_trivial_auth is True


@pytest.mark.parametrize("flag", [("--proxycmd", "nc %host %port"), ("--loghost", "alias"), ("--hostkey", "SHA256:x")])
def test_ssh_only_connect_flags_are_refused_for_serial_targets(flag):
    from termdeck import cli

    args = _connect_args(*flag, "loop://")
    with pytest.raises(ValueError, match=f"{flag[0]}: only for SSH"):
        cli._connect_profile(args, cli.ProfileStore())


# ---------------------------------------------------------------- batch 6 fixes: IPv4 for SOCKS 4, HTTP dns, jump command


async def test_socks4_resolving_locally_prefers_ipv4(ssh_server, proxy_for, monkeypatch):
    """A name that resolves to IPv6 first still reaches SOCKS 4, which cannot carry IPv6."""
    real = socket.getaddrinfo

    def v6_first(host, *a, **kw):
        if host == "dual.example.test":
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0)),
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]
        return real(host, *a, **kw)

    monkeypatch.setattr(socket, "getaddrinfo", v6_first)
    remember_host_key("dual.example.test", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(socks4(), ssh_server.port)
    profile = named(ssh_server, "dual.example.test", proxy_type="socks4", proxy_host="127.0.0.1", proxy_port=proxy.port)
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()
    assert proxy.seen[0][1] == "127.0.0.1"


async def test_socks4_with_only_an_ipv6_address_says_so(ssh_server, proxy_for, monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **kw: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))] if host == "v6.example.test"
        else real(host, *a, **kw))
    proxy = await proxy_for(socks4(), ssh_server.port)
    profile = named(ssh_server, "v6.example.test", proxy_type="socks4", proxy_host="127.0.0.1", proxy_port=proxy.port)
    with pytest.raises(ConnectError, match="IPv6"):
        await SSHConnection.connect(profile, FakePrompter())


async def test_http_proxy_dns_no_sends_the_resolved_address(ssh_server, proxy_for, monkeypatch):
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **kw: real("127.0.0.1" if host == "ssh.example.test" else host, *a, **kw))
    remember_host_key("ssh.example.test", ssh_server.port, ssh_server.key)
    proxy = await proxy_for(http(), ssh_server.port)
    profile = named(ssh_server, "ssh.example.test", proxy_type="http", proxy_host="127.0.0.1", proxy_port=proxy.port,
                    proxy_dns="no")
    conn = await SSHConnection.connect(profile, FakePrompter())
    conn.close()
    assert proxy.seen[0].startswith(f"CONNECT 127.0.0.1:{ssh_server.port} HTTP/1.1\r\nHost: 127.0.0.1:{ssh_server.port}")


async def test_jump_command_reaches_the_target_through_a_command_on_the_jump_host(opt_server, ssh_server):
    jump = await opt_server(encoding=None)
    jump.trust()
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    prompter = FakePrompter(answers=[PASSWORD, PASSWORD])  # the jump host's login, then the target's
    profile = ssh_server.profile(jump_host=f"{USER}@127.0.0.1:{jump.port}", jump_command="relay %host %port")
    session = await open_session(profile, prompter, 80, 24)
    try:
        await read_until(session, b"hello")  # the target's own shell, not the jump host's
        assert jump.commands == [f"relay 127.0.0.1 {ssh_server.port}"]
        assert jump.logins == 1 and len(prompter.asked) == 2
    finally:
        await session.close()


async def test_jump_command_subsystem_starts_a_subsystem_on_the_jump_host(opt_server, ssh_server):
    jump = await opt_server(encoding=None)
    jump.trust()
    jump.target = ("127.0.0.1", ssh_server.port)
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    prompter = FakePrompter(answers=[PASSWORD, PASSWORD])
    profile = ssh_server.profile(jump_host=f"{USER}@127.0.0.1:{jump.port}", jump_command="subsystem:relay")
    session = await open_session(profile, prompter, 80, 24)
    try:
        await read_until(session, b"hello")
        assert jump.subsystems == ["relay"] and jump.commands == []
    finally:
        await session.close()


async def test_failing_jump_command_shows_its_error_output(opt_server, ssh_server):
    from termdeck.session import describe_error

    jump = await opt_server(encoding=None)
    jump.trust()
    prompter = FakePrompter(answers=[PASSWORD, PASSWORD])
    profile = ssh_server.profile(jump_host=f"{USER}@127.0.0.1:{jump.port}", jump_command="nc-broken %host %port")
    with pytest.raises(Exception) as caught:
        await SSHConnection.connect(profile, prompter)
    assert "nc: refused" in describe_error(caught.value)
    await asyncio.wait_for(jump.closed.wait(), 5)  # the jump connection is not left open
    assert jump.open == 0


def test_jump_command_is_shown_only_while_a_jump_host_is_set():
    from termdeck.profiles import describe_options

    [option] = [o for o in describe_options()["ssh"] if o["key"] == "jump_command"]
    assert option["when"] == {"key": "jump_host", "values": None}
