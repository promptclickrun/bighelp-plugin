"""The computer-screen relay: the status probe, the one-use tickets and the WebSocket bridge, against fake
local servers that behave like a VNC server (they speak first, with an RFB banner)."""
from __future__ import annotations

from contextlib import contextmanager
import os
import socket
import threading
import unittest
import uuid
from unittest.mock import patch

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from loopdy_plugin import computer_screen, native_api
from test_agents_activity import route_client
from test_native_api import PREFIX

BANNER = b"RFB 003.008\n"
SOCKET = PREFIX + "/computer-screen/ws"


class FakeServer:
    """Listens on a free 127.0.0.1 port. `greeting` is sent on connect; with `echo` it then sends back
    what it receives, so a test sees the bytes that went through the bridge."""

    def __init__(self, greeting: bytes | None = BANNER, echo: bool = True):
        self.greeting, self.echo = greeting, echo
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.connections: list[socket.socket] = []
        self.closed = threading.Event()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            self.connections.append(connection)
            threading.Thread(target=self._serve, args=(connection,), daemon=True).start()

    def _serve(self, connection):
        try:
            if self.greeting:
                connection.sendall(self.greeting)
            while True:
                data = connection.recv(65536)
                if not data:
                    self.closed.set()
                    return
                if self.echo:
                    connection.sendall(data)
        except OSError:
            self.closed.set()

    def hang_up(self):
        for connection in self.connections:
            connection.close()

    def stop(self):
        self.listener.close()
        self.hang_up()


@contextmanager
def server_on(**kwargs):
    server = FakeServer(**kwargs)
    with patch.dict(os.environ, {computer_screen.PORT_ENV: str(server.port)}):
        try:
            yield server
        finally:
            server.stop()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ComputerScreenTestCase(unittest.TestCase):
    def setUp(self):
        computer_screen.forget()
        self.addCleanup(computer_screen.forget)


class StatusTests(ComputerScreenTestCase):
    def test_no_server(self):
        with patch.dict(os.environ, {computer_screen.PORT_ENV: str(free_port())}):
            value = computer_screen.status_value()
        self.assertEqual((value["available"], value["reason"]), (False, "no_server"))
        self.assertIn(value["os"], {"macos", "linux", "windows", "other"})

    def test_an_rfb_server_is_available_and_the_answer_hides_the_port(self):
        with server_on() as server:
            value = computer_screen.status_value()
        self.assertEqual(value, {"available": True, "os": computer_screen.os_family(), "reason": None})
        self.assertNotIn(str(server.port), repr(value))

    def test_a_server_that_is_not_vnc(self):
        for greeting in (b"HTTP/1.1 400 Bad Request\r\n\r\n", b"SSH-2.0-test\r\n", b"RFB 3.8\n", None):
            computer_screen.forget()
            with server_on(greeting=greeting, echo=False):
                with patch.object(computer_screen, "PROBE_TIMEOUT_SECONDS", 0.2):
                    value = computer_screen.status_value()
            self.assertEqual((value["available"], value["reason"]), (False, "not_vnc"), greeting)

    def test_the_answer_is_kept_for_two_seconds(self):
        clock = [100.0]
        with patch.object(computer_screen, "_clock", lambda: clock[0]), server_on():
            self.assertTrue(computer_screen.status_value()["available"])
        with patch.object(computer_screen, "_clock", lambda: clock[0]), \
                patch.dict(os.environ, {computer_screen.PORT_ENV: str(computer_screen._probe_cache[0])}):
            self.assertTrue(computer_screen.status_value()["available"])
            clock[0] += 2.1
            self.assertFalse(computer_screen.status_value()["available"])

    def test_the_port_comes_from_the_environment_and_a_bad_value_means_5900(self):
        for text, expected in (("", 5900), ("6000", 6000), ("0", 5900), ("65536", 5900), ("abc", 5900), (" 5901 ", 5901)):
            with patch.dict(os.environ, {computer_screen.PORT_ENV: text}):
                self.assertEqual(computer_screen.port(), expected, text)


class TicketTests(ComputerScreenTestCase):
    def test_a_ticket_is_url_safe_random_and_used_once(self):
        ticket = computer_screen.mint_ticket()
        self.assertRegex(ticket, r"^[A-Za-z0-9_-]{43}$")
        self.assertNotEqual(ticket, computer_screen.mint_ticket())
        self.assertTrue(computer_screen.redeem_ticket(ticket))
        self.assertFalse(computer_screen.redeem_ticket(ticket))
        self.assertFalse(computer_screen.redeem_ticket(""))
        self.assertFalse(computer_screen.redeem_ticket("not-a-ticket-é"))

    def test_a_ticket_expires_after_30_seconds(self):
        clock = [10.0]
        with patch.object(computer_screen, "_clock", lambda: clock[0]):
            fresh, late = computer_screen.mint_ticket(), computer_screen.mint_ticket()
            clock[0] += 29.9
            self.assertTrue(computer_screen.redeem_ticket(fresh))
            clock[0] += 0.2
            self.assertFalse(computer_screen.redeem_ticket(late))

    def test_at_most_16_stay_open_and_the_oldest_goes_first(self):
        clock = [0.0]
        with patch.object(computer_screen, "_clock", lambda: clock[0]):
            tickets = []
            for _ in range(18):
                clock[0] += 0.1
                tickets.append(computer_screen.mint_ticket())
            self.assertEqual([computer_screen.redeem_ticket(ticket) for ticket in tickets],
                             [False, False] + [True] * 16)


