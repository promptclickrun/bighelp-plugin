"""A host that still has Link, Direct and wiki state starts Hermes' dashboard and gateway cleanly.

The config is the shape real hosts have (made-up address): the old Direct setting, a gateway block for
the loopdy platform, loopdy in plugins.enabled and in platform_toolsets, Link keys in .env, and old
plugin files. Everything runs in a throwaway HOME and HERMES_HOME, with outbound connects refused.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

CONFIG = """\
plugins:
  enabled: [loopdy]
  loopdy:
    settings:
      direct: {enabled: true, origin: "https://hermes-host.tailnet-fixture.ts.net", port: 8768}
      templates_allow_create_agent: true
  entries:
    loopdy:
      settings:
        direct: {enabled: true, origin: "https://hermes-host.tailnet-fixture.ts.net", port: 8768}
        templates_allow_create_agent: true
platforms:
  loopdy:
    extra:
      agent_defaults:
        scheduled_tasks:
          reasoning_effort: medium
platform_toolsets:
  cli: [hermes-cli, loopdy]
  loopdy: [hermes-cli, loopdy]
"""
OLD_ENV = (
    "EXAMPLE_SETTING=keep-me\n"
    "LOOPDY_LINK_BASE_URL=https://link.loopdy.example\n"
    "LOOPDY_LINK_DEVICE_ID=host-fixture\n"
    "LOOPDY_LINK_AUTHORIZATION_EPOCH=3\n"
    "LOOPDY_LINK_SIGNING_PRIVATE_KEY=fixture-signing-key\n"
    "LOOPDY_LINK_AGREEMENT_PRIVATE_KEY=fixture-agreement-key\n"
    "LOOPDY_LINK_ACCOUNT_KEY=fixture-account-key\n"
)
STARTUP = textwrap.dedent("""
    import asyncio, json, logging, socket, sys
    problems = []
    class Collect(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                problems.append(f"{record.levelname} {record.name}: {record.getMessage()[:300]}")
    logging.getLogger().addHandler(Collect())
    logging.getLogger().setLevel(logging.INFO)
    def refuse(*_args, **_kwargs):
        raise OSError("network refused in this test")
    socket.create_connection = refuse
    socket.socket.connect = refuse
    socket.socket.connect_ex = refuse
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv()

    # The dashboard: the plugin loads and its routes answer.
    from fastapi.testclient import TestClient
    from hermes_cli import web_server
    from hermes_cli.web_server_dashboard import _mount_plugin_api_routes
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    discover_plugins()
    plugin = get_plugin_manager()._plugins["loopdy"]
    _mount_plugin_api_routes()
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app)
    headers = {"Authorization": "Bearer fixture-dashboard-token"}
    answers = {path: client.get(path, headers=headers).status_code for path in (
        "/api/plugins/loopdy/native/context", "/api/plugins/loopdy/capabilities",
        "/api/plugins/loopdy/notifications/capabilities")}
    answers["/api/plugins/loopdy/native/wiki/roots"] = client.post(
        "/api/plugins/loopdy/native/wiki/roots", headers=headers, json={}).status_code
    features = client.get("/api/plugins/loopdy/native/context", headers=headers).json()["features"]
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.toolset_validation import saved_toolset_resolver, validate_platform_toolsets
    except ImportError:
        toolset_warnings = []  # Older Hermes has no saved-toolset check.
    else:
        config = load_config_readonly()
        toolset_warnings = validate_platform_toolsets(config.get("platform_toolsets"), saved_toolset_resolver(config))

    # The messaging gateway: it starts and stops with this config.
    from gateway.config import Platform, load_gateway_config
    from gateway.run import GatewayRunner
    gateway_config = load_gateway_config()
    loopdy = gateway_config.platforms.get(Platform("loopdy"))
    runner = GatewayRunner(gateway_config)
    async def run():
        started = await asyncio.wait_for(runner.start(), 120)
        adapters = sorted(platform.value for platform in runner.adapters)
        await runner.stop()
        return started, adapters
    started, adapters = asyncio.run(run())
    with open(sys.argv[1], "w") as result:
        json.dump({"plugin_enabled": plugin.enabled, "plugin_error": plugin.error, "answers": answers,
                   "features": features, "toolset_warnings": toolset_warnings, "started": started,
                   "adapters": adapters, "loopdy_enabled": bool(loopdy and loopdy.enabled),
                   "loopdy_extra": loopdy.extra if loopdy else None, "problems": problems}, result)
