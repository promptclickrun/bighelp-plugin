from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from loopdy_plugin import agent_talk
from loopdy_plugin.agent_talk import find_runs, message_hash
import test_native_api as native_fixtures
from test_native_api import PREFIX


# The app's tests use the same vector (`ChatAgentTalkTests.messageHashMatchesThePluginsNormalization`).
MONDAY_HASH = "3f8b2abb9596992352771eb54daf4895b0b8beca852321f474bca45b96e49a87"
NOW = 1_760_000_000.0


class AgentHistory:
    """A profile's `state.db` with just the columns Hermes' history has and this reads."""

    def __init__(self, path: Path):
        self.path = path
        self.next_id = 1
        with sqlite3.connect(path) as db:
            db.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
                started_at REAL NOT NULL, ended_at REAL, archived INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
                timestamp REAL NOT NULL)""")

    def session(self, session_id, started, *, title=None, ended=None):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO sessions (id, source, title, started_at, ended_at) VALUES (?, 'cli', ?, ?, ?)",
                       (session_id, title, started, ended))

    def message(self, session_id, role, at, content=None, *, tool=None, result_for=None):
        calls = json.dumps([{"id": tool[0], "type": "function",
                             "function": {"name": tool[1], "arguments": "{}"}}]) if tool else None
        with sqlite3.connect(self.path) as db:
            db.execute("""INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, timestamp)
                VALUES (?, ?, ?, ?, ?, ?)""", (session_id, role, content, calls, result_for, at))


class FindRunsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.paths = {name: Path(folder.name) / f"{name}.db" for name in ("nova", "havok")}
        self.nova = AgentHistory(self.paths["nova"])
        self.havok = AgentHistory(self.paths["havok"])
        patcher = patch.object(agent_talk, "_state_db", side_effect=lambda profile: self.paths.get(profile))
        patcher.start()
        self.addCleanup(patcher.stop)

    def runs(self, *calls):
        return find_runs(calls, now=lambda: NOW)

    def test_a_broken_hermes_turns_the_feature_off_instead_of_failing(self):
        """`available()` feeds /native/context, which every app reads: it may say no, never raise."""
        import builtins
        real_import = builtins.__import__

        def broken(name, *args, **kwargs):
            if name == "hermes_cli.profiles":
                raise RuntimeError("half-updated Hermes")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=broken):
            self.assertFalse(agent_talk.available())

    def test_hash_matches_the_app_without_the_signature(self):
        self.assertEqual(message_hash("Is Monday free?"), MONDAY_HASH)
        self.assertEqual(message_hash("Message from \U0001F916 Avery (@hermes):  Is Monday free?\n"), MONDAY_HASH)
        # A comma runs into the words, so they stay whole on both sides.
        self.assertNotEqual(message_hash("Message from Avery (@default), Is Monday free?"), MONDAY_HASH)

    def test_a_one_shot_run_says_what_the_agent_is_running(self):
        self.nova.session("20261017_170115_941fc0", NOW - 30)
        self.nova.message("20261017_170115_941fc0", "user", NOW - 30, "Is Monday free?")
        self.nova.message("20261017_170115_941fc0", "assistant", NOW - 25, tool=("call_1", "web_search"))
        self.assertEqual(self.runs(("nova", MONDAY_HASH)), [{
            "agentId": "nova", "sessionId": "20261017_170115_941fc0", "message": MONDAY_HASH,
            "startedAt": NOW - 30, "working": True, "tool": "web_search"}])

        # Its result is in: still working, no tool.
        self.nova.message("20261017_170115_941fc0", "tool", NOW - 20, "{}", result_for="call_1")
        self.assertEqual(self.runs(("nova", MONDAY_HASH))[0]["tool"], None)
        self.assertTrue(self.runs(("nova", MONDAY_HASH))[0]["working"])
        # Answered.
        self.nova.message("20261017_170115_941fc0", "assistant", NOW - 10, "Monday is free after 2 PM.")
        self.assertFalse(self.runs(("nova", MONDAY_HASH))[0]["working"])

    def test_message_agent_delivery_is_found_in_the_bot_chat(self):
        self.havok.session("20261009_035506_d75b61", NOW - 9 * 24 * 3600, title="Bot Chat")
        self.havok.message("20261009_035506_d75b61", "user", NOW - 600, "Something the person asked")
        self.havok.message("20261009_035506_d75b61", "assistant", NOW - 590, "An answer")
        self.havok.message("20261009_035506_d75b61", "user", NOW - 20,
                           "Message from \U0001F916 Avery (@hermes): Is Monday free?")
        self.havok.message("20261009_035506_d75b61", "assistant", NOW - 15, tool=("call_2", "terminal"))
        runs = self.runs(("havok", MONDAY_HASH))
        self.assertEqual([(run["sessionId"], run["working"], run["tool"]) for run in runs],
                         [("20261009_035506_d75b61", True, "terminal")])
        # The person's next message ends that turn, even without an answer.
        self.havok.message("20261009_035506_d75b61", "user", NOW - 5, "Thanks")
        self.assertFalse(self.runs(("havok", MONDAY_HASH))[0]["working"])

    def test_stopped_or_ended_runs_are_not_working(self):
        self.nova.session("s-ended", NOW - 60, ended=NOW - 40)
        self.nova.message("s-ended", "user", NOW - 60, "Is Monday free?")
        self.nova.message("s-ended", "assistant", NOW - 50, tool=("call_3", "terminal"))
        self.nova.session("s-stale", NOW - 7200)
        self.nova.message("s-stale", "user", NOW - 7200, "Is Monday free?")
        runs = self.runs(("nova", MONDAY_HASH))
        self.assertEqual([(run["sessionId"], run["working"], run["tool"]) for run in runs],
                         [("s-ended", False, None), ("s-stale", False, None)])

    def test_only_matching_recent_runs_of_local_agents(self):
        self.nova.session("s-other", NOW - 30)
        self.nova.message("s-other", "user", NOW - 30, "Something else")
        self.nova.session("s-old", NOW - agent_talk.WINDOW_SECONDS - 10)
        self.nova.message("s-old", "user", NOW - agent_talk.WINDOW_SECONDS - 10, "Is Monday free?")
        self.assertEqual(self.runs(("nova", MONDAY_HASH)), [])
        self.assertEqual(self.runs(("sage", MONDAY_HASH)), [], "Not a profile here")
        self.assertEqual(self.runs(("spark/rio", MONDAY_HASH), ("nova", "not-a-hash")), [])

    def test_the_history_is_only_read(self):
        self.nova.session("s1", NOW - 30)
        self.nova.message("s1", "user", NOW - 30, "Is Monday free?")
        before = self.paths["nova"].read_bytes()
        self.runs(("nova", MONDAY_HASH))
        self.assertEqual(self.paths["nova"].read_bytes(), before)
        self.assertFalse(Path(str(self.paths["nova"]) + "-wal").exists())


class AgentTalkRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "research.db"
        history = AgentHistory(path)
        history.session("20261017_170115_941fc0", 1_000)
        history.message("20261017_170115_941fc0", "user", 1_000, "Is Monday free?")
        patcher = patch.object(agent_talk, "_state_db", side_effect=lambda profile: path if profile == "research" else None)
        patcher.start()
        self.addCleanup(patcher.stop)
        clock = patch.object(agent_talk.time, "time", return_value=1_030.0)
        clock.start()
        self.addCleanup(clock.stop)

    def call(self, body, headers=None):
        return self.client.post(PREFIX + "/agents/talk", json=body,
                                headers=self.fixture.headers() if headers is None else headers)

    def test_runs_come_back_without_words(self):
        result = self.call({"agentId": "default", "sessionId": "20261017_170034_98063e",
                            "calls": [{"agentId": "research", "message": MONDAY_HASH}]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(result.json(), {"runs": [{
            "agentId": "research", "sessionId": "20261017_170115_941fc0", "message": MONDAY_HASH,
            "startedAt": 1000.0, "working": True, "tool": None}]})
        self.assertNotIn("Monday", result.text)

    def test_bad_requests_are_refused(self):
        call = {"agentId": "research", "message": MONDAY_HASH}
        for body in ({"agentId": "default", "sessionId": "s1", "calls": [{"agentId": "research", "message": "x"}]},
                     {"agentId": "Default!", "sessionId": "s1", "calls": []},
                     {"agentId": "default", "sessionId": "s1", "calls": [call] * 17},
                     {"agentId": "default", "sessionId": "s1", "calls": [dict(call, words="Is Monday free?")]},
                     {"agentId": "default", "sessionId": "s1"}):
            self.assertEqual(self.call(body).status_code, 422, body)
        self.assertEqual(self.call({"agentId": "gone", "sessionId": "s1", "calls": []}).status_code, 404)
        headers = self.fixture.headers()
        headers["If-Match"] = '"sha256:' + "0" * 64 + '"'
        self.assertEqual(self.call({"agentId": "default", "sessionId": "s1", "calls": []}, headers).status_code, 412)

    def test_feature_is_listed(self):
        self.assertIn(agent_talk.CAPABILITY, self.fixture.context().json()["features"])


if __name__ == "__main__":
    unittest.main()
