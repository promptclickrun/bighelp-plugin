"""Each agent's Bot Chat state for the app's home. Each agent's `state.db` is Hermes' own (its
SessionDB, its turn leases), a real child process stands in for `hermes -p <agent> chat -q`, and the
dashboard's open chats and the approvals they wait on are Hermes' own structures, as the dashboard
fills them.
"""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from loopdy_plugin import agents_activity
import test_native_api as native_fixtures
from test_native_api import PREFIX

BOT_CHAT = "20261021_090000_b0b0b0"
QUERY_CHAT = "20261021_091500_c1c1c1"
OTHER_CHAT = "20261021_092000_d2d2d2"


def hermes_available() -> bool:
    try:
        from hermes_state import SessionDB  # noqa: F401
    except Exception:
        return False
    return True


class Agent:
    """One profile's Hermes home with its own state.db, written by Hermes."""

    def __init__(self, root: Path, name: str):
        from hermes_state import SessionDB
        self.home = root / name
        self.home.mkdir()
        self.db = SessionDB(db_path=self.home / "state.db")

    def close(self):
        self.db.close()

    def chat(self, session_id, *, source="bighelp", title=None, parent=None):
        self.db.create_session(session_id, source, parent_session_id=parent)
        if title:
            self.db.set_session_title(session_id, title)

    def say(self, session_id, role, content=None, *, tool=None, result_for=None):
        calls = [{"id": tool[0], "type": "function", "function": {"name": tool[1], "arguments": "{}"}}] if tool else None
        self.db.append_message(session_id, role, content, tool_calls=calls, tool_call_id=result_for,
                               tool_name=tool[1] if tool else None)

    def turn(self, session_id, *, pid=None, platform="bighelp"):
        holder = f"pid={pid or os.getpid()}:turn=t-{session_id[-6:]}:platform={platform}"
        assert self.db.try_acquire_session_turn_lease(session_id, holder)
        return holder

    def end_turn(self, session_id, holder):
        self.db.release_session_turn_lease(session_id, holder)


