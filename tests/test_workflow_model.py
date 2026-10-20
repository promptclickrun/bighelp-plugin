from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest

from loopdy_plugin import workflow_model as model
from workflow_fixtures import graph_definition


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
            {"schemaVersion": 3, "name": "x", "stages": []},
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


class GraphTests(unittest.TestCase):
    """schemaVersion 2: `next` edges, a layout, and checks on the whole graph."""

    def stage(self, definition, key):
        return next(stage for stage in definition["stages"] if stage["key"] == key)

    def test_graph_definition_parses_as_is_and_is_valid(self):
        definition = graph_definition()
        parsed = model.parse_definition(definition)
        self.assertEqual(parsed["layout"], definition["layout"])
        self.assertIsNone(self.stage(parsed, "signoff")["next"])
        self.assertNotIn("next", self.stage(parsed, "draft"))
        self.assertEqual(model.validate(parsed), {"valid": True, "host": False, "issues": []})
        self.assertEqual(model.forward_graph(parsed), {"research": ["draft"], "signoff": [], "draft": ["review"],
                                                       "review": ["review_decision"],
                                                       "review_decision": ["signoff"]})
        self.assertEqual(model.stages_between(parsed, "draft", "review_decision"),
                         ["draft", "review", "review_decision"])
        self.assertEqual(model.next_stage_key(parsed, "signoff"), None)
        self.assertEqual(model.next_stage_key(parsed, "draft"), "review")

    def test_next_and_layout_are_checked_for_shape(self):
        for change in (
            lambda d: d.update(schemaVersion=1),  # v1 has no next or layout
            lambda d: self.stage(d, "research").update(next="Not a key"),
            lambda d: self.stage(d, "review_decision").update(next="signoff"),
            lambda d: d["layout"]["stages"].update(draft={"x": 100001, "y": 0}),
            lambda d: d["layout"]["stages"].update(draft={"x": float("nan"), "y": 0}),
            lambda d: d["layout"]["stages"].update(draft={"x": 1}),
            lambda d: d["layout"].update(extra={}),
            lambda d: d["layout"]["stages"].update({f"s{index}": {"x": 0, "y": 0} for index in range(16)}),
        ):
            definition = graph_definition()
            change(definition)
            with self.subTest(definition=definition), self.assertRaises(model.DefinitionError):
                model.parse_definition(definition)

    def test_create_from_scratch_saves_with_no_stages(self):
        empty = {"schemaVersion": 2, "name": "New workflow", "description": "", "roles": [], "inputs": [],
                 "limits": {"stageMinutes": 20, "maxRevisions": 2}, "stages": []}
        parsed = model.parse_definition(empty)
        self.assertEqual(parsed, empty)
        self.assertEqual(codes(model.validate(parsed)), ["no_stages"])

    def issues(self, definition):
        validation = model.validate(model.parse_definition(definition))
        self.assertFalse(validation["valid"])
        return [(item.get("stageKey"), item["code"]) for item in validation["issues"]]

    def test_graph_issue_codes(self):
        unreachable = graph_definition()
        unreachable["stages"].append({"key": "orphan", "kind": "agent", "title": "Orphan", "role": "writer",
                                      "instructions": "x", "outputs": [{"name": "x", "type": "text"}]})
        self.assertEqual(self.issues(unreachable), [("orphan", "unreachable_stage")])

        looped = graph_definition()
        self.stage(looped, "review_decision")["pass"] = "research"
        self.assertEqual(self.issues(looped), [("research", "cycle"), ("signoff", "unreachable_stage"),
                                               (None, "no_end")])

        unknown = graph_definition()
        self.stage(unknown, "research")["next"] = "nowhere"
        found = self.issues(unknown)
        self.assertEqual(found[0], ("research", "next_unknown"))
        self.assertIn(("draft", "unreachable_stage"), found)

        back = graph_definition()
        back["stages"].append({"key": "summary", "kind": "agent", "title": "Summary", "role": "writer",
                               "instructions": "x", "outputs": [{"name": "x", "type": "text"}], "next": None})
        self.stage(back, "signoff")["next"] = "summary"
        self.stage(back, "review_decision")["changes"]["goTo"] = "summary"
        self.assertEqual(self.issues(back), [("review_decision", "goto_not_earlier")])

        uses = graph_definition()
        self.stage(uses, "research")["uses"].append("review.notes")
        self.assertEqual(self.issues(uses), [("research", "uses_not_before")])

        late_file = graph_definition()
        # Listed before the stage that writes it is fine; running before it isn't.
        self.stage(late_file, "research")["next"] = "signoff"
        self.stage(late_file, "signoff")["next"] = "draft"
        self.stage(late_file, "review_decision")["pass"] = "next"
        found = self.issues(late_file)
        self.assertIn(("signoff", "signoff_file"), found)

    def test_this_computer_without_tool_scope_blocks_agent_stages(self):
        validation = model.validate(model.parse_definition(graph_definition()), bindings={}, tool_scope=False)
        self.assertTrue(validation["valid"])
        self.assertFalse(validation["host"])
        self.assertEqual(codes(validation).count("tool_scope_unsupported"), 3)

    def test_version_one_keeps_its_rules(self):
        definition = template()
        # Unreachable is a schemaVersion 2 check: a version 1 workflow means "each stage goes to the next one".
        definition["stages"].insert(5, {"key": "skipped", "kind": "agent", "title": "Skipped", "role": "writer",
                                        "instructions": "x", "outputs": [{"name": "x", "type": "text"}]})
        definition["stages"][4]["pass"] = "signoff"
        definition = model.parse_definition(definition)
        self.assertEqual(model.validate(definition, bindings={"researcher": "a", "writer": "b", "reviewer": "c"},
                                        profile_exists=lambda agent: True, toolset_known=lambda name: True),
                         {"valid": True, "host": True, "issues": []})


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
