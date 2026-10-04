from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from loopdy_plugin import workflow_model as model


VECTORS = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "workflows-v1"


def template():
    return model.template("research-draft-review")


def codes(validation):
    return [issue["code"] for issue in validation["issues"]]


class DefinitionShapeTests(unittest.TestCase):
    def test_template_matches_the_shared_vector_and_parses_unchanged(self):
        vector = json.loads((VECTORS / "template-research-draft-review.json").read_text())
        self.assertEqual(vector, model.RESEARCH_DRAFT_REVIEW)
        self.assertEqual(model.parse_definition(template()), template())

    def test_defaults_are_filled_and_unknown_fields_refused(self):
        parsed = model.parse_definition({"schemaVersion": 1, "name": "Tiny", "stages": []})
        self.assertEqual(parsed["limits"], {"stageMinutes": 20, "maxRevisions": 2})
        self.assertEqual((parsed["roles"], parsed["inputs"], parsed["description"]), ([], [], ""))
        for broken in (
            {"schemaVersion": 2, "name": "x", "stages": []},
            {"schemaVersion": 1, "name": "x", "stages": [], "extra": 1},
            {"schemaVersion": 1, "name": "x", "stages": [{"key": "a", "kind": "robot", "title": "A"}]},
            {"schemaVersion": 1, "name": "x", "stages": [{"key": "A b", "kind": "check", "title": "A", "rules": []}]},
            {"schemaVersion": 1, "name": "x", "limits": {"stageMinutes": 61}, "stages": []},
            {"schemaVersion": True, "name": "x", "stages": []},
            {"schemaVersion": 1, "name": "x", "stages": [{"key": "a", "kind": "agent", "title": "A", "role": "r",
                                                         "instructions": "i", "outputs": [], "minutes": 0}]},
            {"schemaVersion": 1, "name": "x", "stages": [{"key": "a", "kind": "agent", "title": "A", "role": "r",
                                                         "instructions": "i", "outputs": [
                                                             {"name": "n", "type": "text", "values": ["a"]}]}]},
            {"schemaVersion": 1, "name": "x", "stages": [{"key": "c", "kind": "check", "title": "C", "rules": [
                {"type": "word_range", "of": "inputs.topic", "min": 1, "max": 2}]}]},
        ):
            with self.subTest(broken=broken), self.assertRaises(model.DefinitionError):
                model.parse_definition(broken)

    def test_size_and_stage_count_are_bounded(self):
        many = template()
        many["stages"] = [copy.deepcopy(many["stages"][0]) for _ in range(21)]
        with self.assertRaises(model.DefinitionError):
            model.parse_definition(many)
        large = template()
        for stage in large["stages"]:
            if stage["kind"] == "agent":
                stage["instructions"] = "x" * 8000
        large["stages"] = [dict(large["stages"][0], key=f"s{index}") for index in range(9)]
        with self.assertRaisesRegex(model.DefinitionError, "too large"):
            model.parse_definition(large)


class ValidationTests(unittest.TestCase):
    def test_template_is_valid_but_needs_agents_on_this_computer(self):
        unbound = model.validate(template(), bindings={})
        self.assertTrue(unbound["valid"])
        self.assertFalse(unbound["host"])
        self.assertEqual(codes(unbound), ["role_unbound"] * 3)
        bound = model.validate(template(), bindings={"researcher": "a", "writer": "b", "reviewer": "c"},
                               profile_exists=lambda agent: agent != "c", toolset_known=lambda name: True)
        self.assertEqual(codes(bound), ["agent_missing"])
        ready = model.validate(template(), bindings={"researcher": "a", "writer": "b", "reviewer": "c"},
                               profile_exists=lambda agent: True, toolset_known=lambda name: True)
        self.assertEqual(ready, {"valid": True, "host": True, "issues": []})

    def test_references_point_only_backwards(self):
        definition = template()
        definition["stages"][0]["uses"].append("draft.draft")
        definition["stages"][1]["uses"].append("research.summary")
        definition["stages"][1]["uses"].append("inputs.missing")
        issues = model.validate(definition)["issues"]
        self.assertIn(("research", "use_forward"), [(item["stageKey"], item["code"]) for item in issues])
        self.assertEqual(codes({"issues": [item for item in issues if item.get("stageKey") == "draft"]}),
                         ["use_unknown", "use_unknown"])
        self.assertFalse(model.validate(definition)["valid"])

    def test_dangerous_tools_are_refused_and_terminal_warned(self):
        definition = template()
        definition["stages"][0]["tools"] = ["web", "terminal", "cronjob", "kanban", "delegation", "messaging",
                                            "send_message", "homeassistant", "hermes-telegram", "clarify"]
        validation = model.validate(definition, bindings={"researcher": "a", "writer": "b", "reviewer": "c"},
                                    profile_exists=lambda agent: True,
                                    toolset_known=lambda name: name in {"web", "terminal"})
        found = codes(validation)
        self.assertEqual(found.count("tool_not_allowed"), 8)
        self.assertEqual(found.count("tool_terminal"), 1)
        terminal = next(item for item in validation["issues"] if item["code"] == "tool_terminal")
        self.assertEqual(terminal["severity"], "warning")
        self.assertFalse(validation["valid"])

    def test_unknown_toolset_only_blocks_this_computer(self):
        definition = template()
        definition["stages"][0]["tools"] = ["web", "sparkles"]
        validation = model.validate(definition, bindings={"researcher": "a", "writer": "b", "reviewer": "c"},
                                    profile_exists=lambda agent: True, toolset_known=lambda name: name == "web")
        self.assertTrue(validation["valid"])
        self.assertFalse(validation["host"])
        self.assertEqual(codes(validation), ["toolset_unknown"])

    def test_decision_check_and_signoff_targets(self):
        definition = template()
        stages = {stage["key"]: stage for stage in definition["stages"]}
        stages["review_decision"]["on"] = "review.notes"
        stages["review_decision"]["changes"]["goTo"] = "signoff"
        stages["review_decision"]["pass"] = "research"
        stages["length_check"]["rules"].append({"type": "number_range", "of": "draft.draft", "min": 5, "max": 1})
        stages["signoff"]["file"] = "draft.word_count"
        found = codes(model.validate(definition))
        for code in ("decision_source", "goto_invalid", "pass_invalid", "check_target", "check_range",
                     "signoff_file"):
            self.assertIn(code, found)

    def test_decision_needs_pass_and_changes_values(self):
        definition = template()
        definition["stages"][3]["outputs"][0]["values"] = ["yes", "no"]
        self.assertIn("decision_values", codes(model.validate(definition)))


class InputTests(unittest.TestCase):
    def test_inputs_are_checked_and_optional_text_defaults_empty(self):
        definition = template()
        self.assertEqual(model.parse_inputs(definition, {"topic": "Checklists", "length": "Long"}),
                         {"topic": "Checklists", "audience": "", "length": "Long"})
        for broken in ({"length": "Long"}, {"topic": "x", "length": "Huge"}, {"topic": 4, "length": "Long"},
                       {"topic": "x" * 2001, "length": "Long"}, {"topic": "x", "length": "Long", "extra": 1}, []):
            with self.subTest(broken=broken), self.assertRaises(model.InputsError):
                model.parse_inputs(definition, broken)


if __name__ == "__main__":
    unittest.main()
