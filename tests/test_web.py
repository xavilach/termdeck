import asyncio

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
    assert [p["name"] for p in await (await client.get("/api/profiles")).json()] == ["board2"]
    bad = {"name": "x", "kind": "serial", "serial": {"device": "d", "parity": "Q"}}
    assert (await client.post("/api/profiles", json=bad)).status == 400
    assert (await client.delete("/api/profiles/board2")).status == 200
    assert (await client.delete("/api/profiles/board2")).status == 404


async def test_options(client):
    await login(client)
    data = await (await client.get("/api/options")).json()
    assert {"profile", "ssh", "serial", "terminal"} <= set(data["options"])
    assert data["defaults"]["serial"]["baudrate"] == 115200


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
    m, out = await _receive_until(ws, lambda m, _: m and m["type"] == "connected")
    assert m["sftp"] is True and m["can_break"] is False
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
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "closed")
    await ws.close()
    assert (await client.get(f"/api/sessions/{sid}/sftp")).status == 404


async def test_rejected_host_key_reports_error(client, ssh_server):
    await login(client)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "profile": ssh_server.profile().to_dict()})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "hostkey")
    await ws.send_json({"type": "answer", "id": m["id"], "value": False})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "error")
    assert m["message"] == "Host key not trusted, connection aborted"


async def test_saved_serial_profile_and_break(client):
    await login(client)
    await client.post("/api/profiles", json={"name": "loop", "kind": "serial", "serial": {"device": "loop://"}})
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "name": "loop"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "connected")
    assert m["can_break"] is True and m["sftp"] is False
    await ws.send_json({"type": "break"})
    await ws.send_json({"type": "input", "data": "AT\r"})
    _, out = await _receive_until(ws, lambda m, out: b"AT\r" in out)
    await ws.close()


async def test_unknown_profile_reports_error(client):
    await login(client)
    ws = await client.ws_connect("/api/terminal")
    await ws.send_json({"type": "open", "name": "nope"})
    m, _ = await _receive_until(ws, lambda m, _: m and m["type"] == "error")
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
    await _receive_until(ws, lambda m, _: m and m["type"] == "connected")
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