class RouteTests(ComputerScreenTestCase):
    def setUp(self):
        super().setUp()
        self.client = route_client(("native-context-v1", computer_screen.CAPABILITY))
        self.addCleanup(self.client.stop)

    def call(self, name, body=None, *, client=None):
        client = client or self.client
        return client.post(PREFIX + "/computer-screen/" + name, json={} if body is None else body,
                           headers={"If-Match": client.etag, "X-Loopdy-Request-ID": str(uuid.uuid4())})

    def test_status_answers_whether_a_server_is_there(self):
        with patch.dict(os.environ, {computer_screen.PORT_ENV: str(free_port())}):
            result = self.call("status")
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"available": False, "os": computer_screen.os_family(), "reason": "no_server"})
        self.assertEqual(result.headers["cache-control"], "no-store")
        computer_screen.forget()
        with server_on() as server:
            result = self.call("status")
        self.assertEqual(result.json()["available"], True)
        self.assertNotIn(str(server.port), result.text)

    def test_observe_gives_a_ticket_and_the_socket_path(self):
        with server_on():
            result = self.call("observe")
        self.assertEqual(result.status_code, 200, result.text)
        value = result.json()
        self.assertEqual(set(value), {"ticket", "path"})
        self.assertEqual(value["path"], "/api/plugins/loopdy/native/computer-screen/ws")
        self.assertRegex(value["ticket"], r"^[A-Za-z0-9_-]{43}$")
        self.assertTrue(computer_screen.redeem_ticket(value["ticket"]))

    def test_observe_without_a_server_is_503_and_mints_nothing(self):
        with patch.dict(os.environ, {computer_screen.PORT_ENV: str(free_port())}):
            result = self.call("observe")
        self.assertEqual((result.status_code, result.json()["error"]["code"]), (503, "computer_screen_unavailable"))
        self.assertEqual(computer_screen._tickets, {})

    def test_the_body_must_be_empty_and_the_usual_guards_apply(self):
        with server_on():
            for name in ("status", "observe"):
                self.assertEqual(self.call(name, {"x": 1}).status_code, 422)
            missing = self.client.post(PREFIX + "/computer-screen/status", json={},
                                       headers={"X-Loopdy-Request-ID": str(uuid.uuid4())})
            self.assertEqual(missing.status_code, 428)
            bare = route_client(("native-context-v1",))
            self.addCleanup(bare.stop)
            for name in ("status", "observe"):
                refused = self.call(name, client=bare)
                self.assertEqual((refused.status_code, refused.json()["error"]["code"]),
                                 (503, "computer_screen_unavailable"))


