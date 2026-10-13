"""Shared fakes for the workflow tests: a Hermes home with agents, a fake clock and a fake process host.

The fake host behaves like Hermes' worker: it writes `stream.jsonl` records and an atomic `report.json`
({pid, exit_code, error, reply}) into the attempt folder, then exits. No Hermes or agent is started; a Dev Factory
command or pull request step (anything that isn't the fixture `hermes`) really runs, in a throwaway repository.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from loopdy_plugin import workflow_coordinator, workflow_runner, workflow_store


AGENTS = ("research", "writer", "editor")
BINDINGS = (("researcher", "research"), ("writer", "writer"), ("reviewer", "editor"))
INPUTS = {"topic": "How small teams use checklists", "audience": "New team leads", "length": "Short"}
DRAFT = "# Checklists for small teams\n\n" + "A checklist keeps the team on track. " * 40


def graph_definition() -> dict:
    """schemaVersion 2: the list order isn't the run order. Research, draft, review, then your sign-off."""
    return {
        "schemaVersion": 2, "name": "Newsletter with a loop", "description": "Draft until the review passes.",
        "roles": [{"key": "researcher", "label": "Researcher"}, {"key": "writer", "label": "Writer"},
                  {"key": "reviewer", "label": "Reviewer"}],
        "inputs": [{"key": "topic", "label": "Topic", "type": "text", "required": True}],
        "limits": {"stageMinutes": 20, "maxRevisions": 2},
        "stages": [
            {"key": "research", "kind": "agent", "title": "Research the topic", "role": "researcher",
             "instructions": "Research the topic.", "tools": ["web"], "uses": ["inputs.topic"],
             "outputs": [{"name": "brief", "type": "markdown_file"}], "next": "draft"},
            {"key": "signoff", "kind": "signoff", "title": "Your sign-off", "file": "draft.draft", "next": None},
            {"key": "draft", "kind": "agent", "title": "Write the draft", "role": "writer",
             "instructions": "Write the draft.", "uses": ["research.brief"],
             "outputs": [{"name": "draft", "type": "markdown_file"}, {"name": "word_count", "type": "number"}]},
            {"key": "review", "kind": "agent", "title": "Review the draft", "role": "reviewer",
             "instructions": "Review the draft.", "uses": ["draft.draft"],
             "outputs": [{"name": "decision", "type": "decision", "values": ["pass", "changes"]},
                         {"name": "notes", "type": "notes"}]},
            {"key": "review_decision", "kind": "decision", "title": "Pass or send back", "on": "review.decision",
             "pass": "signoff", "changes": {"goTo": "draft", "maxRevisions": 1}},
        ],
        "layout": {"inputs": {"x": 0, "y": 0},
                   "stages": {"research": {"x": 0, "y": 120}, "draft": {"x": 0, "y": 240.5},
                              "review": {"x": 0, "y": 360}, "review_decision": {"x": 0, "y": 480},
                              "signoff": {"x": -220, "y": 600}}},
    }


def code_definition(repo: str, *, placement: str = "worktree", run="sh check.sh", stages=None,
                    projects=None, **extra) -> dict:
    """schemaVersion 3, a Dev Factory recipe: fix, test (with a fix agent), your review of the changes, a PR."""
    return {
        "schemaVersion": 3, "name": "Fix and ship", "description": "Fix it, test it, open a pull request.",
        "roles": [{"key": "coder", "label": "Coder"}],
        "inputs": [{"key": "task", "label": "Task", "type": "text", "required": True}],
        "limits": {"stageMinutes": 20, "maxRevisions": 2},
        "projects": projects if projects is not None else [{"key": "app", "label": "App", "path": repo}],
        "workspace": {"placement": placement},
        "stages": stages if stages is not None else [
            {"key": "fix", "kind": "agent", "title": "Fix it", "role": "coder",
             "instructions": "Fix the task in {project.label} ({project.key}).", "uses": ["inputs.task"],
             "outputs": [{"name": "title", "type": "text"}], "next": "tests"},
            {"key": "tests", "kind": "command", "title": "Tests", "run": {"*": run},
             "fix": {"role": "coder", "instructions": "Make the tests pass.", "maxTries": 2}, "next": "review"},
            {"key": "review", "kind": "signoff", "title": "Your review", "changes": True, "next": "open_pr"},
            {"key": "open_pr", "kind": "pull_request", "title": "Open a pull request", "titleFrom": "fix.title",
             "next": None},
        ],
        **extra,
    }


