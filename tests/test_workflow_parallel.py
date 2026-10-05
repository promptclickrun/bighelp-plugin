"""Parallel blocks: several agent stages at once, then one decision reads all their verdicts."""
from __future__ import annotations

import copy
import signal
import unittest

from loopdy_plugin import workflow_model as model
from workflow_fixtures import Engine

TOPIC = {"topic": "How small teams use checklists"}


def take(key: str, role: str, title: str) -> dict:
    return {"key": key, "kind": "agent", "title": title, "role": role, "instructions": f"Give {title}.",
            "uses": ["inputs.topic"],
            "outputs": [{"name": "take", "type": "markdown_file"},
                        {"name": "decision", "type": "decision", "values": ["pass", "changes"]},
                        {"name": "notes", "type": "notes"}]}


def parallel_definition() -> dict:
    return {
        "schemaVersion": 2, "name": "Three takes", "description": "Three agents at once, then one answer.",
        "roles": [{"key": "researcher", "label": "Researcher"}, {"key": "writer", "label": "Writer"},
                  {"key": "reviewer", "label": "Reviewer"}],
        "inputs": [{"key": "topic", "label": "Topic", "type": "text", "required": True}],
        "limits": {"stageMinutes": 20, "maxRevisions": 1},
        "stages": [
            {"key": "takes", "kind": "parallel", "title": "Three takes", "next": "vote",
             "branches": [take("take_a", "researcher", "the facts"), take("take_b", "writer", "a story"),
                          take("take_c", "reviewer", "the risks")]},
            {"key": "vote", "kind": "decision", "title": "All three agree?",
             "on": ["take_a.decision", "take_b.decision", "take_c.decision"],
             "pass": "combine", "changes": {"goTo": "takes"}},
            {"key": "combine", "kind": "agent", "title": "One answer", "role": "writer",
             "instructions": "Combine the three takes.", "uses": ["take_a.take", "take_b.take", "take_c.take"],
             "outputs": [{"name": "answer", "type": "markdown_file"}], "next": None},
        ],
    }


def handoff(decision: str = "pass", notes=()) -> dict:
    return {"take": {"content": "# A take\n\nIt helps."}, "decision": decision, "notes": list(notes)}


class ParallelModelTests(unittest.TestCase):
    def validate(self, definition):
        return model.validate(model.parse_definition(definition))

    def test_a_parallel_block_and_a_decision_on_its_verdicts_are_valid(self):
        result = self.validate(parallel_definition())
        self.assertTrue(result["valid"], result["issues"])
        parsed = model.parse_definition(parallel_definition())
        self.assertEqual(model.top_key(parsed, "take_b"), "takes")
        self.assertEqual(model.stage_by_key(parsed, "take_c")["title"], "the risks")
        self.assertEqual(model.decision_sources(model.stage_by_key(parsed, "vote")),
                         ["take_a.decision", "take_b.decision", "take_c.decision"])

    def test_agents_in_a_block_cant_read_each_other_and_a_block_needs_two(self):
        definition = parallel_definition()
        definition["stages"][0]["branches"][1]["uses"] = ["take_a.take"]
        codes = [issue["code"] for issue in self.validate(definition)["issues"]]
        self.assertIn("uses_parallel", codes)
        definition = parallel_definition()
        del definition["stages"][0]["branches"][1:]
        definition["stages"][1]["on"] = ["take_a.decision"]
        definition["stages"][2]["uses"] = ["take_a.take"]
        codes = [issue["code"] for issue in self.validate(definition)["issues"]]
        self.assertIn("parallel_branches", codes)

    def test_parallel_blocks_need_the_stage_graph_and_agents_only(self):
        definition = parallel_definition()
        definition["schemaVersion"] = 1
        del definition["stages"][0]["next"]
        with self.assertRaises(model.DefinitionError):
            model.parse_definition(definition)
        definition = parallel_definition()
        definition["stages"][0]["branches"][0] = {"key": "look", "kind": "check", "title": "Look", "rules": []}
        with self.assertRaises(model.DefinitionError):
            model.parse_definition(definition)
        definition = parallel_definition()
        definition["stages"][1]["require"] = "most"
        with self.assertRaises(model.DefinitionError):
            model.parse_definition(definition)

    def test_the_three_takes_template_is_a_valid_parallel_workflow(self):
        definition = model.parse_definition(model.template("three-takes"))
        self.assertTrue(model.validate(definition)["valid"])
        self.assertEqual([stage["kind"] for stage in definition["stages"]], ["parallel", "decision", "agent", "signoff"])

    def test_any_passes_with_one_verdict(self):
        stage = {"on": ["a.decision", "b.decision"], "require": "any"}
        self.assertTrue(model.decision_passes(stage, ["changes", "pass"]))
        self.assertFalse(model.decision_passes({"on": stage["on"]}, ["changes", "pass"]))


class ParallelRunTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(self)
        self.engine.start()
        self.workflow_id, self.revision = self.engine.workflow(parallel_definition(), name="Three takes")
        self.run_id = self.engine.run(self.workflow_id, self.revision, inputs=TOPIC)

    def live(self):
        return [process for process in self.engine.host.processes.values() if process.alive]

    def stages(self):
        return {stage["key"]: stage for stage in self.engine.detail(self.run_id)["stages"]}

    def test_all_agents_start_at_once_and_the_decision_waits_for_every_one(self):
        self.engine.tick()
        self.assertEqual(len(self.live()), 3, "The three agents run at the same time")
        self.assertEqual(self.engine.state(self.run_id)[1], "takes")
        self.assertEqual({key: stage.get("group") for key, stage in self.stages().items()
                          if key.startswith("take_")}, {"take_a": "takes", "take_b": "takes", "take_c": "takes"})
        first, second, third = sorted(self.live(), key=lambda process: process.pid)
        self.engine.host.reply(first.pid, handoff())
        self.engine.host.reply(second.pid, handoff())
        self.engine.tick(2)
        self.assertEqual(self.engine.state(self.run_id), ("running", "takes"), "It waits for the third")
        self.assertEqual(self.stages()["take_a"]["state"], "accepted")
        self.engine.host.reply(third.pid, handoff())
        self.engine.tick(3)
        self.assertEqual(self.engine.state(self.run_id)[1], "combine", "All three passed, so the run goes on")
        combine = self.engine.host.latest()
        brief = (combine.cwd / "brief.md").read_text()
        self.assertIn("the risks: take", brief, "The next stage reads every agent's work")
        self.engine.finish_stage({"answer": {"content": "# One answer\n\nUse checklists."}})
        self.engine.tick()
        self.assertEqual(self.engine.state(self.run_id)[0], "succeeded")
        self.assertEqual(self.engine.detail(self.run_id)["stagesDone"], 3)

    def test_one_verdict_asking_for_changes_runs_the_whole_block_again(self):
        self.engine.tick()
        processes = sorted(self.live(), key=lambda process: process.pid)
        self.engine.host.reply(processes[0].pid, handoff())
        self.engine.host.reply(processes[1].pid, handoff("changes", [{"severity": "major", "text": "Too vague."}]))
        self.engine.host.reply(processes[2].pid, handoff())
        self.engine.tick(4)
        again = self.live()
        self.assertEqual(len(again), 3, "Sent back to the block: all three run again")
        self.assertEqual(self.engine.detail(self.run_id)["iteration"], 2)
        self.assertIn("Too vague.", (again[0].cwd / "brief.md").read_text())

    def test_a_failed_agent_stops_the_others_and_a_retry_runs_only_the_unfinished(self):
        self.engine.tick()
        first, second, third = sorted(self.live(), key=lambda process: process.pid)
        self.engine.host.reply(first.pid, handoff())
        self.engine.tick()
        self.engine.host.crash(second.pid, code=2)
        self.engine.tick(2)
        run = self.engine.detail(self.run_id)
        self.assertEqual((run["state"], run["stageKey"], run["failure"]["stageKey"]), ("failed", "takes", "take_b"))
        self.assertIn(signal.SIGTERM, third.signals, "Nothing waits for the third agent any more")
        self.engine.tick(2)
        self.assertEqual(self.stages()["take_c"]["state"], "cancelled")
        self.assertEqual(self.stages()["take_a"]["state"], "accepted")
        before = len(self.engine.host.processes)
        self.engine.store.control(self.run_id, "retry", self.engine.detail(self.run_id)["version"])
        self.engine.tick()
        started = len(self.engine.host.processes) - before
        self.assertEqual(started, 2, "The finished agent keeps its work; the other two run again")
        self.assertEqual(self.stages()["take_a"]["state"], "accepted")

    def test_cancel_stops_every_agent_and_says_so_once(self):
        self.engine.tick()
        run = self.engine.detail(self.run_id)
        self.engine.store.control(self.run_id, "cancel", run["version"])
        self.engine.tick(3)
        self.assertEqual(self.engine.state(self.run_id)[0], "cancelled")
        self.assertFalse(self.live())
        events = self.engine.store.events(self.run_id, 0, 200)["events"]
        self.assertEqual(sum(1 for event in events if event["kind"] == "cancelled"), 1)


if __name__ == "__main__":
    unittest.main()
