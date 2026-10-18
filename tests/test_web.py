import asyncio
import os

import pytest
from aiohttp import FormData, WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from termdeck.profiles import ProfileStore
from termdeck.ssh import remember_host_key
from termdeck.web.server import AUTH_HEADER, create_app

TOKEN = "t0ken"


@pytest.fixture
async def client(tmp_path):
    app = create_app(ProfileStore(tmp_path / "profiles.json"), token=TOKEN)
    async with TestClient(TestServer(app, host="127.0.0.1")) as c:
        yield c


async def login(client, token=TOKEN) -> str:
    """Log in like the page does and send the secret with every later request."""
    r = await client.post("/api/login", json={"token": token})
    assert r.status == 200
    secret = (await r.json())["secret"]
    client.session.headers[AUTH_HEADER] = secret
    return secret


async def test_requires_login_and_start_token_is_single_use(client):
    assert (await client.get("/")).status == 200  # the page itself holds no secrets
    assert (await client.get("/api/profiles")).status == 401
    assert (await client.post("/api/login", json={"token": "wrong"})).status == 403
    assert (await client.post("/api/login", json={"token": "nön-ascii"})).status == 403
    await login(client)
    assert (await client.get("/api/profiles")).status == 200
    assert (await client.post("/api/login", json={"token": TOKEN})).status == 403  # already used
    assert (await client.get("/api/profiles", headers={AUTH_HEADER: "forged"})).status == 401


async def test_new_window_link(client):
    await login(client)
    url = (await (await client.post("/api/link")).json())["url"]
    assert url.startswith("/#token=")
    other = await client.post("/api/login", json={"token": url.split("=", 1)[1]})
    assert other.status == 200


async def test_rejects_foreign_host_and_origin(client):
    await login(client)
    assert (await client.get("/api/profiles", headers={"Host": "evil.example"})).status == 403
    assert (await client.get("/api/profiles", headers={"Origin": "http://evil.example"})).status == 403
    assert (await client.get("/api/profiles", headers={"Origin": "null"})).status == 403


async def test_security_headers_and_no_cookie(client):
    r = await client.get("/")
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert r.headers["X-Frame-Options"] == "DENY"
    r = await client.post("/api/login", json={"token": TOKEN})
    assert "Set-Cookie" not in r.headers


@pytest.mark.parametrize("body", [
    [1, 2], {"name": "x", "kind": "ssh", "ssh": "abc"}, {"name": "x", "kind": "ssh", "ssh": {"host": "h", "forwards": "abc"}},
    {"name": "x", "kind": "ssh", "ssh": {"host": "h", "port": "22"}}, {"name": "x", "kind": "serial", "serial": {"device": "d", "baudrate": "fast"}},
    {"name": "x", "kind": "ssh", "ssh": {"host": "h", "forwards": [{"kind": "bogus", "listen_port": 1}]}},
    {"name": "x", "kind": "ssh", "ssh": {"host": "a\nb"}},
])
async def test_malformed_profiles_get_400(client, body):
    await login(client)
    r = await client.post("/api/profiles", json=body)
    assert r.status == 400, await r.text()


async def test_empty_forward_listen_host_becomes_localhost(client):
    await login(client)
    body = {"name": "x", "kind": "ssh", "ssh": {"host": "h", "forwards": [
        {"kind": "local", "listen_host": "", "listen_port": 8080, "dest_host": "db", "dest_port": 5432}]}}
    saved = await (await client.post("/api/profiles", json=body)).json()
    assert saved["ssh"]["forwards"][0]["listen_host"] == "localhost"


async def test_profile_crud(client):
    await login(client)
    body = {"name": "board", "kind": "serial", "serial": {"device": "loop://", "parity": "E"}}
    r = await client.post("/api/profiles", json=body)
    assert r.status == 200 and (await r.json())["summary"] == "serial loop:// 115200 8E1"
    assert (await client.post("/api/profiles", json=body)).status == 400  # duplicate name
    body["name"] = "board2"
    assert (await client.put("/api/profiles/board", json=body)).status == 200
    assert [p["name"] for p in (await (await client.get("/api/profiles")).json())["profiles"]] == ["board2"]
    bad = {"name": "x", "kind": "serial", "serial": {"device": "d", "parity": "Q"}}
    assert (await client.post("/api/profiles", json=bad)).status == 400
    assert (await client.delete("/api/profiles/board2")).status == 200
    assert (await client.delete("/api/profiles/board2")).status == 404


