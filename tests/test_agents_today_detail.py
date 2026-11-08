"""One item of the Today page, opened. Chats and messages are written by Hermes' own SessionDB (a child of a
delegation is a `subagent` chat with the `_delegate_from` and `_subagent_id` marks Hermes stamps), command
notices are Hermes' own `format_process_notification` text, and the cron ledger has Hermes' own schema.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import uuid

from loopdy_plugin import agents_activity, agents_background, agents_today, agents_today_detail as opened
from test_agents_activity import (
    BOT_CHAT, Agent, gone_owner, hermes_available, live_owner, process_entry, route_client,
)
from test_agents_today import SINCE, completed_event, display_text, exited
from test_native_api import PREFIX

T0 = SINCE + 100


def tool_call(call_id, name, **arguments):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def completion_notice(command, code=0, output="", reason="exited", **more):
    """The text Hermes saves for a command that ended, from its own formatter."""
    from tools.process_registry_notifications import format_process_notification
    return format_process_notification({"type": "completion", "session_id": "proc_x", "command": command,
                                        "exit_code": code, "completion_reason": reason, "output": output, **more})


@unittest.skipUnless(hermes_available(), "this Hermes has no session store")
class AgentsTodayDetailTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        folder = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.agents: dict[str, Agent] = {}
        homes = patch.object(agents_activity, "_home", side_effect=lambda name: self.agents[name].home
                             if name in self.agents else None)
        homes.start()
        self.addCleanup(homes.stop)
        agents_activity.forget()
        self.addCleanup(opened.forget)
        self.nova = self.agent("nova")
        self.nova.chat(BOT_CHAT, title="Bot Chat")
        self.nova.write("UPDATE sessions SET started_at = ?, last_activity_at = ? WHERE id = ?",
                        (SINCE - 86_400, SINCE + 9_000, BOT_CHAT))

    def agent(self, name):
        agent = Agent(self.root, name)
        self.addCleanup(agent.close)
        self.agents[name] = agent
        return agent

    # What Hermes writes -------------------------------------------------------------------------------

    def child(self, goal, *, parent=BOT_CHAT, started=T0 + 1, mark="sa-0-aaaa1111", model="gpt-6-luna", agent=None):
        """A delegation's child chat, as `_reserve_child_transcript` and the child agent write it."""
        agent = agent or self.nova
        session_id = f"20261107_16{len(self.made):02d}00_{uuid.uuid4().hex[:6]}"
        self.made.append(session_id)
        agent.db.create_session(session_id, "subagent", model=model, parent_session_id=parent,
                                model_config={"_delegate_from": parent, "_subagent_id": mark})
        agent.write("UPDATE sessions SET started_at = ? WHERE id = ?", (started, session_id))
        agent.db.append_message(session_id, "user", goal, timestamp=started)
        return session_id

    @property
    def made(self):
        return self.__dict__.setdefault("_made", [])

    def calls(self, session_id, at, *calls, content=None, reasoning=None):
        self.nova.db.append_message(session_id, "assistant", content, tool_calls=list(calls), timestamp=at,
                                    reasoning=reasoning)

    def result(self, session_id, at, call_id, content):
        self.nova.db.append_message(session_id, "tool", content, tool_call_id=call_id, timestamp=at)

    def today_items(self):
        agents_today.forget()
        agents_background.forget()
        return {item["id"]: item for item in agents_today.today("nova", SINCE)["items"]}

    def open(self, item_id, name="nova"):
        opened.forget()
        agents_background.forget()
        return opened.detail(name, item_id)

    # Subagents ----------------------------------------------------------------------------------------

    def test_a_running_subagent_shows_its_steps_in_order_with_what_each_call_did(self):
        self.nova.delegation("d1", "Find a ryokan near Kyoto station", dispatched=T0, parent=BOT_CHAT)
        child = self.child("Find a ryokan near Kyoto station")
        self.calls(child, T0 + 2, tool_call("c1", "web_search", query="kyoto station ryokan"),
                   content="I will search first.", reasoning="The person wants somewhere quiet.")
        self.result(child, T0 + 3, "c1", json.dumps({"success": True, "data": {"web": []}}))
        self.calls(child, T0 + 4, tool_call("c2", "read_file", path="/home/sam/trips/notes.md"))
        self.result(child, T0 + 5, "c2", json.dumps({"error": "File not found: notes.md"}))
        self.calls(child, T0 + 6, tool_call("c3", "terminal", command="ls"))
        self.result(child, T0 + 7, "c3", json.dumps({"output": "x" * 5_000, "exit_code": 2, "error": None}))
        self.calls(child, T0 + 8, tool_call("c4", "terminal", command="pwd"))
        self.result(child, T0 + 9, "c4", json.dumps({"output": "/work", "exit_code": 0, "error": None}))
        self.calls(child, T0 + 10, tool_call("c5", "browser_navigate", url="https://example.com/rooms"))
        self.result(child, T0 + 11, "c5", "Error: the page did not load")
        self.calls(child, T0 + 12, tool_call("c6", "web_search", query="still going"))
        self.nova.db.append_message(child, "assistant", "Here is my answer.", timestamp=T0 + 13)
        result = self.open("delegation:d1")
        self.assertEqual(result["item"], self.today_items()["delegation:d1"])
        self.assertEqual(result["subagent"], {
            "goal": "Find a ryokan near Kyoto station",
            "model": "gpt-6-luna",
            "steps": [
                {"at": T0 + 2, "kind": "thinking", "preview": "The person wants somewhere quiet."},
                {"at": T0 + 2, "kind": "message", "preview": "I will search first."},
                {"at": T0 + 2, "kind": "tool", "tool": "web_search", "preview": "kyoto station ryokan", "state": "done"},
                {"at": T0 + 4, "kind": "tool", "tool": "read_file", "preview": "notes.md", "state": "failed"},
                {"at": T0 + 6, "kind": "tool", "tool": "terminal", "preview": "ls", "state": "failed"},
                {"at": T0 + 8, "kind": "tool", "tool": "terminal", "preview": "pwd", "state": "done"},
                {"at": T0 + 10, "kind": "tool", "tool": "browser_navigate", "preview": "example.com/rooms",
                 "state": "failed"},
                {"at": T0 + 12, "kind": "tool", "tool": "web_search", "preview": "still going", "state": "running"},
            ],
            "stepsTruncated": False,
        })

    def test_a_tool_preview_is_the_one_telling_argument_in_plain_form(self):
        self.nova.delegation("d1", "Look around", dispatched=T0, parent=BOT_CHAT)
        child = self.child("Look around")
        long_command = "python build.py --target " + "x" * 100 + " --token abcdefghijklmnopqrstuvwxyz0123456789"
        for index, call in enumerate((
                tool_call("a", "web_extract", urls=["https://user:pw@example.com/docs/a?token=abcdef1234567890#top"]),
                tool_call("b", "terminal", command=long_command),
                tool_call("c", "write_file", path="/srv/app/out/case.tsx", content="secret body"),
                tool_call("d", "some_plugin_tool", query="plain words", other="ignored"),
                tool_call("e", "empty_tool"))):
            self.calls(child, T0 + 2 + index, call)
        steps = {step["tool"]: step.get("preview") for step in self.open("delegation:d1")["subagent"]["steps"]}
        self.assertEqual(steps["web_extract"], "example.com/docs/a")
        self.assertEqual(len(steps["terminal"]), 80)
        self.assertTrue(steps["terminal"].startswith("python build.py --target xxx") and steps["terminal"].endswith("..."))
        self.assertEqual((steps["write_file"], steps["some_plugin_tool"], steps["empty_tool"]),
                         ("case.tsx", "plain words", None))

    def test_a_finished_subagent_has_its_summary_error_and_no_step_for_the_final_answer(self):
        answer = "## Found it\n\nThe gap is 2 mm. See /srv/app/out/report.md and https://example.com/a/b.\n"
        self.nova.delegation("ok", "Measure the case", state="completed", dispatched=T0, completed=T0 + 90,
                             parent=BOT_CHAT, event=completed_event("ok", "completed", summary=answer))
        self.nova.delegation("bad", "Read /home/sam/photo.jpg", state="failed", dispatched=T0, completed=T0 + 50,
                             parent=BOT_CHAT, event=completed_event(
                                 "bad", "failed", error="Could not read /home/sam/photo.jpg\nTraceback (most recent call last)\n"
                                 + "x" * 400))
        good = self.child("Measure the case", mark="sa-0-1111")
        self.nova.db.append_message(good, "assistant", answer, timestamp=T0 + 80)
        value = self.open("delegation:ok")["subagent"]
        self.assertEqual(value["summary"], "## Found it\n\nThe gap is 2 mm. See report.md and https://example.com/a/b.")
        self.assertNotIn("steps", value, "Only the final answer was saved, and it is the summary")
        failed = self.open("delegation:bad")["subagent"]
        self.assertEqual(failed["goal"], "Read photo.jpg")
        self.assertEqual(failed["error"], "Could not read photo.jpg")
        self.assertNotIn("summary", failed)

    def test_a_batch_joins_its_children_and_their_compressed_parts_in_time_order(self):
        goals = ["Look at photo one", "Look at photo two"]
        results = [{"task_index": 0, "status": "completed", "summary": "Photo one is fine", "error": None},
                   {"task_index": 1, "status": "failed", "summary": "Partial: blurry", "error": "Could not read photo two"}]
        event = {**completed_event("batch", "completed"), "is_batch": True, "results": results, "goals": goals}
        self.nova.delegation("batch", goals[0], state="completed", dispatched=T0, completed=T0 + 200,
                             parent=BOT_CHAT, goals=goals, event=event)
        one, two = self.child(goals[0], mark="sa-0-1111"), self.child(goals[1], mark="sa-1-2222")
        self.calls(one, T0 + 10, tool_call("a", "web_search", query="one first"))
        self.calls(two, T0 + 20, tool_call("b", "web_search", query="two first"))
        # The first child's chat was compressed: its work goes on in a new chat that carries its mark.
        more = "20261107_169999_cccccc"
        self.nova.db.create_session(more, "subagent", parent_session_id=one,
                                    model_config={"_delegate_from": BOT_CHAT, "_subagent_id": "sa-0-1111"})
        self.nova.db.append_message(more, "assistant", None, tool_calls=[tool_call("c", "read_file", path="one.md")],
                                    timestamp=T0 + 30)
        self.calls(two, T0 + 40, tool_call("d", "web_search", query="two second"))
        # Not this delegation's: another goal, and a child of another chat.
        self.calls(self.child("Something unrelated", mark="sa-2-3333"), T0 + 15, tool_call("x", "web_search", query="no"))
        self.nova.chat("20261107_150000_eeeeee")
        self.calls(self.child(goals[0], parent="20261107_150000_eeeeee", mark="sa-3-4444"), T0 + 16,
                   tool_call("y", "web_search", query="no either"))
        value = self.open("delegation:batch")["subagent"]
        self.assertEqual([step["preview"] for step in value["steps"]],
                         ["one first", "two first", "one.md", "two second"])
        self.assertEqual(value["goal"], "Look at photo one\n\nLook at photo two")
        self.assertEqual(value["summary"],
                         "### Look at photo one\n\nPhoto one is fine\n\n### Look at photo two\n\nPartial: blurry")
        self.assertEqual(value["error"], "Could not read photo two")

    def test_the_newest_300_steps_are_kept_and_the_cut_is_said(self):
        self.nova.delegation("long", "Do a lot", dispatched=T0, parent=BOT_CHAT)
        child = self.child("Do a lot")
        rows = []
        for index in range(320):
            call = json.dumps([tool_call(f"c{index}", "web_search", query=f"query {index}")])
            rows.append((child, "assistant", None, call, None, T0 + 2 + index * 2))
            rows.append((child, "tool", "{}", None, f"c{index}", T0 + 3 + index * 2))
        self.nova.write("SELECT 1")
        with closing(sqlite3.connect(self.nova.home / "state.db")) as db:
            db.executemany("INSERT INTO messages (session_id, role, content, tool_calls, tool_call_id, timestamp) "
                           "VALUES (?, ?, ?, ?, ?, ?)", rows)
            db.commit()
        value = self.open("delegation:long")["subagent"]
        self.assertEqual((len(value["steps"]), value["stepsTruncated"]), (300, True))
        self.assertEqual((value["steps"][0]["preview"], value["steps"][-1]["preview"]), ("query 20", "query 319"))

    def test_a_long_answer_never_goes_past_the_routes_size(self):
        steps = [{"at": float(index), "kind": "message", "preview": "é" * 2_000} for index in range(300)]
        value = opened._fit({"item": {}, "subagent": {"steps": steps, "stepsTruncated": False}})
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.assertLessEqual(size, opened.RESPONSE_BUDGET)
        self.assertTrue(value["subagent"]["stepsTruncated"])
        self.assertEqual(value["subagent"]["steps"][-1]["at"], 299.0, "The oldest steps go first")

    # Commands -----------------------------------------------------------------------------------------

    def log(self, name, text):
        folder = self.nova.home / "logs" / "process-output"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / name
        path.write_bytes(text.encode() if isinstance(text, str) else text)
        return path

    def test_a_running_command_shows_the_end_of_its_output_log_as_plain_safe_text(self):
        log = self.log("proc_1a2b3c.log", "old line\n" + "\x1b[32mready\x1b[0m on /home/sam/app/server.js\n"
                       "progress 10%\rprogress 90%\nOPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456\n")
        self.nova.checkpoint({**process_entry("proc_1a2b3c", "cd /srv/app && npm run dev", live_owner(),
                                              parent=BOT_CHAT, started_at=SINCE - 600), "output_log": str(log)})
        result = self.open("process:proc_1a2b3c")
        self.assertEqual(result["item"], self.today_items()["process:proc_1a2b3c"])
        command = result["command"]
        self.assertEqual(command["command"], "cd app && npm run dev")
        self.assertEqual(command["output"], "old line\nready on server.js\nprogress 90%\nOPENAI_API_KEY=***")
        self.assertNotIn("exitCode", command, "A running command has no exit code")
        self.assertFalse(command["outputTruncated"])

    def test_a_long_output_log_is_read_from_its_end_only(self):
        lines = "".join(f"line {index:05d}\n" for index in range(20_000))
        log = self.log("proc_big.log", lines)
        self.nova.checkpoint({**process_entry("proc_big", "make", live_owner()), "output_log": str(log)})
        command = self.open("process:proc_big")["command"]
        self.assertTrue(command["outputTruncated"])
        self.assertLessEqual(len(command["output"]), 16_000)
        self.assertTrue(command["output"].endswith("line 19999"))

    def test_only_a_log_in_the_agents_own_output_folder_is_read(self):
        outside = self.root / "elsewhere.log"
        outside.write_text("not for the phone")
        link = self.nova.home / "logs" / "process-output"
        link.mkdir(parents=True)
        (link / "proc_link.log").symlink_to(outside)
        self.nova.checkpoint({**process_entry("proc_out", "a", live_owner()), "output_log": str(outside)},
                             {**process_entry("proc_link", "b", live_owner()), "output_log": str(link / "proc_link.log")},
                             {**process_entry("proc_none", "c", live_owner()), "output_log": ""})
        for name in ("proc_out", "proc_link", "proc_none"):
            self.assertNotIn("output", self.open(f"process:{name}")["command"], name)

    def finished(self, text, at, display):
        return self.nova.db.append_message(BOT_CHAT, "user", content=text, timestamp=at,
                                           display_kind="process_complete", display_metadata={"display_text": display})

    def test_a_command_that_ended_shows_its_exit_code_and_output_from_the_notice(self):
        failed = self.finished(
            completion_notice("./test-hotspot.sh --out /srv/app/log.txt", 1,
                              "\x1b[31mFAIL\x1b[0m hotspot at /srv/app/run.py:12\nAPI_KEY=abcdef1234567890abcdef"),
            T0, display_text(exited("./test-hotspot.sh", 1)))
        done = self.finished(completion_notice("./build.sh", 0, "built"), T0 + 1, display_text(exited("./build.sh")))
        killed = self.finished(completion_notice("tail -f log", -15, "", "killed"), T0 + 2,
                               display_text(exited("tail -f log", None, "killed")))
        result = self.open(f"message:{failed}")
        self.assertEqual(result["item"], self.today_items()[f"message:{failed}"])
        self.assertEqual(result["command"], {
            "command": "./test-hotspot.sh --out log.txt", "exitCode": 1, "outputTruncated": False,
            "output": "FAIL hotspot at run.py:12\nAPI_KEY=***"})
        self.assertEqual(self.open(f"message:{done}")["command"],
                         {"command": "./build.sh", "exitCode": 0, "output": "built", "outputTruncated": False})
        self.assertEqual(self.open(f"message:{killed}")["command"]["exitCode"], -15)
        self.assertNotIn("output", self.open(f"message:{killed}")["command"])

    def test_a_cut_notice_and_a_batch_notice_say_what_they_are(self):
        cut = self.finished(completion_notice("make", 0, "tail of it", output_cut=900), T0,
                            display_text(exited("make")))
        both = self.finished("[IMPORTANT: 2 background processes completed.]\n\n" + completion_notice("a", 0, "A out")
                             + "\n\n" + completion_notice("b", 1, "B out"), T0 + 1,
                             display_text(exited("a"), exited("b")))
        value = self.open(f"message:{cut}")["command"]
        self.assertEqual((value["output"], value["outputTruncated"]), ("tail of it", True))
        batch = self.open(f"message:{both}")
        self.assertEqual(batch["item"]["title"], "2 commands")
        self.assertNotIn("command", batch["command"])
        self.assertNotIn("exitCode", batch["command"])
        self.assertIn("A out", batch["command"]["output"])

    # Scheduled tasks ----------------------------------------------------------------------------------

    def set_error(self, error):
        with closing(sqlite3.connect(self.nova.home / "cron" / "executions.db")) as db:
            db.execute("UPDATE executions SET error = ?", (error,))
            db.commit()

    def test_a_scheduled_run_shows_the_final_answer_of_its_chat_and_the_ledgers_status(self):
        self.nova.cron_jobs(job42="Morning brief")
        self.nova.cron_run("job42", "completed", T0, finished=T0 + 60)
        session = "cron_job42_20261107_080010"
        self.nova.chat(session, source="cron")
        self.nova.write("UPDATE sessions SET started_at = ? WHERE id = ?", (T0 + 1, session))
        self.nova.db.append_message(session, "user", "Write the brief", timestamp=T0 + 1)
        self.nova.db.append_message(session, "assistant", None, tool_calls=[tool_call("c", "web_search", query="news")],
                                    timestamp=T0 + 2)
        self.nova.db.append_message(session, "assistant", "# Brief\n\nQuiet day. Notes in /home/sam/brief.md", timestamp=T0 + 50)
        item_id = f"cron:job42:{int(T0)}"
        result = self.open(item_id)
        self.assertEqual(result["item"], self.today_items()[item_id])
        self.assertEqual(result["scheduled"], {"job": "Morning brief", "status": "done",
                                               "output": "# Brief\n\nQuiet day. Notes in brief.md"})

    def test_a_failed_run_has_its_ledger_error_and_a_run_with_a_file_has_that_files_response(self):
        self.nova.cron_jobs(job42="Morning brief")
        self.nova.cron_run("job42", "failed", T0, finished=T0 + 60)
        self.set_error("script_failed: exit 2 at /home/sam/job.sh")
        folder = self.nova.home / "cron" / "output" / "job42"
        folder.mkdir(parents=True)
        mine = folder / "2026-11-07_08-01-00.md"
        mine.write_text("# Cron Job: Morning brief\n\n**Job ID:** job42\n\n## Prompt\n\nDo it\n\n## Response\n\nIt broke.\n")
        os.utime(mine, (T0 + 61, T0 + 61))
        result = self.open(f"cron:job42:{int(T0)}")["scheduled"]
        self.assertEqual(result, {"job": "Morning brief", "status": "failed", "output": "It broke.",
                                  "error": "script_failed: exit 2 at job.sh"})

    def test_a_file_that_is_a_neighbours_is_not_taken_and_a_running_run_has_no_output(self):
        self.nova.cron_run("job7", "completed", T0, finished=T0 + 60)
        self.nova.cron_run("job7", "completed", T0 + 80, finished=T0 + 100)
        self.nova.cron_run("job7", "running", T0 + 500)
        folder = self.nova.home / "cron" / "output" / "job7"
        folder.mkdir(parents=True)
        later = folder / "2026-11-07_08-02-40.md"
        later.write_text("## Response\n\nSecond run")
        os.utime(later, (T0 + 101, T0 + 101))  # near both runs, nearest the second
        first, second, running = (self.open(f"cron:job7:{int(start)}")["scheduled"] for start in (T0, T0 + 80, T0 + 500))
        self.assertEqual((first.get("output"), second.get("output"), running.get("output")), (None, "Second run", None))
        self.assertEqual((first["status"], running["status"]), ("done", "running"))
        self.assertNotIn("job", first, "A job with no name has no name here")

    # Not found, safety, cache -------------------------------------------------------------------------

    def test_an_agent_or_item_that_isnt_there_is_not_found(self):
        self.nova.delegation("d1", "Mine", parent=BOT_CHAT)
        self.nova.checkpoint(process_entry("proc_here", "make", live_owner()),
                             process_entry("proc_gone", "sleep 9", gone_owner()))
        self.nova.cron_run("job1", "completed", T0, finished=T0 + 5)
        other = self.agent("sage")
        with self.assertRaises(opened.AgentNotFound):
            self.open("delegation:d1", "ghost")
        for item_id in ("delegation:nope", "process:proc_gone", "process:proc_nope", "message:99999", "message:abc",
                        f"message:{self.nova.db.append_message(BOT_CHAT, 'user', 'hello', timestamp=T0)}",
                        "cron:job1:12345", "cron:job1:abc", "cron:nojob:1", "cron:job1", "delegation:d1:x"):
            with self.assertRaises(opened.ActivityNotFound, msg=item_id):
                self.open(item_id)
        with self.assertRaises(opened.ActivityNotFound):
            self.open("delegation:d1", "sage")
        self.assertEqual(other.home.name, "sage")

    def test_text_that_cannot_be_redacted_is_never_sent(self):
        self.nova.delegation("ok", "Measure the case", state="completed", dispatched=T0, completed=T0 + 90,
                             parent=BOT_CHAT, event=completed_event("ok", "completed", summary="The secret answer"))
        child = self.child("Measure the case")
        self.calls(child, T0 + 2, tool_call("a", "web_search", query="private query"), content="private thought")
        self.result(child, T0 + 3, "a", "{}")
        log = self.log("proc_r.log", "private output\n")
        self.nova.checkpoint({**process_entry("proc_r", "make", live_owner()), "output_log": str(log)})
        with patch.object(agents_background, "_redact", return_value=None):
            value = self.open("delegation:ok")["subagent"]
            command = self.open("process:proc_r")["command"]
        self.assertEqual(value["steps"], [{"at": T0 + 2, "kind": "tool", "tool": "web_search", "state": "done"}],
                         "The step stays, with no text, and the message with no text is dropped")
        self.assertEqual(set(value), {"steps", "stepsTruncated"}, "No goal, model or summary text either")
        self.assertEqual(command, {})

    def test_the_answer_is_kept_for_two_seconds(self):
        self.nova.delegation("d1", "Mine", parent=BOT_CHAT, dispatched=T0)
        clock = [1_000.0]
        opened.forget()
        first = opened.detail("nova", "delegation:d1", now=lambda: clock[0])
        self.nova.write("DELETE FROM async_delegations")
        clock[0] += 1.0
        self.assertIs(opened.detail("nova", "delegation:d1", now=lambda: clock[0]), first)
        clock[0] += 1.5
        with self.assertRaises(opened.ActivityNotFound):
            opened.detail("nova", "delegation:d1", now=lambda: clock[0])


class AgentsTodayDetailRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = route_client(("native-context-v1", agents_today.DETAIL_CAPABILITY))
        self.addCleanup(self.client.stop)

    def call(self, body, *, etag=True, client=None):
        client = client or self.client
        headers = {"X-Loopdy-Request-ID": str(uuid.uuid4())}
        if etag:
            headers["If-Match"] = client.etag
        return client.post(PREFIX + "/agents/today/detail", json=body, headers=headers)

    def test_answers_with_the_detail_and_never_caches(self):
        value = {"item": {"id": "process:proc_1", "kind": "command", "title": "make", "state": "running"},
                 "command": {"command": "make", "output": "ok"}}
        with patch.object(opened, "detail", return_value=value) as read:
            result = self.call({"agentId": "nova", "id": "process:proc_1"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), value)
        self.assertEqual(result.headers["cache-control"], "no-store")
        read.assert_called_once_with("nova", "process:proc_1")

    def test_refuses_bad_requests(self):
        good = {"agentId": "nova", "id": "delegation:abc123"}
        with patch.object(opened, "detail", return_value={"item": {}}):
            self.assertEqual(self.call(good).status_code, 200)
            self.assertEqual(self.call({**good, "id": "cron:job42:1794038400"}).status_code, 200)
            for body in ({}, {"agentId": "nova"}, {"id": good["id"]}, {**good, "extra": 1}, {**good, "agentId": "../x"},
                         {**good, "agentId": "Nova"}, {**good, "id": "session:abc"}, {**good, "id": "delegation:"},
                         {**good, "id": "delegation:../etc"}, {**good, "id": "message:" + "9" * 81}, {**good, "id": 5},
                         {**good, "id": None}):
                self.assertEqual(self.call(body).status_code, 422, body)
        wrong = self.client.post(PREFIX + "/agents/today/detail?x=1", json=good, headers={
            "If-Match": self.client.etag, "X-Loopdy-Request-ID": str(uuid.uuid4())})
        self.assertEqual(wrong.status_code, 422)

    def test_not_found_the_context_guard_and_a_host_without_the_feature(self):
        for error, code in ((opened.AgentNotFound, "agent_not_found"), (opened.ActivityNotFound, "activity_not_found")):
            with patch.object(opened, "detail", side_effect=error):
                missing = self.call({"agentId": "nova", "id": "delegation:abc"})
            self.assertEqual((missing.status_code, missing.json()["error"]["code"]), (404, code))
        self.assertEqual(self.call({"agentId": "nova", "id": "delegation:abc"}, etag=False).status_code, 428)
        # Today's list alone does not open items.
        today_only = route_client(("native-context-v1", agents_today.CAPABILITY))
        self.addCleanup(today_only.stop)
        refused = self.call({"agentId": "nova", "id": "delegation:abc"}, client=today_only)
        self.assertEqual((refused.status_code, refused.json()["error"]["code"]), (503, "agents_today_detail_unavailable"))

    def test_the_feature_is_a_late_one(self):
        from loopdy_plugin.native_context import LATE_FEATURES
        self.assertIn(agents_today.DETAIL_CAPABILITY, LATE_FEATURES)
        self.assertEqual(agents_today.DETAIL_CAPABILITY, "native-agents-today-detail-v1")


if __name__ == "__main__":
    unittest.main()
