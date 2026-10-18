"""`termdeck connect` in scripted mode (pipes), the way plink and ssh are used in scripts."""

import asyncio
import io
import os
import sys

import pytest

from termdeck import console
from termdeck.profiles import Profile, SerialSettings, describe_options
from termdeck.session import open_session

from .conftest import PASSWORD, USER, FakePrompter, free_port, read_until

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pipes of the POSIX test setup")


@pytest.fixture
def password_file(tmp_path):
    path = tmp_path / "pw"
    path.write_text(f"{PASSWORD}\nsecond line\n")
    return path


async def run_cli(ssh_server, *args, stdin=b"", trust=True, target=None):
    """Runs `termdeck connect` in a subprocess; returns (exit code, stdout, stderr).

    The subprocess has no controlling terminal (a new session), so nothing can be asked on /dev/tty."""
    if trust:
        from termdeck.ssh import remember_host_key

        remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    target = target or f"{USER}@127.0.0.1:{ssh_server.port}"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "termdeck", "connect", *_split(args, target),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=dict(os.environ), start_new_session=True)
    out, err = await asyncio.wait_for(proc.communicate(stdin), 60)
    return proc.returncode, out, err.decode()


def _split(args, target):
    """Arguments given as (options..., None, command...): the target goes where None is."""
    if None in args:
        i = args.index(None)
        return [*args[:i], target, *args[i + 1:]]
    return [*args, target]


async def test_pipe_command_returns_remote_status_and_prints_output(ssh_server, password_file):
    code, out, err = await run_cli(ssh_server, "--password-file", str(password_file), None, "echo", "hi")
    assert code == 0 and b"ran echo hi" in out
    assert "[termdeck] connecting" not in err and "connected" not in err
    code, out, err = await run_cli(ssh_server, "--password-file", str(password_file), None, "exit3")
    assert code == 3


async def test_pipe_without_command_echoes_stdin_and_ends_at_eof(ssh_server, password_file):
    code, out, err = await run_cli(ssh_server, "--password-file", str(password_file), stdin=b"hello there\n")
    assert code == 0
    assert b"hello None" in out and b"hello there" in out  # no terminal asked: no terminal type
    assert "[termdeck] connecting" not in err


async def test_batch_unknown_host_key_fails_without_asking(ssh_server, password_file):
    code, out, err = await run_cli(ssh_server, "--batch", "--password-file", str(password_file), trust=False)
    assert code == 1 and "--batch never asks" in err and "Trust this key" not in err
    from termdeck.ssh import known_hosts_path

    assert not known_hosts_path().exists() or "ssh-ed25519" not in known_hosts_path().read_text()


async def test_batch_password_prompt_fails(ssh_server):
    code, out, err = await run_cli(ssh_server, "--batch")
    assert code == 1 and "needs an answer" in err


async def test_password_file_answers_the_prompt(ssh_server, password_file):
    code, out, err = await run_cli(ssh_server, "--batch", "--password-file", str(password_file), None, "hello")
    assert code == 0 and b"ran hello" in out


async def test_wrong_password_in_file_is_not_retried(ssh_server, tmp_path):
    wrong = tmp_path / "wrong"
    wrong.write_text("nope\n")
    # With --batch a second question fails at once; the file must not answer it again.
    code, out, err = await run_cli(ssh_server, "--batch", "--password-file", str(wrong), None, "hello")
    assert code == 1 and "needs an answer" in err and b"ran" not in out


async def test_command_from_file(ssh_server, password_file, tmp_path):
    script = tmp_path / "cmd.txt"
    script.write_text("from-file")
    code, out, _ = await run_cli(ssh_server, "--password-file", str(password_file), "-m", str(script))
    assert code == 0 and b"ran from-file" in out


async def test_subsystem(ssh_server, password_file):
    code, out, _ = await run_cli(ssh_server, "--password-file", str(password_file), "-s", "netconf")
    assert code == 0 and b"subsystem netconf" in out


