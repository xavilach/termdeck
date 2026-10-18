import io
import json
import os
import re
import sys
import time

import pytest

from termdeck import cli
from termdeck.console import EscapeFilter
from termdeck.errors import SFTPFailure
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
    assert capsys.readouterr().out.split()[-1] == "up.txt"  # mode, size, name
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
        raise ConnectionRefusedError("stop here")

    monkeypatch.setattr(cli, "open_session", fake_open_session)
    code, out, err = run(capsys, "connect", "bob@host.example:2200")
    assert code == 1 and "Connection refused" in err
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


async def test_console_prompter_reads_in_a_daemon_thread(monkeypatch):
    import threading

    seen = []

    def fake_getpass(prompt, stream=None):
        seen.append((prompt, threading.current_thread()))
        return "hunter2"

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    monkeypatch.setattr(cli, "_open_terminal", lambda: io.StringIO(""))
    assert await cli.ConsolePrompter().ask("Password: ") == "hunter2"
    [(prompt, thread)] = seen
    assert prompt == "Password: "
    assert thread.daemon and thread is not threading.main_thread()  # exit must not wait for a pending prompt


async def test_console_prompter_end_of_input_is_a_cancel(monkeypatch):
    def eof(prompt, stream=None):
        raise EOFError

    monkeypatch.setattr(cli.getpass, "getpass", eof)
    monkeypatch.setattr(cli, "_open_terminal", lambda: io.StringIO(""))  # a terminal at end of input
    prompter = cli.ConsolePrompter()
    assert await prompter.ask("Password: ") is None
    assert await prompter.ask("login: ", secret=False) is None


def test_unexpected_exception_exits_70_with_a_traceback(monkeypatch, capsys):
    def boom(args, store):
        raise RuntimeError("bug in a command")

    monkeypatch.setattr(cli, "cmd_list", boom)
    code, out, err = run(capsys, "list")
    assert code == 70
    assert "Traceback" in err and "RuntimeError: bug in a command" in err and "Internal error" in err


def test_expected_exception_exits_1_without_a_traceback(monkeypatch, capsys):
    def refused(args, store):
        raise ConnectionRefusedError(111, "Connection refused")

    monkeypatch.setattr(cli, "cmd_list", refused)
    code, out, err = run(capsys, "list")
    assert code == 1 and "Connection refused" in err and "Traceback" not in err




@pytest.fixture
def sftp_cli(ssh_server, tmp_path, monkeypatch):
    """sftp_cli(*words, target=..., stdin=None) runs `termdeck sftp TARGET WORDS...`; returns the exit code."""
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    monkeypatch.setattr(cli, "ConsolePrompter", FakePrompter)
    store = ProfileStore()
    store.save(ssh_server.profile("srv"))
    monkeypatch.chdir(tmp_path)

    async def run_sftp(*words, target="srv", stdin=None, force=False, options=()):
        if stdin is not None:
            monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        argv = ["sftp", *(["--force"] if force else []), *options, target, *words]
        return await cli.cmd_sftp(cli.build_parser().parse_args(argv), store)

    return run_sftp


async def test_sftp_ls_long_format_and_quick_target(sftp_cli, ssh_server, capsys):
    (ssh_server.root / "dir").mkdir()
    (ssh_server.root / "f.txt").write_text("12345")
    target = f"alice@127.0.0.1:{ssh_server.port}"
    assert await sftp_cli("ls", "/", target=target) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [l.split()[-1] for l in lines] == ["dir/", "f.txt"]  # folders first
    assert lines[0].split()[0].startswith("d")
    # mode, size, modification time, name
    assert re.fullmatch(r"-[rwx-]{9} +\d+(?: \w+)? +\d{4}-\d\d-\d\d \d\d:\d\d f\.txt", lines[1]), lines[1]


