from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
import uuid
import hashlib
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ec

from loopdy_plugin import quiet_hours
from loopdy_plugin.managed_notifications import ManagedNotifications, ManagedNotificationError
from loopdy_plugin.relay_crypto import b64url_encode, public_key_bytes


def at(text: str) -> float:
    """Unix seconds for an ISO time in UTC."""
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


NIGHT = {"enabled": True, "startMinute": 22 * 60, "endMinute": 7 * 60, "timeZone": "Europe/Berlin"}


class QuietHoursWindowTests(unittest.TestCase):
    def test_a_window_on_one_day(self):
        self.assertFalse(quiet_hours.contains(9 * 60, 17 * 60, 8 * 60 + 59))
        self.assertTrue(quiet_hours.contains(9 * 60, 17 * 60, 9 * 60))
        self.assertTrue(quiet_hours.contains(9 * 60, 17 * 60, 16 * 60 + 59))
        self.assertFalse(quiet_hours.contains(9 * 60, 17 * 60, 17 * 60), "The end is outside the window")

    def test_a_window_across_midnight(self):
        start, end = 22 * 60, 7 * 60
        self.assertFalse(quiet_hours.contains(start, end, 21 * 60 + 59))
        self.assertTrue(quiet_hours.contains(start, end, 22 * 60))
        self.assertTrue(quiet_hours.contains(start, end, 23 * 60 + 59))
        self.assertTrue(quiet_hours.contains(start, end, 0))
        self.assertTrue(quiet_hours.contains(start, end, 6 * 60 + 59))
        self.assertFalse(quiet_hours.contains(start, end, 7 * 60))
        self.assertFalse(quiet_hours.contains(start, end, 12 * 60))

    def test_equal_start_and_end_is_never_quiet(self):
        for minute in (0, 22 * 60, 22 * 60 + 1, 1439):
            self.assertFalse(quiet_hours.contains(22 * 60, 22 * 60, minute))

    def test_now_is_read_on_the_devices_own_clock(self):
        # 21:30 UTC is 23:30 in Berlin in summer (UTC+2) and 22:30 in winter (UTC+1).
        self.assertTrue(quiet_hours.is_quiet(NIGHT, at("2026-07-01T21:30:00")))
        self.assertTrue(quiet_hours.is_quiet(NIGHT, at("2026-01-15T21:30:00")))
        # 20:30 UTC is 22:30 in summer but 21:30 in winter.
        self.assertTrue(quiet_hours.is_quiet(NIGHT, at("2026-07-01T20:30:00")))
        self.assertFalse(quiet_hours.is_quiet(NIGHT, at("2026-01-15T20:30:00")))
        # 05:30 UTC is 07:30 in summer (awake) and 06:30 in winter (still quiet).
        self.assertFalse(quiet_hours.is_quiet(NIGHT, at("2026-07-01T05:30:00")))
        self.assertTrue(quiet_hours.is_quiet(NIGHT, at("2026-01-15T05:30:00")))

    def test_daylight_saving_changes_move_the_window_with_the_clock(self):
        new_york = {"enabled": True, "startMinute": 22 * 60, "endMinute": 7 * 60, "timeZone": "America/New_York"}
        # Clocks went forward on 8 March 2026 at 02:00. 11:30 UTC is 06:30 before
        # and 07:30 after, so the same instant on the next day is no longer quiet.
        self.assertTrue(quiet_hours.is_quiet(new_york, at("2026-03-07T11:30:00")))
        self.assertFalse(quiet_hours.is_quiet(new_york, at("2026-03-09T11:30:00")))
        # Clocks went back on 1 November 2026. 02:30 UTC is 22:30 before and 21:30 after.
        self.assertTrue(quiet_hours.is_quiet(new_york, at("2026-10-31T02:30:00")))
        self.assertFalse(quiet_hours.is_quiet(new_york, at("2026-11-02T02:30:00")))

    def test_off_or_missing_is_never_quiet(self):
        self.assertFalse(quiet_hours.is_quiet(None, at("2026-07-01T21:30:00")))
        self.assertFalse(quiet_hours.is_quiet(dict(NIGHT, enabled=False), at("2026-07-01T21:30:00")))

    def test_input_is_checked(self):
        good = dict(enabled=True, start_minute=1320, end_minute=420, time_zone="Asia/Kolkata")
        self.assertEqual(quiet_hours.window(**good), {"enabled": True, "startMinute": 1320, "endMinute": 420,
                                                      "timeZone": "Asia/Kolkata"})
        for change, code in (({"enabled": "yes"}, "quiet_hours_invalid"),
                             ({"start_minute": 1440}, "quiet_hours_invalid"),
                             ({"end_minute": -1}, "quiet_hours_invalid"),
                             ({"start_minute": True}, "quiet_hours_invalid"),
                             ({"start_minute": 60.0}, "quiet_hours_invalid"),
                             ({"time_zone": "Mars/Olympus_Mons"}, "quiet_hours_time_zone_invalid"),
                             ({"time_zone": "../../etc/passwd"}, "quiet_hours_time_zone_invalid"),
                             ({"time_zone": "/etc/localtime"}, "quiet_hours_time_zone_invalid"),
                             ({"time_zone": "A" * 65}, "quiet_hours_time_zone_invalid"),
                             ({"time_zone": ""}, "quiet_hours_time_zone_invalid"),
                             ({"time_zone": None}, "quiet_hours_time_zone_invalid")):
            with self.subTest(change=change):
                with self.assertRaises(quiet_hours.QuietHoursError) as raised:
                    quiet_hours.window(**(good | change))
                self.assertEqual(raised.exception.code, code)


