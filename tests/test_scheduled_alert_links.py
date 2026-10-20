"""Where a tap on a scheduled task's alert goes (#181).

A scheduled run happens in a session of its own, which no person opened. When the job was made in
a chat (the agent called Hermes' cron tool there), the alert names that chat; otherwise it names
the job itself. Only phones that said they can open these links get them: an older phone keeps
the run's own session, as before.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ec

from loopdy_plugin.managed_notifications import ManagedNotifications, ManagedNotificationError, session_reference
from loopdy_plugin.notification_crypto import b64url_decode, b64url_encode, canonical_json_bytes, public_key_bytes
from test_managed_notifications import open_sealed

JOB = "a1b2c3d4e5f6"
RUN = f"cron_{JOB}_20261021_070000"


def created(job: str = JOB, name: str = "Morning weather") -> str:
    """Hermes' `cronjob_manage` answer to action='create' (tools/cronjob_tools.py `_action_create`)."""
    formatted = {"job_id": job, "name": name, "skill": None, "skills": [],
                 "prompt_preview": "Check the weather for Lisbon and say if I need an umbrella.",
                 "model": None, "provider": None, "pinned": False, "base_url": None,
                 "schedule": "0 7 * * *", "repeat": "forever", "deliver": "local",
                 "next_run_at": "2026-10-21T07:00:00+00:00", "last_run_at": None, "last_status": None,
                 "enabled": True, "state": "scheduled", "paused_at": None, "paused_reason": None}
    return json.dumps({"success": True, "job_id": job, "name": name, "skill": None, "skills": [],
                       "schedule": "0 7 * * *", "repeat": "forever", "deliver": "local",
                       "next_run_at": "2026-10-21T07:00:00+00:00", "job": formatted,
                       "message": f"Cron job '{name}' created."}, indent=2)


def updated(job: str = JOB) -> str:
    return json.dumps({"success": True, "job": {"job_id": job, "name": "Morning weather",
                                                "schedule": "30 7 * * *", "deliver": "local"}}, indent=2)


def removed(job: str = JOB) -> str:
    return json.dumps({"success": True, "message": "Cron job 'Morning weather' removed.",
                       "removed_job": {"id": job, "name": "Morning weather", "schedule": "0 7 * * *"}}, indent=2)


class _Store:
    """One profile's Hermes session store, as `open_profile_store` hands it to a reader."""
    def __init__(self, rows): self.rows = rows
    def get_session(self, sid): return self.rows.get(sid)


class ScheduledAlertLinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name) / "managed"
        self.now = 1_800_000_000
        self.calls = []
        # Each profile's sessions, by id. Chats people opened, and a scheduled run of their own.
        self.sessions = {
            "default": {"chat-1": {"id": "chat-1", "profile_name": "default", "source": "bighelp", "title": "Trip"},
                        "chat-2": {"id": "chat-2", "profile_name": "default", "source": "bighelp", "title": "Garden"},
                        "tg-1": {"id": "tg-1", "profile_name": "default", "source": "telegram", "title": "Telegram"},
                        RUN: {"id": RUN, "profile_name": "default", "source": "cron", "title": "Morning weather"}},
            "atlas": {"atlas-chat": {"id": "atlas-chat", "profile_name": "atlas", "source": "bighelp", "title": "Atlas"}},
        }
        self.store_available = True
        self.service = self.make_service()
        avatar = {"mimeType": "image/png", "sha256": hashlib.sha256(b"fixture-avatar").hexdigest(),
                  "data": "data:image/png;base64,Zml4dHVyZS1hdmF0YXI="}
        presentation = patch.object(ManagedNotifications, "_agent_presentation", return_value=("Fixture Agent", avatar))
        presentation.start()
        self.addCleanup(presentation.stop)
        self.phone = ec.generate_private_key(ec.SECP256R1())
        self.phone_public = b64url_encode(public_key_bytes(self.phone.public_key()))
        self.grants = {}
        self.grant_id = self.enroll("default")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def make_service(self):
        return ManagedNotifications(self.directory, transport=self.transport, clock=lambda: self.now,
                                    session_opener=self.open_store)

    def open_store(self, profile, read, read_only=True):
        if not self.store_available:
            raise OSError("store unavailable")
        if profile not in self.sessions:
            raise LookupError("Profile unavailable")
        return read(_Store(self.sessions[profile]))

    def enroll(self, profile):
        grant_id = str(uuid.uuid4())
        self.grants[grant_id] = dict(
            grantId=grant_id, hostKeyId=self.service.key_id, hostPublicKey=self.service.public_key,
            authorizationEpoch=1, profile=profile,
            eventTypes=["session.completed", "session.failed", "scheduled.completed", "scheduled.failed"],
            createdAt=self.now - 10, expiresAt=self.now + 3600, revision=1, provider="buzzkit",
            subscriberScope="account", state="active")
        self.service.enroll(grant_id, str(uuid.uuid4()))
        self.service.register_recipient(grant_id, self.phone_public)
        self.calls.clear()
        return grant_id

    def transport(self, method, path, raw, headers):
        self.calls.append((method, path, raw))
        if path.endswith("/events"):
            return {"version": 1, "status": "accepted", "deliveryId": "msg_fixture"}
        grant_id = path.split("/")[4]
        return {"version": 1, "grant": self.grants[grant_id]}

    def links_on(self, grant_id=None):
        self.service.set_preferences(grant_id or self.grant_id, peer_chats=False, scheduled_links=True)

    def cron_tool(self, action, result, *, session="chat-1", profile="default", tool="cronjob_manage",
                  args=None, **extra):
        """A real-shaped `post_tool_call` for Hermes' cron tool, as a chat turn runs it."""
        arguments = {"action": action, "prompt": "Check the weather for Lisbon and say if I need an umbrella.",
                     "schedule": "every day at 7am", "name": "Morning weather", "deliver": "local"}
        if action != "create":
            arguments = {"action": action, "job_id": JOB}
        self.service.observe("post_tool_call", profile=profile, tool_name=tool, args=args or arguments,
                             result=result, task_id=session, session_id=session, tool_call_id="call_7f3a",
                             turn_id="turn-1", api_request_id="req-1", duration_ms=12, status="ok",
                             error_type=None, error_message=None, middleware_trace=[], **extra)

    def finish_run(self, *, session=RUN, profile="default", failed=False, turn="turn-c", execution=None):
        """A scheduled run finishing, through the hooks Hermes fires for it."""
        module = types.ModuleType("cron.execution_identity")
        module.current_cron_execution = lambda: execution
        with patch.dict(sys.modules, {"cron.execution_identity": module}):
            if failed:
                self.service.observe("on_session_end", profile=profile, session_id=session, turn_id=turn,
                                     failed=True, error="provider unreachable", platform="cron")
            else:
                self.service.observe("post_llm_call", profile=profile, session_id=session, turn_id=turn,
                                     assistant_response="Rain after 3 pm. Take an umbrella.", platform="cron")
                self.service.observe("on_session_end", profile=profile, session_id=session, turn_id=turn,
                                     completed=True, platform="cron")

    def sent(self):
        self.service.drain_pending()
        alerts = []
        for _, path, raw in self.calls:
            if not path.endswith("/events"):
                continue
            body = json.loads(raw)
            open_sealed(body["sealed"], self.phone, b64url_decode(self.service.public_key))
            alerts.append(body)
        self.calls.clear()
        return alerts

    def links(self):
        with sqlite3.connect(self.service.db_path) as db:
            return db.execute("SELECT profile,job_id,session_id FROM scheduled_chats ORDER BY profile,job_id").fetchall()

    @staticmethod
    def chat(session, profile="default"):
        return session_reference(profile, "scheduled.chat." + session)

    @staticmethod
    def job(job=JOB, profile="default"):
        return session_reference(profile, "scheduled.job." + job)

    def run_event_id(self, event_type, *, session=RUN, turn="turn-c", grant_id=None):
        digest = hashlib.sha256(canonical_json_bytes(["default", session, turn, event_type, ""])).hexdigest()
        return (grant_id or self.grant_id) + ":" + digest

    # The association

    def test_a_job_made_in_a_chat_is_linked_to_that_chat(self):
        self.cron_tool("create", created())
        self.assertEqual(self.links(), [("default", JOB, "chat-1")])

    def test_the_older_tool_name_links_too(self):
        self.cron_tool("create", created(), tool="cronjob")
        self.assertEqual(self.links(), [("default", JOB, "chat-1")])

    def test_an_update_from_another_chat_moves_the_link_and_a_removal_ends_it(self):
        self.cron_tool("create", created())
        self.cron_tool("update", updated(), session="chat-2")
        self.assertEqual(self.links(), [("default", JOB, "chat-2")])
        self.cron_tool("pause", updated(), session="chat-1")
        self.assertEqual(self.links(), [("default", JOB, "chat-2")])
        self.cron_tool("remove", removed(), session="tg-1")
        self.assertEqual(self.links(), [])

    def test_only_a_successful_well_formed_answer_links(self):
        for result in (json.dumps({"success": False, "error": "schedule is required for create"}),
                       "not json", json.dumps(["a"]), json.dumps({"success": True}),
                       json.dumps({"success": True, "job_id": "has space"}),
                       json.dumps({"success": True, "job_id": "x" * 65}),
                       json.dumps({"success": "true", "job_id": JOB}),
                       " " * 300_000 + created()):
            self.cron_tool("create", result)
        self.cron_tool("list", created())
        self.cron_tool("create", created(), args="not arguments")
        for tool in ("terminal", ["cronjob_manage"], None):
            self.service.observe("post_tool_call", profile="default", tool_name=tool,
                                 args={"action": "create"}, result=created(), session_id="chat-1", turn_id="turn-1")
        self.assertEqual(self.links(), [])
        # A dict answer (an in-process caller) reads the same as its JSON text.
        self.cron_tool("create", json.loads(created()))
        self.assertEqual(self.links(), [("default", JOB, "chat-1")])

    def test_a_job_made_outside_a_persons_chat_has_no_chat(self):
        # A scheduled run that makes another job, an unknown session, a helper's turn.
        self.cron_tool("create", created(), session=RUN)
        self.cron_tool("create", created(), session="not-a-session")
        self.cron_tool("create", created(), parent_session_id="chat-1", session="child-1")
        module = types.ModuleType("cron.execution_identity")
        module.current_cron_execution = lambda: types.SimpleNamespace(job_id="b0b0b0b0b0b0", profile="default")
        self.sessions["default"]["local-run"] = {"id": "local-run", "profile_name": "default", "source": "bighelp"}
        with patch.dict(sys.modules, {"cron.execution_identity": module}):
            self.cron_tool("create", created(), session="local-run")
        self.store_available = False
        self.cron_tool("create", created())
        self.assertEqual(self.links(), [])

    def test_a_chat_of_another_agent_is_never_linked(self):
        # The tool call says "atlas", but the session belongs to the default agent.
        self.sessions["atlas"]["chat-1"] = dict(self.sessions["default"]["chat-1"])
        self.cron_tool("create", created(), profile="atlas")
        self.assertEqual(self.links(), [])
        self.cron_tool("create", created(), profile="atlas", session="atlas-chat")
        self.cron_tool("create", created())
        self.assertEqual(self.links(), [("atlas", JOB, "atlas-chat"), ("default", JOB, "chat-1")])
        # Removing the default agent's job leaves atlas's job of the same id linked.
        self.cron_tool("remove", removed())
        self.assertEqual(self.links(), [("atlas", JOB, "atlas-chat")])

    def test_links_are_kept_bounded(self):
        for index in range(520):
            self.now += 1
            self.cron_tool("create", created(job=f"job{index:04d}"))
        links = self.links()
        self.assertEqual(len(links), 512)
        self.assertNotIn(("default", "job0000", "chat-1"), links)
        self.assertIn(("default", "job0519", "chat-1"), links)

    # The alert

    def test_a_phone_that_opens_links_gets_the_chat_the_job_was_made_in(self):
        self.links_on()
        self.cron_tool("create", created())
        self.finish_run()
        self.finish_run(failed=True, turn="turn-f")
        alerts = sorted(self.sent(), key=lambda alert: alert["eventType"])
        self.assertEqual([alert["eventType"] for alert in alerts], ["scheduled.completed", "scheduled.failed"])
        self.assertEqual({alert["sessionReference"] for alert in alerts}, {self.chat("chat-1")})
        # The run stays the alert's identity: a retry is still one alert.
        self.assertEqual([alert["eventId"] for alert in alerts],
                         [self.run_event_id("scheduled.completed"),
                          self.run_event_id("scheduled.failed", turn="turn-f")])
        with sqlite3.connect(self.service.db_path) as db:
            stored = json.loads(db.execute("SELECT detail_json FROM events LIMIT 1").fetchone()[0])
        self.assertEqual(stored["sessionId"], RUN)

    def test_an_older_phone_keeps_the_runs_own_session(self):
        self.cron_tool("create", created())
        self.finish_run()
        self.assertEqual([alert["sessionReference"] for alert in self.sent()], [session_reference("default", RUN)])

    def test_a_job_without_a_chat_opens_the_job(self):
        self.links_on()
        self.finish_run()
        self.assertEqual([alert["sessionReference"] for alert in self.sent()], [self.job()])

    def test_a_deleted_chat_falls_back_to_the_job_and_forgets_the_link(self):
        self.links_on()
        self.cron_tool("create", created())
        del self.sessions["default"]["chat-1"]
        self.finish_run()
        self.assertEqual([alert["sessionReference"] for alert in self.sent()], [self.job()])
        self.assertEqual(self.links(), [])

    def test_an_unreadable_store_falls_back_to_the_job_and_keeps_the_link(self):
        self.links_on()
        self.cron_tool("create", created())
        self.store_available = False
        self.finish_run()
        self.assertEqual([alert["sessionReference"] for alert in self.sent()], [self.job()])
        self.assertEqual(self.links(), [("default", JOB, "chat-1")])

    def test_a_removed_job_opens_the_job(self):
        self.links_on()
        self.cron_tool("create", created())
        self.cron_tool("remove", removed())
        self.finish_run()
        self.assertEqual([alert["sessionReference"] for alert in self.sent()], [self.job()])

    def test_a_unified_runtime_run_names_its_job_through_hermes(self):
        # Hermes' unified runtime names runs `local-<sha256>`; the job comes from ctx.current_cron_execution().
        run = "local-" + "c" * 64
        self.links_on()
        self.cron_tool("create", created())
        execution = types.SimpleNamespace(job_id=JOB, job_name="Morning weather", execution_id="e1",
                                          source="builtin", scheduled_instant=None, started_at=None,
                                          profile="default")
        self.finish_run(session=run, execution=execution)
        self.assertEqual([alert["sessionReference"] for alert in self.sent()], [self.chat("chat-1")])
        # Without that API, and with a run named for no job, the alert keeps the run's session.
        self.finish_run(session=run, turn="turn-d")
        self.assertEqual([alert["sessionReference"] for alert in self.sent()], [session_reference("default", run)])

    def test_another_agents_link_is_never_used(self):
        atlas = self.enroll("atlas")
        self.links_on(atlas)
        self.links_on()
        self.cron_tool("create", created(), profile="atlas", session="atlas-chat")
        self.finish_run()
        self.assertEqual([alert["sessionReference"] for alert in self.sent()], [self.job()])
        self.finish_run(profile="atlas", turn="turn-a")
        self.assertEqual([alert["sessionReference"] for alert in self.sent()],
                         [self.chat("atlas-chat", profile="atlas")])

    def test_links_survive_a_restart(self):
        self.links_on()
        self.cron_tool("create", created())
        self.service.close()
        self.service = self.make_service()
        self.finish_run()
        self.assertEqual([alert["sessionReference"] for alert in self.sent()], [self.chat("chat-1")])

    def test_ordinary_chat_alerts_are_unchanged(self):
        self.links_on()
        self.cron_tool("create", created())
        self.service.observe("post_llm_call", profile="default", session_id="chat-1", turn_id="turn-9",
                             assistant_response="Booked.", platform="bighelp")
        self.assertEqual([alert["sessionReference"] for alert in self.sent()], [session_reference("default", "chat-1")])

    # The phone's choice

    def test_the_choice_is_advertised_validated_and_starts_off(self):
        self.assertTrue(self.service.capabilities()["preferences"]["scheduledLinks"])
        self.assertFalse(self.service.preferences(self.grant_id)["scheduledLinks"])
        self.assertTrue(self.service.set_preferences(self.grant_id, peer_chats=False,
                                                     scheduled_links=True)["scheduledLinks"])
        # A phone that leaves it out keeps what it chose.
        self.assertTrue(self.service.set_preferences(self.grant_id, peer_chats=True)["scheduledLinks"])
        with self.assertRaises(ManagedNotificationError):
            self.service.set_preferences(self.grant_id, peer_chats=False, scheduled_links="yes")

    def test_the_route_accepts_the_choice(self):
        from loopdy_plugin.managed_notifications_api import PreferencesBody
        body = PreferencesBody.model_validate({"version": 1, "peerChats": False, "scheduledLinks": True})
        self.assertTrue(body.scheduledLinks)
        self.assertIsNone(PreferencesBody.model_validate({"version": 1, "peerChats": False}).scheduledLinks)
        with self.assertRaises(ValueError):
            PreferencesBody.model_validate({"version": 1, "peerChats": False, "scheduledLinks": "yes"})


if __name__ == "__main__":
    unittest.main()
