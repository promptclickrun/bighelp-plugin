"""Execution boundaries for self-update; no production service mutations."""
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from loopdy_plugin import plugin_update as api
from loopdy_plugin import yaml_compat
_REAL_POPEN = subprocess.Popen
with patch.dict(sys.modules, {"plugin_update": api}):
    worker = importlib.import_module("loopdy_plugin.plugin_update_worker")


class RepositoryRenameTests(unittest.TestCase):
    """The repositories were renamed to bighelp-*; former names stay recognized."""

    def test_canonical_source_is_the_renamed_repository(self):
        self.assertEqual(api.SOURCE_URL, "https://github.com/promptclickrun/bighelp-plugin")
        self.assertEqual(api.APP_SOURCE_URL, "https://github.com/promptclickrun/bighelp-ios")

    def test_current_and_former_names_are_recognized_in_every_spelling(self):
        for name in ("bighelp-plugin", "loopdy-plugin"):
            for value in (f"https://github.com/promptclickrun/{name}", f"https://github.com/promptclickrun/{name}.git",
                          f"git@github.com:promptclickrun/{name}.git", f"ssh://git@github.com/promptclickrun/{name}"):
                self.assertTrue(worker._is_github_repository(value, api.PLUGIN_REPOSITORIES), value)
        for name in ("bighelp-ios", "loopdy-ios"):
            self.assertTrue(worker._is_github_repository(f"https://github.com/promptclickrun/{name}", api.APP_REPOSITORIES))

    def test_unrelated_repositories_are_not_recognized(self):
        for value in ("https://github.com/someone/bighelp-plugin", "https://gitlab.com/promptclickrun/bighelp-plugin",
                      "https://github.com/promptclickrun/bighelp-ios"):
            self.assertFalse(worker._is_github_repository(value, api.PLUGIN_REPOSITORIES), value)


class PluginUpdateExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plugin = self.root / "plugins" / "loopdy"
        self.plugin.mkdir(parents=True)
        (self.plugin / "plugin.yaml").write_text('name: loopdy\nversion: "2.4.0"\n')
        self.manager = api.PluginUpdateManager(self.root / "plugin-data" / "loopdy" / "plugin-update", self.plugin, "default", launch_worker=lambda _: None)
        self.operation_id = "update_0123456789abcdef"
        self.device = "device_0123456789"

    def begin(self):
        return self.manager.start(self.operation_id, self.device, True)

    def test_public_status_is_decodable_by_native_contract(self):
        idle = self.manager.status(device_id=self.device)
        self.assertIsNone(idle["operation_id"])
        self.assertIsNone(idle["target_revision"])
        self.assertIsNone(idle["active_revision"])
        self.assertEqual(self.begin()["phase"], "accepted")
        expected = {
            "resolved": "resolving", "validating_installation": "validating",
            "staging": "validating", "backing_up": "validating", "installed": "installing",
            "restart_requested": "restarting", "awaiting_activation": "waiting_for_activation",
            "reconnecting": "waiting_for_activation",
        }
        for internal, external in expected.items():
            self.manager._worker_transition(self.operation_id, internal, "Status")
            self.assertEqual(self.manager.status(self.operation_id, self.device)["phase"], external)

    def test_ambiguous_timeout_does_not_authorize_a_second_update(self):
        self.begin()
        self.manager._worker_transition(self.operation_id, "timed_out", "Waiting", restart_requested_at=1)
        with self.assertRaises(ValueError):
            self.manager.start("update_second_0123456789", self.device, True)

    def test_git_checkout_revision_wins_over_a_stale_installer_record(self):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        def git(*args):
            return subprocess.run(["git", "-C", str(self.plugin), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()
        git("init", "-q")
        git("add", ".")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "fixture")
        head = git("rev-parse", "HEAD")
        (self.root / "plugins" / ".install-metadata.json").write_text(json.dumps({"loopdy": {"pinned": True, "revision": "d" * 40, "source": "https://github.com/promptclickrun/loopdy-plugin"}}))
        self.assertEqual(api._metadata_revision(self.plugin), head)
        shutil.rmtree(self.plugin / ".git")
        # Archive-style installs have no checkout and keep using the installer record.
        self.assertEqual(api._metadata_revision(self.plugin), "d" * 40)

    def test_timeout_superseded_by_a_newer_runtime_on_other_bytes_releases_the_host(self):
        target, other = "a" * 40, "c" * 40
        with patch.object(api, "LOADED_REVISION", "b" * 40), patch.object(api, "RUNTIME_ID", "runtime_started"):
            self.manager.record_runtime_loaded()
        self.begin()
        self.manager._worker_transition(self.operation_id, "timed_out", "Waiting", target_revision=target, restart_requested_at=1)
        with patch.object(api, "_metadata_revision", return_value=other):
            with patch.object(api, "LOADED_REVISION", other), patch.object(api, "RUNTIME_ID", "runtime_later"):
                self.manager.record_runtime_loaded()
            status = self.manager.status(self.operation_id, self.device)
            self.assertEqual(status["phase"], "failed")
            self.assertIn("Superseded", status["message"])
            self.assertEqual(self.manager.start("update_second_0123456789", self.device, True)["phase"], "accepted")

    def test_timeout_on_a_host_that_installed_other_bytes_is_superseded_without_a_runtime_report(self):
        # Hosts without the old push gateway never report a runtime. When the installation holds other
        # bytes (the app installed a newer release), the target can never activate: release the host.
        target, other = "a" * 40, "c" * 40
        self.begin()
        self.manager._worker_transition(self.operation_id, "timed_out", "Waiting", target_revision=target, restart_requested_at=1)
        with patch.object(api, "_metadata_revision", return_value=other):
            status = self.manager.status(self.operation_id, self.device)
            self.assertEqual(status["phase"], "failed")
            self.assertIn("Superseded", status["message"])
            self.assertEqual(self.manager.start("update_second_0123456789", self.device, True)["phase"], "accepted")

    def test_timeout_with_an_unknown_installation_stays_held(self):
        self.begin()
        self.manager._worker_transition(self.operation_id, "timed_out", "Waiting", target_revision="a" * 40, restart_requested_at=1)
        with patch.object(api, "_metadata_revision", return_value=""):
            self.assertEqual(self.manager.status(self.operation_id, self.device)["phase"], "timed_out")

    def test_timeout_is_held_while_the_target_is_still_installed(self):
        target = "a" * 40
        self.begin()
        self.manager._worker_transition(self.operation_id, "timed_out", "Waiting", target_revision=target, restart_requested_at=1)
        with patch.object(api, "_metadata_revision", return_value=target), patch.object(api, "LOADED_REVISION", "c" * 40), patch.object(api, "RUNTIME_ID", "runtime_later"):
            self.manager.record_runtime_loaded()
            self.assertEqual(self.manager.status(self.operation_id, self.device)["phase"], "timed_out")

    def test_completion_requires_a_fresh_runtime_on_the_target_revision(self):
        revision = "a" * 40
        with patch.object(api, "LOADED_REVISION", revision), patch.object(api, "RUNTIME_ID", "runtime_old"):
            self.manager.record_runtime_loaded()
            self.begin()
            self.manager._worker_transition(self.operation_id, "awaiting_activation", "Waiting", target_revision=revision)
            # The runtime that started the update is not proof that the new bytes loaded.
            self.assertNotEqual(self.manager.status(self.operation_id, self.device)["phase"], "complete")
        with patch.object(api, "LOADED_REVISION", "b" * 40), patch.object(api, "RUNTIME_ID", "runtime_wrong"), \
                patch.object(api, "_metadata_revision", return_value=revision):
            self.manager.record_runtime_loaded()
            self.assertNotEqual(self.manager.status(self.operation_id, self.device)["phase"], "complete")
        with patch.object(api, "LOADED_REVISION", revision), patch.object(api, "RUNTIME_ID", "runtime_fresh"):
            self.manager.record_runtime_loaded()
            status = self.manager.status(self.operation_id, self.device)
            self.assertEqual(status["phase"], "complete")
            self.assertNotIn("Link", status["message"])

    def test_launcher_preserves_virtual_environment_interpreter(self):
        interpreter = self.root / "venv" / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        commands = []
        def capture(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0)
        original_which = api.shutil.which
        with patch.object(api.sys, "executable", str(interpreter)), patch.object(api.platform, "system", return_value="Darwin"), patch.object(api.shutil, "which", side_effect=lambda name: "/bin/launchctl" if name == "launchctl" else original_which(name)), patch.object(api.subprocess, "run", side_effect=capture):
            self.manager._launch_detached_worker(self.operation_id)
        self.assertTrue((self.manager.data_root / "workers" / self.operation_id / "yaml_compat.py").is_file(),
                        "The worker reads manifests with it")
        self.assertEqual(commands[0][1], "submit")
        self.assertIn(str(interpreter), commands[0])
        self.assertNotIn("KeepAlive", commands[0])
        self.assertIn("--service-label", commands[0])

    def _launch_on_linux(self, *, systemd_run, user_manager, run_result=0):
        """Launch the worker as Linux would; returns (commands given to systemd-run, detached Popen calls)."""
        commands, detached = [], []
        def capture(command, **kwargs):
            commands.append(command)
            return subprocess.CompletedProcess(command, run_result)
        def popen(command, **kwargs):
            detached.append((command, kwargs))
            return _REAL_POPEN([sys.executable, "-c", "pass"])
        original_which = api.shutil.which
        which = lambda name: (systemd_run if name == "systemd-run" else original_which(name))
        with patch.object(api.platform, "system", return_value="Linux"), \
                patch.object(api.shutil, "which", side_effect=which), \
                patch.object(api, "_systemd_user_available", return_value=user_manager), \
                patch.object(api.subprocess, "run", side_effect=capture), \
                patch.object(api.subprocess, "Popen", side_effect=popen):
            self.manager._launch_detached_worker(self.operation_id)
        return commands, detached

    def test_a_system_service_host_without_a_user_manager_runs_the_worker_detached(self):
        """`hermes gateway install --system --run-as-user` hosts have no user bus: `systemd-run --user` can't work."""
        commands, detached = self._launch_on_linux(systemd_run="/usr/bin/systemd-run", user_manager=False)
        self.assertEqual(commands, [], "No systemd-run --user without a user manager")
        self.assertEqual(len(detached), 1)
        command, kwargs = detached[0]
        self.assertTrue(kwargs["start_new_session"], "Its own session, so it outlives the process that started it")
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(command[1], "-i", "The same clean environment as the managed launch")
        self.assertIn(str(self.manager.data_root / "workers" / self.operation_id / "plugin_update_worker.py"), command)

    def test_linux_without_systemd_run_runs_the_worker_detached(self):
        commands, detached = self._launch_on_linux(systemd_run=None, user_manager=False)
        self.assertEqual(commands, [])
        self.assertEqual(len(detached), 1)

    def test_a_user_manager_keeps_the_managed_systemd_launch(self):
        commands, detached = self._launch_on_linux(systemd_run="/usr/bin/systemd-run", user_manager=True)
        self.assertEqual(detached, [])
        self.assertEqual(commands[0][:2], ["/usr/bin/systemd-run", "--user"])

    def test_a_refused_systemd_job_falls_back_to_detached(self):
        commands, detached = self._launch_on_linux(systemd_run="/usr/bin/systemd-run", user_manager=True, run_result=1)
        self.assertEqual(len(commands), 1)
        self.assertEqual(len(detached), 1)

    def test_start_on_a_host_without_a_user_manager_is_not_blocked(self):
        """The 3.25.0 symptom: `hermes bighelp update` answered `blocked` on a supported system-service host."""
        manager = api.PluginUpdateManager(self.manager.data_root, self.plugin, "default")
        self.manager = manager
        # The real launcher, on a host where systemd-run exists but has no user bus (it exits 1 there).
        _commands, detached = self._launch_on_linux_start(manager)
        self.assertNotEqual(self._started["phase"], "blocked")
        self.assertEqual(len(detached), 1)

    def _launch_on_linux_start(self, manager):
        detached = []
        def popen(command, **kwargs):
            detached.append(command)
            return _REAL_POPEN([sys.executable, "-c", "pass"])
        original_which = api.shutil.which
        which = lambda name: ("/usr/bin/systemd-run" if name == "systemd-run" else original_which(name))
        with patch.object(api.platform, "system", return_value="Linux"), \
                patch.object(api.shutil, "which", side_effect=which), \
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root / "no-user-manager")}), \
                patch.object(api.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)), \
                patch.object(api.subprocess, "Popen", side_effect=popen):
            os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
            self._started = manager.start(self.operation_id, self.device, False)
        return [], detached

    def test_the_worker_gets_the_hermes_import_path_of_the_process_that_starts_it(self):
        """A bundled interpreter finds `hermes_cli` only in-process (Hermes' launcher adds it). The scrubbed worker
        runs `python -m hermes_cli.main` for Plugin Doctor, the installer and the restart, so it needs the path."""
        with patch.object(api, "_hermes_import_path", return_value=["/fixture/hermes-agent", "/fixture/site-packages"]), \
                patch.dict(os.environ, {"HERMES_DISABLE_LAZY_INSTALLS": "1", "SECRET_CANARY": "x"}):
            command = api._clean_exec_command(["/fixture/python", "worker.py"], hermes_home=self.root)
        self.assertEqual(command[1], "-i")
        self.assertIn("PYTHONPATH=/fixture/hermes-agent" + os.pathsep + "/fixture/site-packages", command)
        self.assertIn("HERMES_DISABLE_LAZY_INSTALLS=1", command, "A host that turned lazy installs off keeps them off")
        self.assertFalse(any(item.startswith("SECRET_CANARY") for item in command))
        self.assertLess(command.index("PYTHONUTF8=1"), command.index("/fixture/python"))
        with patch.object(api, "_hermes_import_path", return_value=[]), patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_DISABLE_LAZY_INSTALLS", None)
            command = api._clean_exec_command(["/fixture/python"], hermes_home=self.root)
        self.assertFalse(any(item.startswith(("PYTHONPATH=", "HERMES_DISABLE_LAZY_INSTALLS=")) for item in command),
                         "An installed wheel needs no path")

    def test_hermes_commands_from_the_worker_keep_that_path(self):
        with patch.dict(os.environ, {"PYTHONPATH": "/fixture/hermes-agent", "HERMES_DISABLE_LAZY_INSTALLS": "1",
                                     "SECRET_CANARY": "x"}):
            env = worker._process_env(self.root)
            git_env = worker._process_env(self.root, git=True)
        self.assertEqual(env["PYTHONPATH"], "/fixture/hermes-agent")
        self.assertEqual(env["HERMES_DISABLE_LAZY_INSTALLS"], "1")
        self.assertNotIn("SECRET_CANARY", env)
        self.assertEqual(git_env["GIT_TERMINAL_PROMPT"], "0")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PYTHONPATH", None)
            os.environ.pop("HERMES_DISABLE_LAZY_INSTALLS", None)
            self.assertNotIn("PYTHONPATH", worker._process_env(self.root))

    def test_hermes_import_path_uses_hermes_own_pin(self):
        root = str(self.root / "hermes-agent")
        pin = type(sys)("cron.scheduler_worker_env")
        pin.pin_hermes_tree_on_pythonpath = lambda env, tree: {"PYTHONPATH": f"{tree}{os.pathsep}/fixture/venv/site"}
        with patch.object(api, "_hermes_import_root", return_value=root), \
                patch.dict(sys.modules, {"cron.scheduler_worker_env": pin}):
            self.assertEqual(api._hermes_import_path(), [root, "/fixture/venv/site"])
        with patch.object(api, "_hermes_import_root", return_value=None):
            self.assertEqual(api._hermes_import_path(), [])

    def test_production_manager_refuses_a_shared_other_profile_installation(self):
        import hermes_constants
        another_home = self.root / "other-profile"
        with patch.object(api, "_PLUGIN_ROOT", self.plugin), patch.object(hermes_constants, "get_hermes_home", return_value=another_home), patch.object(api.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            manager = api.production_manager("other")
            with self.assertRaises(api.PluginUpdateError):
                manager._launch_detached_worker(self.operation_id)

    def test_same_capabilities_pass_without_pyyaml(self):
        """Hermes 0.22 and later ship only ruamel.yaml: an update that adds nothing must not stop here."""
        installed = self.root / "plugins" / "loopdy"
        installed.mkdir(parents=True, exist_ok=True)
        (installed / "plugin.yaml").write_text('name: loopdy\ncapabilities: [tools.read]\n')
        staged = self.root / "staged"
        staged.mkdir()
        (staged / "plugin.yaml").write_text('name: loopdy\ncapabilities: [tools.read]\n')
        guard = type(sys)("tools.plugin_guard")
        guard.scan_plugin = lambda root, source: object()
        guard.should_allow_plugin_install = lambda scan, force: (True, "")
        # The worker runs from a folder holding its copied siblings, so it imports them by plain name.
        with patch.dict(sys.modules, {"yaml": None, "yaml_compat": yaml_compat, "tools.plugin_guard": guard}), \
                patch.object(worker, "_run_quiet", return_value=0):
            worker._validate_staged_plugin(staged, self.root)

    def test_new_privileged_capability_blocks_before_installer(self):
        staged = self.root / "staged"
        staged.mkdir()
        (staged / "plugin.yaml").write_text('name: loopdy\ncapabilities: [tools.override]\n')
        with patch.object(worker, "_run_quiet", return_value=0):
            with self.assertRaises(worker.UpdateBlocked):
                worker._validate_staged_plugin(staged, self.root)

    def test_dirty_installation_is_checked_before_up_to_date(self):
        revision = "a" * 40
        with patch.object(api, "LOADED_REVISION", revision), patch.object(api, "RUNTIME_ID", "runtime_active"):
            self.manager.record_runtime_loaded()
        self.begin()
        self.manager._worker_transition(self.operation_id, "resolved", "Resolved", target_revision=revision)
        with patch.object(worker, "_metadata_revision", return_value=revision), patch.object(worker, "_recognized_installation", side_effect=worker.UpdateBlocked("dirty")):
            with self.assertRaises(worker.UpdateBlocked):
                worker._run_operation(self.manager, self.operation_id)

    def test_cli_restart_completes_when_the_gateway_loads_the_update(self):
        revision = "a" * 40
        cli_device = api.local_cli_device_id("default")
        with patch.object(api, "LOADED_REVISION", revision), patch.object(api, "RUNTIME_ID", "runtime_old"):
            self.manager.record_runtime_loaded()
            self.manager.start(self.operation_id, cli_device, True)
        self.manager._worker_transition(self.operation_id, "awaiting_activation", "Waiting", target_revision=revision)
        with patch.object(api, "LOADED_REVISION", revision), patch.object(api, "RUNTIME_ID", "runtime_fresh"):
            self.manager.record_runtime_loaded()
            self.assertEqual(self.manager.status(self.operation_id)["phase"], "complete")

    def test_restart_timeout_is_observation_only(self):
        with patch.object(worker.subprocess, "run", side_effect=subprocess.TimeoutExpired(["hermes"], 1)) as runner:
            self.assertEqual(worker._restart_gateway(self.manager), "reconnecting")
            self.assertEqual(runner.call_count, 1)

if __name__ == "__main__":
    unittest.main()
