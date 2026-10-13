"""Dev Factory runs (schemaVersion 3): one per git project, each in its own worktree, in real throwaway repositories.

Agents are fakes (FakeHost); test commands and the pull request step really run, against a local bare `origin`
and a stand-in `gh`. Nothing reaches GitHub.
"""
from __future__ import annotations

import base64
from pathlib import Path
import sqlite3
import unittest

from loopdy_plugin.workflow_store import WorkflowError, WorkflowStore
from workflow_fixtures import Engine, code_definition, fake_gh, git, git_project, graph_definition


def stage(definition: dict, key: str) -> dict:
    return next(item for item in definition["stages"] if item["key"] == key)


class CodeRunTests(unittest.TestCase):
    """Runs of a recipe: one per project, each in its own worktree, walking the stages on its own."""

    def setUp(self):
        self.engine = Engine(self)
        self.engine.host.real_enabled = True
        self.engine.start()
        self.store = self.engine.store
        self.repo = git_project(self)

    def publish(self, definition: dict) -> tuple[str, int]:
        saved = self.store.save_draft(None, 0, definition, self.engine.facts)
        if any(role["key"] == "coder" for role in definition["roles"]):
            self.store.bind(saved["workflowId"], "coder", "writer")
        return saved["workflowId"], self.store.publish(saved["workflowId"], saved["draftVersion"],
                                                        self.engine.facts)["revision"]

    def start(self, definition=None, token="6f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b", projects=None) -> dict:
        workflow_id, revision = self.publish(definition or code_definition(str(self.repo)))
        return self.store.start_run(workflow_id, revision, {"task": "Make the check pass"}, token, False,
                                    self.engine.facts, projects=projects)

    def run_id(self, definition=None) -> str:
        return self.start(definition)["runs"][0]["run_id"]

    def error(self, call, *args, **kwargs) -> WorkflowError:
        with self.assertRaises(WorkflowError) as caught:
            call(*args, **kwargs)
        return caught.exception

    def attempts(self, run_id: str, key: str) -> list[dict]:
        return next(item for item in self.engine.detail(run_id)["stages"] if item["key"] == key)["attempts"]

    @staticmethod
    def brief(process) -> str:
        return Path(process.argv[process.argv.index("--query-file") + 1]).read_text()

    def fix_stage(self, *, fixed: bool, title: str = "Make the check pass"):
        """The coding agent's turn: it works in the project folder and hands off a title."""
        agent = self.engine.host.latest()
        if fixed:
            (agent.cwd / "fixed.txt").write_text("fixed\n")
        self.engine.finish_stage({"title": title})
        return agent

    def to_review(self, run_id: str) -> dict:
        self.engine.tick()
        self.fix_stage(fixed=True)
        return self.engine.wait(run_id, lambda run: run["state"] == "waiting_for_you")

    # MARK: Starting

    def test_start_makes_one_run_per_project_in_one_group(self):
        web = git_project(self, "web")
        definition = code_definition(str(self.repo), projects=[
            {"key": "app", "label": "App", "path": str(self.repo)}, {"key": "web", "label": "Web", "path": str(web)}])
        started = self.start(definition)
        self.assertRegex(started["group_id"], r"^grp_[0-9a-f]{16}$")
        self.assertEqual([item["project"] for item in started["runs"]], ["app", "web"])
        self.assertEqual(started["run"]["id"], started["runs"][0]["run_id"])
        for item, path in zip(started["runs"], (self.repo, web)):
            run = self.engine.detail(item["run_id"])
            self.assertEqual((run["state"], run["group_id"], run["workspace"], run["pull_request"], run["reason"]),
                             ("planned", started["group_id"], None, None, None))
            self.assertEqual(run["project"]["path"], str(path))
        self.assertEqual(len({self.engine.detail(item["run_id"])["number"] for item in started["runs"]}), 2)
        workflow_id = started["run"]["workflowId"]
        again = self.store.start_run(workflow_id, 1, {"task": "x"}, "6f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b", False,
                                     self.engine.facts)
        self.assertEqual(again["group_id"], started["group_id"], "A start sent again is the same start")
        self.assertEqual(again["runs"], started["runs"])
        one = self.store.start_run(workflow_id, 1, {"task": "x"}, "7f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b", False,
                                   self.engine.facts, projects=["web"])
        self.assertEqual([item["project"] for item in one["runs"]], ["web"])
        for projects in (["nope"], ["web", "web"], []):
            with self.subTest(projects=projects):
                self.assertEqual(self.error(self.store.start_run, workflow_id, 1, {"task": "x"},
                                            "8f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b", False, self.engine.facts,
                                            projects=projects).code, "projects_invalid")
        listed = self.store.list_runs(workflow_id, "active", None, 20)["runs"]
        self.assertEqual({run["project"]["key"] for run in listed}, {"app", "web"})

    def test_older_workflows_take_no_projects_and_show_no_code_fields(self):
        workflow_id, revision = self.engine.workflow(graph_definition())
        self.assertEqual(self.error(self.store.start_run, workflow_id, revision, {"topic": "x"},
                                    "9f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b", False, self.engine.facts,
                                    projects=["app"]).code, "projects_invalid")
        started = self.store.start_run(workflow_id, revision, {"topic": "x"}, "9f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b",
                                       False, self.engine.facts)
        self.assertEqual(set(started), {"run"})
        for key in ("group_id", "project", "workspace", "pull_request", "reason"):
            self.assertNotIn(key, started["run"])
            self.assertNotIn(key, self.engine.detail(started["run"]["id"]))

    def test_a_missing_project_folder_blocks_the_start(self):
        definition = code_definition("/private/tmp/bighelp-no-such-project")
        workflow_id, revision = self.publish(definition)
        self.assertEqual(self.error(self.store.start_run, workflow_id, revision, {"task": "x"},
                                    "6f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b", False, self.engine.facts).code,
                         "not_valid")

    # MARK: Workspaces and agents

    def test_each_run_gets_its_own_worktree_and_its_agent_works_there(self):
        run_id = self.run_id()
        self.engine.tick()
        run = self.engine.detail(run_id)
        self.assertEqual((run["state"], run["stageKey"]), ("running", "fix"))
        suffix = run_id[4:10]
        folder = self.repo / ".worktrees" / f"factory-fix-and-ship-{suffix}"
        self.assertEqual(run["workspace"], {"placement": "worktree", "path": str(folder),
                                            "branch": f"factory/fix-and-ship-{suffix}", "base": "main"})
        self.assertEqual(git(folder, "rev-parse", "--abbrev-ref", "HEAD").strip(), f"factory/fix-and-ship-{suffix}")
        agent = self.engine.host.latest()
        self.assertEqual(agent.cwd, folder)
        self.assertEqual(agent.env["TERMINAL_CWD"], str(folder))
        self.assertEqual(agent.argv[agent.argv.index("--toolsets") + 1], "file,terminal,todo")
        brief_path = Path(agent.argv[agent.argv.index("--query-file") + 1])
        self.assertTrue(brief_path.is_absolute())
        self.assertEqual(agent.env["HERMES_QUIET_TURN_REPORT_FILE"], str(brief_path.parent / "report.json"))
        brief = brief_path.read_text()
        self.assertIn(f"You work in the project folder {folder}", brief)
        self.assertIn("You may read and change files there", brief)
        self.assertIn("Fix the task in App (app).", brief)
        self.assertIn("Make the check pass", brief)
        self.assertIn(f"{brief_path.parent}/out/", brief)
        events = [event["kind"] for event in self.store.events(run_id, 0, 50)["events"]]
        self.assertIn("workspace_ready", events)

    def test_a_workspace_that_cant_be_made_needs_attention(self):
        definition = code_definition(str(self.repo), workspace={"placement": "worktree", "base": "no-such-branch"})
        run_id = self.run_id(definition)
        self.engine.tick()
        run = self.engine.detail(run_id)
        self.assertEqual((run["state"], run["attention"]["code"]), ("needs_attention", "workspace_failed"))
        self.assertEqual(run["reason"], "App has no branch no-such-branch to start from.")
        self.assertIn("retry", run["allowedActions"])
        self.assertEqual(self.engine.host.processes, {}, "No agent started")

    def test_local_placement_works_in_the_repository_on_its_branch(self):
        run_id = self.run_id(code_definition(str(self.repo), placement="local"))
        self.engine.tick()
        run = self.engine.detail(run_id)
        self.assertEqual(run["workspace"], {"placement": "local", "path": str(self.repo), "branch": "main",
                                            "base": "main"})
        self.assertEqual(self.engine.host.latest().cwd, self.repo)

    # MARK: Commands

    def test_tests_that_pass_go_on_to_your_review_of_the_changes(self):
        run_id = self.run_id()
        run = self.to_review(run_id)
        self.assertEqual((run["stageKey"], run["reason"]), ("review", "Look at the changes in App and approve them."))
        command = self.engine.host.real_runs[0]
        self.assertEqual(command["argv"], ["sh", "check.sh"])
        self.assertEqual(command["cwd"], Path(run["workspace"]["path"]))
        self.assertEqual(command["env"]["CI"], "1")
        self.assertNotIn("OPENAI_API_KEY", command["env"])
        self.assertNotIn("PYTHONPATH", command["env"])
        (attempt,) = self.attempts(run_id, "tests")
        self.assertEqual((attempt["kind"], attempt["exit_code"], attempt["tries"]), ("command", 0, 0))
        log = self.store.read_artifact(run_id, attempt["log"]["sha256"], 0, 1000)
        self.assertIn("all 3 tests pass", base64.b64decode(log["data"]).decode())
        self.assertEqual(run["signoff"]["changes"], {"files": 1, "additions": 1, "deletions": 0,
                                                     "branch": run["workspace"]["branch"], "base": "main"})
        self.assertRegex(run["signoff"]["sha256"], r"^[0-9a-f]{64}$")
        review = next(item for item in run["stages"] if item["key"] == "review")
        self.assertEqual(review["changes"], run["signoff"]["changes"])
        self.assertEqual([item["state"] for item in run["stages"]],
                         ["accepted", "accepted", "waiting_for_you", "pending"])

    def test_failing_tests_get_a_fix_and_run_again(self):
        run_id = self.run_id()
        self.engine.tick()
        self.fix_stage(fixed=False)
        run = self.engine.wait(run_id, lambda run: len(self.engine.host.processes) == 2)
        self.assertEqual((run["state"], run["stageKey"]), ("running", "tests"))
        fixer = self.engine.host.latest()
        self.assertEqual(fixer.cwd, Path(run["workspace"]["path"]))
        brief = self.brief(fixer)
        self.assertIn("Make the tests pass.", brief)
        self.assertIn("FAIL: fixed.txt is missing", brief)
        self.assertIn("`sh check.sh` failed with exit code 1", brief)
        (fixer.cwd / "fixed.txt").write_text("fixed\n")
        self.engine.finish_stage({"summary": "Added fixed.txt."})
        run = self.engine.wait(run_id, lambda run: run["state"] == "waiting_for_you")
        self.assertEqual(run["stageKey"], "review")
        attempts = self.attempts(run_id, "tests")
        self.assertEqual([(item["kind"], item.get("exit_code"), item.get("tries")) for item in attempts],
                         [("command", 1, 0), ("fix", None, None), ("command", 0, 1)])
        self.assertEqual(attempts[1]["agentId"], "writer")
        outputs = {(item["stageKey"], item["name"]): item for item in run["outputs"]}
        self.assertEqual(outputs[("tests", "summary")]["value"], "Added fixed.txt.")
        kinds = [event["kind"] for event in self.store.events(run_id, 0, 100)["events"]]
        self.assertIn("command_failed", kinds)
        self.assertIn("command_passed", kinds)

    def test_tests_that_still_fail_wait_for_you_and_retry_runs_them_again(self):
        run_id = self.run_id()
        self.engine.tick()
        self.fix_stage(fixed=False)
        for _ in range(2):
            self.engine.wait(run_id, lambda run: self.engine.host.latest().alive)
            self.engine.finish_stage({"summary": "Tried something."})
        run = self.engine.wait(run_id, lambda run: run["state"] == "waiting_for_you")
        self.assertEqual((run["stageKey"], run["reason"]), ("tests", "Tests: still failing after 2 tries."))
        self.assertEqual(run["waiting"]["kind"], "command")
        self.assertEqual(set(run["allowedActions"]), {"cancel", "retry", "pause"})
        self.assertNotIn("signoff", run)
        self.assertEqual(self.error(self.store.signoff, run_id, "tests", "approve", "0" * 64, "").code, "not_waiting")
        self.assertEqual(len(self.engine.host.real_runs), 3)
        # You fix it by hand and try again: the command runs again (with its fix tries back).
        (Path(run["workspace"]["path"]) / "fixed.txt").write_text("fixed\n")
        retried = self.store.control(run_id, "retry", run["version"])["run"]
        self.assertEqual((retried["state"], retried["stageKey"], retried["reason"]), ("planned", "tests", None))
        run = self.engine.wait(run_id, lambda run: run["stageKey"] == "review")
        self.assertEqual(run["state"], "waiting_for_you")

    def test_tests_without_a_fix_wait_for_you_at_once_and_can_be_cancelled(self):
        definition = code_definition(str(self.repo))
        del stage(definition, "tests")["fix"]
        run_id = self.run_id(definition)
        self.engine.tick()
        self.fix_stage(fixed=False)
        run = self.engine.wait(run_id, lambda run: run["state"] == "waiting_for_you")
        self.assertEqual(run["reason"], "Tests: failed with exit code 1.")
        cancelled = self.store.control(run_id, "cancel", run["version"])["run"]
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue(Path(run["workspace"]["path"]).is_dir(), "Worktrees are never deleted by themselves")

    def test_a_command_that_runs_too_long_is_stopped(self):
        definition = code_definition(str(self.repo), run="sleep 120")
        del stage(definition, "tests")["fix"]
        stage(definition, "tests")["minutes"] = 1
        run_id = self.run_id(definition)
        self.engine.tick()
        self.fix_stage(fixed=True)
        self.engine.wait(run_id, lambda run: bool(self.engine.host.real_runs))
        self.engine.tick(seconds=61)
        run = self.engine.wait(run_id, lambda run: run["state"] == "waiting_for_you")
        self.assertEqual(run["reason"], "Tests: ran out of time (1 min).")
        (attempt,) = self.attempts(run_id, "tests")
        self.assertEqual(attempt["state"], "timed_out")

    # MARK: Your review

    def test_approval_binds_to_the_changes_you_saw(self):
        run_id = self.run_id()
        run = self.to_review(run_id)
        seen = run["signoff"]["sha256"]
        (Path(run["workspace"]["path"]) / "late.txt").write_text("changed after you looked\n")
        self.assertEqual(self.error(self.store.signoff, run_id, "review", "approve", seen, "").code, "approval_stale")
        self.assertEqual(self.engine.detail(run_id)["state"], "waiting_for_you")
        (Path(run["workspace"]["path"]) / "late.txt").unlink()
        approved = self.store.signoff(run_id, "review", "approve", seen, "")["run"]
        self.assertEqual((approved["state"], approved["stageKey"]), ("planned", "open_pr"))

    def test_asking_for_changes_goes_back_to_the_coding_agent(self):
        run_id = self.run_id()
        run = self.to_review(run_id)
        back = self.store.signoff(run_id, "review", "changes", run["signoff"]["sha256"], "Name the file better.")
        self.assertEqual((back["run"]["state"], back["run"]["stageKey"], back["run"]["iteration"]),
                         ("planned", "fix", 2))
        self.engine.tick()
        brief = self.brief(self.engine.host.latest())
        self.assertIn("[major] Name the file better.", brief)

    # MARK: Pull requests

    def approve(self, run_id: str, gh: Path | None) -> dict:
        run = self.to_review(run_id)
        path = "/usr/bin:/bin" if gh is None else f"{gh}:/usr/bin:/bin"
        self.engine.coordinator.environ["PATH"] = path
        self.store.signoff(run_id, "review", "approve", run["signoff"]["sha256"], "")
        return self.engine.wait(run_id, lambda run: run["state"] in ("succeeded", "waiting_for_you", "failed",
                                                                     "needs_attention"))

    def test_the_pull_request_step_commits_pushes_and_opens_it(self):
        gh = fake_gh(self, number=12)
        run_id = self.run_id()
        run = self.approve(run_id, gh)
        self.assertEqual(run["state"], "succeeded", run.get("reason"))
        self.assertEqual(run["pull_request"], {"url": "https://github.example/fixture/app/pull/12", "number": 12})
        branch = run["workspace"]["branch"]
        origin = self.repo.parent / "app-origin.git"
        self.assertEqual(git(origin, "log", "-1", "--format=%s", branch).strip(), "Make the check pass")
        self.assertIn("fixed.txt", git(origin, "show", "--name-only", "--format=", branch))
        calls = (gh / "calls.txt").read_text().splitlines()
        self.assertEqual(calls[0], "auth status")
        self.assertTrue(calls[1].startswith(f"pr create --base main --head {branch} --title Make the check pass "
                                            "--body-file "), calls[1])
        step = self.engine.host.real_runs[-1]
        self.assertEqual(step["argv"][1], "-I")
        self.assertEqual(step["cwd"], Path(run["workspace"]["path"]))
        self.assertNotIn("OPENAI_API_KEY", step["env"])
        kinds = [event["kind"] for event in self.store.events(run_id, 0, 100)["events"]]
        self.assertIn("pull_request_opened", kinds)

    def test_without_the_github_cli_the_run_waits_and_nothing_is_committed(self):
        run_id = self.run_id()
        run = self.approve(run_id, None)
        self.assertEqual((run["state"], run["stageKey"]), ("waiting_for_you", "open_pr"))
        self.assertEqual(run["reason"], "Install the GitHub CLI (gh) on this computer to open pull requests.")
        self.assertIn("fixed.txt", git(run["workspace"]["path"], "status", "--porcelain"))
        gh = fake_gh(self)
        self.engine.coordinator.environ["PATH"] = f"{gh}:/usr/bin:/bin"
        self.store.control(run_id, "retry", run["version"])
        run = self.engine.wait(run_id, lambda run: run["state"] == "succeeded")
        self.assertEqual(run["pull_request"]["number"], 7)

    def test_a_signed_out_github_cli_waits_for_you(self):
        run = self.approve(self.run_id(), fake_gh(self, signed_in=False))
        self.assertEqual(run["reason"], "Sign in to the GitHub CLI on this computer (gh auth login) to open pull "
                                        "requests.")

    def test_local_placement_on_the_base_branch_refuses_to_open_one(self):
        gh = fake_gh(self)
        run = self.approve(self.run_id(code_definition(str(self.repo), placement="local")), gh)
        self.assertEqual(run["reason"], "Pick a new worktree or a branch other than main to open a pull request.")
        self.assertNotIn("pr create", (gh / "calls.txt").read_text())


class CodeStoreTests(unittest.TestCase):
    def test_a_version_three_store_gains_the_code_columns(self):
        engine = Engine(self)
        workflow_id, revision = engine.workflow()
        run_id = engine.run(workflow_id, revision)
        with sqlite3.connect(str(engine.store.database)) as connection:
            for column in ("group_id", "project_json", "workspace_json", "pull_request_json", "waiting_reason"):
                connection.execute(f"ALTER TABLE runs DROP COLUMN {column}")
            connection.execute("ALTER TABLE attempts DROP COLUMN tries")
            connection.execute("PRAGMA user_version=3")
        upgraded = WorkflowStore(engine.root, clock=engine.clock)
        self.assertEqual(upgraded.get_run(run_id)["run"]["state"], "planned")
        with upgraded.read() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertIsNone(connection.execute("SELECT group_id FROM runs").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
