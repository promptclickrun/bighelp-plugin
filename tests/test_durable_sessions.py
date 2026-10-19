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


if __name__ == "__main__":
    unittest.main()