async def test_sftp_get_refuses_to_overwrite_a_local_file_unless_forced(sftp_cli, ssh_server, tmp_path):
    (ssh_server.root / "r.txt").write_text("remote")
    local = tmp_path / "r.txt"
    local.write_text("local")
    with pytest.raises(ValueError, match="already exists.*-f"):
        await sftp_cli("get", "/r.txt", str(local))
    assert local.read_text() == "local"
    assert await sftp_cli("get", "-f", "/r.txt", str(local)) == 0
    assert local.read_text() == "remote"


async def test_sftp_force_flag_replaces(sftp_cli, ssh_server, tmp_path):
    (ssh_server.root / "r.txt").write_text("remote")
    local = tmp_path / "r.txt"
    local.write_text("local")
    assert await sftp_cli("get", "/r.txt", str(local), force=True) == 0
    assert local.read_text() == "remote"


async def test_sftp_put_recursive_folder(sftp_cli, ssh_server, tmp_path):
    src = tmp_path / "tree"
    (src / "inner").mkdir(parents=True)
    (src / "a.txt").write_text("a")
    (src / "inner" / "b.txt").write_text("世界")
    with pytest.raises(Exception):  # a folder needs -r
        await sftp_cli("put", str(src), "/tree")
    assert await sftp_cli("put", "-r", str(src), "/tree") == 0
    assert (ssh_server.root / "tree" / "a.txt").read_text() == "a"
    assert (ssh_server.root / "tree" / "inner" / "b.txt").read_text(encoding="utf-8") == "世界"


async def test_sftp_remote_file_commands(sftp_cli, ssh_server):
    root = ssh_server.root
    assert await sftp_cli("mkdir", "/d") == 0 and (root / "d").is_dir()
    (root / "d" / "f").write_text("x")
    with pytest.raises(Exception):
        await sftp_cli("rmdir", "/d")  # not empty
    assert await sftp_cli("mv", "/d/f", "/d/g") == 0 and (root / "d" / "g").exists()
    (root / "d" / "h").write_text("h")
    with pytest.raises(ValueError, match="already exists"):
        await sftp_cli("mv", "/d/g", "/d/h")
    assert await sftp_cli("mv", "/d/g", "/d/i") == 0
    (root / "d" / "h").write_text("x")
    assert await sftp_cli("chmod", "600", "/d/h") == 0
    assert (root / "d" / "h").stat().st_mode & 0o777 == 0o600
    assert await sftp_cli("rm", "-r", "/d") == 0 and not (root / "d").exists()
    assert await sftp_cli("mkdir", "/e") == 0 and await sftp_cli("rmdir", "/e") == 0 and not (root / "e").exists()


async def test_sftp_mv_force_replaces_the_target(sftp_cli, ssh_server):
    (ssh_server.root / "g").write_text("g")
    (ssh_server.root / "h").write_text("h")
    assert await sftp_cli("mv", "-f", "/g", "/h") == 0
    assert (ssh_server.root / "h").read_text() == "g"


async def test_sftp_unknown_command_and_bad_chmod_mode(sftp_cli, ssh_server):
    (ssh_server.root / "f").write_text("x")
    with pytest.raises(ValueError, match="unknown command 'frobnicate'"):
        await sftp_cli("frobnicate")
    with pytest.raises((ValueError, SFTPFailure), match="octal"):
        await sftp_cli("chmod", "rwx", "/f")
    with pytest.raises((ValueError, SFTPFailure), match="octal"):
        await sftp_cli("chmod", "789", "/f")
    with pytest.raises(ValueError, match="usage"):
        await sftp_cli("get")


async def test_sftp_shell_reads_commands_from_a_pipe(sftp_cli, ssh_server, tmp_path, capsys):
    (ssh_server.root / "sub").mkdir()
    (tmp_path / "x").write_text("payload")
    script = "cd sub\nput x\nls\npwd\nquit\nmkdir never\n"
    assert await sftp_cli(stdin=script) == 0
    out = capsys.readouterr().out
    assert (ssh_server.root / "sub" / "x").read_text() == "payload"
    assert out.splitlines()[-2].split()[-1] == "x" and out.splitlines()[-1] == "/sub"
    assert not (ssh_server.root / "never").exists()  # nothing runs after quit


