"""Kinds of alert a phone can turn off (scheduled tasks, deliveries, group chats) and the
``bighelp_send_notification`` tool that makes a delivery."""
from __future__ import annotations
import telemetry_off  # noqa: F401  (no test run posts host telemetry)

import json
import sqlite3
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from loopdy_plugin import notify_tool
from loopdy_plugin.managed_notifications import ManagedNotifications, ManagedNotificationError, session_reference
from loopdy_plugin.managed_notifications_api import PreferencesBody
from test_scheduled_alert_links import RUN, ScheduledAlertLinkTests

ALL_ON = {"scheduledTasks": True, "deliveries": True, "groupChats": True}


class AlertKindTests(unittest.TestCase):
    tearDown = ScheduledAlertLinkTests.tearDown
    make_service = ScheduledAlertLinkTests.make_service
    open_store = ScheduledAlertLinkTests.open_store
    enroll = ScheduledAlertLinkTests.enroll
    transport = ScheduledAlertLinkTests.transport
    finish_run = ScheduledAlertLinkTests.finish_run
    sent = ScheduledAlertLinkTests.sent
    links_on = ScheduledAlertLinkTests.links_on

    def setUp(self):
        ScheduledAlertLinkTests.setUp(self)
        self.sessions["default"]["room-1"] = {"id": "room-1", "profile_name": "default", "source": "bot_room",
                                              "title": "Planning"}

    def kinds(self, value, grant_id=None):
        return self.service.set_preferences(grant_id or self.grant_id, peer_chats=False, kinds=value)

    def turn_on_questions(self):
        grant = dict(self.grants[self.grant_id])
        grant["eventTypes"] = grant["eventTypes"] + ["clarification.required", "approval.required"]
        self.grants[self.grant_id] = grant
        with sqlite3.connect(self.service.db_path) as db:
            db.execute("UPDATE grants SET public_json=? WHERE grant_id=?",
                       (json.dumps(grant, separators=(",", ":"), sort_keys=True), self.grant_id))

    def reply(self, session, turn="turn-a"):
        self.service.observe("post_llm_call", profile="default", session_id=session, turn_id=turn,
                             assistant_response="Here is your answer.", platform="bighelp")

    def types(self):
        return sorted(alert["eventType"] for alert in self.sent())

    def phones(self):
        return sorted(alert["eventId"].split(":")[0] for alert in self.sent())

    # The preferences

    def test_every_kind_starts_on_and_the_capability_is_listed(self):
        self.assertEqual(self.service.preferences(self.grant_id)["kinds"], ALL_ON)
        self.assertIs(self.service.capabilities()["preferences"]["kinds"], True)

    def test_kinds_are_stored_and_left_alone_when_the_body_has_none(self):
        value = {"scheduledTasks": False, "deliveries": True, "groupChats": False}
        self.assertEqual(self.kinds(value)["kinds"], value)
        # Another section, with no kinds: they stay.
        self.assertEqual(self.service.set_preferences(self.grant_id, peer_chats=True, scheduled_links=True)["kinds"], value)
        self.assertEqual(self.service.preferences(self.grant_id)["kinds"], value)

    def test_partial_unknown_and_non_boolean_kinds_are_refused_and_change_nothing(self):
        value = {"scheduledTasks": False, "deliveries": False, "groupChats": True}
        self.kinds(value)
        for bad in ({"scheduledTasks": True}, {**ALL_ON, "chats": True}, {**ALL_ON, "deliveries": "yes"},
                    {**ALL_ON, "groupChats": 1}, [], "all"):
            with self.assertRaises(ManagedNotificationError) as caught:
                self.kinds(bad)
            self.assertEqual((caught.exception.code, caught.exception.status), ("notification_preferences_invalid", 422))
        self.assertEqual(self.service.preferences(self.grant_id)["kinds"], value)

    def test_the_route_body_takes_all_three_kinds_or_none(self):
        base = {"version": 1, "peerChats": False}
        self.assertIsNone(PreferencesBody.model_validate(base).kinds)
        self.assertEqual(PreferencesBody.model_validate({**base, "kinds": ALL_ON}).kinds.model_dump(), ALL_ON)
        for bad in ({"scheduledTasks": True, "deliveries": True}, {**ALL_ON, "extra": True},
                    {**ALL_ON, "groupChats": "no"}, {**ALL_ON, "deliveries": 0}):
            with self.assertRaises(ValidationError):
                PreferencesBody.model_validate({**base, "kinds": bad})

    def test_turning_alerts_off_forgets_the_phones_kinds(self):
        self.kinds({**ALL_ON, "deliveries": False})
        self.service.remove(self.grant_id)
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM alert_kinds").fetchone()[0], 0)

    # Scheduled tasks

    def test_scheduled_tasks_off_drops_a_runs_alert_and_nothing_else(self):
        self.kinds({**ALL_ON, "scheduledTasks": False})
        self.finish_run()
        self.finish_run(failed=True, turn="turn-d")
        self.reply("chat-1")
        self.assertEqual(self.types(), ["session.completed"])
        self.assertEqual(self.service.notify("default", "chat-1", "Build", "Done.")["sent"], 1)
        self.assertEqual(self.types(), ["scheduled.completed"], "A delivery isn't a scheduled task")

    def test_scheduled_tasks_on_still_alerts(self):
        self.kinds({**ALL_ON, "deliveries": False, "groupChats": False})
        self.finish_run()
        self.assertEqual(self.types(), ["scheduled.completed"])

    # Group chats

    def test_group_chats_off_drops_a_group_chats_replies_and_failures_only(self):
        self.kinds({**ALL_ON, "groupChats": False})
        self.reply("room-1")
        self.service.observe("on_session_end", profile="default", session_id="room-1", turn_id="turn-b",
                             failed=True, error="provider unreachable", platform="bot_room")
        self.reply("chat-1", turn="turn-c")
        alerts = self.sent()
        self.assertEqual([alert["eventType"] for alert in alerts], ["session.completed"])
        self.assertEqual(alerts[0]["sessionReference"], session_reference("default", "chat-1"))
        self.assertEqual(self.service.notify("default", "room-1", "Plan", "Ready.")["sent"], 1,
                         "A delivery isn't a group chat alert")

    def test_group_chats_on_alerts_and_an_unreadable_session_alerts(self):
        self.reply("room-1")
        self.assertEqual(self.types(), ["session.completed"])
        self.kinds({**ALL_ON, "groupChats": False})
        self.reply("not-in-the-store", turn="turn-b")
        self.assertEqual(self.types(), ["session.completed"])

    def test_questions_and_approvals_from_a_group_chat_always_alert(self):
        self.turn_on_questions()
        self.kinds({**ALL_ON, "groupChats": False})
        self.service._queue_event("default", "room-1", "turn-q", "clarification.required",
                                  event_key="tool:call_1", content_text="Which day works?")
        self.service._queue_event("default", "room-1", "turn-q", "approval.required",
                                  tool_call_id="call_2", content_text="Run the deploy script")
        with sqlite3.connect(self.service.db_path) as db:
            queued = [json.loads(row[0])["eventType"] for row in db.execute("SELECT detail_json FROM events")]
        self.assertEqual(sorted(queued), ["approval.required", "clarification.required"])

    def test_each_phone_follows_its_own_kinds(self):
        other = self.enroll("default")
        self.kinds({**ALL_ON, "groupChats": False})
        self.reply("room-1")
        self.assertEqual(self.phones(), [other])

    # The tool's alert

    def test_a_delivery_alerts_each_phone_with_the_agents_name_and_the_chats_link(self):
        links_on = self.enroll("default")
        self.links_on(links_on)
        self.assertEqual(self.service.notify("default", "chat-1", "Build done", "All 214 tests passed."), {"sent": 2})
        alerts = self.sent()
        self.assertEqual({alert["eventType"] for alert in alerts}, {"scheduled.completed"})
        references = {alert["eventId"].split(":")[0]: alert["sessionReference"] for alert in alerts}
        self.assertEqual(references[links_on], session_reference("default", "scheduled.chat.chat-1"))
        self.assertEqual(references[self.grant_id], session_reference("default", "chat-1"),
                         "A phone that doesn't ask for scheduled links gets the chat's own name")
        with sqlite3.connect(self.service.db_path) as db:
            detail = json.loads(db.execute("SELECT detail_json FROM events").fetchone()[0])
        self.assertEqual((detail["agent"]["name"], detail["content"]["kind"], detail["content"]["text"]),
                         ("Fixture Agent", "scheduled", "Build done — All 214 tests passed."))

    def test_a_delivery_from_a_session_nobody_reads_names_no_chat(self):
        self.links_on()
        self.assertEqual(self.service.notify("default", RUN, "Weather", "Rain at 3.")["sent"], 1)
        self.assertEqual(self.sent()[0]["sessionReference"], session_reference("default", RUN))

    def test_a_delivery_needs_a_phone_for_this_agent(self):
        self.assertEqual(self.service.notify("atlas", "atlas-chat", "Hi", "There."),
                         {"sent": 0, "reason": "No phone has notifications on for this agent."})
        self.assertEqual(self.sent(), [])

    def test_a_delivery_is_dropped_when_every_phone_turned_deliveries_off(self):
        other = self.enroll("default")
        self.kinds({**ALL_ON, "deliveries": False})
        self.assertEqual(self.service.notify("default", "chat-1", "Hi", "There.")["sent"], 1)
        self.assertEqual(self.phones(), [other])
        self.kinds({**ALL_ON, "deliveries": False}, other)
        self.assertEqual(self.service.notify("default", "chat-1", "Hi", "There."),
                         {"sent": 0, "reason": "Every phone turned off Deliveries for this agent."})
        self.assertEqual(self.sent(), [])

    def test_a_notification_nobody_got_does_not_count_toward_the_limit(self):
        with sqlite3.connect(self.service.db_path) as db:
            db.execute("DELETE FROM recipients")  # The phone hasn't registered its content key yet.
        for _ in range(12):
            self.assertEqual(self.service.notify("default", "chat-1", "Hi", "There."),
                             {"sent": 0, "reason": "No phone could be alerted right now."})

    def test_ten_deliveries_an_hour_for_each_agent(self):
        self.enroll("atlas")
        with sqlite3.connect(self.service.db_path) as db:  # The fixture's grants last an hour; this test lasts longer.
            db.execute("UPDATE grants SET expires=?, public_json=json_set(public_json,'$.expiresAt',?)",
                       (self.now + 10_000, self.now + 10_000))
        for count in range(10):
            self.assertEqual(self.service.notify("default", "chat-1", f"Note {count}", "Text.")["sent"], 1)
        refused = self.service.notify("default", "chat-1", "Eleventh", "Text.")
        self.assertEqual(refused["sent"], 0)
        self.assertIn("10 notifications in the last hour", refused["reason"])
        self.assertEqual(self.service.notify("atlas", "atlas-chat", "Other agent", "Own limit.")["sent"], 1)
        self.assertEqual(len(self.sent()), 11)
        self.now += 3601
        self.assertEqual(self.service.notify("default", "chat-1", "Later", "An hour on.")["sent"], 1)
        self.assertEqual(len(self.sent()), 1)

    def test_a_delivery_in_quiet_hours_sends_no_push(self):
        # The clock reads 08:00 UTC; the window is 07:00 to 09:00.
        self.service.set_quiet_hours(self.grant_id, enabled=True, start_minute=420, end_minute=540, time_zone="UTC")
        self.assertEqual(self.service.notify("default", "chat-1", "Hi", "There.")["sent"], 1)
        self.assertEqual(self.sent(), [])