async def test_no_shell_session_ends_only_when_the_connection_closes(ssh_server):
    profile = ssh_server.profile()
    profile.ssh.no_shell = True
    from termdeck.ssh import remember_host_key

    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    session = await open_session(profile, FakePrompter())
    assert b"connected without a shell" in await read_until(session, b"without a shell")
    pending = asyncio.ensure_future(session.read())
    await asyncio.sleep(0)
    assert not pending.done()
    await session.close()
    assert await asyncio.wait_for(pending, 5) == b""


async def test_no_terminal_flag_and_pipe_default_send_no_terminal_type(ssh_server, password_file):
    code, out, _ = await run_cli(ssh_server, "--password-file", str(password_file), "-T")
    assert b"hello None" in out
    code, out, _ = await run_cli(ssh_server, "--password-file", str(password_file))
    assert b"hello None" in out  # pipes: no terminal by default


async def test_forced_terminal_with_pipes(ssh_server, password_file):
    code, out, _ = await run_cli(ssh_server, "--password-file", str(password_file), "-t")
    assert b"hello xterm" in out


async def test_stderr_stays_apart_without_a_terminal(ssh_server, password_file):
    code, out, err = await run_cli(ssh_server, "--password-file", str(password_file), None, "err")
    assert code == 0 and b"to-stdout" in out and b"to-stderr" not in out
    assert "to-stderr" in err


async def test_nc_connects_stdin_and_stdout_to_a_tcp_port(ssh_server, password_file):
    async def echo(reader, writer):
        while data := await reader.read(1024):
            writer.write(data)
            await writer.drain()
        writer.close()

    echo_server = await asyncio.start_server(echo, "127.0.0.1", 0)
    try:
        port = echo_server.sockets[0].getsockname()[1]
        code, out, err = await run_cli(ssh_server, "--password-file", str(password_file), "--nc",
                                       f"127.0.0.1:{port}", stdin=b"round trip\n")
    finally:
        echo_server.close()
    assert code == 0 and out == b"round trip\n"


async def test_nc_rejects_a_malformed_target(ssh_server, password_file):
    code, _, err = await run_cli(ssh_server, "--password-file", str(password_file), "--nc", "nonsense")
    assert code == 2 and "HOST:PORT" in err


@pytest.mark.parametrize("flag, value", [("-4", "ipv4"), ("-6", "ipv6")])
def test_address_family_flags(flag, value):
    from termdeck import cli

    args = cli.build_parser().parse_args(["connect", flag, "host:22"])
    profile = cli._connect_profile(args, cli.ProfileStore())
    assert profile.ssh.address_family == value


async def test_ipv4_flag_still_connects(ssh_server, password_file):
    code, out, _ = await run_cli(ssh_server, "-4", "--password-file", str(password_file), None, "x")
    assert code == 0 and b"ran x" in out


@pytest.mark.parametrize("flags", [["-N"], ["-L", "1:h:2"], ["-s", "sftp"], ["--nc", "h:1"], ["-4"]])
async def test_serial_target_refuses_ssh_only_flags(ssh_server, flags):
    code, out, err = await run_cli(ssh_server, *flags, target="loop://")
    assert code == 2 and "only for SSH connections" in err


async def test_serial_target_refuses_a_command(ssh_server):
    code, _, err = await run_cli(ssh_server, None, "ls", target="loop://")
    assert code == 2 and "a command: only for SSH connections" in err


async def test_verbose_logs_go_to_stderr(ssh_server, password_file):
    code, out, err = await run_cli(ssh_server, "-v", "--password-file", str(password_file), None, "x")
    assert code == 0 and "asyncssh" in err and b"ran x" in out


async def test_ssh_log_file_has_protocol_lines_but_not_the_password(ssh_server, tmp_path):
    secret = tmp_path / "pw"
    secret.write_text(f"{PASSWORD}\n")
    log = tmp_path / "ssh.log"
    code, out, _ = await run_cli(ssh_server, "--ssh-log", str(log), "--password-file", str(secret), None, "x")
    assert code == 0
    text = log.read_text()
    assert "asyncssh" in text and len(text.splitlines()) > 5
    assert PASSWORD not in text


# console

class _Stdin:
    def __init__(self, fd):
        self.fd = fd

    def fileno(self):
        return self.fd


