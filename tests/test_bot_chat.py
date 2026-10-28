"""An agent's Bot Chat made by Hermes itself. The agent's gateway is Hermes' own session authority on a real
`state.db`, built the way Hermes' tests build it; only the socket between the plugin and that owner is
replaced. Hermes' own Bot Chat delivery (`gateway.session_bot._target`) then decides whether it keeps the chat.
"""
from __future__ import annotations
import telemetry_off  # noqa: F401  (no test run posts host telemetry)

from contextlib import asynccontextmanager, closing
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from loopdy_plugin import bot_chat
from hermes_process_state import single_profile_host
import test_native_api as native_fixtures
from test_native_api import PREFIX

UID = "uid:fixture"
LEGACY_BOT_CHAT = "20261028_101500_a1b2c3"


def hermes_owner_available() -> bool:
    try:
        from gateway.session_authority import initialize_session_authority  # noqa: F401
        from gateway.session_bot import _target  # noqa: F401
        from gateway.session_controls import AuthorityConnection  # noqa: F401
    except Exception:
        return False
    return bot_chat.available()


class Owner:
    """One agent's gateway: Hermes' session authority on the agent's own `state.db`."""

    @classmethod
    async def start(cls, home: Path, profile: str):
        from gateway.config import GatewayConfig
        from gateway.session import SessionStore
        from gateway.session_authority import initialize_session_authority
        from gateway.session_contract import Principal
        from gateway.session_controls import AuthorityConnection
        from hermes_state import SessionDB

        self = cls()
        self.home, self.profile = home, profile
        self.store = SessionStore(home / "sessions", GatewayConfig())
        self.store._db = SessionDB(db_path=home / "state.db")
        runner = SimpleNamespace(session_store=self.store, _session_db=self.store._db, adapters={}, _draining=False,
                                 _evict_cached_agent=lambda route: None, _cached_agent_for=lambda route: None,
                                 _handle_message=None, _peek_session_state=lambda key: None)
        runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
        self.authority = await initialize_session_authority(runner, profile_id=profile, instance_id="fixture")
        self.db = self.authority.db
        # What a ticket from the owner's control socket stamps: this computer's user.
        capabilities = ["session:create", "session:read", "session:submit", "session:control"]
        self.connection = AuthorityConnection(self.authority, object(), {
            "provider": "local", "user_id": UID, "profile_id": profile, "instance_id": "fixture",
            "capabilities": capabilities})
        # Another agent's `message_agent` reaches this one's Bot Chat through the same kind of ticket.
        self.delivery = Principal(UID, profile, frozenset(capabilities), "delivery")
        self.calls: list[tuple[str, dict]] = []
        return self

    async def close(self):
        await self.connection.close()
        self.store.close_all_db_handles()
        self.store._db.close()

    @asynccontextmanager
    async def client(self, endpoint=None):
        from hermes_cli.gateway_client import rpc_error
        owner = self

        class Client:
            async def rpc(self, method, **params):
                # As sent on the wire: the owner's handler takes fields out of the params it gets.
                owner.calls.append((method, dict(params)))
                answer = await owner.connection.dispatch({"jsonrpc": "2.0", "id": len(owner.calls),
                                                          "method": method, "params": dict(params)})
                if "error" in answer:
                    raise rpc_error(answer["error"])
                return answer["result"]

        yield Client()

    async def deliver(self) -> str:
        """Where Hermes puts another agent's message: its Bot Chat delivery's own target."""
        from gateway.session_bot import _target
        ref, _live, _entry = await _target(self.authority, self.delivery)
        return ref.session_id


