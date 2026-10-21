from __future__ import annotations

import types
import unittest

from loopdy_plugin import durable_sessions


def fake_server() -> types.ModuleType:
    """Hermes' tui_gateway.server as far as the session lifecycle goes: Desktop's automatic cleanup keeps the
    saved row; any other app's ends it."""
    server = types.ModuleType("tui_gateway.server")
    server._AUTOMATIC_SESSION_END_REASONS = frozenset({"ws_orphan_reap", "ws_disconnect", "idle_timeout"})
    server.ended: list[tuple[str, str]] = []

    def _finalize_session(session, end_reason="tui_close"):
        keeps = end_reason in server._AUTOMATIC_SESSION_END_REASONS and session.get("source") == "desktop"
        if not keeps:
            server.ended.append((session["session_key"], end_reason))
    server._finalize_session = _finalize_session
    return server


class DurableSessionsTests(unittest.TestCase):
    def test_a_dropped_connection_keeps_a_bighelp_chat_as_it_keeps_a_desktop_one(self):
        server = fake_server()
        self.assertTrue(durable_sessions.install(server))
        chat = {"session_key": "chat", "source": "bighelp"}
        server._finalize_session(chat, "ws_orphan_reap")
        self.assertEqual(server.ended, [], "The app went away: the chat stays open and resumable")
        self.assertEqual(chat["source"], "bighelp", "The session keeps its own source")

        server._finalize_session({"session_key": "closed", "source": "bighelp"}, "tui_close")
        server._finalize_session({"session_key": "terminal", "source": "tui"}, "ws_orphan_reap")
        self.assertEqual(server.ended, [("closed", "tui_close"), ("terminal", "ws_orphan_reap")],
                         "Closing a chat, and other apps' sessions, are unchanged")

    def test_installs_once_and_only_where_the_lifecycle_is_known(self):
        server = fake_server()
        self.assertTrue(durable_sessions.install(server))
        wrapped = server._finalize_session
        self.assertTrue(durable_sessions.install(server))
        self.assertIs(server._finalize_session, wrapped)
        self.assertFalse(durable_sessions.install(types.ModuleType("tui_gateway.server")),
                         "A Hermes without that lifecycle is left as it is")


class ReopenTests(unittest.TestCase):
    def test_chats_ended_only_by_a_dropped_connection_reopen_and_nothing_else(self):
        class Store:
            def __init__(self):
                self.rows = {"reaped": ("bighelp", "ws_orphan_reap"), "closed": ("bighelp", "tui_close"),
                             "terminal": ("tui", "ws_orphan_reap"), "open": ("bighelp", None)}
                self.reopened = []

            def _read_all(self, sql, params):
                source, *reasons = params
                return [{"id": id} for id, (row_source, reason) in self.rows.items()
                        if row_source == source and reason in reasons]

            def reopen_session(self, session_id):
                self.reopened.append(session_id)

        store = Store()
        self.assertEqual(durable_sessions.reopen_automatically_ended([store]), 1)
        self.assertEqual(store.reopened, ["reaped"])


class _Agent:
    def __init__(self, *owners: str):
        self._process_owner_task_ids = set(owners)


def _process(owner: str, *, status: str = "running", notify: bool = True) -> dict:
    """One row of Hermes' process_registry.list_sessions()."""
    row = {"session_id": f"proc_{owner}", "owner_task_id": owner, "status": status}
    if notify:
        row["notify_on_complete"] = True
    return row