async def test_pipe_ends_a_serial_session_after_drain_when_stdin_ends(monkeypatch):
    monkeypatch.setattr(console, "DRAIN", 0.05)
    r, w = os.pipe()
    os.write(w, b"abc")
    os.close(w)
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", _Stdin(r))
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(out, write_through=True))
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    session = await open_session(profile, FakePrompter())
    try:
        await asyncio.wait_for(console.pipe(session), 10)
    finally:
        await session.close()
        os.close(r)
    assert b"abc" in out.getvalue()  # the loop echoed it back before the end


def test_terminal_size_ignores_columns_and_lines(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("LINES", "100")
    monkeypatch.setattr(os, "get_terminal_size", lambda fd: os.terminal_size((90, 30)))
    assert tuple(console.terminal_size()) == (90, 30)

    def fail(fd):
        raise OSError("not a tty")

    monkeypatch.setattr(os, "get_terminal_size", fail)
    assert tuple(console.terminal_size()) == (80, 24)


async def test_interact_refuses_without_a_tty(monkeypatch):
    class Out:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdout", Out())
    with pytest.raises(ValueError, match="scripted mode"):
        await console.interact(object())


def test_windows_console_write_holds_a_split_utf8_sequence(monkeypatch):
    import codecs

    out = io.BytesIO()

    class Stdout:
        buffer = out

    monkeypatch.setattr(sys, "stdout", Stdout())
    c = console._WindowsConsole()
    c._utf8 = codecs.getincrementaldecoder("utf-8")("replace")
    data = "a界\U0001F600".encode()  # 1-, 3- and 4-byte characters
    c.write(data[:2])
    assert out.getvalue() == b"a"
    c.write(data[2:5])
    assert out.getvalue() == "a界".encode()
    c.write(data[5:])
    assert out.getvalue() == "a界\U0001F600".encode()


def test_new_ssh_options_are_advanced_and_ssh_only():
    ssh = {o["key"]: o for o in describe_options()["ssh"]}
    for key in ("pty", "no_shell", "subsystem", "address_family"):
        assert ssh[key]["advanced"] is True, key
        assert ssh[key].get("kinds", ["ssh"]) == ["ssh"], key


# batch 4: prompts, --password-file, raw pipes, exit statuses

import asyncssh  # noqa: E402

from termdeck import cli  # noqa: E402
from termdeck.profiles import ProfileStore  # noqa: E402
from termdeck.session import HostKey  # noqa: E402
from termdeck.ssh import known_hosts_path, remember_host_key  # noqa: E402


def _no_key_saved():
    return not known_hosts_path().exists() or "ssh-ed25519" not in known_hosts_path().read_text()


async def test_piped_stdin_never_answers_the_host_key_question(ssh_server):
    # No terminal to ask on (new session): the "y" in the data must not trust the key.
    code, out, err = await run_cli(ssh_server, stdin=b"y\nmore data\n", trust=False)
    assert code == 1 and "no terminal to ask on" in err
    assert _no_key_saved()
    assert b"hello" not in out


async def test_the_question_is_answered_on_the_terminal_not_on_stdin(monkeypatch):
    stdin = io.StringIO("y\n")
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(cli, "_open_terminal", lambda: io.StringIO("n\n"))
    key = HostKey("h", 22, "ssh-ed25519", "SHA256:x")
    assert await cli.ConsolePrompter().confirm_host_key(key) is False  # the terminal said n
    assert stdin.tell() == 0  # stdin was not touched
    monkeypatch.setattr(cli, "_open_terminal", lambda: io.StringIO("yes\n"))
    assert await cli.ConsolePrompter().confirm_host_key(key) is True


async def test_password_file_does_not_answer_the_jump_host(ssh_server, password_file):
    remember_host_key("localhost", ssh_server.port, ssh_server.key)
    code, out, err = await run_cli(ssh_server, "--batch", "--password-file", str(password_file),
                                   "--set", f"ssh.jump_host={USER}@localhost:{ssh_server.port}", None, "x")
    assert code == 1 and b"ran" not in out
    assert f"{USER}@localhost password:" in err and "needs an answer" in err  # the jump host's question failed


async def test_password_file_does_not_answer_a_key_passphrase(ssh_server, password_file, tmp_path):
    key_path = tmp_path / "id_test"
    asyncssh.generate_private_key("ssh-ed25519").write_private_key(key_path, passphrase="pw")
    code, out, err = await run_cli(ssh_server, "--batch", "--password-file", str(password_file),
                                   "--set", f"ssh.key_file={key_path}", None, "x")
    assert code == 1 and "Passphrase for" in err and "needs an answer" in err and b"ran" not in out


async def test_password_file_answers_the_targets_keyboard_interactive_prompt(ssh_server, password_file):
    ssh_server.kbdint = True
    code, out, err = await run_cli(ssh_server, "--batch", "--password-file", str(password_file), None, "x")
    assert code == 0 and b"ran x" in out


async def test_password_file_prompter_only_answers_hidden_questions_of_the_target(password_file):
    class Inner:
        def __init__(self):
            self.asked = []

        async def ask(self, prompt, secret=True):
            self.asked.append(prompt)

    inner = Inner()
    prompter = cli.PasswordFilePrompter(inner, str(password_file), "example.com")
    assert await prompter.ask("(bob@other.org) Password: ") is None  # another host's question
    assert await prompter.ask("(bob@example.com) Visible: ", secret=False) is None  # echoed: not a password
    assert await prompter.ask("bob@other.org password: ") is None
    assert len(inner.asked) == 3
    assert await prompter.ask("(bob@example.com) Password: ") == PASSWORD
    assert await prompter.ask("bob@example.com password: ") is None  # only once


async def test_raw_session_passes_enter_and_input_unchanged(ssh_server):
    profile = ssh_server.profile()
    profile.ssh.pty = False
    profile.terminal.enter_sends = "crlf"
    profile.terminal.local_echo = True
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    session = await open_session(profile, FakePrompter(), raw=True)
    try:
        await read_until(session, b"\n")
        await session.write(b"ab\r")
        assert await read_until(session, b"ab\r") == b"ab\r"  # no CRLF, no second copy from local echo
    finally:
        await session.close()


@pytest.mark.parametrize("raw, expected", [(True, "got 'go\\n'"), (False, "got 'Xgo\\n'")])
async def test_answerback_is_not_sent_in_raw_sessions(ssh_server, raw, expected):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    profile = ssh_server.profile(remote_command="enq", pty=False)
    profile.terminal.answerback = "X"
    session = await open_session(profile, FakePrompter(), raw=raw)
    try:
        await read_until(session, b"\x05")  # an answerback would be sent before this is returned
        await session.write(b"go\n")
        text = (await read_until(session, b"\r\n")).decode()
        if raw:
            assert expected in text
        else:
            assert "got 'X" in text  # the answerback came first
    finally:
        await session.close()


async def test_notices_go_to_stderr_in_pipe_mode(ssh_server, password_file):
    code, out, err = await run_cli(ssh_server, "-L", f"0.0.0.0:{free_port()}:127.0.0.1:9", "--password-file",
                                   str(password_file), None, "x")
    assert code == 0 and b"ran x" in out
    assert b"[termdeck]" not in out and b"accepts connections from other machines" not in out
    assert "accepts connections from other machines" in err


async def test_batch_changed_host_key_is_loud_on_stderr(ssh_server, password_file):
    old = asyncssh.generate_private_key("ssh-ed25519")
    remember_host_key("127.0.0.1", ssh_server.port, old)
    code, out, err = await run_cli(ssh_server, "--batch", "--password-file", str(password_file), trust=False)
    assert code == 1 and "WARNING" in err and "has CHANGED" in err
    assert old.get_fingerprint() in err
    assert b"hello" not in out


async def test_ssh_log_hides_environment_values(ssh_server, password_file, tmp_path):
    log = tmp_path / "ssh.log"
    code, out, _ = await run_cli(ssh_server, "--ssh-log", str(log), "--password-file", str(password_file),
                                 "--set", "ssh.env=FOO=topsecret", None, "x")
    assert code == 0 and b"FOO=topsecret" in out  # the server did get it
    text = log.read_text()
    assert "FOO" in text and "topsecret" not in text


async def test_no_shell_with_stdin_from_devnull_keeps_running(ssh_server, password_file):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "termdeck", "connect", "-N", "--password-file", str(password_file),
        f"{USER}@127.0.0.1:{ssh_server.port}",
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    try:
        notice = await asyncio.wait_for(proc.stderr.readline(), 30)
        assert b"connected without a shell" in notice  # on stderr
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(proc.wait()), 1)  # still running
    finally:
        proc.kill()
        out = await proc.stdout.read()
        await proc.wait()
    assert out == b""


