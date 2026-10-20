"""A chat busy in another Hermes process: Hermes' lease file says who holds it, Hermes' own liveness
check says that process is still the one that took it, and the agent's history says what it's doing.
A real child process stands in for `hermes -p <agent> chat`, so Stop is a real Ctrl-C.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from loopdy_plugin import chat_activity
import test_native_api as native_fixtures
from test_agent_talk import AgentHistory
from test_native_api import PREFIX

SESSION = "20261018_020300_b0b0b0"


@unittest.skipUnless(chat_activity.available(), "this Hermes has no chat leases")
class ChatActivityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        (self.home / "runtime").mkdir()
        self.history = AgentHistory(self.home / "state.db")
        self.history.session(SESSION, 100.0, title="Bot Chat")
        self.history.message(SESSION, "user", 100.0, "Message from Alfie (@default): Check the PRs.")
        self.history.message(SESSION, "assistant", 110.0, "Done.")
        patcher = patch.object(chat_activity, "_home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        # As `hermes chat -Q -q` would be: alive until the turn ends, and Ctrl-C ends it.
        self.run_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.end_process)
        from hermes_cli.active_sessions import _process_start_time
        self.started = _process_start_time(self.run_process.pid)

    def end_process(self):
        if self.run_process.poll() is None:
            self.run_process.kill()
        self.run_process.wait(timeout=5)

    def lease(self, *, surface="cli", pid=None, start="real", started_at=200.0):
        entry = {"lease_id": "l1", "session_id": SESSION, "surface": surface,
                 "pid": self.run_process.pid if pid is None else pid, "started_at": started_at,
                 "updated_at": started_at, "metadata": {}}
        if start is not None:
            entry["process_start_time"] = self.started if start == "real" else start
        (self.home / "runtime" / "active_sessions.json").write_text(json.dumps({"entries": [entry]}))

    def test_a_run_in_another_process_is_working_and_can_be_stopped(self):
        if self.started is None:
            self.skipTest("this machine can't read process start times")
        self.lease()
        value = chat_activity.activity("bob", SESSION, now=lambda: 210.0)
        self.assertEqual(value, {"working": True, "tool": None, "since": 200.0, "canStop": True},
                         "It took the chat after its last message: its own message isn't saved yet")

        self.history.message(SESSION, "user", 205.0, "Message from Alfie (@default): And #25?")
        self.history.message(SESSION, "assistant", 206.0, tool=("call_1", "web_search"))
        value = chat_activity.activity("bob", SESSION, now=lambda: 210.0)
        self.assertEqual((value["working"], value["tool"]), (True, "web_search"))

        self.assertTrue(chat_activity.stop("bob", SESSION))
        self.assertIsNotNone(self.run_process.wait(timeout=5), "Ctrl-C ends the run")
        self.assertFalse(chat_activity.activity("bob", SESSION, now=lambda: 211.0)["working"])

    def test_the_apps_own_chat_and_idle_or_gone_processes_are_not_busy(self):
        self.lease(surface="tui")
        self.assertFalse(chat_activity.activity("bob", SESSION, now=lambda: 210.0)["working"],
                         "The dashboard's own chat is the app's chat")
        self.lease(started_at=50.0)
        self.assertFalse(chat_activity.activity("bob", SESSION, now=lambda: 210.0)["working"],
                         "An open CLI chat whose last turn ended")
        self.lease(start=12.0)
        self.assertFalse(chat_activity.activity("bob", SESSION, now=lambda: 210.0)["working"],
                         "A reused pid isn't the process that took the chat")
        self.assertFalse(chat_activity.stop("bob", SESSION))
        self.assertIsNone(self.run_process.poll(), "Nothing was signalled")
        self.end_process()
        self.lease()
        self.assertFalse(chat_activity.activity("bob", SESSION, now=lambda: 210.0)["working"], "Gone")

    def test_without_a_start_time_it_shows_busy_but_never_signals(self):
        self.lease(start=None)
        value = chat_activity.activity("bob", SESSION, now=lambda: 210.0)
        self.assertTrue(value["working"])
        self.assertFalse(value["canStop"])
        self.assertFalse(chat_activity.stop("bob", SESSION))
        self.assertIsNone(self.run_process.poll())


class ChatActivityRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def call(self, path, body):
        return self.fixture.client.post(PREFIX + path, json=body, headers=self.fixture.headers())

    def test_routes_answer_and_refuse_bad_requests(self):
        if chat_activity.CAPABILITY not in self.fixture.context().json()["features"]:
            self.skipTest("this Hermes has no chat leases")
        with patch.object(chat_activity, "activity", return_value={"working": False, "tool": None,
                                                                   "since": None, "canStop": False}):
            result = self.call("/chat/activity", {"agentId": "default", "sessionId": SESSION})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertFalse(result.json()["working"])
        with patch.object(chat_activity, "stop", return_value=False):
            self.assertEqual(self.call("/chat/stop", {"agentId": "default", "sessionId": SESSION}).json(),
                             {"stopped": False})
        for body in ({"agentId": "default"}, {"agentId": "default", "sessionId": "../etc"},
                     {"agentId": "default", "sessionId": SESSION, "pid": 1}):
            self.assertEqual(self.call("/chat/activity", body).status_code, 422, body)
        self.assertEqual(self.call("/chat/activity", {"agentId": "ghost", "sessionId": SESSION}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
