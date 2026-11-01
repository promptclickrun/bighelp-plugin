"""An iPhone tool that is off on the phone asks the person with a card, and the call waits for the answer.

The phone lists such tools in `askable` when it connects (only to plugins that list
`native-device-permission-request-v1`). A call for one reaches the phone with a five-minute window, the channel's
lease lasts that long, and the agent gets the call's own result after Turn on or a plain refusal.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import unittest
from unittest.mock import patch
import uuid

from loopdy_plugin import native_context, native_device_tools as ndt
from loopdy_plugin.device_tool_contract import (
    device_tool_request,
    device_tool_result,
    parse_device_tool_request,
)
from loopdy_plugin.native_device_tools import NativeDeviceToolHub


DECLINED_NOTE = ("The person chose not to turn on this iPhone tool in this chat. Answer without it, and don't "
                 "call it again in this chat unless they ask.")
DENIED_NOTE = ("iOS didn't allow bighelp to use this. The person can allow it in the iPhone's Settings app under "
               "bighelp, then ask again.")
TIMEOUT_NOTE = "Nobody answered the request to turn on this iPhone tool. Ask the person if they want to try again."


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


def fields(*, device: str, enabled: tuple[str, ...] = ("calendar",),
           askable: tuple[str, ...] | None = ("reminders", "health")) -> dict:
    value = {
        "channelId": str(uuid.uuid4()), "deviceId": device, "hostId": "local-host", "authorizationEpoch": 1,
        "agentId": "default", "sessionId": "chat-1", "enabled": list(enabled),
    }
    if askable is not None:
        value["askable"] = list(askable)
    return value


class PermissionRequestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.owner = Owner()
        self.phone = str(uuid.uuid4())
        self.hub = NativeDeviceToolHub(
            clock=self.clock,
            profile_session_validator=lambda profile, session: (profile, session) == ("default", "chat-1"),
        )

    def connect(self, **changes) -> dict:
        channel = fields(device=self.phone, **changes)
        # The route checks the body with the same model before the hub sees it.
        self.hub.connect(self.owner, ndt._Connect(**channel).model_dump())
        return channel

    def call(self, operation: str = "reminders.list", arguments: dict | None = None, *, call_id: str = "call-1"):
        return asyncio.create_task(self.hub.execute(
            profile="default", session_id="chat-1", turn_id="turn-1", tool_call_id=call_id,
            operation=operation, arguments=arguments or {}))

    def feed(self, channel: dict) -> list[dict]:
        return [row["request"] for row in self.hub.poll(self.owner, channel, after=0)["requests"]]

    def answer(self, channel: dict, request: dict, *, status: str = "completed", payload: dict | None = None,
               code: str | None = None) -> None:
        self.hub.accept_result(self.owner, channel, device_tool_result(
            request=request, status=status, payload=payload if payload is not None else {"items": []},
            sent_at=int(self.clock.value), code=code))


class AskThePersonTests(PermissionRequestCase):
    async def test_a_tool_that_is_off_but_askable_reaches_the_phone_with_five_minutes(self) -> None:
        channel = self.connect()
        task = self.call()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        [request] = self.feed(channel)
        self.assertEqual(request["operation"], "reminders.list")
        self.assertEqual(request["expiresAt"] - request["sentAt"], ndt.PERMISSION_REQUEST_SECONDS)
        self.assertEqual(ndt.PERMISSION_REQUEST_SECONDS, 300)
        # The phone stops polling while its card is open, so the lease lasts until the call's deadline.
        self.assertGreaterEqual(self.hub.channels[channel["channelId"]].hold_until,
                                request["expiresAt"])
        self.hub.close_profile("default")
        await asyncio.wait_for(task, 2)

    async def test_the_call_waits_past_the_lease_and_gets_its_own_result_after_turn_on(self) -> None:
        channel = self.connect()
        task = self.call()
        await asyncio.sleep(0)
        [request] = self.feed(channel)
        # Far past the 30-second lease: the card is still open and nobody polls.
        self.clock.advance(ndt.LEASE_SECONDS + 1)
        self.assertEqual(self.hub.native_channel_count("default", "chat-1"), 1)
        self.clock.advance(200)
        self.assertEqual(self.hub.native_channel_count("default", "chat-1"), 1)
        self.assertFalse(task.done())
        items = [{"id": "r1", "title": "Call the dentist", "completed": False}]
        self.answer(channel, request, payload={"items": items})
        result = await asyncio.wait_for(task, 2)
        self.assertEqual((result["status"], result["payload"]), ("completed", {"items": items}))

    async def test_not_now_is_a_plain_refusal(self) -> None:
        channel = self.connect()
        task = self.call()
        await asyncio.sleep(0)
        [request] = self.feed(channel)
        self.answer(channel, request, status="failed", payload={}, code="permission_declined")
        result = await asyncio.wait_for(task, 2)
        self.assertEqual((result["status"], result["code"]), ("failed", "permission_declined"))
        self.assertEqual(result["payload"]["note"], DECLINED_NOTE)

    async def test_ios_saying_no_is_a_plain_refusal(self) -> None:
        channel = self.connect()
        task = self.call("health.read", {
            "types": ["sleep_analysis"], "start": "2026-01-01T00:00:00Z", "end": "2026-01-08T00:00:00Z",
            "timeZone": "UTC",
        })
        await asyncio.sleep(0)
        [request] = self.feed(channel)
        self.answer(channel, request, status="failed", payload={}, code="permission_denied")
        result = await asyncio.wait_for(task, 2)
        self.assertEqual((result["status"], result["code"]), ("failed", "permission_denied"))
        self.assertEqual(result["payload"]["note"], DENIED_NOTE)

    async def test_a_card_nobody_answered_is_a_plain_refusal(self) -> None:
        channel = self.connect()
        task = self.call()
        await asyncio.sleep(0)
        [request] = self.feed(channel)
        self.clock.advance(ndt.PERMISSION_REQUEST_SECONDS - 5)
        self.answer(channel, request, status="failed", payload={}, code="permission_timeout")
        result = await asyncio.wait_for(task, 2)
        self.assertEqual((result["status"], result["code"]), ("failed", "permission_timeout"))
        self.assertEqual(result["payload"]["note"], TIMEOUT_NOTE)

    async def test_the_host_gives_up_at_the_deadline_when_the_phone_never_answers(self) -> None:
        waits = []
        channel = self.connect()

        async def fake_wait_for(awaitable, timeout):
            waits.append(timeout)
            # The phone takes the request and never answers.
            self.feed(channel)
            raise asyncio.TimeoutError

        for operation, arguments, code in (
            ("reminders.list", {}, "phone_unavailable"),
            ("reminders.create", {"title": "Water the plants"}, "outcome_unknown"),
        ):
            with self.subTest(operation=operation), \
                    patch("loopdy_plugin.native_device_tools.asyncio.wait_for", fake_wait_for):
                result = await self.call(operation, arguments, call_id=f"call-{operation}")
                self.assertEqual(waits[-1], ndt.PERMISSION_REQUEST_SECONDS)
                self.assertEqual((result["status"], result["code"]), ("failed", code))

    async def test_a_tool_that_is_neither_on_nor_askable_is_refused_at_once(self) -> None:
        self.connect()
        result = await asyncio.wait_for(self.call("contacts.list", {}), 2)
        self.assertEqual(result["code"], "authorization_required")

    async def test_a_tool_that_is_on_keeps_the_usual_window(self) -> None:
        channel = self.connect()
        task = self.call("calendar.list", {
            "start": "2026-01-01T00:00:00Z", "end": "2026-01-01T01:00:00Z", "timeZone": "UTC"})
        await asyncio.sleep(0)
        [request] = self.feed(channel)
        self.assertEqual(request["expiresAt"] - request["sentAt"], ndt.LEASE_SECONDS)
        self.assertEqual(self.hub.channels[channel["channelId"]].hold_until, 0.0)
        self.answer(channel, request)
        await asyncio.wait_for(task, 2)


class OlderPhoneTests(PermissionRequestCase):
    async def test_a_phone_that_sends_no_askable_list_still_gets_authorization_required(self) -> None:
        channel = fields(device=self.phone, askable=None)
        self.assertEqual(ndt._Connect(**channel).askable, [])
        self.hub.connect(self.owner, channel)
        result = await asyncio.wait_for(self.call(), 2)
        self.assertEqual((result["status"], result["code"]), ("failed", "authorization_required"))


class WaitingCallTests(PermissionRequestCase):
    async def test_a_call_that_waits_for_its_phone_keeps_the_askable_window(self) -> None:
        self.connect()
        self.clock.advance(ndt.LEASE_SECONDS + 5)
        task = self.call()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        sent_at = int(self.clock.value)
        fresh = self.connect()
        [request] = self.feed(fresh)
        self.assertEqual((request["sentAt"], request["expiresAt"]),
                         (sent_at, sent_at + ndt.PERMISSION_REQUEST_SECONDS))
        self.assertGreaterEqual(self.hub.channels[fresh["channelId"]].hold_until, request["expiresAt"])
        self.answer(fresh, request)
        self.assertEqual((await asyncio.wait_for(task, 2))["status"], "completed")

    async def test_a_waiting_call_runs_when_the_phone_comes_back_with_the_tool_on(self) -> None:
        self.connect()
        self.clock.advance(ndt.LEASE_SECONDS + 5)
        task = self.call()
        await asyncio.sleep(0)
        fresh = self.connect(enabled=("calendar", "reminders"), askable=("health",))
        [request] = self.feed(fresh)
        self.answer(fresh, request)
        self.assertEqual((await asyncio.wait_for(task, 2))["status"], "completed")

    async def test_a_retry_that_joins_a_permission_request_keeps_its_window(self) -> None:
        self.connect()
        self.clock.advance(ndt.LEASE_SECONDS + 5)
        first = self.call()
        await asyncio.sleep(0)
        # The phone comes back with the tool on; the waiting request moves to the new channel.
        self.connect(enabled=("calendar", "reminders"), askable=())
        waits = []

        async def fake_wait_for(awaitable, timeout):
            waits.append(timeout)
            raise asyncio.TimeoutError

        with patch("loopdy_plugin.native_device_tools.asyncio.wait_for", fake_wait_for):
            await self.call()
        self.assertEqual(waits, [ndt.PERMISSION_REQUEST_SECONDS])
        self.hub.close_profile("default")
        await asyncio.wait_for(first, 2)

    async def test_a_waiting_call_is_refused_when_the_phone_comes_back_without_the_tool(self) -> None:
        self.connect()
        self.clock.advance(ndt.LEASE_SECONDS + 5)
        task = self.call()
        await asyncio.sleep(0)
        self.connect(askable=())
        self.assertEqual((await asyncio.wait_for(task, 2))["code"], "authorization_required")


class ConnectValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = fields(device=str(uuid.uuid4()))

    def test_askable_takes_the_kinds_that_share_data(self) -> None:
        kinds = ["health", "calendar", "reminders", "location", "contacts", "photos", "geofences", "alarms",
                 "music", "focus", "shortcuts"]
        model = ndt._Connect(**{**self.base, "enabled": [], "askable": kinds})
        self.assertEqual(model.askable, kinds)
        self.assertEqual(model.enabled, [])

    def test_askable_refuses_appearance_navigation_unknown_kinds_and_duplicates(self) -> None:
        for askable in (["appearance"], ["navigation"], ["camera"], ["reminders", "reminders"], "reminders"):
            with self.subTest(askable=askable), self.assertRaises(ValueError):
                ndt._Connect(**{**self.base, "askable": askable})

    def test_a_kind_cannot_be_both_on_and_askable(self) -> None:
        with self.assertRaises(ValueError):
            ndt._Connect(**{**self.base, "enabled": ["calendar", "reminders"], "askable": ["reminders"]})


class ContractWindowTests(unittest.TestCase):
    def request(self, seconds: int, **keywords) -> dict:
        return device_tool_request(
            request_id="a" * 64, device_id=str(uuid.uuid4()), host_id="local-host", authorization_epoch=1,
            session_id="chat-1", agent_id="default", turn_id="turn-1", operation="reminders.list",
            arguments={}, sent_at=1_700_000_000, expires_at=1_700_000_000 + seconds, **keywords)

    def test_requests_keep_the_one_minute_window_by_default(self) -> None:
        self.assertEqual(self.request(60)["expiresAt"] - self.request(60)["sentAt"], 60)
        with self.assertRaises(ValueError):
            self.request(61)

    def test_a_permission_request_may_wait_five_minutes_and_no_longer(self) -> None:
        request = self.request(300, max_window=ndt.PERMISSION_REQUEST_SECONDS)
        self.assertEqual(parse_device_tool_request(request, max_window=300), request)
        with self.assertRaises(ValueError):
            parse_device_tool_request(request)
        with self.assertRaises(ValueError):
            self.request(301, max_window=ndt.PERMISSION_REQUEST_SECONDS)
        with self.assertRaises(ValueError):
            self.request(20, max_window=301)


class AdvertisedFeatureTests(unittest.TestCase):
    def test_the_feature_is_named(self) -> None:
        self.assertEqual(ndt.PERMISSION_REQUEST_CAPABILITY, "native-device-permission-request-v1")

    def test_the_context_lists_it_with_the_other_device_tool_features(self) -> None:
        with patch.object(ndt, "available", return_value=True), \
                patch("hermes_constants.profile_name_for_home", return_value="default"):
            _profile, features = native_context._native_features()
        self.assertIn(ndt.PERMISSION_REQUEST_CAPABILITY, features)
        self.assertGreater(features.index(ndt.PERMISSION_REQUEST_CAPABILITY), features.index(ndt.CAPABILITY))

    def test_the_context_leaves_it_out_when_device_tools_are_not_available(self) -> None:
        with patch.object(ndt, "available", return_value=False), \
                patch("hermes_constants.profile_name_for_home", return_value="default"):
            _profile, features = native_context._native_features()
        self.assertNotIn(ndt.PERMISSION_REQUEST_CAPABILITY, features)


if __name__ == "__main__":
    unittest.main()
