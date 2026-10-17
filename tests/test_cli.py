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
    (["set", "web", "ssh.port=abc"], "ssh.port: expected a number"),
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


def test_tui_with_unknown_profile_fails_before_starting_the_ui(monkeypatch, capsys):
    monkeypatch.setattr("termdeck.tui.app.run", lambda *a: pytest.fail("the UI must not start"))
    assert run(capsys, "add", "ssh", "web", "example.com")[0] == 0
    code, out, err = run(capsys, "tui", "nosuch")
    assert code == 2 and out == "" and "'nosuch' is not a profile" in err and "did you mean" not in err
    code, _, err = run(capsys, "tui", "wbe")
    assert code == 2 and "did you mean 'web'?" in err


def test_tui_with_saved_profile_passes_the_profile(monkeypatch, capsys):
    started = []
    monkeypatch.setattr("termdeck.tui.app.run", lambda profile: started.append(profile))
    run(capsys, "add", "ssh", "web", "example.com")
    assert run(capsys, "tui", "web")[0] == 0
    assert started[0].name == "web"


async def test_connect_with_mistyped_profile_name_never_prompts(monkeypatch, capsys):
    async def no_session(*a, **k):
        pytest.fail("must not connect or prompt for a password")

    monkeypatch.setattr(cli, "open_session", no_session)
    monkeypatch.setattr(cli, "ConsolePrompter", lambda *a: pytest.fail("must not prompt"))
    store = ProfileStore()
    store.save(cli.Profile("web", "ssh", ssh=cli.SSHSettings(host="example.com")))
    # main() uses asyncio.run, so call the pieces it uses
    args = cli.build_parser().parse_args(["connect", "wbe"])
    with pytest.raises(KeyError, match="did you mean 'web'"):
        await cli.cmd_connect(args, store)


# ---- batch 2: store problems, option metadata ----

def _profiles_file(entries, **extra):
    path = ProfileStore().path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"profiles": entries, **extra}))
    return path


_BAD_ENTRY = {"name": "bad", "kind": "serial", "serial": {"device": "d", "parity": "Q"}}
_GOOD_ENTRY = {"name": "good", "kind": "serial", "serial": {"device": "d"}}


def test_list_warns_about_invalid_entries_on_stderr(capsys):
    _profiles_file([_GOOD_ENTRY, _BAD_ENTRY])
    code, out, err = run(capsys, "list")
    assert code == 0 and out.splitlines() == ["good\tserial d 115200 8N1"]
    assert "warning:" in err and "bad" in err


def test_show_invalid_profile_says_so(capsys):
    _profiles_file([_BAD_ENTRY])
    code, out, err = run(capsys, "show", "bad")
    assert code == 2 and out == "" and "is invalid" in err


def test_list_without_profiles_hints_on_stderr(capsys):
    code, out, err = run(capsys, "list")
    assert code == 0 and out == "" and "termdeck add" in err


def test_corrupt_profiles_file_exits_2_with_path(capsys):
    path = _profiles_file([])
    path.write_text("{oops")
    for argv in (["list"], ["add", "ssh", "x", "h"], ["rm", "x"]):
        code, out, err = run(capsys, *argv)
        assert code == 2 and str(path) in err
    assert path.read_text() == "{oops"


def test_newer_profiles_file_exits_2(capsys):
    path = _profiles_file([], version=2)
    code, _, err = run(capsys, "list")
    assert code == 2 and str(path) in err


def test_set_rejects_usb_id_and_forwards_as_options(capsys):
    run(capsys, "add", "serial", "board", "/dev/ttyUSB0")
    for option in ("serial.usb_id=x", "ssh.forwards=x"):
        code, _, err = run(capsys, "set", "board", option)
        assert code == 2 and "unknown option" in err


def test_set_error_names_the_option(capsys):
    run(capsys, "add", "serial", "board", "/dev/ttyUSB0")
    code, _, err = run(capsys, "set", "board", "serial.baudrate=abc")
    assert code == 2 and "serial.baudrate" in err
    code, _, err = run(capsys, "set", "board", "serial.baudrate=0")
    assert code == 2 and "speed" in err.lower()