async def test_sftp_shell_local_folder_commands(sftp_cli, ssh_server, tmp_path, capsys):
    (tmp_path / "dl").mkdir()
    (ssh_server.root / "r.txt").write_text("remote")
    assert await sftp_cli(stdin="lcd dl\nlpwd\nget /r.txt\n") == 0
    assert capsys.readouterr().out.strip() == str((tmp_path / "dl").resolve())
    assert (tmp_path / "dl" / "r.txt").read_text() == "remote"


async def test_sftp_script_stops_at_the_first_failure(sftp_cli, ssh_server, capsys):
    code = await sftp_cli(stdin="mkdir /one\nrmdir /missing\nmkdir /two\nquit\n")
    assert code == 1
    assert (ssh_server.root / "one").is_dir() and not (ssh_server.root / "two").exists()
    assert "/missing" in capsys.readouterr().err


async def test_sftp_script_reports_existing_files_and_cd_to_a_file(sftp_cli, ssh_server, tmp_path, capsys):
    (ssh_server.root / "f").write_text("x")
    (tmp_path / "f").write_text("local")
    assert await sftp_cli(stdin="get /f\n") == 1
    assert "already exists (use -f to replace it)" in capsys.readouterr().err
    assert await sftp_cli(stdin="cd /f\n") == 1
    assert "not a folder" in capsys.readouterr().err
    assert await sftp_cli(stdin="cd /nope\n") == 1
    assert "/nope" in capsys.readouterr().err
    assert await sftp_cli(stdin="") == 0  # end of input ends the shell


@pytest.mark.parametrize("alias, words, check", [
    ("dir", ["/"], lambda root, out: "f.txt" in out),
    ("del", ["/f.txt"], lambda root, out: not (root / "f.txt").exists()),
    ("ren", ["/f.txt", "/g.txt"], lambda root, out: (root / "g.txt").exists() and not (root / "f.txt").exists()),
    ("md", ["/newdir"], lambda root, out: (root / "newdir").is_dir()),
    ("rd", ["/emptydir"], lambda root, out: not (root / "emptydir").exists()),
    ("mget", ["/f.txt"], lambda root, out: True),
])
async def test_sftp_psftp_aliases(sftp_cli, ssh_server, tmp_path, capsys, alias, words, check):
    root = ssh_server.root
    (root / "f.txt").write_text("x")
    (root / "emptydir").mkdir()
    assert await sftp_cli(alias, *words) == 0
    assert check(root, capsys.readouterr().out)
    if alias == "mget":
        assert (tmp_path / "f.txt").read_text() == "x"


async def test_sftp_mput_uploads_into_the_remote_folder(sftp_cli, ssh_server, tmp_path):
    (tmp_path / "a.log").write_text("a")
    (tmp_path / "b.log").write_text("b")
    (tmp_path / "c.txt").write_text("c")
    (ssh_server.root / "up").mkdir()
    assert await sftp_cli(stdin="cd /up\nmput *.log\n") == 0
    assert sorted(p.name for p in (ssh_server.root / "up").iterdir()) == ["a.log", "b.log"]
    with pytest.raises(ValueError, match="no local match"):
        await sftp_cli("mput", "*.nothing")


async def test_sftp_script_skips_comments_and_blank_lines(sftp_cli, ssh_server):
    assert await sftp_cli(stdin="# comment\n\n   # indented comment\nmkdir /one\n#mkdir /two\n") == 0
    assert (ssh_server.root / "one").is_dir() and not (ssh_server.root / "two").exists()


async def test_sftp_rm_mkdir_chmod_take_several_names(sftp_cli, ssh_server):
    root = ssh_server.root
    for name in "abc":
        (root / name).write_text(name)
    assert await sftp_cli("chmod", "600", "/a", "/b") == 0
    assert (root / "a").stat().st_mode & 0o777 == 0o600 and (root / "b").stat().st_mode & 0o777 == 0o600
    assert await sftp_cli("chmod", "u+x", "/a", "/b") == 0
    assert (root / "a").stat().st_mode & 0o777 == 0o700
    assert await sftp_cli("rm", "/a", "/b", "/c") == 0
    assert list(root.iterdir()) == []
    assert await sftp_cli("mkdir", "/x", "/y") == 0
    assert (root / "x").is_dir() and (root / "y").is_dir()


