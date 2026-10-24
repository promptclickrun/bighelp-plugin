from __future__ import annotations
import telemetry_off  # noqa: F401  (no test run posts host telemetry)

import unittest

from loopdy_plugin.hooks import normalize_hook


class HomeWorkCompletionTests(unittest.TestCase):
    def test_user_completion_retains_identity_without_copying_reply(self):
        event = normalize_hook(
            "on_session_end", profile="default", platform="loopdy",
            session_id="chat-work", turn_id="turn-work", completed=True,
            failed=False, assistant_response="PRIVATE REPLY MUST NOT LEAK",
        )
        self.assertIsNotNone(event, "Home needs a durable completed user-turn record")
        self.assertEqual(event.type, "session.completed")
        self.assertEqual(event.session_id, "chat-work")
        self.assertNotIn("PRIVATE REPLY", repr(event.detail))

    def test_registered_completion_persists_across_reopen_without_push(self):
        import tempfile
        from pathlib import Path
        from loopdy_plugin.registration import register
        from loopdy_plugin.store import BighelpStore
        if __package__:
            from .test_registration import _Context, _Service
        else:
            from test_registration import _Context, _Service

        with tempfile.TemporaryDirectory() as directory:
            store = BighelpStore(Path(directory) / "events.sqlite3")
            service, context = _Service(), _Context()
            service.store = store
            register(context, service=service)
            payload = dict(platform="loopdy", session_id="stored-work", turn_id="turn-work",
                           profile_name="personal", completed=True, failed=False)
            context.hooks["on_session_end"](**payload)
            context.hooks["on_session_end"](**payload)
            reopened = BighelpStore(Path(directory) / "events.sqlite3")
            rows = [row for row in reopened.list_events() if row["type"] == "session.completed"]
            self.assertEqual(len(rows), 1, "Repeated observer delivery must not duplicate a run")
            self.assertEqual(rows[0]["profile"], "personal")
            self.assertEqual(rows[0]["session_id"], "stored-work")
            self.assertFalse(any(getattr(event, "type", None) == "session.completed"
                                 for event, _ in service.events), "Home records must not add push notifications")

    def test_stop_recovers_purpose_from_its_exact_durable_start(self):
        import tempfile
        from pathlib import Path
        from loopdy_plugin.registration import register
        from loopdy_plugin.store import BighelpStore
        if __package__:
            from .test_registration import _Context, _Service
        else:
            from test_registration import _Context, _Service

        with tempfile.TemporaryDirectory() as directory:
            service, context = _Service(), _Context()
            service.store = BighelpStore(Path(directory) / "events.sqlite3")
            service.enqueue = lambda event, target: service.store.record_event(event, target=target)
            register(context, service=service)
            context.hooks["subagent_start"](parent_session_id="parent-work", child_session_id="child-work",
                child_subagent_id="worker-id", child_goal="Review accessibility\nPRIVATE EXTRA BRIEF")
            context.hooks["subagent_stop"](parent_session_id="parent-work", child_session_id="child-work",
                child_status="completed", child_summary="PRIVATE COMPLETE REPLY")
            rows = [row for row in service.store.list_events() if row["type"] == "delegation.completed"]
            self.assertEqual(len(rows), 1)
            wire = rows[0]
            self.assertEqual(wire["detail"]["title"], "Review accessibility")
            self.assertEqual(wire["detail"]["child_session_id"], "child-work")
            self.assertEqual(wire["detail"]["delegation_id"], "worker-id")
            self.assertNotIn("PRIVATE", repr(wire))

    def test_cron_runs_keep_distinct_identity_and_normalized_failure_is_not_success(self):
        first = normalize_hook("on_session_end", profile="default", platform="cron",
            session_id="cron_job-one_20260906_120000", completed=True)
        second = normalize_hook("on_session_end", profile="default", platform="cron",
            session_id="cron_job-one_20260906_130000", completed=True)
        self.assertNotEqual(first.event_id, second.event_id)
        self.assertEqual(first.job_id, second.job_id)
        for fields in [dict(completed=False), dict(completed=True, interrupted=True),
                       dict(completed=True, failed=True), dict(completed=True, platform="subagent")]:
            payload = dict(platform="loopdy", session_id="work", turn_id="turn") | fields
            event = normalize_hook("on_session_end", profile="default", **payload)
            self.assertTrue(event is None or event.type != "session.completed")

    def test_child_completion_keeps_purpose_and_child_coordinate_on_wire(self):
        event = normalize_hook(
            "subagent_stop", profile="default", parent_session_id="parent-work",
            child_session_id="child-work", child_subagent_id="child-agent",
            child_status="completed", child_role="leaf",
            child_goal="Review the Home activity transitions",
            child_summary="The transitions are correct.",
        )
        self.assertEqual(event.type, "delegation.completed")
        projected = {"detail": event.detail}
        self.assertEqual(projected["detail"].get("child_session_id"), "child-work")
        self.assertEqual(projected["detail"].get("title"), "Review the Home activity transitions")
        self.assertNotEqual(projected["detail"]["title"], "leaf")


if __name__ == "__main__":
    unittest.main()
