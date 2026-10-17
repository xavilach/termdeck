import json
import os
import sys
import time

import pytest

from termdeck import cli
from termdeck.console import EscapeFilter
from termdeck.profiles import ProfileStore
from termdeck.ssh import remember_host_key

from .conftest import FakePrompter


def run(capsys, *argv):
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_profile_commands(capsys):
    assert run(capsys, "add", "ssh", "web", "example.com", "-u", "bob", "-L", "8080:db:5432", "-g", "Prod",
               "--set", "ssh.keepalive=30")[0] == 0
    assert run(capsys, "add", "serial", "board", "/dev/ttyUSB0", "-b", "9600", "--parity", "E")[0] == 0
    assert run(capsys, "set", "web", "terminal.theme=dracula", "ssh.compression=yes", "-D", "1080")[0] == 0
    code, out, _ = run(capsys, "show", "web")
    web = json.loads(out)
    assert web["ssh"]["keepalive"] == 30 and web["ssh"]["compression"] is True
    assert web["terminal"]["theme"] == "dracula"
    assert [f["kind"] for f in web["ssh"]["forwards"]] == ["dynamic"]
    code, out, _ = run(capsys, "list")
    assert out.splitlines() == ["board\tserial /dev/ttyUSB0 9600 8E1", "web\t[Prod] ssh bob@example.com:22"]


@pytest.mark.parametrize("argv, message", [
    (["set", "web", "ssh.nope=1"], "unknown option 'ssh.nope'"),
    (["set", "web", "terminal.theme=pink"], "invalid color theme 'pink'"),
    (["set", "web", "ssh.port=abc"], "could not convert"),
    (["show", "missing"], "no profile named 'missing'"),
    (["add", "ssh", "web", "other"], "already exists"),
])
def test_errors_go_to_stderr_with_exit_2(capsys, argv, message):
    run(capsys, "add", "ssh", "web", "example.com")
    code, out, err = run(capsys, *argv)
    assert code == 2 and out == "" and message in err


def test_resolve_profile_prefers_saved_names():
    store = ProfileStore()
    store.save(cli.Profile("prod", "ssh", ssh=cli.SSHSettings(host="h")))
    assert cli.resolve_profile("prod", store).ssh.host == "h"
    assert cli.resolve_profile("alice@other:2222", store).ssh.port == 2222
    with pytest.raises(KeyError):
        cli.resolve_profile("not a target", store)


def test_escape_filter():
    f = EscapeFilter()
    assert f.feed(b"ls\r") == (b"ls\r", [])
    assert f.feed(b"\x1d") == (b"", [])  # escape pending across reads
    assert f.feed(b"b") == (b"", ["break"])
    assert f.feed(b"a\x1d]z\x1dq") == (b"a\x1dz", ["quit"])
    assert f.feed(b"\x1dx") == (b"", [])  # unknown key cancels the escape


async def test_sftp_command(ssh_server, tmp_path, monkeypatch, capsys):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    monkeypatch.setattr(cli, "ConsolePrompter", FakePrompter)
    store = ProfileStore()
    store.save(ssh_server.profile("srv"))
    local = tmp_path / "up.txt"
    local.write_text("data")
    parser = cli.build_parser()
    await cli.cmd_sftp(parser.parse_args(["sftp", "srv", "put", str(local), "/up.txt"]), store)
    await cli.cmd_sftp(parser.parse_args(["sftp", "srv", "ls", "/"]), store)
    assert capsys.readouterr().out.split() == ["up.txt"]
    await cli.cmd_sftp(parser.parse_args(["sftp", "srv", "get", "/up.txt", str(tmp_path / "down.txt")]), store)
    assert (tmp_path / "down.txt").read_text() == "data"


def test_no_command_starts_tui(monkeypatch):
    started = []
    monkeypatch.setattr("termdeck.tui.app.run", lambda name: started.append(name))
    assert cli.main([]) == 0
    assert started == [None]


@pytest.mark.skipif(sys.platform == "win32", reason="uses a POSIX pseudo-terminal")
def test_connect_in_a_real_terminal():
    """Runs `termdeck connect loop://` in a pty: typed text loops back, Ctrl+] q exits."""
    import pty
    import select
    import subprocess

    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, "-m", "termdeck", "connect", "loop://"],
                            stdin=slave, stdout=slave, stderr=slave, env=os.environ.copy())
    os.close(slave)

    def read_until(marker: bytes, timeout=10.0) -> bytes:
        buf, deadline = b"", time.monotonic() + timeout
        while marker not in buf and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                buf += os.read(master, 4096)
        return buf

    try:
        assert b"connected to serial loop://" in read_until(b"Ctrl+]")
        os.write(master, b"ping")
        assert b"ping" in read_until(b"ping")
        os.write(master, b"\x1dq")
        assert proc.wait(timeout=10) == 0
    finally:
        proc.kill()
        os.close(master)


def test_escape_aliases_from_extended_keyboard_modes():
    f = EscapeFilter()
    assert f.feed(b"\x1b[93;5uq") == (b"", ["quit"])
    assert f.feed(b"\x1b[27;5;93~b") == (b"", ["break"])