class _Registry:
    """What Hermes hands a plugin: the tools it registers."""
    profile_name = "default"

    def __init__(self):
        self.tools = {}

    def register_tool(self, name, toolset, schema, handler, emoji=None):
        self.tools[name] = (toolset, schema, handler)


class NotifyToolTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        calls = self.calls

        class Service:
            def notify(self, *args):
                calls.append(args)
                return {"sent": 1}

        self.registry = _Registry()
        notify_tool.register(self.registry)
        self.toolset, self.schema, self.handler = self.registry.tools["bighelp_send_notification"]
        patcher = patch.object(notify_tool, "get_managed_notifications", lambda: Service())
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tool(self, args, **context):
        return json.loads(self.handler(args, **context))

    def test_the_schema_is_strict_and_in_the_bighelp_toolset(self):
        self.assertEqual(self.toolset, "bighelp")
        parameters = self.schema["parameters"]
        self.assertIs(parameters["additionalProperties"], False)
        self.assertEqual(parameters["required"], ["title", "message"])
        self.assertEqual((parameters["properties"]["title"]["maxLength"], parameters["properties"]["message"]["maxLength"]),
                         (80, 1000))

    def test_a_valid_call_sends_the_trimmed_text_from_the_calls_own_chat(self):
        self.assertEqual(self.run_tool({"title": "  Build done ", "message": " All tests passed.\n"},
                                       session_id="chat-1"), {"sent": 1})
        self.assertEqual(self.calls, [("default", "chat-1", "Build done", "All tests passed.")])

    def test_invalid_arguments_send_nothing_and_say_why(self):
        for args in ({"title": "", "message": "Text."}, {"title": "   ", "message": "Text."},
                     {"title": "x" * 81, "message": "Text."}, {"title": "Hi", "message": ""},
                     {"title": "Hi", "message": "x" * 1001}, {"title": "Hi"},
                     {"title": "Hi", "message": "Text.", "sound": "loud"}, {"title": 5, "message": "Text."}, None):
            result = self.run_tool(args, session_id="chat-1")
            self.assertEqual(result["sent"], 0, args)
            self.assertTrue(result["reason"], args)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.run_tool({"title": "x" * 80, "message": "y" * 1000}, session_id="chat-1"), {"sent": 1})

    def test_an_unavailable_journal_is_a_plain_reason(self):
        def broken():
            raise ManagedNotificationError("notification_platform_unavailable", 503)
        with patch.object(notify_tool, "get_managed_notifications", broken):
            result = self.run_tool({"title": "Hi", "message": "There."}, session_id="chat-1")
        self.assertEqual(result["sent"], 0)
        self.assertNotIn("notification_platform", result["reason"])


if __name__ == "__main__":
    unittest.main()
