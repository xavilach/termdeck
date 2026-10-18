"""sftp.SFTP against the local server (chrooted at ssh_server.root), and Session.sftp()."""

import asyncio
import re

import asyncssh
import pytest

from termdeck.errors import SFTPFailure
from termdeck.session import open_session
from termdeck.sftp import SFTP, join, parse_mode
from termdeck.ssh import SSHConnection, remember_host_key

from .conftest import FakePrompter


@pytest.fixture
async def files(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    conn = await SSHConnection.connect(ssh_server.profile(), FakePrompter())
    yield conn.sftp()
    conn.close()


async def chunks(*parts: bytes):
    for part in parts:
        yield part


async def test_list_is_sorted_folders_first_with_entry_fields(files, ssh_server):
    root = ssh_server.root
    (root / "b.txt").write_bytes(b"12345")
    (root / "A.txt").write_bytes(b"")
    (root / "zdir").mkdir()
    (root / "Adir").mkdir()
    path, entries = await files.list("/")
    assert path == "/"
    assert [e.name for e in entries] == ["Adir", "zdir", "A.txt", "b.txt"]
    adir, b = entries[0], entries[3]
    assert adir.is_dir and not adir.is_link and adir.path == "/Adir" and adir.mode.startswith("d")
    assert not b.is_dir and b.size == 5 and b.path == "/b.txt" and b.mode.startswith("-") and b.mtime > 0


async def test_write_refuses_an_existing_file_unless_overwrite(files, ssh_server):
    assert await files.write("/f", chunks(b"one", b"two")) == 6
    with pytest.raises(FileExistsError):
        await files.write("/f", chunks(b"new"))
    assert (ssh_server.root / "f").read_bytes() == b"onetwo"
    assert await files.write("/f", chunks(b"new"), overwrite=True) == 3
    assert (ssh_server.root / "f").read_bytes() == b"new"


async def test_get_refuses_an_existing_target_unless_overwrite(files, ssh_server, tmp_path):
    (ssh_server.root / "r.txt").write_text("remote")
    target = tmp_path / "t.txt"
    target.write_text("local")
    with pytest.raises(FileExistsError):
        await files.get("/r.txt", target)
    assert target.read_text() == "local"
    await files.get("/r.txt", target, overwrite=True)
    assert target.read_text() == "remote"


async def test_get_into_an_existing_local_folder(files, ssh_server, tmp_path):
    (ssh_server.root / "r.txt").write_text("remote")
    folder = tmp_path / "dest"
    folder.mkdir()
    await files.get("/r.txt", folder)
    assert (folder / "r.txt").read_text() == "remote"
    with pytest.raises(FileExistsError):  # the file inside is what would be replaced
        await files.get("/r.txt", folder)


async def test_put_refuses_an_existing_target_unless_overwrite(files, ssh_server, tmp_path):
    local = tmp_path / "l.txt"
    local.write_text("new")
    (ssh_server.root / "l.txt").write_text("old")
    with pytest.raises(FileExistsError):
        await files.put(local, "/l.txt")
    assert (ssh_server.root / "l.txt").read_text() == "old"
    await files.put(local, "/l.txt", overwrite=True)
    assert (ssh_server.root / "l.txt").read_text() == "new"


async def test_put_into_an_existing_remote_folder(files, ssh_server, tmp_path):
    (ssh_server.root / "up").mkdir()
    local = tmp_path / "l.txt"
    local.write_text("x")
    await files.put(local, "/up")
    assert (ssh_server.root / "up" / "l.txt").read_text() == "x"


async def test_put_of_a_missing_local_file_names_it(files, tmp_path):
    with pytest.raises(SFTPFailure, match="nothing.txt"):
        await files.put(tmp_path / "nothing.txt", "/x")


async def test_remove_recursive_deletes_a_non_empty_folder(files, ssh_server):
    (ssh_server.root / "d" / "sub").mkdir(parents=True)
    (ssh_server.root / "d" / "sub" / "f").write_text("x")
    with pytest.raises(SFTPFailure, match="/d"):
        await files.remove("/d")  # not recursive: a folder is not removed as a file
    assert (ssh_server.root / "d").exists()
    await files.remove("/d", recursive=True)
    assert not (ssh_server.root / "d").exists()


async def test_rename_refuses_an_existing_target(files, ssh_server):
    (ssh_server.root / "a").write_text("a")
    (ssh_server.root / "b").write_text("b")
    with pytest.raises(FileExistsError):
        await files.rename("/a", "/b")
    assert (ssh_server.root / "b").read_text() == "b"
    await files.rename("/a", "/c")
    assert (ssh_server.root / "c").read_text() == "a" and not (ssh_server.root / "a").exists()


async def test_rename_with_overwrite_replaces_the_target(files, ssh_server):
    (ssh_server.root / "a").write_text("a")
    (ssh_server.root / "b").write_text("b")
    await files.rename("/a", "/b", overwrite=True)
    assert (ssh_server.root / "b").read_text() == "a" and not (ssh_server.root / "a").exists()


async def test_chmod_sets_the_mode(files, ssh_server):
    path = ssh_server.root / "m"
    path.write_text("x")
    await files.chmod("/m", 0o600)
    assert path.stat().st_mode & 0o777 == 0o600
    await files.chmod("/m", 0o644)
    assert path.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("call", [
    lambda f: f.list("/missing"),
    lambda f: f.mkdir("/missing/deeper"),
    lambda f: f.rmdir("/missing"),
    lambda f: f.remove("/missing"),
    lambda f: f.chmod("/missing", 0o644),
    lambda f: f.open_read("/missing"),
])
async def test_errors_are_sftp_failures_naming_the_path(files, call):
    with pytest.raises(SFTPFailure, match="/missing"):
        await call(files)


async def test_open_read_of_a_folder_is_refused(files, ssh_server):
    (ssh_server.root / "d").mkdir()
    with pytest.raises(SFTPFailure, match="/d is a folder"):
        await files.open_read("/d")


@pytest.mark.parametrize("name", ["", ".", "..", "a/..", "dir/", "x\\.."])
def test_join_rejects_names_that_are_not_a_file(name):
    with pytest.raises(SFTPFailure):
        join("/up", name)


@pytest.mark.parametrize("name, expected", [
    ("x", "/up/x"), ("../x", "/up/x"), ("/etc/passwd", "/up/passwd"), ("..\\..\\x", "/up/x"),
    ("sub/dir/ü 世界.txt", "/up/ü 世界.txt"),
])
def test_join_strips_directories(name, expected):
    assert join("/up", name) == expected


async def test_two_concurrent_first_requests_open_one_channel(ssh_server, monkeypatch):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    conn = await SSHConnection.connect(ssh_server.profile(), FakePrompter())
    try:
        calls = []
        original = conn.conn.start_sftp_client

        async def counting(*args, **kwargs):
            calls.append(1)
            return await original(*args, **kwargs)

        monkeypatch.setattr(conn.conn, "start_sftp_client", counting)
        files = conn.sftp()
        a, b = await asyncio.gather(files.client(), files.client())
        assert a is b and len(calls) == 1
        assert conn.sftp() is files
    finally:
        conn.close()


async def test_session_sftp_is_none_for_serial():
    from termdeck.profiles import Profile, SerialSettings

    session = await open_session(Profile("s", "serial", serial=SerialSettings(device="loop://")), FakePrompter())
    try:
        assert session.sftp() is None
    finally:
        await session.close()


async def test_session_sftp_for_ssh_and_closing_closes_the_client(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    session = await open_session(ssh_server.profile(), FakePrompter())
    files = session.sftp()
    assert files is not None
    client = await files.client()
    await session.close()
    await asyncio.wait_for(client.wait_closed(), 5)  # the SFTP channel went down with the connection


# ---------- parse_mode and chmod ----------

@pytest.mark.parametrize("spec, current, expected", [
    ("644", 0o777, 0o644), ("0755", 0, 0o755), ("7", 0o777, 0o7), ("4755", 0, 0o4755),
    ("u+x", 0o644, 0o744), ("go-w", 0o666, 0o644), ("a=r", 0o755, 0o444), ("=rw", 0o000, 0o666),
    ("u+s,g+s", 0o755, 0o6755), ("+t", 0o755, 0o1755), ("o=", 0o777, 0o770),
    ("u=rwx,go=rx", 0, 0o755), ("a+x", 0o600, 0o711), ("u-s", 0o4755, 0o755),
    ("u+x", 0o100644, 0o744),  # file-type bits of a stat result are ignored
])
def test_parse_mode(spec, current, expected):
    assert parse_mode(spec, current) == expected


@pytest.mark.parametrize("spec", ["rwx", "", "u+z", "u+x,", "789", "12345", "u x", "x+u"])
def test_parse_mode_rejects_invalid_text(spec):
    with pytest.raises(SFTPFailure, match="invalid mode"):
        parse_mode(spec)


async def test_chmod_symbolic_applies_to_the_current_mode(files, ssh_server):
    path = ssh_server.root / "m"
    path.write_text("x")
    path.chmod(0o640)
    await files.chmod("/m", "u+x")
    assert path.stat().st_mode & 0o7777 == 0o740
    await files.chmod("/m", "go+r,u-w")
    assert path.stat().st_mode & 0o7777 == 0o544
    await files.chmod("/m", "600")
    assert path.stat().st_mode & 0o7777 == 0o600


async def test_chmod_refuses_modes_out_of_range_and_bad_text(files, ssh_server):
    path = ssh_server.root / "m"
    path.write_text("x")
    path.chmod(0o600)
    with pytest.raises(SFTPFailure, match="not a permission mode"):
        await files.chmod("/m", 0o10000)
    with pytest.raises(SFTPFailure, match="invalid mode"):
        await files.chmod("/m", "rwx")
    assert path.stat().st_mode & 0o7777 == 0o600


# ---------- write through a temporary name ----------

def parts(root):
    return [p.name for p in root.iterdir() if p.name.endswith(".part")]


async def broken_after(first: bytes):
    yield first
    raise RuntimeError("source failed")


@pytest.mark.parametrize("overwrite", [True, False])
async def test_write_that_fails_midway_keeps_the_original_and_leaves_no_part(files, ssh_server, overwrite):
    target = ssh_server.root / "f"
    if overwrite:
        target.write_bytes(b"original")
    with pytest.raises(RuntimeError, match="source failed"):
        await files.write("/f", broken_after(b"partial"), overwrite=overwrite)
    assert parts(ssh_server.root) == []
    assert (target.read_bytes() == b"original") if overwrite else not target.exists()


async def test_write_goes_through_a_hidden_part_file(files, ssh_server):
    seen = []

    async def spy():
        yield b"abc"
        seen.extend(p.name for p in ssh_server.root.iterdir())
        yield b"def"

    await files.write("/f", spy())
    assert len(seen) == 1 and re.fullmatch(r"\.f\.[0-9a-f]{8}\.part", seen[0])
    assert [p.name for p in ssh_server.root.iterdir()] == ["f"]
    assert (ssh_server.root / "f").read_bytes() == b"abcdef"


async def test_overwrite_replaces_the_file_in_one_step(files, ssh_server):
    target = ssh_server.root / "f"
    target.write_bytes(b"original")
    during = []

    async def spy():
        yield b"new"
        during.append(target.read_bytes())  # until the end, readers still see the old content

    assert await files.write("/f", spy(), overwrite=True) == 3
    assert during == [b"original"] and target.read_bytes() == b"new"
    assert parts(ssh_server.root) == []


async def test_a_file_appearing_during_the_upload_is_not_replaced(files, ssh_server):
    target = ssh_server.root / "f"

    async def racing():
        yield b"mine"
        target.write_bytes(b"theirs")

    with pytest.raises(FileExistsError):
        await files.write("/f", racing())
    assert target.read_bytes() == b"theirs" and parts(ssh_server.root) == []


async def test_cancelled_write_leaves_no_part_file(files, ssh_server):
    started = asyncio.Event()

    async def endless():
        yield b"x"
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(files.write("/f", endless()))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(ssh_server.root.iterdir()) == []


# ---------- rename into a folder, glob ----------

async def test_rename_into_an_existing_folder_moves_the_file_into_it(files, ssh_server):
    (ssh_server.root / "a").write_text("a")
    (ssh_server.root / "dir").mkdir()
    await files.rename("/a", "/dir")
    assert (ssh_server.root / "dir" / "a").read_text() == "a" and not (ssh_server.root / "a").exists()
    (ssh_server.root / "a").write_text("again")
    with pytest.raises(FileExistsError):  # the file inside is what would be replaced
        await files.rename("/a", "/dir/")
    assert (ssh_server.root / "dir" / "a").read_text() == "a"


async def test_glob(files, ssh_server):
    for name in ("b.txt", "a.txt", "c.log"):
        (ssh_server.root / name).write_text("x")
    assert await files.glob("/*.txt") == ["/a.txt", "/b.txt"]
    assert await files.glob("/?.log") == ["/c.log"]
    assert await files.glob("/[ab].txt") == ["/a.txt", "/b.txt"]
    with pytest.raises(SFTPFailure, match="nomatch"):
        await files.glob("/nomatch*")
    assert await files.glob("/missing.txt") == ["/missing.txt"]  # no wildcard: as is, even if absent


async def test_glob_without_wildcards_does_not_touch_the_server():
    assert await SFTP(None).glob("plain name") == ["plain name"]


# ---------- reget / reput ----------

DATA = bytes(range(256)) * 40


async def test_reget_continues_a_partial_download(files, ssh_server, tmp_path):
    (ssh_server.root / "big").write_bytes(DATA)
    local = tmp_path / "big"
    local.write_bytes(DATA[: len(DATA) // 2])
    seen = []
    await files.reget("/big", local, progress=lambda name, done, total: seen.append((name, done, total)))
    assert local.read_bytes() == DATA
    assert seen[-1] == ("big", len(DATA), len(DATA))
    await files.reget("/big", local)  # already complete: nothing changes
    assert local.read_bytes() == DATA


async def test_reget_without_a_local_file_downloads_all_and_into_a_folder(files, ssh_server, tmp_path):
    (ssh_server.root / "big").write_bytes(DATA)
    await files.reget("/big", tmp_path)
    assert (tmp_path / "big").read_bytes() == DATA


async def test_reget_refuses_a_local_file_larger_than_the_remote(files, ssh_server, tmp_path):
    (ssh_server.root / "big").write_bytes(b"short")
    local = tmp_path / "big"
    local.write_bytes(b"much longer than the source")
    with pytest.raises(SFTPFailure, match="larger"):
        await files.reget("/big", local)
    assert local.read_bytes() == b"much longer than the source"


async def test_reput_continues_a_partial_upload(files, ssh_server, tmp_path):
    local = tmp_path / "big"
    local.write_bytes(DATA)
    (ssh_server.root / "big").write_bytes(DATA[: len(DATA) // 2])
    seen = []
    await files.reput(local, "/big", progress=lambda name, done, total: seen.append((done, total)))
    assert (ssh_server.root / "big").read_bytes() == DATA and seen[-1] == (len(DATA), len(DATA))
    (ssh_server.root / "dir").mkdir()
    await files.reput(local, "/dir")  # into a folder, nothing there yet
    assert (ssh_server.root / "dir" / "big").read_bytes() == DATA


async def test_reput_refuses_a_remote_file_larger_than_the_local(files, ssh_server, tmp_path):
    local = tmp_path / "small"
    local.write_bytes(b"short")
    (ssh_server.root / "small").write_bytes(b"much longer than the source")
    with pytest.raises(SFTPFailure, match="larger"):
        await files.reput(local, "/small")
    assert (ssh_server.root / "small").read_bytes() == b"much longer than the source"


# ---------- a dead channel ----------

async def test_channel_open_error_is_an_sftp_failure_and_the_next_call_reopens(ssh_server, monkeypatch):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    conn = await SSHConnection.connect(ssh_server.profile(), FakePrompter())
    try:
        files = conn.sftp()
        first = await files.client()

        async def dead(*args, **kwargs):
            raise asyncssh.ChannelOpenError(asyncssh.OPEN_CONNECT_FAILED, "channel gone")

        monkeypatch.setattr(first, "readdir", dead)
        with pytest.raises(SFTPFailure, match="channel"):
            await files.list("/")
        monkeypatch.undo()
        path, _ = await files.list("/")  # a new channel
        assert path == "/" and await files.client() is not first
    finally:
        conn.close()


async def test_exited_client_gives_an_sftp_failure_then_recovers(ssh_server):
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    conn = await SSHConnection.connect(ssh_server.profile(), FakePrompter())
    try:
        files = conn.sftp()
        first = await files.client()
        first.exit()
        await first.wait_closed()
        with pytest.raises(SFTPFailure):
            await files.list("/")
        (ssh_server.root / "f").write_text("x")
        _, entries = await files.list("/")
        assert [e.name for e in entries] == ["f"] and await files.client() is not first
    finally:
        conn.close()
