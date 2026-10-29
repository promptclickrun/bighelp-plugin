"""iphone_contacts, _photos, _geofences, _alarms, _music, _focus and _shortcuts, and reminders' places:
schemas, argument checks, the channel and the pictures that cross it. All data here is made up."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jsonschema import Draft202012Validator

from loopdy_plugin import device_photos, native_attachments as na
from loopdy_plugin.attachments import AttachmentStore
from loopdy_plugin.device_tool_arguments import OPERATIONS, validate_operation
from loopdy_plugin.device_tool_contract import (
    DEVICE_TOOL_KINDS,
    DEVICE_TOOL_LONG_OPERATIONS,
    DEVICE_TOOL_OPERATIONS,
    MAX_DEVICE_TOOL_ARGUMENT_BYTES,
    device_tool_request,
    device_tool_result,
    parse_device_tool_result,
)
from loopdy_plugin.native_device_tools import (
    KIND_CAPABILITIES,
    NativeDeviceToolError,
    NativeDeviceToolHub,
    _Connect,
    _operation_for_tool,
)

ROOT = Path(__file__).resolve().parents[1]
NEW_TOOLS = ("contacts", "photos", "geofences", "alarms", "music", "focus", "shortcuts")
ID = "id-0001"
REV = "rev-0001"
DATE = "2026-10-20T09:00:00-05:00"
LATER = "2026-10-21T09:00:00-05:00"

GOOD = {
    "contacts.list": [{}, {"query": "Sam", "limit": 50}],
    "contacts.create": [
        {"givenName": "Sam"},
        {"organization": "Example Co", "birthday": "--02-29", "nickname": "S", "jobTitle": "Baker",
         "phones": [{"label": "mobile", "value": "+1 555 0100"}], "emails": [{"value": "sam@example.com"}],
         "urls": [{"label": "work", "value": "https://example.com"}],
         "addresses": [{"label": "home", "street": "100 Example Street", "city": "Sampleton"}]},
        {"familyName": "Sample", "birthday": "1990-07-04"},
    ],
    "contacts.update": [
        {"id": ID, "expectedRevision": REV, "givenName": "Sam"},
        {"id": ID, "expectedRevision": REV, "givenName": "", "phones": [], "birthday": ""},
    ],
    "contacts.delete": [{"id": ID, "expectedRevision": REV}],
    "photos.list": [
        {}, {"start": DATE, "end": LATER, "albumID": "a1", "mediaType": "video", "favorites": True, "limit": 100},
    ],
    "photos.albums": [{}],
    "photos.get": [{"id": ID}, {"id": ID, "maxPixels": 1024}, {"id": ID, "maxPixels": 256}],
    "photos.save": [{"path": "/tmp/example.png"}, {"path": "/tmp/example.mov", "albumID": "a1"}],
    "photos.update": [
        {"id": ID, "expectedRevision": REV, "favorite": False},
        {"id": ID, "expectedRevision": REV, "addToAlbumIDs": ["a1"], "removeFromAlbumIDs": ["a2"]},
    ],
    "photos.createAlbum": [{"title": "Trip"}],
    "photos.delete": [{"ids": [ID]}, {"ids": [f"id-{n}" for n in range(50)]}],
    "geofences.list": [{}],
    "geofences.create": [
        {"name": "Office", "latitude": 12.3456, "longitude": -65.4321},
        {"name": "Here", "here": True, "radiusMeters": 5000, "on": "both", "note": "Say hi", "repeats": True},
        {"name": "Edge", "latitude": -90, "longitude": 180, "radiusMeters": 100, "on": "leave"},
    ],
    "geofences.delete": [{"id": ID}],
    "alarms.list": [{}],
    "alarms.create": [
        {"label": "Wake", "time": {"hour": 7, "minute": 30}},
        {"label": "Run", "time": {"hour": 0, "minute": 0}, "weekdays": ["monday", "friday"]},
        {"label": "Once", "date": DATE},
        {"label": "Tea", "timerSeconds": 5},
        {"label": "Long", "timerSeconds": 86_400},
    ],
    "alarms.update": [
        {"id": ID, "label": "New"},
        {"id": ID, "timerSeconds": 60},
        {"id": ID, "time": {"hour": 23, "minute": 59}, "weekdays": ["sunday"]},
    ],
    "alarms.delete": [{"id": ID}],
    "music.nowPlaying": [{}],
    "music.search": [{"query": "jazz"}, {"query": "jazz", "types": ["songs", "artists"], "source": "library", "limit": 25}],
    "music.library": [{}, {"type": "recent", "limit": 50}],
    "music.play": [
        {"query": "jazz"}, {"query": "jazz", "shuffle": True},
        {"id": "s1", "type": "song", "source": "catalog"},
    ],
    "music.control": [{"action": "pause"}, {"action": "previous"}],
    "music.createPlaylist": [{"name": "Mix"}, {"name": "Mix", "description": "d", "itemIDs": ["s1"] * 100}],
    "music.addToPlaylist": [{"playlistID": "p1", "itemIDs": ["s1"]}],
    "music.addToLibrary": [{"id": "s1", "type": "song"}],
    "focus.current": [{}],
    "shortcuts.list": [{}],
    "shortcuts.run": [{"name": "Log Water"}, {"name": "Log Water", "input": "one glass"}],
    # test_iphone_navigation_tool.py has the full set of cases.
    "navigation.places": [{}, {"query": "voice", "limit": 20}],
    "navigation.open": [{"place": "settings.voice"}, {"place": "bot.profile", "bot": "research", "note": "Here."}],
}

BAD = {
    "contacts.list": [{"query": ""}, {"query": "x" * 201}, {"limit": 0}, {"limit": 51}, {"limit": True}, {"id": ID}],
    "contacts.create": [
        {}, {"nickname": "S"}, {"givenName": ""}, {"givenName": "x" * 201}, {"givenName": "Sam", "phones": []},
        {"givenName": "Sam", "birthday": "1990-02-30"}, {"givenName": "Sam", "birthday": "--13-01"},
        {"givenName": "Sam", "birthday": "July 4"}, {"givenName": "Sam", "birthday": ""},
        {"givenName": "Sam", "phones": [{"label": "x"}]}, {"givenName": "Sam", "phones": [{"value": "1", "extra": 1}]},
        {"givenName": "Sam", "phones": [{"value": "1"}] * 11}, {"givenName": "Sam", "urls": [{"value": "u"}] * 6},
        {"givenName": "Sam", "addresses": [{"label": "home"}]}, {"givenName": "Sam", "addresses": [{"city": "c"}] * 6},
        {"givenName": "Sam", "phones": [{"label": "l" * 41, "value": "1"}]},
        {"givenName": "Sam", "notes": "secret"}, {"givenName": "Sam", "id": ID},
    ],
    "contacts.update": [
        {"id": ID, "expectedRevision": REV}, {"expectedRevision": REV, "givenName": "S"}, {"id": ID, "givenName": "S"},
        {"id": "x" * 513, "expectedRevision": REV, "givenName": "S"}, {"id": ID, "expectedRevision": REV, "birthday": "x"},
    ],
    "contacts.delete": [{"id": ID}, {"expectedRevision": REV}, {"id": ID, "expectedRevision": REV, "x": 1}],
    "photos.list": [
        {"start": DATE}, {"end": DATE}, {"start": LATER, "end": DATE}, {"start": "2026-10-20T09:00:00", "end": LATER},
        {"mediaType": "audio"}, {"limit": 101}, {"favorites": "yes"}, {"albumID": ""},
    ],
    "photos.albums": [{"limit": 1}],
    "photos.get": [{}, {"id": ID, "maxPixels": 255}, {"id": ID, "maxPixels": 1025}, {"id": ID, "maxPixels": 512.5}],
    "photos.save": [{}, {"path": ""}, {"path": "x" * 4097}, {"path": "/a", "album": "x"}, {"path": 5}],
    "photos.update": [
        {"id": ID, "expectedRevision": REV}, {"id": ID, "favorite": True},
        {"id": ID, "expectedRevision": REV, "addToAlbumIDs": []},
        {"id": ID, "expectedRevision": REV, "addToAlbumIDs": ["a"] * 21},
        {"id": ID, "expectedRevision": REV, "favorite": "true"},
    ],
    "photos.createAlbum": [{}, {"title": ""}, {"title": "x" * 201}],
    "photos.delete": [{}, {"ids": []}, {"ids": [ID] * 51}, {"ids": [""]}, {"ids": ID}, {"id": ID}],
    "geofences.list": [{"limit": 1}],
    "geofences.create": [
        {}, {"name": "Office"}, {"name": "Office", "latitude": 1}, {"name": "Office", "longitude": 1},
        {"latitude": 1, "longitude": 1}, {"name": "", "here": True}, {"name": "x" * 101, "here": True},
        {"name": "O", "latitude": 90.1, "longitude": 0}, {"name": "O", "latitude": 0, "longitude": -180.1},
        {"name": "O", "latitude": float("nan"), "longitude": 0}, {"name": "O", "latitude": "1", "longitude": 0},
        {"name": "O", "here": True, "latitude": 1, "longitude": 1}, {"name": "O", "here": False},
        {"name": "O", "here": True, "radiusMeters": 99}, {"name": "O", "here": True, "radiusMeters": 5001},
        {"name": "O", "here": True, "on": "sometimes"}, {"name": "O", "here": True, "note": "n" * 1001},
        {"name": "O", "here": True, "repeats": 1},
    ],
    "geofences.delete": [{}, {"id": ""}],
    "alarms.list": [{"label": "x"}],
    "alarms.create": [
        {}, {"label": "x"}, {"label": "", "timerSeconds": 60}, {"label": "x" * 61, "timerSeconds": 60},
        {"label": "x", "timerSeconds": 4}, {"label": "x", "timerSeconds": 86_401},
        {"label": "x", "time": {"hour": 24, "minute": 0}}, {"label": "x", "time": {"hour": 1, "minute": 60}},
        {"label": "x", "time": {"hour": 1}}, {"label": "x", "time": {"hour": 1, "minute": 1, "second": 1}},
        {"label": "x", "time": {"hour": 1, "minute": 1}, "date": DATE},
        {"label": "x", "time": {"hour": 1, "minute": 1}, "timerSeconds": 60},
        {"label": "x", "weekdays": ["monday"], "timerSeconds": 60}, {"label": "x", "date": DATE, "weekdays": ["monday"]},
        {"label": "x", "time": {"hour": 1, "minute": 1}, "weekdays": []},
        {"label": "x", "time": {"hour": 1, "minute": 1}, "weekdays": ["funday"]},
        {"label": "x", "time": {"hour": 1, "minute": 1}, "weekdays": ["monday", "monday"]},
        {"label": "x", "date": "tomorrow"}, {"label": "x", "date": "2026-10-20T09:00:00"},
        {"label": "x", "id": ID, "timerSeconds": 60},
    ],
    "alarms.update": [
        {"id": ID}, {"label": "x"}, {"id": ID, "date": DATE, "timerSeconds": 60},
        {"id": ID, "weekdays": ["monday"]}, {"id": ID, "timerSeconds": 4},
    ],
    "alarms.delete": [{}, {"id": ID, "label": "x"}],
    "music.nowPlaying": [{"id": "x"}],
    "music.search": [
        {}, {"query": ""}, {"query": "x" * 201}, {"query": "a", "types": []}, {"query": "a", "types": ["tracks"]},
        {"query": "a", "types": ["songs", "songs"]}, {"query": "a", "source": "cloud"}, {"query": "a", "limit": 26},
    ],
    "music.library": [{"type": "albums"}, {"limit": 51}, {"limit": 0}],
    "music.play": [
        {}, {"id": "s1"}, {"id": "s1", "type": "song"}, {"query": "a", "id": "s1", "type": "song", "source": "catalog"},
        {"query": "a", "type": "song"}, {"query": ""}, {"query": "a", "shuffle": "yes"},
        {"id": "s1", "type": "so ng", "source": "catalog"}, {"id": "s1", "type": "song", "source": "radio"},
    ],
    "music.control": [{}, {"action": "skip"}, {"action": "pause", "x": 1}],
    "music.createPlaylist": [
        {}, {"name": ""}, {"name": "x" * 101}, {"name": "m", "description": "d" * 301}, {"name": "m", "itemIDs": ["s"] * 101},
    ],
    "music.addToPlaylist": [{"playlistID": "p1"}, {"playlistID": "p1", "itemIDs": []}, {"itemIDs": ["s1"]},
                            {"playlistID": "p1", "itemIDs": ["s"] * 101}],
    "music.addToLibrary": [{"id": "s1"}, {"type": "song"}, {"id": "s1", "type": ""}],
    "focus.current": [{"x": 1}],
    "shortcuts.list": [{"x": 1}],
    "shortcuts.run": [{}, {"name": ""}, {"name": "x" * 201}, {"name": "A", "input": "i" * 4001}, {"name": "A", "input": 5},
                      {"name": "A", "args": {}}],
    "navigation.places": [{"limit": 21}, {"place": "settings.voice"}],
    "navigation.open": [{}, {"place": "Settings"}, {"place": "ok", "tap": "Save"}],
}

# A reminder with a place for iOS Reminders to watch; a made-up address.
TRIGGER = {"name": "Home", "latitude": 12.3456, "longitude": -65.4321, "radiusMeters": 150, "proximity": "arrive"}


def _register():
    from loopdy_plugin.device_tools import register

    context = SimpleNamespace(schemas={})
    context.register_tool = lambda *, name, handler, schema, **kwargs: context.schemas.update({name: schema})
    register(context)
    return context.schemas


def _request(operation: str, arguments: dict) -> dict:
    return device_tool_request(
        request_id="request-capabilities-0001", device_id="phone-1", host_id="host-1",
        authorization_epoch=1, session_id="session-1", agent_id="default", turn_id="turn-1",
        operation=operation, arguments=arguments, sent_at=100, expires_at=130,
    )


class ArgumentContractTests(unittest.TestCase):
    def test_every_new_operation_is_in_the_contract_and_has_cases(self):
        self.assertEqual(set(GOOD), set(OPERATIONS))
        self.assertEqual(set(BAD), set(OPERATIONS))
        self.assertTrue(set(OPERATIONS) <= DEVICE_TOOL_OPERATIONS)
        self.assertEqual(len(OPERATIONS), 31)
        for kind in NEW_TOOLS:
            self.assertIn(kind, DEVICE_TOOL_KINDS)

    def test_good_arguments_pass_the_whole_request_path(self):
        for operation, cases in GOOD.items():
            for arguments in cases:
                with self.subTest(operation=operation, arguments=arguments):
                    request = _request(operation, arguments)
                    self.assertEqual(request["operation"], operation)
                    self.assertEqual(request["arguments"], arguments)

    def test_bad_arguments_never_leave_the_host(self):
        for operation, cases in BAD.items():
            for arguments in cases:
                with self.subTest(operation=operation, arguments=arguments), self.assertRaises(ValueError):
                    _request(operation, arguments)

    def test_non_objects_and_unknown_keys_are_refused(self):
        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                for arguments in (None, [], "x"):
                    with self.assertRaises(ValueError):
                        _request(operation, arguments)
                with self.assertRaises(ValueError):
                    validate_operation(operation, {"sessionId": "forged"})

    def test_the_sixteen_kilobyte_argument_limit_still_holds(self):
        ids = [("i" * 500) + str(n) for n in range(50)]  # Each id fits; the list is 25 KB.
        self.assertGreater(len(json.dumps(ids)), MAX_DEVICE_TOOL_ARGUMENT_BYTES)
        with self.assertRaisesRegex(ValueError, "too large"):
            _request("photos.delete", {"ids": ids})

    def test_a_wrong_type_is_not_coerced(self):
        for operation, arguments in (
            ("contacts.list", {"limit": "5"}), ("contacts.list", {"limit": 5.0}),
            ("geofences.delete", {"id": 5}), ("photos.delete", {"ids": [5]}),
            ("alarms.create", {"label": "x", "timerSeconds": True}),
        ):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                _request(operation, arguments)

    def test_long_operations_are_the_slow_ones(self):
        self.assertEqual(DEVICE_TOOL_LONG_OPERATIONS, {"shortcuts.run", "photos.save"})


class ReminderPlaceTests(unittest.TestCase):
    def test_create_and_update_take_a_place_and_update_can_clear_it(self):
        base = {"id": ID, "expectedRevision": REV}
        _request("reminders.create", {"title": "Milk", "locationTrigger": TRIGGER})
        _request("reminders.create", {"title": "Milk", "locationTrigger": {**TRIGGER, "proximity": "leave"}})
        _request("reminders.update", {**base, "locationTrigger": TRIGGER})
        _request("reminders.update", {**base, "clearLocationTrigger": True})
        _request("reminders.update", {**base, "completed": True})  # Unchanged behavior.

    def test_a_bad_place_is_refused(self):
        base = {"id": ID, "expectedRevision": REV}
        without = lambda key: {name: value for name, value in TRIGGER.items() if name != key}  # noqa: E731
        bad_triggers = [
            {}, "home", without("name"), without("latitude"), without("longitude"), without("proximity"),
            {**TRIGGER, "name": ""}, {**TRIGGER, "name": "n" * 101}, {**TRIGGER, "latitude": 91},
            {**TRIGGER, "longitude": 181}, {**TRIGGER, "radiusMeters": 99}, {**TRIGGER, "radiusMeters": 5001},
            {**TRIGGER, "proximity": "near"}, {**TRIGGER, "extra": 1},
        ]
        for trigger in bad_triggers:
            with self.subTest(trigger=trigger), self.assertRaises(ValueError):
                _request("reminders.create", {"title": "Milk", "locationTrigger": trigger})
        for arguments in (
            {**base, "clearLocationTrigger": False}, {**base, "clearLocationTrigger": "yes"},
            {**base, "clearLocationTrigger": True, "locationTrigger": TRIGGER},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                _request("reminders.update", arguments)

    def test_only_reminders_create_and_update_take_a_place(self):
        for operation, arguments in (
            ("reminders.list", {"locationTrigger": TRIGGER}),
            ("reminders.create", {"title": "Milk", "clearLocationTrigger": True}),
            ("reminders.delete", {"id": ID, "expectedRevision": REV, "locationTrigger": TRIGGER}),
            ("calendar.create", {"title": "x", "start": DATE, "end": LATER, "timeZone": "UTC",
                                 "locationTrigger": TRIGGER}),
        ):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                _request(operation, arguments)

    def test_schemas_offer_the_place_on_reminders_only(self):
        schemas = _register()
        reminders = schemas["iphone_reminders"]["parameters"]["properties"]
        self.assertEqual(reminders["locationTrigger"]["type"], "object")
        self.assertIn("radiusMeters: 100-5000", reminders["locationTrigger"]["description"])
        self.assertEqual(reminders["clearLocationTrigger"], {"type": "boolean", "enum": [True]})
        self.assertNotIn("locationTrigger", schemas["iphone_calendar"]["parameters"]["properties"])
        self.assertNotIn("clearLocationTrigger", schemas["iphone_calendar"]["parameters"]["properties"])
        self.assertIn("locationTrigger", schemas["iphone_reminders"]["description"])


class ToolSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schemas = _register()

    def test_each_tool_is_registered_with_a_strict_schema(self):
        for kind in NEW_TOOLS:
            with self.subTest(kind=kind):
                schema = self.schemas[f"iphone_{kind}"]
                parameters = schema["parameters"]
                self.assertEqual(schema["name"], f"iphone_{kind}")
                self.assertEqual(parameters["type"], "object")
                self.assertIs(parameters["additionalProperties"], False)
                self.assertEqual(parameters["required"], ["operation"])
                operations = [name.partition(".")[2] for name in OPERATIONS if name.startswith(f"{kind}.")]
                self.assertEqual(parameters["properties"]["operation"]["enum"], operations)
                self.assertTrue({"deviceId", "hostId", "sessionId", "targetDeviceId"}.isdisjoint(parameters["properties"]))
                Draft202012Validator.check_schema(parameters)
                description = schema["description"]
                self.assertIn("Device access", description)
                self.assertIn("app open", description)
                self.assertLess(len(description), 450)

    def test_descriptions_carry_the_rules_agents_must_follow(self):
        expect = {
            "contacts": ("expectedRevision", "list first", "replaces"),
            "photos": ("vision tool", "50 MB", "cancelled", "outcome_unknown"),
            "geofences": ("Only when asked", "iphone_reminders"),
            "alarms": ("bighelp's own", "Clock app"),
            "music": ("unchanged", "subscription_required"),
            "focus": ("can't be changed",),
            "shortcuts": ("only names you may run", "don't rerun"),
        }
        for kind, phrases in expect.items():
            for phrase in phrases:
                with self.subTest(kind=kind, phrase=phrase):
                    self.assertIn(phrase, self.schemas[f"iphone_{kind}"]["description"])

    def test_all_iphone_schemas_stay_within_eleven_kilobytes(self):
        # Sent with every request to the serving agent, whatever the Device access switches say.
        total = sum(len(json.dumps(schema, separators=(",", ":"))) for schema in self.schemas.values())
        self.assertLessEqual(total, 11_000, total)

    def test_bounds_are_the_contracts(self):
        def prop(kind: str, name: str) -> dict:
            return self.schemas[f"iphone_{kind}"]["parameters"]["properties"][name]

        self.assertEqual(prop("contacts", "limit"), {"type": "integer", "maximum": 50})
        self.assertEqual(prop("contacts", "phones")["maxItems"], 10)
        self.assertEqual(prop("contacts", "urls")["maxItems"], 5)
        self.assertEqual(prop("photos", "maxPixels"), {"type": "integer", "minimum": 256, "maximum": 1024})
        self.assertEqual(prop("photos", "ids")["maxItems"], 50)
        self.assertEqual(prop("photos", "addToAlbumIDs")["maxItems"], 20)
        self.assertEqual(prop("photos", "limit")["maximum"], 100)
        self.assertEqual(prop("geofences", "radiusMeters"), {"type": "integer", "minimum": 100, "maximum": 5000})
        self.assertEqual(prop("geofences", "latitude")["maximum"], 90)
        # Long text limits stay in the host's check, not in the schema the model reads on every request.
        self.assertNotIn("maxLength", prop("shortcuts", "input"))
        self.assertEqual(prop("contacts", "phones")["description"], "[{label, value}]")
        self.assertEqual(prop("alarms", "timerSeconds"), {"type": "integer", "minimum": 5, "maximum": 86_400})
        self.assertEqual(prop("alarms", "label")["maxLength"], 60)
        self.assertEqual(prop("alarms", "weekdays")["maxItems"], 7)
        self.assertEqual(prop("music", "limit")["maximum"], 50)
        self.assertEqual(prop("music", "itemIDs")["maxItems"], 100)
        self.assertEqual(prop("music", "action")["enum"], ["pause", "resume", "next", "previous", "stop"])
        self.assertEqual(self.schemas["iphone_focus"]["parameters"]["properties"],
                         {"operation": {"type": "string", "enum": ["current"]}})

    def test_the_schema_accepts_what_the_contract_accepts(self):
        for operation, cases in GOOD.items():
            kind, _, name = operation.partition(".")
            validator = Draft202012Validator(self.schemas[f"iphone_{kind}"]["parameters"], format_checker=Draft202012Validator.FORMAT_CHECKER)
            for arguments in cases:
                with self.subTest(operation=operation, arguments=arguments):
                    validator.validate({"operation": name, **arguments})

    def test_the_schema_refuses_what_a_single_field_makes_wrong(self):
        for operation, arguments in (
            ("contacts.list", {"limit": 51}), ("photos.get", {"id": ID, "maxPixels": 2000}),
            ("alarms.create", {"label": "x", "timerSeconds": 4}), ("geofences.create", {"name": "O", "here": True, "radiusMeters": 50}),
            ("music.control", {"action": "skip"}), ("photos.delete", {"ids": ["a"] * 51}),
            ("alarms.create", {"label": "x", "sound": "bell", "timerSeconds": 60}),
        ):
            kind, _, name = operation.partition(".")
            with self.subTest(operation=operation, arguments=arguments):
                errors = list(Draft202012Validator(self.schemas[f"iphone_{kind}"]["parameters"]).iter_errors({"operation": name, **arguments}))
                self.assertTrue(errors)

    def test_manifest_guide_and_skill_name_every_tool(self):
        from loopdy_plugin.agent_guide import CHAT_BRIEF

        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        skill = (ROOT / "skills" / "bighelp" / "SKILL.md").read_text(encoding="utf-8")
        for kind in NEW_TOOLS:
            with self.subTest(kind=kind):
                self.assertIn(f"  - iphone_{kind}\n", manifest)
                self.assertIn(f"iphone_{kind}", CHAT_BRIEF)
                self.assertIn(f"iphone_{kind}", skill)
        self.assertLess(len(CHAT_BRIEF), 3_500)  # Hermes caps every plugin's sections together at 8,000.


class ChannelKindTests(unittest.TestCase):
    def test_features_and_enabled_kinds(self):
        self.assertEqual(KIND_CAPABILITIES, {kind: f"native-device-{kind}-v1" for kind in (*NEW_TOOLS, "navigation")})
        fields = {
            "channelId": str(uuid.uuid4()), "deviceId": str(uuid.uuid4()), "hostId": "local-host",
            "authorizationEpoch": 1, "agentId": "default", "sessionId": "stored-session",
            "enabled": list(DEVICE_TOOL_KINDS),
        }
        self.assertEqual(_Connect(**fields).enabled, list(DEVICE_TOOL_KINDS))
        self.assertEqual(_Connect(**{**fields, "enabled": ["music", "focus"]}).enabled, ["music", "focus"])
        for enabled in (["music", "music"], ["radio"], list(DEVICE_TOOL_KINDS) + ["health"]):
            with self.subTest(enabled=enabled), self.assertRaises(ValueError):
                _Connect(**{**fields, "enabled": enabled})

    def test_a_tool_call_maps_to_kind_dot_operation(self):
        for operation in OPERATIONS:
            kind, _, name = operation.partition(".")
            with self.subTest(operation=operation):
                self.assertEqual(
                    _operation_for_tool(f"iphone_{kind}", {"operation": name, "sessionId": "forged", "toolCallId": "x"}),
                    (operation, {}))
        self.assertEqual(
            _operation_for_tool("iphone_photos", {"operation": "createAlbum", "title": "Trip"}),
            ("photos.createAlbum", {"title": "Trip"}))

    def test_a_tool_call_with_another_tools_operation_is_refused(self):
        for tool, args in (
            ("iphone_contacts", {"operation": "albums"}), ("iphone_photos", {"operation": "current"}),
            ("iphone_focus", {"operation": "list"}), ("iphone_shortcuts", {"operation": "create"}),
            ("iphone_music", {}), ("iphone_music", {"operation": ["play"]}), ("iphone_alarms", "list"),
            ("iphone_geofences", {"operation": "update"}), ("iphone_photos", {"operation": "photos.list"}),
        ):
            with self.subTest(tool=tool, args=args), self.assertRaises(NativeDeviceToolError):
                _operation_for_tool(tool, args)


class ChannelCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.now = 1_700_000_000
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.pictures = Path(self.temporary.name) / "phone-photos"
        self.owner = SimpleNamespace(provider="test", user_id="user-1", serving_profile_id="default", runtime_id="runtime-1")
        self.hub = NativeDeviceToolHub(clock=lambda: self.now, profile_session_validator=lambda profile, session: True,
                                       photos_directory=lambda: self.pictures)

    def fields(self, enabled: list[str]) -> dict:
        return {
            "channelId": str(uuid.uuid4()), "deviceId": str(uuid.uuid4()), "hostId": "local-host",
            "authorizationEpoch": 1, "agentId": "default", "sessionId": "stored-session", "enabled": enabled,
        }

    def call(self, operation: str, arguments: dict, call_id: str = "call-1"):
        return asyncio.create_task(self.hub.execute(
            profile="default", session_id="stored-session", turn_id="turn-1", tool_call_id=call_id,
            operation=operation, arguments=arguments,
        ))

    def answer(self, fields: dict, request: dict, payload: dict, *, status: str = "completed") -> dict:
        result = device_tool_result(request=request, status=status, payload=payload, sent_at=self.now)
        self.assertEqual(self.hub.accept_result(self.owner, fields, result), {"accepted": True})
        return result


class ChannelGatingTests(ChannelCase):
    async def test_each_kind_needs_its_own_switch(self):
        others = [kind for kind in DEVICE_TOOL_KINDS if kind not in NEW_TOOLS]
        for kind in NEW_TOOLS:
            with self.subTest(kind=kind):
                fields = self.fields([other for other in NEW_TOOLS if other != kind] + others)
                self.hub.connect(self.owner, fields)
                operation, arguments = next((name, GOOD[name][0]) for name in OPERATIONS if name.startswith(f"{kind}."))
                denied = await self.call(operation, arguments)
                self.assertEqual(denied["code"], "authorization_required")
                self.assertEqual(self.hub.poll(self.owner, fields, after=0)["requests"], [])
                self.hub.close(self.owner, fields)

    async def test_a_round_trip_for_each_kind(self):
        for operation, cases in GOOD.items():
            if operation in ("photos.save", "photos.get"):
                continue
            with self.subTest(operation=operation):
                fields = self.fields([operation.partition(".")[0]])
                self.hub.connect(self.owner, fields)
                task = self.call(operation, cases[0])
                await asyncio.sleep(0)
                [row] = self.hub.poll(self.owner, fields, after=0)["requests"]
                self.assertEqual(row["request"]["operation"], operation)
                self.assertEqual(row["request"]["arguments"], cases[0])
                self.answer(fields, row["request"], {"ok": True})
                self.assertEqual((await task)["payload"], {"ok": True})
                self.hub.close(self.owner, fields)

    async def test_an_answer_for_a_kind_that_was_turned_off_is_refused(self):
        fields = self.fields(["music"])
        self.hub.connect(self.owner, fields)
        task = self.call("music.nowPlaying", {})
        await asyncio.sleep(0)
        [row] = self.hub.poll(self.owner, fields, after=0)["requests"]
        self.hub.channels[fields["channelId"]].enabled = frozenset({"focus"})
        with self.assertRaises(NativeDeviceToolError) as caught:
            self.answer(fields, row["request"], {})
        self.assertEqual(caught.exception.code, "authorization_required")
        task.cancel()

    async def test_bad_arguments_never_reach_the_phone(self):
        fields = self.fields(["alarms"])
        self.hub.connect(self.owner, fields)
        result = await self.call("alarms.create", {"label": "x", "timerSeconds": 4})
        self.assertEqual(result["code"], "invalid_arguments")
        self.assertEqual(result["payload"], {"note": "timerSeconds is invalid"})  # The schema leaves the bound out.
        secret = await self.call("alarms.create", {"label": "x", "secret-value": 1}, "call-2")
        self.assertEqual(secret["payload"], {"note": "arguments contain an unknown key"})
        self.assertNotIn("secret-value", json.dumps(secret))
        self.assertEqual(self.hub.poll(self.owner, fields, after=0)["requests"], [])


class HostPictureFenceTests(ChannelCase):
    """contacts.create and calendar.create from a chat holding a picture stop at the host (docs/IMAGE_DRAFTS.md)."""

    def setUp(self) -> None:
        super().setUp()
        self.picture_chats: set[str] = {"stored-session"}
        self.asked: list[tuple[str, list[str]]] = []

        def holds(profile: str, sessions: list[str]) -> bool:
            self.asked.append((profile, list(sessions)))
            return bool(self.picture_chats & set(sessions))

        self.hub = NativeDeviceToolHub(clock=lambda: self.now, profile_session_validator=lambda profile, session: True,
                                       photos_directory=lambda: self.pictures, picture_chat=holds)
        self.channel_fields = self.fields(["contacts", "calendar"])
        self.hub.connect(self.owner, self.channel_fields)
        self.after = 0

    def polled(self) -> list[dict]:
        polled = self.hub.poll(self.owner, self.channel_fields, after=self.after)
        self.after = polled["next"]
        return [row["request"] for row in polled["requests"]]

    async def test_a_create_in_a_picture_chat_never_reaches_the_phone(self):
        calendar = {"title": "Night Market", "start": DATE, "end": LATER, "timeZone": "UTC"}
        for number, (operation, arguments) in enumerate((("contacts.create", GOOD["contacts.create"][0]),
                                                        ("calendar.create", calendar))):
            with self.subTest(operation=operation):
                result = await self.call(operation, arguments, f"call-{number}")
                self.assertEqual(("failed", "review_required"), (result["status"], result["code"]))
                self.assertIn("bighelp_draft_contact", result["payload"]["note"])
                self.assertEqual([], self.polled())
        self.assertEqual("default", self.asked[0][0])
        self.assertIn("stored-session", self.asked[0][1])

    async def test_other_operations_in_a_picture_chat_still_reach_the_phone(self):
        for number, operation in enumerate(("contacts.list", "contacts.update", "contacts.delete")):
            with self.subTest(operation=operation):
                task = self.call(operation, GOOD[operation][0], f"call-{number}")
                await asyncio.sleep(0)
                [request] = self.polled()
                self.assertEqual(operation, request["operation"])
                self.answer(self.channel_fields, request, {"ok": True})
                self.assertEqual("completed", (await task)["status"])

    async def test_a_create_in_a_plain_chat_still_reaches_the_phone(self):
        self.picture_chats.clear()
        task = self.call("contacts.create", GOOD["contacts.create"][0])
        await asyncio.sleep(0)
        [request] = self.polled()
        self.assertEqual("contacts.create", request["operation"])
        self.answer(self.channel_fields, request, {"ok": True})
        self.assertEqual("completed", (await task)["status"])

    async def test_an_unreadable_history_lets_the_phone_decide(self):
        import sqlite3

        def broken(_profile, _sessions):
            raise sqlite3.DatabaseError("file is not a database")

        self.hub.picture_chat = broken
        with self.assertLogs("hermes.plugins.bighelp", level="WARNING") as logs:
            task = self.call("contacts.create", GOOD["contacts.create"][0])
            await asyncio.sleep(0)
            [request] = self.polled()
        self.assertEqual("contacts.create", request["operation"])
        self.assertIn("DatabaseError", logs.output[0])
        self.assertNotIn("not a database", logs.output[0])
        self.answer(self.channel_fields, request, {"ok": True})
        self.assertEqual("completed", (await task)["status"])


class TimeoutTests(ChannelCase):
    async def test_only_shortcuts_run_and_photos_save_wait_sixty_seconds(self):
        image = Path(self.temporary.name) / "ok.png"
        image.write_bytes(b"\x89PNG\r\n" + b"x" * 100)
        arguments = {"shortcuts.run": {"name": "Log Water"}, "photos.save": {"path": str(image)}}
        for operation, cases in GOOD.items():
            if operation in ("photos.get",):
                continue
            with self.subTest(operation=operation):
                fields = self.fields([operation.partition(".")[0]])
                self.hub.connect(self.owner, fields)
                with patch.object(device_photos, "check_save_path", side_effect=lambda raw: raw):
                    task = self.call(operation, arguments.get(operation, cases[0]))
                    await asyncio.sleep(0)
                [row] = self.hub.poll(self.owner, fields, after=0)["requests"]
                wait = row["request"]["expiresAt"] - row["request"]["sentAt"]
                self.assertEqual(wait, 60 if operation in ("shortcuts.run", "photos.save") else 30)
                task.cancel()
                self.hub.close(self.owner, fields)

    async def test_a_long_call_keeps_its_channel_while_the_phone_is_away(self):
        fields = self.fields(["shortcuts"])
        self.hub.connect(self.owner, fields)
        task = self.call("shortcuts.run", {"name": "Log Water"})
        await asyncio.sleep(0)
        [row] = self.hub.poll(self.owner, fields, after=0)["requests"]
        self.now += 45  # No poll for longer than a lease: the phone is in the Shortcuts app.
        self.answer(fields, row["request"], {"id": "r1", "status": "completed", "output": "done", "truncated": False})
        self.assertEqual((await task)["payload"]["output"], "done")

    async def test_a_short_call_still_loses_its_channel_after_the_lease(self):
        fields = self.fields(["music"])
        self.hub.connect(self.owner, fields)
        task = self.call("music.nowPlaying", {})
        await asyncio.sleep(0)
        [row] = self.hub.poll(self.owner, fields, after=0)["requests"]
        self.now += 31
        with self.assertRaises(NativeDeviceToolError):
            self.answer(fields, row["request"], {})
        self.assertEqual((await task)["code"], "phone_unavailable")

    async def test_the_wait_is_as_long_as_the_request_allows(self):
        waits = []

        async def fake_wait_for(awaitable, timeout):
            waits.append(timeout)
            raise asyncio.TimeoutError

        for operation, arguments, seconds, code in (
            ("shortcuts.run", {"name": "Log Water"}, 70, "outcome_unknown"),
            ("music.nowPlaying", {}, 30, "phone_unavailable"),
        ):
            with self.subTest(operation=operation):
                fields = self.fields([operation.partition(".")[0]])
                self.hub.connect(self.owner, fields)
                with patch("loopdy_plugin.native_device_tools.asyncio.wait_for", fake_wait_for):
                    result = await self.call(operation, arguments)
                self.assertEqual((result["status"], result["code"]), ("failed", code))
                self.assertEqual(waits[-1], seconds)
                self.hub.close(self.owner, fields)


class OutcomeUnknownTests(ChannelCase):
    """The phone has 60 seconds; the host waits 10 more, so a late answer still counts. A long operation the
    phone never answers may have happened (a shortcut can do anything): the agent must not run it again."""

    async def send(self, operation: str, arguments: dict, fields: dict):
        self.hub.connect(self.owner, fields)
        with patch.object(device_photos, "check_save_path", side_effect=lambda raw: raw):
            task = self.call(operation, arguments)
            await asyncio.sleep(0)
        [row] = self.hub.poll(self.owner, fields, after=0)["requests"]
        request = row["request"]
        self.assertEqual(request["expiresAt"] - request["sentAt"], 60)  # The phone's own deadline stays 60.
        return task, request

    async def test_the_host_gives_up_ten_seconds_after_the_phone_would(self):
        waits = []

        async def fake_wait_for(awaitable, timeout):
            waits.append(timeout)
            raise asyncio.TimeoutError

        fields = self.fields(["shortcuts"])
        self.hub.connect(self.owner, fields)
        with patch("loopdy_plugin.native_device_tools.asyncio.wait_for", fake_wait_for):
            result = await self.call("shortcuts.run", {"name": "Log Water"})
        self.assertEqual((waits, result["status"], result["code"]), ([70], "failed", "outcome_unknown"))
        self.assertIn("Do not run it again", result["payload"]["note"])

    async def test_an_answer_up_to_ten_seconds_late_is_still_taken(self):
        fields = self.fields(["shortcuts"])
        task, request = await self.send("shortcuts.run", {"name": "Log Water"}, fields)
        self.now += 65
        self.answer(fields, request, {"id": "r1", "status": "completed", "output": "done", "truncated": False})
        self.assertEqual((await task)["payload"]["output"], "done")

    async def test_an_answer_after_the_host_deadline_is_refused_and_the_agent_hears_outcome_unknown(self):
        fields = self.fields(["shortcuts"])
        task, request = await self.send("shortcuts.run", {"name": "Log Water"}, fields)
        self.now += 71
        late = device_tool_result(request=request, status="completed", payload={"id": "r1"}, sent_at=self.now)
        with self.assertRaises(NativeDeviceToolError):
            self.hub.accept_result(self.owner, fields, late)
        self.assertEqual((await task)["code"], "outcome_unknown")

    async def test_a_channel_that_ends_during_a_long_call_leaves_it_unknown(self):
        fields = self.fields(["shortcuts"])
        task, _request_ = await self.send("shortcuts.run", {"name": "Log Water"}, fields)
        self.hub.close(self.owner, fields)
        self.assertEqual((await task)["code"], "outcome_unknown")

    async def test_other_calls_still_say_phone_unavailable(self):
        fields = self.fields(["contacts"])
        self.hub.connect(self.owner, fields)
        task = self.call("contacts.create", {"givenName": "Sam"})
        await asyncio.sleep(0)
        self.hub.close(self.owner, fields)
        self.assertEqual((await task)["code"], "phone_unavailable")


# Results ---------------------------------------------------------------------------------------

def _jpeg(width: int = 768, height: int = 512, size: int = 2_000) -> bytes:
    """A JPEG's headers around filler: whole enough for the host's checks, never decoded."""
    frame = b"\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x03\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    scan = b"\xff\xda\x00\x0c\x03\x01\x00\x02\x11\x03\x11\x00\x3f\x00"
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    body = b"\xff\xd8" + app0 + frame + scan
    return body + bytes((n * 7) % 251 for n in range(max(0, size - len(body) - 2))) + b"\xff\xd9"


def _photo_payload(raw: bytes, **extra) -> dict:
    return {
        "id": "photo-1", "revision": "rev-1", "mediaType": "image", "created": DATE, "width": 4032, "height": 3024,
        "favorite": False, "access": "full",
        "image": {"format": "jpeg", "width": 768, "height": 512, "data": base64.b64encode(raw).decode()}, **extra,
    }


class PhotoResultTests(ChannelCase):
    def setUp(self) -> None:
        super().setUp()
        self.channel_fields = self.fields(["photos"])
        self.hub.connect(self.owner, self.channel_fields)
        self.after = 0

    async def get(self, payload: dict, *, call_id: str = "call-1") -> dict:
        task = self.call("photos.get", {"id": "photo-1"}, call_id)
        await asyncio.sleep(0)
        polled = self.hub.poll(self.owner, self.channel_fields, after=self.after)
        self.after = polled["next"]
        self.answer(self.channel_fields, polled["requests"][-1]["request"], payload)
        return await task

    async def test_the_agent_gets_a_file_and_never_the_bytes(self):
        raw = _jpeg()
        with self.assertNoLogs("hermes.plugins.bighelp", level="DEBUG"):
            result = await self.get(_photo_payload(raw))
        payload = result["payload"]
        picture = payload["image"]
        self.assertEqual(set(picture), {"format", "width", "height", "path"})
        self.assertEqual((picture["format"], picture["width"], picture["height"]), ("jpeg", 768, 512))
        self.assertEqual(payload["id"], "photo-1")  # The item's own fields stay.
        self.assertEqual((payload["width"], payload["height"]), (4032, 3024))
        path = Path(picture["path"])
        self.assertEqual(path.parent, self.pictures)
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.pictures.stat().st_mode), 0o700)
        encoded = base64.b64encode(raw).decode()
        everything = json.dumps(result)
        self.assertNotIn(encoded, everything)
        self.assertNotIn(encoded[:60], everything)
        self.assertNotIn("data", picture)

    async def test_the_chat_that_read_a_picture_is_noted_for_photo_drafts(self):
        raw = _jpeg()
        result = await self.get(_photo_payload(raw))
        path = result["payload"]["image"]["path"]
        self.assertEqual([path], [found for _when, found in device_photos.fetched_pictures(self.pictures,
                                                                                            ["stored-session"])])
        self.assertEqual([], device_photos.fetched_pictures(self.pictures, ["another-session"]))
        record = self.pictures.parent / "phone-photos-fetched.sqlite3"
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)

    async def test_the_same_picture_keeps_one_file(self):
        raw = _jpeg()
        first = await self.get(_photo_payload(raw))
        second = await self.get(_photo_payload(raw), call_id="call-2")
        self.assertEqual(first["payload"]["image"]["path"], second["payload"]["image"]["path"])
        self.assertEqual(len(list(self.pictures.iterdir())), 1)

    async def test_pictures_that_are_not_whole_jpegs_within_limits_are_refused(self):
        good = _jpeg()
        for name, raw in (
            ("png", b"\x89PNG\r\n\x1a\n" + b"x" * 200),
            ("no end", good[:-2]),
            ("no frame header", b"\xff\xd8\xff\xe0\x00\x04ab\xff\xd9"),
            ("too many pixels", _jpeg(width=4000, height=3000)),
            ("zero pixels", _jpeg(width=0, height=0)),
        ):
            with self.subTest(name=name), self.assertLogs("hermes.plugins.bighelp", level="WARNING") as logs:
                result = await self.get(_photo_payload(raw), call_id=name)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["code"], "photo_unreadable")
                self.assertEqual(result["payload"], {})
                self.assertEqual([record.getMessage() for record in logs.records],
                                 ["bighelp phone photo not stored: photo_unreadable"])
        self.assertFalse(self.pictures.exists() and list(self.pictures.iterdir()))

    async def test_a_picture_over_ninety_kilobytes_never_gets_this_far(self):
        # The contract bounds it first: the phone steps quality down to fit.
        request = _request("photos.get", {"id": ID})
        with self.assertRaises(ValueError):
            device_tool_result(request=request, status="completed",
                               payload=_photo_payload(_jpeg(size=device_photos.MAX_PHOTO_JPEG_BYTES + 3)), sent_at=101)
        stored = device_photos.store_picture(
            {"data": base64.b64encode(_jpeg(size=device_photos.MAX_PHOTO_JPEG_BYTES)).decode()}, self.pictures)
        self.assertTrue(Path(stored["path"]).is_file())
        with self.assertRaises(device_photos.PhotoRefusal):
            device_photos.store_picture(
                {"data": base64.b64encode(_jpeg(size=device_photos.MAX_PHOTO_JPEG_BYTES + 1)).decode()}, self.pictures)

    async def test_text_that_is_not_base64_is_refused_by_the_channel(self):
        payload = _photo_payload(_jpeg())
        payload["image"]["data"] = "not base64 !!"
        task = self.call("photos.get", {"id": "photo-1"})
        await asyncio.sleep(0)
        [row] = self.hub.poll(self.owner, self.channel_fields, after=0)["requests"]
        with self.assertRaises(ValueError):
            self.answer(self.channel_fields, row["request"], payload)
        task.cancel()

    async def test_a_symlinked_folder_is_not_written_through(self):
        target = Path(self.temporary.name) / "elsewhere"
        target.mkdir()
        self.pictures.symlink_to(target)
        result = await self.get(_photo_payload(_jpeg()))
        self.assertEqual(result["code"], "photo_unavailable")
        self.assertEqual(list(target.iterdir()), [])

    async def test_a_failed_read_passes_through_unchanged(self):
        task = self.call("photos.get", {"id": "photo-1"})
        await asyncio.sleep(0)
        [row] = self.hub.poll(self.owner, self.channel_fields, after=0)["requests"]
        result = device_tool_result(request=row["request"], status="failed", payload={}, sent_at=self.now, code="not_found")
        self.hub.accept_result(self.owner, self.channel_fields, result)
        self.assertEqual((await task)["code"], "not_found")

    async def test_a_failed_read_never_hands_the_agent_picture_data(self):
        task = self.call("photos.get", {"id": "photo-1"})
        await asyncio.sleep(0)
        [row] = self.hub.poll(self.owner, self.channel_fields, after=0)["requests"]
        data = base64.b64encode(_jpeg()).decode()
        result = device_tool_result(
            request=row["request"], status="failed", code="cancelled", sent_at=self.now,
            payload={"image": {"format": "jpeg", "width": 1, "height": 1, "data": data}, "id": "photo-1"})
        self.hub.accept_result(self.owner, self.channel_fields, result)
        answer = await task
        self.assertEqual(answer["code"], "cancelled")
        self.assertNotIn(data, json.dumps(answer))
        self.assertNotIn("data", answer["payload"]["image"])
        self.assertEqual(answer["payload"]["id"], "photo-1")
        self.assertFalse(self.pictures.exists())

    async def test_the_folder_is_made_private_from_the_start(self):
        old = os.umask(0)
        try:
            await self.get(_photo_payload(_jpeg()))
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(self.pictures.stat().st_mode), 0o700)

    async def test_other_operations_pass_through_unchanged(self):
        task = self.call("photos.list", {})
        await asyncio.sleep(0)
        [row] = self.hub.poll(self.owner, self.channel_fields, after=0)["requests"]
        self.answer(self.channel_fields, row["request"], {"photos": [{"id": "p1", "image": {"data": "abc"}}], "truncated": False})
        self.assertEqual((await task)["payload"]["photos"][0]["image"], {"data": "abc"})


class PictureFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / "bighelp" / "phone-photos"

    def store(self, seed: int, now: float) -> Path:
        raw = _jpeg(size=1_000 + seed)
        data = {"data": base64.b64encode(raw).decode()}
        return Path(device_photos.store_picture(data, self.folder, now=now)["path"])

    def test_keeps_the_newest_fifty(self):
        now = 1_800_000_000.0
        paths = [self.store(n, now + n) for n in range(60)]
        kept = sorted(path.name for path in self.folder.iterdir())
        self.assertEqual(len(kept), 50)
        self.assertEqual(kept, sorted(path.name for path in paths[10:]))
        self.assertTrue(all(path.exists() for path in paths[10:]))

    def test_drops_pictures_older_than_seven_days_but_never_the_new_one(self):
        now = 1_800_000_000.0
        old = self.store(1, now - 8 * 86_400)
        recent = self.store(2, now - 6 * 86_400)
        self.store(3, now - 6 * 86_400 + 1)
        newest = self.store(4, now)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(newest.exists())

    def test_only_pictures_are_pruned(self):
        now = 1_800_000_000.0
        self.store(1, now)
        stranger = self.folder / "notes.txt"
        stranger.write_text("keep")
        os.utime(stranger, (now - 30 * 86_400, now - 30 * 86_400))
        partial = self.folder / ".abc.jpg.1.2.tmp"
        partial.write_bytes(b"x")
        os.utime(partial, (now - 3 * 86_400, now - 3 * 86_400))
        self.store(2, now + 1)
        self.assertTrue(stranger.exists())
        self.assertFalse(partial.exists())

    def test_a_symlink_in_the_folder_is_never_followed_or_removed_as_a_picture(self):
        now = 1_800_000_000.0
        self.store(1, now)
        outside = Path(self.temporary.name) / "outside.jpg"
        outside.write_bytes(b"mine")
        link = self.folder / ("a" * 40 + ".jpg")
        link.symlink_to(outside)
        os.utime(link, (now - 30 * 86_400, now - 30 * 86_400), follow_symlinks=False)
        self.store(2, now + 1)
        self.assertEqual(outside.read_bytes(), b"mine")

    def test_jpeg_headers_give_the_size(self):
        self.assertEqual(device_photos._jpeg_size(_jpeg(300, 200)), (300, 200))
        for raw in (b"", b"\xff\xd8\xff", b"GIF89a" + b"x" * 30, b"\xff\xd8\xff\xff\xd9"):
            with self.subTest(raw=raw), self.assertRaises(device_photos.PhotoRefusal):
                device_photos._jpeg_size(raw)


class ResultLimitTests(ChannelCase):
    def test_a_photo_result_may_be_far_larger_than_an_ordinary_one(self):
        request = _request("photos.get", {"id": ID})
        raw = _jpeg(size=device_photos.MAX_PHOTO_JPEG_BYTES)
        result = device_tool_result(request=request, status="completed", payload=_photo_payload(raw), sent_at=101)
        self.assertGreater(len(json.dumps(result)), 120_000)
        self.assertEqual(parse_device_tool_result(result)["payload"]["image"]["data"], base64.b64encode(raw).decode())

    def test_only_a_photo_get_result_may_carry_long_text(self):
        long_text = "A" * 9_000
        for operation in ("photos.list", "contacts.list"):
            request = _request(operation, {})
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                device_tool_result(request=request, status="completed", payload={"image": {"data": long_text}}, sent_at=101)
        with self.assertRaises(ValueError):
            device_tool_result(request=_request("photos.get", {"id": ID}), status="completed",
                               payload={"note": long_text}, sent_at=101)

    def test_a_picture_over_the_limit_or_not_base64_is_refused_by_the_contract(self):
        request = _request("photos.get", {"id": ID})
        too_long = "A" * (device_photos.MAX_PHOTO_JPEG_BYTES * 2)
        for data in (too_long, "not base64!", "AA=A"):
            with self.subTest(data=data[:12]), self.assertRaises(ValueError):
                device_tool_result(request=request, status="completed", payload={"image": {"data": data}}, sent_at=101)

    def test_a_result_is_bounded_at_128_kilobytes(self):
        request = _request("contacts.list", {})
        rows = [{"id": f"c{n}", "note": "n" * 4_000} for n in range(40)]  # 160 KB.
        with self.assertRaisesRegex(ValueError, "too large"):
            device_tool_result(request=request, status="completed", payload={"contacts": rows}, sent_at=101)
        fits = [{"id": f"c{n}", "familyName": "f" * 1_000} for n in range(50)]
        self.assertEqual(len(device_tool_result(request=request, status="completed",
                                                payload={"contacts": fits}, sent_at=101)["payload"]["contacts"]), 50)

    def test_contacts_nest_lists_inside_lists(self):
        request = _request("contacts.list", {})
        payload = {"access": "full", "truncated": False, "contacts": [{
            "id": "c1", "revision": "r1", "givenName": "Sam",
            "phones": [{"label": "mobile", "value": "+1 555 0100"}],
            "addresses": [{"label": "home", "street": "100 Example Street"}],
        }]}
        self.assertEqual(device_tool_result(request=request, status="completed", payload=payload, sent_at=101)["payload"], payload)
        deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}}
        with self.assertRaisesRegex(ValueError, "too deep"):
            device_tool_result(request=request, status="completed", payload=deep, sent_at=101)


# photos.save -----------------------------------------------------------------------------------

class SavePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(os.path.realpath(self.temporary.name))

    def file(self, name: str, size: int = 100) -> str:
        path = self.root / name
        path.write_bytes(b"x" * size)
        return str(path)

    def refusal(self, raw) -> str:
        with self.assertRaises(device_photos.PhotoRefusal) as caught:
            device_photos.check_save_path(raw)
        return caught.exception.code

    def test_a_picture_or_video_passes_and_comes_back_as_the_resolved_path(self):
        for name in ("a.png", "b.JPG", "d.mov", "e.mp4", "f.webp"):
            with self.subTest(name=name):
                path = self.file(name)
                self.assertEqual(device_photos.check_save_path(path), path)

    def test_kinds_the_gateway_does_not_deliver_are_unsupported_media_not_a_bad_path(self):
        # Hermes' delivery list has no .heic or .dng (some versions lack .m4v and .tif too).
        from gateway.platforms import base

        for name in ("c.heic", "c.heif", "c.m4v", "c.tif", "c.dng"):
            with self.subTest(name=name):
                path = self.file(name)
                if Path(name).suffix in base.MEDIA_DELIVERY_EXTS:
                    self.assertEqual(device_photos.check_save_path(path), path)
                else:
                    self.assertEqual(self.refusal(path), "unsupported_media")

    def test_the_gateways_own_list_decides_not_the_host_pythons(self):
        path = self.file("trip.png")
        with patch("gateway.platforms.base.MEDIA_DELIVERY_EXTS", (".mp4",)):
            self.assertEqual(self.refusal(path), "unsupported_media")
        with patch("gateway.platforms.base.MEDIA_DELIVERY_EXTS", (".png",)):
            self.assertEqual(device_photos.check_save_path(path), path)

    def test_a_link_to_another_kind_of_file_is_unsupported_media(self):
        link = self.root / "looks-like.png"
        link.symlink_to(self.file("notes.txt"))
        self.assertEqual(self.refusal(str(link)), "unsupported_media")

    def test_without_the_attachment_routes_save_says_files_unavailable(self):
        path = self.file("trip.png")
        with patch.dict("sys.modules", {"hermes_cli.profiles": None}):
            self.assertEqual(self.refusal(path), "files_unavailable")

    def test_a_symlink_is_resolved_before_the_phone_hears_of_it(self):
        target = self.file("real.png")
        link = self.root / "link.png"
        link.symlink_to(target)
        self.assertEqual(device_photos.check_save_path(str(link)), target)

    def test_other_files_are_unsupported_media(self):
        for name in ("a.pdf", "b.txt", "c.zip", "noextension"):
            with self.subTest(name=name):
                self.assertEqual(self.refusal(self.file(name)), "unsupported_media")

    def test_fifty_megabytes_is_the_limit(self):
        limit = device_photos.MAX_SAVE_BYTES
        exact = self.root / "exact.png"
        with exact.open("wb") as handle:
            handle.truncate(limit)
        self.assertEqual(device_photos.check_save_path(str(exact)), str(exact))
        over = self.root / "over.mov"
        with over.open("wb") as handle:
            handle.truncate(limit + 1)
        self.assertEqual(self.refusal(str(over)), "file_too_large")

    def test_paths_the_resolve_route_would_not_serve_are_refused(self):
        self.assertEqual(self.refusal(str(self.root / "missing.png")), "invalid_arguments")
        folder = self.root / "folder.png"
        folder.mkdir()
        self.assertEqual(self.refusal(str(folder)), "invalid_arguments")
        self.assertEqual(self.refusal("relative/path.png"), "invalid_arguments")
        self.assertEqual(self.refusal(self.file("empty.png", 0)), "invalid_arguments")
        for raw in (None, 5, "", "x" * 5000, "/tmp/a\n.png", "/tmp/a\x00.png"):
            with self.subTest(raw=raw):
                self.assertEqual(self.refusal(raw), "invalid_arguments")

    def test_the_gateways_delivery_policy_decides(self):
        path = self.file("denied.png")
        with patch("gateway.platforms.base.BasePlatformAdapter.validate_media_delivery_path", return_value=None):
            self.assertEqual(self.refusal(path), "invalid_arguments")

    def test_a_path_that_does_not_read_back_as_one_file_is_refused(self):
        path = self.file("odd.png")
        with patch("gateway.platforms.base.BasePlatformAdapter.extract_media", return_value=([], "")):
            self.assertEqual(self.refusal(path), "invalid_arguments")
        with patch("gateway.platforms.base.BasePlatformAdapter.extract_media",
                   return_value=([(path, False), ("/other.png", False)], "")):
            self.assertEqual(self.refusal(path), "invalid_arguments")


class SaveThroughTheChannelTests(ChannelCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(os.path.realpath(self.temporary.name))
        device_photos._state.grants.clear()
        self.addCleanup(device_photos._state.grants.clear)

    async def test_a_refused_path_never_reaches_the_phone(self):
        fields = self.fields(["photos"])
        self.hub.connect(self.owner, fields)
        (self.root / "notes.txt").write_text("hello")
        (self.root / "huge.png").touch()
        os.truncate(self.root / "huge.png", device_photos.MAX_SAVE_BYTES + 1)
        for number, (path, code) in enumerate((
            (str(self.root / "notes.txt"), "unsupported_media"),
            (str(self.root / "huge.png"), "file_too_large"),
            (str(self.root / "missing.png"), "invalid_arguments"),
            ("relative.png", "invalid_arguments"),
        )):
            with self.subTest(path=path):
                result = await self.call("photos.save", {"path": path}, f"call-{number}")
                self.assertEqual((result["status"], result["code"]), ("failed", code))
        self.assertEqual(self.hub.poll(self.owner, fields, after=0)["requests"], [])
        self.assertEqual(device_photos._state.grants, {})

    async def test_the_phone_gets_the_resolved_path_and_the_chat_may_resolve_it(self):
        fields = self.fields(["photos"])
        self.hub.connect(self.owner, fields)
        real = self.root / "real.png"
        real.write_bytes(b"x" * 100)
        link = self.root / "link.png"
        link.symlink_to(real)
        task = self.call("photos.save", {"path": str(link), "albumID": "a1"})
        await asyncio.sleep(0)
        [row] = self.hub.poll(self.owner, fields, after=0)["requests"]
        self.assertEqual(row["request"]["arguments"], {"path": str(real), "albumID": "a1"})
        self.assertEqual(device_photos.granted_paths("default", "stored-session", [str(real), str(link)]), {str(real)})
        self.assertEqual(device_photos.granted_paths("default", "another-chat", [str(real)]), set())
        self.assertEqual(device_photos.granted_paths("another-agent", "stored-session", [str(real)]), set())
        self.answer(fields, row["request"], {"id": "photo-9"})
        self.assertEqual((await task)["payload"], {"id": "photo-9"})

    async def test_a_grant_ends_and_is_bounded(self):
        with patch.object(device_photos, "_clock", return_value=1_000.0):
            device_photos.grant_save_path("default", ["s1"], "/a.png")
            self.assertEqual(device_photos.granted_paths("default", "s1", ["/a.png"]), {"/a.png"})
        with patch.object(device_photos, "_clock", return_value=1_000.0 + device_photos.GRANT_SECONDS + 1):
            self.assertEqual(device_photos.granted_paths("default", "s1", ["/a.png"]), set())
        device_photos._state.grants.clear()
        with patch.object(device_photos, "_clock", return_value=5.0):
            device_photos.grant_save_path("default", ["other-chat"], "/keep.png")
            for number in range(device_photos.MAX_GRANTS_PER_SESSION * 3):
                device_photos.grant_save_path("default", ["s1"], f"/{number}.png")
            chat = [key for key in device_photos._state.grants if key[1] == "s1"]
            self.assertEqual(len(chat), device_photos.MAX_GRANTS_PER_SESSION)
            # The newest stay; another chat's grant is not pushed out by this chat's.
            self.assertIn(("default", "s1", f"/{device_photos.MAX_GRANTS_PER_SESSION * 3 - 1}.png"), chat)
            self.assertNotIn(("default", "s1", "/0.png"), chat)
            self.assertEqual(device_photos.granted_paths("default", "other-chat", ["/keep.png"]), {"/keep.png"})


class AttachmentsServeGrantedPathsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(os.path.realpath(self.temporary.name))
        self.db = self.root / "state.db"
        import sqlite3
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)")
            connection.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT)")
            connection.execute("INSERT INTO sessions VALUES ('chat-1', NULL)")
            connection.execute("INSERT INTO messages (session_id, role, content) VALUES ('chat-1', 'assistant', 'No file here.')")
        na._store = AttachmentStore(self.root / "a.sqlite3")
        self.addCleanup(setattr, na, "_store", None)
        patcher = patch.object(na, "_state_db", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)
        device_photos._state.grants.clear()
        self.addCleanup(device_photos._state.grants.clear)
        self.picture = self.root / "trip.png"
        self.picture.write_bytes(b"p" * 1_500)

    def resolve(self, stored_id: str = "chat-1", item_id: str = "m1") -> list:
        body = na._Resolve(agentId="default", storedId=stored_id,
                           items=[na._Item(itemId=item_id, text=f"MEDIA:{self.picture}")])
        return na.resolve(body)["items"][0]["attachments"]

    def test_without_a_grant_the_path_is_not_served(self):
        self.assertEqual(self.resolve(), [])

    def test_with_a_grant_only_that_chat_gets_the_file(self):
        device_photos.grant_save_path("default", ["chat-1"], str(self.picture))
        [attachment] = self.resolve()
        self.assertEqual((attachment["fileName"], attachment["byteCount"]), ("trip.png", 1_500))
        self.assertEqual(self.resolve("chat-2"), [])

    def test_a_granted_file_may_be_larger_than_an_ordinary_agent_file(self):
        device_photos.grant_save_path("default", ["chat-1"], str(self.picture))
        with patch("loopdy_plugin.attachments.MAX_ARTIFACT_BYTES", 1_000), \
                patch.object(device_photos, "MAX_SAVE_BYTES", 2_000):
            self.assertEqual(len(self.resolve()), 1)
        with patch("loopdy_plugin.attachments.MAX_ARTIFACT_BYTES", 1_000), \
                patch.object(device_photos, "MAX_SAVE_BYTES", 1_400):
            self.assertEqual(self.resolve(item_id="m2"), [])

    def test_a_file_the_agent_already_sent_still_gets_the_photo_limit_when_granted(self):
        import sqlite3
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE messages SET content = ?", (f"MEDIA:{self.picture}",))
        device_photos.grant_save_path("default", ["chat-1"], str(self.picture))
        with patch("loopdy_plugin.attachments.MAX_ARTIFACT_BYTES", 1_000), \
                patch.object(device_photos, "MAX_SAVE_BYTES", 2_000):
            self.assertEqual(len(self.resolve()), 1)

    def fetch_all(self, attachment: dict) -> bytes:
        data, offset = b"", 0
        while offset is not None:
            chunk = na.fetch(na._Fetch(agentId="default", attachmentId=attachment["id"], offset=offset))
            data += base64.b64decode(chunk["data"])
            offset = chunk["nextOffset"]
        return data

    def test_the_same_path_saved_again_after_a_rewrite_serves_the_new_file(self):
        device_photos.grant_save_path("default", ["chat-1"], str(self.picture))
        [first] = self.resolve(item_id="photo")  # The app names every picture it saves "photo".
        self.assertEqual(self.fetch_all(first), b"p" * 1_500)
        self.picture.write_bytes(b"q" * 1_500)  # Same size: only the time differs.
        os.utime(self.picture, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        [second] = self.resolve(item_id="photo")
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(self.fetch_all(second), b"q" * 1_500)
        self.picture.write_bytes(b"r" * 1_700)  # A different size.
        [third] = self.resolve(item_id="photo")
        self.assertEqual(self.fetch_all(third), b"r" * 1_700)

    def test_an_unchanged_file_saved_again_reuses_the_cached_copy(self):
        device_photos.grant_save_path("default", ["chat-1"], str(self.picture))
        [first] = self.resolve(item_id="photo")
        [second] = self.resolve(item_id="photo")
        self.assertEqual(first["id"], second["id"])

    def test_a_file_past_the_old_offset_limit_can_be_fetched_to_its_end(self):
        # A 27 MB file is fetched in 3 MB pieces; the ninth starts past 25 MiB.
        big = self.root / "clip.mov"
        with big.open("wb") as handle:
            handle.truncate(27 * 1024 * 1024 + 5)
            handle.seek(27 * 1024 * 1024)
            handle.write(b"END!!")
        self.picture = big
        device_photos.grant_save_path("default", ["chat-1"], str(big))
        [attachment] = self.resolve(item_id="photo")
        self.assertEqual(attachment["byteCount"], 27 * 1024 * 1024 + 5)
        offset = 26 * 1024 * 1024
        chunk = na.fetch(na._Fetch(agentId="default", attachmentId=attachment["id"], offset=offset))
        self.assertEqual(chunk["offset"], offset)
        last = na.fetch(na._Fetch(agentId="default", attachmentId=attachment["id"], offset=27 * 1024 * 1024))
        self.assertEqual(base64.b64decode(last["data"]), b"END!!")
        self.assertIsNone(last["nextOffset"])
        na._Fetch(agentId="default", attachmentId="a" * 32, offset=device_photos.MAX_SAVE_BYTES)
        with self.assertRaises(ValueError):
            na._Fetch(agentId="default", attachmentId="a" * 32, offset=device_photos.MAX_SAVE_BYTES + 1)

    def test_an_ungranted_file_keeps_the_ordinary_limit(self):
        import sqlite3
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE messages SET content = ?", (f"MEDIA:{self.picture}",))
        with patch("loopdy_plugin.attachments.MAX_ARTIFACT_BYTES", 1_000), \
                patch.object(device_photos, "MAX_SAVE_BYTES", 2_000):
            self.assertEqual(self.resolve(), [])


if __name__ == "__main__":
    unittest.main()
