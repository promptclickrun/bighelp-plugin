"""Environment passed to the self-update launcher."""
import os
import tempfile
import unittest
from unittest import mock

from loopdy_plugin.plugin_update import _launcher_env


class UpdateLauncherEnvironmentTests(unittest.TestCase):
    def test_carries_existing_xdg_runtime_directory(self):
        with tempfile.TemporaryDirectory() as runtime_dir:
            with mock.patch.dict(os.environ, {
                "PATH": "/test/bin",
                "HOME": "/test/home",
                "XDG_RUNTIME_DIR": runtime_dir,
            }, clear=True):
                with mock.patch("loopdy_plugin.plugin_update.os.getuid", return_value=1234):
                    env = _launcher_env()
        self.assertEqual(env.get("XDG_RUNTIME_DIR"), runtime_dir)

    def test_carries_dbus_session_bus_address_when_set(self):
        with mock.patch.dict(os.environ, {
            "PATH": "/test/bin",
            "HOME": "/test/home",
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/runtime/bus",
        }, clear=True):
            env = _launcher_env()
        self.assertEqual(env.get("DBUS_SESSION_BUS_ADDRESS"), "unix:path=/runtime/bus")

    def test_keeps_path_home_and_lang(self):
        with mock.patch.dict(os.environ, {
            "PATH": "/test/bin",
            "HOME": "/test/home",
        }, clear=True):
            env = _launcher_env()
        self.assertEqual(env["PATH"], "/test/bin")
        self.assertEqual(env["HOME"], "/test/home")
        self.assertEqual(env["LANG"], "C.UTF-8")

    @unittest.skipUnless(os.path.isdir(f"/run/user/{os.getuid()}"), "real per-user runtime directory is unavailable")
    def test_falls_back_to_existing_per_user_runtime_directory(self):
        runtime_dir = f"/run/user/{os.getuid()}"
        with mock.patch.dict(os.environ, {
            "PATH": "/test/bin",
            "HOME": "/test/home",
        }, clear=True):
            with mock.patch("loopdy_plugin.plugin_update.os.getuid", return_value=os.getuid()):
                env = _launcher_env()
        self.assertEqual(env.get("XDG_RUNTIME_DIR"), runtime_dir)

    def test_does_not_fall_back_when_per_user_runtime_directory_is_missing(self):
        with mock.patch.dict(os.environ, {
            "PATH": "/test/bin",
            "HOME": "/test/home",
        }, clear=True):
            with mock.patch("loopdy_plugin.plugin_update.os.getuid", return_value=1234):
                with mock.patch("loopdy_plugin.plugin_update.os.path.isdir", return_value=False):
                    env = _launcher_env()
        self.assertNotIn("XDG_RUNTIME_DIR", env)

    def test_does_not_emit_nonexistent_xdg_runtime_directory(self):
        with mock.patch.dict(os.environ, {
            "PATH": "/test/bin",
            "HOME": "/test/home",
            "XDG_RUNTIME_DIR": "/no/such/runtime-directory-for-test",
        }, clear=True):
            env = _launcher_env()
        self.assertNotIn("XDG_RUNTIME_DIR", env)


if __name__ == "__main__":
    unittest.main()