class WaitingChatTests(unittest.TestCase):
    """message_agent hands a teammate's turn to a background process and ends the asking turn. Once the app's
    connection has been gone ws_orphan_reap_grace_s, Hermes reclaimed that idle chat: closing its agent killed
    the teammate's process (exit -15) and stopped the poller that delivers the reply."""

    def setUp(self):
        self.processes: list[dict] = []
        original = durable_sessions._running_processes
        durable_sessions._running_processes = lambda: list(self.processes)
        self.addCleanup(setattr, durable_sessions, "_running_processes", original)

    def server(self) -> types.ModuleType:
        server = fake_server()
        server._session_has_active_delegations = lambda sid, session=None: False
        self.assertTrue(durable_sessions.install(server))
        return server

    def test_a_bighelp_chat_waiting_on_background_work_stays_until_it_ends(self):
        server = self.server()
        chat = {"source": "bighelp", "agent": _Agent("turn-1")}
        self.processes = [_process("turn-1")]
        self.assertTrue(server._session_has_active_delegations("sid", chat),
                        "A teammate's delivery is still running: the chat keeps its runtime")
        self.processes = [_process("turn-1", status="exited")]
        self.assertFalse(server._session_has_active_delegations("sid", chat), "Once it ends, the chat is reclaimed")

    def test_only_work_that_reports_back_to_this_chat_counts(self):
        server = self.server()
        chat = {"source": "bighelp", "agent": _Agent("turn-1")}
        self.processes = [_process("turn-1", notify=False), _process("other-turn")]
        self.assertFalse(server._session_has_active_delegations("sid", chat),
                         "A server left running, or another chat's job, doesn't hold this chat")

    def test_other_apps_sessions_and_hermes_own_answer_are_unchanged(self):
        server = fake_server()
        server._session_has_active_delegations = lambda sid, session=None: sid == "delegating"
        self.assertTrue(durable_sessions.install(server))
        self.processes = [_process("turn-1")]
        self.assertFalse(server._session_has_active_delegations("sid", {"source": "desktop", "agent": _Agent("turn-1")}))
        self.assertTrue(server._session_has_active_delegations("delegating", {"source": "tui"}))
        self.assertFalse(server._session_has_active_delegations("sid", None))

    def test_an_unreadable_registry_never_holds_a_chat(self):
        server = self.server()

        def broken():
            raise RuntimeError("registry moved")
        durable_sessions._running_processes = broken
        self.assertFalse(server._session_has_active_delegations("sid", {"source": "bighelp", "agent": _Agent("t")}))

    def test_installs_once_and_leaves_a_hermes_without_that_check_alone(self):
        server = self.server()
        wrapped = server._session_has_active_delegations
        self.assertTrue(durable_sessions.install(server))
        self.assertIs(server._session_has_active_delegations, wrapped)
        plain = fake_server()
        self.assertTrue(durable_sessions.install(plain), "The saved-chat half still installs")
        self.assertFalse(hasattr(plain, "_session_has_active_delegations"))


class RealReaperTests(unittest.TestCase):
    """Hermes' own ws_orphan_reap timer, with a grace of milliseconds, against a detached bighelp chat."""

    def setUp(self):
        try:
            from tui_gateway import server
        except Exception as error:  # pragma: no cover - only without a Hermes checkout on the path
            self.skipTest(f"Hermes' tui_gateway is unavailable: {type(error).__name__}")
        if not hasattr(server, "_schedule_ws_orphan_reap"):
            self.skipTest("This Hermes has no ws_orphan_reap timer")
        self.server = server
        self.processes: list[dict] = []
        original = durable_sessions._running_processes
        durable_sessions._running_processes = lambda: list(self.processes)
        self.addCleanup(setattr, durable_sessions, "_running_processes", original)
        self.reaped: list[str] = []
        for name, value in (
            ("_WS_ORPHAN_REAP_GRACE_S", 0.05),
            ("_teardown_popped_session", lambda session, end_reason="tui_close": self.reaped.append(end_reason)),
            ("_session_owns_live_wakeup_schedule", lambda session: False),
        ):
            self.addCleanup(setattr, server, name, getattr(server, name))
            setattr(server, name, value)
        self.assertTrue(durable_sessions.install(server))

    def detached_chat(self, sid: str) -> dict:
        import threading
        chat = {"source": "bighelp", "running": False, "agent": _Agent("turn-1"),
                "transport": self.server._detached_ws_transport, "history_lock": threading.Lock()}
        with self.server._sessions_lock:
            self.server._sessions[sid] = chat
        self.addCleanup(self.server._sessions.pop, sid, None)
        self.addCleanup(self.server._cancel_ws_orphan_reap, sid)
        return chat

    def wait(self, condition, seconds: float = 2.0) -> bool:
        import time
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return condition()

    def test_the_real_reaper_waits_for_the_teammate_then_reclaims_the_chat(self):
        sid = "loopdy-test-waiting-chat"
        self.detached_chat(sid)
        self.processes = [_process("turn-1")]
        self.server._schedule_ws_orphan_reap(sid)
        self.assertFalse(self.wait(lambda: self.reaped, 0.4), "Several graces pass; the chat is still there")
        self.assertIn(sid, self.server._sessions)
        self.processes = [_process("turn-1", status="exited")]
        self.assertTrue(self.wait(lambda: self.reaped), "With the work done, the reaper reclaims it as before")
        self.assertEqual(self.reaped, ["ws_orphan_reap"])
        self.assertNotIn(sid, self.server._sessions)

    def test_an_idle_bighelp_chat_with_nothing_running_is_reclaimed_as_before(self):
        sid = "loopdy-test-idle-chat"
        self.detached_chat(sid)
        self.server._schedule_ws_orphan_reap(sid)
        self.assertTrue(self.wait(lambda: self.reaped))


if __name__ == "__main__":
    unittest.main()