async def test_options(client):
    await login(client)
    data = await (await client.get("/api/options")).json()
    assert {"profile", "ssh", "serial", "terminal"} <= set(data["options"])
    assert data["defaults"]["serial"]["baudrate"] == 115200


async def test_options_carry_the_themes(client):
    from termdeck.themes import COLOURS, THEMES

    await login(client)
    data = await (await client.get("/api/options")).json()
    assert data["themes"] == THEMES and "putty" in data["themes"]
    assert all(set(colours) == set(COLOURS) for colours in data["themes"].values())
    theme = {o["key"]: o for o in data["options"]["terminal"]}["theme"]
    assert ["putty", "PuTTY"] in theme["choices"]


async def test_profiles_carry_their_palette(client):
    from termdeck.themes import THEMES

    await login(client)
    body = {"name": "board", "kind": "serial", "serial": {"device": "loop://"},
            "terminal": {"theme": "dracula", "colours": "background=#112233\nbright_blue=#ABCDEF"}}
    saved = await (await client.post("/api/profiles", json=body)).json()
    expected = {**THEMES["dracula"], "background": "#112233", "brightBlue": "#abcdef"}
    assert saved["palette"] == expected
    [listed] = (await (await client.get("/api/profiles")).json())["profiles"]
    assert listed["palette"] == expected
    quick = await (await client.post("/api/target", json={"text": "board"})).json()
    assert quick["palette"] == expected
    other = await (await client.post("/api/target", json={"text": "bob@host.example"})).json()
    assert other["palette"] == THEMES["dark"]


async def test_bad_colours_are_a_400_and_nothing_is_saved(client):
    await login(client)
    body = {"name": "board", "kind": "serial", "serial": {"device": "loop://"},
            "terminal": {"colours": "red=notacolour"}}
    r = await client.post("/api/profiles", json=body)
    assert r.status == 400 and "invalid colour" in (await r.json())["error"]
    assert (await (await client.get("/api/profiles")).json())["profiles"] == []


async def _receive_until(ws, predicate, timeout=5):
    async def loop():
        output = b""
        while True:
            msg = await ws.receive()
            if msg.type == WSMsgType.BINARY:
                output += msg.data
                if predicate(None, output):
                    return None, output
            elif msg.type == WSMsgType.TEXT:
                m = msg.json()
                if predicate(m, output):
                    return m, output
            else:
                return None, output

    return await asyncio.wait_for(loop(), timeout)


async def test_terminal_over_websocket_with_prompts_and_sftp(client, ssh_server):
    secret = await login(client)
    profile = ssh_server.profile().to_dict()
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": profile, "cols": 90, "rows": 20})

    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "hostkey")
    assert m["fingerprint"] == ssh_server.key.get_fingerprint() and m["changed"] is False
    await ws.send_json({"type": "answer", "id": m["id"], "value": True})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "prompt")
    assert m["secret"] is True
    await ws.send_json({"type": "answer", "id": m["id"], "value": "secret"})
    m, out = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    assert m["capabilities"]["sftp"] is True and m["capabilities"]["send_break"] is False
    sid = m["session"]

    _, out = await _receive_until(ws, lambda m, out: b"90x20" in out)
    await ws.send_json({"type": "resize", "cols": 100, "rows": 30})
    _, out = await _receive_until(ws, lambda m, out: b"resized 100x30" in out)
    await ws.send_json({"type": "input", "data": "héllo\r"})
    _, out = await _receive_until(ws, lambda m, out: "héllo".encode() in out)

    form = FormData()
    form.add_field("file", b"remote data", filename="file")
    r = await client.post(f"/api/sessions/{sid}/sftp/upload?path=/", data=form)
    assert r.status == 200, await r.text()
    listing = await (await client.get(f"/api/sessions/{sid}/sftp?path=/")).json()
    assert listing["path"] == "/" and [e["name"] for e in listing["entries"]] == ["file"]
    ticket = await (await client.post(f"/api/sessions/{sid}/sftp/ticket", json={"path": "/file"})).json()
    del client.session.headers[AUTH_HEADER]  # a download link works on its own...
    r = await client.get(ticket["url"])
    assert await r.read() == b"remote data"
    assert "filename*=UTF-8''file" in r.headers["Content-Disposition"]
    assert (await client.get(ticket["url"])).status == 403  # ...but only once
    client.session.headers[AUTH_HEADER] = secret
    r = await client.post(f"/api/sessions/{sid}/sftp/op", json={"op": "mkdir", "path": "/dir"})
    assert r.status == 200
    assert (ssh_server.root / "dir").is_dir()

    await ws.send_json({"type": "input", "data": "exit\r"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "ended")
    await ws.close()
    assert (await client.get(f"/api/sessions/{sid}/sftp")).status == 404


async def test_rejected_host_key_reports_error(client, ssh_server):
    await login(client)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": ssh_server.profile().to_dict()})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "hostkey")
    await ws.send_json({"type": "answer", "id": m["id"], "value": False})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "failed")
    assert m["message"] == "Host key not trusted, connection aborted"