async def test_sftp_mv_several_files_into_a_folder(sftp_cli, ssh_server):
    root = ssh_server.root
    for name in ("a", "b"):
        (root / name).write_text(name)
    (root / "dir").mkdir()
    (root / "plain").write_text("p")
    with pytest.raises(ValueError, match="needs a folder"):
        await sftp_cli("mv", "/a", "/b", "/plain")
    assert (root / "a").exists() and (root / "b").exists() and (root / "plain").read_text() == "p"
    with pytest.raises(ValueError, match="needs a folder"):
        await sftp_cli("mv", "/a", "/b", "/missing")
    assert await sftp_cli("mv", "/a", "/b", "/dir/") == 0
    assert sorted(p.name for p in (root / "dir").iterdir()) == ["a", "b"]
    assert not (root / "a").exists() and not (root / "b").exists()


async def test_sftp_wildcards_for_get_and_rm(sftp_cli, ssh_server, tmp_path):
    root = ssh_server.root
    for name in ("a.txt", "b.txt", "c.log", "x.tmp", "y.tmp"):
        (root / name).write_text(name)
    (tmp_path / "dl").mkdir()
    assert await sftp_cli(stdin="lcd dl\nget /*.txt\n") == 0
    assert sorted(p.name for p in (tmp_path / "dl").iterdir()) == ["a.txt", "b.txt"]
    assert await sftp_cli("rm", "/*.tmp") == 0
    assert sorted(p.name for p in root.iterdir()) == ["a.txt", "b.txt", "c.log"]
    with pytest.raises(SFTPFailure, match=r"/\*\.tmp"):
        await sftp_cli("rm", "/*.tmp")


async def test_sftp_unknown_option_and_double_dash(sftp_cli, ssh_server):
    root = ssh_server.root
    (root / "f").write_text("x")
    (root / "-dash").write_text("d")
    with pytest.raises(ValueError, match="unknown option -x"):
        await sftp_cli("rm", "-x", "/f")
    assert (root / "f").exists()
    with pytest.raises(ValueError, match="unknown option"):
        await sftp_cli("ls", "-l")
    assert await sftp_cli("rm", "--", "-dash") == 0
    assert not (root / "-dash").exists() and (root / "f").exists()


async def test_sftp_force_in_every_position(sftp_cli, ssh_server, tmp_path):
    (ssh_server.root / "x").write_text("remote")
    local = tmp_path / "x"
    local.write_text("local")
    with pytest.raises(ValueError, match="already exists"):
        await sftp_cli("get", "/x")
    assert await sftp_cli("get", "--force", "/x") == 0 and local.read_text() == "remote"
    local.write_text("local")
    assert await sftp_cli("--force", "get", "/x") == 0 and local.read_text() == "remote"  # termdeck sftp T --force get x
    local.write_text("local")
    assert await sftp_cli("get", "-rf", "/x") == 0 and local.read_text() == "remote"  # combined letters
    local.write_text("local")
    assert await sftp_cli("get", "--recursive", "--force", "/x") == 0 and local.read_text() == "remote"


async def test_sftp_rm_recursive_removes_a_folder(sftp_cli, ssh_server):
    (ssh_server.root / "dir" / "sub").mkdir(parents=True)
    (ssh_server.root / "dir" / "sub" / "f").write_text("x")
    with pytest.raises(SFTPFailure):
        await sftp_cli("rm", "/dir")
    assert await sftp_cli("rm", "-r", "/dir") == 0
    assert not (ssh_server.root / "dir").exists()


