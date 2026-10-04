"""The coordinator on a fake process host and a fake clock: nothing real is started here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import stat
import threading
import unittest
from unittest.mock import patch

from loopdy_plugin import workflow_model as model
from loopdy_plugin import workflow_coordinator
from loopdy_plugin.workflow_store import WorkflowStore
from workflow_fixtures import DRAFT, Engine, FakeHost


BRIEF = {"brief": {"content": "# Brief\n\nThree facts and two sources."}}
GOOD_DRAFT = {"draft": {"content": DRAFT}, "word_count": 280}


class HappyPathTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(self)
        self.workflow_id, self.revision = self.engine.workflow()
        self.engine.start()

    def test_template_runs_to_signoff_and_approve_finishes(self):
        run_id = self.engine.run(self.workflow_id, self.revision)
        self.engine.tick()
        self.assertEqual(self.engine.state(run_id), ("running", "research"))
        process = self.engine.host.latest()
        home = self.engine.home
        self.assertEqual(process.argv, ["/fixture/bin/hermes", "-p", "research", "--cli", "chat", "--source",
                                        "workflow", "--toolsets", "web", "--query-file", "brief.md", "--format",
                                        "stream-json"])
        self.assertEqual(process.env["HERMES_HOME"], str(home / "profiles" / "research"))
        self.assertEqual(process.env["TERMINAL_CWD"], str(process.cwd))
        self.assertEqual(process.env["HERMES_SESSION_SOURCE"], "workflow")
        self.assertEqual(process.env["HERMES_QUIET_TURN_REPORT_FILE"], str(process.cwd / "report.json"))
        self.assertNotIn("OPENAI_API_KEY", process.env)
        self.assertNotIn("PYTHONPATH", process.env)
        brief = (process.cwd / "brief.md").read_text()
        self.assertIn("How small teams use checklists", brief)
        self.assertIn("```bighelp-handoff", brief)
        self.assertEqual(stat.S_IMODE(os.stat(process.cwd).st_mode), 0o700)

        self.engine.finish_stage(BRIEF)
        self.assertEqual(self.engine.state(run_id), ("running", "draft"))
        draft = self.engine.host.latest()
        self.assertIn("--toolsets", draft.argv)
        self.assertEqual(draft.argv[draft.argv.index("--toolsets") + 1], "todo")
        self.assertEqual((draft.cwd / "inputs" / "research.brief.md").read_text(), BRIEF["brief"]["content"])
        self.assertEqual(stat.S_IMODE(os.stat(draft.cwd / "inputs" / "research.brief.md").st_mode), 0o400)
        self.assertIn("Three facts and two sources.", (draft.cwd / "brief.md").read_text())

        self.engine.finish_stage(GOOD_DRAFT)
        self.assertEqual(self.engine.state(run_id), ("accepted", "length_check"))
        self.engine.tick()
        self.assertEqual(self.engine.state(run_id), ("running", "review"))
        self.engine.finish_stage({"decision": "pass", "notes": [{"severity": "minor", "text": "Nice."}]})
        self.assertEqual(self.engine.state(run_id), ("accepted", "review_decision"))
        self.engine.tick()
        detail = self.engine.detail(run_id)
        self.assertEqual((detail["state"], detail["stageKey"], detail["stagesDone"]),
                         ("waiting_for_you", "signoff", 5))
        self.assertEqual(detail["waiting"]["kind"], "signoff")
        self.assertEqual(detail["signoff"]["artifact"]["wordCount"], len(DRAFT.split()))
        self.assertEqual(detail["signoff"]["reviewNotes"], [{"severity": "minor", "text": "Nice."}])
        self.assertNotIn("previous", detail["signoff"])
        self.assertEqual(detail["tokens"], {"in": 3600, "out": 900})
        self.assertEqual([stage["state"] for stage in detail["stages"]],
                         ["accepted"] * 5 + ["waiting_for_you"])
        values = {output["name"]: output.get("value") for output in detail["outputs"]}
        self.assertEqual((values["word_count"], values["decision"]), (280, "pass"))
        self.assertEqual(self.engine.store.status()["slots"], {"used": 0, "total": 2})

        approved = self.engine.store.signoff(run_id, "signoff", "approve", detail["signoff"]["artifact"]["sha256"],
                                             "")["run"]
        self.assertEqual(approved["state"], "succeeded")
        kinds = [event["kind"] for event in self.engine.store.events(run_id, 0, 200)["events"]]
        self.assertEqual(kinds[0], "run_planned")
        self.assertEqual(kinds[-3:], ["waiting_for_you", "approved", "succeeded"])
        for kind in ("stage_launched", "stage_running", "stage_checking", "stage_accepted", "check_passed",
                     "decision_pass"):
            self.assertIn(kind, kinds)
        with self.engine.store.read() as connection:
            lines = connection.execute("SELECT kind, text FROM live_lines ORDER BY id").fetchall()
        self.assertIn(("tool", 'web_search {"query": "checklists"}'), [tuple(row) for row in lines])
        self.assertIn(("text", "Working on it."), [tuple(row) for row in lines])

    def test_report_without_exit_is_booked_after_a_short_grace(self):
        run_id = self.engine.run(self.workflow_id, self.revision)
        self.engine.tick()
        process = self.engine.host.latest()
        self.engine.host.reply(process.pid, BRIEF, exit=False)
        self.engine.tick()
        self.assertEqual(self.engine.state(run_id), ("running", "research"))
        self.engine.tick(seconds=6)
        self.assertEqual(process.signals, [signal.SIGTERM])
        self.engine.tick()
        self.assertEqual(self.engine.state(run_id), ("running", "draft"))

    def test_live_lines_are_capped_per_attempt(self):
        self.engine.run(self.workflow_id, self.revision)
        self.engine.tick()
        process = self.engine.host.latest()
        with open(process.cwd / "stream.jsonl", "a") as handle:
            for number in range(700):
                handle.write(json.dumps({"type": "tool_use", "name": f"tool{number}"}) + "\n")
        self.engine.tick()
        with self.engine.store.read() as connection:
            rows = connection.execute("SELECT text FROM live_lines ORDER BY seq").fetchall()
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows[-1]["text"], "tool699")


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(self)
        self.workflow_id, self.revision = self.engine.workflow()
        self.engine.start()

    def test_changes_go_back_with_notes_and_the_limit_needs_attention(self):
        run_id = self.engine.run(self.workflow_id, self.revision)
        notes = [{"severity": "major", "text": "Cut the second section."}]
        self.engine.through_review(run_id, "changes", notes)
        run = self.engine.detail(run_id)
        self.assertEqual((run["state"], run["stageKey"], run["iteration"]), ("running", "draft", 2))
        brief = (self.engine.host.latest().cwd / "brief.md").read_text()
        self.assertIn("[major] Cut the second section.", brief)
        self.assertIn("round 2", brief)
        self.assertEqual(self.engine.host.latest().cwd.name, "2-2")
        for round_number in (3, 4):
            self.engine.finish_stage(GOOD_DRAFT)
            self.engine.tick()
            self.engine.finish_stage({"decision": "changes", "notes": notes})
            self.engine.tick()
            if round_number == 3:
                self.assertEqual(self.engine.state(run_id), ("running", "draft"))
        run = self.engine.detail(run_id)
        self.assertEqual((run["state"], run["stageKey"]), ("needs_attention", "review_decision"))
        self.assertEqual(run["attention"]["code"], "revision_limit")
        self.assertEqual(run["allowedActions"], ["cancel", "retry"])
        retried = self.engine.store.control(run_id, "retry", run["version"])["run"]
        self.assertEqual((retried["state"], retried["stageKey"], retried["iteration"]), ("planned", "draft", 4))
        self.engine.tick()
        self.assertIn("Cut the second section.", (self.engine.host.latest().cwd / "brief.md").read_text())
        self.engine.finish_stage(GOOD_DRAFT)
        self.engine.tick()
        self.engine.finish_stage({"decision": "pass", "notes": []})
        self.engine.tick()
        detail = self.engine.detail(run_id)
        self.assertEqual(detail["state"], "waiting_for_you")
        self.assertEqual(detail["signoff"]["previous"]["iteration"], 3)

    def test_signoff_changes_send_the_writer_your_notes(self):
        run_id = self.engine.run(self.workflow_id, self.revision)
        self.engine.through_review(run_id)
        detail = self.engine.detail(run_id)
        digest = detail["signoff"]["artifact"]["sha256"]
        run = self.engine.store.signoff(run_id, "signoff", "changes", digest, "Add a closing tip.")["run"]
        self.assertEqual((run["state"], run["stageKey"], run["iteration"]), ("planned", "draft", 2))
        self.engine.tick()
        brief = (self.engine.host.latest().cwd / "brief.md").read_text()
        self.assertIn("The person who runs this workflow sent this back", brief)
        self.assertIn("Add a closing tip.", brief)
        self.engine.finish_stage({"draft": {"content": DRAFT + "\nA closing tip."}, "word_count": 283})
        self.engine.tick()
        self.engine.finish_stage({"decision": "pass", "notes": []})
        self.engine.tick()
        detail = self.engine.detail(run_id)
        self.assertEqual(detail["signoff"]["history"][0]["decision"], "changes")
        self.assertEqual(detail["signoff"]["history"][0]["artifactSha256"], digest)
        self.assertEqual(detail["signoff"]["previous"]["sha256"], digest)


class FailureTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(self)
        self.workflow_id, self.revision = self.engine.workflow()
        self.engine.start()
        self.run_id = self.engine.run(self.workflow_id, self.revision)
        self.engine.tick()

    def test_contract_failure_then_retry_is_a_new_attempt(self):
        self.engine.finish_stage({"summary": "wrong output"})
        run = self.engine.detail(self.run_id)
        self.assertEqual((run["state"], run["failure"]["code"], run["failure"]["stageKey"]),
                         ("failed", "contract_missing_output", "research"))
        self.assertEqual(run["stages"][0]["attempts"][0]["outcomeCode"], "contract_missing_output")
        self.assertEqual(run["allowedActions"], ["retry"])
        self.engine.store.control(self.run_id, "retry", run["version"])
        self.engine.tick()
        run = self.engine.detail(self.run_id)
        self.assertEqual((run["state"], len(run["stages"][0]["attempts"])), ("running", 2))
        self.assertEqual(self.engine.host.latest().cwd.name, "1-2")

    def test_nonzero_exit_fails_the_stage(self):
        self.engine.finish_stage(BRIEF, exit_code=1)
        self.assertEqual(self.engine.detail(self.run_id)["failure"]["code"], "agent_exit")
        crashed = self.engine.run(self.workflow_id, self.revision, client_run="6f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0c")
        self.engine.tick()
        self.engine.host.crash(self.engine.host.latest().pid, code=3)
        self.engine.tick()
        self.assertEqual(self.engine.detail(crashed)["failure"]["code"], "agent_exit")

    def test_failed_check_retries_the_stage_it_reads(self):
        self.engine.finish_stage(BRIEF)
        self.engine.finish_stage({"draft": {"content": "# Too short\n\nTiny."}, "word_count": 4})
        run = self.engine.detail(self.run_id)
        self.assertEqual((run["state"], run["failure"]["code"], run["stageKey"]),
                         ("failed", "check_failed", "length_check"))
        self.assertIn("words", run["failure"]["message"])
        self.engine.store.control(self.run_id, "retry", run["version"])
        self.engine.tick()
        self.assertEqual(self.engine.state(self.run_id), ("running", "draft"))

    def test_run_file_budget_is_enforced(self):
        with patch.object(workflow_coordinator, "MAX_RUN_ARTIFACT_BYTES", 10):
            self.engine.finish_stage(BRIEF)
        self.assertEqual(self.engine.detail(self.run_id)["failure"]["code"], "storage_full")

    def test_timeout_stops_the_process_group_then_kills_it(self):
        process = self.engine.host.latest()
        process.ignore_term = True
        self.engine.tick(seconds=20 * 60)
        self.assertEqual(process.signals, [signal.SIGTERM])
        self.assertEqual(self.engine.state(self.run_id), ("running", "research"))
        self.engine.tick(seconds=31)
        self.assertEqual(process.signals, [signal.SIGTERM, signal.SIGKILL])
        self.engine.tick()
        run = self.engine.detail(self.run_id)
        self.assertEqual((run["state"], run["failure"]["code"]), ("failed", "timed_out"))
        self.assertEqual(run["stages"][0]["attempts"][0]["state"], "timed_out")

    def test_cancel_stops_a_running_stage(self):
        run = self.engine.detail(self.run_id)
        requested = self.engine.store.control(self.run_id, "cancel", run["version"])["run"]
        self.assertEqual(requested["state"], "running")
        self.assertNotIn("cancel", self.engine.detail(self.run_id)["allowedActions"])
        self.engine.tick()
        self.assertEqual(self.engine.host.latest().signals, [signal.SIGTERM])
        self.engine.tick()
        run = self.engine.detail(self.run_id)
        self.assertEqual((run["state"], run["stages"][0]["attempts"][0]["state"]), ("cancelled", "cancelled"))

    def test_missing_agent_needs_attention_and_spawn_failure_fails(self):
        self.engine.finish_stage(BRIEF)
        self.engine.host.crash(self.engine.host.latest().pid, 1)
        self.engine.tick()
        os.remove(self.engine.home / "profiles" / "writer" / "config.yaml")
        run = self.engine.detail(self.run_id)
        self.engine.store.control(self.run_id, "retry", run["version"])
        self.engine.tick()
        run = self.engine.detail(self.run_id)
        self.assertEqual((run["state"], run["attention"]["code"]), ("needs_attention", "agent_missing"))
        (self.engine.home / "profiles" / "writer" / "config.yaml").write_text("model: fixture\n")
        self.engine.host.fail_spawn = True
        self.engine.store.control(self.run_id, "retry", run["version"])
        self.engine.tick()
        run = self.engine.detail(self.run_id)
        self.assertEqual((run["state"], run["failure"]["code"]), ("failed", "spawn_failed"))


class AdmissionTests(unittest.TestCase):
    def test_two_slots_oldest_first_and_waiting_runs_use_none(self):
        engine = Engine(self)
        quick = {"schemaVersion": 1, "name": "Quick note", "roles": [{"key": "writer", "label": "Writer"}],
                 "stages": [{"key": "write", "kind": "agent", "title": "Write", "role": "writer",
                             "instructions": "Write a note.", "outputs": [{"name": "note", "type": "markdown_file"}]},
                            {"key": "ok", "kind": "signoff", "title": "Approve", "file": "write.note"}]}
        workflow_id = engine.store.save_draft(None, 0, quick, engine.facts)["workflowId"]
        engine.store.bind(workflow_id, "writer", "writer")
        revision = engine.store.publish(workflow_id, 1, engine.facts)["revision"]
        engine.start()
        runs = [engine.run(workflow_id, revision, client_run=f"6f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a{number:02d}",
                           inputs={}) for number in range(4)]
        engine.store.control(runs[3], "pause", engine.detail(runs[3])["version"])
        engine.tick()
        self.assertEqual([engine.state(run)[0] for run in runs], ["running", "running", "planned", "planned"])
        self.assertEqual(engine.store.status()["slots"], {"used": 2, "total": 2})
        first = engine.host.processes[min(engine.host.processes)]
        engine.host.reply(first.pid, {"note": {"content": "# Note"}})
        engine.tick()
        self.assertEqual([engine.state(run)[0] for run in runs],
                         ["waiting_for_you", "running", "running", "planned"])
        self.assertEqual(engine.store.status()["slots"], {"used": 2, "total": 2})
        engine.store.control(runs[1], "cancel", engine.detail(runs[1])["version"])
        engine.tick(2)
        self.assertEqual(engine.state(runs[1])[0], "cancelled")
        self.assertEqual(engine.state(runs[3])[0], "planned")
        self.assertTrue(engine.detail(runs[3])["paused"])
        engine.store.control(runs[3], "resume", engine.detail(runs[3])["version"])
        engine.tick()
        self.assertEqual(engine.state(runs[3])[0], "running")

    def test_pause_holds_after_the_current_stage(self):
        engine = Engine(self)
        workflow_id, revision = engine.workflow()
        engine.start()
        run_id = engine.run(workflow_id, revision)
        engine.tick()
        engine.store.control(run_id, "pause", engine.detail(run_id)["version"])
        engine.finish_stage(BRIEF)
        engine.tick(3)
        self.assertEqual(engine.state(run_id), ("accepted", "research"))
        engine.store.control(run_id, "resume", engine.detail(run_id)["version"])
        engine.tick()
        self.assertEqual(engine.state(run_id), ("running", "draft"))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(self)
        self.workflow_id, self.revision = self.engine.workflow()
        self.engine.start()
        self.run_id = self.engine.run(self.workflow_id, self.revision)
        self.engine.tick()
        self.process = self.engine.host.latest()
        self.engine.coordinator.release()

    def restart(self, host: FakeHost):
        """A new coordinator process: the old one's children are not its children."""
        for process in host.processes.values():
            process.own = False
        coordinator = self.engine.new_coordinator(host)
        self.assertTrue(coordinator.acquire())
        coordinator.begin()
        self.engine.coordinator = coordinator

    def test_gone_process_without_a_report_needs_attention(self):
        self.engine.host.crash(self.process.pid)
        self.restart(self.engine.host)
        run = self.engine.detail(self.run_id)
        self.assertEqual((run["state"], run["attention"]["code"]), ("needs_attention", "coordinator_restarted"))
        self.assertEqual(run["attention"]["message"], "We don't know how Research the topic ended.")
        self.assertEqual(run["stages"][0]["attempts"][0]["outcomeCode"], "unknown")
        retried = self.engine.store.control(self.run_id, "retry", run["version"])["run"]
        self.assertEqual(retried["state"], "planned")
        self.engine.tick()
        self.assertEqual(len(self.engine.detail(self.run_id)["stages"][0]["attempts"]), 2)

    def test_live_matching_process_is_adopted(self):
        self.restart(self.engine.host)
        self.assertEqual(self.engine.state(self.run_id), ("running", "research"))
        self.assertIn("adopted", [event["kind"] for event in self.engine.store.events(self.run_id, 0, 200)["events"]])
        self.engine.finish_stage(BRIEF)
        self.assertEqual(self.engine.state(self.run_id), ("running", "draft"))

    def test_reused_pid_is_never_adopted_or_signalled(self):
        self.process.start += 999
        self.restart(self.engine.host)
        run = self.engine.detail(self.run_id)
        self.assertEqual((run["state"], run["attention"]["code"]), ("needs_attention", "coordinator_restarted"))
        self.engine.tick(seconds=3600)
        self.assertEqual(self.process.signals, [])

    def test_finished_turn_with_its_report_is_checked(self):
        self.engine.host.reply(self.process.pid, BRIEF)
        self.restart(self.engine.host)
        self.engine.tick()
        self.assertEqual(self.engine.state(self.run_id), ("running", "draft"))

    def test_launch_without_a_recorded_process_needs_attention(self):
        with self.engine.store.transaction() as connection:
            connection.execute("UPDATE attempts SET state='launched', pid=NULL, pid_fingerprint=NULL")
        self.restart(self.engine.host)
        run = self.engine.detail(self.run_id)
        self.assertEqual((run["state"], run["attention"]["code"]), ("needs_attention", "coordinator_restarted"))

    def test_reboot_is_reported_as_host_restarted(self):
        rebooted = FakeHost(boot="boot-2")
        rebooted.next_pid = 52000
        self.restart(rebooted)
        run = self.engine.detail(self.run_id)
        self.assertEqual(run["attention"]["code"], "host_restarted")


