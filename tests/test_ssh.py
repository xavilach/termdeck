import asyncio

import asyncssh
import pytest

from termdeck.profiles import Forward
from termdeck.session import open_session
from termdeck.ssh import SSHConnection, known_hosts_path, remember_host_key

from .conftest import FakePrompter, free_port, read_until


async def test_shell_asks_host_key_then_password_and_echoes(ssh_server):
    prompter = FakePrompter()
    session = await open_session(ssh_server.profile(), prompter, cols=100, rows=30)
    try:
        assert b"hello xterm-256color 100x30" in await read_until(session, b"\n")
        await session.write(b"ping")
        assert b"ping" in await read_until(session, b"ping")
        session.resize(120, 40)
        assert b"resized 120x40" in await read_until(session, b"resized 120x40")
    finally:
        await session.close()
    assert len(prompter.asked) == 1  # password or keyboard-interactive, server's choice
    [(host, port, fingerprint, changed)] = prompter.host_keys
    assert (host, port, changed) == ("127.0.0.1", ssh_server.port, False)
    assert fingerprint == ssh_server.key.get_fingerprint()
    assert f"[127.0.0.1]:{ssh_server.port} ssh-ed25519" in known_hosts_path().read_text()


async def test_known_host_is_not_asked_again(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    prompter = FakePrompter()
    conn = await SSHConnection.connect(ssh_server.profile(), prompter)
    conn.close()
    assert prompter.host_keys == []


async def test_rejected_host_key_aborts_without_saving(ssh_server):
    prompter = FakePrompter(trust=False)
    with pytest.raises(asyncssh.HostKeyNotVerifiable):
        await SSHConnection.connect(ssh_server.profile(), prompter)
    assert prompter.asked == []
    assert "ssh-ed25519" not in known_hosts_path().read_text()


async def test_changed_host_key_is_flagged_and_replaced(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, asyncssh.generate_private_key("ssh-ed25519"))
    prompter = FakePrompter()
    conn = await SSHConnection.connect(ssh_server.profile(), prompter)
    conn.close()
    assert prompter.host_keys[0][3] is True
    assert known_hosts_path().read_text().count("ssh-ed25519") == 1


async def test_wrong_passwords_stop_after_three_tries(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    prompter = FakePrompter(answers=["a", "b", "c", "d"])
    with pytest.raises(asyncssh.PermissionDenied):
        await SSHConnection.connect(ssh_server.profile(), prompter)
    assert len(prompter.asked) == 3


async def test_empty_username_is_asked(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    prompter = FakePrompter(answers=["alice", "secret"])
    profile = ssh_server.profile()
    profile.ssh.username = ""
    conn = await SSHConnection.connect(profile, prompter)
    conn.close()
    assert prompter.asked[0] == "login as (127.0.0.1): "


async def test_key_file_with_passphrase(ssh_server, tmp_path):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    key = asyncssh.generate_private_key("ssh-ed25519")
    key_path = tmp_path / "id_test"
    key.write_private_key(key_path, passphrase="pw")
    prompter = FakePrompter(answers=["pw", "secret"])
    conn = await SSHConnection.connect(ssh_server.profile(key_file=str(key_path)), prompter)
    conn.close()
    assert prompter.asked[0] == f"Passphrase for {key_path}: "


async def test_local_forward(ssh_server):
    async def echo(reader, writer):
        writer.write(await reader.read(100))
        await writer.drain()
        writer.close()

    target = await asyncio.start_server(echo, "127.0.0.1", 0)
    target_port = target.sockets[0].getsockname()[1]
    listen_port = free_port()
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    profile = ssh_server.profile()
    profile.ssh.forwards = [Forward("local", listen_port, "127.0.0.1", "127.0.0.1", target_port)]
    conn = await SSHConnection.connect(profile, FakePrompter())
    try:
        assert await conn.start_forwards() == []
        reader, writer = await asyncio.open_connection("127.0.0.1", listen_port)
        writer.write(b"through the tunnel")
        assert await asyncio.wait_for(reader.read(100), 5) == b"through the tunnel"
        writer.close()
    finally:
        conn.close()
        target.close()


async def test_failed_forward_is_reported_in_terminal(ssh_server):
    busy = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    busy_port = busy.sockets[0].getsockname()[1]
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    profile = ssh_server.profile()
    profile.ssh.forwards = [Forward("local", busy_port, "127.0.0.1", "127.0.0.1", 1)]
    session = await open_session(profile, FakePrompter())
    try:
        out = await read_until(session, b"failed")
        assert f"forward -L 127.0.0.1:{busy_port}:127.0.0.1:1 failed".encode() in out
    finally:
        await session.close()
        busy.close()


async def test_sftp_put_and_get(ssh_server, tmp_path):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    local = tmp_path / "local.txt"
    local.write_text("payload")
    conn = await SSHConnection.connect(ssh_server.profile(), FakePrompter())
    try:
        async with conn.sftp() as sftp:
            await sftp.put(str(local), "/remote.txt")
            assert "remote.txt" in await sftp.listdir("/")
            await sftp.get("/remote.txt", str(tmp_path / "back.txt"))
    finally:
        conn.close()
    assert (ssh_server.root / "remote.txt").read_text() == "payload"
    assert (tmp_path / "back.txt").read_text() == "payload"


async def test_remote_command_and_env(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    profile = ssh_server.profile(remote_command="uptime", env="FOO=bar\n", keepalive=30, compression=True)
    session = await open_session(profile, FakePrompter())
    try:
        out = await read_until(session, b"\n")
        assert b"ran uptime FOO=bar" in out
        assert await read_until(session, b"never") == b""  # command exit ends the session
    finally:
        await session.close()


async def test_jump_host(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    profile = ssh_server.profile(jump_host=f"alice@127.0.0.1:{ssh_server.port}")
    prompter = FakePrompter(answers=["secret", "secret"])
    session = await open_session(profile, prompter)
    try:
        assert b"hello" in await read_until(session, b"\n")
    finally:
        await session.close()
    assert len(prompter.asked) == 2  # one login per hop


def test_jump_profile_parsing(ssh_server):
    from termdeck.ssh import jump_profile

    for spec, expected in [
        ("bastion", ("bastion", 22, "alice")),
        ("bob@bastion:2222", ("bastion", 2222, "bob")),
        ("[::1]:2200", ("::1", 2200, "alice")),
    ]:
        jump = jump_profile(ssh_server.profile(jump_host=spec)).ssh
        assert (jump.host, jump.port, jump.username) == expected
        assert jump.jump_host == ""
