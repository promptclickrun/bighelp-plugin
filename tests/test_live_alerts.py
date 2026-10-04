"""Instant alerts for a device with bighelp open, push only as the fallback."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from cryptography.hazmat.primitives.asymmetric import ec

from loopdy_plugin import live_alerts
from loopdy_plugin.managed_notifications import ManagedNotifications, session_reference
from loopdy_plugin.relay_crypto import b64url_decode, b64url_encode, key_id, public_key_bytes
from test_managed_notifications import open_sealed


class LiveAlertFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_800_000_000.0
        self.calls = []
        self.grant_id = str(uuid.uuid4())
        self.directory = Path(self.temp.name) / "managed"
        avatar = {"mimeType": "image/png", "sha256": hashlib.sha256(b"fixture-avatar").hexdigest(),
                  "data": "data:image/png;base64,Zml4dHVyZS1hdmF0YXI="}
        presentation = patch.object(ManagedNotifications, "_agent_presentation", return_value=("Fixture Agent", avatar))
        presentation.start()
        self.addCleanup(presentation.stop)
        self.avatar_sha256 = avatar["sha256"]
        self.service = self.process()
        self.grant = dict(grantId=self.grant_id, hostKeyId=self.service.key_id, hostPublicKey=self.service.public_key,
                          authorizationEpoch=1, profile="default",
                          eventTypes=["session.completed", "session.failed", "approval.required"],
                          createdAt=int(self.now) - 10, expiresAt=int(self.now) + 3600, revision=1,
                          provider="buzzkit", subscriberScope="account", state="active")
        self.phone = ec.generate_private_key(ec.SECP256R1())
        self.phone_public = b64url_encode(public_key_bytes(self.phone.public_key()))
        self.service.enroll(self.grant_id, str(uuid.uuid4()))
        self.service.register_recipient(self.grant_id, self.phone_public)
        self.recipient_key_id = key_id(b64url_decode(self.phone_public))
        self.calls.clear()

    def process(self, **options):
        """One Hermes process's copy of the notification service (its own memory)."""
        service = ManagedNotifications(self.directory, transport=self.transport, clock=lambda: self.now,
                                       session_opener=lambda profile, read, read_only: read(self), **options)
        self.addCleanup(service.close)
        return service

    def get_session(self, sid):
        return {"id": sid, "profile_name": "default", "title": "Trip"}

    def transport(self, method, path, raw, headers):
        self.calls.append((method, path, raw, self.now))
        if path.endswith("/events"):
            return {"version": 1, "status": "accepted", "deliveryId": "msg_fixture"}
        return {"version": 1, "grant": dict(self.grant, grantId=path.split("/")[4])}

    def pushes(self):
        return [call for call in self.calls if call[1].endswith("/events")]

    def listen_body(self, *, listener=None, wait=0, grants=None, known=()):
        return live_alerts.ListenBody(
            listenerId=listener or str(uuid.uuid4()), waitSeconds=wait, knownAvatars=list(known),
            grants=grants or [live_alerts.LiveGrant(grantId=self.grant_id, recipientKeyId=self.recipient_key_id)])

    def ack_body(self, event_ids):
        return live_alerts.AckBody(eventIds=list(event_ids), grants=[
            live_alerts.LiveGrant(grantId=self.grant_id, recipientKeyId=self.recipient_key_id)])

    def go_live(self, service=None, listener=None):
        service = service or self.service
        body = self.listen_body(listener=listener, wait=25)
        return live_alerts.register(service, body), body.listenerId

    def reply(self, service=None, turn="turn-a", text="Your flight is booked."):
        (service or self.service).observe("post_llm_call", profile="default", session_id="native-session",
                                          turn_id=turn, assistant_response=text, platform="desktop")

    def send_now(self, service=None):
        """The queueing process's own sender, with a clock that moves while it waits."""
        service = service or self.service
        def sleep(seconds):
            self.now += seconds
        with sqlite3.connect(service.db_path) as db:
            ids = [row[0] for row in db.execute("SELECT intent_id FROM pending WHERE path='/events'")]
        with patch.object(live_alerts, "await_acks", lambda svc, intents: original(svc, intents, sleep=sleep)):
            service._send_now(ids)