class BridgeTests(ComputerScreenTestCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(native_api.router, prefix="/api/plugins/loopdy")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def url(self, ticket=None):
        return f"{SOCKET}?ticket={ticket or computer_screen.mint_ticket()}"

    def test_bytes_go_both_ways_starting_with_the_servers_banner(self):
        with server_on():
            with self.client.websocket_connect(self.url()) as ws:
                self.assertEqual(ws.receive_bytes(), BANNER)
                payload = bytes(range(256)) * 600  # 153,600 bytes: more than one 64 KB message
                ws.send_bytes(payload)
                received = b""
                while len(received) < len(payload):
                    chunk = ws.receive_bytes()
                    self.assertLessEqual(len(chunk), computer_screen.CHUNK_BYTES)
                    received += chunk
                self.assertEqual(received, payload)

    def test_a_ticket_opens_one_socket_only(self):
        with server_on():
            ticket = computer_screen.mint_ticket()
            with self.client.websocket_connect(self.url(ticket)) as ws:
                self.assertEqual(ws.receive_bytes(), BANNER)
            with self.client.websocket_connect(self.url(ticket)) as ws:
                self.assertEqual(ws.receive()["code"], 1008)

    def test_a_bad_missing_or_expired_ticket_closes_with_1008(self):
        clock = [0.0]
        with server_on(), patch.object(computer_screen, "_clock", lambda: clock[0]):
            expired = computer_screen.mint_ticket()
            clock[0] += 31
            for url in (self.url("nope"), SOCKET, self.url(expired)):
                with self.client.websocket_connect(url) as ws:
                    self.assertEqual(ws.receive()["code"], 1008, url)

    def test_a_text_message_closes_with_1003_and_the_server_side_too(self):
        with server_on() as server:
            with self.client.websocket_connect(self.url()) as ws:
                ws.receive_bytes()
                ws.send_text("hello")
                self.assertEqual(ws.receive()["code"], 1003)
            self.assertTrue(server.closed.wait(2), "the VNC connection must close with the socket")

    def test_the_server_hanging_up_closes_the_socket(self):
        with server_on() as server:
            with self.client.websocket_connect(self.url()) as ws:
                ws.receive_bytes()
                server.hang_up()
                self.assertEqual(ws.receive()["code"], 1000)

    def test_the_viewer_leaving_closes_the_server_side(self):
        with server_on() as server:
            with self.client.websocket_connect(self.url()) as ws:
                ws.receive_bytes()
            self.assertTrue(server.closed.wait(2))

    def test_no_server_or_a_server_that_is_not_vnc_closes_with_1011(self):
        with patch.dict(os.environ, {computer_screen.PORT_ENV: str(free_port())}):
            with self.client.websocket_connect(self.url()) as ws:
                self.assertEqual(ws.receive()["code"], 1011)
        with server_on(greeting=b"HTTP/1.1 200 OK\r\n\r\n", echo=False):
            with self.client.websocket_connect(self.url()) as ws:
                self.assertEqual(ws.receive()["code"], 1011)
        self.assertEqual(computer_screen._bridges, 0, "a refused bridge gives its place back")

    def test_at_most_4_bridges_at_once_and_a_closed_one_frees_its_place(self):
        with server_on():
            with self.client.websocket_connect(self.url()) as first, \
                    self.client.websocket_connect(self.url()) as second, \
                    self.client.websocket_connect(self.url()) as third, \
                    self.client.websocket_connect(self.url()) as fourth:
                for ws in (first, second, third, fourth):
                    self.assertEqual(ws.receive_bytes(), BANNER)
                with self.client.websocket_connect(self.url()) as fifth:
                    self.assertEqual(fifth.receive()["code"], 1013)
            deadline = threading.Event()
            for _ in range(100):
                if computer_screen._bridges == 0:
                    break
                deadline.wait(0.05)
            self.assertEqual(computer_screen._bridges, 0)
            with self.client.websocket_connect(self.url()) as again:
                self.assertEqual(again.receive_bytes(), BANNER)


class HermesPluginSocketGateTests(ComputerScreenTestCase):
    """Hermes puts `PluginApiWebSocketGate` in front of every plugin socket. It checks Host, Origin and
    peer, and nothing else: the plugin ticket alone must get through it."""

    def setUp(self):
        super().setUp()
        try:
            from hermes_cli import web_server
            from hermes_cli.web_server_dashboard import PluginApiWebSocketGate, _plugin_route_secret_scope
        except Exception:
            self.skipTest("this Hermes has no plugin socket gate")
        state = web_server.app.state
        saved = {name: getattr(state, name, None) for name in ("bound_host", "auth_required")}
        self.addCleanup(lambda: [setattr(state, name, value) for name, value in saved.items()])
        state.bound_host, state.auth_required = "127.0.0.1", False
        app = FastAPI()
        # Mounted as Hermes mounts a plugin router, with its profile scope dependency.
        app.include_router(native_api.router, prefix="/api/plugins/loopdy",
                           dependencies=[Depends(_plugin_route_secret_scope)])
        self.client = TestClient(PluginApiWebSocketGate(app))
        self.addCleanup(self.client.close)
        # The test client's peer is not an address. A loopback peer is what a local app has.
        peer = patch("hermes_cli.web_server_boundary.ws_client_reason", return_value=None)
        peer.start()
        self.addCleanup(peer.stop)
        # The plugin is enabled on a real host; this test host has no plugin list to say so.
        consent = patch("hermes_cli.web_server_dashboard._plugin_api_request_refused", return_value=False)
        consent.start()
        self.addCleanup(consent.stop)

    def test_the_ticket_alone_passes_with_no_origin_and_a_bound_host(self):
        with server_on():
            with self.client.websocket_connect(f"{SOCKET}?ticket={computer_screen.mint_ticket()}",
                                               headers={"host": "127.0.0.1:9119"}) as ws:
                self.assertEqual(ws.receive_bytes(), BANNER)

    def test_a_foreign_host_or_origin_never_reaches_the_route(self):
        with server_on():
            for headers in ({"host": "evil.example"},
                            {"host": "127.0.0.1:9119", "origin": "https://evil.example"}):
                ticket = computer_screen.mint_ticket()
                with self.assertRaises(WebSocketDisconnect):
                    with self.client.websocket_connect(f"{SOCKET}?ticket={ticket}", headers=headers):
                        pass
                self.assertTrue(computer_screen.redeem_ticket(ticket), "the gate refused before the route spent it")


if __name__ == "__main__":
    unittest.main()