async def test_saved_serial_profile_and_break(client):
    await login(client)
    await client.post("/api/profiles", json={"name": "loop", "kind": "serial", "serial": {"device": "loop://"}})
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "name": "loop"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    assert m["capabilities"]["send_break"] is True and m["capabilities"]["sftp"] is False
    await ws.send_json({"type": "break"})
    await ws.send_json({"type": "input", "data": "AT\r"})
    _, out = await _receive_until(ws, lambda m, out: b"AT\r" in out)
    await ws.close()


async def test_reconnect_uses_the_profile_as_saved_now_and_sends_it(client):
    """P1-374: the page gets the profile it was connected with, so options saved elsewhere apply."""
    await login(client)
    await client.post("/api/profiles", json={"name": "loop", "kind": "serial", "serial": {"device": "loop://"}})
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "name": "loop"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connecting")
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    assert m["profile"]["terminal"]["theme"] == "dark" and m["profile"]["summary"].startswith("serial loop://")
    await ws.close()
    await client.put("/api/profiles/loop", json={"name": "loop", "kind": "serial", "serial": {"device": "loop://"},
                                                 "terminal": {"theme": "putty"}})
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "name": "loop"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    assert m["profile"]["terminal"]["theme"] == "putty" and m["profile"]["palette"]["background"]
    await ws.close()


async def test_unknown_profile_reports_error(client):
    await login(client)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "name": "nope"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "failed")
    assert "nope" in m["message"]


async def test_websocket_auth_by_subprotocol(client):
    secret = await login(client)
    del client.session.headers[AUTH_HEADER]
    with pytest.raises(Exception):
        await client.ws_connect("/api/terminal")
    client.session.headers[AUTH_HEADER] = secret
    await client.post("/api/profiles", json={"name": "loop", "kind": "serial", "serial": {"device": "loop://"}})
    del client.session.headers[AUTH_HEADER]
    ws = await client.ws_connect("/api/terminal", protocols=("termdeck", f"auth.{secret}"))
    assert ws.protocol == "termdeck"
    await ws.send_json({"type": "open", "name": "loop"})
    await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    await ws.send_str("not json")  # ignored, the session keeps working
    await ws.send_json({"type": "resize", "cols": "x", "rows": 10**9})
    await ws.send_json({"type": "input", "data": "still here"})
    _, out = await _receive_until(ws, lambda m, out: b"still here" in out)
    await ws.close()


async def test_content_disposition_is_safe():
    from termdeck.web.server import _content_disposition

    value = _content_disposition('ré"port\r\n.txt')
    assert "\r" not in value and "\n" not in value
    assert value.startswith('attachment; filename="r_')
    assert "filename*=UTF-8''r%C3%A9%22port.txt" in value


async def test_target_resolves_saved_quick_and_mistyped(client):
    await login(client)
    r = await client.post("/api/profiles", json={"name": "router", "kind": "ssh", "ssh": {"host": "r.lan"}})
    assert r.status == 200

    saved = await (await client.post("/api/target", json={"text": "router"})).json()
    assert saved["saved"] is True and saved["ssh"]["host"] == "r.lan"

    quick = await (await client.post("/api/target", json={"text": "bob@host.example"})).json()
    assert quick["saved"] is False
    assert quick["ssh"]["username"] == "bob" and quick["ssh"]["host"] == "host.example"

    r = await client.post("/api/target", json={"text": "routr"})
    assert r.status == 400
    assert "did you mean 'router'?" in (await r.json())["error"]