class OwnershipTests(unittest.TestCase):
    def test_two_coordinators_race_for_the_lock(self):
        engine = Engine(self)
        engine.workflow()
        first, second = engine.new_coordinator(), engine.new_coordinator()
        results = []
        barrier = threading.Barrier(2)

        def take(coordinator):
            barrier.wait()
            results.append(coordinator.acquire())

        threads = [threading.Thread(target=take, args=(item,)) for item in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [False, True])
        winner = first if first._lock_fd is not None else second
        epoch = winner.begin()
        self.assertTrue(winner.tick())
        loser = second if winner is first else first
        loser._lock_fd = -1  # pretend it got in anyway: the epoch fence still stops it
        loser.epoch = epoch - 1
        self.assertFalse(loser.tick())
        loser._lock_fd = None

    def test_ensure_starts_one_coordinator_only_when_there_is_work(self):
        engine = Engine(self)
        launches = []

        def launcher(root):
            launches.append(root)
            return True

        self.assertEqual(workflow_coordinator.ensure_coordinator(engine.root, launcher=launcher, clock=engine.clock),
                         "offline")
        workflow_id, revision = engine.workflow()
        self.assertEqual(workflow_coordinator.ensure_coordinator(engine.root, launcher=launcher, clock=engine.clock),
                         "offline")
        engine.run(workflow_id, revision)
        for _ in range(3):
            self.assertEqual(workflow_coordinator.ensure_coordinator(engine.root, launcher=launcher,
                                                                     clock=engine.clock), "starting")
        self.assertEqual(launches, [engine.root])
        self.assertEqual(engine.store.status()["coordinator"]["state"], "starting")
        engine.start()
        self.assertEqual(workflow_coordinator.ensure_coordinator(engine.root, launcher=launcher, clock=engine.clock),
                         "online")
        engine.clock.advance(60)
        self.assertEqual(workflow_coordinator.ensure_coordinator(engine.root, launcher=launcher, clock=engine.clock),
                         "starting")
        self.assertEqual(len(launches), 1)
        engine.coordinator.release()
        self.assertEqual(workflow_coordinator.ensure_coordinator(engine.root, launcher=launcher, clock=engine.clock),
                         "starting")
        self.assertEqual(len(launches), 2)

    def test_idle_coordinator_exits(self):
        engine = Engine(self)
        engine.workflow()
        engine.start()
        stop = threading.Event()
        engine.coordinator.run(stop, idle_exit=0)
        self.assertFalse(stop.is_set())

    def test_finished_runs_are_pruned_after_thirty_days(self):
        engine = Engine(self)
        workflow_id, revision = engine.workflow()
        engine.start()
        run_id = engine.run(workflow_id, revision)
        engine.through_review(run_id)
        digest = engine.detail(run_id)["signoff"]["artifact"]["sha256"]
        engine.store.signoff(run_id, "signoff", "approve", digest, "")
        engine.clock.advance(31 * 24 * 3600)
        engine.coordinator.prune()
        self.assertEqual(engine.store.list_runs(None, "all", None, 50)["runs"], [])
        self.assertFalse((engine.store.runs_dir / run_id).exists())
        self.assertEqual([name for name in os.listdir(engine.store.artifacts) if len(name) == 64], [])


if __name__ == "__main__":
    unittest.main()
