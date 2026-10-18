import asyncio

import asyncssh
import pytest

from termdeck.errors import ConnectError
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
    lines = known_hosts_path().read_text().splitlines()
    # One trusted key; the replaced one is revoked so no other copy of it is trusted.
    assert len([line for line in lines if not line.startswith("@revoked")]) == 1
    assert len([line for line in lines if line.startswith("@revoked")]) == 1


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


# ---- how sessions end ----

async def test_remote_command_exit_status_is_the_end_reason(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    session = await open_session(ssh_server.profile(remote_command="exit3"), FakePrompter())
    try:
        assert await read_until(session, b"never") == b""
        assert "status 3" in session.end_reason
    finally:
        await session.close()


async def test_exit_status_zero_has_no_end_reason(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    session = await open_session(ssh_server.profile(remote_command="uptime"), FakePrompter())
    try:
        assert await read_until(session, b"never") != b"never"
        assert session.end_reason is None
    finally:
        await session.close()


async def test_lost_connection_sets_an_end_reason(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    session = await open_session(ssh_server.profile(remote_command="abort"), FakePrompter())
    try:
        assert await read_until(session, b"never") == b""
        assert "Connection lost" in session.end_reason
    finally:
        await session.close()


async def test_server_side_disconnect_sets_an_end_reason(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    session = await open_session(ssh_server.profile(remote_command="disconnect"), FakePrompter())
    try:
        assert await read_until(session, b"never") == b""
        assert "bye bye" in (session.end_reason or "")
    finally:
        await session.close()


async def test_empty_username_answered_with_nothing_is_a_connect_error(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    profile = ssh_server.profile()
    profile.ssh.username = ""
    prompter = FakePrompter(answers=[None])
    with pytest.raises(ConnectError, match="No username given"):
        await SSHConnection.connect(profile, prompter)
    assert prompter.host_keys == []


# ---- messages from the server ----

async def test_auth_banner_reaches_the_prompter(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    ssh_server.banner = "Authorized users only\r\nBe nice‮\n"
    prompter = FakePrompter()
    conn = await SSHConnection.connect(ssh_server.profile(), prompter)
    conn.close()
    assert prompter.notices == ["Authorized users only\nBe nice"]  # CRLF normalised, bidi override dropped


async def test_keyboard_interactive_prompts_are_labelled_and_instructions_shown(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    ssh_server.kbdint = True
    prompter = FakePrompter(answers=["secret"])
    conn = await SSHConnection.connect(ssh_server.profile(), prompter)
    conn.close()
    assert prompter.asked == ["(alice@127.0.0.1) Code: "]
    assert prompter.notices == ["MFA\nEnter the code from your phone"]


# ---- host keys ----

async def test_unknown_host_key_details(ssh_server):
    prompter = FakePrompter()
    (await SSHConnection.connect(ssh_server.profile(), prompter)).close()
    [key] = prompter.keys
    assert key.key_type == "ssh-ed25519"
    assert key.fingerprint.startswith("SHA256:") and key.fingerprint == ssh_server.key.get_fingerprint()
    assert key.known_hosts == str(known_hosts_path())
    assert key.changed is False and key.old_fingerprints == []


async def test_changed_host_key_shows_the_old_fingerprint_and_revokes_it(ssh_server):
    old = asyncssh.generate_private_key("ssh-ed25519")
    remember_host_key("127.0.0.1", ssh_server.port, old)
    prompter = FakePrompter()
    (await SSHConnection.connect(ssh_server.profile(), prompter)).close()
    [key] = prompter.keys
    assert key.changed is True
    assert set(key.old_fingerprints) == {f"ssh-ed25519 {old.get_fingerprint()}"}
    assert key.fingerprint == ssh_server.key.get_fingerprint()
    old_blob = old.export_public_key("openssh").decode().split()[1]
    lines = known_hosts_path().read_text().splitlines()
    assert any(line.startswith("@revoked") and line.endswith(old_blob) for line in lines)

    # The server comes back with the old key: refused, without asking again.
    ssh_server.listener.close()
    await ssh_server.listener.wait_closed()
    ssh_server.key = old
    await ssh_server.start(ssh_server.port)
    again = FakePrompter()
    with pytest.raises(asyncssh.Error):
        await SSHConnection.connect(ssh_server.profile(), again)
    assert again.host_keys == []


async def test_declining_a_changed_host_key_keeps_the_old_one(ssh_server):
    old = asyncssh.generate_private_key("ssh-ed25519")
    remember_host_key("127.0.0.1", ssh_server.port, old)
    before = known_hosts_path().read_text()
    with pytest.raises(asyncssh.HostKeyNotVerifiable):
        await SSHConnection.connect(ssh_server.profile(), FakePrompter(trust=False))
    assert known_hosts_path().read_text() == before


async def test_old_fingerprints_have_no_duplicates(ssh_server):
    old = asyncssh.generate_private_key("ssh-ed25519")
    remember_host_key("127.0.0.1", ssh_server.port, old)
    prompter = FakePrompter()
    (await SSHConnection.connect(ssh_server.profile(), prompter)).close()
    assert len(prompter.keys[0].old_fingerprints) == 1


# ---- password change ----

async def test_expired_password_asks_for_a_new_one_twice(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    ssh_server.expired = True
    prompter = FakePrompter(answers=["secret", "fresh", "fresh"])
    conn = await SSHConnection.connect(ssh_server.profile(), prompter)
    conn.close()
    assert prompter.asked == ["alice@127.0.0.1 password: ", "New password for alice@127.0.0.1: ",
                              "New password again for alice@127.0.0.1: "]
    assert ssh_server.changes == [("secret", "fresh")]
    assert any("expired" in n for n in prompter.notices)


async def test_mismatched_new_passwords_are_asked_again(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    ssh_server.expired = True
    prompter = FakePrompter(answers=["secret", "one", "two", "three", "three"])
    conn = await SSHConnection.connect(ssh_server.profile(), prompter)
    conn.close()
    assert [a for a in prompter.asked if a.startswith("New password for")] == ["New password for alice@127.0.0.1: "] * 2
    assert ssh_server.changes == [("secret", "three")]
    assert "The passwords do not match." in prompter.notices


async def test_cancelling_the_password_change_fails_the_login(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    ssh_server.expired = True
    with pytest.raises(asyncssh.Error):
        await SSHConnection.connect(ssh_server.profile(), FakePrompter(answers=["secret", None]))
    assert ssh_server.changes == []


# ---- revoked keys, forget-host, secrets ----

async def _restart_with(ssh_server, key):
    ssh_server.listener.close()
    await ssh_server.listener.wait_closed()
    ssh_server.key = key
    await ssh_server.start(ssh_server.port)


def _revoked(blob_of):
    blob = blob_of.export_public_key("openssh").decode().split()[1]
    return [line for line in known_hosts_path().read_text().splitlines()
            if line.startswith("@revoked") and line.endswith(blob)]


async def test_revoked_key_error_points_to_forget_host_which_makes_it_unknown_again(ssh_server, capsys):
    from termdeck import cli
    from termdeck.session import describe_error

    old = asyncssh.generate_private_key("ssh-ed25519")
    remember_host_key("127.0.0.1", ssh_server.port, old)
    (await SSHConnection.connect(ssh_server.profile(), FakePrompter())).close()  # accepts the changed key
    assert _revoked(old)

    await _restart_with(ssh_server, old)
    refused = FakePrompter()
    with pytest.raises(asyncssh.HostKeyNotVerifiable) as exc:
        await SSHConnection.connect(ssh_server.profile(), refused)
    assert "forget-host" in describe_error(exc.value) and refused.host_keys == []

    assert cli.main(["forget-host", f"127.0.0.1:{ssh_server.port}"]) == 0
    assert "forgot 2 key line(s)" in capsys.readouterr().err
    assert known_hosts_path().read_text().strip() == ""

    again = FakePrompter()
    conn = await SSHConnection.connect(ssh_server.profile(), again)
    conn.close()
    assert [k[3] for k in again.host_keys] == [False]  # asked again, as an unknown host
    assert _revoked(old) == []


def test_trusting_a_revoked_key_again_removes_its_revocation(ssh_server):
    a = asyncssh.generate_private_key("ssh-ed25519")
    b = asyncssh.generate_private_key("ssh-ed25519")
    remember_host_key("127.0.0.1", 2200, a)
    remember_host_key("127.0.0.1", 2200, b, [a])
    assert len(_revoked(a)) == 1
    remember_host_key("127.0.0.1", 2200, a, [b])
    assert _revoked(a) == [] and len(_revoked(b)) == 1
    trusted = [line for line in known_hosts_path().read_text().splitlines() if not line.startswith("@")]
    assert len(trusted) == 1 and trusted[0].endswith(a.export_public_key("openssh").decode().split()[1])


def test_a_changed_key_does_not_revoke_a_key_of_another_type(tmp_path):
    ecdsa = asyncssh.generate_private_key("ecdsa-sha2-nistp256")
    old = asyncssh.generate_private_key("ssh-ed25519")
    new = asyncssh.generate_private_key("ssh-ed25519")
    openssh = tmp_path / "home" / ".ssh"
    openssh.mkdir()
    (openssh / "known_hosts").write_text(f"[127.0.0.1]:2200 {ecdsa.export_public_key('openssh').decode()}")
    remember_host_key("127.0.0.1", 2200, old)
    remember_host_key("127.0.0.1", 2200, new, [ecdsa, old])  # both were trusted for the host
    assert _revoked(ecdsa) == [] and len(_revoked(old)) == 1


async def test_typed_password_is_not_kept_after_login(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    conn = await SSHConnection.connect(ssh_server.profile(), FakePrompter())
    try:
        assert conn.client._password is None  # secret retention check: no private API for it
    finally:
        conn.close()


async def test_signal_name_from_the_server_is_cleaned_in_the_end_reason():
    from types import SimpleNamespace

    from termdeck.ssh import SSHShell

    class Stream:
        async def read(self, n):
            return b""

    process = SimpleNamespace(stdout=Stream(), exit_status=None, exit_signal=("\x1b]0;evil\x07KILL", False, "", ""),
                              wait_closed=lambda: asyncio.sleep(0))
    connection = SimpleNamespace(profile=SimpleNamespace(name="x"), client=None)
    shell = SSHShell(connection, process, [])
    assert await shell.read() == b""
    assert shell.end_reason == "Remote program killed by signal ]0;evilKILL"
