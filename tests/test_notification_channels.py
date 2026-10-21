"""Settings › Notifications › Channels: each phone picks the channels it gets alerts from.

An ordinary turn comes from its session's channel (bighelp, iMessage, Telegram, a terminal). A scheduled run
comes from where its job delivers. A phone that never picked anything gets every alert, as before.
"""
from __future__ import annotations

import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

from pydantic import ValidationError

from loopdy_plugin import notification_channels as channels
from loopdy_plugin.managed_notifications import ManagedNotifications, ManagedNotificationError
from loopdy_plugin.managed_notifications_api import PreferencesBody
from test_scheduled_alert_links import JOB, RUN, ScheduledAlertLinkTests

ONLY_BIGHELP = {"other": False, "choices": {}}


class _Store:
    """One profile's Hermes session store, as `open_profile_store` hands it to a reader."""
    def __init__(self, rows): self.rows = rows
    def get_session(self, sid): return self.rows.get(sid)

    def session_count_by_source(self, *, include_archived=False):
        counts: dict[str, int] = {}
        for row in self.rows.values():
            counts[row.get("source") or "cli"] = counts.get(row.get("source") or "cli", 0) + 1
        return counts


class ChannelAlertTests(unittest.TestCase):
    setUp = ScheduledAlertLinkTests.setUp
    tearDown = ScheduledAlertLinkTests.tearDown
    make_service = ScheduledAlertLinkTests.make_service
    enroll = ScheduledAlertLinkTests.enroll
    transport = ScheduledAlertLinkTests.transport
    finish_run = ScheduledAlertLinkTests.finish_run
    sent = ScheduledAlertLinkTests.sent

    def open_store(self, profile, read, read_only=True):
        if not self.store_available:
            raise OSError("store unavailable")
        if profile not in self.sessions:
            raise LookupError("Profile unavailable")
        return read(_Store(self.sessions[profile]))

    def add_session(self, sid, source, profile="default"):
        self.sessions[profile][sid] = {"id": sid, "profile_name": profile, "source": source, "title": sid}

    def jobs(self, *found):
        self.service.cron_jobs = lambda profile: list(found)

    def rules(self, value, grant_id=None, **extra):
        return self.service.set_preferences(grant_id or self.grant_id, peer_chats=False, channels=value, **extra)

    def reply(self, session, turn="turn-a", profile="default"):
        self.service.observe("post_llm_call", profile=profile, session_id=session, turn_id=turn,
                             assistant_response="Here is your answer.", platform="cli")

    def alerted(self):
        # Each phone's alert has its own event, named for the phone's grant.
        return [(alert["eventId"].split(":", 1)[0], alert["eventType"]) for alert in self.sent()]

    def test_a_phone_that_never_picked_gets_every_channel_without_extra_reads(self):
        self.add_session("imsg-1", "imsg")
        with patch.object(ManagedNotifications, "_alert_channels", side_effect=AssertionError("not needed")):
            self.reply("imsg-1")
            self.reply("chat-1")
        self.assertEqual(self.alerted(), [(self.grant_id, "session.completed")] * 2)
        self.assertEqual(self.service.preferences(self.grant_id)["channels"], {"other": True, "choices": {}})
        self.assertTrue(self.service.capabilities()["preferences"]["channels"])

    def test_only_bighelp_keeps_bighelp_chats_and_drops_every_other_channel(self):
        for sid, source in (("imsg-1", "imsg"), ("cli-1", "cli"), ("tui-1", "tui"), ("desk-1", "desktop"),
                            ("old-1", "loopdy"), ("room-1", "bot_room")):
            self.add_session(sid, source)
        self.rules(ONLY_BIGHELP)
        for turn, session in enumerate(("imsg-1", "tg-1", "cli-1", "tui-1", "desk-1")):
            self.reply(session, turn=f"turn-{turn}")
        self.assertEqual(self.alerted(), [])
        for turn, session in enumerate(("chat-1", "old-1", "room-1")):
            self.reply(session, turn=f"turn-{turn}")
        self.assertEqual(len(self.alerted()), 3, "bighelp, an older bighelp build's chat and a Group Chat")

    def test_questions_and_approvals_follow_their_channel(self):
        grant = dict(self.grants[self.grant_id])
        grant["eventTypes"] = grant["eventTypes"] + ["clarification.required", "approval.required"]
        self.grants[self.grant_id] = grant
        with sqlite3.connect(self.service.db_path) as db:
            db.execute("UPDATE grants SET public_json=? WHERE grant_id=?",
                       (json.dumps(grant, separators=(",", ":"), sort_keys=True), self.grant_id))
        self.add_session("imsg-1", "imsg")
        self.rules(ONLY_BIGHELP)
        for session in ("imsg-1", "chat-1"):
            self.service._queue_event("default", session, "turn-q", "clarification.required",
                                      event_key="tool:call_1", content_text="Which day works?")
            self.service._queue_event("default", session, "turn-q", "approval.required",
                                      tool_call_id="call_2", content_text="Run the deploy script")
        with sqlite3.connect(self.service.db_path) as db:
            queued = db.execute("SELECT detail_json FROM events").fetchall()
        self.assertEqual(sorted((json.loads(row[0])["sessionId"], json.loads(row[0])["eventType"]) for row in queued),
                         [("chat-1", "approval.required"), ("chat-1", "clarification.required")])

    def test_two_phones_that_disagree_each_get_their_own(self):
        everything = self.grant_id
        bighelp_only = self.enroll("default")
        self.rules(ONLY_BIGHELP, grant_id=bighelp_only)
        self.add_session("imsg-1", "imsg")
        self.reply("imsg-1")
        self.assertEqual(self.alerted(), [(everything, "session.completed")])
        self.reply("chat-1", turn="turn-b")
        self.assertEqual(sorted(self.alerted()), sorted([(everything, "session.completed"),
                                                         (bighelp_only, "session.completed")]))

    def test_choose_turns_single_channels_off_and_decides_for_new_ones(self):
        self.add_session("imsg-1", "imsg")
        self.add_session("dc-1", "discord")
        self.rules({"other": True, "choices": {"imsg": False, "telegram": True}})
        self.reply("imsg-1"); self.reply("tg-1"); self.reply("dc-1")
        self.assertEqual(len(self.alerted()), 2, "Telegram, and Discord as a channel it hasn't picked yet")
        self.rules({"other": False, "choices": {"imsg": False, "telegram": True}})
        self.reply("imsg-1", turn="turn-b"); self.reply("tg-1", turn="turn-b"); self.reply("dc-1", turn="turn-b")
        self.assertEqual(len(self.alerted()), 1, "Only Telegram")

    def test_scheduled_runs_come_from_where_their_job_delivers(self):
        self.rules(ONLY_BIGHELP)
        cases = [
            ({"id": JOB, "deliver": "local"}, False, 1),
            ({"id": JOB, "deliver": "imsg:+15550100"}, False, 0),
            ({"id": JOB, "deliver": "imsg:+15550100", "failure_deliver": "local"}, True, 1),
            ({"id": JOB, "deliver": "local", "failure_deliver": "imsg:+15550100"}, True, 0),
            ({"id": JOB, "deliver": "origin", "origin": {"platform": "telegram", "chat_id": "42"}}, False, 0),
            ({"id": JOB, "deliver": "origin", "origin": {"platform": "loopdy"}}, False, 1),
            ({"id": JOB, "deliver": "telegram,bot-chat"}, False, 1),
            ({"id": JOB, "deliver": "all"}, False, 0),
            ({"id": "another-job", "deliver": "imsg:+15550100"}, False, 1),
        ]
        for turn, (job, failed, expected) in enumerate(cases):
            with self.subTest(job=job, failed=failed):
                self.jobs(job)
                self.finish_run(failed=failed, turn=f"turn-{turn}")
                self.assertEqual(len(self.alerted()), expected)

    def test_a_job_that_delivers_everywhere_alerts_a_phone_with_any_other_channel_on(self):
        self.jobs({"id": JOB, "deliver": "all"})
        self.rules({"other": False, "choices": {"imsg": True}})
        self.finish_run()
        self.assertEqual(len(self.alerted()), 1)

    def test_an_alert_whose_session_cant_be_read_still_goes_out(self):
        self.rules(ONLY_BIGHELP)
        self.store_available = False
        self.service._queue_event("default", "gone-session", "turn-a", "session.completed", content_text="Done.")
        self.assertEqual(len(self.alerted()), 1)

    def test_workflow_alerts_keep_their_own_switches(self):
        self.rules(ONLY_BIGHELP)
        with patch.object(ManagedNotifications, "_alert_channels", side_effect=AssertionError("not needed")):
            self.service._queue_event("default", "workflow.run.run_1", "turn-w", "session.completed",
                                      content_text="The run is done.", workflow_kind="succeeded", alerted_devices=set())
        self.assertEqual(len(self.alerted()), 1)

    def test_rules_round_trip_and_bad_ones_are_refused(self):
        value = {"other": False, "choices": {"imsg": True, "terminal": False}}
        self.assertEqual(self.rules(value)["channels"], value)
        self.assertEqual(self.service.preferences(self.grant_id)["channels"], value)
        bad = [{"other": True}, {"other": "no", "choices": {}}, {"other": True, "choices": {"bighelp": False}},
               {"other": True, "choices": {"Not A Channel": False}}, {"other": True, "choices": {"imsg": 0}},
               {"other": True, "choices": {f"c{n}": False for n in range(65)}},
               {"other": True, "choices": {}, "extra": 1}, []]
        for rules in bad:
            with self.subTest(rules=rules), self.assertRaises(ManagedNotificationError):
                self.rules(rules)
        self.assertEqual(self.service.preferences(self.grant_id)["channels"], value, "A refused change keeps the old")
        with self.assertRaises(ValidationError):
            PreferencesBody.model_validate({"version": 1, "peerChats": False,
                                            "channels": {"other": True, "choices": {}, "mode": "all"}})
        body = PreferencesBody.model_validate({"version": 1, "peerChats": False, "channels": value})
        self.assertEqual(body.channels.model_dump(), value)

    def test_turning_alerts_off_forgets_the_phones_channels(self):
        self.rules(ONLY_BIGHELP)
        self.service.remove(self.grant_id)
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM channel_rules").fetchone()[0], 0)

    def test_the_channels_list_names_what_the_agent_talked_on_and_delivers_to(self):
        self.add_session("imsg-1", "imsg")
        self.add_session("cli-1", "cli")
        self.add_session("old-1", "loopdy")
        self.jobs({"id": JOB, "deliver": "local", "failure_deliver": "discord:123"},
                  {"id": "other", "deliver": "all"})
        listed = self.service.channels(self.grant_id)
        self.assertEqual(listed["profile"], "default")
        self.assertEqual(listed["channels"], [{"id": "discord", "name": "Discord"}, {"id": "imsg", "name": "iMessage"},
                                              {"id": "telegram", "name": "Telegram"},
                                              {"id": "terminal", "name": "Terminal"}])
        with self.assertRaises(ManagedNotificationError):
            self.service.channels(str(uuid.uuid4()))


class ChannelNameTests(unittest.TestCase):
    def test_sources_become_channels(self):
        for source, channel in (("bighelp", "bighelp"), ("loopdy", "bighelp"), ("ios", "bighelp"),
                                ("bot_room", "bighelp"), ("workflow", "bighelp"), ("cli", "terminal"),
                                ("tui", "terminal"), ("oneshot", "terminal"), ("", "terminal"), (" IMSG ", "imsg"),
                                ("telegram", "telegram"), ("cron", None), ("not a channel!", None), (None, "terminal")):
            with self.subTest(source=source):
                self.assertEqual(channels.channel_of_source(source), channel)

    def test_names(self):
        self.assertEqual(channels.name("imsg"), "iMessage")
        self.assertEqual(channels.name("terminal"), "Terminal")
        self.assertEqual(channels.name("acme_chat"), "Acme Chat")

    def test_a_missing_jobs_file_is_no_jobs(self):
        with patch("hermes_cli.profiles.get_profile_dir", side_effect=ValueError("bad profile")):
            self.assertEqual(channels.read_cron_jobs("default"), [])


if __name__ == "__main__":
    unittest.main()
