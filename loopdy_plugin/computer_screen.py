"""See and control this computer's own desktop in the bighelp app, through the computer's VNC server.

The plugin only relays bytes. A VNC (RFB) server on this computer (macOS Screen Sharing, x11vnc,
TightVNC, ...) listens on 127.0.0.1; the app speaks RFB to it through a WebSocket of this plugin and
answers the server's own password (VNC auth) itself. Nothing here reads or stores that password.

- `status`: whether a VNC server answers on the port, from its 12-byte RFB banner.
- `mint_ticket` / `redeem_ticket`: `computer-screen/observe` hands out a single-use ticket (30 s, at most
  16 open). The WebSocket takes only a live ticket. Hermes' WebSocket gate for plugin sockets checks the
  Host, Origin and peer and nothing else (HTTP middleware never sees an upgrade), so the ticket is the
  credential, and it is only minted for a request that passed the dashboard's own sign-in.
- `bridge`: splices the accepted WebSocket and a TCP connection to the VNC server, as binary messages of at
  most 64 KB, like Hermes' own display bridge (`hermes_cli/web_routers/display.py`).

Responses and logs carry fixed words only: never the port, an address or exception text.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import socket
import threading
import time
from typing import Callable

from fastapi import WebSocket, status


CAPABILITY = "native-computer-screen-v1"
SOCKET_PATH = "/api/plugins/loopdy/native/computer-screen/ws"
PORT_ENV = "BIGHELP_COMPUTER_SCREEN_PORT"
DEFAULT_PORT = 5900
HOST = "127.0.0.1"
PROBE_TIMEOUT_SECONDS = 1.0
PROBE_CACHE_SECONDS = 2.0
TICKET_SECONDS = 30.0
MAX_TICKETS = 16
MAX_BRIDGES = 4
CHUNK_BYTES = 64 * 1024
# The server sends its banner first, so a slow connect or banner means this isn't a VNC server for us.
CONNECT_TIMEOUT_SECONDS = 3.0
BANNER = re.compile(rb"RFB [0-9]{3}\.[0-9]{3}\n\Z")
BANNER_BYTES = 12

logger = logging.getLogger("hermes.plugins.bighelp")

_probe_cache: tuple[int, float, dict] | None = None
_probe_lock = threading.Lock()
_tickets: dict[str, float] = {}
_tickets_lock = threading.Lock()
_bridges = 0
_bridges_lock = threading.Lock()
# Tests move the clock.
_clock: Callable[[], float] = time.monotonic


def port() -> int:
    """The VNC server's port on this computer: `BIGHELP_COMPUTER_SCREEN_PORT`, else 5900."""
    try:
        value = int(os.environ.get(PORT_ENV, "").strip() or DEFAULT_PORT)
    except ValueError:
        return DEFAULT_PORT
    return value if 1 <= value <= 65535 else DEFAULT_PORT


def os_family() -> str:
    from .host_telemetry import _os_family
    return _os_family()


def _probe(target: int) -> str | None:
    """None when an RFB server answers on `target`, else the fixed reason it doesn't."""
    try:
        connection = socket.create_connection((HOST, target), timeout=PROBE_TIMEOUT_SECONDS)
    except OSError:
        return "no_server"
    try:
        connection.settimeout(PROBE_TIMEOUT_SECONDS)
        banner = b""
        while len(banner) < BANNER_BYTES:
            piece = connection.recv(BANNER_BYTES - len(banner))
            if not piece:
                break
            banner += piece
    except OSError:
        return "not_vnc"
    finally:
        connection.close()
    return None if BANNER.fullmatch(banner) else "not_vnc"


def status_value() -> dict:
    """`{"available", "os", "reason"}`, read at most every 2 seconds."""
    global _probe_cache
    target = port()
    moment = _clock()
    with _probe_lock:
        cached = _probe_cache
        if cached and cached[0] == target and 0 <= moment - cached[1] < PROBE_CACHE_SECONDS:
            return cached[2]
        reason = _probe(target)
        value = {"available": reason is None, "os": os_family(), "reason": reason}
        _probe_cache = (target, moment, value)
        return value


