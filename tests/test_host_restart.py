"""In-place restart used by the bighelp app after it updates the plugin."""
import sys
import unittest
from unittest import mock

from loopdy_plugin import host_restart


class HostRestartTests(unittest.TestCase):
    def setUp(self):
        host_restart._scheduled = False

    def test_restart_reuses_the_exact_command_line(self):
        calls, timers = [], []

        class Timer:
            def __init__(self, delay, function):
                self.delay, self.function, self.daemon = delay, function, False
                timers.append(self)
            def start(self):
                pass

        with mock.patch.object(sys, "orig_argv", ["python3", "-m", "hermes_cli.main", "serve", "--isolated", "--port", "9121"]), \
                mock.patch.object(host_restart.os, "name", "posix"):
            result = host_restart.schedule(execv=lambda path, argv: calls.append((path, argv)), timer=Timer)
            self.assertEqual(result, {"restarting": True, "alreadyScheduled": False})
            # A second request while one is pending doesn't restart twice.
            self.assertEqual(host_restart.schedule(execv=lambda *_: None, timer=Timer)["alreadyScheduled"], True)
            self.assertEqual(len(timers), 1)
            self.assertTrue(timers[0].daemon)
            timers[0].function()
        self.assertEqual(calls, [(sys.executable, [sys.executable, "-m", "hermes_cli.main", "serve", "--isolated", "--port", "9121"])])

    def test_failed_exec_keeps_running_and_allows_another_try(self):
        class Timer:
            def __init__(self, delay, function): self.function, self.daemon = function, False
            def start(self): self.function()

        def fail(path, argv):
            raise OSError("exec failed")

        with mock.patch.object(host_restart.os, "name", "posix"):
            host_restart.schedule(execv=fail, timer=Timer)
            self.assertFalse(host_restart._scheduled)

    def test_unavailable_without_posix_exec(self):
        with mock.patch.object(host_restart.os, "name", "nt"):
            self.assertFalse(host_restart.available())
            with self.assertRaises(RuntimeError):
                host_restart.schedule(execv=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