class LiveAlertTests(LiveAlertFixture):
    def test_live_device_gets_the_sealed_alert_and_its_ack_means_no_push(self):
        grants, listener = self.go_live()
        self.reply()
        alerts, owned = live_alerts.poll(self.service, grants, listener, set())
        self.assertTrue(owned)
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert["grantId"], self.grant_id)
        self.assertEqual(alert["agentId"], "default")
        self.assertEqual(alert["eventType"], "session.completed")
        self.assertEqual(alert["sessionReference"], session_reference("default", "native-session"))
        # The same envelope the push carries: only the phone opens it.
        with sqlite3.connect(self.service.db_path) as db:
            raw = json.loads(db.execute("SELECT raw FROM pending").fetchone()[0])
        self.assertEqual(alert["sealed"], raw["sealed"])
        opened = open_sealed(alert["sealed"], self.phone, b64url_decode(self.service.public_key))
        self.assertEqual((opened["title"], opened["body"]), ("Fixture Agent", "Your flight is booked."))
        self.assertEqual(live_alerts.ack(self.service, self.ack_body([alert["eventId"]])), [alert["eventId"]])
        self.send_now()
        self.now += 120
        self.service.drain_pending()
        self.assertEqual(self.pushes(), [])

    def test_no_ack_inside_the_window_sends_todays_push_unchanged(self):
        grants, listener = self.go_live()
        self.reply()
        alerts, _ = live_alerts.poll(self.service, grants, listener, set())
        self.assertEqual(len(alerts), 1)
        start = self.now
        self.send_now()
        self.assertGreaterEqual(self.now - start, live_alerts.ACK_WINDOW_SECONDS)
        self.assertLess(self.now - start, live_alerts.ACK_WINDOW_SECONDS + 0.2)
        pushes = self.pushes()
        self.assertEqual(len(pushes), 1)
        with sqlite3.connect(self.service.db_path) as db:
            raw = db.execute("SELECT raw FROM pending").fetchone()[0]
        self.assertEqual(pushes[0][2], raw)
        # A late ack can't take back a push that already went.
        self.assertEqual(live_alerts.ack(self.service, self.ack_body([alerts[0]["eventId"]])), [])

    def test_a_device_that_is_not_live_gets_the_push_at_once(self):
        self.reply()
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT next_attempt FROM pending").fetchone()[0], int(self.now))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM live_alerts").fetchone()[0], 0)
        start = self.now
        self.send_now()
        self.assertEqual(self.now, start)
        self.assertEqual(len(self.pushes()), 1)

    def test_a_live_device_never_holds_up_another_devices_push(self):
        ipad = str(uuid.uuid4())
        self.service.enroll(ipad, str(uuid.uuid4()))
        self.service.register_recipient(ipad, self.phone_public)
        self.go_live()
        self.reply()
        start = self.now
        self.send_now()
        when = {path.split("/")[4]: sent_at for _, path, _, sent_at in self.pushes()}
        self.assertEqual(when[ipad], start)
        self.assertGreaterEqual(when[self.grant_id], start + live_alerts.ACK_WINDOW_SECONDS)

    def test_listener_in_one_process_gets_an_alert_queued_in_another(self):
        dashboard = self.service
        gateway = self.process()
        grants, listener = self.go_live(dashboard)
        self.reply(gateway)
        alerts, _ = live_alerts.poll(dashboard, grants, listener, set())
        self.assertEqual([alert["eventType"] for alert in alerts], ["session.completed"])
        live_alerts.ack(dashboard, self.ack_body([alerts[0]["eventId"]]))
        self.send_now(gateway)
        self.assertEqual(self.pushes(), [])

    def test_long_poll_returns_as_soon_as_another_process_queues(self):
        gateway = self.process()
        body = self.listen_body(wait=25)
        ticks = []

        async def run(function, *args, **kwargs):
            return function(*args, **kwargs)

        async def sleep(seconds):
            ticks.append(seconds)
            if len(ticks) == 3:
                self.reply(gateway)

        async def disconnected():
            return False

        result = asyncio.run(live_alerts.listen(body, service=self.service, is_disconnected=disconnected,
                                                run=run, sleep=sleep, clock=lambda: len(ticks)))
        self.assertEqual(len(ticks), 3)
        self.assertEqual(len(result["alerts"]), 1)
        self.assertEqual(result["ackWindowMilliseconds"], 1000)
        # Between polls the device stays live for a short grace, then it isn't.
        with sqlite3.connect(self.service.db_path) as db:
            lease = db.execute("SELECT lease_expires FROM live_listeners").fetchone()[0]
        self.assertEqual(lease, self.now + live_alerts.LEASE_GRACE_SECONDS)
        self.now += live_alerts.LEASE_GRACE_SECONDS + 1
        self.reply(gateway, turn="turn-b", text="And the hotel too.")
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM live_alerts").fetchone()[0], 1)

    def test_a_device_that_goes_away_stops_being_live(self):
        body = self.listen_body(wait=25)

        async def run(function, *args, **kwargs):
            return function(*args, **kwargs)

        async def disconnected():
            return True

        result = asyncio.run(live_alerts.listen(body, service=self.service, is_disconnected=disconnected,
                                                run=run, sleep=lambda _: asyncio.sleep(0)))
        self.assertEqual(result["alerts"], [])
        self.reply()
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM live_listeners").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT next_attempt FROM pending").fetchone()[0], int(self.now))

    def test_each_alert_is_handed_to_a_poll_once_and_a_newer_poll_takes_over(self):
        grants, listener = self.go_live()
        self.reply()
        self.assertEqual(len(live_alerts.poll(self.service, grants, listener, set())[0]), 1)
        self.assertEqual(live_alerts.poll(self.service, grants, listener, set()), ([], True))
        newer = live_alerts.register(self.service, self.listen_body(wait=25))
        self.assertEqual(newer, grants)
        self.assertEqual(live_alerts.poll(self.service, grants, listener, set()), ([], False))

    def test_questions_and_approvals_keep_their_grace_period(self):
        grants, listener = self.go_live()
        self.service.observe("pre_approval_request", profile="default", surface="gateway",
                             session_id="native-session", turn_id="turn-a", tool_call_id="tool-a",
                             description="Delete the build folder")
        self.assertEqual(live_alerts.poll(self.service, grants, listener, set())[0], [])
        self.now += 3
        alerts, _ = live_alerts.poll(self.service, grants, listener, set())
        self.assertEqual([alert["eventType"] for alert in alerts], ["approval.required"])
        # Answered in the chat before the device acked: retired, never offered or pushed.
        self.service.observe("post_approval_response", profile="default", surface="gateway",
                             session_id="native-session", turn_id="turn-a", tool_call_id="tool-a")
        self.assertEqual(live_alerts.ack(self.service, self.ack_body([alerts[0]["eventId"]])), [])

    def test_the_picture_rides_along_only_when_the_device_lacks_it(self):
        grants, listener = self.go_live()
        self.reply()
        self.reply(turn="turn-b", text="Second reply.")
        first, second = live_alerts.poll(self.service, grants, listener, set())[0]
        self.assertTrue(first["avatar"]["data"].startswith("data:application/octet-stream;base64,"))
        live_alerts.ack(self.service, self.ack_body([first["eventId"], second["eventId"]]))
        grants, other = self.go_live()
        self.reply(turn="turn-c", text="Third reply.")
        (third,) = live_alerts.poll(self.service, grants, other, {self.avatar_sha256})[0]
        self.assertNotIn("data", third["avatar"])
        self.assertEqual(third["avatar"]["sha256"], first["avatar"]["sha256"])

    def test_bounds_and_enrollment_checks(self):
        wrong_key = live_alerts.LiveGrant(grantId=self.grant_id, recipientKeyId="A" * 43)
        with self.assertRaises(live_alerts.LiveAlertError) as raised:
            live_alerts.register(self.service, self.listen_body(grants=[wrong_key]))
        self.assertEqual(raised.exception.code, "live_alerts_not_enrolled")
        self.service.remove(self.grant_id)
        with self.assertRaises(live_alerts.LiveAlertError):
            live_alerts.register(self.service, self.listen_body())
        with self.assertRaises(ValueError):
            self.listen_body(wait=live_alerts.MAX_WAIT_SECONDS + 1)
        with self.assertRaises(ValueError):
            self.listen_body(grants=[wrong_key] * (live_alerts.MAX_GRANTS_PER_REQUEST + 1))
        with self.assertRaises(ValueError):
            self.listen_body(known=["not-a-hash"])
        with self.assertRaises(ValueError):
            self.ack_body(["not-an-event"])

    def test_live_devices_per_host_are_capped(self):
        with sqlite3.connect(self.service.db_path) as db:
            for _ in range(live_alerts.MAX_LIVE_GRANTS):
                other = str(uuid.uuid4())
                db.execute("INSERT INTO grants VALUES(?,?,'active',?)", (other, "{}", int(self.now) + 3600))
                db.execute("INSERT INTO live_listeners VALUES(?,?,?)", (other, str(uuid.uuid4()), self.now + 30))
        with self.assertRaises(live_alerts.LiveAlertError) as raised:
            self.go_live()
        self.assertEqual(raised.exception.code, "live_alerts_busy")
        self.now += 31
        self.go_live()