def _parse(argv):
    parser = cli.build_parser()
    head, command = cli._split_command(parser.connect_parser, argv)
    args = parser.parse_args(["connect", *head])
    args.command = command
    return args


def test_options_after_the_target_and_the_command():
    parser = cli.build_parser()
    assert cli._split_command(parser.connect_parser, ["web", "--set", "ssh.keepalive=3", "uptime"]) == (
        ["web", "--set", "ssh.keepalive=3"], ["uptime"])
    assert cli._split_command(parser.connect_parser, ["web", "-t", "cmd"]) == (["web", "-t"], ["cmd"])
    assert cli._split_command(parser.connect_parser, ["web", "--", "-dash-cmd"]) == (["web"], ["-dash-cmd"])
    assert cli._split_command(parser.connect_parser, ["--set=ssh.keepalive=3", "web", "ls", "-l"]) == (
        ["--set=ssh.keepalive=3", "web"], ["ls", "-l"])
    store = ProfileStore()
    store.save(Profile("web", "ssh", ssh=cli.SSHSettings(host="h")))
    profile = cli._connect_profile(_parse(["web", "--set", "ssh.keepalive=3", "uptime"]), store)
    assert profile.ssh.keepalive == 3 and profile.ssh.remote_command == "uptime"
    assert cli._connect_profile(_parse(["web", "-t", "cmd"]), store).ssh.pty is True
    assert cli._connect_profile(_parse(["web", "--", "-dash-cmd"]), store).ssh.remote_command == "-dash-cmd"