""")


class OldHostStartupTests(unittest.TestCase):
    def test_dashboard_and_gateway_start_cleanly_on_a_host_with_old_state(self):
        try:
            import gateway.run  # noqa: F401
            import hermes_cli.web_server_dashboard  # noqa: F401
        except ImportError as error:
            self.skipTest(f"Hermes dashboard and gateway are not importable here: {error}")
        with tempfile.TemporaryDirectory(prefix="loopdy-old-host-") as directory:
            base = Path(directory).resolve()
            hermes_home = base / "home" / ".hermes"
            shutil.copytree(PLUGIN_ROOT, hermes_home / "plugins" / "loopdy",
                            ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", "*.pyc"))
            (hermes_home / "config.yaml").write_text(CONFIG, encoding="utf-8")
            (hermes_home / ".env").write_text(OLD_ENV, encoding="utf-8")
            data = hermes_home / "plugin-data" / "loopdy"
            old_files = (data / "workspace-git-link.sqlite3", data / "wiki" / "grants.sqlite3",
                         data / "direct" / "enrollment.json")
            for path in old_files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"left by an older plugin")
            from loopdy_plugin.store import BighelpStore
            BighelpStore(data / "loopdy.sqlite3")
            with sqlite3.connect(data / "loopdy.sqlite3") as db:
                db.execute("INSERT INTO metadata(key,value) VALUES('relay_config_v1','{}')")
                db.execute("INSERT INTO devices(device_id,endpoint_id,provider,label,groups_json,preferences_json,"
                           "created_at,updated_at) VALUES('legacy','obsolete','relay','','[]','{}',1,1)")
            environment = {
                "HOME": str(base / "home"), "HERMES_HOME": str(hermes_home), "TMPDIR": str(base),
                "HERMES_DISABLE_LAZY_INSTALLS": "1", "HERMES_DASHBOARD_SESSION_TOKEN": "fixture-dashboard-token",
                "PATH": os.environ.get("PATH", ""),
                # The test runner's import roots minus this checkout: Hermes loads the installed copy.
                "PYTHONPATH": os.pathsep.join(
                    str(Path(entry).resolve()) for entry in sys.path
                    if entry and Path(entry).resolve() not in {PLUGIN_ROOT, PLUGIN_ROOT / "tests"}),
            }
            result_path = base / "result.json"
            run = subprocess.run([sys.executable, "-c", STARTUP, str(result_path)], env=environment, cwd=base,
                                 capture_output=True, text=True, timeout=240)
            self.assertEqual(run.returncode, 0, run.stderr[-4000:])
            result = json.loads(result_path.read_text(encoding="utf-8"))

            self.assertTrue(result["plugin_enabled"], result["plugin_error"])
            self.assertIsNone(result["plugin_error"])
            wiki = result["answers"].pop("/api/plugins/loopdy/native/wiki/roots")
            self.assertEqual(result["answers"], {
                "/api/plugins/loopdy/native/context": 200,
                "/api/plugins/loopdy/capabilities": 200,
                "/api/plugins/loopdy/notifications/capabilities": 200,
            })
            # No plugin route: Hermes answers 404, or 405 from the dashboard's page fallback before 0.22.
            self.assertIn(wiki, {404, 405})
            self.assertIn("native-context-v1", result["features"])
            self.assertFalse([feature for feature in result["features"] if "wiki" in feature])
            self.assertEqual(result["toolset_warnings"], [])
            self.assertTrue(result["started"])
            # Without the legacy push provider the platform stays off, as before; its block is kept as is.
            self.assertFalse(result["loopdy_enabled"])
            self.assertNotIn("loopdy", result["adapters"])
            self.assertEqual(result["loopdy_extra"], {"agent_defaults": {"scheduled_tasks": {"reasoning_effort": "medium"}}})
            bighelp_problems = [line for line in result["problems"]
                                if "loopdy" in line.lower() or "bighelp" in line.lower()]
            self.assertEqual(bighelp_problems, [], result["problems"])
            self.assertFalse([line for line in result["problems"] if line.startswith(("ERROR", "CRITICAL"))])

            self.assertEqual((hermes_home / ".env").read_text(encoding="utf-8"), "EXAMPLE_SETTING=keep-me\n")
            self.assertEqual((hermes_home / "config.yaml").read_text(encoding="utf-8"), CONFIG)
            self.assertTrue(all(path.read_bytes() == b"left by an older plugin" for path in old_files))
            with sqlite3.connect(data / "loopdy.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM devices WHERE provider='relay'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
