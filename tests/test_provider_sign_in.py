"""Signing in to provider accounts from the app with each provider's own CLI, in a private terminal.

The fake CLIs print what the real ones print (captured from Copilot CLI 1.0 and Claude Code 2.1):
a device link and code, an OSC 8 hyperlink and a paste prompt, or a full-screen token screen.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch
import uuid

from loopdy_plugin import provider_sign_in as si


FAKE_TOKEN = "sk-ant-oat01-" + "Fake0Token_" * 6


def executable(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


COPILOT = """
    import os, sys, time
    assert sys.argv[1:] == ["login", "--device-code"], sys.argv
    assert sys.stdin.isatty()
    leaked = [name for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN") if name in os.environ]
    print("To authenticate, visit https://github.com/login/device and enter code WDJB-MJHT", flush=True)
    print("Waiting for authorization...", flush=True)
    sys.stdout.write("\\x1b]52;c;V0RKQi1NSkhU\\x07")
    sys.stdout.flush()
    time.sleep(float(os.environ.get("FAKE_APPROVE_AFTER", "0.6")))
    sys.exit(3 if leaked else int(os.environ.get("FAKE_EXIT", "0")))
"""

# `claude auth login`, `claude auth status --json` and `claude setup-token`.
CLAUDE = """
    import json, os, sys, time
    state = os.path.join(os.environ["FAKE_STATE"], "claude-signed-in")
    link = ("https://claude.com/cai/oauth/authorize?code=true&client_id=fixture&response_type=code"
            "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=fixture")
    if sys.argv[1:] == ["auth", "status", "--json"]:
        print(json.dumps({"loggedIn": os.path.exists(state), "authMethod": "claude.ai"}))
        sys.exit(0)
    if sys.argv[1:] == ["auth", "login", "--claudeai"]:
        sys.stdout.write("Opening browser to sign in\\u2026\\r\\nIf the browser didn't open, visit: "
                         "\\x1b]8;;" + link + "\\x07\\x1b[94m" + link + "\\x1b[39m\\x1b]8;;\\x07\\r\\n"
                         "Paste code here if prompted > ")
        sys.stdout.flush()
        code = sys.stdin.readline().strip()
        if code == "good#code":
            open(state, "w").close()
            print("Login successful.")
            sys.exit(0)
        print("OAuth error: Invalid code")
        sys.exit(1)
    if sys.argv[1:] == ["setup-token"]:
        sys.stdout.write("\\x1b[?2004h\\x1b[38;5;174mWelcome\\x1b[9Gto\\x1b[12GClaude\\x1b[19GCode\\x1b[39m\\r\\r\\n"
                         "\\x1b]8;id=1;" + link + "\\x07\\x1b[38;5;246m" + link[:60] + "\\x1b[39m\\x1b]8;;\\x07\\r\\r\\n"
                         "\\x1b]8;id=1;" + link + "\\x07\\x1b[38;5;246m" + link[60:] + "\\x1b[39m\\x1b]8;;\\x07\\r\\r\\n"
                         "Paste code here if prompted >")
        sys.stdout.flush()
        code = sys.stdin.readline().strip()
        if code != "good#code":
            # The real screen stays up after a bad code.
            sys.stdout.write("\\x1b[2G\\x1b[31mOAuth\\x1b[8Gerror:\\x1b[15GRequest failed with status code 400"
                             "\\x1b[39m Press Enter to retry.\\r\\r\\n")
            sys.stdout.flush()
            time.sleep(60)
        sys.stdout.write("\\x1b[2G\\x1b[32m\\u2713\\x1b[39m Long-lived authentication token created successfully!\\r\\r\\n"
                         "\\x1b[2GYour OAuth token (valid for 1 year):\\r\\r\\n\\r\\r\\n\\x1b[2G" + os.environ["FAKE_TOKEN"]
                         + "\\r\\r\\n\\r\\r\\n\\x1b[2GStore this token securely.\\r\\r\\n")
        sys.stdout.flush()
        time.sleep(60)
    sys.exit(9)
"""


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bighelp-sign-in-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "bin").mkdir()
        (self.root / "state").mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        environment = patch.dict(os.environ, {
            "HERMES_HOME": str(self.home), "HOME": str(self.home), "FAKE_STATE": str(self.root / "state"),
            "FAKE_TOKEN": FAKE_TOKEN, "GH_TOKEN": "fixture-env-token",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.copilot = executable(self.root / "bin", "copilot", COPILOT)
        self.claude = executable(self.root / "bin", "claude", CLAUDE)
        tools = {"copilot": self.copilot, "claude": self.claude}
        finder = patch.object(si, "find_cli", side_effect=lambda name: tools.get(name))
        finder.start()
        self.addCleanup(finder.stop)
        self.addCleanup(si.cancel_all)
        self.addCleanup(si._sessions.clear)
        si._status_cache.clear()

    def wait_for(self, session_id, statuses, seconds=10.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = si.status("default", session_id)
            if value["status"] in statuses:
                return value
            time.sleep(0.05)
        self.fail(f"still {si.status('default', session_id)}")


class DeviceSignInTests(Base):
    def test_copilot_cli_shows_its_link_and_code_then_finishes_on_its_own(self):
        started = si.start("default", "copilot-acp")
        self.assertEqual(started["status"], si.WAITING, started)
        self.assertEqual((started["link"], started["code"], started["flow"]),
                         ("https://github.com/login/device", "WDJB-MJHT", si.DEVICE))
        done = self.wait_for(started["sessionId"], {si.SIGNED_IN, si.FAILED})
        # The fake exits 3 if a token from the environment reached it.
        self.assertEqual(done["status"], si.SIGNED_IN, done)
        self.assertNotIn("link", done)
        self.assertNotIn("code", done)

    def test_a_tool_that_exits_with_an_error_fails_without_its_output(self):
        with patch.dict(os.environ, {"FAKE_EXIT": "1"}):
            started = si.start("default", "copilot-acp")
            done = self.wait_for(started["sessionId"], {si.SIGNED_IN, si.FAILED})
        self.assertEqual(done["status"], si.FAILED)
        self.assertEqual(done["message"], "GitHub Copilot CLI didn't finish signing in. Try again.")

    def test_cancel_stops_the_tool(self):
        with patch.dict(os.environ, {"FAKE_APPROVE_AFTER": "30"}):
            started = si.start("default", "copilot-acp")
            session = si._sessions[started["sessionId"]]
            self.assertEqual(si.cancel("default", started["sessionId"])["status"], si.CANCELLED)
        deadline = time.monotonic() + 5
        while session._process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(session._process.poll())

    def test_a_new_sign_in_replaces_an_unfinished_one(self):
        with patch.dict(os.environ, {"FAKE_APPROVE_AFTER": "30"}):
            first = si.start("default", "copilot-acp")
            second = si.start("default", "copilot-acp")
        self.assertEqual(si.status("default", first["sessionId"])["status"], si.CANCELLED)
        self.assertEqual(si.status("default", second["sessionId"])["status"], si.WAITING)

    def test_links_off_the_providers_domains_are_never_passed_on(self):
        executable(self.root / "bin", "copilot", """
            import sys, time
            print("To authenticate, visit https://github.com.evil.example/login/device and enter code WDJB-MJHT",
                  flush=True)
            time.sleep(30)
        """)
        started = si.start("default", "copilot-acp", wait=1.0)
        self.assertEqual(started["status"], si.FAILED)
        self.assertNotIn("link", started)
        self.assertEqual(started["message"], "GitHub Copilot CLI didn't show a sign-in link. Try again.")

    def test_sessions_belong_to_their_profile(self):
        with patch.dict(os.environ, {"FAKE_APPROVE_AFTER": "30"}):
            started = si.start("default", "copilot-acp")
        with self.assertRaises(si.SignInError) as refused:
            si.status("research", started["sessionId"])
        self.assertEqual(refused.exception.code, "sign_in_not_found")


class PasteSignInTests(Base):
    def test_claude_code_takes_the_code_from_the_page(self):
        started = si.start("default", "claude-code")
        self.assertEqual(started["status"], si.NEEDS_CODE, started)
        self.assertTrue(started["link"].startswith("https://claude.com/cai/oauth/authorize?"), started["link"])
        self.assertNotIn("code", started)
        self.assertIs(si.recipe("claude-code").signed_in(), False)
        self.assertEqual(si.submit("default", started["sessionId"], " good#code ")["status"], si.FINISHING)
        done = self.wait_for(started["sessionId"], {si.SIGNED_IN, si.FAILED})
        self.assertEqual(done["status"], si.SIGNED_IN, done)
        # The status cache is dropped once a sign-in lands.
        self.assertIs(si.recipe("claude-code").signed_in(), True)

    def test_a_wrong_code_says_so(self):
        started = si.start("default", "claude-code")
        si.submit("default", started["sessionId"], "wrong#code")
        done = self.wait_for(started["sessionId"], {si.SIGNED_IN, si.FAILED})
        self.assertEqual((done["status"], done["message"]),
                         (si.FAILED, "Claude Code didn't accept that code. Try again."))

    def test_a_wrong_code_on_the_token_screen_says_so_and_closes_it(self):
        started = si.start("default", "anthropic")
        si.submit("default", started["sessionId"], "wrong#code")
        done = self.wait_for(started["sessionId"], {si.SIGNED_IN, si.FAILED})
        self.assertEqual((done["status"], done["message"]),
                         (si.FAILED, "Claude Code didn't accept that code. Try again."))
        self.assertFalse((self.home / ".env").exists())

    def test_codes_are_one_line_of_visible_text(self):
        started = si.start("default", "claude-code")
        for code in ("", "two\nlines", "tab\there", "x" * 2049, "café"):
            with self.assertRaises(si.SignInError) as refused:
                si.submit("default", started["sessionId"], code)
            self.assertEqual(refused.exception.code, "sign_in_code_invalid", code)
        self.assertEqual(si.status("default", started["sessionId"])["status"], si.NEEDS_CODE)

    def test_a_code_is_only_taken_when_the_tool_asks(self):
        with patch.dict(os.environ, {"FAKE_APPROVE_AFTER": "30"}):
            started = si.start("default", "copilot-acp")
        with self.assertRaises(si.SignInError) as refused:
            si.submit("default", started["sessionId"], "good#code")
        self.assertEqual(refused.exception.code, "sign_in_not_waiting")

    def test_setup_token_is_saved_to_the_profile_and_never_returned_or_logged(self):
        started = si.start("default", "anthropic")
        self.assertEqual(started["status"], si.NEEDS_CODE, started)
        # The full link comes from the hyperlink, not the wrapped text.
        self.assertTrue(started["link"].endswith("&state=fixture"), started["link"])
        with self.assertLogs("hermes.plugins.bighelp", level="INFO") as logs:
            si.submit("default", started["sessionId"], "good#code")
            done = self.wait_for(started["sessionId"], {si.SIGNED_IN, si.FAILED})
        self.assertEqual(done["status"], si.SIGNED_IN, done)
        self.assertNotIn(FAKE_TOKEN, repr(done))
        self.assertFalse(any(FAKE_TOKEN in record.getMessage() for record in logs.records))
        self.assertIn(f"CLAUDE_CODE_OAUTH_TOKEN={FAKE_TOKEN}", (self.home / ".env").read_text())
        session = si._sessions[started["sessionId"]]
        deadline = time.monotonic() + 5
        while session._process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(session._process.poll(), "the token screen is closed once the token is saved")


class CatalogTests(Base):
    def test_lists_ready_missing_and_retired_sign_ins(self):
        rows = {row["providerId"]: row for row in si.providers()}
        self.assertEqual(rows["copilot-acp"]["state"], "ready")
        self.assertEqual(rows["claude-code"], {
            "providerId": "claude-code", "name": "Claude Code", "client": "Claude Code", "flow": si.PASTE,
            "docsURL": "https://docs.claude.com/en/docs/claude-code/setup", "state": "ready", "signedIn": False})
        self.assertEqual(rows["qwen-oauth"]["state"], "retired")
        self.assertEqual(rows["qwen-oauth"]["replacementKey"], "DASHSCOPE_API_KEY")
        with patch.object(si, "find_cli", return_value=None):
            missing = {row["providerId"]: row for row in si.providers()}
        self.assertEqual((missing["claude-code"]["state"], missing["claude-code"]["installCommand"]),
                         ("notInstalled", "npm install -g @anthropic-ai/claude-code"))
        self.assertEqual(missing["claude-code"]["message"], "Claude Code isn't installed on this computer.")

    def test_refuses_unknown_retired_and_missing_tools(self):
        for provider, code in (("nope", "sign_in_unavailable"), ("qwen-oauth", "sign_in_retired")):
            with self.assertRaises(si.SignInError) as refused:
                si.start("default", provider)
            self.assertEqual(refused.exception.code, code)
        with patch.object(si, "find_cli", return_value=None), self.assertRaises(si.SignInError) as refused:
            si.start("default", "claude-code")
        self.assertEqual(refused.exception.code, "sign_in_tool_missing")

    def test_override_settings_pick_the_configured_cli(self):
        other = executable(self.root / "bin", "claude-other", "print('x')\n")
        with patch.dict(os.environ, {"CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND": other}):
            self.assertEqual(si.recipe("claude-subscription-directsdk-experimental").command(),
                             [other, "auth", "login", "--claudeai"])
        with patch.dict(os.environ, {"CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND": "/no/such/claude"}):
            self.assertEqual(si.recipe("claude-subscription-directsdk-experimental").command()[0], self.claude)
        with patch.dict(os.environ, {"HERMES_COPILOT_ACP_COMMAND": "copilot --acp; echo other"}):
            self.assertEqual(si.recipe("copilot-acp").command(), [self.copilot, "login", "--device-code"])

    def test_hermes_copilot_login_runs_the_helper_with_this_python(self):
        self.assertEqual(si.recipe("copilot").command(), [sys.executable, str(si._HELPER), "copilot"])
        self.assertEqual(si._profile_home("default")["HERMES_HOME"], str(self.home))

    def test_too_many_sign_ins_at_once(self):
        with patch.dict(os.environ, {"FAKE_APPROVE_AFTER": "30"}), patch.object(si, "_MAX_SESSIONS", 1):
            si.start("default", "copilot-acp")
            with self.assertRaises(si.SignInError) as refused:
                si.start("default", "claude-code")
        self.assertEqual(refused.exception.code, "sign_in_busy")


class HelperTests(unittest.TestCase):
    def test_saves_the_token_where_hermes_model_does_and_never_prints_it(self):
        from loopdy_plugin import provider_sign_in_helper as helper
        saved = {}
        with patch("hermes_cli.copilot_auth.copilot_device_code_login", return_value="gho_fixture"), \
                patch("hermes_cli.config.save_env_value_secure", side_effect=saved.__setitem__), \
                patch("builtins.print") as printed:
            self.assertEqual(helper.main(["copilot"]), 0)
        self.assertEqual(saved, {"COPILOT_GITHUB_TOKEN": "gho_fixture"})
        printed.assert_not_called()
        with patch("hermes_cli.copilot_auth.copilot_device_code_login", return_value=None):
            self.assertEqual(helper.main(["copilot"]), 1)
        self.assertEqual(helper.main(["other"]), 2)


class OutputTests(unittest.TestCase):
    def test_reads_links_from_hyperlinks_and_text(self):
        text, links = si._visible("\x1b]8;id=1;https://claude.com/a?b=c\x07\x1b[94mhttps://claude.com/a\x1b[39m"
                                  "\x1b]8;;\x07\r\nWelcome\x1b[9Gto\x1b]52;c;Zm9v\x07")
        self.assertEqual(links, ["https://claude.com/a?b=c"])
        self.assertEqual(text, "https://claude.com/a\nWelcome to")

    def test_only_https_links_on_the_providers_domains(self):
        hosts = ("github.com",)
        self.assertTrue(si.allowed_link("https://github.com/login/device", hosts))
        self.assertTrue(si.allowed_link("https://www.github.com/login/device", hosts))
        for url in ("http://github.com/login/device", "https://github.com.evil.example/", "https://evilgithub.com/",
                    "https://user@github.com/", "https://github.com:8443/", "https://github.com/" + "a" * 4096):
            self.assertFalse(si.allowed_link(url, hosts), url)


class RouteTests(unittest.TestCase):
    """POST /native/provider-sign-in/* through Hermes' real auth middleware."""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
        from hermes_cli.dashboard_auth.registry import register_provider, unregister_global_provider
        from loopdy_plugin import native_api, room_activity
        from test_native_api import FixtureProvider

        hub = patch.object(room_activity, "_HUB", room_activity.RoomActivityHub())
        hub.start()
        self.addCleanup(hub.stop)
        temporary = tempfile.TemporaryDirectory(prefix="bighelp-sign-in-route-")
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {"HERMES_HOME": str(home), "HOME": str(home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.provider = FixtureProvider()
        register_provider(self.provider)
        self.addCleanup(unregister_global_provider, self.provider.name, self.provider)
        app = FastAPI()
        app.state.auth_required = True
        app.middleware("http")(gated_auth_middleware)
        app.include_router(native_api.router, prefix="/api/plugins/loopdy")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        self.addCleanup(si._sessions.clear)

    def headers(self):
        context = self.client.get("/api/plugins/loopdy/native/context", headers={"Authorization": "Bearer fixture-alice"})
        self.assertIn(si.CAPABILITY, context.json()["features"])
        return {"Authorization": "Bearer fixture-alice", "If-Match": context.headers["etag"],
                "X-Loopdy-Request-ID": str(uuid.uuid4())}

    def post(self, operation, body):
        return self.client.post(f"/api/plugins/loopdy/native/provider-sign-in/{operation}", headers=self.headers(),
                                json=body)

    def test_runs_each_operation_for_the_profile(self):
        snapshot = {"sessionId": str(uuid.uuid4()), "providerId": "copilot-acp", "status": si.WAITING}
        with patch.object(si, "providers", return_value=[{"providerId": "copilot-acp", "state": "ready"}]), \
                patch.object(si, "start", return_value=snapshot) as start, \
                patch.object(si, "status", return_value=snapshot) as status, \
                patch.object(si, "submit", return_value=snapshot) as submit, \
                patch.object(si, "cancel", return_value=snapshot) as cancel:
            listed = self.post("list", {"agentId": "default"})
            self.assertEqual(listed.status_code, 200, listed.text)
            self.assertEqual(listed.json(), {"agentId": "default",
                                             "providers": [{"providerId": "copilot-acp", "state": "ready"}]})
            self.assertEqual(listed.headers["cache-control"], "no-store")
            session = {"agentId": "default", "sessionId": snapshot["sessionId"]}
            self.assertEqual(self.post("start", {"agentId": "default", "providerId": "copilot-acp"}).json(),
                             {"agentId": "default", **snapshot})
            self.assertEqual(self.post("status", session).status_code, 200)
            self.assertEqual(self.post("submit", {**session, "code": "good#code"}).status_code, 200)
            self.assertEqual(self.post("cancel", session).status_code, 200)
        start.assert_called_once_with("default", "copilot-acp")
        status.assert_called_once_with("default", snapshot["sessionId"])
        submit.assert_called_once_with("default", snapshot["sessionId"], "good#code")
        cancel.assert_called_once_with("default", snapshot["sessionId"])

    def test_refusals_carry_their_code(self):
        response = self.post("start", {"agentId": "default", "providerId": "qwen-oauth"})
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["error"]["code"], "sign_in_retired")
        response = self.post("status", {"agentId": "default", "sessionId": str(uuid.uuid4())})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (404, "sign_in_not_found"))

    def test_rejects_bad_bodies_unknown_operations_profiles_and_missing_auth(self):
        self.assertEqual(self.post("list", {"agentId": "ghost"}).status_code, 404)
        self.assertEqual(self.post("run", {"agentId": "default"}).status_code, 404)
        self.assertEqual(self.post("start", {"agentId": "default", "providerId": "../x"}).status_code, 422)
        self.assertEqual(self.post("start", {"agentId": "default", "providerId": "x", "argv": ["sh"]}).status_code, 422)
        self.assertEqual(self.post("submit", {"agentId": "default", "sessionId": "x", "code": "c"}).status_code, 422)
        self.assertEqual(self.client.post("/api/plugins/loopdy/native/provider-sign-in/list",
                                          json={"agentId": "default"}).status_code, 401)
        headers = self.headers()
        del headers["If-Match"]
        self.assertEqual(self.client.post("/api/plugins/loopdy/native/provider-sign-in/list", headers=headers,
                                          json={"agentId": "default"}).status_code, 428)


if __name__ == "__main__":
    unittest.main()
