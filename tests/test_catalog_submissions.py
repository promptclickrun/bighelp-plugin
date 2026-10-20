import json
import os
import re
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
BLUEPRINT = {
    "kind": "blueprint", "board": "goals", "category": "personal", "goalCategory": "health",
    "text": "Add a goal to my Goals: walk [8,000] steps a day. Check in each evening.",
}
# The catalog's own example (services/catalog/test/variables.test.ts in the app repo).
FIELD_LEAD_VARIABLES = [
    {"key": "agent_role", "label": "Role", "type": "text", "required": True, "example": "Release coordinator",
     "help": "What this agent does for you, in a few words.", "maxLength": 80},
    {"key": "operating_context", "label": "Where it works", "type": "long_text", "required": False,
     "example": "A two-person studio shipping an iPhone app.", "maxLength": 600,
     "whenEmpty": "General work for the user."},
    {"key": "tone", "label": "Tone", "type": "choice", "options": ["Warm", "Direct", "Playful"], "default": "Direct"},
]
FIELD_LEAD = {
    "kind": "agent", "name": "Field Lead", "role": "{{agent_role}}", "vibe": "Confident, practical, quick",
    "description": "Turns intent into owners, milestones and decision points.",
    "instructions": "\n".join([
        "# {{agent_name}}", "", "You are {{agent_name}}, a {{agent_role}} for {{user_name}}.", "",
        "Operating context: {{operating_context}}", "", "Tone: {{tone}}. Keep the {{agent_role}} title to the work.",
    ]),
    "category": "productivity",
    "variables": FIELD_LEAD_VARIABLES,
}
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class FakeServers:
    """GitHub's device flow and the catalog Worker (services/catalog/src/index.ts), answering like the real ones."""

    def __init__(self):
        self.calls = []
        self.github_answers = [{"error": "authorization_pending"}, {"access_token": "gho_test"}]
        self.registered_with = None
        self.installs = {}
        self.statuses = {}
        self.submitted_today = 0

    def __call__(self, method, url, *, form=None, body=None, bearer=None):
        self.calls.append((url, form, body, bearer))
        if url == catalog.GITHUB_DEVICE_URL:
            return 200, {"device_code": "dev-1", "user_code": "ABCD-1234", "interval": 5, "expires_in": 900,
                         "verification_uri": "https://github.com/login/device"}
        if url == catalog.GITHUB_TOKEN_URL:
            return 200, self.github_answers.pop(0)
        path = url.split("://", 1)[1].split("/", 1)[1]
        if path == "agent/register":
            token = body.get("githubToken") if isinstance(body, dict) else None
            if not isinstance(token, str) or not token.strip() or len(token) > 512:
                return 400, {"error": "Send the GitHub token.", "field": "githubToken"}
            self.registered_with = body
            install = "t" * 43
            self.installs[install] = "samr"
            return 201, {"token": install, "login": "samr", "dailyLimit": 5}
        if path == "agent/revoke":
            revoked = isinstance(bearer, str) and self.installs.pop(bearer, None) is not None
            return 200, {"revoked": revoked}
        if path == "submit/agent/templates":
            if not (isinstance(bearer, str) and TOKEN_SHAPE.match(bearer) and bearer in self.installs):
                return 401, {"error": "This install isn't signed in. Run the catalog sign-in again."}
            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > 32 * 1024:
                return 413, {"error": "That's too long."}
            if self.submitted_today >= 5:
                return 429, {"error": "That's 5 templates today. Try again tomorrow."}
            self.submitted_today += 1
            return 201, {"id": "agent-1", "status": "pending", "statusToken": "s" * 43,
                         "credit": self.installs[bearer], "remainingToday": 5 - self.submitted_today}
        if path == "submit/status":
            tokens = [t for t in body.get("tokens", []) if isinstance(t, str) and TOKEN_SHAPE.match(t)][:50]
            rows = [{"id": i, "kind": "agent", "title": "Pilot", "createdAt": "2026-10-01T00:00:00Z",
                     **v, "reviewNote": v.get("reviewNote") if v.get("status") == "rejected" else None}
                    for i, v in self.statuses.items()] if tokens else []
            return 200, {"submissions": rows}
        raise AssertionError(url)

    def submissions(self):
        return [call for call in self.calls if call[0].endswith("/submit/agent/templates")]


