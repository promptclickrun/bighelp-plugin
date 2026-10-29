"""The iphone_navigation tool: argument checks, schema, gating and the round trip to the phone."""

from __future__ import annotations

import asyncio
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from jsonschema import Draft202012Validator

from loopdy_plugin.device_tool_contract import (
    DEVICE_TOOL_KINDS,
    DEVICE_TOOL_OPERATIONS,
    DEVICE_TOOL_READ_OPERATIONS,
    device_tool_request,
    device_tool_result,
)
from loopdy_plugin.native_device_tools import (
    KIND_CAPABILITIES,
    NativeDeviceToolError,
    NativeDeviceToolHub,
    _Connect,
    _operation_for_tool,
)

ROOT = Path(__file__).resolve().parents[1]
# Made-up pages, as the phone would answer them.
PLACES = {
    "places": [{
        "id": "settings.appearance", "title": "Appearance", "what": "How bighelp looks.",
        "steps": ["Open the menu", "Tap Settings", "Tap Appearance"], "needs": None,
    }],
    "total": 1,
}
OPENED = {"opened": "settings.appearance", "steps": ["Open the menu", "Tap Settings", "Tap Appearance"], "mode": "chat"}


def _request(operation: str, arguments: dict) -> dict:
    return device_tool_request(
        request_id="request-navigation-0001", device_id="phone-1", host_id="host-1",
        authorization_epoch=7, session_id="session-1", agent_id="default", turn_id="turn-1",
        operation=operation, arguments=arguments, sent_at=100, expires_at=130,
    )


