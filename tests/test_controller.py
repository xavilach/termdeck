import asyncio
import threading

import pytest
import serial

from termdeck.controller import Capabilities, SessionController, State
from termdeck.profiles import Forward, Profile, ProfileStore, SerialSettings

from .conftest import FakePrompter


def _loop(name="loop", **terminal) -> Profile:
    profile = Profile(name, "serial", serial=SerialSettings(device="loop://"))
    for key, value in terminal.items():
        setattr(profile.terminal, key, value)
    return profile


def _controller(profile, store=None, saved=False):
    changes = []
    controller = SessionController(profile, FakePrompter(), store, saved, on_change=lambda c: changes.append(c.state))
    return controller, changes


async def _drain(controller):
    """Read until the session is over (what is left in the queue first)."""
    while await asyncio.wait_for(controller.read(), 5):
        pass


def _pull_the_cable(patch_port):
    gone = threading.Event()

    def read(real, size=1):
        if gone.wait(0.02):
            raise serial.SerialException("cable pulled")
        return real(size)

    patch_port(read=read)
    return gone


async def test_states_follow_a_session_from_connecting_to_a_clean_end():
    controller, changes = _controller(_loop())
    assert controller.state is State.CONNECTING
    assert await controller.connect() is True
    assert controller.state is State.CONNECTED and controller.message == ""
    await controller.session.close()
    await _drain(controller)
    assert controller.state is State.ENDED and controller.clean is True
    assert controller.message == "Session ended"
    assert changes == [State.CONNECTING, State.CONNECTED, State.ENDED]


async def test_an_end_with_a_reason_is_not_clean(patch_port):
    gone = _pull_the_cable(patch_port)
    controller, _ = _controller(_loop())
    assert await controller.connect()
    gone.set()
    await _drain(controller)
    assert controller.state is State.ENDED and controller.clean is False
    assert controller.message.startswith("Session ended: loop: ")


async def test_a_remote_exit_status_is_an_unclean_end(ssh_server):
    controller, _ = _controller(ssh_server.profile(remote_command="exit3"))
    assert await controller.connect()
    await _drain(controller)
    assert controller.state is State.ENDED and not controller.clean
    assert "status 3" in controller.message
    await controller.close()


async def test_a_refused_connection_fails_with_a_message(ssh_server):
    profile = ssh_server.profile()
    ssh_server.listener.close()  # nothing listens on the port any more
    controller, changes = _controller(profile)
    assert await controller.connect() is False
    assert controller.state is State.FAILED and controller.message
    assert controller.session is None and controller.forwards() == []
    assert changes == [State.CONNECTING, State.FAILED]


async def test_a_wrong_password_fails_without_a_session(ssh_server):
    controller = SessionController(ssh_server.profile(), FakePrompter(answers=["wrong"] * 3))
    assert await controller.connect() is False
    assert controller.state is State.FAILED and controller.session is None


async def test_a_new_connect_forgets_the_last_end():
    controller, _ = _controller(_loop())
    await controller.connect()
    await controller.session.close()
    await _drain(controller)
    assert controller.clean
    assert await controller.connect()
    assert controller.state is State.CONNECTED and controller.clean is False and controller.message == ""
    await controller.close()