def _git_env(home: Path) -> dict:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
            "LANG": "C"}


def git(cwd, *args: str, home: Path | None = None) -> str:
    """Test-side git with a throwaway HOME, so the person's own git config never applies."""
    home = home or Path(cwd)
    result = subprocess.run(["git", *args], cwd=str(cwd), env=_git_env(home), capture_output=True, check=True)
    return result.stdout.decode()


# A project's test command: passes once fixed.txt exists, and says why it fails until then.
CHECK_SCRIPT = 'if [ -f fixed.txt ]; then echo "all 3 tests pass"; else echo "FAIL: fixed.txt is missing"; exit 1; fi\n'


def git_project(test, name: str = "app", files: dict | None = None) -> Path:
    """A throwaway repository on `main` with one commit, pushed to a bare `origin` next to it."""
    folder = Path(tempfile.mkdtemp(prefix="bighelp-factory-", dir=Path(tempfile.gettempdir()).resolve()))
    test.addCleanup(shutil.rmtree, folder, True)
    repo, origin = folder / name, folder / f"{name}-origin.git"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main", home=folder)
    for key, value in (("user.name", "Fixture Person"), ("user.email", "fixture@example.com"),
                       ("commit.gpgsign", "false")):
        git(repo, "config", key, value, home=folder)
    for path, text in (files or {"README.md": "# App\n", "check.sh": CHECK_SCRIPT}).items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    git(repo, "add", "-A", home=folder)
    git(repo, "commit", "-q", "-m", "First commit", home=folder)
    git(folder, "init", "-q", "--bare", str(origin), home=folder)
    git(repo, "remote", "add", "origin", str(origin), home=folder)
    git(repo, "push", "-q", "-u", "origin", "main", home=folder)
    return repo


def fake_gh(test, *, signed_in: bool = True, number: int = 7) -> Path:
    """A folder with a stand-in `gh` (never the real GitHub CLI): it records its arguments and prints a URL."""
    folder = Path(tempfile.mkdtemp(prefix="bighelp-fake-gh-", dir=Path(tempfile.gettempdir()).resolve()))
    test.addCleanup(shutil.rmtree, folder, True)
    script = folder / "gh"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{folder}/calls.txt"\n'
        'case "$1 $2" in\n'
        f'  "auth status") exit {0 if signed_in else 1} ;;\n'
        f'  "pr create") echo "https://github.example/fixture/app/pull/{number}" ;;\n'
        '  "pr view") exit 1 ;;\n'
        'esac\n')
    script.chmod(0o755)
    return folder


class Clock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float = 1.0) -> None:
        self.value += seconds


class FakeProcess:
    def __init__(self, pid, argv, env, cwd, start):
        self.pid, self.argv, self.env, self.cwd, self.start = pid, argv, env, Path(cwd), start
        self.alive, self.code, self.own, self.signals = True, None, True, []
        self.ignore_term = False


FAKE_HERMES = "/fixture/bin/hermes"