async def test_options_and_dash_command_after_the_target_end_to_end(ssh_server, password_file):
    pw = ("--password-file", str(password_file))
    code, out, _ = await run_cli(ssh_server, *pw, None, "--set", "ssh.keepalive=3", "uptime")
    assert code == 0 and b"ran uptime" in out
    code, out, _ = await run_cli(ssh_server, *pw, None, "--", "-dash-cmd")
    assert code == 0 and b"ran -dash-cmd" in out
    code, out, _ = await run_cli(ssh_server, *pw, None, "-t")
    assert b"hello xterm" in out


class _Backpressure:
    """A session whose write() blocks until released."""

    def __init__(self):
        self.release = asyncio.Event()
        self.ended = asyncio.Event()
        self.received = 0

    async def write(self, data):
        await self.release.wait()
        self.received += len(data)

    async def write_eof(self):
        self.ended.set()
        return True

    async def read(self):
        await self.ended.wait()
        return b""


async def test_pipe_reads_stdin_only_a_few_chunks_ahead_of_the_remote_side(monkeypatch):
    import threading

    r, w = os.pipe()
    reads, end_seen = [], threading.Event()
    real_read = os.read

    def counting_read(fd, n):
        data = real_read(fd, n)
        if fd == r:
            reads.append(len(data))
            if not data:
                end_seen.set()
        return data

    monkeypatch.setattr(os, "read", counting_read)
    monkeypatch.setattr(sys, "stdin", _Stdin(r))
    total = console.PIPE_BACKLOG * 2 * 65536
    writer = threading.Thread(target=lambda: (os.write(w, b"x" * total), os.close(w)), daemon=True)
    session = _Backpressure()
    task = asyncio.ensure_future(console.pipe(session))
    writer.start()
    loop = asyncio.get_running_loop()
    try:
        async def until_backlog_is_full():
            while len(reads) < console.PIPE_BACKLOG:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(until_backlog_is_full(), 10)
        with pytest.raises(asyncio.TimeoutError):  # the end of stdin is not reached while the remote side waits
            await asyncio.wait_for(loop.run_in_executor(None, end_seen.wait), 0.3)
        assert len(reads) == console.PIPE_BACKLOG
        session.release.set()
        await asyncio.wait_for(task, 10)
        assert session.received == total
    finally:
        session.release.set()
        session.ended.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        os.close(r)
        writer.join(5)
        end_seen.set()  # lets the executor thread of a timed out wait finish


