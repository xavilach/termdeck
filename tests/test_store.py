"""Profile store, validation, option metadata, config helpers and log paths."""

import asyncio
import json
import os
import stat
import sys
import threading
import time
from datetime import datetime

import asyncssh
import pytest

from termdeck.config import atomic_write, open_private, user_path
from termdeck.profiles import (
    FORMAT_VERSION, Profile, ProfileFileError, ProfileStore, SerialSettings, SSHSettings, TerminalSettings,
    describe_options, normalize_device, parse_jump, parse_target,
)
from termdeck.session import Session, TerminalSession, log_path, open_session
from termdeck.ssh import jump_profile, known_hosts_path, remember_host_key

from .conftest import FakePrompter

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")


def ssh_profile(name="web", host="h", **ssh):
    return Profile(name, "ssh", ssh=SSHSettings(host=host, **ssh))


def serial_profile(name="board", device="/dev/ttyUSB0", **serial):
    return Profile(name, "serial", serial=SerialSettings(device=device, **serial))


def write_file(path, entries, **extra):
    path.write_text(json.dumps({"profiles": entries, **extra}))
    return ProfileStore(path)


GOOD = {"name": "good", "kind": "serial", "serial": {"device": "d"}}
BAD = {"name": "bad", "kind": "serial", "serial": {"device": "d", "parity": "Q"}}


# ---- P1-10 log folders are not ours to change ----

@posix_only
@pytest.mark.parametrize("mode", [0o755, 0o2775])
def test_open_private_leaves_existing_folder_mode(tmp_path, mode):
    folder = tmp_path / "logs"
    folder.mkdir()
    os.chmod(folder, mode)
    before = stat.S_IMODE(os.stat(folder).st_mode)
    with open_private(folder / "a.log", "ab", own_dir=False) as f:
        f.write(b"x")
    assert stat.S_IMODE(os.stat(folder).st_mode) == before
    assert stat.S_IMODE(os.stat(folder / "a.log").st_mode) == 0o600


def test_open_private_creates_missing_parent(tmp_path):
    with open_private(tmp_path / "a" / "b" / "x.log", "ab", own_dir=False) as f:
        f.write(b"x")
    assert (tmp_path / "a" / "b" / "x.log").read_bytes() == b"x"


@posix_only
async def test_session_log_keeps_folder_mode(tmp_path):
    folder = tmp_path / "logs"
    folder.mkdir()
    os.chmod(folder, 0o755)
    profile = serial_profile("loop", "loop://")
    profile.terminal.log_file = str(folder / "{name}.log")
    session = await open_session(profile, FakePrompter())
    await session.close()
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o755
    assert stat.S_IMODE(os.stat(folder / "loop.log").st_mode) == 0o600


# ---- P1-11 log file names ----

def test_log_path_replaces_known_placeholders(tmp_path):
    p = ssh_profile("web", "example.com")
    now = datetime(2025, 1, 2, 3, 4, 5)
    path = log_path(str(tmp_path / "{name}-{host}-{date}.log"), p, now)
    assert path == tmp_path / "web-example.com-20250102-030405.log"


def test_log_path_keeps_unknown_and_lone_braces(tmp_path):
    p = ssh_profile("web")
    assert log_path(str(tmp_path / "{x}-{name}.log"), p).name == "{x}-web.log"
    assert log_path(str(tmp_path / "a{b.log"), p).name == "a{b.log"


@pytest.mark.parametrize("name, expected", [("con", "_con"), ("NUL", "_NUL"), ("com1", "_com1"), ("foo...", "foo")])
def test_log_path_windows_names(tmp_path, name, expected):
    assert log_path(str(tmp_path / "{name}"), ssh_profile(name)).name == expected


async def test_open_session_with_unknown_placeholder_in_log_file(tmp_path):
    profile = serial_profile("loop", "loop://")
    profile.terminal.log_file = str(tmp_path / "logs" / "{x}.log")
    session = await open_session(profile, FakePrompter())
    await session.close()
    assert [p.name for p in (tmp_path / "logs").iterdir()] == ["{x}.log"]


# ---- P1-12 warnings ----

def test_warnings_describe_invalid_entries(tmp_path):
    store = write_file(tmp_path / "p.json", [GOOD, BAD])
    assert list(store.load()) == ["good"]
    warnings = store.warnings()
    assert len(warnings) == 1 and "bad" in warnings[0]