class FakeHost:
    def __init__(self, boot: str = "boot-1"):
        self.processes: dict[int, FakeProcess] = {}
        self.next_pid = 41000
        self.boot = boot
        self.fail_spawn = False
        # Dev Factory tests turn this on: their commands and pull request step really run, in a throwaway
        # repository. Anything that looks like Hermes stays fake, always.
        self.real_enabled = False
        self.real = workflow_runner.OSProcessHost()
        self.real_runs: list[dict] = []

    @staticmethod
    def _hermes(argv) -> bool:
        return (argv[0] == FAKE_HERMES or workflow_runner.is_module_argv(list(argv))
                or "hermes" in os.path.basename(argv[0]).lower())

    # ProcessHost methods
    def spawn(self, argv, *, env, cwd, stdout_path, stderr_path=None):
        if self.fail_spawn:
            raise OSError("spawn refused")
        if self.real_enabled and not self._hermes(argv):
            pid = self.real.spawn(argv, env=env, cwd=cwd, stdout_path=stdout_path, stderr_path=stderr_path)
            self.real_runs.append({"pid": pid, "argv": list(argv), "env": dict(env), "cwd": Path(cwd)})
            return pid
        self.next_pid += 1
        process = FakeProcess(self.next_pid, list(argv), dict(env), cwd, start=self.next_pid * 10)
        process.stdout_path, process.stderr_path = Path(stdout_path), stderr_path
        Path(stdout_path).touch()
        if stderr_path is not None:
            Path(stderr_path).touch()
        self.processes[process.pid] = process
        return process.pid

    def _real(self, pid) -> bool:
        return pid not in self.processes and any(item["pid"] == pid for item in self.real_runs)

    def poll(self, pid):
        if self._real(pid):
            return self.real.poll(pid)
        process = self.processes.get(pid)
        if process is None or not process.own or process.alive:
            return None
        process.own = False
        return process.code

    def owns(self, pid):
        if self._real(pid):
            return self.real.owns(pid)
        process = self.processes.get(pid)
        return process is not None and process.own

    def alive(self, pid):
        if self._real(pid):
            return self.real.alive(pid)
        process = self.processes.get(pid)
        return process is not None and process.alive

    def fingerprint(self, pid):
        if self._real(pid):
            return self.real.fingerprint(pid)
        process = self.processes.get(pid)
        return f"{self.boot}|{process.start}" if process is not None and process.alive else None

    def boot_id(self):
        return self.boot

    def signal_group(self, pid, number):
        if self._real(pid):
            return self.real.signal_group(pid, number)
        process = self.processes[pid]
        process.signals.append(number)
        if number == signal.SIGTERM and process.ignore_term:
            return
        process.alive, process.code = False, -number

    # Test helpers
    def latest(self) -> FakeProcess:
        return self.processes[max(self.processes)]

    def reply(self, pid, outputs=None, *, text=None, exit_code=0, tokens=(1200, 300), report=True, exit=True):
        process = self.processes[pid]
        if text is None:
            text = "Here is my work.\n\n```bighelp-handoff\n" + json.dumps({"outputs": outputs}) + "\n```\n"
        records = [{"type": "system", "subtype": "init", "model": "fixture-model", "session_id": "s1"},
                   {"type": "tool_use", "name": "web_search", "input": {"query": "checklists"}},
                   {"type": "tool_result", "name": "web_search", "output": "ok", "is_error": False},
                   {"type": "text", "text": "Working on it.\nAlmost done.\n"},
                   {"type": "result", "exit_code": exit_code, "text": text,
                    "tokens": {"input": tokens[0], "output": tokens[1], "total": sum(tokens)}}]
        # Like Hermes: the stream goes to stdout and the turn report to the file its environment names.
        with open(process.stdout_path, "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        if report:
            Path(process.env.get("HERMES_QUIET_TURN_REPORT_FILE", process.cwd / "report.json")).write_text(json.dumps(
                {"pid": pid, "exit_code": exit_code, "error": "", "reply": text}), encoding="utf-8")
        if exit:
            process.alive, process.code = False, exit_code

    def crash(self, pid, code=1, stderr=b""):
        process = self.processes[pid]
        if stderr and process.stderr_path is not None:
            with open(process.stderr_path, "ab") as handle:
                handle.write(stderr)
        process.alive, process.code = False, code

    def reply_text(self, pid, outputs=None, *, text=None, exit_code=0, exit=True):
        """What the text runner sees: the final reply on stdout, no stream and no report."""
        process = self.processes[pid]
        if text is None:
            text = "Here is my work.\n\n```bighelp-handoff\n" + json.dumps({"outputs": outputs}) + "\n```\n"
        with open(process.stdout_path, "a", encoding="utf-8") as handle:
            handle.write(text)
        if exit:
            process.alive, process.code = False, exit_code


def make_home(test) -> Path:
    temporary = tempfile.TemporaryDirectory(prefix="bighelp-workflows-", dir=Path(tempfile.gettempdir()).resolve())
    test.addCleanup(temporary.cleanup)
    home = Path(temporary.name)
    (home / "config.yaml").write_text("model: fixture\n")
    for agent in AGENTS:
        (home / "profiles" / agent).mkdir(parents=True)
        (home / "profiles" / agent / "config.yaml").write_text("model: fixture\n")
    return home


def host_facts(home: Path) -> workflow_store.HostFacts:
    return workflow_store.HostFacts(
        profile_exists=lambda agent: agent == "default" or (home / "profiles" / agent / "config.yaml").is_file(),
        toolset_known=lambda name: name in {"web", "file", "terminal", "todo", "search"},
        repository=lambda path: (Path(path) / ".git").exists())


class Engine:
    """A store, a coordinator on a fake host and helpers to drive a run tick by tick."""

    def __init__(self, test, *, home: Path | None = None, boot: str = "boot-1"):
        self.test = test
        self.home = home or make_home(test)
        self.clock = Clock()
        self.root = workflow_store.root_for_home(self.home)
        self.store = workflow_store.WorkflowStore(self.root, clock=self.clock)
        self.facts = host_facts(self.home)
        self.host = FakeHost(boot)
        self.coordinator = self.new_coordinator()

    def new_coordinator(self, host=None):
        coordinator = workflow_coordinator.Coordinator(
            self.store, host=host or self.host, clock=self.clock, hermes=[FAKE_HERMES],
            environ={"PATH": "/usr/bin:/bin", "HOME": str(self.home), "OPENAI_API_KEY": "fixture-key-not-real",
                     "PYTHONPATH": "/elsewhere"},
            memory_ok=lambda: True)
        self.test.addCleanup(coordinator.release)
        return coordinator

    def start(self):
        self.test.assertTrue(self.coordinator.acquire())
        self.coordinator.begin()

    def workflow(self, definition=None, *, bind=True, name="Weekly newsletter") -> tuple[str, int]:
        if definition is None:
            created = self.store.use_template("research-draft-review", name, self.facts)
            workflow_id = created["workflowId"]
            version = created["draftVersion"]
        else:
            created = self.store.save_draft(None, 0, definition, self.facts)
            workflow_id, version = created["workflowId"], created["draftVersion"]
        if bind:
            for role, agent in BINDINGS:
                self.store.bind(workflow_id, role, agent)
        revision = self.store.publish(workflow_id, version, self.facts)["revision"]
        return workflow_id, revision

    def run(self, workflow_id, revision, client_run="6f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b", inputs=None) -> str:
        return self.store.start_run(workflow_id, revision, dict(INPUTS if inputs is None else inputs), client_run, False,
                                    self.facts)["run"]["id"]

    def tick(self, count: int = 1, seconds: float = 1.0):
        for _ in range(count):
            self.clock.advance(seconds)
            self.test.assertTrue(self.coordinator.tick())

    def wait(self, run_id, until, seconds: float = 20.0) -> dict:
        """Tick until `until(run detail)` holds, while real processes (commands, the PR step) finish."""
        deadline = time.monotonic() + seconds
        while True:
            self.tick()
            run = self.detail(run_id)
            if until(run):
                return run
            if time.monotonic() > deadline:
                self.test.fail(f"run stayed {run['state']} at {run['stageKey']}")
            time.sleep(0.05)

    def detail(self, run_id) -> dict:
        return self.store.get_run(run_id)["run"]

    def state(self, run_id) -> tuple[str, str]:
        run = self.detail(run_id)
        return run["state"], run["stageKey"]

    def finish_stage(self, outputs, **kwargs):
        process = self.host.latest()
        self.host.reply(process.pid, outputs, **kwargs)
        self.tick()
        return process

    def through_review(self, run_id, decision="pass", notes=()):
        """Drive research, draft, check and review once."""
        self.tick()
        self.finish_stage({"brief": {"content": "# Brief\n\nThree facts and two sources."}})
        self.finish_stage({"draft": {"content": DRAFT}, "word_count": 280})
        self.tick()
        self.finish_stage({"decision": decision, "notes": list(notes)})
        self.tick()


def no_launch(_root) -> bool:
    return True


__all__ = ["graph_definition", "code_definition", "git", "git_project", "fake_gh", "CHECK_SCRIPT", "Engine", "FakeHost",
           "FAKE_HERMES", "Clock", "make_home", "host_facts", "no_launch", "workflow_coordinator"]
