"""The iphone_location tool: schema, validation and the round trip to the phone."""

from __future__ import annotations

import asyncio
import json
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from loopdy_plugin.link_contracts import (
    DEVICE_TOOL_OPERATIONS,
    device_tool_request,
    device_tool_result,
    device_tool_status,
)

ROOT = Path(__file__).resolve().parents[1]
# A made-up place; never a real person's location.
PAYLOAD = {
    "latitude": 12.3456,
    "longitude": -65.4321,
    "horizontalAccuracyMeters": 25.0,
    "timestamp": "2026-10-03T17:00:00Z",
    "precise": True,
    "place": {"street": "100 Example Street", "city": "Sampleton", "country": "Exampleland"},
}


def _request(arguments: dict | None = None) -> dict:
    return device_tool_request(
        request_id="request-location-0001", device_id="phone-1", host_id="host-1",
        authorization_epoch=7, session_id="session-1", agent_id="default", turn_id="turn-1",
        operation="location.current", arguments={} if arguments is None else arguments,
        sent_at=100, expires_at=130,
    )


class LocationContractTests(unittest.TestCase):
    def test_current_location_is_a_known_operation_without_arguments(self):
        self.assertIn("location.current", DEVICE_TOOL_OPERATIONS)
        request = _request()
        self.assertEqual(request["operation"], "location.current")
        self.assertEqual(request["arguments"], {})
        result = device_tool_result(request=request, status="completed", payload=PAYLOAD, sent_at=101)
        self.assertEqual(result["payload"], PAYLOAD)

    def test_current_location_rejects_any_argument(self):
        for arguments in ({"precise": True}, {"id": "x"}, {"start": "2026-10-03T00:00:00Z"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                _request(arguments)

    def test_phone_status_may_enable_location(self):
        status = device_tool_status(
            device_id="phone-1", host_id="host-1", authorization_epoch=7,
            enabled=["health", "calendar", "reminders", "location"], available=True, sent_at=100,
        )
        self.assertEqual(status["enabled"], ["health", "calendar", "reminders", "location"])
        with self.assertRaises(ValueError):
            device_tool_status(
                device_id="phone-1", host_id="host-1", authorization_epoch=7,
                enabled=["location", "location"], available=True, sent_at=100,
            )


class LocationToolSchemaTests(unittest.TestCase):
    def _register(self, bridge=None):
        from loopdy_plugin.device_tools import register

        context = SimpleNamespace(tools={}, schemas={})
        context.register_tool = lambda *, name, handler, schema, **kwargs: (
            context.tools.update({name: handler}) or context.schemas.update({name: schema}))
        register(context, bridge=bridge)
        return context

    def test_tool_schema_has_one_current_operation(self):
        schema = self._register().schemas["iphone_location"]
        self.assertEqual(schema["name"], "iphone_location")
        parameters = schema["parameters"]
        self.assertEqual(parameters["required"], ["operation"])
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(parameters["properties"], {"operation": {"type": "string", "enum": ["current"]}})
        description = schema["description"]
        self.assertIn("approximate", description)
        self.assertIn("foreground", description)

    def test_handler_sends_location_current_with_official_coordinates(self):
        from loopdy_plugin.device_tools import DeviceToolBridge

        bridge = DeviceToolBridge()
        bridge.execute = AsyncMock(return_value={"status": "completed", "payload": PAYLOAD})
        context = self._register(bridge)
        execution = SimpleNamespace(
            source="loopdy_link", owner_id="phone-1", scope_id="default",
            authorization_epoch=7, attributes={"host_id": "host-1"},
        )
        result = asyncio.run(context.tools["iphone_location"](
            {"operation": "current"}, tool_execution_context=execution,
            session_id="session-1", turn_id="turn-1", tool_call_id="call-1",
        ))
        self.assertEqual(json.loads(result)["payload"], PAYLOAD)
        request = bridge.execute.await_args.kwargs
        self.assertEqual(request["operation"], "location.current")
        self.assertEqual(request["arguments"], {})
        for payload in ({"operation": "list"}, {}, {"operation": "current", "precise": True}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                asyncio.run(context.tools["iphone_location"](
                    payload, tool_execution_context=execution,
                    session_id="session-1", turn_id="turn-1", tool_call_id="call-2",
                ))

    def test_manifest_and_agent_guidance_name_the_tool(self):
        from loopdy_plugin.agent_guide import CHAT_BRIEF

        self.assertIn("  - iphone_location\n", (ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertIn("iphone_location", CHAT_BRIEF)
        self.assertIn("iphone_location", (ROOT / "skills" / "bighelp" / "SKILL.md").read_text(encoding="utf-8"))


class LocationLinkBridgeTests(unittest.TestCase):
    def test_location_is_never_kept_for_a_retry(self):
        from loopdy_plugin.device_tools import DeviceToolBridge

        bridge = None

        class Client:
            connected = True
            peer_capabilities = {"directed-frames-v1"}
            config = SimpleNamespace(device_id="host-1")

            async def send_payload(self, request, **_kwargs):
                bridge.accept_result(
                    device_tool_result(request=request, status="completed", payload=PAYLOAD, sent_at=101),
                    sender_device_id="phone-1", sender_epoch=7, target_device_id="host-1",
                )

        bridge = DeviceToolBridge(Client(), clock=lambda: 100)
        self.assertTrue(bridge.accept_status(
            device_tool_status(device_id="phone-1", host_id="host-1", authorization_epoch=7,
                               enabled=["location"], available=True, sent_at=100),
            sender_device_id="phone-1", sender_epoch=7, target_device_id="host-1",
        ))
        context = SimpleNamespace(
            source="loopdy_link", owner_id="phone-1", scope_id="default",
            authorization_epoch=7, attributes={"host_id": "host-1"},
        )
        result = asyncio.run(bridge.execute(
            context=context, device_id="phone-1", host_id="host-1", authorization_epoch=7,
            session_id="session-1", agent_id="default", turn_id="turn-1", tool_call_id="call-1",
            operation="location.current", arguments={},
        ))
        self.assertEqual(result["payload"], PAYLOAD)
        self.assertFalse(bridge._outcomes)


class LocationNativeChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from loopdy_plugin.native_device_tools import NativeDeviceToolHub

        self.now = 1_700_000_000
        self.owner = SimpleNamespace(provider="test", user_id="user-1",
                                     serving_profile_id="default", runtime_id="runtime-1")
        self.hub = NativeDeviceToolHub(clock=lambda: self.now,
                                       profile_session_validator=lambda profile, session: True)

    def _fields(self, enabled: list[str]) -> dict:
        return {
            "channelId": str(uuid.uuid4()), "deviceId": str(uuid.uuid4()), "hostId": "local-host",
            "authorizationEpoch": 1, "agentId": "default", "sessionId": "stored-session",
            "enabled": enabled,
        }

    def test_capability_names(self):
        from loopdy_plugin.native_device_tools import LOCATION_CAPABILITY, _Connect

        self.assertEqual(LOCATION_CAPABILITY, "native-device-location-v1")
        fields = self._fields(["health", "calendar", "reminders", "location"])
        self.assertEqual(_Connect(**fields).enabled, ["health", "calendar", "reminders", "location"])

    async def test_round_trip_through_the_phone_channel(self):
        fields = self._fields(["location"])
        self.hub.connect(self.owner, fields)
        task = asyncio.create_task(self.hub.execute(
            profile="default", session_id="stored-session", turn_id="turn-1",
            tool_call_id="call-1", operation="location.current", arguments={},
        ))
        await asyncio.sleep(0)
        request = self.hub.poll(self.owner, fields, after=0)["requests"][0]["request"]
        self.assertEqual(request["operation"], "location.current")
        self.assertEqual(request["arguments"], {})
        result = device_tool_result(request=request, status="completed", payload=PAYLOAD, sent_at=self.now)
        self.assertEqual(self.hub.accept_result(self.owner, fields, result), {"accepted": True})
        self.assertEqual((await task)["payload"], PAYLOAD)

    async def test_location_needs_its_own_switch(self):
        fields = self._fields(["calendar", "reminders", "health"])
        self.hub.connect(self.owner, fields)
        denied = await self.hub.execute(
            profile="default", session_id="stored-session", turn_id="turn-1",
            tool_call_id="call-1", operation="location.current", arguments={},
        )
        self.assertEqual(denied["code"], "authorization_required")

    def test_middleware_maps_the_tool_and_drops_model_identity(self):
        from loopdy_plugin.native_device_tools import NativeDeviceToolError, _operation_for_tool

        self.assertEqual(
            _operation_for_tool("iphone_location", {"operation": "current", "sessionId": "forged"}),
            ("location.current", {}),
        )
        for args in ({"operation": "list"}, {}, "current"):
            with self.subTest(args=args), self.assertRaises(NativeDeviceToolError):
                _operation_for_tool("iphone_location", args)


if __name__ == "__main__":
    unittest.main()
