"""A decision's ways can end the run: succeeded, cancelled or failed, with a note."""
from __future__ import annotations

import unittest

from loopdy_plugin import workflow_model as model
from workflow_fixtures import Engine

TOPIC = {"topic": "Release 2.4"}


def outcomes_definition(changes=None, passing="write") -> dict:
    """Look for new pull requests; when there are none, there is nothing to write."""
    return {
        "schemaVersion": 2, "name": "Release notes", "description": "Notes from new pull requests.",
        "roles": [{"key": "researcher", "label": "Researcher"}, {"key": "writer", "label": "Writer"},
                  {"key": "reviewer", "label": "Reviewer"}],
        "inputs": [{"key": "topic", "label": "Release", "type": "text", "required": True}],
        "limits": {"stageMinutes": 20, "maxRevisions": 1},
        "stages": [
            {"key": "look", "kind": "agent", "title": "Look for new pull requests", "role": "researcher",
             "instructions": "List the pull requests merged since the last release. Say pass when there are some.",
             "uses": ["inputs.topic"],
             "outputs": [{"name": "list", "type": "text"},
                         {"name": "decision", "type": "decision", "values": ["pass", "changes"]},
                         {"name": "notes", "type": "notes"}],
             "next": "anything"},
            {"key": "anything", "kind": "decision", "title": "Anything new?", "on": "look.decision",
             "pass": passing,
             "changes": changes or {"end": "succeeded", "message": "No new pull requests, so nothing to write."}},
            {"key": "write", "kind": "agent", "title": "Write the notes", "role": "writer",
             "instructions": "Write release notes.", "uses": ["look.list"],
             "outputs": [{"name": "notes_text", "type": "text"}], "next": None},
        ],
    }


class OutcomeModelTests(unittest.TestCase):
    def test_either_way_of_a_decision_can_end_the_run(self):
        result = model.validate(model.parse_definition(outcomes_definition()))
        self.assertTrue(result["valid"], result["issues"])
        definition = outcomes_definition(changes={"goTo": "look"}, passing={"end": "succeeded"})
        codes = [issue["code"] for issue in model.validate(model.parse_definition(definition))["issues"]]
        self.assertNotIn("pass_invalid", codes)
        self.assertIn("unreachable_stage", codes, "Both ways skip the writer, so nothing leads to it")

    def test_only_the_three_outcomes(self):
        with self.assertRaises(model.DefinitionError):
            model.parse_definition(outcomes_definition(changes={"end": "skipped"}))
        with self.assertRaises(model.DefinitionError):
            model.parse_definition(outcomes_definition(changes={"end": "failed", "goTo": "look"}))

    def test_an_ending_way_counts_as_an_end(self):
        definition = outcomes_definition()
        definition["stages"][2]["next"] = "look"
        codes = [issue["code"] for issue in model.validate(model.parse_definition(definition))["issues"]]
        self.assertNotIn("no_end", codes)


class OutcomeRunTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(self)
        self.engine.start()

    def run_until_decision(self, definition, decision, notes=()):
        workflow_id, revision = self.engine.workflow(definition, name="Release notes")
        run_id = self.engine.run(workflow_id, revision, inputs=TOPIC)
        self.engine.tick()
        self.engine.finish_stage({"list": "", "decision": decision, "notes": list(notes)})
        self.engine.tick(2)
        return run_id

    def outcome(self, run_id):
        return {stage["key"]: stage for stage in self.engine.detail(run_id)["stages"]}["anything"].get("outcome")

    def test_no_new_pull_requests_ends_the_run_as_succeeded_with_its_note(self):
        run_id = self.run_until_decision(outcomes_definition(), "changes")
        run = self.engine.detail(run_id)
        self.assertEqual((run["state"], run["stageKey"]), ("succeeded", "anything"))
        self.assertEqual(self.outcome(run_id), {"end": "succeeded", "note": "No new pull requests, so nothing to write."})
        events = self.engine.store.events(run_id, 0, 200)["events"]
        self.assertEqual([event["text"] for event in events if event["kind"] == "succeeded"],
                         ["No new pull requests, so nothing to write."], "The alert says why, once")
        self.assertEqual(len(self.engine.host.processes), 1, "The writer never started")

    def test_without_a_message_the_verdict_note_says_why(self):
        definition = outcomes_definition(changes={"end": "cancelled"})
        run_id = self.run_until_decision(definition, "changes", [{"severity": "minor", "text": "Nothing merged."}])
        self.assertEqual(self.engine.detail(run_id)["state"], "cancelled")
        self.assertEqual(self.outcome(run_id), {"end": "cancelled", "note": "Nothing merged."})

    def test_failed_can_be_tried_again_from_the_stage_that_decided(self):
        run_id = self.run_until_decision(outcomes_definition(changes={"end": "failed"}), "changes")
        run = self.engine.detail(run_id)
        self.assertEqual((run["state"], run["failure"]["code"]), ("failed", "decision_failed"))
        self.assertEqual(run["failure"]["message"], "Anything new? failed the run.")
        self.engine.store.control(run_id, "retry", run["version"])
        self.engine.tick()
        self.assertEqual(self.engine.state(run_id), ("running", "look"), "Try again asks the same stage again")
        self.assertIsNone(self.outcome(run_id), "The old outcome is gone")

    def test_a_passing_way_can_end_the_run_too(self):
        definition = outcomes_definition(passing={"end": "succeeded"}, changes={"goTo": "look"})
        del definition["stages"][2]
        run_id = self.run_until_decision(definition, "pass")
        self.assertEqual(self.engine.detail(run_id)["state"], "succeeded")
        self.assertEqual(self.outcome(run_id)["note"], "Anything new?: the run is done.")


if __name__ == "__main__":
    unittest.main()