async def test_sftp_ticket_for_a_missing_path_is_a_400(client, ssh_server):
    await login(client)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": ssh_server.profile().to_dict(), "cols": 80, "rows": 24})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "hostkey")
    await ws.send_json({"type": "answer", "id": m["id"], "value": True})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "prompt")
    await ws.send_json({"type": "answer", "id": m["id"], "value": "secret"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    sid = m["session"]

    r = await client.post(f"/api/sessions/{sid}/sftp/ticket", json={"path": "/nope"})
    assert r.status == 400
    assert (await r.json())["error"]
    (ssh_server.root / "there").write_bytes(b"x")
    assert (await client.post(f"/api/sessions/{sid}/sftp/ticket", json={"path": "/there"})).status == 200
    await ws.close()


async def _sftp_session(client, ssh_server):
    await login(client)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": ssh_server.profile().to_dict(), "cols": 80, "rows": 24})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "hostkey")
    await ws.send_json({"type": "answer", "id": m["id"], "value": True})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "prompt")
    await ws.send_json({"type": "answer", "id": m["id"], "value": "secret"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    return ws, m["session"]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root can read any file")
async def test_sftp_ticket_for_an_unreadable_file_is_a_400(client, ssh_server):
    ws, sid = await _sftp_session(client, ssh_server)
    secret = ssh_server.root / "secret"
    secret.write_bytes(b"x")
    secret.chmod(0)
    r = await client.post(f"/api/sessions/{sid}/sftp/ticket", json={"path": "/secret"})
    assert r.status == 400
    assert (await r.json())["error"]
    await ws.close()


async def test_sftp_ticket_for_a_directory_is_a_400(client, ssh_server):
    ws, sid = await _sftp_session(client, ssh_server)
    (ssh_server.root / "dir").mkdir()
    r = await client.post(f"/api/sessions/{sid}/sftp/ticket", json={"path": "/dir"})
    assert r.status == 400
    assert (await r.json())["error"]
    await ws.close()


async def test_profiles_list_reports_warnings(client, tmp_path):
    import json

    (tmp_path / "profiles.json").write_text(json.dumps({"profiles": [
        {"name": "good", "kind": "serial", "serial": {"device": "d"}},
        {"name": "bad", "kind": "serial", "serial": {"device": "d", "parity": "Q"}},
    ]}))
    await login(client)
    body = await (await client.get("/api/profiles")).json()
    assert [p["name"] for p in body["profiles"]] == ["good"]
    assert len(body["warnings"]) == 1 and "bad" in body["warnings"][0]
    (tmp_path / "profiles.json").write_text(json.dumps({"profiles": []}))
    body = await (await client.get("/api/profiles")).json()
    assert (body["profiles"], body["warnings"]) == ([], [])


async def test_profiles_list_with_corrupt_file_is_200_with_error(client, tmp_path):
    path = tmp_path / "profiles.json"
    path.write_text("{oops")
    await login(client)
    r = await client.get("/api/profiles")
    assert r.status == 200
    body = await r.json()
    assert body["profiles"] == [] and str(path) in body["error"]
    assert path.read_text() == "{oops"


async def test_quick_target_works_with_corrupt_profiles_file(client, tmp_path):
    (tmp_path / "profiles.json").write_text("{oops")
    await login(client)
    r = await client.post("/api/target", json={"text": "bob@host.example"})
    assert r.status == 200
    assert (await r.json())["ssh"]["host"] == "host.example"


class FakeSocket:
    """Stands in for the WebSocket: records what the prompter sends to the page."""

    def __init__(self):
        self.sent = []
        self.arrived = asyncio.Event()

    async def send_json(self, message):
        self.sent.append(message)
        self.arrived.set()


async def _asked(prompter, socket, call):
    """Start `call`, wait for the question to reach the page and return (task, question id)."""
    task = asyncio.create_task(call)
    await asyncio.wait_for(socket.arrived.wait(), 2)
    socket.arrived.clear()
    return task, socket.sent[-1]["id"]


@pytest.mark.parametrize("answer, expected", [("hunter2", "hunter2"), ("", ""), (None, None),
                                              (5, None), (True, None), (["a"], None), ({"x": 1}, None)])
async def test_web_prompter_only_accepts_text_answers(answer, expected):
    from termdeck.web.server import WebPrompter

    socket = FakeSocket()
    prompter = WebPrompter(socket)
    task, qid = await _asked(prompter, socket, prompter.ask("Password: "))
    assert socket.sent[-1] == {"type": "prompt", "text": "Password: ", "secret": True, "id": qid}
    prompter.answer(qid, answer)
    assert await asyncio.wait_for(task, 2) == expected


@pytest.mark.parametrize("answer, expected", [(True, True), (False, False), (None, False), ("true", False),
                                              ("yes", False), (1, False), ([True], False)])
async def test_web_prompter_trusts_a_host_key_only_for_true(answer, expected):
    from termdeck.session import HostKey
    from termdeck.web.server import WebPrompter

    socket = FakeSocket()
    prompter = WebPrompter(socket)
    key = HostKey("h", 22, "ssh-ed25519", "SHA256:abc", changed=True, old_fingerprints=["ssh-ed25519 SHA256:old"],
                  known_hosts="/kh")
    task, qid = await _asked(prompter, socket, prompter.confirm_host_key(key))
    message = socket.sent[-1]
    assert (message["type"], message["key_type"], message["fingerprint"], message["changed"]) == \
        ("hostkey", "ssh-ed25519", "SHA256:abc", True)
    assert message["old_fingerprints"] == ["ssh-ed25519 SHA256:old"] and message["known_hosts"] == "/kh"
    prompter.answer(qid, answer)
    assert await asyncio.wait_for(task, 2) is expected


async def test_web_prompter_cancelled_connection_answers_no():
    from termdeck.web.server import WebPrompter

    socket = FakeSocket()
    prompter = WebPrompter(socket)
    task, _ = await _asked(prompter, socket, prompter.ask("x"))
    prompter.cancel_all()
    assert await asyncio.wait_for(task, 2) is None
    prompter.answer("not-an-id", "late")  # ignored


async def test_server_banner_is_sent_as_a_notice(client, ssh_server):
    await login(client)
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    ssh_server.banner = "Authorized users only\n"
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": ssh_server.profile().to_dict()})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] in ("notice", "prompt"))
    assert m == {"type": "notice", "text": "Authorized users only"}
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "prompt")
    await ws.send_json({"type": "answer", "id": m["id"], "value": "secret"})
    await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    await ws.close()


async def test_hostkey_message_carries_the_key_details(client, ssh_server):
    await login(client)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": ssh_server.profile().to_dict()})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "hostkey")
    assert m["key_type"] == "ssh-ed25519" and m["fingerprint"].startswith("SHA256:")
    assert m["old_fingerprints"] == [] and m["known_hosts"].endswith("known_hosts")
    await ws.send_json({"type": "answer", "id": m["id"], "value": "true"})  # not a real yes
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "failed")
    assert m["message"] == "Host key not trusted, connection aborted"


async def test_closed_message_says_why_for_a_remote_exit_status(client, ssh_server):
    await login(client)
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": ssh_server.profile(remote_command="exit3").to_dict()})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "prompt")
    await ws.send_json({"type": "answer", "id": m["id"], "value": "secret"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "ended")
    assert m["message"].startswith("Session ended: ") and "status 3" in m["message"]


async def test_closed_message_says_why_when_the_serial_port_disappears(client, patch_port):
    import threading

    import serial

    gone = threading.Event()

    def read(real, size=1):
        if gone.wait(0.02):
            raise serial.SerialException("port is gone")
        return real(size)

    patch_port(read=read)
    await login(client)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": {"name": "loop", "kind": "serial", "serial": {"device": "loop://"}}})
    await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    gone.set()
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "ended")
    assert m["message"].startswith("Session ended: loop: ")


async def test_closed_message_without_reason_for_a_normal_exit(client, ssh_server):
    await login(client)
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": ssh_server.profile(remote_command="uptime").to_dict()})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "prompt")
    await ws.send_json({"type": "answer", "id": m["id"], "value": "secret"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "ended")
    assert m["message"] == "Session ended"


async def test_a_bug_in_a_handler_is_a_500_not_a_400(client, monkeypatch):
    def broken(self):
        raise AttributeError("'NoneType' object has no attribute 'x'")

    monkeypatch.setattr(ProfileStore, "load", broken)
    await login(client)
    assert (await client.get("/api/profiles")).status == 500


async def _upload(client, sid, name, data, query="path=/"):
    form = FormData()
    form.add_field("file", data, filename=name)
    return await client.post(f"/api/sessions/{sid}/sftp/upload?{query}", data=form)


async def test_sftp_check_returns_existing_names(client, ssh_server):
    ws, sid = await _sftp_session(client, ssh_server)
    (ssh_server.root / "here.txt").write_text("x")
    r = await client.post(f"/api/sessions/{sid}/sftp/check", json={"path": "/", "names": ["here.txt", "new.txt", "../here.txt"]})
    assert r.status == 200
    assert (await r.json())["existing"] == ["here.txt", "../here.txt"]  # the page's names, judged by base name
    r = await client.post(f"/api/sessions/{sid}/sftp/check", json={"path": "/", "names": [".."]})
    assert r.status == 400
    await ws.close()


async def test_upload_to_an_existing_name_is_a_409_unless_overwrite(client, ssh_server):
    ws, sid = await _sftp_session(client, ssh_server)
    (ssh_server.root / "f.txt").write_bytes(b"old")
    r = await _upload(client, sid, "f.txt", b"new")
    assert r.status == 409 and "already exists" in (await r.json())["error"]
    assert (ssh_server.root / "f.txt").read_bytes() == b"old"
    r = await _upload(client, sid, "f.txt", b"new", "path=/&overwrite=0")
    assert r.status == 409
    r = await _upload(client, sid, "f.txt", b"new", "path=/&overwrite=1")
    assert r.status == 200 and (await r.json())["uploaded"] == ["f.txt"]
    assert (ssh_server.root / "f.txt").read_bytes() == b"new"
    await ws.close()


async def test_upload_names_are_reduced_to_the_base_name(client, ssh_server):
    ws, sid = await _sftp_session(client, ssh_server)
    (ssh_server.root / "sub").mkdir()
    # Sent as is: aiohttp's FormData would percent-encode the slash.
    for raw, saved in [("../escape.txt", "escape.txt"), ("a/b/../../deep.txt", "deep.txt")]:
        body = f'--B\r\nContent-Disposition: form-data; name="file"; filename="{raw}"\r\n\r\nx\r\n--B--\r\n'.encode()
        r = await client.post(f"/api/sessions/{sid}/sftp/upload?path=/sub", data=body,
                              headers={"Content-Type": "multipart/form-data; boundary=B"})
        assert r.status == 200 and (await r.json())["uploaded"] == [saved]
        assert (ssh_server.root / "sub" / saved).read_bytes() == b"x"
    assert sorted(p.name for p in ssh_server.root.iterdir()) == ["sub"]  # nothing escaped the folder
    r = await _upload(client, sid, "..", b"x", "path=/sub")
    assert r.status == 400
    await ws.close()


async def test_sftp_remove_recursive_and_chmod(client, ssh_server):
    ws, sid = await _sftp_session(client, ssh_server)
    (ssh_server.root / "d").mkdir()
    (ssh_server.root / "d" / "f").write_text("x")
    (ssh_server.root / "m").write_text("x")
    op = f"/api/sessions/{sid}/sftp/op"
    r = await client.post(op, json={"op": "remove", "path": "/d"})
    assert r.status == 400 and (ssh_server.root / "d").exists()
    r = await client.post(op, json={"op": "remove", "path": "/d", "recursive": True})
    assert r.status == 200 and not (ssh_server.root / "d").exists()
    r = await client.post(op, json={"op": "chmod", "path": "/m", "mode": "644"})
    assert r.status == 200 and (ssh_server.root / "m").stat().st_mode & 0o777 == 0o644
    r = await client.post(op, json={"op": "chmod", "path": "/m", "mode": "89"})  # not octal
    assert r.status == 400
    r = await client.post(op, json={"op": "nope", "path": "/m"})
    assert r.status == 400
    await ws.close()


async def test_api_and_downloads_are_not_cached_but_static_files_are_allowed_to_be(client, ssh_server):
    ws, sid = await _sftp_session(client, ssh_server)
    (ssh_server.root / "f").write_bytes(b"data")
    r = await client.get("/api/profiles")
    assert r.headers["Cache-Control"] == "no-store"
    assert (await client.get(f"/api/sessions/{sid}/sftp?path=/")).headers["Cache-Control"] == "no-store"
    ticket = await (await client.post(f"/api/sessions/{sid}/sftp/ticket", json={"path": "/f"})).json()
    r = await client.get(ticket["url"])
    assert await r.read() == b"data" and r.headers["Cache-Control"] == "no-store"
    r = await client.get("/static/app.js")
    assert r.status == 200 and r.headers.get("Cache-Control") != "no-store"
    await ws.close()


async def test_sftp_chmod_accepts_symbolic_modes_and_rejects_bad_ones(client, ssh_server):
    ws, sid = await _sftp_session(client, ssh_server)
    target = ssh_server.root / "m"
    target.write_text("x")
    target.chmod(0o644)
    op = f"/api/sessions/{sid}/sftp/op"
    r = await client.post(op, json={"op": "chmod", "path": "/m", "mode": "u+x"})
    assert r.status == 200 and target.stat().st_mode & 0o777 == 0o744
    r = await client.post(op, json={"op": "chmod", "path": "/m", "mode": "bad"})
    assert r.status == 400 and "mode" in (await r.json())["error"]
    r = await client.post(op, json={"op": "chmod", "path": "/m"})  # no mode at all
    assert r.status == 400
    assert target.stat().st_mode & 0o777 == 0o744
    await ws.close()


@pytest.mark.parametrize("route", ["op", "check"])
@pytest.mark.parametrize("body", [[], "text", 5, None])
async def test_sftp_json_body_that_is_not_an_object_is_a_400(client, ssh_server, route, body):
    ws, sid = await _sftp_session(client, ssh_server)
    r = await client.post(f"/api/sessions/{sid}/sftp/{route}", json=body)
    assert r.status == 400 and "error" in await r.json()
    await ws.close()


async def test_closed_message_is_clean_only_without_a_reason(client, ssh_server, patch_port):
    import threading

    import serial

    await login(client)
    remember_host_key("127.0.0.1", ssh_server.port, ssh_server.key)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": ssh_server.profile(remote_command="uptime").to_dict()})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "prompt")
    await ws.send_json({"type": "answer", "id": m["id"], "value": "secret"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "ended")
    assert m["clean"] is True

    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": ssh_server.profile(remote_command="exit3").to_dict()})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "prompt")
    await ws.send_json({"type": "answer", "id": m["id"], "value": "secret"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "ended")
    assert m["clean"] is False

    gone = threading.Event()

    def read(real, size=1):
        if gone.wait(0.02):
            raise serial.SerialException("port is gone")
        return real(size)

    patch_port(read=read)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": {"name": "loop", "kind": "serial", "serial": {"device": "loop://"}}})
    await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    gone.set()
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "ended")
    assert m["clean"] is False


# ---- batch 8: option errors name the field, sections, break replies, forwards, deleted profiles ----

async def test_options_carry_the_section_titles(client):
    from termdeck.profiles import SECTION_TITLES

    await login(client)
    data = await (await client.get("/api/options")).json()
    assert data["sections"] == SECTION_TITLES
    assert {"profile", "ssh", "serial", "terminal", "forwards"} <= set(data["sections"])


@pytest.mark.parametrize("body, field", [
    ({"name": "x", "kind": "ssh", "ssh": {"host": ""}}, "ssh.host"),
    ({"name": "x", "kind": "ssh", "ssh": {"host": "h", "port": 70000}}, "ssh.port"),
    ({"name": "x", "kind": "serial", "serial": {"device": "http://x"}}, "serial.device"),
    ({"name": "x", "kind": "serial", "serial": {"device": "loop://"}, "terminal": {"colours": "red=zzz"}},
     "terminal.colours"),
    ({"name": "x", "kind": "ssh", "ssh": {"host": "h", "no_shell": True, "remote_command": "ls"}},
     "ssh.remote_command"),
])
async def test_an_invalid_option_is_a_400_naming_the_field(client, body, field):
    await login(client)
    r = await client.post("/api/profiles", json=body)
    assert r.status == 400
    data = await r.json()
    assert data["field"] == field and data["error"]
    assert (await (await client.get("/api/profiles")).json())["profiles"] == []


async def test_updating_with_an_invalid_option_names_the_field_and_keeps_the_profile(client):
    await login(client)
    body = {"name": "box", "kind": "ssh", "ssh": {"host": "box.example"}}
    assert (await client.post("/api/profiles", json=body)).status == 200
    body["ssh"]["host"] = ""
    r = await client.put("/api/profiles/box", json=body)
    assert r.status == 400 and (await r.json())["field"] == "ssh.host"
    [kept] = (await (await client.get("/api/profiles")).json())["profiles"]
    assert kept["ssh"]["host"] == "box.example"


async def test_a_400_without_a_field_has_no_field_key(client):
    await login(client)
    r = await client.post("/api/profiles", json=[1, 2])
    assert r.status == 400 and "field" not in await r.json()


async def test_break_message_is_answered_with_the_result(client):
    await login(client)
    await client.post("/api/profiles", json={"name": "loop", "kind": "serial", "serial": {"device": "loop://"}})
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "name": "loop"})
    await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    await ws.send_json({"type": "break"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "break")
    assert m["ok"] is True and m["message"] == "Break sent"
    await ws.close()


async def test_break_on_an_ssh_session_is_refused_with_a_message(client, ssh_server):
    await login(client)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": ssh_server.profile().to_dict()})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "hostkey")
    await ws.send_json({"type": "answer", "id": m["id"], "value": True})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "prompt")
    await ws.send_json({"type": "answer", "id": m["id"], "value": "secret"})
    await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    await ws.send_json({"type": "break"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "break")
    assert m["ok"] is False and m["message"]
    await ws.close()


async def test_break_reports_a_device_that_went_away(client, patch_port):
    import serial

    await login(client)
    await client.post("/api/profiles", json={"name": "loop", "kind": "serial", "serial": {"device": "loop://"}})
    ports = patch_port()

    def refuse(*a, **k):
        raise serial.SerialException("device vanished")

    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "name": "loop"})
    await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    port, _kwargs = ports[0]
    port.send_break = refuse
    await ws.send_json({"type": "break"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "break")
    assert m["ok"] is False and m["message"].startswith("Break not sent: ") and "device vanished" in m["message"]
    await ws.close()


async def test_opening_a_profile_deleted_meanwhile_fails_with_a_reason(client):
    await login(client)
    await client.post("/api/profiles", json={"name": "loop", "kind": "serial", "serial": {"device": "loop://"}})
    assert (await client.delete("/api/profiles/loop")).status == 200
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "name": "loop"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] != "connecting")
    assert m["state"] == "failed" and "deleted or renamed" in m["message"]
    await ws.close()


