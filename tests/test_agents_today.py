"""What an agent's background work did today, for the app's Today page. The agent's `state.db` is Hermes' own
(its SessionDB and its `async_delegations` table), the notices of finished commands are written with Hermes'
own `process_completion_display_text`, `processes.json` carries Hermes' checkpoint fields, and the cron ledger
has Hermes' own schema. Owners are real processes: this one, one that ended, one whose PID was recycled.
"""
from __future__ import annotations

from contextlib import closing
import sqlite3
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from loopdy_plugin import agents_activity, agents_background, agents_today, native_api
from test_agents_activity import (
    BOT_CHAT, Agent, gone_owner, hermes_available, live_owner, process_entry, route_client,
)
from test_native_api import PREFIX

SINCE = 1_794_038_400.0
OTHER_CHAT = "20261107_090000_d2d2d2"


def completed_event(delegation_id, status, *, summary=None, error=None, started=SINCE + 100, ended=SINCE + 160):
    """The event `_push_completion_event` stores in `event_json` for one subagent."""
    return {"type": "async_delegation", "delegation_id": delegation_id, "session_key": "", "goal": "x", "status": status,
            "summary": summary, "error": error, "duration_seconds": ended - started, "dispatched_at": started,
            "completed_at": ended, "exit_reason": "completed"}


def display_text(*events) -> str:
    from tools.process_registry_notifications import process_completion_display_text
    return process_completion_display_text(list(events))


def exited(command, code=0, reason="exited"):
    return {"type": "completion", "session_id": "proc_x", "command": command, "exit_code": code,
            "completion_reason": reason}


