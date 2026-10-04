"""Shared fakes for the workflow tests: a Hermes home with agents, a fake clock and a fake process host.

The fake host behaves like Hermes' worker: it writes `stream.jsonl` records and an atomic `report.json`
({pid, exit_code, error, reply}) into the attempt folder, then exits. Nothing real is started.
"""
from __future__ import annotations

import json
import signal
import tempfile
from pathlib import Path

from loopdy_plugin import workflow_coordinator, workflow_store


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


class FakeHost:
    def __init__(self, boot: str = "boot-1"):
        self.processes: dict[int, FakeProcess] = {}
        self.next_pid = 41000
        self.boot = boot
        self.fail_spawn = False

    # ProcessHost methods
    def spawn(self, argv, *, env, cwd, stdout_path, stderr_path=None):
        if self.fail_spawn:
            raise OSError("spawn refused")
        self.next_pid += 1
        process = FakeProcess(self.next_pid, list(argv), dict(env), cwd, start=self.next_pid * 10)
        process.stdout_path, process.stderr_path = Path(stdout_path), stderr_path
        Path(stdout_path).touch()
        if stderr_path is not None:
            Path(stderr_path).touch()
        self.processes[process.pid] = process
        return process.pid

    def poll(self, pid):
        process = self.processes.get(pid)
        if process is None or not process.own or process.alive:
            return None
        process.own = False
        return process.code

    def owns(self, pid):
        process = self.processes.get(pid)
        return process is not None and process.own

    def alive(self, pid):
        process = self.processes.get(pid)
        return process is not None and process.alive

    def fingerprint(self, pid):
        process = self.processes.get(pid)
        return f"{self.boot}|{process.start}" if process is not None and process.alive else None

    def boot_id(self):
        return self.boot

    def signal_group(self, pid, number):
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
        with open(process.cwd / "stream.jsonl", "a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        if report:
            (process.cwd / "report.json").write_text(json.dumps(
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
        toolset_known=lambda name: name in {"web", "file", "terminal", "todo", "search"})


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
            self.store, host=host or self.host, clock=self.clock, hermes=["/fixture/bin/hermes"],
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


__all__ = ["graph_definition", "Engine", "FakeHost", "Clock", "make_home", "host_facts", "no_launch", "workflow_coordinator"]
