import pytest

from termdeck.profiles import Forward, Profile, ProfileStore, SerialSettings, SSHSettings, parse_forward
from termdeck.session import open_session

from .conftest import FakePrompter, read_until


@pytest.mark.parametrize(
    "kind, spec, expected",
    [
        ("local", "8080:db:5432", Forward("local", 8080, "localhost", "db", 5432)),
        ("local", "0.0.0.0:8080:db:5432", Forward("local", 8080, "0.0.0.0", "db", 5432)),
        ("remote", "[::1]:9000:[fe80::1]:22", Forward("remote", 9000, "::1", "fe80::1", 22)),
        ("dynamic", "1080", Forward("dynamic", 1080)),
    ],
)
def test_parse_forward(kind, spec, expected):
    assert parse_forward(kind, spec) == expected


@pytest.mark.parametrize("kind, spec", [("local", "8080"), ("dynamic", "1080:h:1"), ("local", "x:y:z")])
def test_parse_forward_rejects(kind, spec):
    with pytest.raises(ValueError):
        parse_forward(kind, spec)


def test_store_roundtrip_rename_delete(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    ssh = Profile("web", "ssh", ssh=SSHSettings(host="h", forwards=[Forward("dynamic", 1080)]))
    ser = Profile("board", "serial", serial=SerialSettings(device="/dev/ttyUSB0", parity="E"))
    store.save(ssh)
    store.save(ser)
    assert store.load() == {"web": ssh, "board": ser}

    ssh.name = "web2"
    store.save(ssh, replace="web")
    assert sorted(store.load()) == ["board", "web2"]
    store.delete("board")
    with pytest.raises(KeyError):
        store.get("board")


@pytest.mark.parametrize(
    "profile",
    [
        Profile("", "ssh", ssh=SSHSettings(host="h")),
        Profile("x", "ssh"),
        Profile("x", "ssh", ssh=SSHSettings(host="h", port=0)),
        Profile("x", "serial"),
        Profile("x", "serial", serial=SerialSettings(device="d", parity="Q")),
        Profile("x", "telnet"),
    ],
)
def test_invalid_profiles(profile):
    with pytest.raises(ValueError):
        profile.validate()


async def test_serial_loopback():
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://", baudrate=9600))
    session = await open_session(profile, FakePrompter())
    try:
        await session.write(b"AT\r")
        assert await read_until(session, b"AT\r") == b"AT\r"
    finally:
        await session.close()
    assert await session.read() == b""


async def _loop_session(**terminal):
    profile = Profile("loop", "serial", serial=SerialSettings(device="loop://"))
    for key, value in terminal.items():
        setattr(profile.terminal, key, value)
    return await open_session(profile, FakePrompter())


async def test_terminal_enter_and_backspace_mapping():
    session = await _loop_session(enter_sends="crlf", backspace_sends="bs")
    try:
        await session.write(b"a\x7f\r")
        assert await read_until(session, b"\n") == b"a\x08\r\n"
    finally:
        await session.close()


async def test_terminal_charset_conversion():
    session = await _loop_session(encoding="latin-1")
    try:
        await session.write("é".encode())
        # The loopback returns the single latin-1 byte, which comes back as UTF-8.
        assert await read_until(session, "é".encode()) == "é".encode()
        assert session.inner._port.out_waiting == 0
    finally:
        await session.close()


async def test_terminal_local_echo_and_log(tmp_path):
    session = await _loop_session(local_echo=True, log_file=str(tmp_path / "logs" / "{name}.log"))
    try:
        await session.write(b"hi\r")
        # Local echo comes first, then the loopback.
        assert await read_until(session, b"\nhi\r") == b"hi\r\nhi\r"
    finally:
        await session.close()
    assert (tmp_path / "logs" / "loop.log").read_bytes() == b"hi\r"


def test_profile_ignores_unknown_keys_and_fills_defaults():
    p = Profile.from_dict({"name": "x", "kind": "ssh", "ssh": {"host": "h", "future": 1}})
    assert p.ssh.connect_timeout == 15 and p.terminal.theme == "dark"


def test_describe_options_is_json_and_marks_advanced():
    import json

    from termdeck.profiles import describe_options

    options = json.loads(json.dumps(describe_options()))
    ssh = {o["key"]: o for o in options["ssh"]}
    assert ssh["host"]["advanced"] is False
    assert ssh["jump_host"]["advanced"] is True
    assert ssh["forwards"]["type"] == "forwards"
    parity = {o["key"]: o for o in options["serial"]}["parity"]
    assert ["E", "Even"] in parity["choices"]


def test_invalid_charset():
    p = Profile("x", "serial", serial=SerialSettings(device="d"))
    p.terminal.encoding = "nope"
    with pytest.raises(ValueError):
        p.validate()


def test_options_declare_their_connection_kinds():
    from termdeck.profiles import describe_options

    terminal = {o["key"]: o for o in describe_options()["terminal"]}
    assert terminal["term_type"]["kinds"] == ["ssh"]
    assert terminal["local_echo"]["kinds"] == ["ssh", "serial"]


@pytest.mark.parametrize("text, kind, attrs", [
    ("bob@host:2222", "ssh", {"username": "bob", "host": "host", "port": 2222}),
    ("host", "ssh", {"username": "", "host": "host", "port": 22}),
    ("[::1]:2200", "ssh", {"host": "::1", "port": 2200}),
    ("COM3@9600", "serial", {"device": "COM3", "baudrate": 9600}),
    ("/dev/ttyUSB0", "serial", {"device": "/dev/ttyUSB0", "baudrate": 115200}),
    ("rfc2217://srv:7000", "serial", {"device": "rfc2217://srv:7000"}),
    ("loop://", "serial", {"device": "loop://"}),
])
def test_parse_target(text, kind, attrs):
    from termdeck.profiles import parse_target

    p = parse_target(text)
    assert p.kind == kind
    section = getattr(p, kind)
    assert {k: getattr(section, k) for k in attrs} == attrs


def test_parse_target_rejects_garbage():
    from termdeck.profiles import parse_target

    assert parse_target("not a target") is None


def test_files_are_private(tmp_path):
    import os
    import stat as st

    from termdeck.ssh import known_hosts_path, remember_host_key
    import asyncssh

    store = ProfileStore(tmp_path / "cfg" / "p.json")
    store.save(Profile("x", "serial", serial=SerialSettings(device="d")))
    remember_host_key("h", 22, asyncssh.generate_private_key("ssh-ed25519"))
    for path in (store.path, known_hosts_path()):
        assert st.S_IMODE(os.stat(path).st_mode) == 0o600, path


def test_invalid_entry_is_skipped_but_kept(tmp_path):
    import json

    path = tmp_path / "p.json"
    path.write_text(json.dumps({"profiles": [
        {"name": "good", "kind": "serial", "serial": {"device": "d"}},
        {"name": "bad", "kind": "serial", "serial": {"device": "d", "parity": "Q"}},
    ]}))
    store = ProfileStore(path)
    assert list(store.load()) == ["good"]
    assert store.invalid[0][0] == "bad"
    store.save(Profile("new", "serial", serial=SerialSettings(device="d")))
    names = [p["name"] for p in json.loads(path.read_text())["profiles"]]
    assert sorted(names) == ["bad", "good", "new"]  # the broken entry was not thrown away


@pytest.mark.parametrize("host", ["a b", "a,b", "*", "h\nx", "|1|abc|def", "@cert-authority", "[h]:22"])
def test_hosts_that_would_corrupt_known_hosts_are_rejected(host):
    with pytest.raises(ValueError):
        Profile("x", "ssh", ssh=SSHSettings(host=host)).validate()


async def test_log_name_cannot_escape_directory(tmp_path):
    profile = Profile("../../evil", "serial", serial=SerialSettings(device="loop://"))
    profile.terminal.log_file = str(tmp_path / "logs" / "{name}.log")
    session = await open_session(profile, FakePrompter())
    await session.close()
    assert [p.name for p in (tmp_path / "logs").iterdir()] == ["_.._evil.log"]


def test_public_forward_flag():
    assert not Forward("local", 1, "localhost", "h", 2).public
    assert Forward("local", 1, "0.0.0.0", "h", 2).public


def test_existing_loose_files_are_tightened(tmp_path):
    import os
    import stat as st

    import asyncssh

    from termdeck.ssh import known_hosts_path, remember_host_key

    path = known_hosts_path()
    path.parent.mkdir(parents=True)
    os.chmod(path.parent, 0o755)
    path.write_text("")
    os.chmod(path, 0o644)
    remember_host_key("h", 22, asyncssh.generate_private_key("ssh-ed25519"))
    assert st.S_IMODE(os.stat(path).st_mode) == 0o600
    assert st.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_mistyped_profile_does_not_break_loading(tmp_path):
    import json

    path = tmp_path / "p.json"
    path.write_text(json.dumps({"profiles": [{"name": 5, "kind": "ssh"},
                                             {"name": "ok", "kind": "serial", "serial": {"device": "d"}}]}))
    store = ProfileStore(path)
    assert list(store.load()) == ["ok"] and len(store.invalid) == 1