@unittest.skipUnless(hermes_available(), "this Hermes has no session store")
class AgentsTodayTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        folder = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.agents: dict[str, Agent] = {}
        homes = patch.object(agents_activity, "_home", side_effect=lambda name: self.agents[name].home
                             if name in self.agents else None)
        homes.start()
        self.addCleanup(homes.stop)
        agents_activity.forget()
        agents_today.forget()
        self.addCleanup(agents_today.forget)
        self.nova = self.agent("nova")
        # The Bot Chat began yesterday and is busy today.
        self.nova.chat(BOT_CHAT, title="Bot Chat")
        self.active(BOT_CHAT)

    def agent(self, name):
        agent = Agent(self.root, name)
        self.addCleanup(agent.close)
        self.agents[name] = agent
        return agent

    def active(self, session_id, *, started=SINCE - 86_400, last=SINCE + 9_000):
        self.nova.write("UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = ?",
                        (started, last, session_id))

    def notice(self, text, at, session_id=BOT_CHAT):
        """A background command's end, as Hermes saves it in the chat."""
        message = self.nova.db.append_message(
            session_id, "user", content="[IMPORTANT: Background process finished.]", timestamp=at,
            display_kind="process_complete", display_metadata={"display_text": text})
        return message

    def today(self, since=SINCE, name="nova"):
        agents_today.forget()
        agents_background.forget()
        return agents_today.today(name, since)

    def by_id(self, result):
        return {item["id"]: item for item in result["items"]}

    def test_a_subagents_outcome_maps_to_a_state_with_a_one_line_detail(self):
        rows = {
            "done": ("completed", completed_event("done", "completed", summary="Found the 2 mm gap in the case\nMore.")),
            "boom": ("failed", completed_event("boom", "failed", error="Provider refused the call\nTraceback...")),
            "late": ("timeout", completed_event("late", "timeout", error="No answer in 600 s")),
            "stuck": ("stalled", completed_event("stuck", "stalled", error="No progress")),
            "nope": ("rejected", completed_event("nope", "rejected", error="Tool not allowed")),
            "oops": ("error", completed_event("oops", "error", error="Crashed")),
            "stop": ("interrupted", completed_event("stop", "interrupted", error="Interrupted by the person")),
            "quit": ("cancelled", completed_event("quit", "cancelled")),
            "lost": ("unknown", completed_event("lost", "unknown",
                                                 error="Delegation owner exited before recording a terminal result.")),
        }
        for name, (status, event) in rows.items():
            self.nova.delegation(name, f"Goal of {name}", state=status, dispatched=SINCE + 100, completed=SINCE + 160,
                                 parent=BOT_CHAT, event=event)
        items = self.by_id(self.today())
        expected = {"done": "done", "boom": "failed", "late": "failed", "stuck": "failed", "nope": "failed",
                    "oops": "failed", "stop": "stopped", "quit": "stopped", "lost": "unknown"}
        self.assertEqual({key[len("delegation:"):]: value["state"] for key, value in items.items()}, expected)
        self.assertEqual(items["delegation:done"], {
            "id": "delegation:done", "kind": "subagent", "title": "Goal of done", "state": "done",
            "startedAt": SINCE + 100, "endedAt": SINCE + 160, "detail": "Found the 2 mm gap in the case",
            "sessionId": BOT_CHAT})
        self.assertEqual(items["delegation:boom"]["detail"], "Provider refused the call")
        self.assertNotIn("detail", items["delegation:quit"])
        self.assertEqual(items["delegation:lost"]["detail"],
                         "Delegation owner exited before recording a terminal result.")

    def test_a_batch_shows_its_group_or_first_goal_and_why_a_part_failed(self):
        results = [{"task_index": 0, "status": "completed", "summary": "Photo one is fine", "error": None},
                   {"task_index": 1, "status": "failed", "summary": None, "error": "Could not read photo two"}]
        event = {**completed_event("batch", "completed"), "is_batch": True, "results": results, "group": "Photo review",
                 "goals": ["Look at photo one", "Look at photo two"]}
        self.nova.delegation("batch", "Look at photo one", state="completed", dispatched=SINCE + 10,
                             completed=SINCE + 90, goals=["Look at photo one", "Look at photo two"], event=event)
        self.nova.delegation("fan", "Look at photo one", goals=["Look at photo one", "Look at photo two"])
        items = self.by_id(self.today())
        self.assertEqual((items["delegation:batch"]["title"], items["delegation:batch"]["detail"]),
                         ("Photo review", "Could not read photo two"))
        self.assertEqual((items["delegation:fan"]["title"], items["delegation:fan"]["state"]),
                         ("Look at photo one", "running"), "A running batch is one item, titled by its first goal")

    def test_a_running_row_whose_owner_is_gone_is_unknown_and_only_if_it_belongs_to_today(self):
        self.nova.delegation("today", "Started today", owner=gone_owner(), dispatched=SINCE + 500)
        self.nova.delegation("old", "Started yesterday", owner=gone_owner(), dispatched=SINCE - 5_000)
        items = self.by_id(self.today())
        self.assertEqual(items["delegation:today"]["state"], "unknown")
        self.assertNotIn("endedAt", items["delegation:today"])
        self.assertNotIn("delegation:old", items)

    def test_commands_running_now_and_finished_today(self):
        self.nova.checkpoint(
            process_entry("proc_1a2b3c", "npm run dev", live_owner(), parent=BOT_CHAT, started_at=SINCE - 600),
            process_entry("proc_gone", "sleep 9", gone_owner()))
        done = self.notice(display_text(exited("./build.sh")), SINCE + 100)
        failed = self.notice(display_text(exited("./test-hotspot.sh", 1)), SINCE + 200)
        stopped = self.notice(display_text(exited("tail -f log", None, "killed")), SINCE + 300)
        lost = self.notice(display_text(exited("rsync big", None, "lost")), SINCE + 400)
        no_start = self.notice(display_text(exited("./missing.sh", None, "failed_start")), SINCE + 500)
        batch = self.notice(display_text(exited("a"), exited("b"), exited("c")), SINCE + 600)
        long_one = self.notice(display_text(exited("echo " + "word " * 40)), SINCE + 700)
        yesterday = self.notice(display_text(exited("./old.sh")), SINCE - 60)
        self.notice("Subagent Task Completed: Something else", SINCE + 800)
        items = self.by_id(self.today())
        self.assertEqual(items["process:proc_1a2b3c"], {
            "id": "process:proc_1a2b3c", "kind": "command", "title": "npm run dev", "state": "running",
            "startedAt": SINCE - 600, "sessionId": BOT_CHAT}, "Running since before today still counts")
        self.assertNotIn("process:proc_gone", items)
        seen = {key: (value["state"], value["title"], value.get("detail")) for key, value in items.items()
                if key.startswith("message:")}
        self.assertEqual(seen, {
            f"message:{done}": ("done", "./build.sh", None),
            f"message:{failed}": ("failed", "./test-hotspot.sh", "Exit 1"),
            f"message:{stopped}": ("stopped", "tail -f log", None),
            f"message:{lost}": ("failed", "rsync big", None),
            f"message:{no_start}": ("failed", "./missing.sh", None),
            f"message:{batch}": ("done", "3 commands", None),
            f"message:{long_one}": ("done", seen[f"message:{long_one}"][1], None),
        })
        self.assertEqual(len(seen[f"message:{long_one}"][1]), 80, "Hermes' own short command")
        self.assertNotIn(f"message:{yesterday}", items)
        self.assertEqual(items[f"message:{failed}"], {
            "id": f"message:{failed}", "kind": "command", "title": "./test-hotspot.sh", "state": "failed",
            "endedAt": SINCE + 200, "detail": "Exit 1", "sessionId": BOT_CHAT})

    def test_only_chats_active_today_are_read(self):
        self.nova.chat(OTHER_CHAT, source="bighelp")
        self.active(OTHER_CHAT, started=SINCE - 90_000, last=SINCE - 80_000)
        stray = self.notice(display_text(exited("./stray.sh")), SINCE + 100, OTHER_CHAT)
        self.assertNotIn(f"message:{stray}", self.by_id(self.today()),
                         "A chat that was quiet all day isn't searched, whatever its rows say")

    def test_running_first_newest_start_first_then_the_rest_by_end(self):
        self.nova.delegation("older", "Older run", dispatched=SINCE - 4_000)
        self.nova.delegation("newer", "Newer run", dispatched=SINCE + 4_000)
        self.nova.delegation("fin1", "Early", state="completed", dispatched=SINCE + 10, completed=SINCE + 20,
                             event=completed_event("fin1", "completed", started=SINCE + 10, ended=SINCE + 20))
        self.nova.delegation("fin2", "Late", state="completed", dispatched=SINCE + 5, completed=SINCE + 900,
                             event=completed_event("fin2", "completed", started=SINCE + 5, ended=SINCE + 900))
        self.nova.delegation("before", "Ended before today", state="completed", dispatched=SINCE - 500,
                             completed=SINCE - 100, event=completed_event("before", "completed"))
        self.nova.delegation("across", "Started yesterday, ended today", state="completed", dispatched=SINCE - 500,
                             completed=SINCE + 50, event=completed_event("across", "completed"))
        result = self.today()
        self.assertEqual([item["id"] for item in result["items"]],
                         ["delegation:newer", "delegation:older", "delegation:fin2", "delegation:across",
                          "delegation:fin1"])
        self.assertFalse(result["truncated"])

    def test_at_most_100_items_and_running_work_is_never_the_part_left_out(self):
        self.nova.delegation("live", "Still going", dispatched=SINCE - 100)
        for index in range(105):
            self.notice(display_text(exited(f"job {index}")), SINCE + 100 + index)
        result = self.today()
        self.assertEqual((len(result["items"]), result["truncated"]), (100, True))
        self.assertEqual(result["items"][0]["id"], "delegation:live")
        self.assertEqual(result["items"][1]["endedAt"], SINCE + 204, "Newest first; the oldest were left out")

    def test_scheduled_runs_and_their_chat(self):
        self.nova.cron_jobs(job42="Morning brief", job7="Weekly digest")
        self.nova.cron_run("job42", "completed", SINCE + 10, finished=SINCE + 70)
        self.nova.cron_run("job42", "failed", SINCE + 3_600, finished=SINCE + 3_630)
        self.nova.cron_run("job42", "cancelled", SINCE + 7_200, finished=SINCE + 7_210)
        self.nova.cron_run("job42", "unknown", SINCE + 10_800, finished=SINCE + 10_900)
        self.nova.cron_run("job42", "missed", SINCE + 14_400)
        self.nova.cron_run("job42", "completed", SINCE - 7_200, finished=SINCE - 7_100)
        self.nova.cron_run("job7", "running", SINCE - 300)
        self.nova.cron_run("job7", "running", SINCE + 20_000, owner=gone_owner())
        session = "cron_job42_20261107_080010"
        self.nova.chat(session, source="cron")
        self.nova.write("UPDATE sessions SET started_at = ? WHERE id = ?", (SINCE + 11, session))
        self.nova.chat("cron_job420_20261107_080010", source="cron")
        self.nova.write("UPDATE sessions SET started_at = ? WHERE id = ?", (SINCE + 10, "cron_job420_20261107_080010"))
        result = self.today()
        rows = [(item["id"], item["title"], item["state"], item.get("endedAt"), item.get("sessionId"))
                for item in result["items"]]
        self.assertEqual(rows, [
            (f"cron:job7:{int(SINCE - 300)}", "Weekly digest", "running", None, None),
            (f"cron:job7:{int(SINCE + 20_000)}", "Weekly digest", "unknown", None, None),
            (f"cron:job42:{int(SINCE + 10_800)}", "Morning brief", "unknown", SINCE + 10_900, None),
            (f"cron:job42:{int(SINCE + 7_200)}", "Morning brief", "stopped", SINCE + 7_210, None),
            (f"cron:job42:{int(SINCE + 3_600)}", "Morning brief", "failed", SINCE + 3_630, None),
            (f"cron:job42:{int(SINCE + 10)}", "Morning brief", "done", SINCE + 70, session),
        ])
        # Its own state is the ledger's word; no text of an error is passed on.
        self.assertTrue(all("detail" not in item for item in result["items"]))

    def test_a_frequent_jobs_newest_run_still_finds_its_chat(self):
        self.nova.cron_jobs(job9="Every minute")
        for minute in range(250):
            name = f"cron_job9_20261106_{minute // 60:02d}{minute % 60:02d}00"
            self.nova.chat(name, source="cron")
            self.nova.write("UPDATE sessions SET started_at = ? WHERE id = ?", (SINCE - 40_000 + minute * 60, name))
        latest = "cron_job9_20261107_080000"
        self.nova.chat(latest, source="cron")
        self.nova.write("UPDATE sessions SET started_at = ? WHERE id = ?", (SINCE + 2, latest))
        self.nova.cron_run("job9", "completed", SINCE + 1, finished=SINCE + 30)
        self.assertEqual([item.get("sessionId") for item in self.today()["items"]], [latest])

    def test_a_chat_that_isnt_this_agents_has_no_session_link(self):
        self.nova.delegation("a", "Linked", parent=BOT_CHAT)
        self.nova.delegation("b", "Stranger", parent="20261107_111111_aaaaaa")
        self.nova.delegation("c", "Not an ID", parent="../../etc/passwd")
        items = self.by_id(self.today())
        self.assertEqual([items[f"delegation:{key}"].get("sessionId") for key in "abc"], [BOT_CHAT, None, None])

    def test_a_missing_source_adds_nothing_and_the_others_still_answer(self):
        # An older Hermes: a sessions table and nothing else.
        old = self.agent("old")
        old.close()
        (old.home / "state.db").unlink()
        for suffix in ("-wal", "-shm"):
            (old.home / f"state.db{suffix}").unlink(missing_ok=True)
        with closing(sqlite3.connect(old.home / "state.db")) as db:
            db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, started_at REAL NOT NULL)")
            db.execute("INSERT INTO sessions VALUES (?, 'bighelp', ?)", (BOT_CHAT, SINCE + 5))
            db.commit()
        self.assertEqual(self.today(name="old"), {"items": [], "truncated": False})
        old.checkpoint(process_entry("proc_ok", "make", live_owner()))
        self.assertEqual([item["id"] for item in self.today(name="old")["items"]], ["process:proc_ok"])
        # No state.db at all, no cron folder, junk where a file should be.
        bare = self.root / "bare"
        bare.mkdir()
        self.agents["bare"] = type("Bare", (), {"home": bare})()
        (bare / "processes.json").write_text("{not json")
        self.assertEqual(self.today(name="bare"), {"items": [], "truncated": False})

    def test_an_agent_that_isnt_a_profile_here_is_none_and_the_answer_is_kept_for_two_seconds(self):
        self.assertIsNone(agents_today.today("ghost", SINCE))
        clock = [1_000.0]
        agents_today.forget()
        first = agents_today.today("nova", SINCE, now=lambda: clock[0])
        self.nova.delegation("fresh", "New", dispatched=SINCE + 1)
        clock[0] += 1.0
        self.assertIs(agents_today.today("nova", SINCE, now=lambda: clock[0]), first)
        clock[0] += 1.5
        self.assertEqual(len(agents_today.today("nova", SINCE, now=lambda: clock[0])["items"]), 1)


class AgentsTodayRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = route_client(("native-context-v1", agents_today.CAPABILITY))
        self.addCleanup(self.client.stop)
        self.now = 1_794_100_000.0
        clock = patch.object(native_api, "time", SimpleNamespace(time=lambda: self.now))
        clock.start()
        self.addCleanup(clock.stop)

    def call(self, body, *, etag=True):
        headers = {"X-Loopdy-Request-ID": str(uuid.uuid4())}
        if etag:
            headers["If-Match"] = self.client.etag
        return self.client.post(PREFIX + "/agents/today", json=body, headers=headers)

    def test_answers_with_the_items_and_never_caches(self):
        value = {"items": [{"id": "process:proc_1", "kind": "command", "title": "make", "state": "running"}],
                 "truncated": False}
        with patch.object(agents_today, "today", return_value=value) as read:
            result = self.call({"agentId": "nova", "since": int(self.now - 3_600)})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), value)
        self.assertEqual(result.headers["cache-control"], "no-store")
        read.assert_called_once_with("nova", self.now - 3_600)

    def test_a_start_ahead_of_the_host_clock_is_read_as_now(self):
        with patch.object(agents_today, "today", return_value={"items": [], "truncated": False}) as read:
            self.assertEqual(self.call({"agentId": "nova", "since": self.now + 47 * 3_600}).status_code, 200)
        read.assert_called_once_with("nova", self.now)

    def test_refuses_bad_requests_and_a_since_outside_two_days(self):
        good = {"agentId": "nova", "since": self.now - 3_600}
        with patch.object(agents_today, "today", return_value={"items": [], "truncated": False}):
            self.assertEqual(self.call({**good, "since": self.now + 48 * 3600}).status_code, 200)
            self.assertEqual(self.call({**good, "since": self.now - 48 * 3600}).status_code, 200)
            for body in ({}, {"agentId": "nova"}, {"since": self.now}, {**good, "extra": 1}, {**good, "agentId": "../x"},
                         {**good, "agentId": "Nova"}, {**good, "since": "1794000000"}, {**good, "since": True},
                         {**good, "since": self.now + 48 * 3600 + 1}, {**good, "since": self.now - 48 * 3600 - 1},
                         {**good, "since": -5}):
                self.assertEqual(self.call(body).status_code, 422, body)
        wrong = self.client.post(PREFIX + "/agents/today?x=1", json=good, headers={
            "If-Match": self.client.etag, "X-Loopdy-Request-ID": str(uuid.uuid4())})
        self.assertEqual(wrong.status_code, 422)

    def test_an_unknown_agent_the_context_guard_and_a_host_without_the_feature(self):
        with patch.object(agents_activity, "_home", return_value=None):
            missing = self.call({"agentId": "ghost", "since": self.now - 60})
        self.assertEqual((missing.status_code, missing.json()["error"]["code"]), (404, "agent_not_found"))
        self.assertEqual(self.call({"agentId": "nova", "since": self.now - 60}, etag=False).status_code, 428)
        bare = route_client(("native-context-v1",))
        self.addCleanup(bare.stop)
        refused = bare.post(PREFIX + "/agents/today", json={"agentId": "nova", "since": self.now - 60},
                            headers={"If-Match": bare.etag, "X-Loopdy-Request-ID": str(uuid.uuid4())})
        self.assertEqual((refused.status_code, refused.json()["error"]["code"]), (503, "agents_today_unavailable"))


if __name__ == "__main__":
    unittest.main()
