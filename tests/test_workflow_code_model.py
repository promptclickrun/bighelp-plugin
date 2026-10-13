"""Dev Factory recipes (schemaVersion 3): their shape, validation and the built-in templates."""
from __future__ import annotations

import copy
import unittest

from loopdy_plugin import workflow_model as model
from workflow_fixtures import code_definition


REPO = "/private/tmp/fixture-app"


def codes(validation) -> list[str]:
    return [issue["code"] for issue in validation["issues"]]


def stage(definition: dict, key: str) -> dict:
    return next(item for item in definition["stages"] if item["key"] == key)


class CodeDefinitionTests(unittest.TestCase):
    def validate(self, definition, **host):
        return model.validate(model.parse_definition(definition), **host)

    def test_a_recipe_parses_with_its_projects_and_workspace(self):
        parsed = model.parse_definition(code_definition(REPO))
        self.assertEqual(parsed["projects"], [{"key": "app", "label": "App", "path": REPO}])
        self.assertEqual(parsed["workspace"], {"placement": "worktree"})
        self.assertEqual(stage(parsed, "tests")["run"], {"*": "sh check.sh"})
        self.assertEqual(stage(parsed, "review")["changes"], True)
        self.assertEqual(model.validate(parsed), {"valid": True, "host": False, "issues": []})
        defaulted = code_definition(REPO)
        del defaulted["workspace"]
        self.assertEqual(model.parse_definition(defaulted)["workspace"], {"placement": "worktree"})
        local = model.parse_definition(code_definition(REPO, placement="local", workspace={
            "placement": "local", "base": "release/2.0"}))
        self.assertEqual(local["workspace"], {"placement": "local", "base": "release/2.0"})

    def test_versions_one_and_two_refuse_the_new_fields(self):
        for version in (1, 2):
            definition = code_definition(REPO)
            definition["schemaVersion"] = version
            with self.subTest(version=version), self.assertRaises(model.DefinitionError):
                model.parse_definition(definition)  # projects and workspace are version 3 fields
            del definition["projects"], definition["workspace"]
            with self.subTest(version=version), self.assertRaises(model.DefinitionError):
                model.parse_definition(definition)  # and so are command and pull request stages
            plain = {"schemaVersion": version, "name": "x", "stages": [
                {"key": "s", "kind": "signoff", "title": "S", "changes": True}]}
            with self.assertRaises(model.DefinitionError):
                model.parse_definition(plain)
            for kind in ({"kind": "command", "run": "make test"}, {"kind": "pull_request"}):
                with self.assertRaises(model.DefinitionError):
                    model.parse_definition({"schemaVersion": version, "name": "x", "stages": [
                        {"key": "s", "title": "S", **kind}]})
        with self.assertRaises(model.DefinitionError):
            model.parse_definition({"schemaVersion": 4, "name": "x", "stages": [], "projects": []})

    def test_shapes_are_checked(self):
        for change in (
            lambda d: d.pop("projects"),
            lambda d: d["projects"].append({"key": "web", "label": "Web"}),
            lambda d: d["projects"].append({"key": "Web App", "label": "Web", "path": "/private/tmp/web"}),
            lambda d: d["projects"].append({"key": "web", "label": "Web", "path": "relative/web"}),
            lambda d: d["projects"].append({"key": "web", "label": "Web", "path": "/private/tmp/../etc"}),
            lambda d: d["projects"].append({"key": "web", "label": "Web", "path": "/private/tmp/web",
                                            "extra": 1}),
            lambda d: d["projects"].extend({"key": f"p{index}", "label": "P", "path": f"/private/tmp/p{index}"}
                                           for index in range(10)),
            lambda d: d.update(workspace={"placement": "cloud"}),
            lambda d: d.update(workspace={"placement": "worktree", "base": "-x"}),
            lambda d: d.update(workspace={"placement": "worktree", "base": "a..b"}),
            lambda d: d.update(workspace={"placement": "worktree", "extra": 1}),
            lambda d: stage(d, "tests").update(run=""),
            lambda d: stage(d, "tests").update(run="make 'test"),
            lambda d: stage(d, "tests").update(run={"Bad Key": "make test"}),
            lambda d: stage(d, "tests").update(run={}),
            lambda d: stage(d, "tests").update(run=["make", "test"]),
            lambda d: stage(d, "tests").update(minutes=61),
            lambda d: stage(d, "tests").update(shell=True),
            lambda d: stage(d, "tests")["fix"].update(maxTries=4),
            lambda d: stage(d, "tests")["fix"].update(tools=["terminal"]),
            lambda d: stage(d, "tests").update(fix={"instructions": "x"}),
            lambda d: stage(d, "review").update(file="fix.title"),
            lambda d: stage(d, "review").pop("changes"),
            lambda d: stage(d, "review").update(changes=False),
            lambda d: stage(d, "open_pr").update(titleFrom="inputs.task"),
            lambda d: stage(d, "open_pr").update(draft="yes"),
            lambda d: stage(d, "open_pr").update(body="x"),
        ):
            definition = code_definition(REPO)
            change(definition)
            with self.subTest(definition=definition), self.assertRaises(model.DefinitionError):
                model.parse_definition(definition)

    def test_validation_codes(self):
        empty = code_definition(REPO, projects=[])
        self.assertEqual(codes(self.validate(empty)), ["no_projects"])
        self.assertFalse(self.validate(empty)["valid"])

        twice = code_definition(REPO, projects=[{"key": "app", "label": "App", "path": REPO},
                                                {"key": "app", "label": "App 2", "path": "/private/tmp/app2"}])
        self.assertIn("duplicate_key", codes(self.validate(twice)))

        unknown = code_definition(REPO)
        stage(unknown, "tests")["run"] = {"web": "npm test"}
        self.assertEqual([(issue.get("stageKey"), issue["code"]) for issue in self.validate(unknown)["issues"]],
                         [("tests", "command_project"), ("tests", "command_missing")])

        fixer = code_definition(REPO)
        stage(fixer, "tests")["fix"] = {"role": "pilot", "instructions": " "}
        self.assertEqual(codes(self.validate(fixer)), ["role_unknown", "instructions_missing"])

        early = code_definition(REPO)
        stage(early, "fix")["next"] = "open_pr"
        stage(early, "open_pr")["next"] = "tests"
        stage(early, "review")["next"] = None
        found = codes(self.validate(early))
        self.assertIn("pull_request_signoff", found)

        title = code_definition(REPO)
        stage(title, "open_pr")["titleFrom"] = "fix.missing"
        self.assertEqual(codes(self.validate(title)), ["pull_request_title"])

    def test_this_computer_needs_each_project_and_terminal_is_no_warning(self):
        definition = code_definition(REPO)
        stage(definition, "fix")["tools"] = ["terminal", "file"]
        validation = self.validate(definition, bindings={"coder": "writer"}, profile_exists=lambda agent: True,
                                   repository=lambda path: False)
        self.assertEqual((validation["valid"], validation["host"]), (True, False))
        self.assertEqual(codes(validation), ["project_missing"])
        ready = self.validate(definition, bindings={"coder": "writer"}, profile_exists=lambda agent: True,
                              repository=lambda path: path == REPO)
        self.assertEqual(ready, {"valid": True, "host": True, "issues": []})
        unbound = self.validate(definition, bindings={}, repository=lambda path: True)
        self.assertEqual(codes(unbound), ["role_unbound"])

    def test_a_command_finds_its_command_for_each_project(self):
        definition = model.parse_definition(code_definition(REPO))
        tests = stage(definition, "tests")
        self.assertEqual(model.project_command(tests, "app"), "sh check.sh")
        tests["run"] = {"*": "make test", "web": "npm test"}
        self.assertEqual((model.project_command(tests, "web"), model.project_command(tests, "app")),
                         ("npm test", "make test"))
        tests["run"] = "pytest -q"
        self.assertEqual(model.project_command(tests, "web"), "pytest -q")
        self.assertEqual(model.fix_tries(tests), 2)
        self.assertEqual(model.fix_tries({"kind": "command", "run": "make", "fix": {"role": "r", "instructions": "i"}}),
                         2)
        self.assertEqual(model.fix_tries({"kind": "command", "run": "make"}), 0)
        self.assertEqual(model.stage_minutes(definition, tests), 20)
        self.assertEqual(model.fill_project("In {project.label} ({project.key})", {"key": "app", "label": "App"}),
                         "In App (app)")

    def test_changes_go_back_to_the_nearest_agent_before_the_signoff(self):
        definition = model.parse_definition(code_definition(REPO))
        self.assertEqual(model.changes_target(definition, "review"), "fix")
        only_tests = code_definition(REPO, stages=[
            {"key": "tests", "kind": "command", "title": "Tests", "run": "make test", "next": "review"},
            {"key": "review", "kind": "signoff", "title": "Review", "changes": True, "next": None}],
            roles=[])
        self.assertEqual(model.changes_target(model.parse_definition(only_tests), "review"), "tests")

    def test_templates_start_without_projects(self):
        for template_id in ("fix-test-pr", "bump-dependency", "test-sweep"):
            definition = model.template(template_id)
            self.assertEqual((definition["schemaVersion"], definition["projects"]), (3, []))
            parsed = model.parse_definition(definition)
            self.assertEqual(codes(model.validate(parsed)), ["no_projects"], template_id)
            filled = copy.deepcopy(definition)
            filled["projects"] = [{"key": "app", "label": "App", "path": REPO}]
            self.assertTrue(model.validate(model.parse_definition(filled))["valid"], template_id)
        listed = [item["id"] for item in model.template_summaries()]
        self.assertEqual(listed, ["research-draft-review", "three-takes"])
        self.assertEqual([item["id"] for item in model.template_summaries(include_code=True)][2:],
                         ["fix-test-pr", "bump-dependency", "test-sweep"])


if __name__ == "__main__":
    unittest.main()