async def test_sftp_rm_with_combined_flags_rf(sftp_cli, ssh_server):
    (ssh_server.root / "dir").mkdir()
    (ssh_server.root / "dir" / "f").write_text("x")
    assert await sftp_cli("rm", "-rf", "/dir") == 0
    assert not (ssh_server.root / "dir").exists()


async def test_sftp_script_file_with_b(sftp_cli, ssh_server, tmp_path):
    script = tmp_path / "script.txt"
    script.write_text("mkdir /fromfile\n", encoding="utf-8")
    assert await sftp_cli(options=["-b", str(script)]) == 0
    assert (ssh_server.root / "fromfile").is_dir()
    with pytest.raises(Exception, match="batch file"):
        await sftp_cli(options=["-b", str(tmp_path / "missing.txt")])


async def test_sftp_echo_prints_each_command_to_stderr(sftp_cli, ssh_server, capsys):
    assert await sftp_cli(stdin="# skipped\nmkdir /e\npwd\n", options=["--echo"]) == 0
    captured = capsys.readouterr()
    assert captured.err.splitlines() == ["sftp> mkdir /e", "sftp> pwd"]
    assert captured.out.strip() == "/"
    assert await sftp_cli(stdin="pwd\n") == 0
    assert "sftp>" not in capsys.readouterr().err


async def test_sftp_keep_going_continues_and_exits_1(sftp_cli, ssh_server, capsys):
    script = "mkdir /one\nrmdir /missing\nmkdir /two\n"
    assert await sftp_cli(stdin=script, options=["--keep-going"]) == 1
    assert (ssh_server.root / "one").is_dir() and (ssh_server.root / "two").is_dir()
    err = capsys.readouterr().err
    assert "line 2:" in err and "/missing" in err and "line 3" not in err


async def test_sftp_script_without_keep_going_names_the_failing_line(sftp_cli, ssh_server, capsys):
    assert await sftp_cli(stdin="# c\nmkdir /one\nrmdir /missing\nmkdir /two\n") == 1
    err = capsys.readouterr().err
    assert "line 3:" in err and not (ssh_server.root / "two").exists()


async def test_sftp_script_reports_a_bad_option_with_the_line(sftp_cli, ssh_server, capsys):
    assert await sftp_cli(stdin="mkdir /ok\nrm -x /ok\n") == 1
    assert "line 2:" in capsys.readouterr().err and (ssh_server.root / "ok").exists()


async def test_sftp_names_from_the_server_never_carry_escape_sequences(sftp_cli, ssh_server, capsys):
    evil = "evil\x1b[2Jname"
    (ssh_server.root / evil).write_text("x")
    (ssh_server.root / "dir\x1b]0;title\x07").mkdir()
    assert await sftp_cli("ls", "/") == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "evil" in out and "name" in out
    (ssh_server.root / "dir\x1b]0;title\x07" / "f").write_text("x")
    assert await sftp_cli(stdin="cd /dir\x1b]0;title\x07\npwd\n") == 0
    assert "\x1b" not in capsys.readouterr().out
    assert await sftp_cli("get", "/evil\x1b[2Jname") == 0
    with pytest.raises(ValueError, match="already exists") as caught:
        await sftp_cli("get", "/evil\x1b[2Jname")
    assert "\x1b" not in str(caught.value)


async def test_sftp_prompt_names_are_cleaned(sftp_cli, ssh_server, monkeypatch):
    (ssh_server.root / "d\x1b[31m").mkdir()
    prompts = []

    class Recording(FakePrompter):
        async def ask(self, prompt, secret=True):
            if not secret:
                prompts.append(prompt)
                return "cd /d\x1b[31m" if len(prompts) == 1 else None
            return await super().ask(prompt, secret)

    monkeypatch.setattr(cli, "ConsolePrompter", Recording)
    monkeypatch.setattr(sys, "stdin", type("Tty", (io.StringIO,), {"isatty": lambda self: True})())
    assert await sftp_cli() == 0
    assert len(prompts) == 2 and all("\x1b" not in p for p in prompts) and "d" in prompts[1]