class NavigationContractTests(unittest.TestCase):
    def test_both_operations_are_known_and_not_journaled(self):
        for operation in ("navigation.places", "navigation.open"):
            self.assertIn(operation, DEVICE_TOOL_OPERATIONS)
            self.assertIn(operation, DEVICE_TOOL_READ_OPERATIONS)
        self.assertIn("navigation", DEVICE_TOOL_KINDS)
        request = _request("navigation.places", {"query": "voice", "limit": 3})
        result = device_tool_result(request=request, status="completed", payload=PLACES, sent_at=101)
        self.assertEqual(result["payload"], PLACES)

    def test_places_takes_a_query_and_a_limit(self):
        for arguments in ({}, {"query": "v"}, {"query": "x" * 120}, {"limit": 1}, {"limit": 20}, {"query": "voice", "limit": 8}):
            with self.subTest(arguments=arguments):
                self.assertEqual(_request("navigation.places", arguments)["arguments"], arguments)

    def test_places_refuses_values_outside_the_contract(self):
        for arguments in (
            {"query": ""}, {"query": "x" * 121}, {"query": 5}, {"limit": 0}, {"limit": 21}, {"limit": True},
            {"limit": 8.0}, {"limit": "8"}, {"place": "settings.appearance"}, {"bot": "default"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                _request("navigation.places", arguments)

    def test_open_takes_a_place_and_optionally_a_bot_and_a_note(self):
        for arguments in (
            {"place": "settings.appearance"}, {"place": "a"}, {"place": "bot-profile.2" + "x" * 83},
            {"place": "bot.profile", "bot": "research"}, {"place": "bot.profile", "bot": "b" * 96},
            {"place": "settings.voice", "note": "This is where you pick the voice."}, {"place": "p", "note": "n" * 160},
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(_request("navigation.open", arguments)["arguments"], arguments)

    def test_open_refuses_values_outside_the_contract(self):
        for arguments in (
            {}, {"bot": "research"}, {"note": "Here."},
            {"place": ""}, {"place": "p" * 97}, {"place": "Settings.Appearance"}, {"place": "settings appearance"},
            {"place": "settings/appearance"}, {"place": "settings_appearance"}, {"place": "tabé"}, {"place": 7},
            {"place": "ok", "bot": ""}, {"place": "ok", "bot": "b" * 97}, {"place": "ok", "bot": 3},
            {"place": "ok", "note": ""}, {"place": "ok", "note": "n" * 161}, {"place": "ok", "note": ["x"]},
            {"place": "ok", "save": True}, {"place": "ok", "query": "voice"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                _request("navigation.open", arguments)


class NavigationToolSchemaTests(unittest.TestCase):
    def setUp(self):
        from loopdy_plugin.device_tools import register

        self.schemas = {}
        register(SimpleNamespace(register_tool=lambda *, name, schema, **_kwargs: self.schemas.update({name: schema})))
        self.schema = self.schemas["iphone_navigation"]

    def test_schema_is_strict_and_names_both_operations(self):
        parameters = self.schema["parameters"]
        Draft202012Validator.check_schema(parameters)
        self.assertEqual(parameters["required"], ["operation"])
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(set(parameters["properties"]), {"operation", "query", "limit", "place", "bot", "note"})
        self.assertEqual(parameters["properties"]["operation"]["enum"], ["places", "open"])
        self.assertEqual(parameters["properties"]["limit"], {"type": "integer", "maximum": 20})
        validator = Draft202012Validator(parameters)
        self.assertTrue(validator.is_valid({"operation": "open", "place": "settings.voice", "note": "Here."}))
        for arguments in ({}, {"operation": "close"}, {"operation": "open", "tap": "Save"}, {"operation": "places", "limit": 21}):
            with self.subTest(arguments=arguments):
                self.assertFalse(validator.is_valid(arguments))

    def test_description_tells_the_model_when_and_how(self):
        description = self.schema["description"]
        for words in ("where", "places", "open", "note", "never changes a setting", "Save, Send or Delete", "Tell the user"):
            self.assertIn(words, description)

    def test_manifest_brief_and_skill_name_the_tool(self):
        from loopdy_plugin.agent_guide import CHAT_BRIEF

        self.assertIn("  - iphone_navigation\n", (ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertIn("iphone_navigation", CHAT_BRIEF)
        self.assertIn("iphone_navigation", (ROOT / "skills" / "bighelp" / "SKILL.md").read_text(encoding="utf-8"))


class NavigationNativeChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
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

    def _execute(self, operation: str, arguments: dict, call_id: str = "call-1"):
        return self.hub.execute(
            profile="default", session_id="stored-session", turn_id="turn-1",
            tool_call_id=call_id, operation=operation, arguments=arguments,
        )

    def test_the_feature_name_and_the_enabled_kind(self):
        self.assertEqual(KIND_CAPABILITIES["navigation"], "native-device-navigation-v1")
        self.assertEqual(_Connect(**self._fields(["navigation"])).enabled, ["navigation"])
        self.assertEqual(_Connect(**self._fields(list(DEVICE_TOOL_KINDS))).enabled, list(DEVICE_TOOL_KINDS))

    async def test_places_and_open_round_trip_through_the_phone(self):
        fields = self._fields(["navigation"])
        self.hub.connect(self.owner, fields)
        for operation, arguments, answer in (
            ("navigation.places", {"query": "looks", "limit": 3}, PLACES),
            ("navigation.open", {"place": "settings.appearance", "note": "Pick a color here."}, OPENED),
        ):
            with self.subTest(operation=operation):
                task = asyncio.create_task(self._execute(operation, arguments, call_id=operation))
                await asyncio.sleep(0)
                request = self.hub.poll(self.owner, fields, after=0)["requests"][-1]["request"]
                self.assertEqual((request["operation"], request["arguments"]), (operation, arguments))
                result = device_tool_result(request=request, status="completed", payload=answer, sent_at=self.now)
                self.assertEqual(self.hub.accept_result(self.owner, fields, result), {"accepted": True})
                self.assertEqual((await task)["payload"], answer)

    async def test_a_phone_that_did_not_list_navigation_gets_nothing(self):
        fields = self._fields(["calendar", "reminders", "location", "appearance"])
        self.hub.connect(self.owner, fields)
        for operation, arguments in (("navigation.places", {}), ("navigation.open", {"place": "settings.voice"})):
            with self.subTest(operation=operation):
                self.assertEqual((await self._execute(operation, arguments))["code"], "authorization_required")
        self.assertEqual(self.hub.poll(self.owner, fields, after=0)["requests"], [])

    async def test_invalid_arguments_never_reach_the_phone(self):
        fields = self._fields(["navigation"])
        self.hub.connect(self.owner, fields)
        for operation, arguments in (
            ("navigation.places", {"limit": 21}), ("navigation.open", {}), ("navigation.open", {"place": "Settings"}),
            ("navigation.open", {"place": "ok", "note": "n" * 161}), ("navigation.open", {"place": "ok", "tap": "Save"}),
        ):
            with self.subTest(operation=operation, arguments=arguments):
                self.assertEqual((await self._execute(operation, arguments))["code"], "invalid_arguments")
        self.assertEqual(self.hub.poll(self.owner, fields, after=0)["requests"], [])

    def test_the_tool_maps_to_its_operations_and_drops_model_identity(self):
        self.assertEqual(
            _operation_for_tool("iphone_navigation", {"operation": "places", "query": "voice", "sessionId": "forged"}),
            ("navigation.places", {"query": "voice"}))
        self.assertEqual(
            _operation_for_tool("iphone_navigation", {"operation": "open", "place": "settings.voice", "deviceId": "x"}),
            ("navigation.open", {"place": "settings.voice"}))
        for args in ({"operation": "get"}, {"operation": "navigation.open"}, {}, "open"):
            with self.subTest(args=args), self.assertRaises(NativeDeviceToolError):
                _operation_for_tool("iphone_navigation", args)


if __name__ == "__main__":
    unittest.main()