class QuietHoursDeliveryTests(unittest.TestCase):
    """The host checks each device's window just before it sends that device an alert."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        # 23:00 in Berlin (summer time): inside 22:00 to 07:00.
        self.now = int(at("2026-07-01T21:00:00"))
        self.calls = []
        self.service = ManagedNotifications(Path(self.temp.name) / "managed", transport=self.transport,
            clock=lambda: self.now, session_opener=lambda profile, read, read_only: read(self))
        avatar = {"mimeType": "image/png", "sha256": hashlib.sha256(b"fixture-avatar").hexdigest(),
                  "data": "data:image/png;base64,Zml4dHVyZS1hdmF0YXI="}
        presentation = patch.object(ManagedNotifications, "_agent_presentation", return_value=("Fixture Agent", avatar))
        presentation.start()
        self.addCleanup(presentation.stop)
        self.grants = {}
        self.phone = b64url_encode(public_key_bytes(ec.generate_private_key(ec.SECP256R1()).public_key()))
        # One person, two devices: an iPhone with Quiet Hours and an iPad without.
        self.iphone, self.ipad = self.enroll(), self.enroll()
        self.calls.clear()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def enroll(self) -> str:
        grant_id = str(uuid.uuid4())
        self.grants[grant_id] = dict(grantId=grant_id, hostKeyId=self.service.key_id,
            hostPublicKey=self.service.public_key, authorizationEpoch=1, profile="default",
            eventTypes=["session.completed", "session.failed", "approval.required"],
            createdAt=self.now - 10, expiresAt=self.now + 30 * 86400 - 20, revision=1, provider="buzzkit",
            subscriberScope="account", state="active")
        self.service.enroll(grant_id, str(uuid.uuid4()))
        self.service.register_recipient(grant_id, self.phone)
        return grant_id

    def get_session(self, sid):
        return {"id": sid, "profile_name": "default", "title": "Trip"}

    def transport(self, method, path, raw, headers):
        self.calls.append((method, path, raw))
        if path.endswith("/events") or path.endswith("/wake"):
            return {"version": 1, "status": "accepted", "deliveryId": "msg_fixture"}
        grant_id = path.split("/")[4]
        return {"version": 1, "grant": self.grants[grant_id]}

    def sent_to(self):
        return [path.split("/")[4] for method, path, raw in self.calls if path.endswith("/events")]

    def reply(self, turn: str):
        self.service.observe("post_llm_call", profile="default", session_id="native-session", turn_id=turn,
                             assistant_response="Here is your answer.", platform="desktop")
        self.service.drain_pending()

    def states(self, grant_id):
        with sqlite3.connect(self.service.db_path) as db:
            return [row[0] for row in db.execute(
                "SELECT state FROM pending WHERE grant_id=? AND path='/events' ORDER BY rowid", (grant_id,))]

    def test_a_device_in_its_quiet_hours_gets_nothing_and_the_others_still_do(self):
        saved = self.service.set_quiet_hours(self.iphone, **self.night())
        self.assertEqual(saved, {"version": 1, "grantId": self.iphone, "quietHours": NIGHT, "quietNow": True})
        self.reply("turn-a")
        self.assertEqual(self.sent_to(), [self.ipad], "Nothing for the iPhone reaches the notification service")
        self.assertEqual(self.states(self.iphone), ["quiet"])

    def test_a_device_with_bighelp_open_in_its_quiet_hours_gets_no_instant_alert(self):
        self.service.set_quiet_hours(self.iphone, **self.night())
        with sqlite3.connect(self.service.db_path) as db:
            for grant_id in (self.iphone, self.ipad):
                db.execute("INSERT INTO live_listeners VALUES(?,?,?)", (grant_id, str(uuid.uuid4()), self.now + 30))
        self.service.observe("post_llm_call", profile="default", session_id="native-session", turn_id="turn-a",
                             assistant_response="Here is your answer.", platform="desktop")
        with sqlite3.connect(self.service.db_path) as db:
            offered = [row[0] for row in db.execute("SELECT grant_id FROM live_alerts")]
        self.assertEqual(offered, [self.ipad], "Only the device outside its window is offered the alert")
        self.now += 5  # past the ack window: the iPad's push may go, the iPhone's never does
        self.service.drain_pending()
        self.assertEqual(self.sent_to(), [self.ipad])
        self.assertEqual(self.states(self.iphone), ["quiet"])

    def test_a_skipped_alert_is_not_sent_later(self):
        self.service.set_quiet_hours(self.iphone, **self.night())
        self.reply("turn-a")
        self.now = int(at("2026-07-02T06:00:00"))  # 08:00 in Berlin: the window is over.
        self.service.drain_pending()
        self.assertEqual(self.sent_to(), [self.ipad])
        self.reply("turn-b")
        self.assertEqual(self.sent_to()[0], self.ipad)
        self.assertEqual(sorted(self.sent_to()[1:]), sorted([self.iphone, self.ipad]), "New alerts go out again")

    def test_an_alert_waiting_for_a_retry_is_checked_again_before_it_goes(self):
        self.now = int(at("2026-07-01T19:59:00"))  # 21:59 in Berlin
        self.service.set_quiet_hours(self.iphone, **self.night())
        def offline(method, path, raw, headers):
            raise ManagedNotificationError("synthetic_unavailable", 503)
        working, self.service.transport = self.service.transport, offline
        self.reply("turn-a")
        self.service.transport = working
        self.now += 120  # 22:01: the retry falls in the window.
        self.service.drain_pending()
        self.assertEqual(self.sent_to(), [self.ipad])
        self.assertEqual(self.states(self.iphone), ["quiet"])

    def test_questions_and_approvals_wait_in_the_chat(self):
        self.service.set_quiet_hours(self.iphone, **self.night())
        self.service.set_quiet_hours(self.ipad, **self.night())
        self.service.subscribe(self.iphone, "default", "native-session", True)
        self.service._queue_event("default", "native-session", "turn-a", "approval.required",
                                  tool_call_id="call-1", content_text="Run ls?")
        self.now += 5  # past the approval's short grace period
        self.service.drain_pending()
        self.assertEqual(self.sent_to(), [])
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM approval_attention WHERE state='pending'").fetchone()[0], 0)

    def test_turning_quiet_hours_off_sends_again(self):
        self.service.set_quiet_hours(self.iphone, **self.night())
        self.service.set_quiet_hours(self.iphone, **(self.night() | {"enabled": False}))
        self.assertFalse(self.service.quiet_hours(self.iphone)["quietNow"])
        self.reply("turn-a")
        self.assertEqual(sorted(self.sent_to()), sorted([self.iphone, self.ipad]))

    def test_the_sign_in_wake_is_not_an_alert_and_still_goes(self):
        self.service.set_quiet_hours(self.iphone, **self.night())
        self.service.queue_sign_in_wakes()
        self.service.drain_pending()
        woken = [path.split("/")[4] for method, path, raw in self.calls if path.endswith("/wake")]
        self.assertEqual(sorted(woken), sorted([self.iphone, self.ipad]))

    def test_window_belongs_to_one_active_grant_and_goes_with_it(self):
        self.assertEqual(self.service.quiet_hours(self.iphone),
                         {"version": 1, "grantId": self.iphone, "quietHours": None, "quietNow": False})
        with self.assertRaises(ManagedNotificationError) as unknown:
            self.service.set_quiet_hours(str(uuid.uuid4()), **self.night())
        self.assertEqual(unknown.exception.status, 404)
        with self.assertRaises(ManagedNotificationError) as invalid:
            self.service.set_quiet_hours(self.iphone, **(self.night() | {"time_zone": "Nowhere/Land"}))
        self.assertEqual((invalid.exception.status, invalid.exception.code), (422, "quiet_hours_time_zone_invalid"))
        self.service.set_quiet_hours(self.iphone, **self.night())
        self.service.remove(self.iphone)
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM quiet_hours").fetchone()[0], 0)

    @staticmethod
    def night():
        return dict(enabled=True, start_minute=22 * 60, end_minute=7 * 60, time_zone="Europe/Berlin")


if __name__ == "__main__":
    unittest.main()