class Approvals:
    """Hermes' approval prompt as the tools see it: the person's answer, or why nobody can answer."""

    def __init__(self, answer=None, unattended=None):
        self.answer = answer if answer is not None else {"approved": True, "message": None}
        self.unattended_reason = unattended
        self.asked = []

    def ask(self, description, rule_key):
        self.asked.append({"description": description, "rule_key": rule_key})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def unattended(self):
        return self.unattended_reason


class Clock:
    def __init__(self):
        self.value = 1_000_000.0

    def __call__(self):
        return self.value


class CatalogTestCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=os.environ.get("TMPDIR")))
        self.servers = FakeServers()
        self.clock = Clock()
        self.approvals = Approvals()
        self.patches = [
            patch.object(catalog, "_http", self.servers),
            patch.object(catalog, "_home", lambda: self.home),
            patch.dict(os.environ, {"BIGHELP_CATALOG_GITHUB_CLIENT_ID": "client-test", "BIGHELP_CATALOG_URL": "",
                                    "HERMES_SESSION_SOURCE": ""}),
        ]
        for item in self.patches:
            item.start()
        self.use_approvals(self.approvals)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def use_approvals(self, approvals):
        self.approvals = approvals
        for name, fake in (("_approve", lambda description, rule_key: approvals.ask(description, rule_key)),
                           ("_unattended", lambda: approvals.unattended())):
            # create=True: the tests run (and fail on their assertions) against a version without the gate.
            item = patch.object(catalog, name, fake, create=True)
            item.start()
            self.patches.append(item)

    def sign_in(self):
        catalog.start_login(self.home, now=self.clock)
        self.clock.value += 5
        self.assertEqual(catalog.poll_login(self.home, now=self.clock)["status"], "waiting")
        self.clock.value += 5
        return catalog.poll_login(self.home, now=self.clock)


class SignInTests(CatalogTestCase):
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

    def test_a_catalog_install_token_of_the_wrong_shape_is_not_kept(self):
        original = self.servers.__call__

        def odd_token(method, url, **kwargs):
            status, body = original(method, url, **kwargs)
            return (status, dict(body, token="not a catalog token")) if url.endswith("/agent/register") else (status, body)

        with patch.object(catalog, "_http", odd_token):
            with self.assertRaises(catalog.CatalogError):
                self.sign_in()
        self.assertIsNone(catalog.signed_in(self.home))

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


class CatalogAddressTests(CatalogTestCase):
    """BIGHELP_CATALOG_URL is for tests: the GitHub and catalog tokens only ever go to the catalog itself."""

    def test_the_override_needs_the_exact_catalog_host_or_a_loopback_address(self):
        cases = {
            "": catalog.DEFAULT_CATALOG_URL,
            "https://catalog.bighelp.app": catalog.DEFAULT_CATALOG_URL,
            "https://catalog.bighelp.app/": catalog.DEFAULT_CATALOG_URL,
            "https://catalog.bighelp.app:443": catalog.DEFAULT_CATALOG_URL,
            "http://127.0.0.1:8787": "http://127.0.0.1:8787",
            "http://127.0.0.1:8787/": "http://127.0.0.1:8787",
            "http://[::1]:8787": "http://[::1]:8787",
            # Look-alikes and other hosts fall back to the real catalog.
            "http://127.0.0.1.attacker.example": catalog.DEFAULT_CATALOG_URL,
            "http://127.0.0.1.attacker.example:8787": catalog.DEFAULT_CATALOG_URL,
            "http://127.0.0.1@attacker.example": catalog.DEFAULT_CATALOG_URL,
            "http://attacker.example#@127.0.0.1": catalog.DEFAULT_CATALOG_URL,
            "http://user:pass@127.0.0.1:8787": catalog.DEFAULT_CATALOG_URL,
            "https://attacker.example": catalog.DEFAULT_CATALOG_URL,
            "https://catalog.bighelp.app.attacker.example": catalog.DEFAULT_CATALOG_URL,
            "https://attacker.example/catalog.bighelp.app": catalog.DEFAULT_CATALOG_URL,
            "http://catalog.bighelp.app": catalog.DEFAULT_CATALOG_URL,
            "https://catalog.bighelp.app:8443": catalog.DEFAULT_CATALOG_URL,
            "http://127.0.0.1:8787/elsewhere": catalog.DEFAULT_CATALOG_URL,
            "http://127.0.0.1:99999": catalog.DEFAULT_CATALOG_URL,
            "ftp://127.0.0.1": catalog.DEFAULT_CATALOG_URL,
        }
        for value, expected in cases.items():
            with self.subTest(value=value), patch.dict(os.environ, {"BIGHELP_CATALOG_URL": value}):
                self.assertEqual(catalog.catalog_url(), expected)

    def test_a_look_alike_override_never_gets_the_tokens(self):
        with patch.dict(os.environ, {"BIGHELP_CATALOG_URL": "http://127.0.0.1.attacker.example"}):
            self.sign_in()
            json.loads(catalog.handle_submit(AGENT))
            catalog.logout(self.home)
        catalog_calls = [c for c in self.servers.calls if not c[0].startswith("https://github.com/")]
        self.assertGreaterEqual(len(catalog_calls), 3)  # register, submit, revoke
        for url, *_ in catalog_calls:
            self.assertTrue(url.startswith(catalog.DEFAULT_CATALOG_URL + "/"), url)
            self.assertNotIn("attacker", url)


