"""The iPhone tool request and result shapes, and the tools' fail-closed handlers."""
from __future__ import annotations

import asyncio
import unittest


class DeviceToolContractTests(unittest.TestCase):
    def test_device_tool_round_trip_preserves_composite_hermes_turn_ids(self):
        from loopdy_plugin.device_tool_contract import device_tool_request, device_tool_result

        for turn_id in (
            "session-1:session:loopdy_link:phone-1:9e4a120b",
            "session-1:" + "t" * 493 + ":12345678",  # Native 512-byte boundary.
        ):
            with self.subTest(turn_id=turn_id):
                request = device_tool_request(
                    request_id="request-device-tool-turn", device_id="phone-1",
                    host_id="host-1", authorization_epoch=7, session_id="session-1",
                    agent_id="default", turn_id=turn_id, operation="health.read",
                    arguments={
                        "start": "2026-09-09T17:39:16.650462-05:00",
                        "end": "2026-09-10T17:39:16.650462-05:00",
                        "timeZone": "America/Chicago", "limit": 20,
                        "types": ["step_count", "heart_rate", "sleep_analysis"],
                    }, sent_at=100, expires_at=130,
                )
                response = device_tool_result(
                    request=request, status="completed", payload={"items": []}, sent_at=101,
                )
                self.assertEqual(request["turnId"], turn_id)
                self.assertEqual(response["turnId"], turn_id)

    def test_device_tool_turn_ids_remain_bounded_and_reject_invalid_coordinates(self):
        from loopdy_plugin.device_tool_contract import (
            device_tool_request, device_tool_result, parse_device_tool_request,
            parse_device_tool_result,
        )

        request = device_tool_request(
            request_id="request-device-tool-turn", device_id="phone-1",
            host_id="host-1", authorization_epoch=7, session_id="session-1",
            agent_id="default", turn_id="turn-1", operation="reminders.list",
            arguments={}, sent_at=100, expires_at=130,
        )
        response = device_tool_result(
            request=request, status="completed", payload={"items": []}, sent_at=101,
        )
        for turn_id in (None, True, 123, "", "t" * 513, "turn\n1", "turn\x001",
                        "turn\x7f1", "turn 1", "turn/1", "turn\u202e1"):
            with self.subTest(turn_id=turn_id):
                with self.assertRaises(ValueError):
                    parse_device_tool_request({**request, "turnId": turn_id})
                with self.assertRaises(ValueError):
                    parse_device_tool_result({**response, "turnId": turn_id})

    def test_device_tool_request_and_result_are_strict_and_correlated(self):
        from loopdy_plugin.device_tool_contract import (
            device_tool_request,
            device_tool_result,
            parse_device_tool_request,
            parse_device_tool_result,
        )

        request = device_tool_request(
            request_id="request-device-tool-0001",
            device_id="phone-1",
            host_id="host-1",
            authorization_epoch=3,
            session_id="session-1",
            agent_id="default",
            turn_id="turn-1",
            operation="calendar.create",
            arguments={
                "title": "Dentist",
                "start": "2026-09-11T15:00:00Z",
                "end": "2026-09-11T16:00:00Z",
                "timeZone": "America/Chicago",
                "span": "thisEvent",
            },
            sent_at=100,
            expires_at=130,
        )
        parsed = parse_device_tool_request(request)
        self.assertEqual(parsed["requestId"], "request-device-tool-0001")
        self.assertEqual(parsed["operation"], "calendar.create")
        self.assertEqual(parsed["expiresAt"], 130)

        result = device_tool_result(
            request=parsed,
            status="completed",
            payload={"id": "event-1", "revision": "rev-1"},
            sent_at=101,
        )
        parsed_result = parse_device_tool_result(result, sender_device_id="phone-1")
        self.assertEqual(parsed_result["hostId"], "host-1")
        self.assertEqual(parsed_result["payload"]["id"], "event-1")

        with self.assertRaises(ValueError):
            parse_device_tool_request({**request, "unexpected": True})

        recurring = device_tool_request(
            request_id="request-device-tool-0002",
            device_id="phone-1",
            host_id="host-1",
            authorization_epoch=3,
            session_id="session-1",
            agent_id="default",
            turn_id="turn-1",
            operation="calendar.update",
            arguments={
                "id": "event-1",
                "expectedRevision": "rev-1",
                "occurrenceStart": "2026-09-11T15:00:00Z",
                "span": "thisEvent",
            },
            sent_at=100,
            expires_at=130,
        )
        self.assertEqual(recurring["arguments"]["occurrenceStart"], "2026-09-11T15:00:00Z")
        reminders = device_tool_request(
            request_id="request-device-tool-0003",
            device_id="phone-1",
            host_id="host-1",
            authorization_epoch=3,
            session_id="session-1",
            agent_id="default",
            turn_id="turn-1",
            operation="reminders.list",
            arguments={"completed": False, "includeUndated": True},
            sent_at=100,
            expires_at=130,
        )
        self.assertTrue(reminders["arguments"]["includeUndated"])

    def test_list_arguments_validate_health_catalog_ids_limits_and_ranges(self):
        from loopdy_plugin.device_tool_contract import device_tool_request

        base = {
            "start": "2026-09-01T00:00:00Z",
            "end": "2026-09-02T00:00:00Z",
            "timeZone": "America/Chicago",
            "limit": 200,
        }

        def request(operation, arguments):
            return device_tool_request(
                request_id=f"request-{operation.replace('.', '-')}-0001",
                device_id="phone-1",
                host_id="host-1",
                authorization_epoch=3,
                session_id="session-1",
                agent_id="default",
                turn_id="turn-1",
                operation=operation,
                arguments=arguments,
                sent_at=100,
                expires_at=130,
            )

        request("health.read", {**base, "types": ["step_count", "sleep_analysis"]})
        request("calendar.list", {**base, "calendarIDs": ["calendar-1"]})
        request("reminders.list", {"listIDs": ["list-1"], "limit": 1})

        with self.assertRaises(ValueError):
            request("health.read", {**base, "types": ["not-a-health-type"]})
        with self.assertRaises(ValueError):
            request("health.read", {**base, "limit": 201})
        with self.assertRaises(ValueError):
            request("calendar.list", {**base, "calendarIDs": []})
        with self.assertRaises(ValueError):
            request("calendar.list", {**base, "calendarIDs": ["calendar-1"] * 51})
        with self.assertRaises(ValueError):
            request("reminders.list", {"listIDs": ["list-1"], "limit": 0})

    def test_list_arguments_require_ordered_bounded_native_date_ranges(self):
        from loopdy_plugin.device_tool_contract import device_tool_request

        def request(operation, arguments):
            return device_tool_request(
                request_id=f"request-{operation.replace('.', '-')}-0002",
                device_id="phone-1",
                host_id="host-1",
                authorization_epoch=3,
                session_id="session-1",
                agent_id="default",
                turn_id="turn-1",
                operation=operation,
                arguments=arguments,
                sent_at=100,
                expires_at=130,
            )

        valid = {
            "start": "2026-09-01T00:00:00Z",
            "end": "2026-10-02T00:00:00Z",
            "timeZone": "America/Chicago",
        }
        request("health.read", valid)
        request("calendar.list", valid)
        request("reminders.list", {})

        for operation in ("health.read", "calendar.list"):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                request(operation, {**valid, "end": "2026-09-01T00:00:00Z"})
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                request(operation, {**valid, "end": "2026-10-03T00:00:00Z"})
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                request(operation, {"start": valid["start"], "timeZone": valid["timeZone"]})

        with self.assertRaises(ValueError):
            request("reminders.list", {"start": valid["start"], "timeZone": valid["timeZone"]})
        with self.assertRaises(ValueError):
            request("reminders.list", {**valid, "end": "2026-10-03T00:00:00Z"})

    def test_result_builder_rejects_timestamp_before_originating_request(self):
        from loopdy_plugin.device_tool_contract import device_tool_request, device_tool_result

        request = device_tool_request(
            request_id="request-device-tool-0004",
            device_id="phone-1",
            host_id="host-1",
            authorization_epoch=3,
            session_id="session-1",
            agent_id="default",
            turn_id="turn-1",
            operation="health.read",
            arguments={
                "start": "2026-09-01T00:00:00Z",
                "end": "2026-09-02T00:00:00Z",
                "timeZone": "UTC",
            },
            sent_at=100,
            expires_at=130,
        )
        with self.assertRaises(ValueError):
            device_tool_result(request=request, status="completed", payload={}, sent_at=99)
        self.assertEqual(
            device_tool_result(request=request, status="completed", payload={}, sent_at=100)["sentAt"],
            100,
        )

    def test_calendar_create_rejects_reversed_date_range(self):
        from loopdy_plugin.device_tool_contract import device_tool_request

        with self.assertRaises(ValueError):
            device_tool_request(
                request_id="request-device-tool-0005",
                device_id="phone-1",
                host_id="host-1",
                authorization_epoch=3,
                session_id="session-1",
                agent_id="default",
                turn_id="turn-1",
                operation="calendar.create",
                arguments={
                    "title": "Invalid",
                    "start": "2026-09-02T16:00:00Z",
                    "end": "2026-09-02T15:00:00Z",
                    "timeZone": "UTC",
                },
                sent_at=100,
                expires_at=130,
            )


class DeviceToolRegistrationTests(unittest.TestCase):
    def test_tools_fail_closed_when_the_native_channel_does_not_take_the_call(self):
        from loopdy_plugin.device_tools import register

        tools = {}

        class Context:
            def register_tool(self, *, name, handler, **_kwargs):
                tools[name] = handler

        register(Context())
        self.assertEqual(
            set(tools),
            {"iphone_health", "iphone_calendar", "iphone_reminders", "iphone_location", "iphone_appearance",
             "iphone_contacts", "iphone_photos", "iphone_geofences", "iphone_alarms", "iphone_music",
             "iphone_focus", "iphone_shortcuts"},
        )
        for name, handler in tools.items():
            with self.subTest(tool=name), self.assertRaisesRegex(ValueError, "context is unavailable"):
                asyncio.run(handler({"operation": "get"}, tool_name=name))


if __name__ == "__main__":
    unittest.main()
