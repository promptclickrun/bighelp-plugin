"""The runner with real processes: a fake `hermes` script stands in for Hermes. No agent or model runs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import textwrap
import subprocess
import time
import unittest
from unittest.mock import patch

from loopdy_plugin import workflow_coordinator
from loopdy_plugin import workflow_runner as runner
from workflow_fixtures import Engine


FAKE_HERMES = textwrap.dedent('''\
    import json, os, subprocess, sys, time
    brief = open("brief.md", encoding="utf-8").read()
    print(json.dumps({"type": "system", "subtype": "init", "model": "fixture-model"}), flush=True)
    with open("argv.json", "w") as handle:
        json.dump({"argv": sys.argv[1:], "env": sorted(os.environ), "pgid": os.getpgid(0), "pid": os.getpid()},
                  handle)
    if "MODE:sleep" in brief:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        open("child.pid", "w").write(str(child.pid))
        time.sleep(120)
    reply = 'Done.\\n```bighelp-handoff\\n{"outputs": {"note": {"content": "# Note\\\\n\\\\nFrom the fake."}}}\\n```'
    print(json.dumps({"type": "text", "text": "Writing the note.\\n"}), flush=True)
    print(json.dumps({"type": "result", "exit_code": 0, "text": reply,
                      "tokens": {"input": 10, "output": 5, "total": 15}}), flush=True)
    path = os.environ["HERMES_QUIET_TURN_REPORT_FILE"]
    with open(path + ".tmp", "w") as handle:
        json.dump({"pid": os.getpid(), "exit_code": 0, "error": "", "reply": reply}, handle)
    os.replace(path + ".tmp", path)
''')


def quick(instructions: str) -> dict:
    return {"schemaVersion": 1, "name": "Quick note", "roles": [{"key": "writer", "label": "Writer"}],
            "stages": [{"key": "write", "kind": "agent", "title": "Write", "role": "writer",
                        "instructions": instructions, "outputs": [{"name": "note", "type": "markdown_file"}],
                        "minutes": 1},
                       {"key": "ok", "kind": "signoff", "title": "Approve", "file": "write.note"}]}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        return waited == 0
    except ChildProcessError:
        return True


class RealProcessTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(self)
        script = self.engine.home / "fake-hermes.py"
        script.write_text(FAKE_HERMES)
        self.engine.host = runner.OSProcessHost()
        self.engine.coordinator = self.engine.new_coordinator(self.engine.host)
        self.engine.coordinator.hermes = [sys.executable, str(script)]
        self.engine.start()

    def workflow(self, instructions: str) -> str:
        store = self.engine.store
        workflow_id = store.save_draft(None, 0, quick(instructions), self.engine.facts)["workflowId"]
        store.bind(workflow_id, "writer", "writer")
        store.publish(workflow_id, 1, self.engine.facts)
        return self.engine.run(workflow_id, 1, inputs={})

    def wait_for(self, predicate, seconds: float = 20.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.engine.tick()
            if predicate():
                return
            time.sleep(0.05)
        self.fail("timed out waiting for the coordinator")

    def test_a_real_child_hands_off_through_report_and_stream(self):
        run_id = self.workflow("Write a short note.")
        self.wait_for(lambda: self.engine.state(run_id)[0] == "waiting_for_you")
        attempt = self.engine.detail(run_id)["stages"][0]["attempts"][0]
        self.assertEqual((attempt["state"], attempt["tokens"]), ("accepted", {"in": 10, "out": 5}))
        folder = self.engine.store.runs_dir / run_id / "write" / "1-1"
        seen = json.loads((folder / "argv.json").read_text())
        self.assertEqual(seen["argv"], ["-p", "writer", "--cli", "chat", "--source", "workflow", "--toolsets", "todo",
                                        "--query-file", "brief.md", "--format", "stream-json"])
        self.assertEqual(seen["pgid"], seen["pid"])
        self.assertNotIn("OPENAI_API_KEY", seen["env"])
        self.assertIn("HERMES_QUIET_TURN_REPORT_FILE", seen["env"])
        detail = self.engine.detail(run_id)
        self.assertEqual(detail["signoff"]["artifact"]["bytes"], len(b"# Note\n\nFrom the fake."))

    def test_time_limit_stops_the_whole_process_group(self):
        run_id = self.workflow("MODE:sleep Wait.")
        folder = self.engine.store.runs_dir / run_id / "write" / "1-1"
        self.wait_for(lambda: (folder / "child.pid").exists() and (folder / "child.pid").read_text())
        child = int((folder / "child.pid").read_text())
        self.assertTrue(alive(child))
        self.engine.clock.advance(61)
        self.wait_for(lambda: self.engine.state(run_id)[0] == "failed")
        self.assertEqual(self.engine.detail(run_id)["failure"]["code"], "timed_out")
        deadline = time.monotonic() + 5
        while alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(child))


class DetachedCoordinatorTests(unittest.TestCase):
    """A real coordinator started the way a hosted Hermes (no launchd, no systemd) starts it.

    Its interpreter is a bare venv that can't import Hermes, like the bundled Python some Hermes launchers exec with
    Hermes' folder added only in-process. `hermes_cli` here is a fake package in a temporary folder that only the
    launching process (this test) knows about. PATH has no `hermes` launcher, so nothing real can start.
    """

    def setUp(self):
        for folder in ("/usr/bin", "/bin"):
            self.assertFalse(os.path.exists(os.path.join(folder, "hermes")))
        self.engine = Engine(self)
        self.engine.clock.value = time.time()
        base = self.engine.home.parent / (self.engine.home.name + "-tools")
        base.mkdir()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(base)], check=False))
        package = base / "fake-hermes" / "hermes_cli"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "main.py").write_text(FAKE_HERMES)
        self.fake_root = package.parent
        subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(base / "bare")], check=True,
                       capture_output=True, timeout=120)
        self.python = str(base / "bare" / "bin" / "python")
        probe = subprocess.run([self.python, "-c", "import importlib.util as u; print(u.find_spec('hermes_cli'))"],
                               capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, check=True)
        self.assertEqual(probe.stdout.strip(), "None")
        self.processes: list[subprocess.Popen] = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            if process in workflow_coordinator._detached:
                workflow_coordinator._detached.remove(process)

    def launch(self) -> subprocess.Popen:
        environ = {key: value for key, value in os.environ.items() if key != "HERMES_BIN"}
        environ["PATH"] = "/usr/bin:/bin"
        with patch.object(runner, "hermes_import_root", return_value=str(self.fake_root)), \
                patch.object(sys, "executable", self.python), patch.dict(os.environ, environ, clear=True):
            self.assertTrue(workflow_coordinator.launch_service(self.engine.root, manager="detached"))
        process = workflow_coordinator._detached[-1]
        self.processes.append(process)
        return process

    def wait_for(self, predicate, seconds: float = 30.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.engine.clock.value = time.time()
            if predicate():
                return
            time.sleep(0.1)
        self.fail("timed out waiting for the coordinator")

    def test_workers_get_hermes_import_path_from_the_launching_process(self):
        store = self.engine.store
        workflow_id = store.save_draft(None, 0, quick("Write a short note."), self.engine.facts)["workflowId"]
        store.bind(workflow_id, "writer", "writer")
        store.publish(workflow_id, 1, self.engine.facts)
        run_id = store.start_run(workflow_id, 1, {}, "6f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b", False,
                                 self.engine.facts)["run"]["id"]
        process = self.launch()
        working = ("planned", "launched", "running", "checking_output", "accepted")
        self.wait_for(lambda: self.engine.state(run_id)[0] not in working)
        detail = self.engine.detail(run_id)
        events = [event["text"] for event in store.events(run_id, 0, 50)["events"]]
        self.assertEqual(detail["state"], "waiting_for_you", (detail.get("failure"), events))
        folder = store.runs_dir / run_id / "write" / "1-1"
        seen = json.loads((folder / "argv.json").read_text())
        self.assertIn("PYTHONPATH", seen["env"])
        self.assertEqual(seen["argv"][:2], ["-p", "writer"])
        self.assertIsNone(process.poll())

    def test_a_second_detached_coordinator_leaves_at_once(self):
        first = self.launch()
        self.wait_for(lambda: self.engine.store.status()["coordinator"]["state"] == "online")
        second = self.launch()
        second.wait(timeout=30)
        self.assertEqual(second.returncode, 0)
        self.assertIsNone(first.poll())
        self.assertEqual(self.engine.store.status()["coordinator"]["epoch"], 1)


class HelperTests(unittest.TestCase):
    def test_environment_is_an_allowlist(self):
        env = runner.worker_env({"PATH": "/bin", "HOME": "/home/a", "OPENAI_API_KEY": "x", "AWS_SECRET": "y",
                                 "HERMES_HOME": "/elsewhere"}, home=Path("/h/profiles/w"), profile="w",
                                attempt_dir=Path("/r/a"))
        self.assertEqual(sorted(env), ["HERMES_HOME", "HERMES_PROFILE", "HERMES_QUIET_TURN_REPORT_FILE",
                                       "HERMES_SESSION_SOURCE", "HOME", "PATH", "PYTHONUTF8", "TERMINAL_CWD"])
        self.assertEqual(env["HERMES_HOME"], "/h/profiles/w")

    def test_profile_homes_follow_hermes_rules(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as folder:
            root = Path(folder)
            (root / "profiles" / "live").mkdir(parents=True)
            (root / "profiles" / "live" / "SOUL.md").write_text("x")
            (root / "profiles" / "ghost").mkdir()
            (root / "profiles" / "gone").mkdir()
            (root / "profiles" / "gone" / "config.yaml").write_text("x")
            (root / "profiles" / ".deleted" / "gone").mkdir(parents=True)
            self.assertEqual(runner.profile_home(root, "live"), root / "profiles" / "live")
            self.assertEqual(runner.profile_home(root, "default"), root)
            for name in ("ghost", "gone", "../live", "Live"):
                self.assertIsNone(runner.profile_home(root, name))

    def test_stream_report_and_fingerprints(self):
        with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve()) as folder:
            path = Path(folder) / "stream.jsonl"
            path.write_bytes(b'{"type": "text", "text": "a"}\nnot json\n{"type": "res')
            offset, records = runner.read_stream(path, 0)
            self.assertEqual((offset, records), (len(b'{"type": "text", "text": "a"}\nnot json\n'),
                                                 [{"type": "text", "text": "a"}]))
            self.assertEqual(runner.read_stream(path, offset), (offset, []))
            report = Path(folder) / "report.json"
            report.write_text(json.dumps({"pid": 7, "exit_code": 0, "reply": "x"}))
            self.assertIsNotNone(runner.read_report(report, 7))
            self.assertIsNone(runner.read_report(report, 8))
        self.assertTrue(runner.same_process("boot|100.0", "boot|101.5"))
        self.assertFalse(runner.same_process("boot|100.0", "boot|103.0"))
        self.assertFalse(runner.same_process("boot|100.0", "other|100.0"))
        self.assertFalse(runner.same_process(None, "boot|1"))
        host = runner.OSProcessHost()
        self.assertTrue(runner.same_process(host.fingerprint(os.getpid()), host.fingerprint(os.getpid())))

    def test_live_text_hides_secrets(self):
        live = runner.LiveText()
        lines = live.lines({"type": "text", "text": "token=sk-proj-ABCDEFGHIJKLMNOPQRSTUV in " + str(Path.home())
                            + "/notes\n"})
        self.assertEqual(lines, [("text", "[hidden] in ~/notes")])
        self.assertEqual(runner.tokens({"type": "result", "tokens": {"input": 5, "output": -1}}), (5, 0))


if __name__ == "__main__":
    unittest.main()