async def test_a_saved_profile_is_read_again_on_each_connect(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.save(_loop())
    controller, _ = _controller(store.get("loop"), store, saved=True)
    assert await controller.connect()
    assert controller.profile.serial.baudrate == 115200
    await controller.close()

    edited = store.get("loop")
    edited.serial.baudrate = 9600
    store.save(edited)
    assert await controller.connect()
    assert controller.profile.serial.baudrate == 9600
    await controller.close()


async def test_a_profile_that_is_not_saved_is_not_looked_up_again(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.save(_loop())
    controller, _ = _controller(_loop(), store, saved=False)
    store_copy = store.get("loop")
    store_copy.serial.baudrate = 9600
    store.save(store_copy)
    assert await controller.connect()
    assert controller.profile.serial.baudrate == 115200
    await controller.close()


async def test_a_deleted_saved_profile_fails_and_says_why(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.save(_loop())
    controller, _ = _controller(store.get("loop"), store, saved=True)
    assert await controller.connect()
    await controller.close()
    store.delete("loop")
    assert await controller.connect() is False
    assert controller.state is State.FAILED and "deleted or renamed" in controller.message
    assert "loop" in controller.message


async def test_saved_without_a_store_means_not_saved():
    controller = SessionController(_loop(), FakePrompter(), None, saved=True)
    assert controller.saved is False
    assert await controller.connect()
    await controller.close()


async def test_rename_reconnects_under_the_new_name(tmp_path):
    store = ProfileStore(tmp_path / "p.json")
    store.save(_loop())
    controller, _ = _controller(store.get("loop"), store, saved=True)
    controller.rename("other", "x")  # not this profile
    assert controller.profile.name == "loop"
    controller.rename("loop", "board")
    assert controller.profile.name == "board"
    assert controller.profile.serial.device == "loop://"
    renamed = store.get("loop")
    renamed.name = "board"
    store.update("loop", renamed)
    assert await controller.connect()
    assert controller.profile.name == "board"
    await controller.close()


async def test_rename_leaves_an_unsaved_profile_alone():
    controller, _ = _controller(_loop())
    controller.rename("loop", "board")
    assert controller.profile.name == "loop"


@pytest.mark.parametrize("when, clean_end, closes", [
    ("never", True, False), ("clean", True, True), ("always", True, True),
    ("never", False, False), ("clean", False, False), ("always", False, True),
])
async def test_closes_tab_follows_close_on_exit(patch_port, when, clean_end, closes):
    gone = None if clean_end else _pull_the_cable(patch_port)
    controller, _ = _controller(_loop(close_on_exit=when))
    assert await controller.connect()
    assert controller.closes_tab is False  # still connected
    if clean_end:
        await controller.session.close()
    else:
        gone.set()
    await _drain(controller)
    assert controller.state is State.ENDED and controller.clean is clean_end
    assert controller.closes_tab is closes


async def test_a_failed_connection_never_closes_its_tab(ssh_server):
    profile = ssh_server.profile()
    profile.terminal.close_on_exit = "always"
    ssh_server.listener.close()
    controller, _ = _controller(profile)
    assert not await controller.connect()
    assert controller.closes_tab is False


async def test_a_serial_loop_can_send_a_break_but_has_no_files():
    controller, _ = _controller(_loop())
    assert controller.capabilities == Capabilities()  # nothing before it is connected
    assert await controller.connect()
    assert controller.capabilities == Capabilities(send_break=True, sftp=False, typing=True)
    assert controller.capabilities.to_dict() == {"send_break": True, "sftp": False, "typing": True}
    assert await controller.send_break() == (True, "Break sent")
    await controller.close()


async def test_an_ssh_session_has_files_and_no_break(ssh_server):
    controller, _ = _controller(ssh_server.profile())
    assert await controller.connect()
    assert controller.capabilities == Capabilities(send_break=False, sftp=True, typing=True)
    sent, text = await controller.send_break()
    assert sent is False and "cannot send a break" in text
    await controller.close()


async def test_a_connection_without_a_shell_cannot_be_typed_into(ssh_server):
    controller, _ = _controller(ssh_server.profile(no_shell=True))
    assert await controller.connect()
    assert controller.capabilities.typing is False and controller.capabilities.sftp is True
    await controller.close()


async def test_send_break_before_connecting_is_refused():
    controller, _ = _controller(_loop())
    sent, text = await controller.send_break()
    assert sent is False and text


async def test_send_break_reports_a_session_that_raises():
    controller, _ = _controller(_loop())
    assert await controller.connect()

    async def refuse():
        raise serial.SerialException("device vanished")

    controller.session.send_break = refuse
    sent, text = await controller.send_break()
    assert sent is False and text.startswith("Break not sent: ") and "device vanished" in text
    await controller.close()


async def test_close_of_a_live_session_ends_it_as_disconnected():
    controller, changes = _controller(_loop())
    assert await controller.connect()
    await controller.close()
    assert controller.state is State.ENDED and controller.message == "Disconnected"
    assert controller.session is None
    assert changes[-1] is State.ENDED
    await controller.close()  # closing twice is harmless
    assert controller.state is State.ENDED


async def test_close_does_not_overwrite_a_failure(ssh_server):
    profile = ssh_server.profile()
    ssh_server.listener.close()
    controller, _ = _controller(profile)
    await controller.connect()
    message = controller.message
    await controller.close()
    assert controller.state is State.FAILED and controller.message == message


async def test_forwards_report_what_opened_and_what_failed(ssh_server):
    busy = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    busy_port = busy.sockets[0].getsockname()[1]
    profile = ssh_server.profile(forwards=[
        Forward("local", 0, "127.0.0.1", "127.0.0.1", 9),
        Forward("local", busy_port, "127.0.0.1", "127.0.0.1", 9),
    ])
    controller, _ = _controller(profile)
    try:
        assert await controller.connect()
        ok, failed = controller.session.forwards()
        assert ok.active and ok.port and ok.port != 0 and ok.error == "" and ok.kind == "local"
        assert not failed.active and failed.port is None and failed.error
        assert str(busy_port) in failed.spec
        assert f"listening on port {ok.port}" in ok.describe()
        assert "failed" in failed.describe() and failed.error in failed.describe()
        assert controller.forwards() == [ok, failed]
        assert controller.forwards_summary() == "1 of 2 forwards failed"
    finally:
        await controller.close()
        busy.close()


async def test_forwards_summary_counts(ssh_server):
    controller, _ = _controller(ssh_server.profile())
    assert controller.forwards_summary() == ""  # not connected
    assert await controller.connect()
    assert controller.forwards_summary() == ""  # no forwards
    await controller.close()

    one, _ = _controller(ssh_server.profile(forwards=[Forward("local", 0, "127.0.0.1", "127.0.0.1", 9)]))
    assert await one.connect()
    assert one.forwards_summary() == "1 forward"
    await one.close()

    two, _ = _controller(ssh_server.profile(forwards=[Forward("local", 0, "127.0.0.1", "127.0.0.1", 9),
                                                      Forward("dynamic", 0, "127.0.0.1")]))
    assert await two.connect()
    assert two.forwards_summary() == "2 forwards"
    await two.close()


async def test_closing_while_output_is_read_is_a_local_close_not_a_clean_end():
    """P1-370: the reader sees the session end while close() runs; that is not the remote side ending."""
    controller, changes = _controller(_loop(close_on_exit="always"))
    assert await controller.connect()
    reader = asyncio.create_task(_drain(controller))
    await asyncio.sleep(0.05)
    await controller.close()
    await asyncio.wait_for(reader, 5)
    assert controller.state is State.ENDED and controller.message == "Disconnected"
    assert controller.closed_locally and not controller.closes_tab
    assert changes.count(State.ENDED) == 1


async def test_close_while_connecting_drops_the_connection_that_finishes(monkeypatch):
    """P1-371: a close() during connect() is not undone when the connect completes."""
    import termdeck.controller as module

    opened, release = [], asyncio.Event()
    real = module.open_session

    async def slow(*args, **kwargs):
        await release.wait()
        session = await real(*args, **kwargs)
        opened.append(session)
        return session

    monkeypatch.setattr(module, "open_session", slow)
    controller, _ = _controller(_loop())
    connecting = asyncio.create_task(controller.connect())
    await asyncio.sleep(0)
    await controller.close()
    release.set()
    assert await connecting is False
    assert controller.session is None and controller.state is State.FAILED
    assert await asyncio.wait_for(opened[0].read(), 5) == b""  # the late session was closed


async def test_a_second_connect_closes_the_first_session():
    controller, _ = _controller(_loop())
    assert await controller.connect()
    first = controller.session
    assert await controller.connect()
    assert controller.session is not first
    assert await asyncio.wait_for(first.read(), 5) == b""
    await controller.close()


async def test_a_cancelled_connect_reports_failed():
    controller, changes = _controller(_loop())
    task = asyncio.create_task(controller.connect())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert controller.state is State.FAILED and changes[-1] is State.FAILED


async def test_snapshot_is_what_the_uis_show():
    controller, _ = _controller(_loop())
    assert controller.snapshot()["status"] == "Connecting…"
    await controller.connect()
    snap = controller.snapshot()
    assert snap["state"] == "connected" and snap["live"] is True
    assert snap["capabilities"] == {"send_break": True, "sftp": False, "typing": True}
    assert snap["forwards"] == [] and snap["forwards_failed"] == 0
    await controller.close()
    snap = controller.snapshot()
    assert snap["state"] == "ended" and snap["close_tab"] is False and snap["live"] is False
