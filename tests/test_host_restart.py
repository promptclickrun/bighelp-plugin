"""In-place restart used by the bighelp app after it updates the plugin."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from loopdy_plugin import host_restart, native_api


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


class LocksLeftByRestartTests(unittest.TestCase):
    def start_native_api(self):
        async def start():
            async with native_api.router.lifespan_context(None):
                pass
        with mock.patch.object(native_api, "log_native_feature_startup"):
            asyncio.run(start())

    def test_startup_releases_every_lock_in_this_process_name(self):
        released = []
        hermes = mock.Mock(release_orphaned_leases=lambda live: released.append(live) or 1)
        with mock.patch.dict(sys.modules, {"hermes_cli.active_sessions": hermes}):
            self.start_native_api()
        # Nothing in a starting process holds a lock yet, so none is vouched for.
        self.assertEqual(released, [set()])

    def test_startup_goes_on_when_hermes_cannot_release(self):
        def fail(live):
            raise OSError("registry unavailable")
        with mock.patch.dict(sys.modules, {"hermes_cli.active_sessions": mock.Mock(release_orphaned_leases=fail)}), \
                self.assertLogs("hermes.plugins.bighelp", level="WARNING") as logs:
            self.start_native_api()
        self.assertIn("release_failed", logs.output[0])
        with mock.patch.dict(sys.modules, {"hermes_cli.active_sessions": None}):
            self.start_native_api()  # a Hermes without the registry: nothing to release

    @unittest.skipUnless(importlib.util.find_spec("hermes_cli"), "needs a Hermes checkout")
    def test_a_chat_open_before_an_in_place_restart_takes_messages_after_it(self):
        from hermes_cli import active_sessions as hermes
        chat = f"restart-{os.getpid()}"
        _, refused = hermes.try_acquire_active_session(
            session_id=chat, surface="bighelp", config={}, metadata={"live_session_id": "before-restart"})
        self.assertIsNone(refused)
        # An in-place restart keeps the PID and start time, so the new copy of the chat is refused.
        _, refused = hermes.try_acquire_active_session(
            session_id=chat, surface="bighelp", config={}, metadata={"live_session_id": "after-restart"})
        self.assertEqual(getattr(refused, "reason", None), hermes.SESSION_NOT_OWNED)
        # The lock was taken minutes before the restart, past Hermes' 30-second hold.
        with mock.patch.object(hermes, "_SELF_ORPHAN_GRACE_SECONDS", 0):
            self.start_native_api()
        lease, refused = hermes.try_acquire_active_session(
            session_id=chat, surface="bighelp", config={}, metadata={"live_session_id": "after-restart"})
        self.assertIsNone(refused)
        lease.release()


# A tiny server that restarts itself exactly like a Hermes process does when the
# app asks: same PID, same command line, listening socket released and bound again.
_SERVER = textwrap.dedent("""
    import json, os, sys, uuid
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    sys.path.insert(0, sys.argv[1])
    from loopdy_plugin import host_restart
    RUNTIME = uuid.uuid4().hex
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            self.reply({"pid": os.getpid(), "runtime": RUNTIME, "argv": sys.orig_argv[1:]})
        def do_POST(self):
            self.reply(host_restart.schedule(delay=0.2))
    server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[2])), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()
""")


@unittest.skipUnless(host_restart.available(), "in-place restart needs a POSIX host")
class RealRestartTests(unittest.TestCase):
    def test_process_restarts_in_place_and_serves_again(self):
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        root = str(Path(__file__).resolve().parents[1])
        script = Path(self.id().replace(".", "_") + ".py")
        tmp = Path(os.environ.get("TMPDIR", "/tmp")) / script
        tmp.write_text(_SERVER)
        self.addCleanup(tmp.unlink, missing_ok=True)
        child = subprocess.Popen([sys.executable, str(tmp), root, str(port)], stdout=subprocess.PIPE, text=True)

        def stop():
            child.kill()
            child.wait(timeout=5)
            child.stdout.close()
        self.addCleanup(stop)
        self.assertEqual(int(child.stdout.readline()), port)

        def get(method="GET"):
            request = urllib.request.Request(f"http://127.0.0.1:{port}/", method=method, data=b"" if method == "POST" else None)
            with urllib.request.urlopen(request, timeout=2) as response:
                return json.load(response)

        before = get()
        self.assertEqual(get("POST"), {"restarting": True, "alreadyScheduled": False})
        after = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(0.2)
            try:
                value = get()
            except OSError:
                continue
            if value["runtime"] != before["runtime"]:
                after = value
                break
        self.assertIsNotNone(after, "the process never came back")
        # Same process for launchd/systemd, same flags, fresh code.
        self.assertEqual(after["pid"], before["pid"])
        self.assertEqual(after["argv"], before["argv"])
        self.assertIsNone(child.poll())


if __name__ == "__main__":
    unittest.main()