async def test_stderr_is_merged_without_a_terminal_unless_scripted(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    profile = ssh_server.profile(remote_command="err", pty=False)
    session = await open_session(profile, FakePrompter())  # interactive / TUI / web paths
    try:
        out = await read_until(session, b"to-stderr")
        assert b"to-stdout" in out and b"to-stderr" in out
        assert await session.read_stderr() == b""
    finally:
        await session.close()
    session = await open_session(profile, FakePrompter(), raw=True)
    try:
        assert b"to-stderr" in await asyncio.wait_for(session.read_stderr(), 5)
        assert b"to-stderr" not in await read_until(session, b"to-stdout")
    finally:
        await session.close()


@pytest.mark.parametrize("argv, pty", [
    (["web"], True), (["web", "uptime"], False), (["web", "-s", "netconf"], False),
    (["web", "-t", "uptime"], True), (["web", "-T"], False),
])
def test_command_or_subsystem_from_a_terminal_gets_no_pty_unless_asked(monkeypatch, argv, pty):
    class Terminal:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    store = ProfileStore()
    store.save(Profile("web", "ssh", ssh=cli.SSHSettings(host="h")))
    assert cli._connect_profile(_parse(argv), store).ssh.pty is pty


async def test_remote_program_killed_by_a_signal(ssh_server, password_file):
    code, out, err = await run_cli(ssh_server, "--password-file", str(password_file), None, "kill")
    assert code == 128 + 9
    assert "[termdeck] Session ended" in err


async def test_plain_nonzero_exit_prints_no_end_line_in_pipe_mode(ssh_server, password_file):
    code, out, err = await run_cli(ssh_server, "--password-file", str(password_file), None, "exit3")
    assert code == 3 and "Session ended" not in err


def _main(capsys, *argv):
    code = cli.main(["connect", *argv])
    return code, capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--password-file", "-m"])
def test_utf16_files_are_refused(capsys, tmp_path, flag):
    path = tmp_path / "f"
    path.write_bytes("secret\n".encode("utf-16"))
    code, err = _main(capsys, flag, str(path), "alice@127.0.0.1:1")
    assert code == 2 and "not UTF-8" in err


def test_empty_and_missing_password_files_are_refused(capsys, tmp_path):
    path = tmp_path / "empty"
    path.write_bytes(b"")
    code, err = _main(capsys, "--password-file", str(path), "alice@127.0.0.1:1")
    assert code == 2 and "is empty" in err
    path.write_bytes(b"\xef\xbb\xbf\n")  # a BOM and nothing else
    assert _main(capsys, "--password-file", str(path), "alice@127.0.0.1:1")[0] == 2
    code, err = _main(capsys, "--password-file", str(tmp_path / "missing"), "alice@127.0.0.1:1")
    assert code == 2 and "cannot read" in err


async def test_bom_is_stripped_from_password_and_command_files(ssh_server, tmp_path):
    pw = tmp_path / "pw"
    pw.write_bytes(b"\xef\xbb\xbf" + PASSWORD.encode() + b"\r\n")
    script = tmp_path / "cmd"
    script.write_bytes(b"\xef\xbb\xbffrom-file")
    code, out, err = await run_cli(ssh_server, "--batch", "--password-file", str(pw), "-m", str(script))
    assert code == 0 and b"ran from-file" in out


async def test_nc_cannot_be_combined_with_a_command_or_forwards(ssh_server):
    for extra in (["-N"], ["-L", "8080:db:5432"]):
        code, out, err = await run_cli(ssh_server, "--nc", "127.0.0.1:9", *extra)
        assert code == 2 and "--nc replaces the shell" in err
    code, out, err = await run_cli(ssh_server, "--nc", "127.0.0.1:9", None, "uptime")
    assert code == 2 and "--nc replaces the shell" in err