async def test_connected_message_lists_the_forwards_and_their_summary(client, ssh_server):
    import asyncio

    busy = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    busy_port = busy.sockets[0].getsockname()[1]
    profile = ssh_server.profile()
    from termdeck.profiles import Forward

    profile.ssh.forwards = [Forward("local", 0, "127.0.0.1", "127.0.0.1", 9),
                            Forward("local", busy_port, "127.0.0.1", "127.0.0.1", 9)]
    await login(client)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": profile.to_dict()})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "hostkey")
    await ws.send_json({"type": "answer", "id": m["id"], "value": True})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "prompt")
    await ws.send_json({"type": "answer", "id": m["id"], "value": "secret"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    ok, failed = m["forwards"]
    assert ok["active"] is True and ok["port"] and ok["kind"] == "local" and "listening on port" in ok["text"]
    assert failed["active"] is False and failed["port"] is None and failed["error"]
    assert "failed" in failed["text"]
    assert m["forwards_summary"] == "1 of 2 forwards failed"
    await ws.close()
    busy.close()


async def test_connected_message_without_forwards_has_an_empty_summary(client):
    await login(client)
    await client.post("/api/profiles", json={"name": "loop", "kind": "serial", "serial": {"device": "loop://"}})
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "name": "loop"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "state" and m["state"] == "connected")
    assert m["forwards"] == [] and m["forwards_summary"] == ""
    await ws.close()


# ---- open_in_browser: the start link goes through a private file, not the command line ----

async def test_open_in_browser_writes_a_private_page_and_removes_it_later(tmp_path, monkeypatch):
    import json
    import stat
    import sys
    import tempfile
    import webbrowser
    from pathlib import Path
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    from termdeck.web.server import open_in_browser

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url) or True)
    url = "http://127.0.0.1:5000/#token=abc_DEF-123"
    page = open_in_browser(url, keep=0.1)
    assert page.parent == tmp_path and page.exists()
    if sys.platform != "win32":
        assert stat.S_IMODE(page.stat().st_mode) == 0o600
    text = page.read_text(encoding="utf-8")
    assert json.dumps(url) in text and "location.replace(" in text
    assert 'name="referrer" content="no-referrer"' in text
    [uri] = opened
    assert uri.startswith("file://") and Path(url2pathname(urlparse(uri).path)) == page
    assert url not in uri  # the link is never on the browser's command line

    async def gone():
        while page.exists():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(gone(), 5)


async def test_open_in_browser_escapes_the_url_inside_the_script(tmp_path, monkeypatch):
    import tempfile
    import webbrowser

    from termdeck.web.server import open_in_browser

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: True)
    page = open_in_browser('http://127.0.0.1:1/#a");alert(1);("', keep=60)
    try:
        assert 'a\\");alert(1);(\\"' in page.read_text(encoding="utf-8")  # a string literal, not code
    finally:
        page.unlink()