def test_get_invalid_profile_says_so(tmp_path):
    store = write_file(tmp_path / "p.json", [GOOD, BAD])
    with pytest.raises(KeyError, match="is invalid"):
        store.get("bad")
    with pytest.raises(KeyError, match="no profile named"):
        store.get("nothing")


# ---- P1-13 / P1-23 corrupt and future files ----

def test_corrupt_file_raises_with_path_and_line(tmp_path):
    path = tmp_path / "p.json"
    path.write_text('{\n  "profiles": [\n')
    with pytest.raises(ProfileFileError) as exc:
        ProfileStore(path).load()
    assert isinstance(exc.value, ValueError)
    assert str(path) in str(exc.value) and "line 3" in str(exc.value)


@pytest.mark.parametrize("content", ["[]", '{"profiles": 3}', "\xff".encode("latin-1")])
def test_wrong_shape_or_encoding_is_refused(tmp_path, content):
    path = tmp_path / "p.json"
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    with pytest.raises(ProfileFileError):
        ProfileStore(path).load()


def test_saving_does_not_overwrite_corrupt_file(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("{not json")
    store = ProfileStore(path)
    for action in (lambda: store.save(ssh_profile()), lambda: store.add(ssh_profile()),
                   lambda: store.delete("web")):
        with pytest.raises(ValueError):
            action()
    assert path.read_text() == "{not json"


def test_saved_file_has_version(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.save(ssh_profile())
    assert json.loads(store.path.read_text())["version"] == FORMAT_VERSION == 1


def test_newer_file_version_is_refused_and_untouched(tmp_path):
    store = write_file(tmp_path / "p.json", [GOOD], version=2)
    before = store.path.read_text()
    with pytest.raises(ProfileFileError, match="newer"):
        store.load()
    with pytest.raises(ProfileFileError):
        store.save(ssh_profile())
    assert store.path.read_text() == before


# ---- P1-14 duplicate names ----

def test_duplicate_name_is_kept_in_file_and_reported(tmp_path):
    first = {"name": "dup", "kind": "ssh", "ssh": {"host": "first"}}
    second = {"name": "dup", "kind": "ssh", "ssh": {"host": "second"}}
    store = write_file(tmp_path / "p.json", [first, second])
    assert store.load()["dup"].ssh.host == "first"
    assert any("dup" in w for w in store.warnings())
    store.save(ssh_profile("other"))
    entries = json.loads(store.path.read_text())["profiles"]
    assert sorted(e["ssh"]["host"] for e in entries) == ["first", "h", "second"]
    store.load()
    assert any("dup" in w for w in store.warnings())


# ---- P1-15 add, update, concurrency ----

def test_add_refuses_existing_name(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.add(ssh_profile("a", "one"))
    with pytest.raises(ValueError, match="already exists"):
        store.add(ssh_profile("a", "two"))
    assert store.load()["a"].ssh.host == "one"


def test_add_refuses_name_of_a_skipped_invalid_entry(tmp_path):
    store = write_file(tmp_path / "p.json", [BAD])
    with pytest.raises(ValueError, match="already exists"):
        store.add(serial_profile("bad"))


def test_update_errors(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.add(ssh_profile("a"))
    store.add(ssh_profile("b"))
    with pytest.raises(KeyError):
        store.update("gone", ssh_profile("gone"))
    with pytest.raises(ValueError, match="already exists"):
        store.update("a", ssh_profile("b"))
    renamed = ssh_profile("c")
    store.update("a", renamed)
    assert sorted(store.load()) == ["b", "c"]


def test_concurrent_adds_lose_nothing(tmp_path):
    path = tmp_path / "p.json"
    errors = []

    def worker(tag):
        store = ProfileStore(path)  # one store per thread, like separate windows
        try:
            for i in range(20):
                store.add(ssh_profile(f"{tag}{i}"))
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    assert len(ProfileStore(path).load()) == 40


# ---- P1-16 atomic writes ----

def test_atomic_write_replaces_contents(tmp_path):
    path = tmp_path / "dir" / "f.txt"
    path.parent.mkdir()
    path.write_text("old old old")
    atomic_write(path, "new\n")
    assert path.read_text() == "new\n"
    assert [p.name for p in path.parent.iterdir()] == ["f.txt"]  # no temporary file left


def test_known_hosts_keeps_lf_and_replaces_old_key(tmp_path):
    old = asyncssh.generate_private_key("ssh-ed25519")
    new = asyncssh.generate_private_key("ssh-ed25519")
    remember_host_key("h", 22, old)
    remember_host_key("other", 22, old)
    remember_host_key("h", 22, new)
    raw = known_hosts_path().read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    lines = raw.decode().splitlines()
    assert len(lines) == 2
    h_line = next(line for line in lines if line.startswith("h "))
    assert new.export_public_key("openssh").decode().split()[1] in h_line
    assert old.export_public_key("openssh").decode().split()[1] not in h_line


# ---- P1-17 / P1-21 limits and metadata ----

@pytest.mark.parametrize("make", [
    lambda: serial_profile(baudrate=0),
    lambda: serial_profile(baudrate=-9600),
    lambda: ssh_profile(port=70000),
    lambda: ssh_profile(port=0),
    lambda: _with_terminal(ssh_profile(), font_size=0),
    lambda: _with_terminal(ssh_profile(), scrollback=2_000_000),
    lambda: ssh_profile("a\nb"),
    lambda: ssh_profile("a\tb"),
    lambda: Profile("ok", "ssh", group="g\n", ssh=SSHSettings(host="h")),
    lambda: Profile("ok", "ssh", group="g\tx", ssh=SSHSettings(host="h")),
])
def test_out_of_range_and_control_characters_rejected(make):
    with pytest.raises(ValueError):
        make().validate()


def _with_terminal(profile, **kw):
    for k, v in kw.items():
        setattr(profile.terminal, k, v)
    return profile


def test_describe_options_exposes_limits_and_width():
    opts = {s: {o["key"]: o for o in options} for s, options in describe_options().items()}
    assert (opts["serial"]["baudrate"]["min"], opts["serial"]["baudrate"]["max"]) == (1, 100_000_000)
    assert opts["ssh"]["port"]["max"] == 65535
    assert opts["terminal"]["scrollback"]["max"] == 1_000_000
    assert opts["terminal"]["font_size"]["min"] > 0
    assert opts["ssh"]["host"]["wide"] is True and opts["ssh"]["port"]["wide"] is False
    assert opts["ssh"]["env"]["wide"] is True  # multiline fields are wide too


def test_option_kinds():
    opts = describe_options()
    assert all(o["kinds"] == ["ssh"] for o in opts["ssh"])
    assert all(o["kinds"] == ["serial"] for o in opts["serial"])
    term = {o["key"]: o for o in opts["terminal"]}
    assert term["term_type"]["kinds"] == ["ssh"]
    assert term["theme"]["kinds"] == ["ssh", "serial"]


# ---- P1-18 / P1-19 / P1-20 / P1-63 SSH validation ----

@pytest.mark.parametrize("text, expected", [
    ("bob@[::1]:2200", ("bob", "::1", 2200)),
    ("[::1]", ("", "::1", 22)),
    ("fe80::1", ("", "fe80::1", 22)),  # a bare IPv6 address takes the default port
    ("h:22", ("", "h", 22)),
    ("h", ("", "h", 22)),
    ("bob@h:2222", ("bob", "h", 2222)),
])
def test_parse_jump(text, expected):
    assert parse_jump(text) == expected


@pytest.mark.parametrize("text", ["h:notaport", "h:1 evil\n* x:y", "bastion:1 evil", "[::1", "h:0", "h:65536",
                                  "", "[::1]x"])
def test_parse_jump_rejects(text):
    with pytest.raises(ValueError):
        parse_jump(text)


def test_profile_rejects_bad_jump_host():
    with pytest.raises(ValueError):
        ssh_profile(jump_host="bastion:1 evil").validate()
    ssh_profile(jump_host="bob@bastion:2222").validate()


def test_jump_profile_drops_forwarding_and_commands():
    from termdeck.profiles import Forward

    p = ssh_profile(jump_host="bob@bastion", agent_forwarding=True, x11_forwarding=True, env="A=1",
                    remote_command="top", forwards=[Forward("dynamic", 1080)], key_file="/k", compression=True)
    jump = jump_profile(p).ssh
    assert (jump.host, jump.username) == ("bastion", "bob")
    assert (jump.agent_forwarding, jump.x11_forwarding, jump.env, jump.remote_command, jump.forwards) == \
        (False, False, "", "", [])
    assert jump.key_file == "/k" and jump.compression is True  # connection options are inherited


@pytest.mark.parametrize("env", ["NOEQUALS", "=value", "A=1\nB"])
def test_invalid_env_rejected(env):
    with pytest.raises(ValueError):
        ssh_profile(env=env).validate()


def test_valid_env_accepted():
    ssh_profile(env="A=1\n\nB=two words").validate()


def test_agent_forwarding_needs_agent():
    with pytest.raises(ValueError, match="agent"):
        ssh_profile(agent_forwarding=True, use_agent=False).validate()
    ssh_profile(agent_forwarding=True, use_agent=True).validate()


# ---- P1-24 / P1-25 serial devices ----

@pytest.mark.parametrize("given, expected", [("com3", "COM3"), ("\\\\.\\COM10", "COM10"), (" Com7 ", "COM7"),
                                             ("/dev/ttyUSB0", "/dev/ttyUSB0")])
def test_device_is_normalised_on_validate(given, expected):
    p = serial_profile(device=given)
    p.validate()
    assert p.serial.device == expected


def test_parse_target_normalises_device():
    p = parse_target("\\\\.\\COM10@9600")
    assert (p.kind, p.serial.device, p.serial.baudrate) == ("serial", "COM10", 9600)
    assert parse_target("com3").serial.device == "COM3"


@pytest.mark.parametrize("device", ["spy://COM3", "hwgrep://x", "SPY://COM3", "alt://COM3"])
def test_dangerous_device_urls_rejected(device):
    with pytest.raises(ValueError, match="unsupported"):
        serial_profile(device=device).validate()


@pytest.mark.parametrize("device", ["rfc2217://h:1", "socket://h:1", "loop://"])
def test_network_and_loop_devices_accepted(device):
    serial_profile(device=device).validate()


# ---- P1-171 pasted paths ----

@pytest.mark.parametrize("quote", ['"', "'"])
def test_user_path_strips_quotes(tmp_path, quote):
    assert user_path(f'  {quote}{tmp_path}/a b.log{quote} ') == tmp_path / "a b.log"


def test_user_path_expands_home_and_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("TD_TEST_DIR", str(tmp_path / "x"))
    assert user_path("$TD_TEST_DIR/f.log") == tmp_path / "x" / "f.log"
    assert user_path('"~/f.log"') == tmp_path / "home" / "f.log"


@pytest.mark.skipif(sys.platform != "win32", reason="%VAR% syntax")
def test_user_path_expands_percent_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("TD_TEST_DIR", str(tmp_path))
    assert user_path("%TD_TEST_DIR%\\f.log") == tmp_path / "f.log"


# ---- batch 2 review fixes ----

def test_concurrent_host_key_saves_keep_every_host():
    keys = {f"host{i}": asyncssh.generate_private_key("ssh-ed25519") for i in range(8)}
    for _ in range(3):
        errors = []

        def worker(host):
            try:
                remember_host_key(host, 22, keys[host])
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(h,)) for h in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors
        lines = known_hosts_path().read_text().splitlines()
        assert sorted(line.split(" ", 1)[0] for line in lines) == sorted(keys)


def test_updates_while_loading_keep_invalid_entries(tmp_path):
    bad = [{"name": f"bad{i}", "kind": "serial", "serial": {"device": "d", "parity": "Q"}} for i in range(20)]
    path = tmp_path / "p.json"
    store = write_file(path, [{"name": "ok", "kind": "serial", "serial": {"device": "d"}}, *bad])
    errors = []

    def writer():
        try:
            for i in range(30):
                store.update("ok", serial_profile("ok", f"d{i}"))
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    reader = ProfileStore(path)
    while thread.is_alive():
        try:
            reader.load()
        except ProfileFileError as exc:
            errors.append(exc)
            break
    thread.join(30)
    assert not errors
    names = [e["name"] for e in json.loads(path.read_text())["profiles"]]
    assert sorted(names) == sorted(["ok", *(f"bad{i}" for i in range(20))])


def test_delete_removes_valid_entry_and_skipped_duplicate(tmp_path):
    first = {"name": "x", "kind": "ssh", "ssh": {"host": "one"}}
    second = {"name": "x", "kind": "ssh", "ssh": {"host": "two"}}
    store = write_file(tmp_path / "p.json", [first, second, GOOD])
    store.delete("x")
    assert list(store.load()) == ["good"]
    assert store.invalid == []
    assert [e["name"] for e in json.loads(store.path.read_text())["profiles"]] == ["good"]
    with pytest.raises(KeyError):
        store.delete("x")


def test_delete_removes_an_invalid_entry(tmp_path):
    store = write_file(tmp_path / "p.json", [GOOD, BAD])
    store.delete("bad")
    store.load()
    assert store.invalid == []


def test_invalid_entry_can_be_repaired_through_update(tmp_path):
    store = write_file(tmp_path / "p.json", [BAD])
    fixed = store.get_raw("bad")
    fixed.serial.parity = "N"
    store.update("bad", fixed)
    assert list(store.load()) == ["bad"] and store.invalid == []


@posix_only
def test_open_private_refuses_symlink(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("precious")
    link = tmp_path / "log"
    link.symlink_to(target)
    with pytest.raises(OSError):  # ELOOP from O_NOFOLLOW (not PermissionError)
        open_private(link, "ab", own_dir=False)
    assert target.read_text() == "precious"


@posix_only
def test_open_private_keeps_mode_of_existing_log(tmp_path):
    log = tmp_path / "a.log"
    log.write_bytes(b"old")
    os.chmod(log, 0o644)
    with open_private(log, "ab", own_dir=False) as f:
        f.write(b"new")
    assert log.read_bytes() == b"oldnew"
    assert stat.S_IMODE(os.stat(log).st_mode) == 0o644


@posix_only
def test_open_private_refuses_hard_link(tmp_path):
    log = tmp_path / "a.log"
    log.write_bytes(b"data")
    os.link(log, tmp_path / "other")
    assert os.stat(log).st_nlink == 2
    with pytest.raises(PermissionError):
        open_private(log, "ab", own_dir=False)
    assert log.read_bytes() == b"data"


@posix_only
def test_open_private_new_file_is_private(tmp_path):
    old = os.umask(0)
    try:
        with open_private(tmp_path / "new.log", "ab", own_dir=False) as f:
            f.write(b"x")
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(tmp_path / "new.log").st_mode) == 0o600


class _FakeInner(Session):
    """A raw session fed from a queue; b"" ends it."""

    def __init__(self):
        self.title = "fake"
        self.incoming: asyncio.Queue[bytes] = asyncio.Queue()
        self.closed = False

    async def read(self):
        return await self.incoming.get()

    async def write(self, data):
        pass

    async def close(self):
        self.closed = True
        self.incoming.put_nowait(b"")


async def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.02)


async def test_log_reaches_disk_while_session_is_idle(tmp_path):
    log = tmp_path / "s.log"
    inner = _FakeInner()
    session = TerminalSession(inner, TerminalSettings(), log)
    try:
        inner.incoming.put_nowait("héllo 世界\r\n".encode())
        assert (await session.read()).startswith("héllo".encode())
        await _wait_for(lambda: log.stat().st_size > 0, 3.0)  # flushed by the timer, session still open
        assert log.read_bytes() == "héllo 世界\r\n".encode()
        assert not inner.closed
    finally:
        await session.close()


async def test_log_is_flushed_when_the_remote_ends(tmp_path):
    log = tmp_path / "s.log"
    inner = _FakeInner()
    session = TerminalSession(inner, TerminalSettings(), log)
    try:
        inner.incoming.put_nowait(b"last words")
        inner.incoming.put_nowait(b"")
        assert await session.read() == b"last words"
        assert await session.read() == b""
        # The end was seen, but close() has not been called yet.
        assert log.read_bytes() == b"last words"
    finally:
        await session.close()


def test_com0com_style_device_keeps_its_prefix():
    assert normalize_device("\\\\.\\CNCA0") == "\\\\.\\CNCA0"
    assert normalize_device("\\\\.\\com12") == "COM12"
    p = serial_profile(device="\\\\.\\CNCA0")
    p.validate()
    assert p.serial.device == "\\\\.\\CNCA0"


def test_cp2110_device_is_accepted():
    serial_profile(device="cp2110://0").validate()