@unittest.skipUnless(hermes_available(), "this Hermes has no session store")
class AgentsActivityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.agents: dict[str, Agent] = {}
        homes = patch.object(agents_activity, "_home", side_effect=lambda name: self.agents[name].home
                             if name in self.agents else None)
        homes.start()
        self.addCleanup(homes.stop)
        agents_activity.forget()
        self.addCleanup(agents_activity.forget)
        self.nova = self.agent("nova")
        self.nova.chat(BOT_CHAT, title="Bot Chat")
        self.nova.say(BOT_CHAT, "user", "Message from Alfie (@default): Check the PRs.")
        self.nova.say(BOT_CHAT, "assistant", "Done.")

    def agent(self, name):
        agent = Agent(self.root, name)
        self.addCleanup(agent.close)
        self.agents[name] = agent
        return agent

    def states(self, *names):
        agents_activity.forget()
        return {row["agentId"]: (row["state"], row.get("tool")) for row in agents_activity.activity(names)}

    def child(self):
        # As `hermes -p nova chat -q` would be: alive until its turn ends.
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

        def end():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(end)
        return process, end

    def test_a_turn_sent_from_the_app_is_working_with_its_tool_and_clears_when_it_ends(self):
        self.assertEqual(self.states("nova"), {"nova": ("idle", None)})
        self.nova.say(BOT_CHAT, "user", "Find me a flight.")
        holder = self.nova.turn(BOT_CHAT)
        self.assertEqual(self.states("nova"), {"nova": ("working", None)}, "Thinking, no tool yet")
        self.nova.say(BOT_CHAT, "assistant", tool=("call_1", "web_search"))
        self.assertEqual(self.states("nova"), {"nova": ("working", "web_search")})
        self.nova.say(BOT_CHAT, "tool", "results", result_for="call_1")
        self.assertEqual(self.states("nova"), {"nova": ("working", None)}, "The search came back")
        self.nova.say(BOT_CHAT, "assistant", "Here are three flights.")
        self.nova.end_turn(BOT_CHAT, holder)
        self.assertEqual(self.states("nova"), {"nova": ("idle", None)}, "The turn ended")

    def test_a_turn_in_another_process_counts_until_that_process_is_gone(self):
        process, end = self.child()
        self.nova.say(BOT_CHAT, "user", "Message from Alfie (@default): And #25?")
        self.nova.turn(BOT_CHAT, pid=process.pid, platform="cli")
        self.nova.say(BOT_CHAT, "assistant", tool=("call_2", "terminal"))
        self.assertEqual(self.states("nova"), {"nova": ("working", "terminal")},
                         "message_agent's background run in the Bot Chat")
        end()
        self.assertEqual(self.states("nova"), {"nova": ("idle", None)},
                         "Its process died mid-turn: the lease it left isn't a turn")

    def test_a_chat_q_run_in_its_own_chat_counts_and_unrelated_chats_dont(self):
        process, _ = self.child()
        self.nova.chat(QUERY_CHAT, source="cli")
        self.nova.say(QUERY_CHAT, "user", "Summarize the release notes.")
        self.nova.say(QUERY_CHAT, "assistant", tool=("call_3", "read_file"))
        self.nova.turn(QUERY_CHAT, pid=process.pid, platform="cli")
        self.assertEqual(self.states("nova"), {"nova": ("working", "read_file")})

        self.nova.end_turn(QUERY_CHAT, f"pid={process.pid}:turn=t-{QUERY_CHAT[-6:]}:platform=cli")
        self.nova.chat(OTHER_CHAT, source="telegram")
        self.nova.turn(OTHER_CHAT, platform="telegram")
        self.assertEqual(self.states("nova"), {"nova": ("idle", None)}, "A messaging app's chat isn't the Bot Chat")
        self.nova.end_turn(OTHER_CHAT, f"pid={os.getpid()}:turn=t-{OTHER_CHAT[-6:]}:platform=telegram")
        self.nova.db.create_session("20261021_093000_e3e3e3", "bighelp")
        self.nova.turn("20261021_093000_e3e3e3")
        self.assertEqual(self.states("nova"), {"nova": ("idle", None)}, "The app's other chats don't count")

    def test_a_compressed_bot_chat_keeps_its_lease_on_the_first_chat(self):
        tip = "20261021_100000_f4f4f4"
        self.nova.db.end_session(BOT_CHAT, "compression")
        self.nova.db.set_session_title(BOT_CHAT, "")
        self.nova.chat(tip, parent=BOT_CHAT, title="Bot Chat")
        self.nova.say(tip, "user", "Keep going.")
        self.nova.say(tip, "assistant", tool=("call_4", "browser_navigate"))
        self.nova.turn(tip)
        with closing(sqlite3.connect(self.nova.home / "state.db")) as db:
            keys = [row[0] for row in db.execute("SELECT conversation_id FROM session_turn_leases")]
        self.assertEqual(keys, [BOT_CHAT], "Hermes keeps the lease under the chain's first chat")
        self.assertEqual(self.states("nova"), {"nova": ("working", "browser_navigate")})

    def test_waiting_on_the_person_wins_over_working(self):
        import tui_gateway.server as server
        import tui_gateway.server_requests as requests
        sid = "live-nova-1"
        with server._sessions_lock:
            server._sessions[sid] = {"session_key": BOT_CHAT, "running": True}
        self.addCleanup(lambda: server._sessions.pop(sid, None))
        self.nova.turn(BOT_CHAT)
        self.assertEqual(self.states("nova"), {"nova": ("working", None)})

        terminal = requests.send_async("terminal.read", sid, {"start": 0, "count": 10}, lambda result: None)
        self.addCleanup(terminal, "test")
        self.assertEqual(self.states("nova"), {"nova": ("working", None)}, "Reading a terminal isn't the person")

        settle = requests.send_async("approval", sid, {"request_id": "approval-1", "command": "rm -rf build",
                                                       "description": "Delete the build folder"}, lambda result: None)
        self.addCleanup(settle, "test")
        self.assertEqual(self.states("nova"), {"nova": ("waiting", None)})
        settle("resolved")
        self.assertEqual(self.states("nova"), {"nova": ("working", None)}, "Answered: back to work")

        with server._sessions_lock:
            server._sessions[sid]["_compute_host_open_request"] = {"id": "srq-1", "method": "clarify", "params": {}}
        self.assertEqual(self.states("nova"), {"nova": ("waiting", None)}, "A question from an isolated turn")

    def test_unknown_missing_and_unreadable_agents(self):
        fresh = self.root / "fresh"
        fresh.mkdir()
        self.agents["fresh"] = type("Fresh", (), {"home": fresh})()
        self.assertEqual(self.states("ghost", "nova", "fresh", "BAD id", "nova"),
                         {"nova": ("idle", None), "fresh": ("idle", None)},
                         "Not a profile, or not an ID: left out. No history yet: nothing has run.")
        rows = agents_activity.activity(["fresh", "nova"])
        self.assertEqual([row["agentId"] for row in rows], ["fresh", "nova"], "In the order asked")
        with patch.object(agents_activity.sqlite3, "connect", side_effect=sqlite3.OperationalError("locked")):
            agents_activity.forget()
            self.assertEqual(agents_activity.activity(["nova"]), [], "Can't read it now: unknown, not idle")

    def test_an_older_hermes_without_turn_leases_uses_the_dashboard_and_chat_leases(self):
        with closing(sqlite3.connect(self.nova.home / "state.db")) as db:
            db.execute("DROP TABLE session_turn_leases")
            db.commit()
        self.assertEqual(self.states("nova"), {"nova": ("idle", None)})
        import tui_gateway.server as server
        with server._sessions_lock:
            server._sessions["live-nova-2"] = {"session_key": BOT_CHAT, "running": True}
        self.addCleanup(lambda: server._sessions.pop("live-nova-2", None))
        self.assertEqual(self.states("nova"), {"nova": ("working", None)})

    def test_at_most_64_agents_and_a_short_cache(self):
        names = [f"a{index}" for index in range(70)]
        for name in names[:66]:
            home = self.root / name
            home.mkdir()
            self.agents[name] = type("Home", (), {"home": home})()
        agents_activity.forget()
        self.assertEqual(len(agents_activity.activity(names)), 64)
        self.nova.say(BOT_CHAT, "user", "Again.")
        agents_activity.forget()
        clock = [1_000.0]
        first = agents_activity.activity(["nova"], now=lambda: clock[0])
        holder = self.nova.turn(BOT_CHAT)
        self.addCleanup(self.nova.end_turn, BOT_CHAT, holder)
        clock[0] += 0.5
        self.assertEqual(agents_activity.activity(["nova"], now=lambda: clock[0]), first, "Asked again within a second")


class AgentsActivityRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def call(self, body):
        return self.fixture.client.post(PREFIX + "/agents/activity", json=body, headers=self.fixture.headers())

    def test_route_answers_and_refuses_bad_requests(self):
        self.assertIn(agents_activity.CAPABILITY, self.fixture.context().json()["features"])
        with patch.object(agents_activity, "activity", return_value=[{"agentId": "default", "state": "working",
                                                                      "tool": "web_search"}]) as read:
            result = self.call({"agentIds": ["default", "nova"]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"agents": [{"agentId": "default", "state": "working", "tool": "web_search"}]})
        self.assertEqual(result.headers["cache-control"], "no-store")
        read.assert_called_once_with(["default", "nova"])
        for body in ({}, {"agentIds": []}, {"agentIds": ["../etc"]}, {"agentIds": ["default"], "sessionId": "x"},
                     {"agentIds": [f"a{index}" for index in range(65)]}, {"agentIds": "default"}):
            self.assertEqual(self.call(body).status_code, 422, body)

    def test_real_read_of_the_serving_agent(self):
        agents_activity.forget()
        result = self.call({"agentIds": ["default", "ghost"]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"agents": [{"agentId": "default", "state": "idle"}]})


if __name__ == "__main__":
    unittest.main()