@unittest.skipUnless(hermes_owner_available(), "this Hermes has no gateway session authority")
class BotChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        single_profile_host(self)
        folder = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        (self.root / "profiles/nova").mkdir(parents=True)
        (self.root / "profiles/nova/config.yaml").write_text("model: {}\n", encoding="utf-8")
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.root), "HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        from gateway import run
        # A Bot Chat follows its profile's config; the owner reads it by path.
        config = patch.object(run, "_load_gateway_config",
                              lambda *_path: {"platform_toolsets": {"cli": []}, "model": {"default": "fixture"}})
        config.start()
        self.addCleanup(config.stop)
        self.owners: dict[Path, Owner] = {}
        self.started: list[Path] = []

        def ensure(home, *, timeout, idle_exit):
            self.started.append(Path(home))
            self.assertTrue(idle_exit, "A gateway the app starts ends itself once idle")
            return SimpleNamespace(state="ready", reason_code=None, endpoint=SimpleNamespace(home=Path(home)))

        @asynccontextmanager
        async def connect(endpoint=None, subject=None):
            self.assertIsNone(subject, "The owner's own ticket, as message_agent's delivery uses")
            async with self.owners[endpoint.home].client() as client:
                yield client

        for target, replacement in (("hermes_cli.gateway_runtime.ensure_gateway_runtime", ensure),
                                    ("hermes_cli.gateway_client.connect_gateway", connect)):
            patcher = patch(target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def owner(self, home: Path, profile: str) -> Owner:
        owner = await Owner.start(home, profile)
        self.addAsyncCleanup(owner.close)
        self.owners[home] = owner
        return owner

    async def test_hermes_makes_the_bot_chat_as_its_own_chat_and_its_delivery_keeps_it(self):
        owner = await self.owner(self.root, "default")
        key = str(uuid.uuid4())
        made = await bot_chat.create("default", key)
        self.assertTrue(made["storedSessionId"].startswith("local-"), made)
        self.assertEqual(made, {"storedSessionId": made["storedSessionId"], "created": True, "native": True})
        self.assertEqual(self.started, [self.root])
        method, params = owner.calls[0]
        self.assertEqual((method, params), ("session.create", {
            "request_id": "bighelp-bot-chat:" + key, "title": "Bot Chat", "source": "cli", "cwd": str(self.root),
            "follow_profile_config": True, "hidden": True}), "The parameters of Hermes' own Bot Chat creation")
        row = owner.db.get_session(made["storedSessionId"])
        self.assertEqual((row["title"], bool(row["hidden"]), row["chat_id"]), ("Bot Chat", True, row["id"]))

        self.assertEqual(await bot_chat.create("default", key), made, "A retry with the same key")
        self.assertEqual(await bot_chat.create("default", str(uuid.uuid4())),
                         {**made, "created": False}, "Another key: the Bot Chat that's there")
        self.assertEqual(len(owner.calls), 1, "A Bot Chat that's there needs no gateway")

        with self.assertNoLogs("gateway.session_bot", level=logging.WARNING):
            self.assertEqual(await owner.deliver(), made["storedSessionId"],
                             "Another agent's message lands in the same chat")
        row = owner.db.get_session(made["storedSessionId"])
        self.assertEqual((row["title"], bool(row["hidden"])), ("Bot Chat", True), "Not moved aside")

    async def test_hermes_moves_aside_a_bot_chat_the_app_made_over_the_socket(self):
        # The counterfactual: the app's old Bot Chat, made by the dashboard's socket (source `bighelp`).
        owner = await self.owner(self.root, "default")
        owner.db.create_session(LEGACY_BOT_CHAT, "bighelp")
        owner.db.set_session_title(LEGACY_BOT_CHAT, "Bot Chat")
        owner.db.set_session_hidden(LEGACY_BOT_CHAT, True)
        self.assertEqual(await bot_chat.create("default", str(uuid.uuid4())),
                         {"storedSessionId": LEGACY_BOT_CHAT, "created": False, "native": False},
                         "The plugin answers the chat that holds the title and changes nothing")
        self.assertEqual(owner.calls, [])
        self.assertEqual(owner.db.get_session(LEGACY_BOT_CHAT)["title"], "Bot Chat")

        with self.assertLogs("gateway.session_bot", level=logging.WARNING) as logs:
            delivered = await owner.deliver()
        self.assertIn("moving it aside", " ".join(logs.output))
        self.assertNotEqual(delivered, LEGACY_BOT_CHAT)
        old = owner.db.get_session(LEGACY_BOT_CHAT)
        self.assertEqual((old["title"], bool(old["hidden"])), (f"Bot Chat (bighelp {LEGACY_BOT_CHAT})", False))

    async def test_each_agent_gets_its_own_from_its_own_gateway(self):
        default = await self.owner(self.root, "default")
        nova_home = self.root / "profiles/nova"
        nova = await self.owner(nova_home, "nova")
        mine = await bot_chat.create("default", str(uuid.uuid4()))
        theirs = await bot_chat.create("nova", str(uuid.uuid4()))
        self.assertEqual(self.started, [self.root, nova_home])
        self.assertNotEqual(mine["storedSessionId"], theirs["storedSessionId"])
        self.assertIsNone(default.db.get_session(theirs["storedSessionId"]))
        self.assertEqual(nova.db.get_session(theirs["storedSessionId"])["title"], "Bot Chat")
        self.assertEqual(await nova.deliver(), theirs["storedSessionId"])
        with self.assertRaises(LookupError):
            await bot_chat.create("ghost", str(uuid.uuid4()))

    async def test_a_gateway_that_does_not_take_hidden_still_makes_it(self):
        owner = await self.owner(self.root, "default")
        real_create = type(owner.connection).create

        async def older_create(connection, ref, params):
            # A gateway from before `hidden`: its policy check refuses a field it doesn't know, before any write.
            if "hidden" in params:
                from hermes_state_runtime import RuntimeStoreError
                raise RuntimeStoreError("invalid_params", field="params.hidden", detail="unknown field")
            return await real_create(connection, ref, params)
        with patch.object(type(owner.connection), "create", older_create):
            made = await bot_chat.create("default", str(uuid.uuid4()))
        self.assertEqual([("hidden" in params) for _method, params in owner.calls], [True, False])
        row = owner.db.get_session(made["storedSessionId"])
        self.assertEqual((made["native"], made["created"], row["title"], bool(row["hidden"])),
                         (True, True, "Bot Chat", False), "Made, and visible, as Hermes' own delivery makes it")

    async def test_gateway_refusals_and_outages(self):
        await self.owner(self.root, "default")
        from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError
        from hermes_cli.gateway_runtime_discovery import DiscoveryError

        def failing(error):
            @asynccontextmanager
            async def connect(endpoint=None, subject=None):
                raise error
                yield
            return connect

        cases = ((GatewayRPCError("runtime_draining"), "bot_chat_refused", True, "runtime_draining"),
                 (GatewayRPCError("permission_denied"), "bot_chat_refused", False, "permission_denied"),
                 (GatewayClientError("Gateway connection failed; no local fallback"),
                  "bot_chat_gateway_unavailable", True, None),
                 (DiscoveryError("control_unavailable"), "bot_chat_gateway_unavailable", True, None))
        for error, code, busy, reason in cases:
            with self.subTest(code=code, reason=reason), \
                    patch("hermes_cli.gateway_client.connect_gateway", failing(error)), \
                    self.assertRaises(bot_chat.BotChatError) as raised:
                await bot_chat.create("default", str(uuid.uuid4()))
            self.assertEqual((raised.exception.code, raised.exception.busy, raised.exception.reason),
                             (code, busy, reason))
        starting = SimpleNamespace(state="starting", reason_code="deadline", endpoint=None)
        with patch("hermes_cli.gateway_runtime.ensure_gateway_runtime", lambda *a, **k: starting), \
                self.assertRaises(bot_chat.BotChatError) as raised:
            await bot_chat.create("default", str(uuid.uuid4()))
        self.assertEqual((raised.exception.code, raised.exception.busy), ("bot_chat_gateway_unavailable", True))
        with closing(__import__("sqlite3").connect(self.root / "state.db")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0, "Nothing was made")


class BotChatRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def call(self, body):
        return self.fixture.client.post(PREFIX + "/bot-chat/create", json=body, headers=self.fixture.headers())

    def test_route_answers_maps_failures_and_refuses_bad_requests(self):
        if not bot_chat.available():
            self.skipTest("this Hermes has no gateway client")
        self.assertIn(bot_chat.CAPABILITY, self.fixture.context().json()["features"])
        key = str(uuid.uuid4())
        made = {"storedSessionId": "local-" + "a" * 64, "created": True, "native": True}

        async def create(profile, idempotency_key):
            return made
        with patch.object(bot_chat, "create", side_effect=create) as called:
            result = self.call({"profile": "research", "idempotencyKey": key})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"ok": True, **made})
        self.assertEqual(result.headers["cache-control"], "no-store")
        called.assert_called_once_with("research", key)

        for error, status, code in ((LookupError("ghost"), 404, "profile_not_found"),
                                    (bot_chat.BotChatError("bot_chat_gateway_unavailable", busy=True), 503,
                                     "bot_chat_gateway_unavailable"),
                                    (bot_chat.BotChatError("bot_chat_refused", reason="permission_denied"), 409,
                                     "bot_chat_refused")):
            with self.subTest(code=code), patch.object(bot_chat, "create", side_effect=error):
                result = self.call({"profile": "research", "idempotencyKey": key})
            self.assertEqual((result.status_code, result.json()["error"]["code"]), (status, code), result.text)
        self.assertEqual(result.json()["error"]["details"], {"reason": "permission_denied"})
        for body in ({}, {"profile": "research"}, {"profile": "../etc", "idempotencyKey": key},
                     {"profile": "research", "idempotencyKey": key.upper()},
                     {"profile": "research", "idempotencyKey": key, "title": "Bot Chat"}):
            self.assertEqual(self.call(body).status_code, 422, body)


if __name__ == "__main__":
    unittest.main()
