"""Phone tools for every agent on a host, and calls that wait while the phone reconnects.

One Hermes process loads this plugin once for each profile it serves, so each agent has its own copy of the
module. These tests load the module a second time, like Hermes does, to prove the copies share one hub.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from starlette.datastructures import Headers, QueryParams

from loopdy_plugin import device_photos, native_api, native_context, native_device_tools as ndt
from loopdy_plugin.device_tool_contract import device_tool_result
from loopdy_plugin.native_device_tools import NativeDeviceToolError, NativeDeviceToolHub


CALENDAR = {"start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z", "timeZone": "UTC"}
NOTE = "bighelp isn't open on the phone right now. Ask the person to open bighelp, then try again."


@dataclass(frozen=True)
class Owner:
    provider: str = "test"
    user_id: str = "user-1"
    serving_profile_id: str = "default"
    runtime_id: str = "runtime-1"


class Clock:
    def __init__(self, value: float = 1_700_000_000) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def fields(*, agent: str = "research", session: str = "chat-1", device: str | None = None,
           enabled: tuple[str, ...] = ("calendar", "shortcuts", "contacts")) -> dict:
    return {
        "channelId": str(uuid.uuid4()), "deviceId": device or str(uuid.uuid4()), "hostId": "local-host",
        "authorizationEpoch": 1, "agentId": agent, "sessionId": session, "enabled": list(enabled),
    }


def validator(*pairs: tuple[str, str]):
    return lambda profile, session: (profile, session) in set(pairs)


def hub_with(clock: Clock, *pairs: tuple[str, str], profiles: tuple[str, ...] = ("default", "research", "garden")):
    return NativeDeviceToolHub(
        clock=clock, profile_session_validator=validator(*pairs),
        profile_validator=lambda profile: profile in profiles,
    )


def load_instance(case: unittest.TestCase, tag: str, root: Path | None = None) -> ModuleType:
    """Import native_device_tools again under its own package name, the way Hermes does for each profile."""
    root = root or Path(native_device_tools_file()).parent
    namespace = f"hermes_plugins.loopdy__home_{tag}_{uuid.uuid4().hex[:8]}"
    package = namespace + ".loopdy_plugin"
    for name, path in (("hermes_plugins", []), (namespace, []), (package, [str(root)])):
        if name not in sys.modules:
            parent = ModuleType(name)
            parent.__path__ = path
            parent.__package__ = name
            sys.modules[name] = parent
    spec = importlib.util.spec_from_file_location(package + ".native_device_tools", root / "native_device_tools.py")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = package
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def native_device_tools_file() -> str:
    return ndt.__file__


class IsolatedRegistry(unittest.IsolatedAsyncioTestCase):
    """Each test gets its own process-wide hub registry, and gives the real one back afterwards."""

    def setUp(self) -> None:
        patcher = patch.dict(sys.modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop(ndt._HUB_REGISTRY_NAME, None)


class SharedHubTests(IsolatedRegistry):
    def test_every_copy_of_the_module_uses_one_hub(self) -> None:
        serving = load_instance(self, "serving")
        other = load_instance(self, "research")
        self.assertIsNot(serving, other)
        self.assertIs(serving._HUB, other._HUB)

    def test_a_copy_that_speaks_another_hub_version_keeps_its_own_hub(self) -> None:
        serving = load_instance(self, "serving")
        self.assertIs(serving._shared_hub(), serving._shared_hub())
        self.assertIsNot(serving._shared_hub(), serving._shared_hub(version=serving.HUB_PROTOCOL_VERSION + 1))
        other = load_instance(self, "research")
        future_version = serving.HUB_PROTOCOL_VERSION + 1
        self.assertIs(serving._shared_hub(version=future_version), other._shared_hub(version=future_version))

    async def test_a_call_from_another_profiles_copy_reaches_the_phone_of_its_own_chat(self) -> None:
        serving = load_instance(self, "serving")
        other = load_instance(self, "research")
        hub = serving._HUB
        hub.profile_session_validator = validator(("research", "chat-1"))
        hub.profile_validator = lambda profile: profile == "research"
        owner = Owner()
        channel = fields(agent="research")
        self.assertEqual(hub.connect(owner, channel), {"channelId": channel["channelId"], "connected": True})

        callbacks: list = []

        class Context:
            profile_name = "research"

            def register_middleware(self, kind, callback):
                callbacks.append(callback)

            def on_unload(self, callback):
                pass

        self.assertTrue(other.register_middleware(Context()))
        with patch.dict(sys.modules, {"model_tools": SimpleNamespace(_run_async=lambda c: asyncio.run(c))}):
            task = asyncio.create_task(asyncio.to_thread(
                callbacks[0], tool_name="iphone_calendar", args={"operation": "list", **CALENDAR,
                                                                  "agentId": "default"},
                session_id="chat-1", turn_id="turn-1", tool_call_id="call-1", next_call=lambda: self.fail("chain")))
            request = None
            for _ in range(200):
                await asyncio.sleep(0.01)
                rows = hub.poll(owner, channel, after=0)["requests"]
                if rows:
                    request = rows[0]["request"]
                    break
            self.assertIsNotNone(request, "the call never reached the shared hub")
            # The profile comes from the middleware's own context, never from the model's arguments.
            self.assertEqual(request["agentId"], "research")
            hub.accept_result(owner, channel, device_tool_result(
                request=request, status="completed", payload={"items": []}, sent_at=int(request["sentAt"]) + 1))
            self.assertEqual(json.loads(await asyncio.wait_for(task, 5))["payload"], {"items": []})

    async def test_unloading_one_profile_leaves_the_other_profiles_channels(self) -> None:
        clock = Clock()
        hub = hub_with(clock, ("default", "chat-1"), ("research", "chat-1"))
        owner = Owner()
        kept = fields(agent="default")
        dropped = fields(agent="research")
        hub.connect(owner, kept)
        hub.connect(owner, dropped)
        callbacks, unloads = [], []

        class Context:
            profile_name = "research"

            def register_middleware(self, kind, callback):
                callbacks.append(callback)

            def on_unload(self, callback):
                unloads.append(callback)

        self.assertTrue(ndt.register_middleware(Context(), hub=hub))
        unloads[0]()
        self.assertEqual(hub.poll(owner, kept, after=0)["next"], 0)
        with self.assertRaises(NativeDeviceToolError) as error:
            hub.poll(owner, dropped, after=0)
        self.assertEqual(error.exception.code, "phone_unavailable")

    async def test_the_route_turns_a_refusal_from_a_hub_of_another_copy_into_an_api_error(self) -> None:
        serving = load_instance(self, "serving")
        other = load_instance(self, "research")
        serving._HUB.profile_session_validator = validator()
        serving._HUB.profile_validator = lambda profile: True
        owner = NativeContext = native_context.NativeContext(
            None, None, None, "default", (other.CAPABILITY,), native_context.RUNTIME_ID)
        body = fields(agent="research", session="not-there")

        class Request:
            query_params = QueryParams()
            headers = Headers({
                "content-type": "application/json", "if-match": owner.etag,
                "x-loopdy-request-id": str(uuid.uuid4()),
            })

            async def stream(self):
                yield json.dumps(body).encode()

        with patch.object(other, "available", return_value=True), \
                patch.object(native_api, "native_context", return_value=owner):
            with self.assertRaises(native_api.NativeAPIError) as error:
                await other.request("connect", Request(), owner=owner, auth_module=native_api)
        self.assertEqual(error.exception.code, "session_not_found")


class RouteTests(IsolatedRegistry):
    async def test_a_pause_sent_to_the_route_reaches_the_hub(self) -> None:
        serving = load_instance(self, "serving")
        serving._HUB.profile_session_validator = validator(("research", "chat-1"))
        serving._HUB.profile_validator = lambda profile: profile == "research"
        owner = native_context.NativeContext(
            None, None, None, "default", (serving.CAPABILITY,), native_context.RUNTIME_ID)

        def request(payload: dict):
            class Request:
                query_params = QueryParams()
                headers = Headers({
                    "content-type": "application/json", "if-match": owner.etag,
                    "x-loopdy-request-id": str(uuid.uuid4()),
                })

                async def stream(self):
                    yield json.dumps(payload).encode()

            return Request()

        channel = fields(agent="research")
        with patch.object(serving, "available", return_value=True), \
                patch.object(native_api, "native_context", return_value=owner):
            await serving.request("connect", request(channel), owner=owner, auth_module=native_api)
            response = await serving.request(
                "close", request({"channelId": channel["channelId"], "reason": "paused"}),
                owner=owner, auth_module=native_api)
            self.assertEqual(json.loads(response.body), {"closed": True})
            self.assertEqual(serving._HUB.channels, {})
            self.assertEqual([lapse.profile for lapse in serving._HUB._lapses], ["research"])
            # Any other reason is an invalid request, like any other unknown field.
            again = fields(agent="research")
            await serving.request("connect", request(again), owner=owner, auth_module=native_api)
            with self.assertRaises(native_api.NativeAPIError) as error:
                await serving.request(
                    "close", request({"channelId": again["channelId"], "reason": "gone"}),
                    owner=owner, auth_module=native_api)
            self.assertEqual((error.exception.status, error.exception.code), (422, "invalid_request"))


class ConnectForEveryAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.owner = Owner()
        self.hub = hub_with(self.clock, ("default", "chat-1"), ("research", "chat-1"), ("research", "chat-2"))

    def test_another_agents_chat_can_connect(self) -> None:
        channel = fields(agent="research", session="chat-2")
        self.assertEqual(self.hub.connect(self.owner, channel), {"channelId": channel["channelId"], "connected": True})

    def test_a_session_that_belongs_to_another_profile_is_refused(self) -> None:
        with self.assertRaises(NativeDeviceToolError) as error:
            self.hub.connect(self.owner, fields(agent="default", session="chat-2"))
        self.assertEqual((error.exception.code, error.exception.status), ("session_not_found", 404))
        self.assertEqual(self.hub.channels, {})

    def test_a_profile_that_does_not_exist_is_refused_and_never_replaced_by_another(self) -> None:
        with self.assertRaises(NativeDeviceToolError) as error:
            self.hub.connect(self.owner, fields(agent="ghost", session="chat-1"))
        self.assertEqual((error.exception.code, error.exception.status), ("profile_not_found", 404))
        self.assertEqual(self.hub.channels, {})

    def test_the_same_chat_id_in_two_profiles_is_two_chats(self) -> None:
        default_phone = fields(agent="default", session="chat-1")
        research_phone = fields(agent="research", session="chat-1")
        self.hub.connect(self.owner, default_phone)
        self.hub.connect(self.owner, research_phone)
        self.assertEqual(self.hub.native_channel_count("default", "chat-1"), 1)
        self.assertEqual(self.hub.native_channel_count("research", "chat-1"), 1)

    def test_a_second_phone_for_the_same_chat_is_still_ambiguous(self) -> None:
        self.hub.connect(self.owner, fields(agent="research"))
        with self.assertRaises(NativeDeviceToolError) as error:
            self.hub.connect(self.owner, fields(agent="research"))
        self.assertEqual(error.exception.code, "channel_ambiguous")

    def test_the_owner_fence_still_holds_for_another_agents_channel(self) -> None:
        channel = fields(agent="research")
        self.hub.connect(self.owner, channel)
        stranger = Owner(user_id="someone-else")
        for action in (
            lambda: self.hub.poll(stranger, {"channelId": channel["channelId"], "after": 0}),
            lambda: self.hub.close(stranger, {"channelId": channel["channelId"]}),
            lambda: self.hub.connect(stranger, fields(agent="research", device=channel["deviceId"])),
        ):
            with self.assertRaises(NativeDeviceToolError) as error:
                action()
            self.assertIn(error.exception.code, {"channel_owner_changed", "channel_ambiguous"})

    def test_a_dashboard_without_sign_in_allows_no_more_than_a_signed_in_one(self) -> None:
        anonymous = Owner(provider=None, user_id=None)
        self.hub.connect(anonymous, fields(agent="research"))
        with self.assertRaises(NativeDeviceToolError) as second_phone:
            self.hub.connect(anonymous, fields(agent="research"))
        self.assertEqual(second_phone.exception.code, "channel_ambiguous")
        with self.assertRaises(NativeDeviceToolError) as other_chat:
            self.hub.connect(anonymous, fields(agent="research", session="chat-9"))
        self.assertEqual(other_chat.exception.code, "session_not_found")

    async def test_a_call_only_reaches_the_phone_of_its_own_profile(self) -> None:
        research = fields(agent="research")
        self.hub.connect(self.owner, research)
        result = await self.hub.execute(
            profile="default", session_id="chat-1", turn_id="turn-1", tool_call_id="call-1",
            operation="calendar.list", arguments=CALENDAR)
        self.assertEqual(result["code"], "phone_unavailable")
        self.assertEqual(self.hub.poll(self.owner, research, after=0)["requests"], [])


class DefaultValidatorTests(unittest.TestCase):
    def test_the_session_is_looked_up_in_the_requested_profiles_own_store(self) -> None:
        opened: list[tuple[Path, bool]] = []

        class Store:
            def __init__(self, db_path=None, read_only=False):
                opened.append((Path(db_path), read_only))

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_session(self, session_id):
                return {"id": session_id} if opened[-1][0] == Path("/homes/research/state.db") else None

        with patch("hermes_cli.profiles.profile_exists", return_value=True), \
                patch("hermes_cli.profiles.get_profile_dir", side_effect=lambda name: Path("/homes") / name), \
                patch("hermes_constants.get_process_hermes_home", return_value=Path("/homes/default")), \
                patch("hermes_state.SessionDB", Store):
            self.assertTrue(ndt._default_profile_session_validator("research", "chat-1"))
            self.assertFalse(ndt._default_profile_session_validator("garden", "chat-1"))
        self.assertEqual(opened, [(Path("/homes/research/state.db"), True), (Path("/homes/garden/state.db"), True)])

    def test_a_profile_that_is_not_on_this_host_does_not_exist(self) -> None:
        with patch("hermes_cli.profiles.profile_exists", side_effect=lambda name: name == "research"):
            self.assertTrue(ndt._default_profile_exists("research"))
            self.assertFalse(ndt._default_profile_exists("ghost"))
        with patch("hermes_cli.profiles.profile_exists", side_effect=ValueError):
            self.assertFalse(ndt._default_profile_exists("research"))

    def test_pictures_go_to_the_profile_of_the_agent_that_asked(self) -> None:
        with patch("hermes_constants.get_process_hermes_home", return_value=Path("/homes/default")), \
                patch("hermes_cli.profiles.get_profile_dir", side_effect=lambda name: Path("/homes") / name):
            self.assertEqual(device_photos.photos_directory(), Path("/homes/default/bighelp/phone-photos"))
            self.assertEqual(device_photos.photos_directory("default"), Path("/homes/default/bighelp/phone-photos"))
            self.assertEqual(device_photos.photos_directory("research"), Path("/homes/research/bighelp/phone-photos"))


class WaitCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.owner = Owner()
        self.hub = hub_with(self.clock, ("research", "chat-1"), ("research", "chat-2"))
        self.phone = str(uuid.uuid4())

    def connect(self, **changes) -> dict:
        channel = fields(device=changes.pop("device", self.phone), **changes)
        self.hub.connect(self.owner, channel)
        return channel

    def call(self, operation: str = "calendar.list", arguments: dict | None = None, *, call_id: str = "call-1",
             session: str = "chat-1"):
        return asyncio.create_task(self.hub.execute(
            profile="research", session_id=session, turn_id="turn-1", tool_call_id=call_id, operation=operation,
            arguments=CALENDAR if arguments is None and operation == "calendar.list" else (arguments or {})))

    def feed(self, channel: dict) -> list[dict]:
        return [row["request"] for row in self.hub.poll(self.owner, channel, after=0)["requests"]]

    def answer(self, channel: dict, request: dict, payload: dict | None = None) -> None:
        self.hub.accept_result(self.owner, channel, device_tool_result(
            request=request, status="completed", payload=payload or {"items": []}, sent_at=int(self.clock.value)))

    def pause(self, channel: dict) -> None:
        self.assertEqual(self.hub.close(self.owner, {**channel, "reason": "paused"}), {"closed": True})


class WaitForThePhoneTests(WaitCase):
    async def test_a_call_with_no_channel_and_no_recent_channel_fails_at_once(self) -> None:
        result = await asyncio.wait_for(self.call(), 2)
        self.assertEqual((result["status"], result["code"]), ("failed", "phone_unavailable"))
        self.assertEqual(result["payload"], {"note": NOTE})

    async def test_a_channel_that_was_closed_for_good_leaves_nothing_to_wait_for(self) -> None:
        channel = self.connect()
        self.hub.close(self.owner, {"channelId": channel["channelId"]})
        result = await asyncio.wait_for(self.call(), 2)
        self.assertEqual(result["code"], "phone_unavailable")

    async def test_a_channel_that_lapsed_long_ago_leaves_nothing_to_wait_for(self) -> None:
        self.connect()
        self.clock.advance(30 + ndt.LAPSE_MEMORY_SECONDS + 1)
        result = await asyncio.wait_for(self.call(), 2)
        self.assertEqual(result["code"], "phone_unavailable")

    async def test_a_call_waits_for_the_same_phone_after_its_lease_lapsed(self) -> None:
        self.connect()
        self.clock.advance(30 + ndt.LAPSE_MEMORY_SECONDS - 5)
        task = self.call()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.clock.advance(3)
        fresh = self.connect()
        [request] = self.feed(fresh)
        self.assertEqual(request["operation"], "calendar.list")
        self.assertEqual(request["sessionId"], "chat-1")
        self.assertEqual(request["agentId"], "research")
        self.answer(fresh, request)
        self.assertEqual((await asyncio.wait_for(task, 2))["status"], "completed")

    async def test_the_new_channel_gets_the_calls_own_deadline_and_never_a_longer_one(self) -> None:
        self.connect()
        self.clock.advance(35)
        task = self.call()
        await asyncio.sleep(0)
        sent_at = int(self.clock.value)
        self.clock.advance(12)
        fresh = self.connect()
        [request] = self.feed(fresh)
        self.assertEqual((request["sentAt"], request["expiresAt"]), (sent_at, sent_at + 30))
        self.answer(fresh, request)
        await asyncio.wait_for(task, 2)

    async def test_the_host_never_waits_longer_than_the_call_is_allowed(self) -> None:
        waits = []

        async def fake_wait_for(awaitable, timeout):
            waits.append(timeout)
            raise asyncio.TimeoutError

        for operation, arguments, seconds in (
            ("calendar.list", CALENDAR, 30), ("shortcuts.run", {"name": "Log Water"}, 70),
        ):
            with self.subTest(operation=operation):
                channel = self.connect(session="chat-2")
                self.clock.advance(31)
                with patch("loopdy_plugin.native_device_tools.asyncio.wait_for", fake_wait_for):
                    result = await self.call(operation, arguments, call_id=f"call-{operation}", session="chat-2")
                self.assertEqual(waits[-1], seconds)
                # It never reached the phone, so it is safe to try again.
                self.assertEqual((result["status"], result["code"]), ("failed", "phone_unavailable"))
                self.assertEqual(self.hub.native_channel_count("research", "chat-2"), 0)
                self.hub.close_profile("research")

    async def test_a_different_phone_does_not_receive_the_call(self) -> None:
        self.connect()
        self.clock.advance(31)
        task = self.call()
        await asyncio.sleep(0)
        stranger = self.connect(device=str(uuid.uuid4()))
        self.assertEqual(self.feed(stranger), [])
        result = await asyncio.wait_for(task, 2)
        self.assertEqual(result["code"], "phone_unavailable")
        self.assertEqual(self.feed(stranger), [])

    async def test_the_same_device_for_another_person_does_not_receive_the_call(self) -> None:
        self.connect()
        self.clock.advance(31)
        task = self.call()
        await asyncio.sleep(0)
        other_person = Owner(user_id="someone-else")
        channel = fields(device=self.phone)
        self.hub.connect(other_person, channel)
        self.assertEqual(self.hub.poll(other_person, channel, after=0)["requests"], [])
        self.assertEqual((await asyncio.wait_for(task, 2))["code"], "phone_unavailable")

    async def test_a_call_does_not_wait_for_a_channel_of_another_chat(self) -> None:
        self.connect()
        self.clock.advance(31)
        task = self.call()
        await asyncio.sleep(0)
        elsewhere = self.connect(session="chat-2")
        self.assertEqual(self.feed(elsewhere), [])
        self.assertFalse(task.done())
        self.hub.close_profile("research")
        await asyncio.wait_for(task, 2)

    async def test_a_tool_that_was_turned_off_meanwhile_is_not_sent(self) -> None:
        self.connect()
        self.clock.advance(31)
        task = self.call()
        await asyncio.sleep(0)
        fresh = self.connect(enabled=("contacts",))
        self.assertEqual((await asyncio.wait_for(task, 2))["code"], "authorization_required")
        self.assertEqual(self.feed(fresh), [])

    async def test_a_tool_that_is_off_is_refused_at_once(self) -> None:
        self.connect(enabled=("contacts",))
        self.clock.advance(31)
        result = await asyncio.wait_for(self.call(), 2)
        self.assertEqual(result["code"], "authorization_required")


class PauseTests(WaitCase):
    async def test_pausing_keeps_a_request_the_phone_has_not_received(self) -> None:
        channel = self.connect()
        task = self.call()
        await asyncio.sleep(0)
        self.pause(channel)
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        fresh = self.connect()
        [request] = self.feed(fresh)
        self.answer(fresh, request)
        self.assertEqual((await asyncio.wait_for(task, 2))["status"], "completed")

    async def test_a_plain_close_still_fails_every_pending_call(self) -> None:
        channel = self.connect()
        task = self.call()
        await asyncio.sleep(0)
        self.assertEqual(self.hub.close(self.owner, {"channelId": channel["channelId"]}), {"closed": True})
        self.assertEqual((await asyncio.wait_for(task, 2))["code"], "phone_unavailable")
        self.assertEqual(self.feed(self.connect()), [])

    async def test_a_plain_close_with_no_reason_field_set_is_the_same_as_none(self) -> None:
        channel = self.connect()
        task = self.call()
        await asyncio.sleep(0)
        self.hub.close(self.owner, {"channelId": channel["channelId"], "reason": None})
        self.assertEqual((await asyncio.wait_for(task, 2))["code"], "phone_unavailable")

    async def test_a_call_made_while_paused_waits_for_the_phone(self) -> None:
        channel = self.connect()
        self.pause(channel)
        self.clock.advance(60)
        task = self.call()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        fresh = self.connect()
        [request] = self.feed(fresh)
        self.answer(fresh, request)
        await asyncio.wait_for(task, 2)

    async def test_a_request_the_phone_already_received_is_not_kept(self) -> None:
        channel = self.connect()
        read = self.call(call_id="read")
        write = self.call("shortcuts.run", {"name": "Log Water"}, call_id="write")
        await asyncio.sleep(0)
        self.assertEqual(len(self.feed(channel)), 2)
        self.pause(channel)
        self.assertEqual((await asyncio.wait_for(read, 2))["code"], "phone_unavailable")
        self.assertEqual((await asyncio.wait_for(write, 2))["code"], "outcome_unknown")
        # Neither runs again on the next channel.
        self.assertEqual(self.feed(self.connect()), [])

    async def test_only_the_requests_not_yet_received_move_on(self) -> None:
        channel = self.connect()
        received = self.call(call_id="received")
        await asyncio.sleep(0)
        self.assertEqual(len(self.feed(channel)), 1)
        waiting = self.call(call_id="waiting")
        await asyncio.sleep(0)
        self.pause(channel)
        self.assertEqual((await asyncio.wait_for(received, 2))["code"], "phone_unavailable")
        fresh = self.connect()
        [request] = self.feed(fresh)
        self.assertEqual(request["turnId"], "turn-1")
        self.answer(fresh, request)
        await asyncio.wait_for(waiting, 2)

    async def test_the_same_phone_opening_the_chat_again_takes_over_what_the_old_channel_held(self) -> None:
        stale = self.connect()
        task = self.call()
        await asyncio.sleep(0)
        self.clock.advance(5)
        fresh = self.connect()
        self.assertNotIn(stale["channelId"], self.hub.channels)
        [request] = self.feed(fresh)
        self.answer(fresh, request)
        self.assertEqual((await asyncio.wait_for(task, 2))["status"], "completed")

    async def test_a_request_the_phone_received_is_lost_when_the_phone_opens_the_chat_again(self) -> None:
        stale = self.connect()
        task = self.call("shortcuts.run", {"name": "Log Water"})
        await asyncio.sleep(0)
        self.assertEqual(len(self.feed(stale)), 1)
        fresh = self.connect()
        self.assertEqual((await asyncio.wait_for(task, 2))["code"], "outcome_unknown")
        self.assertEqual(self.feed(fresh), [])

    async def test_a_lapsed_lease_keeps_what_the_phone_never_received(self) -> None:
        self.connect()
        self.clock.advance(20)
        task = self.call()
        await asyncio.sleep(0)
        self.clock.advance(11)
        # Any look at the hub notices the lapse.
        self.assertEqual(self.hub.native_channel_count("research", "chat-1"), 0)
        self.assertFalse(task.done())
        fresh = self.connect()
        [request] = self.feed(fresh)
        self.answer(fresh, request)
        await asyncio.wait_for(task, 2)

    async def test_a_lapsed_lease_does_not_keep_what_the_phone_received(self) -> None:
        channel = self.connect()
        task = self.call()
        await asyncio.sleep(0)
        self.assertEqual(len(self.feed(channel)), 1)
        self.clock.advance(31)
        self.assertEqual(self.hub.native_channel_count("research", "chat-1"), 0)
        self.assertEqual((await asyncio.wait_for(task, 2))["code"], "phone_unavailable")

    async def test_unloading_the_plugin_fails_pending_calls_for_good(self) -> None:
        self.connect()
        task = self.call()
        await asyncio.sleep(0)
        self.hub.close_profile("research")
        self.assertEqual((await asyncio.wait_for(task, 2))["code"], "phone_unavailable")
        again = await asyncio.wait_for(self.call(call_id="call-2"), 2)
        self.assertEqual(again["code"], "phone_unavailable")

    async def test_the_route_accepts_a_pause_and_refuses_other_reasons(self) -> None:
        channel = {"channelId": str(uuid.uuid4())}
        self.assertEqual(ndt._Close(**channel, reason="paused").reason, "paused")
        self.assertIsNone(ndt._Close(**channel).reason)
        for bad in ("gone", "", "PAUSED", 1):
            with self.assertRaises(ValueError):
                ndt._Close(**channel, reason=bad)
        with self.assertRaises(ValueError):
            ndt._Close(**channel, extra=1)


class RequestIdentityTests(WaitCase):
    async def test_a_retried_call_joins_the_one_that_waits(self) -> None:
        self.connect()
        self.clock.advance(31)
        first = self.call()
        await asyncio.sleep(0)
        retry = self.call()
        await asyncio.sleep(0)
        fresh = self.connect()
        [request] = self.feed(fresh)
        self.answer(fresh, request)
        self.assertEqual((await asyncio.wait_for(first, 2))["status"], "completed")
        self.assertEqual((await asyncio.wait_for(retry, 2))["status"], "completed")

    async def test_a_retry_with_other_arguments_is_a_conflict_while_the_call_waits(self) -> None:
        self.connect()
        self.clock.advance(31)
        first = self.call()
        await asyncio.sleep(0)
        conflict = await asyncio.wait_for(
            self.call(arguments={**CALENDAR, "limit": 3}), 2)
        self.assertEqual(conflict["code"], "request_conflict")
        self.hub.close_profile("research")
        await asyncio.wait_for(first, 2)

    async def test_a_retry_after_the_move_joins_the_request_on_the_new_channel(self) -> None:
        channel = self.connect()
        first = self.call()
        await asyncio.sleep(0)
        self.pause(channel)
        fresh = self.connect()
        retry = self.call()
        await asyncio.sleep(0)
        [request] = self.feed(fresh)
        self.assertEqual(len(self.hub.channels[fresh["channelId"]].pending), 1)
        self.answer(fresh, request)
        self.assertEqual((await asyncio.wait_for(first, 2))["status"], "completed")
        self.assertEqual((await asyncio.wait_for(retry, 2))["status"], "completed")

    async def test_a_call_that_finished_is_not_run_again_on_a_later_channel(self) -> None:
        channel = self.connect()
        task = self.call("shortcuts.run", {"name": "Log Water"})
        await asyncio.sleep(0)
        [request] = self.feed(channel)
        self.answer(channel, request, {"id": "r1", "status": "completed", "output": "done", "truncated": False})
        await asyncio.wait_for(task, 2)
        self.pause(channel)
        fresh = self.connect()
        replay = await asyncio.wait_for(self.call("shortcuts.run", {"name": "Log Water"}), 2)
        self.assertEqual(replay["code"], "result_replay")
        self.assertEqual(self.feed(fresh), [])

    async def test_a_call_that_reached_the_phone_is_not_run_again_after_it_was_lost(self) -> None:
        channel = self.connect()
        task = self.call("shortcuts.run", {"name": "Log Water"})
        await asyncio.sleep(0)
        self.assertEqual(len(self.feed(channel)), 1)
        self.pause(channel)
        self.assertEqual((await asyncio.wait_for(task, 2))["code"], "outcome_unknown")
        fresh = self.connect()
        retry = await asyncio.wait_for(self.call("shortcuts.run", {"name": "Log Water"}), 2)
        self.assertEqual(retry["code"], "result_replay")
        self.assertEqual(self.feed(fresh), [])

    async def test_an_answer_for_the_old_channel_is_not_taken_on_the_new_one(self) -> None:
        channel = self.connect()
        task = self.call()
        await asyncio.sleep(0)
        old = next(iter(self.hub.channels[channel["channelId"]].pending.values())).request
        self.pause(channel)
        fresh = self.connect()
        [request] = self.feed(fresh)
        self.assertNotEqual(request["requestId"], old["requestId"])
        late = device_tool_result(request=old, status="completed", payload={"items": []}, sent_at=int(self.clock.value))
        with self.assertRaises(NativeDeviceToolError) as on_new:
            self.hub.accept_result(self.owner, fresh, late)
        self.assertEqual(on_new.exception.code, "result_unknown")
        with self.assertRaises(NativeDeviceToolError) as on_old:
            self.hub.accept_result(self.owner, channel, late)
        self.assertEqual(on_old.exception.code, "phone_unavailable")
        self.assertFalse(task.done())
        self.answer(fresh, request)
        await asyncio.wait_for(task, 2)

    async def test_a_request_is_never_on_two_channels(self) -> None:
        channel = self.connect()
        task = self.call()
        await asyncio.sleep(0)
        self.pause(channel)
        fresh = self.connect()
        self.assertEqual(len(self.feed(fresh)), 1)
        self.clock.advance(5)
        self.pause(fresh)
        third = self.connect()
        # The second channel's request was received, so the third never gets it.
        self.assertEqual(self.feed(third), [])
        self.assertEqual((await asyncio.wait_for(task, 2))["code"], "phone_unavailable")


WRITE_CALLS = (
    ("contacts.create", {"givenName": "Sam"}, "contacts"),
    ("alarms.create", {"label": "x", "timerSeconds": 60}, "alarms"),
    ("geofences.delete", {"id": "g1"}, "geofences"),
    ("photos.createAlbum", {"title": "Trip"}, "photos"),
    ("music.addToLibrary", {"id": "m1", "type": "songs"}, "music"),
    ("appearance.set", {"mode": "dark"}, "appearance"),
    ("shortcuts.run", {"name": "Log Water"}, "shortcuts"),
)
ALL_KINDS = ("calendar", "contacts", "alarms", "geofences", "photos", "music", "appearance", "shortcuts")


class DeliveredWriteTests(WaitCase):
    """A write the phone received and never answered may have happened. The agent must check, not retry."""

    def end(self, how: str, channel: dict) -> None:
        if how == "lease lapse":
            self.clock.advance(80)  # A shortcut holds its channel for the whole wait, 70 seconds.
            self.assertEqual(self.hub.native_channel_count("research", "chat-1"), 0)
        elif how == "paused close":
            self.pause(channel)
        elif how == "same-phone replacement":
            self.connect(enabled=ALL_KINDS)
        else:
            self.assertEqual(self.hub.close(self.owner, {"channelId": channel["channelId"]}), {"closed": True})

    async def test_a_delivered_write_is_outcome_unknown_however_the_channel_ends(self) -> None:
        for how in ("lease lapse", "paused close", "same-phone replacement", "plain close"):
            for operation, arguments, _kind in WRITE_CALLS:
                with self.subTest(how=how, operation=operation):
                    channel = self.connect(enabled=ALL_KINDS)
                    task = self.call(operation, arguments, call_id=f"{how}-{operation}")
                    await asyncio.sleep(0)
                    self.assertEqual(len(self.feed(channel)), 1)
                    self.end(how, channel)
                    result = await asyncio.wait_for(task, 2)
                    self.assertEqual((result["status"], result["code"]), ("failed", "outcome_unknown"))
                    self.assertIn("Do not run it again", result["payload"]["note"])
                    self.hub.close_profile("research")

    async def test_a_delivered_read_may_be_tried_again(self) -> None:
        for how in ("lease lapse", "paused close", "same-phone replacement", "plain close"):
            with self.subTest(how=how):
                channel = self.connect(enabled=ALL_KINDS)
                task = self.call("calendar.list", call_id=f"read-{how}")
                await asyncio.sleep(0)
                self.assertEqual(len(self.feed(channel)), 1)
                self.end(how, channel)
                result = await asyncio.wait_for(task, 2)
                self.assertEqual((result["code"], result["payload"]), ("phone_unavailable", {"note": NOTE}))
                self.hub.close_profile("research")

    async def test_a_write_the_phone_never_received_may_be_tried_again(self) -> None:
        channel = self.connect(enabled=ALL_KINDS)
        task = self.call("contacts.create", {"givenName": "Sam"})
        await asyncio.sleep(0)
        self.hub.close(self.owner, {"channelId": channel["channelId"]})
        result = await asyncio.wait_for(task, 2)
        self.assertEqual((result["code"], result["payload"]), ("phone_unavailable", {"note": NOTE}))

    async def test_a_delivered_write_that_times_out_is_outcome_unknown(self) -> None:
        channel = self.connect(enabled=ALL_KINDS)

        async def fake_wait_for(awaitable, timeout):
            self.assertEqual(len(self.feed(channel)), 1)  # The phone received it, then went quiet.
            raise asyncio.TimeoutError

        with patch("loopdy_plugin.native_device_tools.asyncio.wait_for", fake_wait_for):
            result = await self.call("contacts.create", {"givenName": "Sam"})
        self.assertEqual(result["code"], "outcome_unknown")

    async def test_a_request_that_timed_out_unpolled_is_never_handed_to_the_phone_later(self) -> None:
        async def fake_wait_for(awaitable, timeout):
            raise asyncio.TimeoutError

        channel = self.connect(enabled=ALL_KINDS)
        with patch("loopdy_plugin.native_device_tools.asyncio.wait_for", fake_wait_for):
            result = await self.call("contacts.create", {"givenName": "Sam"})
        self.assertEqual(result["code"], "phone_unavailable")
        self.assertEqual(self.feed(channel), [])

    def test_only_an_explicit_list_of_reads_may_be_tried_again(self) -> None:
        from loopdy_plugin.device_tool_contract import DEVICE_TOOL_OPERATIONS

        reads = ndt.PURE_READ_OPERATIONS
        self.assertTrue(reads <= DEVICE_TOOL_OPERATIONS)
        self.assertEqual(reads, {
            "health.read", "calendar.list", "reminders.list", "location.current", "appearance.get",
            "contacts.list", "photos.list", "photos.albums", "photos.get", "geofences.list", "alarms.list",
            "music.nowPlaying", "music.search", "music.library", "focus.current", "shortcuts.list",
        })
        for operation in DEVICE_TOOL_OPERATIONS:
            expected = "phone_unavailable" if operation in reads else "outcome_unknown"
            if operation in ("shortcuts.run", "photos.save"):
                expected = "outcome_unknown"
            self.assertEqual(ndt._unanswered(operation, delivered=True), expected, operation)
        # An operation nobody listed counts as a write.
        self.assertEqual(ndt._unanswered("future.list", delivered=True), "outcome_unknown")
        self.assertEqual(ndt._unanswered("future.list", delivered=False), "phone_unavailable")


class SessionValidatorFallbackTests(unittest.TestCase):
    def test_a_profile_without_a_store_yet_is_found_by_its_live_chat(self) -> None:
        class MissingStore:
            def __init__(self, *args, **kwargs):
                raise OSError("no state.db yet")

        live = {"runtime-1": {"session_key": "chat-1", "profile_home": "/homes/research"}}
        with patch("hermes_cli.profiles.profile_exists", return_value=True), \
                patch("hermes_cli.profiles.get_profile_dir", side_effect=lambda name: Path("/homes") / name), \
                patch("hermes_constants.get_process_hermes_home", return_value=Path("/homes/default")), \
                patch("hermes_constants.profile_name_for_home", side_effect=lambda home: Path(home).name), \
                patch("hermes_state.SessionDB", MissingStore), \
                patch.object(ndt, "_live_sessions_snapshot", return_value=live):
            self.assertTrue(ndt._default_profile_session_validator("research", "runtime-1"))
            self.assertTrue(ndt._default_profile_session_validator("research", "chat-1"))
            # Another profile's live chat is never taken for this one, and an unknown chat is refused.
            self.assertFalse(ndt._default_profile_session_validator("garden", "chat-1"))
            self.assertFalse(ndt._default_profile_session_validator("research", "chat-9"))

    def test_a_database_error_for_a_chat_that_is_not_live_is_still_a_refusal(self) -> None:
        import sqlite3

        class BrokenStore:
            def __init__(self, *args, **kwargs):
                raise sqlite3.OperationalError("unable to open database file")

        with patch("hermes_cli.profiles.profile_exists", return_value=True), \
                patch("hermes_cli.profiles.get_profile_dir", side_effect=lambda name: Path("/homes") / name), \
                patch("hermes_constants.get_process_hermes_home", return_value=Path("/homes/default")), \
                patch("hermes_state.SessionDB", BrokenStore), \
                patch.object(ndt, "_live_sessions_snapshot", return_value={}):
            self.assertFalse(ndt._default_profile_session_validator("research", "chat-1"))


class HubRegistryKeyTests(IsolatedRegistry):
    def test_copies_of_the_same_code_share_a_hub_and_other_code_never_does(self) -> None:
        serving = load_instance(self, "serving")
        self.assertIs(serving._HUB, load_instance(self, "research")._HUB)
        folder = tempfile.mkdtemp(dir="/private/tmp")
        self.addCleanup(shutil.rmtree, folder, True)
        changed = Path(folder) / "loopdy_plugin"
        shutil.copytree(Path(native_device_tools_file()).parent, changed, ignore=shutil.ignore_patterns("__pycache__"))
        with open(changed / "native_device_tools.py", "a", encoding="utf-8") as handle:
            handle.write("\n# a later release of the hub\n")
        reloaded = load_instance(self, "reloaded", root=changed)
        self.assertNotEqual(reloaded._SOURCE_DIGEST, serving._SOURCE_DIGEST)
        self.assertIsNot(reloaded._HUB, serving._HUB)

    def test_the_digest_is_part_of_the_key(self) -> None:
        self.assertIsNot(ndt._shared_hub(digest="one"), ndt._shared_hub(digest="two"))
        self.assertIs(ndt._shared_hub(digest="one"), ndt._shared_hub(digest="one"))
        self.assertRegex(ndt._SOURCE_DIGEST, r"\A[0-9a-f]{16,}\Z")


class LimitTests(WaitCase):
    async def test_one_chat_cannot_make_every_other_chat_busy(self) -> None:
        self.connect(session="chat-1")
        self.connect(session="chat-2")
        self.clock.advance(31)
        tasks = [self.call(call_id=f"call-{index}") for index in range(ndt.MAX_PARKED_PER_CHAT)]
        await asyncio.sleep(0)
        crowded = await asyncio.wait_for(self.call(call_id="one-too-many"), 2)
        self.assertEqual(crowded["code"], "busy")
        other = self.call(call_id="other-chat", session="chat-2")
        await asyncio.sleep(0)
        self.assertFalse(other.done())
        self.hub.close_profile("research")
        for task in (*tasks, other):
            await asyncio.wait_for(task, 2)

    async def test_the_global_ceiling_still_holds(self) -> None:
        self.assertGreaterEqual(ndt.MAX_QUEUE, ndt.MAX_PARKED_PER_CHAT)
        self.assertLess(ndt.MAX_PARKED_PER_CHAT, ndt.MAX_QUEUE)

    async def test_unloading_a_profile_forgets_everything_it_kept(self) -> None:
        channel = self.connect(enabled=ALL_KINDS)
        task = self.call("shortcuts.run", {"name": "Log Water"})
        await asyncio.sleep(0)
        self.assertEqual(len(self.feed(channel)), 1)
        self.pause(channel)
        await asyncio.wait_for(task, 2)
        self.assertTrue(self.hub._finished)
        self.assertTrue(self.hub._lapses)
        self.hub.close_profile("garden")
        self.assertTrue(self.hub._finished)
        self.hub.close_profile("research")
        self.assertEqual((len(self.hub._finished), len(self.hub._lapses), len(self.hub._parked)), (0, 0, 0))

    async def test_connect_checks_the_session_store_off_the_event_loop(self) -> None:
        serving = load_instance(self, "serving")
        threads: list[int] = []

        def checking(profile, session):
            threads.append(threading.get_ident())
            return True

        serving._HUB.profile_session_validator = checking
        serving._HUB.profile_validator = lambda profile: True
        owner = native_context.NativeContext(
            None, None, None, "default", (serving.CAPABILITY,), native_context.RUNTIME_ID)
        body = fields(agent="research")

        class Request:
            query_params = QueryParams()
            headers = Headers({
                "content-type": "application/json", "if-match": owner.etag,
                "x-loopdy-request-id": str(uuid.uuid4()),
            })

            async def stream(self):
                yield json.dumps(body).encode()

        with patch.object(serving, "available", return_value=True), \
                patch.object(native_api, "native_context", return_value=owner):
            await serving.request("connect", Request(), owner=owner, auth_module=native_api)
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())


class SavePathProfileTests(unittest.TestCase):
    def test_the_picture_check_runs_in_the_home_of_the_agent_that_asked(self) -> None:
        import hermes_constants

        seen: list[Path] = []

        def check(raw):
            seen.append(Path(hermes_constants.get_hermes_home()))
            return raw

        with patch("hermes_cli.profiles.get_profile_dir", side_effect=lambda name: Path("/homes") / name), \
                patch.object(device_photos, "check_save_path", check):
            with device_photos.profile_scope("research"):
                device_photos.check_save_path("/x.png")
        self.assertEqual(seen, [Path("/homes/research")])
        self.assertNotEqual(Path(hermes_constants.get_hermes_home()), Path("/homes/research"))

    def test_a_home_that_cannot_be_found_is_a_refusal(self) -> None:
        with patch("hermes_cli.profiles.get_profile_dir", side_effect=ValueError):
            with self.assertRaises(device_photos.PhotoRefusal) as error:
                with device_photos.profile_scope("../x"):
                    pass
        self.assertEqual(error.exception.code, "files_unavailable")


class PhoneUnavailableNoteTests(unittest.TestCase):
    def test_the_failure_keeps_its_shape_and_tells_the_agent_what_to_ask(self) -> None:
        result = ndt._failed_result(None, "phone_unavailable")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "phone_unavailable")
        self.assertEqual(result["payload"], {"note": NOTE})
        self.assertEqual(ndt.PHONE_UNAVAILABLE_NOTE, NOTE)

    def test_other_failures_keep_their_payload(self) -> None:
        self.assertEqual(ndt._failed_result(None, "busy")["payload"], {})
        self.assertIn("Do not run it again", ndt._failed_result(None, "outcome_unknown")["payload"]["note"])


class ReviewRequiredNoteTests(unittest.TestCase):
    """The phone refuses contacts.create and calendar.create in a chat holding the person's picture."""

    def refused(self, operation: str, payload: dict) -> dict:
        result = {"version": 1, "type": "device.tool.result", "requestId": "r" * 32, "operation": operation,
                  "status": "failed", "code": "review_required", "payload": payload}
        return ndt.NativeDeviceToolHub._for_agent(None, operation, result)

    def test_the_agent_is_told_to_use_the_draft_tools(self) -> None:
        for operation in ("contacts.create", "calendar.create"):
            with self.subTest(operation=operation):
                result = self.refused(operation, {"note": "Use a draft.", "kind": "contact"})
                self.assertEqual(("failed", "review_required"), (result["status"], result["code"]))
                self.assertEqual("contact", result["payload"]["kind"])
                note = result["payload"]["note"]
                self.assertIn("bighelp_draft_contact", note)
                self.assertIn("bighelp_draft_event", note)
                self.assertIn("Don't try again", note)

    def test_other_results_are_left_alone(self) -> None:
        result = {"status": "failed", "code": "permission_denied", "payload": {}}
        self.assertEqual(result, ndt.NativeDeviceToolHub._for_agent(None, "contacts.create", result))


class AdvertisedFeatureTests(unittest.TestCase):
    def test_both_features_are_named(self) -> None:
        self.assertEqual(ndt.AGENTS_CAPABILITY, "native-device-tools-agents-v1")
        self.assertEqual(ndt.WAIT_CAPABILITY, "native-device-tools-wait-v1")

    def test_the_context_lists_them_with_the_other_device_tool_features(self) -> None:
        with patch.object(ndt, "available", return_value=True), \
                patch("hermes_constants.profile_name_for_home", return_value="research"):
            _profile, features = native_context._native_features()
        for feature in (ndt.AGENTS_CAPABILITY, ndt.WAIT_CAPABILITY):
            self.assertIn(feature, features)
            self.assertGreater(features.index(feature), features.index(ndt.CAPABILITY))

    def test_the_context_leaves_them_out_when_device_tools_are_not_available(self) -> None:
        with patch.object(ndt, "available", return_value=False), \
                patch("hermes_constants.profile_name_for_home", return_value="research"):
            _profile, features = native_context._native_features()
        self.assertNotIn(ndt.AGENTS_CAPABILITY, features)
        self.assertNotIn(ndt.WAIT_CAPABILITY, features)


if __name__ == "__main__":
    unittest.main()