def mint_ticket() -> str:
    """A single-use ticket for the WebSocket. At most 16 stay open: the oldest goes first."""
    ticket = secrets.token_urlsafe(32)
    moment = _clock()
    with _tickets_lock:
        for key in [key for key, expires in _tickets.items() if expires <= moment]:
            del _tickets[key]
        while len(_tickets) >= MAX_TICKETS:
            del _tickets[min(_tickets, key=_tickets.__getitem__)]
        _tickets[ticket] = moment + TICKET_SECONDS
    return ticket


def redeem_ticket(candidate: str) -> bool:
    """Spends `candidate` when it is a live ticket. Compares against every open ticket in constant time."""
    moment = _clock()
    given = candidate.encode("utf-8", "replace")
    found = None
    with _tickets_lock:
        for ticket, expires in list(_tickets.items()):
            if hmac.compare_digest(ticket.encode("ascii"), given) and found is None:
                found = ticket, expires
        if found is None:
            return False
        del _tickets[found[0]]
        return found[1] > moment


def forget() -> None:
    """Drops the probe cache and every ticket (tests)."""
    global _probe_cache
    with _probe_lock:
        _probe_cache = None
    with _tickets_lock:
        _tickets.clear()


def _take_bridge() -> bool:
    global _bridges
    with _bridges_lock:
        if _bridges >= MAX_BRIDGES:
            return False
        _bridges += 1
        return True


def _drop_bridge() -> None:
    global _bridges
    with _bridges_lock:
        _bridges = max(0, _bridges - 1)


async def _close(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code=code)
    except Exception:
        pass  # the peer already closed


async def _open_server() -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes] | None:
    """A connection to the VNC server and its banner, or None when nothing RFB answers."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(HOST, port()), CONNECT_TIMEOUT_SECONDS)
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        banner = await asyncio.wait_for(reader.readexactly(BANNER_BYTES), CONNECT_TIMEOUT_SECONDS)
    except (OSError, asyncio.IncompleteReadError, asyncio.TimeoutError):
        banner = b""
    if not BANNER.fullmatch(banner):
        writer.close()
        return None
    return reader, writer, banner


async def bridge(ws: WebSocket) -> None:
    """The WebSocket route: a live ticket in the query, then raw RFB bytes both ways.

    The socket is accepted first so the app reads the close code: 1008 bad ticket, 1013 too many bridges,
    1011 no VNC server answers, 1003 a text message, 1000 when the VNC server ends.
    """
    await ws.accept()
    if not redeem_ticket(ws.query_params.get("ticket", "")):
        logger.info("computer screen: ticket refused")
        await _close(ws, status.WS_1008_POLICY_VIOLATION)
        return
    if not _take_bridge():
        logger.info("computer screen: too many viewers")
        await _close(ws, status.WS_1013_TRY_AGAIN_LATER)
        return
    writer = None
    try:
        server = await _open_server()
        if server is None:
            logger.info("computer screen: no VNC server")
            await _close(ws, status.WS_1011_INTERNAL_ERROR)
            return
        reader, writer, banner = server
        code = await _relay(ws, reader, writer, banner)
        await _close(ws, code)
    finally:
        _drop_bridge()
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def _relay(ws: WebSocket, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, banner: bytes) -> int:
    """Pumps both ways until one side ends. Returns the close code for the WebSocket."""
    async def to_viewer() -> int:
        await ws.send_bytes(banner)
        while True:
            chunk = await reader.read(CHUNK_BYTES)
            if not chunk:
                return status.WS_1000_NORMAL_CLOSURE
            await ws.send_bytes(chunk)  # awaiting the send is the backpressure toward the server

    async def to_server() -> int:
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return status.WS_1000_NORMAL_CLOSURE
            data = message.get("bytes")
            if data is None:
                return status.WS_1003_UNSUPPORTED_DATA
            writer.write(data)
            await writer.drain()  # backpressure toward the viewer

    tasks = [asyncio.create_task(to_viewer()), asyncio.create_task(to_server())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        code = status.WS_1000_NORMAL_CLOSURE
        for task in done:
            if task.cancelled() or task.exception() is not None:
                continue
            code = task.result()
        return code
    finally:
        for task in tasks:
            task.cancel()