class ConsentTests(CatalogTestCase):
    """A public post in the person's GitHub name needs the person's yes, every time, and a person to ask."""

    def test_submits_after_the_person_approves_and_keeps_the_receipt(self):
        self.sign_in()
        result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result, {"schema": catalog.SCHEMA, "version": 1, "ok": True, "status": "pending",
                                  "id": "agent-1", "credit": "samr", "remainingToday": 4})
        self.assertEqual(len(self.approvals.asked), 1)
        description = self.approvals.asked[0]["description"]
        for words in ("@samr", "public", "Template Catalog", "Pilot", "Travel planner", "plan day by day"):
            self.assertIn(words, description)
        self.assertTrue(self.approvals.asked[0]["rule_key"].startswith("bighelp-catalog-submit:"))
        sent = self.servers.submissions()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2]["name"], "Pilot")
        self.assertEqual(catalog.recent(self.home)[0]["status"], "pending")

    def test_a_no_sends_nothing(self):
        self.sign_in()
        self.use_approvals(Approvals(answer={"approved": False, "message": "BLOCKED: Action denied by user."}))
        result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result.get("error"), "not_approved")
        self.assertEqual(len(self.approvals.asked), 1)
        self.assertEqual(self.servers.submissions(), [])
        self.assertEqual(catalog.recent(self.home), [])

    def test_only_a_clear_yes_counts(self):
        self.sign_in()
        for answer in ({"approved": "yes"}, {}, None, RuntimeError("/Users/someone/.hermes")):
            with self.subTest(answer=answer):
                approvals = Approvals()
                approvals.answer = answer
                self.use_approvals(approvals)
                result = json.loads(catalog.handle_submit(AGENT))
                self.assertEqual(result.get("error"), "not_approved")
                self.assertNotIn("/Users", json.dumps(result))
        self.assertEqual(self.servers.submissions(), [])

    def test_unattended_runs_are_refused_without_asking(self):
        self.sign_in()
        for reason in ("approvals_off", "unattended", "approvals_unavailable"):
            with self.subTest(reason=reason):
                self.use_approvals(Approvals(unattended=reason))
                result = json.loads(catalog.handle_submit(AGENT))
                self.assertEqual(result.get("error"), "approval_unavailable")
                self.assertEqual(self.approvals.asked, [])
        self.assertEqual(self.servers.submissions(), [])

    def test_the_sign_in_changing_while_the_person_decides_sends_nothing(self):
        self.sign_in()
        approvals = Approvals()

        def ask_and_sign_out(description, rule_key):
            catalog._delete(catalog.state_dir(self.home), "install.json")
            return {"approved": True}

        approvals.ask = ask_and_sign_out
        self.use_approvals(approvals)
        result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result.get("error"), "not_signed_in")
        self.assertEqual(self.servers.submissions(), [])

    def test_asks_for_sign_in_before_asking_the_person(self):
        result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result.get("error"), "not_signed_in")
        self.assertEqual(self.approvals.asked, [])


