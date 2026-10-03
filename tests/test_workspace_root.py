"""Where an agent's files are: the folder Hermes itself gives the agent's chats."""
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from loopdy_plugin import workspace_root
from loopdy_plugin.native_context import NativeAPIError


def hermes_chat_gateway(cwd=None, error=None):
    """A stand-in for Hermes' in-process chat gateway (tui_gateway.server).

    Like the real one, `config.get` with key "project" answers with the folder a
    new chat starts in and its Git branch, and an unknown request is an error.
    """
    calls = []

    def handle_request(request):
        calls.append(request)
        if request.get("method") != "config.get" or request.get("params", {}).get("key") != "project":
            return {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": 4002, "message": "unknown config key"}}
        if error is not None:
            return {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": error, "message": "failed"}}
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": {"cwd": str(cwd), "branch": ""}}

    module = types.ModuleType("tui_gateway.server")
    module.handle_request = handle_request
    return module, calls


class WorkspaceRootTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="loopdy-workspace-root-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.hermes = self.base / "hermes"
        self.user = self.base / "user"
        self.launch = self.base / "launch"
        for folder in (self.hermes, self.user, self.launch):
            folder.mkdir()
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.hermes), "HOME": str(self.user)})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("TERMINAL_CWD", "TERMINAL_ENV", "TERMINAL_DOCKER_VOLUMES",
                     "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE"):
            os.environ.pop(name, None)
        # No chat gateway in this process unless a test adds one.
        modules = patch.dict(sys.modules)
        modules.start()
        self.addCleanup(modules.stop)
        sys.modules.pop("tui_gateway.server", None)
        previous = os.getcwd()
        os.chdir(self.launch)
        self.addCleanup(os.chdir, previous)

    def configure(self, terminal=None):
        config = {} if terminal is None else {"terminal": terminal}
        (self.hermes / "config.yaml").write_text(json.dumps(config))

    def refusal(self, terminal=None):
        self.configure(terminal)
        with self.assertRaises(NativeAPIError) as raised:
            workspace_root.resolve("default")
        return raised.exception

    def test_unset_cwd_uses_the_folder_hermes_starts_new_chats_in(self):
        chosen = self.user / "garden"
        chosen.mkdir()
        gateway, calls = hermes_chat_gateway(chosen)
        sys.modules["tui_gateway.server"] = gateway
        for terminal in (None, {}, {"cwd": "."}, {"cwd": "auto"}, {"cwd": "cwd"}, {"cwd": ""}):
            self.configure(terminal)
            workspace = workspace_root.resolve("default")
            self.assertEqual(workspace.root, chosen)
            self.assertEqual(workspace.origin, "default")
        self.assertEqual(calls[0]["method"], "config.get")
        self.assertEqual(calls[0]["params"], {"key": "project"})

    def test_without_the_chat_gateway_unset_cwd_follows_terminal_cwd_then_the_launch_folder(self):
        self.configure()
        self.assertEqual(workspace_root.resolve("default").root, self.launch)
        (self.user / "garden").mkdir()
        with patch.dict(os.environ, {"TERMINAL_CWD": "~/garden"}):
            self.assertEqual(workspace_root.resolve("default").root, self.user / "garden")
        with patch.dict(os.environ, {"TERMINAL_CWD": str(self.base / "missing")}):
            self.assertEqual(workspace_root.resolve("default").root, self.launch)

    def test_a_failing_chat_gateway_falls_back_to_the_same_rules(self):
        gateway, _ = hermes_chat_gateway(error=5000)
        sys.modules["tui_gateway.server"] = gateway
        self.configure()
        self.assertEqual(workspace_root.resolve("default").root, self.launch)

    def test_configured_relative_and_home_folders_resolve_like_hermes(self):
        (self.launch / "projects" / "garden").mkdir(parents=True)
        (self.user / "notes").mkdir()
        self.configure({"cwd": "projects/garden"})
        workspace = workspace_root.resolve("default")
        self.assertEqual(workspace.root, self.launch / "projects" / "garden")
        self.assertEqual(workspace.origin, "config")
        self.configure({"cwd": "~/notes"})
        self.assertEqual(workspace_root.resolve("default").root, self.user / "notes")
        self.configure({"cwd": str(self.user / "notes")})
        self.assertEqual(workspace_root.resolve("default").root, self.user / "notes")

    def test_a_configured_folder_that_is_missing_is_unavailable(self):
        error = self.refusal({"cwd": str(self.base / "missing")})
        self.assertEqual((error.status, error.code), (409, "workspace_unavailable"))

    def test_container_backends_say_the_files_are_inside_the_container(self):
        project = self.user / "project"
        project.mkdir()
        for terminal in ({"backend": "docker", "cwd": str(project)}, {"backend": "docker"},
                         {"backend": "docker", "cwd": "/workspace"}, {"backend": "modal"},
                         {"backend": "singularity", "cwd": str(project)}, {"backend": "daytona"},
                         {"env_type": "vercel_sandbox"}):
            error = self.refusal(terminal)
            self.assertEqual((error.status, error.code), (409, "workspace_in_container"), terminal)
        with patch.dict(os.environ, {"TERMINAL_ENV": "docker"}):
            error = self.refusal()
        self.assertEqual(error.code, "workspace_in_container")

    def test_ssh_says_the_files_are_on_another_computer(self):
        for terminal in ({"backend": "ssh"}, {"backend": "ssh", "cwd": "~/site"}, {"backend": "SSH", "cwd": "/srv/site"}):
            error = self.refusal(terminal)
            self.assertEqual((error.status, error.code), (409, "workspace_on_remote"), terminal)

    def test_docker_with_the_working_folder_mounted_shares_the_host_side(self):
        project = self.user / "project"
        project.mkdir()
        self.configure({"backend": "docker", "docker_mount_cwd_to_workspace": True, "cwd": str(project)})
        self.assertEqual(workspace_root.resolve("default").root, project)
        self.configure({"backend": "docker", "docker_mount_cwd_to_workspace": "true"})
        self.assertEqual(workspace_root.resolve("default").root, self.launch)

    def test_docker_volumes_map_the_container_folder_to_the_host(self):
        shared = self.user / "shared"
        (shared / "site").mkdir(parents=True)
        self.configure({"backend": "docker", "cwd": "/work/site", "docker_volumes": [f"{shared}:/work:rw"]})
        workspace = workspace_root.resolve("default")
        self.assertEqual(workspace.root, shared / "site")
        self.assertEqual(workspace.origin, "docker-volume")
        # A host folder Hermes also mounts into the container is the same files.
        self.configure({"backend": "docker", "cwd": str(shared), "docker_volumes": [f"{shared}:/work"]})
        self.assertEqual(workspace_root.resolve("default").root, shared)
        for volumes in (["named-volume:/work"], [f"{shared}:/elsewhere"], "not a list", [7]):
            error = self.refusal({"backend": "docker", "cwd": "/work/site", "docker_volumes": volumes})
            self.assertEqual(error.code, "workspace_in_container", volumes)

    def test_windows_hosts_are_not_supported_yet(self):
        self.configure({"cwd": str(self.user)})
        with patch.object(workspace_root, "_IS_WINDOWS", True):
            with self.assertRaises(NativeAPIError) as raised:
                workspace_root.resolve("default")
        self.assertEqual((raised.exception.status, raised.exception.code), (501, "workspace_windows_unsupported"))

    def test_a_default_in_hermes_own_folder_or_the_disk_root_is_not_a_working_folder(self):
        self.configure()
        os.chdir(self.hermes)
        error = self.refusal()
        self.assertEqual((error.status, error.code), (409, "workspace_not_configured"))
        (self.hermes / "logs").mkdir()
        os.chdir(self.hermes / "logs")
        self.assertEqual(self.refusal().code, "workspace_not_configured")
        gateway, _ = hermes_chat_gateway("/")
        sys.modules["tui_gateway.server"] = gateway
        self.assertEqual(self.refusal().code, "workspace_not_configured")

    def test_hermes_own_folder_is_never_a_configured_working_folder(self):
        error = self.refusal({"cwd": str(self.hermes)})
        self.assertEqual((error.status, error.code), (409, "workspace_hermes_folder"))
        # A folder of its own inside Hermes' folder is fine.
        (self.hermes / "workspace").mkdir()
        self.configure({"cwd": str(self.hermes / "workspace")})
        self.assertEqual(workspace_root.resolve("default").root, self.hermes / "workspace")

    def test_hermes_folders_are_known_so_listings_can_hide_them(self):
        (self.user / ".hermes").mkdir()
        (self.hermes / "profiles" / "research").mkdir(parents=True)
        identities = workspace_root.hermes_folders("default")
        for folder in (self.hermes, self.user / ".hermes", self.hermes / "profiles" / "research"):
            info = folder.stat()
            self.assertIn((info.st_dev, info.st_ino), identities, folder)
        info = self.launch.stat()
        self.assertNotIn((info.st_dev, info.st_ino), identities)


if __name__ == "__main__":
    unittest.main()
