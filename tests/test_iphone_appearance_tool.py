"""The iphone_appearance tool: schema, validation and the round trip to the phone."""

from __future__ import annotations

import asyncio
import re
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from loopdy_plugin.device_tool_contract import (
    DEVICE_TOOL_OPERATIONS,
    DEVICE_TOOL_READ_OPERATIONS,
    device_tool_request,
    device_tool_result,
)

ROOT = Path(__file__).resolve().parents[1]
# What the phone answers: the look it now shows.
APPLIED = {"mode": "dark", "color": "rose"}


def _request(operation: str = "appearance.set", arguments: dict | None = None) -> dict:
    return device_tool_request(
        request_id="request-appearance-0001", device_id="phone-1", host_id="host-1",
        authorization_epoch=7, session_id="session-1", agent_id="default", turn_id="turn-1",
        operation=operation, arguments={"mode": "dark"} if arguments is None else arguments,
        sent_at=100, expires_at=130,
    )


class AppearanceContractTests(unittest.TestCase):
    def test_get_and_set_are_known_operations(self):
        self.assertIn("appearance.get", DEVICE_TOOL_OPERATIONS)
        self.assertIn("appearance.set", DEVICE_TOOL_OPERATIONS)
        self.assertIn("appearance.get", DEVICE_TOOL_READ_OPERATIONS)
        self.assertIn("appearance.set", DEVICE_TOOL_READ_OPERATIONS)  # Not journaled: twice is once.
        self.assertEqual(_request("appearance.get", {})["arguments"], {})
        request = _request(arguments={"mode": "dark", "color": "rose"})
        self.assertEqual(request["arguments"], {"mode": "dark", "color": "rose"})
        result = device_tool_result(request=request, status="completed", payload=APPLIED, sent_at=101)
        self.assertEqual(result["payload"], APPLIED)

    def test_set_takes_a_mode_a_color_name_or_a_hex_color(self):
        for arguments in (
            {"mode": "system"}, {"mode": "light"}, {"color": "ocean"}, {"color": "Sky"},
            {"color": "#0e7c66"}, {"color": "#0E7C66", "mode": "dark"},
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(_request(arguments=arguments)["arguments"], arguments)

    def test_set_refuses_nothing_to_change_and_values_outside_the_contract(self):
        for arguments in (
            {}, {"mode": "dim"}, {"mode": True}, {"color": ""}, {"color": "#12345"},
            {"color": "#1234567"}, {"color": "rgb(1,2,3)"}, {"color": "x" * 25}, {"color": 7},
            {"theme": "nous"}, {"mode": "dark", "layout": "focus"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                _request(arguments=arguments)

    def test_get_takes_no_arguments(self):
        for arguments in ({"mode": "dark"}, {"color": "rose"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                _request("appearance.get", arguments)

class AppearanceToolSchemaTests(unittest.TestCase):
    def _register(self):
        from loopdy_plugin.device_tools import register

        context = SimpleNamespace(tools={}, schemas={})
        context.register_tool = lambda *, name, handler, schema, **kwargs: (
            context.tools.update({name: handler}) or context.schemas.update({name: schema}))
        register(context)
        return context

    def test_tool_schema_is_strict_and_bounded(self):
        schema = self._register().schemas["iphone_appearance"]
        self.assertEqual(schema["name"], "iphone_appearance")
        parameters = schema["parameters"]
        self.assertEqual(parameters["required"], ["operation"])
        self.assertFalse(parameters["additionalProperties"])
        properties = parameters["properties"]
        self.assertEqual(set(properties), {"operation", "mode", "color"})
        self.assertEqual(properties["operation"]["enum"], ["get", "set"])
        self.assertEqual(properties["mode"]["enum"], ["system", "light", "dark"])
        self.assertEqual(properties["color"]["maxLength"], 24)
        pattern = re.compile(properties["color"]["pattern"])
        for value in ("rose", "Ocean", "#0E7C66"):
            self.assertTrue(pattern.fullmatch(value), value)
        for value in ("", "#0E7C6", "rgb(1,2,3)", "dark violet"):
            self.assertFalse(pattern.fullmatch(value), value)
        description = schema["description"]
        for words in ("dark", "#rrggbb", "Device access", "open"):
            self.assertIn(words, description)

    def test_manifest_and_agent_guidance_name_the_tool(self):
        from loopdy_plugin.agent_guide import CHAT_BRIEF

        self.assertIn("  - iphone_appearance\n", (ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertIn("iphone_appearance", CHAT_BRIEF)
        self.assertIn("iphone_appearance", (ROOT / "skills" / "bighelp" / "SKILL.md").read_text(encoding="utf-8"))


class AppearanceNativeChannelTests(unittest.IsolatedAsyncioTestCase):
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

    def _execute(self, operation: str = "appearance.set", arguments: dict | None = None):
        return self.hub.execute(
            profile="default", session_id="stored-session", turn_id="turn-1",
            tool_call_id="call-1", operation=operation,
            arguments={"mode": "dark", "color": "rose"} if arguments is None else arguments,
        )

    def test_capability_name_and_a_channel_with_only_appearance(self):
        from loopdy_plugin.native_device_tools import APPEARANCE_CAPABILITY, _Connect

        self.assertEqual(APPEARANCE_CAPABILITY, "native-device-appearance-v1")
        everything = ["health", "calendar", "reminders", "location", "appearance"]
        self.assertEqual(_Connect(**self._fields(everything)).enabled, everything)
        self.assertEqual(_Connect(**self._fields(["appearance"])).enabled, ["appearance"])
        with self.assertRaises(ValueError):
            _Connect(**self._fields(["appearance", "theme"]))

    async def test_round_trip_through_the_phone_channel(self):
        fields = self._fields(["appearance"])
        self.hub.connect(self.owner, fields)
        task = asyncio.create_task(self._execute())
        await asyncio.sleep(0)
        request = self.hub.poll(self.owner, fields, after=0)["requests"][0]["request"]
        self.assertEqual(request["operation"], "appearance.set")
        self.assertEqual(request["arguments"], {"mode": "dark", "color": "rose"})
        result = device_tool_result(request=request, status="completed", payload=APPLIED, sent_at=self.now)
        self.assertEqual(self.hub.accept_result(self.owner, fields, result), {"accepted": True})
        self.assertEqual((await task)["payload"], APPLIED)

    async def test_appearance_needs_its_own_switch(self):
        self.hub.connect(self.owner, self._fields(["calendar", "reminders", "location"]))
        denied = await self._execute()
        self.assertEqual(denied["code"], "authorization_required")

    async def test_invalid_values_never_reach_the_phone(self):
        fields = self._fields(["appearance"])
        self.hub.connect(self.owner, fields)
        for arguments in ({}, {"mode": "dim"}, {"color": "#12345"}):
            with self.subTest(arguments=arguments):
                result = await self._execute(arguments=arguments)
                self.assertEqual(result["code"], "invalid_arguments")
        self.assertEqual(self.hub.poll(self.owner, fields, after=0)["requests"], [])

    def test_middleware_maps_the_tool_and_drops_model_identity(self):
        from loopdy_plugin.native_device_tools import NativeDeviceToolError, _operation_for_tool

        self.assertEqual(
            _operation_for_tool("iphone_appearance", {"operation": "set", "mode": "light", "sessionId": "forged"}),
            ("appearance.set", {"mode": "light"}),
        )
        self.assertEqual(_operation_for_tool("iphone_appearance", {"operation": "get"}), ("appearance.get", {}))
        for args in ({"operation": "list"}, {}, "get"):
            with self.subTest(args=args), self.assertRaises(NativeDeviceToolError):
                _operation_for_tool("iphone_appearance", args)


if __name__ == "__main__":
    unittest.main()