class HermesGateTests(CatalogTestCase):
    """The real gate is Hermes' own, as for the template tools: no person to ask means no post."""

    def setUp(self):
        super().setUp()
        # Drop the fake approvals: these tests use the plugin's own gate.
        for item in self.patches[3:]:
            item.stop()
        del self.patches[3:]

    def test_without_a_person_to_ask_nothing_is_sent(self):
        import tools.approval as approval

        self.sign_in()
        with patch.object(approval, "_is_interactive_cli", return_value=False), \
                patch.object(approval, "_is_gateway_approval_context", return_value=False), \
                patch.dict(os.environ, {"HERMES_EXEC_ASK": ""}):
            result = json.loads(catalog.handle_submit(AGENT))
        self.assertIn(result.get("error"), ("not_approved", "approval_unavailable"))
        self.assertEqual(self.servers.submissions(), [])

    def test_scheduled_jobs_are_refused_even_when_cron_approves_on_its_own(self):
        import tools.approval as approval
        import tools.approval_context as approval_context

        self.sign_in()
        with patch.object(approval, "is_approval_bypass_active", return_value=False), \
                patch.object(approval_context, "_is_cron_approval_context", return_value=True), \
                patch.object(approval, "request_tool_approval", return_value={"approved": True}):
            result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result.get("error"), "approval_unavailable")
        self.assertEqual(self.servers.submissions(), [])

    def test_approvals_off_is_refused(self):
        import tools.approval as approval

        self.sign_in()
        with patch.object(approval, "is_approval_bypass_active", return_value=True), \
                patch.object(approval, "request_tool_approval", return_value={"approved": True}):
            result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result.get("error"), "approval_unavailable")
        self.assertEqual(self.servers.submissions(), [])

    def test_workflow_stages_are_refused(self):
        import tools.approval as approval

        self.sign_in()
        with patch.dict(os.environ, {"HERMES_SESSION_SOURCE": "workflow"}), \
                patch.object(approval, "request_tool_approval", return_value={"approved": True}):
            result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result.get("error"), "approval_unavailable")
        self.assertEqual(self.servers.submissions(), [])

    def test_the_person_is_asked_through_hermes_for_this_tool(self):
        import tools.approval as approval

        self.sign_in()
        with patch.object(approval, "is_approval_bypass_active", return_value=False), \
                patch.object(catalog, "_unattended", return_value=None, create=True), \
                patch.object(approval, "request_tool_approval", return_value={"approved": True}) as gate:
            result = json.loads(catalog.handle_submit(AGENT))
        self.assertTrue(result["ok"])
        self.assertEqual(gate.call_args.args[0], catalog.SUBMIT_TOOL)
        self.assertTrue(gate.call_args.kwargs["rule_key"].startswith("bighelp-catalog-submit:"))


def _secret(*parts):
    """Secret-shaped test text, assembled at run time."""
    return "".join(parts)


class SecretScanTests(CatalogTestCase):
    """The same scan as My Templates, before anything leaves the computer."""

    def test_templates_with_keys_or_passwords_never_leave_the_computer(self):
        self.sign_in()
        calls_before = len(self.servers.calls)
        long_tail = " Then plan each day with care and check the weather the night before."
        cases = {
            "password in the instructions": dict(AGENT, instructions=AGENT["instructions"] + " My password: "
                                                 + _secret("hunter2", "hunter2", "hunter2")),
            "token in the vibe": dict(AGENT, vibe=_secret("gh", "p_", "abcdefghijklmnopqrstuvwxyz0123")),
            "provider key in the description": dict(AGENT, description="Uses " + _secret("s", "k-", "proj-", "A1b2C3d4E5f6G7h8")),
            "cloud key in a blueprint": dict(BLUEPRINT, text="Check the bucket with " + _secret("AK", "IA", "ABCDEFGHIJKLMNOP") + long_tail),
            "secret in a variable's default": dict(FIELD_LEAD, variables=[
                dict(FIELD_LEAD_VARIABLES[0], default=_secret("api", "_key=", "abcdefgh12345678")),
                *FIELD_LEAD_VARIABLES[1:]]),
            "address with a password": dict(AGENT, instructions=AGENT["instructions"] + " Read "
                                            + _secret("https://sam:", "letmein99", "@example.com/feed")),
        }
        for name, template in cases.items():
            with self.subTest(name):
                result = json.loads(catalog.handle_submit(template))
                self.assertEqual(result.get("error"), "secret_scan_blocked")
        self.assertEqual(len(self.servers.calls), calls_before)
        self.assertEqual(self.approvals.asked, [])

    def test_scans_before_sign_in(self):
        result = json.loads(catalog.handle_submit(dict(AGENT, vibe=_secret("gh", "p_", "abcdefghijklmnopqrstuvwxyz0123"))))
        self.assertEqual(result.get("error"), "secret_scan_blocked")
        self.assertEqual(self.servers.calls, [])


