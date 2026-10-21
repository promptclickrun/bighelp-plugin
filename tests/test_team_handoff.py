from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


SKILL_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "profile-agent-collaboration"
)
SOURCE = SKILL_DIR / "scripts" / "team_handoff.py"
spec = importlib.util.spec_from_file_location("team_handoff", SOURCE)
assert spec is not None and spec.loader is not None
handoff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(handoff)


class TeamHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="loopdy-team-handoff-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "config.yaml").write_text("model:\n  default: fixture\n", encoding="utf-8")
        for profile in ("nova", "havok"):
            profile_dir = self.root / "profiles" / profile
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "profile.yaml").write_text(f"display_name: {profile.title()}\n", encoding="utf-8")
        self.cli = self.root / "fake-hermes"
        self.write_fake_cli()
        self.cli.chmod(0o700)

    def write_fake_cli(self, code: str = "", text: str = "FIXTURE-REPLY") -> None:
        event = {"type": "result", "exit_code": 0, "session_id": "fixture", "text": text}
        self.cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"{code}\n"
            f"print(json.dumps({event!r}))\n",
            encoding="utf-8",
        )

    def run_job(self, **kwargs) -> dict:
        args = dict(
            root=self.root / "jobs",
            job_id="test-123",
            sender="default",
            target="nova",
            message="Bounded test",
            workspace=self.root,
            timeout=5,
            cli=self.cli,
            hermes_home=self.root,
        )
        args.update(kwargs)
        return handoff.run_job(**args)

    def test_skill_markdown_has_no_machine_specific_or_personal_paths(self) -> None:
        skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("persist_on_release=true", skill_text)
        self.assertIn("scripts/team_handoff.py", skill_text)
        for forbidden in ("/Users/", "gordieai", "Colt", "Destery"):
            self.assertNotIn(forbidden, skill_text)

    def test_discovers_local_profiles_from_hermes_root(self) -> None:
        self.assertEqual(
            handoff.discover_profiles(self.root),
            {"default", "nova", "havok"},
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root / "profiles" / "nova")}):
            self.assertEqual(handoff.resolve_hermes_root(), self.root)

    def test_result_saved_with_owner_only_permissions(self) -> None:
        result = self.run_job()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["reply"], "FIXTURE-REPLY")
        receipt = self.root / "jobs" / "test-123" / "result.json"
        self.assertEqual(json.loads(receipt.read_text(encoding="utf-8")), result)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)

    def test_replay_does_not_run_twice(self) -> None:
        self.run_job()
        self.cli.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8")
        self.assertEqual(self.run_job()["status"], "completed")

    def test_changed_payload_rejected(self) -> None:
        self.run_job()
        with self.assertRaises(ValueError):
            self.run_job(message="Different side effect")

    def test_self_and_unknown_targets_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.run_job(target="default")
        with self.assertRaises(ValueError):
            self.run_job(target="unknown_profile")
        with self.assertRaises(ValueError):
            self.run_job(target="../other")

    def test_blank_and_warning_only_outputs_fail(self) -> None:
        self.cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.assertEqual(self.run_job(job_id="blank")["status"], "failed")
        self.cli.write_text('#!/usr/bin/env python3\nprint("Warning: Missing plugin")\n', encoding="utf-8")
        self.assertEqual(self.run_job(job_id="warn")["status"], "failed")

    def test_canonical_bot_chat_arguments_and_clean_environment(self) -> None:
        self.write_fake_cli(
            'a = sys.argv\n'
            'assert a[a.index("-c") + 1] == "Bot Chat"\n'
            'assert "--create-if-missing" in a\n'
            'assert "--query-file" in a\n'
            'assert a[a.index("--in") + 1] == str(Path.home())\n'
            'assert a[a.index("--format") + 1] == "stream-json"\n'
            'assert "--run-budget" in a\n'
            'assert "SENDER_SECRET_CANARY" not in os.environ\n'
            'assert "HERMES_SESSION_ID" not in os.environ\n'
            'assert "HERMES_TURN_AUTHOR" not in os.environ\n'
            f'assert os.environ["HERMES_HOME"] == {str(self.root)!r}\n'
        )
        with patch.dict(
            os.environ,
            {
                "SENDER_SECRET_CANARY": "NOT-A-REAL-SECRET",
                "HERMES_SESSION_ID": "SENDER-SESSION",
                "HERMES_TURN_AUTHOR": "SENDER",
            },
        ):
            self.assertEqual(self.run_job()["status"], "completed")

    def test_timeout_and_busy_target_handled(self) -> None:
        self.cli.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(10)\n", encoding="utf-8")
        self.assertEqual(self.run_job(job_id="timeout", timeout=0.1)["status"], "timed_out")

        jobs = self.root / "jobs"
        jobs.mkdir(exist_ok=True)
        with (jobs / "nova.lock").open("w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_job(job_id="busy")["status"], "target_busy")

    def test_parent_death_recovers_real_child_artifact(self) -> None:
        self.write_fake_cli("import time\ntime.sleep(0.6)", text="SURVIVED-PARENT")
        code = (
            "import importlib.util;from pathlib import Path;"
            f"s=importlib.util.spec_from_file_location('h',{str(SOURCE)!r});"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            f"m.run_job(root=Path({str(self.root / 'jobs')!r}),job_id='crash',sender='default',"
            f"target='nova',message='test',workspace=Path({str(self.root)!r}),timeout=5,"
            f"cli=Path({str(self.cli)!r}),hermes_home=Path({str(self.root)!r}))"
        )
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.DEVNULL)
        receipt = self.root / "jobs" / "crash" / "result.json"
        try:
            for _ in range(100):
                if receipt.exists() and json.loads(receipt.read_text(encoding="utf-8")).get("status") == "running":
                    break
                time.sleep(0.02)
            else:
                self.fail("Child never started")
            proc.kill()
            proc.wait()
            time.sleep(1.0)
            result = handoff.inspect_receipt(receipt)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["reply"], "SURVIVED-PARENT")
            self.assertTrue(result["recovered_from_child_result"])
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main()