def test_options_lists_settable_options_only(capsys):
    code, out, _ = run(capsys, "options")
    keys = [line.split()[0].split("=", 1)[0] for line in out.splitlines() if line.strip()]
    assert "ssh.keepalive" in keys
    assert "serial.usb_id" not in keys and "ssh.forwards" not in keys


def test_add_serial_options_come_from_option_metadata():
    from termdeck.profiles import SerialSettings, describe_options

    parser = cli.build_parser()
    defaults = SerialSettings()
    args = parser.parse_args(["add", "serial", "n", "d"])
    assert (args.baud, args.databits, args.parity, args.stopbits, args.flow) == \
        (defaults.baudrate, defaults.bytesize, defaults.parity, defaults.stopbits, defaults.flow)
    options = {o["key"]: o for o in describe_options()["serial"]}
    for flag, key, convert in (("--parity", "parity", str), ("--flow", "flow", str),
                               ("--databits", "bytesize", str), ("--stopbits", "stopbits", str)):
        for value, _ in options[key]["choices"]:
            parser.parse_args(["add", "serial", "n", "d", flag, convert(value)])  # every declared choice is accepted
        with pytest.raises(SystemExit):
            parser.parse_args(["add", "serial", "n", "d", flag, "bogus"])


# ---- batch 2 review fixes ----

def test_rm_removes_an_invalid_entry(capsys):
    _profiles_file([_GOOD_ENTRY, _BAD_ENTRY])
    assert run(capsys, "rm", "bad")[0] == 0
    code, out, err = run(capsys, "list")
    assert out.splitlines() == ["good\tserial d 115200 8N1"] and "warning" not in err


def test_set_repairs_an_invalid_entry(capsys):
    _profiles_file([_BAD_ENTRY])
    assert run(capsys, "set", "bad", "serial.parity=N")[0] == 0
    code, out, err = run(capsys, "list")
    assert out.splitlines() == ["bad\tserial d 115200 8N1"] and "warning" not in err


def test_rename_onto_an_invalid_entry_is_refused(capsys):
    path = _profiles_file([_GOOD_ENTRY, _BAD_ENTRY])
    before = path.read_text()
    code, out, err = run(capsys, "rename", "good", "bad")
    assert code == 2 and "already exists" in err
    assert [e["name"] for e in json.loads(path.read_text())["profiles"]] == [e["name"] for e in json.loads(before)["profiles"]]


def test_connect_with_corrupt_file_still_resolves_quick_targets(monkeypatch, capsys):
    path = _profiles_file([])
    path.write_text("{oops")
    opened = []

    async def fake_open_session(profile, prompter, *a, **k):
        opened.append(profile)
        raise RuntimeError("stop here")

    monkeypatch.setattr(cli, "open_session", fake_open_session)
    code, out, err = run(capsys, "connect", "bob@host.example:2200")
    assert code == 1 and "stop here" in err
    (profile,) = opened
    assert (profile.kind, profile.ssh.username, profile.ssh.host, profile.ssh.port) == \
        ("ssh", "bob", "host.example", 2200)
    assert "warning" in err and str(path) in err
    assert path.read_text() == "{oops"


@pytest.mark.parametrize("setting, message", [
    ("serial.baudrate=0", "serial.baudrate:"),
    ("serial.parity=e", "expected one of"),
    ("ssh.port=22.5", "whole number"),
])
def test_set_value_errors_are_specific(capsys, setting, message):
    run(capsys, "add", "ssh", "web", "example.com")
    run(capsys, "add", "serial", "board", "/dev/ttyUSB0")
    name = "board" if setting.startswith("serial") else "web"
    code, out, err = run(capsys, "set", name, setting)
    assert code == 2 and out == "" and message in err


def test_options_table_is_readable(capsys):
    code, out, _ = run(capsys, "options")
    lines = [line for line in out.splitlines() if line.startswith(("ssh.", "serial.", "terminal."))]
    rows = {line.split()[0]: line for line in lines}
    assert "true|false" in rows["ssh.compression"]
    assert "1..65535" in rows["ssh.port"]
    assert any("(empty)" in line for line in lines)
    # columns are aligned: the accepted values and the default start at the same place on every row
    second = {len(line) - len(line[len(line.split()[0]):].lstrip()) for line in lines}
    third = {line.index("default ") for line in lines}
    assert len(second) == 1 and len(third) == 1
