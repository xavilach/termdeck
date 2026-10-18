"""Local web UI: profiles API, terminals over WebSocket, SFTP browser.

Listens on localhost only, and the Host header must be local (blocks DNS
rebinding). Authentication:

- The start URL carries a one-time token in its #fragment, so it never reaches
  server logs. The page exchanges it for a random per-tab secret (POST /api/login).
- The secret goes in the X-Termdeck-Auth header, or as a WebSocket subprotocol.
  No cookie: cookies are shared by every port on 127.0.0.1.
- Downloads use single-use tickets that expire quickly, so no secret ends up
  in the browser's download history.
"""

import asyncio
import hmac
import itertools
import json
import re
import secrets
import stat
import time
from urllib.parse import quote
from dataclasses import asdict
from pathlib import Path, PurePosixPath

import asyncssh
from aiohttp import web

from ..profiles import (
    Profile, ProfileFileError, ProfileStore, SerialSettings, SSHSettings, TerminalSettings, describe_options,
    resolve_target,
)
from ..serialport import available_ports
from ..session import HostKey, Session, describe_error, open_session

STATIC = Path(__file__).parent / "static"
AUTH_HEADER = "X-Termdeck-Auth"
WS_PROTOCOL = "termdeck"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
CHUNK = 256 * 1024
MAX_TERMINAL_SIZE = 1000
SECURITY_HEADERS = {
    # Inline styles: xterm.js and the UI set style attributes.
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def _match(given: str, candidates) -> str | None:
    given_bytes = given.encode()
    for candidate in candidates:
        if hmac.compare_digest(given_bytes, candidate.encode()):
            return candidate
    return None


class Auth:
    """One-time start links, per-tab secrets and single-use download tickets."""

    TICKET_TTL = 60
    LINK_TTL = 600

    def __init__(self):
        self._links: dict[str, float] = {}
        self._secrets: set[str] = set()
        self._tickets: dict[str, tuple[str, str, float]] = {}

    def new_link(self, token: str | None = None) -> str:
        token = token or secrets.token_urlsafe(24)
        self._links[token] = time.monotonic() + self.LINK_TTL
        return token

    def login(self, token: str) -> str | None:
        """Consume a start token and return a new secret, or None."""
        if not (found := _match(token, self._links)):
            return None
        if self._links.pop(found) < time.monotonic():
            return None
        secret = secrets.token_urlsafe(32)
        self._secrets.add(secret)
        return secret

    def check(self, secret: str | None) -> bool:
        return bool(secret) and _match(secret, self._secrets) is not None

    def new_ticket(self, sid: str, path: str) -> str:
        now = time.monotonic()
        self._tickets = {t: v for t, v in self._tickets.items() if v[2] > now}
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = (sid, path, now + self.TICKET_TTL)
        return ticket

    def use_ticket(self, ticket: str) -> tuple[str, str] | None:
        if not (found := _match(ticket, self._tickets)):
            return None
        sid, path, expires = self._tickets.pop(found)
        return (sid, path) if expires > time.monotonic() else None


store_key = web.AppKey("store", ProfileStore)
auth_key = web.AppKey("auth", Auth)
sessions_key = web.AppKey("sessions", dict)


class WebPrompter:
    """Forwards connection questions to the browser and waits for the answer."""

    def __init__(self, ws: web.WebSocketResponse):
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}

    async def _request(self, message: dict):
        qid = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[qid] = future
        try:
            await self._ws.send_json({**message, "id": qid})
            return await future
        finally:
            self._pending.pop(qid, None)

    def answer(self, qid, value) -> None:
        if isinstance(qid, int) and (future := self._pending.get(qid)) and not future.done():
            future.set_result(value)

    def cancel_all(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_result(None)

    async def ask(self, prompt: str, secret: bool = True) -> str | None:
        answer = await self._request({"type": "prompt", "text": prompt, "secret": secret})
        return answer if isinstance(answer, str) else None  # anything else from the page is a cancel

    async def confirm_host_key(self, key: HostKey) -> bool:
        answer = await self._request({"type": "hostkey", **asdict(key)})
        return answer is True

    async def notify(self, text: str) -> None:
        try:
            await self._ws.send_json({"type": "notice", "text": text})
        except (ConnectionResetError, RuntimeError):
            pass  # the page went away; the connection attempt is being cancelled anyway


def _is_public(path: str) -> bool:
    return path in ("/", "/api/login") or path.startswith(("/static/", "/api/download/"))


def _ws_secret(request: web.Request) -> str | None:
    for protocol in request.headers.get("Sec-WebSocket-Protocol", "").split(","):
        protocol = protocol.strip()
        if protocol.startswith("auth."):
            return protocol[5:]
    return None


@web.middleware
async def security(request: web.Request, handler):
    if request.host.rsplit(":", 1)[0] not in LOCAL_HOSTS:
        raise web.HTTPForbidden(text="termdeck only answers on localhost")
    origin = request.headers.get("Origin")
    if origin and origin != f"http://{request.host}":
        raise web.HTTPForbidden(text="cross-origin request refused")
    if not _is_public(request.path):
        secret = request.headers.get(AUTH_HEADER) or _ws_secret(request)
        if not request.app[auth_key].check(secret):
            return _json_error(401, "not logged in: open termdeck with the link it printed")
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (KeyError, ValueError, TypeError) as exc:
        # Malformed requests (missing or wrongly typed fields) are the client's fault.
        message = f"missing {exc.args[0]}" if isinstance(exc, KeyError) else str(exc)
        return _json_error(400, message)


async def _security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.update(SECURITY_HEADERS)


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


async def login(request: web.Request) -> web.Response:
    body = await request.json()
    secret = request.app[auth_key].login(str(body["token"]))
    if secret is None:
        return _json_error(403, "this link was already used or is not valid: open a new one from termdeck")
    return web.json_response({"secret": secret})


async def new_link(request: web.Request) -> web.Response:
    """A fresh one-time start link, for opening another window."""
    token = request.app[auth_key].new_link()
    return web.json_response({"url": f"/#token={token}"})


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def get_options(request: web.Request) -> web.Response:
    defaults = {
        "ssh": asdict(SSHSettings()),
        "serial": asdict(SerialSettings()),
        "terminal": asdict(TerminalSettings()),
    }
    return web.json_response({"options": describe_options(), "defaults": defaults})


async def list_profiles(request: web.Request) -> web.Response:
    """Saved profiles, plus a line for each entry of the file that could not be used."""
    store = request.app[store_key]
    try:
        profiles = store.load().values()
    except ProfileFileError as exc:  # the page still works for quick connect
        return web.json_response({"profiles": [], "warnings": [], "error": str(exc)})
    return web.json_response({
        "profiles": [{**p.to_dict(), "summary": p.summary()} for p in profiles],
        "warnings": store.warnings(),
        "path": str(store.path),
    })


async def save_profile(request: web.Request) -> web.Response:
    """POST creates, PUT /api/profiles/{name} updates (and renames)."""
    old_name = request.match_info.get("name")
    store = request.app[store_key]
    try:
        profile = Profile.from_dict(await request.json())
        # In a thread: the store takes a file lock and scans USB ports for serial profiles.
        if old_name is None:
            await asyncio.to_thread(store.add, profile)
        else:
            await asyncio.to_thread(store.update, old_name, profile)
    except KeyError as exc:
        return _json_error(404, str(exc.args[0]))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return _json_error(400, str(exc))
    except OSError as exc:
        return _json_error(500, f"could not save: {describe_error(exc)}")
    return web.json_response({**profile.to_dict(), "summary": profile.summary()})


async def delete_profile(request: web.Request) -> web.Response:
    try:
        await asyncio.to_thread(request.app[store_key].delete, request.match_info["name"])
    except KeyError as exc:
        return _json_error(404, str(exc.args[0]))
    except ValueError as exc:
        return _json_error(400, str(exc))
    except OSError as exc:
        return _json_error(500, f"could not delete: {describe_error(exc)}")
    return web.json_response({"ok": True})


async def quick_target(request: web.Request) -> web.Response:
    """Resolve quick-connect text with the core rules: a saved name or an unsaved profile."""
    text = str((await request.json())["text"])
    try:
        profiles = request.app[store_key].load()
    except ProfileFileError:  # quick targets work without the profile file (the sidebar says why)
        profiles = {}
    try:
        profile = resolve_target(text, profiles)
    except KeyError as exc:
        return _json_error(400, exc.args[0])
    return web.json_response({**profile.to_dict(), "summary": profile.summary(), "saved": text.strip() in profiles})


async def serial_ports(request: web.Request) -> web.Response:
    return web.json_response(await asyncio.to_thread(available_ports))


def _terminal_size(message: dict) -> tuple[int, int]:
    def clamp(value, default):
        try:
            return min(max(1, int(value)), MAX_TERMINAL_SIZE)
        except (TypeError, ValueError):
            return default

    return clamp(message.get("cols"), 80), clamp(message.get("rows"), 24)


async def terminal(request: web.Request) -> web.WebSocketResponse:
    """One terminal tab.

    Client sends JSON text: open (first), input, resize, answer, break.
    Server sends binary frames of UTF-8 output and JSON text for
    prompt, hostkey, connected, error and closed.
    """
    ws = web.WebSocketResponse(heartbeat=30, protocols=(WS_PROTOCOL,))
    await ws.prepare(request)
    prompter = WebPrompter(ws)
    session: Session | None = None
    sessions = request.app[sessions_key]
    sid = secrets.token_urlsafe(12)

    async def handle_messages():
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
                kind = m.get("type")
                if kind == "answer":
                    prompter.answer(m.get("id"), m.get("value"))
                elif session is None:
                    continue
                elif kind == "input":
                    await session.write(str(m["data"]).encode("utf-8", "surrogatepass"))
                elif kind == "resize":
                    session.resize(*_terminal_size(m))
                elif kind == "break" and session.can_break:
                    await session.send_break()
            except (ValueError, TypeError, KeyError, AttributeError):
                continue  # malformed message: ignore it, keep the session
        prompter.cancel_all()

    try:
        first = await ws.receive_json(timeout=30)
        if first.get("type") != "open":
            raise ValueError("expected an open message")
        if first.get("name"):
            profile = request.app[store_key].get(first["name"])
        else:
            profile = Profile.from_dict(first["profile"])
        cols, rows = _terminal_size(first)
    except Exception as exc:
        await ws.send_json({"type": "error", "message": describe_error(exc)})
        await ws.close()
        return ws

    reader = asyncio.create_task(handle_messages())
    connect = asyncio.create_task(
        open_session(profile, prompter, cols, rows)
    )
    try:
        await asyncio.wait({reader, connect}, return_when=asyncio.FIRST_COMPLETED)
        if not connect.done():  # browser went away while connecting
            connect.cancel()
            await asyncio.gather(connect, return_exceptions=True)
            return ws
        try:
            session = connect.result()
        except Exception as exc:
            await ws.send_json({"type": "error", "message": describe_error(exc)})
            return ws

        sessions[sid] = session
        await ws.send_json({
            "type": "connected",
            "session": sid,
            "title": session.title,
            "kind": profile.kind,
            "can_break": session.can_break,
            "sftp": profile.kind == "ssh",
        })

        async def pump():
            while data := await session.read():
                await ws.send_bytes(data)

        output = asyncio.create_task(pump())
        await asyncio.wait({reader, output}, return_when=asyncio.FIRST_COMPLETED)
        if output.done() and not ws.closed:
            reason = session.end_reason
            await ws.send_json({"type": "closed", "message": f"Session ended: {reason}" if reason else "Session ended"})
        output.cancel()
        await asyncio.gather(output, return_exceptions=True)
    finally:
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
        sessions.pop(sid, None)
        if session:
            await session.close()
        await ws.close()
    return ws


# SFTP, reusing the authenticated connection of an open SSH tab.

async def _sftp(request: web.Request, sid: str | None = None) -> asyncssh.SFTPClient:
    session = request.app[sessions_key].get(sid or request.match_info["sid"])
    connection = getattr(getattr(session, "inner", None), "connection", None)
    if connection is None:
        raise web.HTTPNotFound(text="no SSH session with this id")
    return await connection.sftp_client()


def _entry(name: str, attrs: asyncssh.SFTPAttrs) -> dict:
    mode = attrs.permissions or 0
    return {
        "name": name,
        "dir": stat.S_ISDIR(mode),
        "link": stat.S_ISLNK(mode),
        "size": attrs.size or 0,
        "mtime": attrs.mtime or 0,
        "mode": stat.filemode(mode) if mode else "",
    }


async def sftp_list(request: web.Request) -> web.Response:
    sftp = await _sftp(request)
    try:
        path = await sftp.realpath(request.query.get("path") or ".")
        entries = [
            _entry(e.filename, e.attrs)
            for e in await sftp.readdir(path)
            if e.filename not in (".", "..")
        ]
    except asyncssh.SFTPError as exc:
        return _json_error(400, exc.reason)
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return web.json_response({"path": path, "entries": entries})


def _content_disposition(name: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f]", "", name) or "download"
    fallback = name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_").replace("\\", "_")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


async def sftp_ticket(request: web.Request) -> web.Response:
    """Single-use download URL, so the browser can download without holding a secret."""
    sftp = await _sftp(request)  # 404 now rather than at download time
    path = str((await request.json())["path"])
    try:
        # Open it now so errors (missing, permission denied, a directory) become a
        # message here: the browser's download UI would hide them.
        async with await sftp.open(path, "rb"):
            pass
    except asyncssh.SFTPError as exc:
        return _json_error(400, exc.reason)
    except (asyncssh.Error, OSError) as exc:
        return _json_error(400, describe_error(exc))
    ticket = request.app[auth_key].new_ticket(request.match_info["sid"], path)
    return web.json_response({"url": f"/api/download/{ticket}"})


async def sftp_download(request: web.Request) -> web.StreamResponse:
    found = request.app[auth_key].use_ticket(request.match_info["ticket"])
    if found is None:
        return _json_error(403, "download link expired, try again")
    sid, path = found
    sftp = await _sftp(request, sid)
    try:
        size = (await sftp.stat(path)).size
        remote = await sftp.open(path, "rb")
    except asyncssh.SFTPError as exc:
        return _json_error(400, exc.reason)
    response = web.StreamResponse(headers={
        "Content-Disposition": _content_disposition(PurePosixPath(path).name),
        "Content-Type": "application/octet-stream",
    })
    if size is not None:
        response.content_length = size
    await response.prepare(request)
    async with remote:
        while chunk := await remote.read(CHUNK):
            await response.write(chunk)
    await response.write_eof()
    return response


async def sftp_upload(request: web.Request) -> web.Response:
    sftp = await _sftp(request)
    directory = request.query["path"]
    reader = await request.multipart()
    names = []
    try:
        while part := await reader.next():
            name = PurePosixPath(part.filename or "").name
            if not name:
                continue
            async with sftp.open(str(PurePosixPath(directory) / name), "wb") as remote:
                while chunk := await part.read_chunk(CHUNK):
                    await remote.write(chunk)
            names.append(name)
    except asyncssh.SFTPError as exc:
        return _json_error(400, exc.reason)
    return web.json_response({"uploaded": names})


async def sftp_operation(request: web.Request) -> web.Response:
    sftp = await _sftp(request)
    body = await request.json()
    op, path = body.get("op"), body.get("path", "")
    try:
        if op == "mkdir":
            await sftp.mkdir(path)
        elif op == "rename":
            await sftp.rename(path, body["to"])
        elif op == "remove":
            await sftp.remove(path)
        elif op == "rmdir":
            await sftp.rmdir(path)
        else:
            return _json_error(400, f"unknown operation {op!r}")
    except asyncssh.SFTPError as exc:
        return _json_error(400, exc.reason)
    return web.json_response({"ok": True})


def create_app(store: ProfileStore | None = None, token: str | None = None) -> web.Application:
    """`token` registers a known start token (tests); otherwise use new_start_url."""
    app = web.Application(middlewares=[security], client_max_size=1024**2)
    app.on_response_prepare.append(_security_headers)
    app[store_key] = store or ProfileStore()
    app[auth_key] = Auth()
    if token:
        app[auth_key].new_link(token)
    app[sessions_key] = {}
    app.router.add_get("/", index)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/link", new_link)
    app.router.add_static("/static", STATIC)
    app.router.add_get("/api/options", get_options)
    app.router.add_get("/api/profiles", list_profiles)
    app.router.add_post("/api/profiles", save_profile)
    app.router.add_put("/api/profiles/{name}", save_profile)
    app.router.add_delete("/api/profiles/{name}", delete_profile)
    app.router.add_get("/api/serial-ports", serial_ports)
    app.router.add_post("/api/target", quick_target)
    app.router.add_get("/api/terminal", terminal)
    app.router.add_get("/api/sessions/{sid}/sftp", sftp_list)
    app.router.add_post("/api/sessions/{sid}/sftp/ticket", sftp_ticket)
    app.router.add_get("/api/download/{ticket}", sftp_download)
    app.router.add_post("/api/sessions/{sid}/sftp/upload", sftp_upload)
    app.router.add_post("/api/sessions/{sid}/sftp/op", sftp_operation)
    return app


async def start(port: int = 0, store: ProfileStore | None = None) -> tuple[web.AppRunner, str]:
    """Start the server on localhost and return (runner, start URL)."""
    app = create_app(store)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    actual = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{actual}/#token={app[auth_key].new_link()}"


def serve(port: int = 0, open_browser: bool = True) -> None:
    """Run the web UI until interrupted."""
    import webbrowser

    async def main():
        runner, url = await start(port)
        print(f"termdeck web UI (link works once, within 10 minutes): {url}", flush=True)
        if open_browser:
            webbrowser.open(url)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