class LiveAlertRouteTests(LiveAlertFixture):
    """The routes behind Hermes' own sign-in, as the app calls them."""
    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
        from hermes_cli.dashboard_auth.registry import register_provider, unregister_global_provider
        from loopdy_plugin import native_api
        from test_native_api import FixtureProvider
        self.provider = FixtureProvider()
        register_provider(self.provider)
        self.addCleanup(unregister_global_provider, self.provider.name, self.provider)
        service = patch("loopdy_plugin.managed_notifications.get_managed_notifications", return_value=self.service)
        service.start()
        self.addCleanup(service.stop)
        app = FastAPI()
        app.state.auth_required = True
        app.middleware("http")(gated_auth_middleware)
        app.include_router(native_api.router, prefix="/api/plugins/loopdy")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def headers(self):
        auth = {"Authorization": "Bearer fixture-alice"}
        context = self.client.get("/api/plugins/loopdy/native/context", headers=auth)
        self.assertEqual(context.status_code, 200, context.text)
        self.assertIn(live_alerts.CAPABILITY, context.json()["features"])
        return {**auth, "If-Match": context.headers["etag"], "X-Loopdy-Request-ID": str(uuid.uuid4())}

    def post(self, path, payload, headers=None):
        return self.client.post("/api/plugins/loopdy/native/alerts/" + path,
                                headers=headers or self.headers(), json=payload)

    def test_listen_and_ack_over_http(self):
        grants = [{"grantId": self.grant_id, "recipientKeyId": self.recipient_key_id}]
        listener = str(uuid.uuid4())
        first = self.post("listen", {"listenerId": listener, "grants": grants, "waitSeconds": 0})
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["alerts"], [])
        self.assertEqual(first.json()["grantIds"], [self.grant_id])
        self.assertEqual(first.headers["cache-control"], "no-store")
        # With nothing to say, the request is held for the wait the device asked for.
        started = time.monotonic()
        held = self.post("listen", {"listenerId": listener, "grants": grants, "waitSeconds": 1})
        self.assertEqual(held.json()["alerts"], [])
        self.assertGreaterEqual(time.monotonic() - started, 1)
        self.reply()
        second = self.post("listen", {"listenerId": listener, "grants": grants, "waitSeconds": 1})
        self.assertEqual(second.status_code, 200, second.text)
        (alert,) = second.json()["alerts"]
        settled = self.post("ack", {"grants": grants, "eventIds": [alert["eventId"]]})
        self.assertEqual(settled.status_code, 200, settled.text)
        self.assertEqual(settled.json()["settled"], [alert["eventId"]])
        self.send_now()
        self.assertEqual(self.pushes(), [])

    def test_stop_makes_the_next_alert_push_at_once(self):
        grants = [{"grantId": self.grant_id, "recipientKeyId": self.recipient_key_id}]
        listener = str(uuid.uuid4())
        self.assertEqual(self.post("listen", {"listenerId": listener, "grants": grants, "waitSeconds": 0}).status_code, 200)
        stopped = self.post("stop", {"listenerId": listener, "grants": grants})
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.reply()
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM live_alerts").fetchone()[0], 0)
        start = self.now
        self.send_now()
        self.assertEqual((self.now, len(self.pushes())), (start, 1))

    def test_route_refusals_are_plain(self):
        stranger = [{"grantId": str(uuid.uuid4()), "recipientKeyId": self.recipient_key_id}]
        refused = self.post("listen", {"listenerId": str(uuid.uuid4()), "grants": stranger, "waitSeconds": 0})
        self.assertEqual(refused.status_code, 404)
        self.assertEqual(refused.json()["error"]["code"], "live_alerts_not_enrolled")
        too_long = self.post("listen", {"listenerId": str(uuid.uuid4()), "grants": stranger, "waitSeconds": 26})
        self.assertEqual(too_long.status_code, 422)
        no_context = {"Authorization": "Bearer fixture-alice", "X-Loopdy-Request-ID": str(uuid.uuid4())}
        self.assertEqual(self.post("ack", {"grants": stranger, "eventIds": []}, no_context).status_code, 428)
        signed_out = self.client.post("/api/plugins/loopdy/native/alerts/listen", json={})
        self.assertEqual(signed_out.status_code, 401)


original = live_alerts.await_acks


if __name__ == "__main__":
    unittest.main()
