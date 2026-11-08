"""Each agent's Bot Chat state for the app's home. Each agent's `state.db` is Hermes' own (its
SessionDB, its turn leases), a real child process stands in for `hermes -p <agent> chat -q`, and the
dashboard's open chats and the approvals they wait on are Hermes' own structures, as the dashboard
fills them.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from loopdy_plugin import agents_activity, agents_background, native_api
from loopdy_plugin.native_context import NativeContext
import test_native_api as native_fixtures
from test_native_api import PREFIX

BOT_CHAT = "20261021_090000_b0b0b0"
QUERY_CHAT = "20261021_091500_c1c1c1"
OTHER_CHAT = "20261021_092000_d2d2d2"


def process_start(pid: int):
    from gateway.status import get_process_start_time
    return get_process_start_time(pid)


def live_owner() -> dict:
    """An owner that is alive and is who it says: this test process."""
    return {"pid": os.getpid(), "started": process_start(os.getpid())}


def gone_owner() -> dict:
    """An owner whose process ended: a child that ran and was waited for."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    return {"pid": child.pid, "started": 1}


def recycled_owner() -> dict:
    """A PID that is alive, but was another process's when Hermes wrote it down."""
    return {"pid": os.getpid(), "started": process_start(os.getpid()) - 1_000_000}


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def route_client(features: tuple[str, ...]) -> TestClient:
    """The native router with a context that lists exactly these features, as a host would."""
    context = NativeContext(None, None, None, "default", features, "runtime")
    patcher = patch.object(native_api, "native_context", side_effect=lambda _request: context)
    patcher.start()
    app = FastAPI()
    app.include_router(native_api.router, prefix="/api/plugins/loopdy")
    client = TestClient(app)
    client.etag = context.etag
    client.stop = lambda: (client.close(), patcher.stop())
    return client


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

    def write(self, sql, params=()):
        with closing(sqlite3.connect(self.home / "state.db")) as db:
            db.execute(sql, params)
            db.commit()

    def delegation(self, delegation_id, goal, *, owner=None, state="running", dispatched=1_794_090_000.0,
                   completed=None, parent=None, goals=None, event=None):
        """A row as tools.async_delegation writes it: `_persist_dispatch`, then `_persist_completion`."""
        owner = owner or live_owner()
        task = {"goal": goal, "context": None, "toolsets": ["terminal"], "role": "leaf", "model": None,
                "is_batch": goals is not None, "owner_cwd": "/work"}
        if goals is not None:
            task.update(goals=goals, task_indexes=list(range(len(goals))))
        self.write(
            """INSERT INTO async_delegations (delegation_id, origin_session, origin_ui_session_id, parent_session_id,
               state, dispatched_at, completed_at, updated_at, event_json, delivery_state, owner_pid,
               owner_started_at, task_json, origin_session_id) VALUES (?, '', '', ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, '')""",
            (delegation_id, parent, state, dispatched, completed, completed or dispatched,
             json.dumps(event) if event else None, owner["pid"], owner["started"], json.dumps(task)))

    def checkpoint(self, *entries):
        (self.home / "processes.json").write_text(json.dumps(list(entries)))

    def cron_jobs(self, **names):
        (self.home / "cron").mkdir(exist_ok=True)
        jobs = [{"id": job_id, "name": name, "prompt": "Check things.", "schedule": {"kind": "cron", "expr": "0 7 * * *"},
                 "enabled": True} for job_id, name in names.items()]
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs, "updated_at": iso(1_794_000_000)}))

    def cron_run(self, job_id, status, claimed, *, finished=None, owner=None):
        """A row of Hermes' cron execution ledger, in its own schema."""
        from cron.executions import _initialize_schema
        owner = owner or live_owner()
        (self.home / "cron").mkdir(exist_ok=True)
        with closing(sqlite3.connect(self.home / "cron" / "executions.db")) as db:
            _initialize_schema(db)
            db.execute(
                """INSERT INTO executions (id, job_id, source, process_id, pid, process_started_at, status, claimed_at,
                   started_at, finished_at) VALUES (?, ?, 'scheduler', 'p1', ?, ?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex, job_id, owner["pid"], owner["started"], status, iso(claimed), iso(claimed),
                 iso(finished) if finished else None))
            db.commit()


def process_entry(process_id, command, owner, *, parent=None, task_id="", scope="host", started_at=1_794_090_000.0):
    """A `processes.json` entry: `session_id` plus Hermes' `_CHECKPOINT_FIELDS`."""
    return {"session_id": process_id, "command": command, "pid": owner["pid"], "pid_scope": scope,
            "host_start_time": owner["started"], "systemd_unit": "", "wsl_chain": "", "cwd": "/work",
            "started_at": started_at, "task_id": task_id, "owner_task_id": task_id, "session_key": "",
            "parent_session_id": parent, "notify_on_complete": True, "completion_output_chars": 4000,
            "watch_patterns": [], "heartbeat_seconds": 0, "persist_on_release": False, "output_log": ""}


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
        self.nova.say(BOT_CHAT, "user", "Message from Juno (@default): Check the PRs.")
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
        self.nova.say(BOT_CHAT, "user", "Message from Juno (@default): And #25?")
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

        password = requests.send_async("sudo", sid, {"command": "apt update"}, lambda result: None)
        self.addCleanup(password, "test")
        self.assertEqual(self.states("nova"), {"nova": ("waiting", None)}, "A password request waits on the person")
        password("resolved")

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


    def background(self, name="nova"):
        agents_activity.forget()
        row = {row["agentId"]: row for row in agents_activity.activity([name], background=True)}[name]
        return row.get("background"), row["state"]

    def test_background_counts_live_work_of_every_kind_and_names_the_newest(self):
        self.assertEqual(self.background(), (None, "idle"), "Nothing runs: no background field")
        self.nova.delegation("deleg_a1", "Find a battery back plate", dispatched=1_794_090_000.0)
        self.assertEqual(self.background(), ({"count": 1, "title": "Find a battery back plate"}, "idle"))
        self.nova.checkpoint(process_entry("proc_1a2b", "npm  run\n dev", live_owner(), started_at=1_794_090_500.0))
        self.nova.cron_jobs(job42="Morning brief")
        self.nova.cron_run("job42", "running", 1_794_090_200.0)
        self.assertEqual(self.background(), ({"count": 3, "title": "npm run dev"}, "idle"), "The command is newest")
        self.nova.delegation("deleg_b2", "Look at the puck photo", dispatched=1_794_091_000.0)
        holder = self.nova.turn(BOT_CHAT)
        self.addCleanup(self.nova.end_turn, BOT_CHAT, holder)
        self.assertEqual(self.background(), ({"count": 4, "title": "Look at the puck photo"}, "working"),
                         "Independent of the turn the agent is on")
        agents_activity.forget()
        self.assertNotIn("background", agents_activity.activity(["nova"])[0], "Without the feature, no field")

    def test_background_work_counts_only_while_its_owner_really_runs(self):
        self.nova.delegation("deleg_dead", "Owner is gone", owner=gone_owner())
        self.nova.delegation("deleg_reused", "PID was recycled", owner=recycled_owner())
        self.nova.delegation("deleg_done", "Already finished", state="completed", completed=1_794_090_100.0)
        self.nova.delegation("deleg_unknown", "Hermes marked it", state="unknown", owner=gone_owner())
        self.nova.checkpoint(
            process_entry("proc_gone", "sleep 600", gone_owner()),
            process_entry("proc_reused", "sleep 601", recycled_owner()),
            process_entry("proc_sandbox", "sleep 602", live_owner(), scope="sandbox"),
            process_entry("proc_child", "sleep 603", live_owner(), task_id="sa-0-abc"),
            process_entry("proc_other_agent", "sleep 604", live_owner(), parent="20261021_111111_aaaaaa"),
            process_entry("proc_mine", "sleep 605", live_owner(), parent=BOT_CHAT))
        self.nova.cron_run("job1", "completed", 1_794_080_000.0, finished=1_794_080_060.0)
        self.nova.cron_run("job2", "running", 1_794_080_000.0, owner=gone_owner())
        self.nova.cron_run("job3", "missed", 1_794_080_000.0)
        self.assertEqual(self.background(), ({"count": 1, "title": "sleep 605"}, "idle"))

    def test_background_titles_are_one_short_plain_line(self):
        goal = "  First line of the goal   with spaces\nSecond line: more detail. " + "x" * 200
        self.nova.delegation("deleg_t1", goal)
        title = self.background()[0]["title"]
        self.assertEqual(title, "First line of the goal with spaces")
        self.nova.write("DELETE FROM async_delegations")
        self.nova.delegation("deleg_t2", "w" * 300)
        title = self.background()[0]["title"]
        self.assertEqual((len(title), title[-3:]), (60, "..."), "Short on the home: 64 rows must fit one answer")
        # The worst row: 64-character IDs and tool, a 60-letter title in a script of 3-byte letters.
        worst = {"agentId": "a" * 64, "state": "working", "tool": "t" * 64,
                 "background": {"count": 64, "title": "\u6f22" * 60}}
        self.assertLess(64 * len(json.dumps(worst, ensure_ascii=False, separators=(",", ":")).encode()), 32_768)
        self.nova.write("DELETE FROM async_delegations")
        self.nova.delegation("deleg_t3", "Use key sk-abcdefghijklmnopqrstuvwxyz123456 to call it")
        self.assertNotIn("abcdefghij", self.background()[0]["title"])
        self.nova.write("DELETE FROM async_delegations")
        self.nova.delegation("deleg_t4", "  \n ")
        self.assertEqual(self.background()[0], {"count": 1}, "Nothing usable: no title")

    def test_a_title_Hermes_cannot_redact_is_never_sent(self):
        self.nova.delegation("deleg_r1", "Clone https://sam:pw1234secret@example.com/repo.git")
        self.assertNotIn("pw1234secret", self.background()[0]["title"], "URL credentials go too")
        for broken in (None, type("Broken", (), {"redact_sensitive_text": staticmethod(lambda *a, **k: 1 / 0)})):
            with patch.dict(sys.modules, {"agent.redact": broken}):
                self.assertEqual(self.background()[0], {"count": 1}, "No redaction: no title, never the raw text")

    def test_a_scheduled_run_with_no_recorded_start_counts_only_for_the_hermes_that_runs_here(self):
        self.nova.cron_jobs(job1="Digest")
        process, _ = self.child()
        self.nova.cron_run("job1", "running", 1_794_090_000.0, owner={"pid": process.pid, "started": None})
        self.assertEqual(self.background(), (None, "idle"), "Another live process isn't proof, as in Hermes")
        self.nova.cron_run("job1", "running", 1_794_090_100.0, owner={"pid": os.getpid(), "started": None})
        self.assertEqual(self.background(), ({"count": 1, "title": "Digest"}, "idle"))

    def test_the_title_on_the_home_has_no_folders(self):
        self.nova.delegation("deleg_p1", "Run /home/sam/projects/kyoto/build.py on ~/data/photos")
        self.assertEqual(self.background()[0]["title"], "Run build.py on photos")
        self.nova.checkpoint(process_entry("proc_p2", "cd /srv/app && npm run dev", live_owner(),
                                           started_at=1_794_099_000.0))
        self.assertEqual(self.background()[0]["title"], "cd app && npm run dev")

    def test_an_older_hermes_without_background_records_adds_nothing(self):
        self.nova.write("DROP TABLE async_delegations")
        self.assertEqual(self.background(), (None, "idle"), "No table, no processes.json, no cron folder")
        self.nova.checkpoint(process_entry("proc_ok", "make", live_owner()))
        (self.nova.home / "cron").mkdir()
        (self.nova.home / "cron" / "executions.db").write_bytes(b"not a database")
        self.assertEqual(self.background(), ({"count": 1, "title": "make"}, "idle"),
                         "An unreadable ledger leaves out only itself")


class HidePathsTests(unittest.TestCase):
    def test_a_host_path_shows_only_its_file_name_and_everything_else_stays(self):
        for text, shown in (
            ("python /home/sam/build.py", "python build.py"),
            ("~/projects/kyoto/run.sh --x", "run.sh --x"),
            ("cd /srv/app && npm run dev", "cd app && npm run dev"),
            ("cp '/var/log/app.log' /tmp/out/", "cp 'app.log' out"),
            ("tool --out=/tmp/build/result.json", "tool --out=result.json"),
            ("Edited /Users/sam/code/case.tsx.", "Edited case.tsx."),
            ("cat ~sam/notes.md", "cat notes.md"),
            ("./sync.sh src/case.tsx ../shared/x.py", "./sync.sh src/case.tsx ../shared/x.py"),
            ("curl https://example.com/a/b?x=/c#d", "curl https://example.com/a/b?x=/c#d"),
            ("open file:///home/sam/a.html and http://host:8080/x", "open file:///home/sam/a.html and http://host:8080/x"),
            ("and/or 10 / 2 and ~ here, a / b", "and/or 10 / 2 and ~ here, a / b"),
            ("ls ~/ /", "ls ~/ /"),
        ):
            self.assertEqual(agents_background._hide_paths(text), shown, text)


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
        read.assert_called_once_with(["default", "nova"], background=True)
        for body in ({}, {"agentIds": []}, {"agentIds": ["../etc"]}, {"agentIds": ["default"], "sessionId": "x"},
                     {"agentIds": [f"a{index}" for index in range(65)]}, {"agentIds": "default"}):
            self.assertEqual(self.call(body).status_code, 422, body)

    def test_background_is_asked_for_only_when_the_host_lists_it(self):
        client = route_client(("native-context-v1", agents_activity.CAPABILITY))
        self.addCleanup(client.stop)
        headers = {"If-Match": client.etag, "X-Loopdy-Request-ID": str(uuid.uuid4())}
        with patch.object(agents_activity, "activity", return_value=[]) as read:
            client.post(PREFIX + "/agents/activity", json={"agentIds": ["nova"]}, headers=headers)
            read.assert_called_once_with(["nova"], background=False)
        newer = route_client(("native-context-v1", agents_activity.CAPABILITY, agents_background.CAPABILITY))
        self.addCleanup(newer.stop)
        headers = {"If-Match": newer.etag, "X-Loopdy-Request-ID": str(uuid.uuid4())}
        with patch.object(agents_activity, "activity", return_value=[]) as read:
            newer.post(PREFIX + "/agents/activity", json={"agentIds": ["nova"]}, headers=headers)
            read.assert_called_once_with(["nova"], background=True)

    def test_real_read_of_the_serving_agent(self):
        agents_activity.forget()
        result = self.call({"agentIds": ["default", "ghost"]})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"agents": [{"agentId": "default", "state": "idle"}]})


if __name__ == "__main__":
    unittest.main()
