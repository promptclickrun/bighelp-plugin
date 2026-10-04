import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loopdy_plugin import catalog_submissions as catalog

AGENT = {
    "kind": "agent", "name": "Pilot", "role": "Travel planner", "vibe": "Calm, organized",
    "category": "personal",
    "instructions": "You are {{agent_name}}, a travel planner. Ask for dates and budget first, then plan day by day.",
}


class FakeServers:
    """GitHub's device flow and the catalog, answering like the real ones do."""

    def __init__(self):
        self.calls = []
        self.github_answers = [{"error": "authorization_pending"}, {"access_token": "gho_test"}]
        self.registered_with = None
        self.statuses = {}

    def __call__(self, method, url, *, form=None, body=None, bearer=None):
        self.calls.append((url, form, body, bearer))
        if url == catalog.GITHUB_DEVICE_URL:
            return 200, {"device_code": "dev-1", "user_code": "ABCD-1234", "interval": 5, "expires_in": 900,
                         "verification_uri": "https://github.com/login/device"}
        if url == catalog.GITHUB_TOKEN_URL:
            return 200, self.github_answers.pop(0)
        if url.endswith("/agent/register"):
            self.registered_with = body
            return 201, {"token": "t" * 43, "login": "samr", "dailyLimit": 5}
        if url.endswith("/agent/revoke"):
            return 200, {"revoked": True}
        if url.endswith("/submit/agent/templates"):
            if bearer != "t" * 43:
                return 401, {"error": "This install isn't signed in."}
            return 201, {"id": "agent-1", "status": "pending", "statusToken": "s" * 43, "credit": "samr",
                         "remainingToday": 4}
        if url.endswith("/submit/status"):
            return 200, {"submissions": [{"id": i, **v} for i, v in self.statuses.items()]}
        raise AssertionError(url)


class Clock:
    def __init__(self):
        self.value = 1_000_000.0

    def __call__(self):
        return self.value


class CatalogSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.servers = FakeServers()
        self.clock = Clock()
        self.patches = [
            patch.object(catalog, "_http", self.servers),
            patch.object(catalog, "_home", lambda: self.home),
            patch.dict(os.environ, {"BIGHELP_CATALOG_GITHUB_CLIENT_ID": "client-test"}),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()

    def sign_in(self):
        catalog.start_login(self.home, now=self.clock)
        self.clock.value += 5
        self.assertEqual(catalog.poll_login(self.home, now=self.clock)["status"], "waiting")
        self.clock.value += 5
        return catalog.poll_login(self.home, now=self.clock)

    def test_sign_in_trades_the_github_token_and_keeps_only_the_install_token(self):
        self.assertEqual(self.sign_in(), {"status": "signed_in", "login": "samr"})
        self.assertEqual(self.servers.registered_with, {"githubToken": "gho_test"})
        directory = catalog.state_dir(self.home)
        saved = "".join(path.read_text() for path in directory.glob("*.json"))
        self.assertNotIn("gho_test", saved)
        self.assertNotIn("dev-1", saved)  # the pending sign-in is gone once it finishes
        mode = stat.S_IMODE((directory / "install.json").stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_starting_again_while_a_code_is_good_reuses_it(self):
        first = catalog.start_login(self.home, now=self.clock)
        self.clock.value += 60
        second = catalog.start_login(self.home, now=self.clock)
        self.assertEqual(first["userCode"], second["userCode"])
        device_requests = [c for c in self.servers.calls if c[0] == catalog.GITHUB_DEVICE_URL]
        self.assertEqual(len(device_requests), 1)

    def test_polls_no_faster_than_github_allows_and_backs_off_on_slow_down(self):
        self.servers.github_answers = [{"error": "slow_down"}, {"error": "authorization_pending"}]
        catalog.start_login(self.home, now=self.clock)
        catalog.poll_login(self.home, now=self.clock)  # too early: no request
        self.assertFalse(any(c[0] == catalog.GITHUB_TOKEN_URL for c in self.servers.calls))
        self.clock.value += 5
        catalog.poll_login(self.home, now=self.clock)  # slow_down: interval becomes 10
        self.clock.value += 5
        catalog.poll_login(self.home, now=self.clock)
        self.assertEqual(sum(c[0] == catalog.GITHUB_TOKEN_URL for c in self.servers.calls), 1)

    def test_declined_and_expired_sign_ins_end_cleanly(self):
        self.servers.github_answers = [{"error": "access_denied"}]
        catalog.start_login(self.home, now=self.clock)
        self.clock.value += 5
        self.assertEqual(catalog.poll_login(self.home, now=self.clock)["status"], "denied")
        self.assertIsNone(catalog.signed_in(self.home))
        self.clock.value += 2000
        self.assertEqual(catalog.poll_login(self.home, now=self.clock)["status"], "not_started")

    def test_without_a_client_id_it_says_so_instead_of_calling_github(self):
        with patch.dict(os.environ, {"BIGHELP_CATALOG_GITHUB_CLIENT_ID": ""}), \
                patch.object(catalog, "GITHUB_CLIENT_ID", ""):
            with self.assertRaises(catalog.CatalogError) as caught:
                catalog.start_login(self.home)
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertEqual(self.servers.calls, [])

    def test_login_tool_shows_the_code_link_and_a_qr_picture(self):
        with patch.object(catalog, "_poll_in_background") as poller:
            result = json.loads(catalog.handle_login({"action": "start"}))
        poller.assert_called_once()
        self.assertEqual(result["userCode"], "ABCD-1234")
        self.assertEqual(result["verificationUri"], "https://github.com/login/device")
        if catalog.qr_matrix("x") is not None:
            path = Path(result["media"].removeprefix("MEDIA:"))
            self.assertTrue(path.read_bytes().startswith(b"\x89PNG"))
            self.assertIn("cache", path.parts)  # Hermes delivers chat media from its cache unconditionally
        self.assertNotIn("dev-1", json.dumps(result))

    def test_text_qr_has_one_line_per_two_module_rows(self):
        # Dark modules print as spaces and light ones as blocks; an odd last row is padded with light.
        matrix = [[True, False], [False, True], [True, True]]
        self.assertEqual(catalog.qr_text(matrix), "▄▀\n▄▄")

    def test_png_is_a_valid_grayscale_image(self):
        png = catalog.qr_png([[True, False], [False, True]], scale=2)
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(png[16:24], (4).to_bytes(4, "big") * 2)

    def test_checks_the_template_before_spending_a_submission(self):
        self.sign_in()
        calls_before = len(self.servers.calls)
        result = json.loads(catalog.handle_submit({**AGENT, "instructions": "too short"}))
        self.assertEqual(result["error"], "invalid_template")
        result = json.loads(catalog.handle_submit({"kind": "blueprint", "board": "kanban", "category": "personal",
                                                   "text": "x" * 30}))
        self.assertIn("board", result["message"])
        self.assertEqual(len(self.servers.calls), calls_before)

    def test_submits_and_keeps_the_receipt(self):
        self.sign_in()
        result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result, {"schema": catalog.SCHEMA, "version": 1, "ok": True, "status": "pending",
                                  "id": "agent-1", "credit": "samr", "remainingToday": 4})
        sent = next(c for c in self.servers.calls if c[0].endswith("/submit/agent/templates"))
        self.assertEqual(sent[2]["name"], "Pilot")
        self.assertEqual(catalog.recent(self.home)[0]["status"], "pending")

    def test_asks_for_sign_in_when_not_signed_in(self):
        result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result["error"], "not_signed_in")

    def test_a_revoked_install_is_forgotten_so_the_next_step_is_a_fresh_sign_in(self):
        self.sign_in()
        catalog._write(catalog.state_dir(self.home), "install.json", {"token": "x" * 43, "login": "samr"})
        result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result["error"], "not_signed_in")
        self.assertIsNone(catalog.signed_in(self.home))

    def test_review_results_reach_feed_once(self):
        self.sign_in()
        catalog.submit(AGENT, self.home)
        posted = []
        self.servers.statuses = {"agent-1": {"status": "pending"}}
        self.assertEqual(catalog.check_statuses(self.home, notify=posted.append, now=self.clock), [])
        self.servers.statuses = {"agent-1": {"status": "rejected", "reviewNote": "Too close to Anchor."}}
        self.clock.value += 60
        self.assertEqual(catalog.check_statuses(self.home, notify=posted.append, now=self.clock), [])  # throttled
        self.clock.value += catalog.STATUS_CHECK_SECONDS
        catalog.check_statuses(self.home, notify=posted.append, now=self.clock)
        catalog.check_statuses(self.home, force=True, notify=posted.append, now=self.clock)
        self.assertEqual([(p["id"], p["status"], p["reviewNote"]) for p in posted],
                         [("agent-1", "rejected", "Too close to Anchor.")])

    def test_logout_revokes_and_removes_the_token(self):
        self.sign_in()
        self.assertEqual(catalog.logout(self.home), {"status": "signed_out", "login": "samr"})
        self.assertIn(("t" * 43), [c[3] for c in self.servers.calls if c[0].endswith("/agent/revoke")])
        self.assertIsNone(catalog.signed_in(self.home))

    def test_cli_login_prints_the_code_and_waits_for_approval(self):
        lines = []
        with patch.object(catalog, "wait_for_login", return_value={"status": "signed_in", "login": "samr"}):
            catalog.handle_cli(SimpleNamespace(bighelp_catalog_action="login"), out=lines.append)
        text = "\n".join(lines)
        self.assertIn("https://github.com/login/device", text)
        self.assertIn("ABCD-1234", text)
        self.assertIn("Signed in as @samr.", text)

    def test_registers_both_tools_in_the_bighelp_toolset(self):
        tools, hooks = {}, []
        ctx = SimpleNamespace(register_tool=lambda **kw: tools.__setitem__(kw["name"], kw),
                              register_hook=lambda name, fn: hooks.append(name))
        catalog.register(ctx)
        self.assertEqual(set(tools), {catalog.LOGIN_TOOL, catalog.SUBMIT_TOOL})
        self.assertTrue(all(t["toolset"] == "bighelp" for t in tools.values()))
        self.assertEqual(hooks, ["post_llm_call"])


if __name__ == "__main__":
    unittest.main()