class CatalogRulesTests(CatalogTestCase):
    """The catalog Worker's rules (services/catalog/src/templates.ts and variables.ts), checked here first."""

    def refused_field(self, template):
        with self.assertRaises(catalog.CatalogError) as caught:
            catalog.validate_template(template)
        self.assertEqual(caught.exception.code, "invalid_template")
        return caught.exception.field

    def with_variable(self, index, **change):
        return dict(FIELD_LEAD, variables=[dict(v, **change) if at == index else v
                                           for at, v in enumerate(FIELD_LEAD_VARIABLES)])

    def test_checks_the_template_before_asking_or_spending_a_submission(self):
        self.sign_in()
        calls_before = len(self.servers.calls)
        result = json.loads(catalog.handle_submit({**AGENT, "instructions": "too short"}))
        self.assertEqual((result.get("error"), result.get("field")), ("invalid_template", "instructions"))
        result = json.loads(catalog.handle_submit({"kind": "blueprint", "board": "kanban", "category": "personal",
                                                   "text": "x" * 30}))
        self.assertEqual(result.get("field"), "board")
        self.assertIn("board", result["message"])
        self.assertEqual(len(self.servers.calls), calls_before)
        self.assertEqual(self.approvals.asked, [])

    def test_agent_templates_use_the_ten_topics(self):
        topics = ["productivity", "business", "coding", "science", "education", "health", "creative", "personal",
                  "leadership", "security"]
        self.assertEqual(list(catalog.AGENT_CATEGORIES), topics)
        for topic in topics:
            self.assertEqual(catalog.validate_template(dict(AGENT, category=topic))["category"], topic)
        for retired in ("work", "learning", "research", "support", "fun"):
            self.assertEqual(self.refused_field(dict(AGENT, category=retired)), "category")
        categories = catalog.SUBMIT_PARAMETERS["properties"]["category"]
        self.assertEqual(set(categories["enum"]), set(topics) | set(catalog.BLUEPRINT_CATEGORIES))

    def test_blueprints(self):
        self.assertEqual(catalog.validate_template(BLUEPRINT), BLUEPRINT)
        self.assertEqual(catalog.validate_template(dict(BLUEPRINT, goalCategory=None))["goalCategory"], "other")
        self.assertNotIn("goalCategory", catalog.validate_template(dict(BLUEPRINT, board="feed")))
        self.assertEqual(self.refused_field(dict(BLUEPRINT, goalCategory="")), "goalCategory")
        self.assertEqual(self.refused_field(dict(BLUEPRINT, category="coding")), "category")
        self.assertEqual(self.refused_field(dict(BLUEPRINT, text="x" * 19)), "text")
        self.assertEqual(self.refused_field(dict(BLUEPRINT, text="x" * 1001)), "text")
        self.assertEqual(self.refused_field({"kind": "card"}), "kind")

    def test_agent_text_limits_count_like_the_catalog(self):
        self.assertEqual(catalog.validate_template(dict(AGENT, name="  Pilot \n  Two  "))["name"], "Pilot Two")
        self.assertEqual(self.refused_field(dict(AGENT, name="x" * 41)), "name")
        # The catalog counts UTF-16 code units: an emoji is two.
        self.assertEqual(catalog.validate_template(dict(AGENT, name="🧭" * 20))["name"], "🧭" * 20)
        self.assertEqual(self.refused_field(dict(AGENT, name="🧭" * 21)), "name")
        # One-line fields are checked at twice the limit before spaces collapse.
        self.assertEqual(self.refused_field(dict(AGENT, name="a" + " " * 80 + "b")), "name")
        self.assertEqual(self.refused_field(dict(AGENT, role="x")), "role")
        self.assertEqual(self.refused_field(dict(AGENT, vibe=7)), "vibe")
        self.assertEqual(self.refused_field(dict(AGENT, description="x" * 241)), "description")
        self.assertEqual(self.refused_field(dict(AGENT, instructions="x" * 12_001)), "instructions")
        self.assertEqual(catalog.validate_template(dict(AGENT, description="  "))
                         .get("description"), None)

    def test_variables_are_kept_and_sent(self):
        self.sign_in()
        template = catalog.validate_template(FIELD_LEAD)
        self.assertEqual(template["variables"], [
            FIELD_LEAD_VARIABLES[0], FIELD_LEAD_VARIABLES[1],
            {"key": "tone", "label": "Tone", "type": "choice", "required": True,
             "options": ["Warm", "Direct", "Playful"], "default": "Direct"},
        ])
        result = json.loads(catalog.handle_submit(FIELD_LEAD))
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.servers.submissions()[0][2]["variables"], template["variables"])
        self.assertIn("variables", catalog.SUBMIT_PARAMETERS["properties"])

    def test_templates_without_variables_work_as_before(self):
        plain = dict(FIELD_LEAD, role="Lead", instructions="You are {{agent_name}}. Help {{user_name}} plan the "
                     "week ahead, one small and honest step at a time.", variables=None)
        self.assertNotIn("variables", catalog.validate_template(plain))
        self.assertNotIn("variables", catalog.validate_template(dict(plain, variables=[])))

    def test_refuses_what_the_catalog_refuses_naming_the_same_field(self):
        v = FIELD_LEAD_VARIABLES
        thirteen = [{"key": f"v{i}", "label": f"V{i}", "type": "text"} for i in range(13)]
        number = {"key": "tone", "label": "Team size", "type": "number", "min": 1, "max": 50, "default": 4}
        cases = [
            (dict(FIELD_LEAD, variables=v[1:]), "role"),
            (dict(FIELD_LEAD, role="Lead", variables=v[1:]), "instructions"),
            (dict(FIELD_LEAD, description="Good at {{deadlines}} and more."), "description"),
            (dict(FIELD_LEAD, variables=[*v, {"key": "budget", "label": "Budget", "type": "number"}]), "variables[3].key"),
            (self.with_variable(0, key="agent_name"), "variables[0].key"),
            (self.with_variable(0, key="user_name"), "variables[0].key"),
            (self.with_variable(0, key="Agent Role"), "variables[0].key"),
            (self.with_variable(0, key="1role"), "variables[0].key"),
            (self.with_variable(0, key="a" + "b" * 40), "variables[0].key"),
            (dict(FIELD_LEAD, variables=[*v, v[2]]), "variables[3].key"),
            (dict(FIELD_LEAD, instructions=FIELD_LEAD["instructions"] + "\nUse {{ agent_role }} here."), "instructions"),
            (dict(FIELD_LEAD, instructions=FIELD_LEAD["instructions"] + "\nUse {{Role}} here."), "instructions"),
            (dict(FIELD_LEAD, instructions=FIELD_LEAD["instructions"] + "\nA lone }} here."), "instructions"),
            (dict(FIELD_LEAD, name="{{agent_name}}"), "name"),
            (dict(FIELD_LEAD, vibe="Calm {{tone}}"), "vibe"),
            (dict(FIELD_LEAD, variables="agent_role"), "variables"),
            (dict(FIELD_LEAD, role="Lead", variables=thirteen,
                  instructions=" ".join("{{%s}}" % t["key"] for t in thirteen).ljust(90, ".")), "variables"),
            (self.with_variable(0, label=""), "variables[0].label"),
            (self.with_variable(0, label="x" * 41), "variables[0].label"),
            (self.with_variable(0, type="date"), "variables[0].type"),
            (self.with_variable(0, required="yes"), "variables[0].required"),
            (self.with_variable(0, example="x" * 121), "variables[0].example"),
            (self.with_variable(0, help="x" * 161), "variables[0].help"),
            (self.with_variable(0, maxLength=201), "variables[0].maxLength"),
            (self.with_variable(0, maxLength=0), "variables[0].maxLength"),
            (self.with_variable(0, maxLength=True), "variables[0].maxLength"),
            (self.with_variable(1, maxLength=4001), "variables[1].maxLength"),
            (self.with_variable(0, default="x" * 81), "variables[0].default"),
            (self.with_variable(0, default="Use {{tone}}"), "variables[0].default"),
            (self.with_variable(1, whenEmpty="x" * 201), "variables[1].whenEmpty"),
            (self.with_variable(0, whenEmpty="Anything"), "variables[0].whenEmpty"),
            (self.with_variable(0, options=["A", "B"]), "variables[0].options"),
            (self.with_variable(0, min=1), "variables[0].min"),
            (self.with_variable(2, options=["Warm"]), "variables[2].options"),
            (self.with_variable(2, options=[f"O{i}" for i in range(13)]), "variables[2].options"),
            (self.with_variable(2, options=["Warm", "x" * 61]), "variables[2].options[1]"),
            (self.with_variable(2, options=["Warm", "Warm"]), "variables[2].options"),
            (self.with_variable(2, default="Grumpy"), "variables[2].default"),
            (self.with_variable(2, maxLength=20), "variables[2].maxLength"),
            (dict(FIELD_LEAD, variables=[v[0], v[1], dict(number, default=60)]), "variables[2].default"),
            (dict(FIELD_LEAD, variables=[v[0], v[1], dict(number, min=9, max=2)]), "variables[2].max"),
            (dict(FIELD_LEAD, variables=[v[0], v[1], dict(number, min="1")]), "variables[2].min"),
            (dict(FIELD_LEAD, variables=[v[0], v[1], dict(number, options=["A", "B"])]), "variables[2].options"),
            (dict(FIELD_LEAD, variables=[v[0], v[1], dict(number, default="   ")]), "variables[2].default"),
            (dict(FIELD_LEAD, variables=[v[0], v[1], dict(number, default="1_0")]), "variables[2].default"),
            (dict(FIELD_LEAD, variables=[v[0], v[1], dict(number, default=10 ** 400)]), "variables[2].default"),
            (dict(FIELD_LEAD, variables=[v[0], "tone"]), "variables[1]"),
        ]
        for template, field in cases:
            with self.subTest(field=field):
                self.assertEqual(self.refused_field(template), field)

    def test_accepts_what_the_catalog_accepts(self):
        v = FIELD_LEAD_VARIABLES
        number = {"key": "tone", "label": "Team size", "type": "number", "min": 1, "max": 50, "default": 4}
        self.assertTrue(catalog.validate_template(self.with_variable(1, maxLength=4000)))
        other = catalog.validate_template(self.with_variable(2, default="Grumpy", allowOther=True))
        self.assertEqual(other["variables"][2]["default"], "Grumpy")
        numbers = catalog.validate_template(dict(FIELD_LEAD, variables=[v[0], v[1], number]))
        self.assertEqual(numbers["variables"][2], {"key": "tone", "label": "Team size", "type": "number",
                                                   "required": True, "min": 1, "max": 50, "default": 4})
        as_text = catalog.validate_template(dict(FIELD_LEAD, variables=[v[0], v[1], dict(number, default=" 0x10 ")]))
        self.assertEqual(as_text["variables"][2]["default"], 16)
        # Unknown keys are dropped, as the catalog drops them.
        extra = catalog.validate_template(self.with_variable(0, color="blue"))
        self.assertNotIn("color", extra["variables"][0])

    def test_a_body_over_the_catalogs_32_kb_is_refused_here(self):
        self.sign_in()
        wide = dict(AGENT, instructions="You are {{agent_name}}. " + "時" * 11_900)
        result = json.loads(catalog.handle_submit(wide))
        self.assertEqual(result.get("error"), "too_large")
        self.assertEqual(self.servers.submissions(), [])
        self.assertEqual(self.approvals.asked, [])

    def test_catalog_refusals_come_back_with_their_field(self):
        self.sign_in()
        self.servers.submitted_today = 5
        result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result.get("error"), "catalog_429")
        self.assertIn("tomorrow", result["message"])


class ReviewAndRegistrationTests(CatalogTestCase):
    def test_a_revoked_install_is_forgotten_so_the_next_step_is_a_fresh_sign_in(self):
        self.sign_in()
        catalog._write(catalog.state_dir(self.home), "install.json", {"token": "x" * 43, "login": "samr"})
        result = json.loads(catalog.handle_submit(AGENT))
        self.assertEqual(result.get("error"), "not_signed_in")
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

    def test_registers_both_tools_in_the_templates_toolset(self):
        from loopdy_plugin.template_tools import TOOLSET as TEMPLATES_TOOLSET

        tools, hooks = {}, []
        ctx = SimpleNamespace(register_tool=lambda **kw: tools.__setitem__(kw["name"], kw),
                              register_hook=lambda name, fn: hooks.append(name))
        catalog.register(ctx)
        self.assertEqual(set(tools), {catalog.LOGIN_TOOL, catalog.SUBMIT_TOOL})
        self.assertEqual({t["toolset"] for t in tools.values()}, {"bighelp_templates"})
        self.assertEqual(TEMPLATES_TOOLSET, "bighelp_templates")
        self.assertEqual(hooks, ["post_llm_call"])


if __name__ == "__main__":
    unittest.main()
