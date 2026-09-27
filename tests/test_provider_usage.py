"""Usage and limits for the tools on this host: detection, each provider's source, and the native route."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from loopdy_plugin import provider_usage as pu


def executable(directory: Path, name: str, body: str) -> str:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


# Speaks the Codex app server's JSON-lines protocol (no "jsonrpc" field on the wire).
CODEX = """
import json, sys
account = json.loads(sys.argv[2]) if len(sys.argv) > 2 else None
limits = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
for line in sys.stdin:
    message = json.loads(line)
    assert "jsonrpc" not in message
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        sys.stdout.write(json.dumps({"method": "remoteControl/status/changed", "params": {}}) + "\\n")
        result = {"userAgent": "fixture"}
    elif method == "account/read":
        result = {"account": account, "requiresOpenaiAuth": True}
    else:
        result = limits
    sys.stdout.write(json.dumps({"id": message["id"], "result": result}) + "\\n")
    sys.stdout.flush()
"""

# Speaks the Copilot CLI's Content-Length framed JSON-RPC and records whether a token was passed.
COPILOT = """
import json, os, sys
def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit(0)
        if line in (b"\\r\\n", b"\\n"):
            break
        name, _, value = line.decode().partition(":")
        if name.lower() == "content-length":
            length = int(value)
    return json.loads(sys.stdin.buffer.read(length))
def send(value):
    body = json.dumps(value).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
    sys.stdout.buffer.flush()
Path = os.environ.get("FIXTURE_RECORD")
open(Path, "w").write(json.dumps({"argv": sys.argv[1:], "token": os.environ.get("COPILOT_SDK_AUTH_TOKEN")}))
while True:
    message = read()
    assert message["jsonrpc"] == "2.0"
    if "method" not in message:
        assert message["id"] == 99 and message["error"]["code"] == -32601  # our refusal of its request
        continue
    if message["method"] == "connect":
        send({"jsonrpc": "2.0", "id": 99, "method": "session.ask", "params": {}})  # a server request we must refuse
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"ok": True}})
    else:
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"quotaSnapshots": {"premium_interactions": {
            "isUnlimitedEntitlement": False, "entitlementRequests": 300, "usedRequests": 150,
            "remainingPercentage": 50, "tokenBasedBilling": False}}}})
"""

OPENCODE = """
import sys
days = sys.argv[sys.argv.index("--days") + 1]
assert "--pure" in sys.argv
cost = {"7": "$1.50", "30": "$42.10"}[days]
print("┌──────────────────┐")
print("│                       OVERVIEW                         │")
print("│Sessions                                             45 │")
print("│Total Cost                                      " + cost + " │")
print("│Input                                              3.1M │")
print("│Output                                           120.5K │")
print("│ read               ███ 3576 (33.5%)   │")
"""


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bighelp-usage-")
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.clis: dict[str, str] = {}
        self.env: dict[str, str] = {}
        self.keys: dict[str, tuple[str, str]] = {}
        self.http: dict[str, object] = {}
        self.seen: list[tuple[str, dict]] = []
        patches = [
            patch.object(pu, "find_cli", side_effect=lambda name: self.clis.get(name)),
            patch.object(pu, "hermes_env", side_effect=lambda *names: next(
                (self.env[name] for name in names if self.env.get(name)), None)),
            patch.object(pu, "hermes_key", side_effect=lambda provider: self.keys.get(provider)),
            patch.object(pu, "http_json", side_effect=self.fake_http),
            patch.object(pu, "claude_code_logins", return_value=[]),
            patch.object(pu, "hermes_claude_token", return_value=None),
            patch.object(pu, "_hermes_has_codex", return_value=False),
            patch.object(pu, "hermes_account_usage", return_value=None),
            patch.object(pu, "active_provider", return_value=None),
            patch.object(pu, "detect_nous", return_value=None),
            patch.object(pu, "detect_hermes_hooks", return_value=[]),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        pu._cache.clear()

    def fake_http(self, url, headers, **_):
        self.seen.append((url, headers))
        answer = self.http.get(url.split("?")[0])
        if isinstance(answer, int):
            raise pu.HTTPStatus(answer)
        if answer is None:
            raise pu.Unavailable(pu.ERROR, "Couldn't reach the provider.")
        return answer

    def reports(self):
        return {report["id"]: report for report in pu.collect(deadline=10)}


class ClaudeTests(Base):
    login = {"accessToken": "sk-ant-oat-fixture", "expiresAt": (time.time() + 3600) * 1000,
             "subscriptionType": "max", "rateLimitTier": "default_claude_max_5x"}

    def test_claude_code_login_reads_known_windows_and_plan(self):
        self.clis["claude"] = "/bin/claude"
        pu.claude_code_logins.return_value = [self.login]
        self.http[pu._CLAUDE_URL] = {
            "five_hour": {"utilization": 42.0, "resets_at": "2026-10-02T18:00:00.003624+00:00"},
            "seven_day": {"utilization": 61.0, "resets_at": "2026-09-30T09:00:00Z"},
            "unnamed_bucket": {"utilization": 3.0},  # unnamed internal buckets are not shown
            "extra_usage": {"is_enabled": True, "used_credits": 25, "monthly_limit": 100, "utilization": None},
        }
        report = self.reports()["claude"]
        self.assertEqual((report["status"], report["plan"], report["detectedVia"]), ("ok", "Max 5x", ["cli"]))
        self.assertEqual([(w["label"], w["usedPercent"]) for w in report["windows"]],
                         [("Session (5 hours)", 42.0), ("Week", 61.0), ("Extra usage this month", 25.0)])
        self.assertEqual(report["windows"][0]["resetsAt"], "2026-10-02T18:00:00Z")
        self.assertEqual(self.seen[0][1]["Authorization"], "Bearer sk-ant-oat-fixture")
        self.assertNotIn("sk-ant", json.dumps(report))

    def test_expired_login_is_never_refreshed(self):
        pu.claude_code_logins.return_value = [{**self.login, "expiresAt": (time.time() - 60) * 1000}]
        report = self.reports()["claude"]
        self.assertEqual(report["status"], "signInNeeded")
        self.assertIn("Open Claude Code", report["message"])
        self.assertEqual(self.seen, [])

    def test_api_key_only_explains_usage_is_not_shared(self):
        self.env["ANTHROPIC_API_KEY"] = "sk-ant-api-fixture"
        self.assertEqual(self.reports()["claude"]["status"], "notShared")

    def test_rejected_login_asks_to_refresh(self):
        pu.claude_code_logins.return_value = [self.login]
        self.http[pu._CLAUDE_URL] = 401
        self.assertEqual(self.reports()["claude"]["status"], "signInNeeded")

    def test_nothing_installed_or_configured_shows_nothing(self):
        self.assertEqual(self.reports(), {})


class CodexTests(Base):
    limits = {"rateLimits": {"limitId": "codex", "planType": "pro",
                             "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1791046775},
                             "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1791300000},
                             "credits": {"hasCredits": True, "unlimited": False, "balance": "4.5"}},
              "rateLimitsByLimitId": {"codex_spark": {"limitId": "codex_spark", "limitName": "spark",
                                                      "primary": {"usedPercent": 5, "windowDurationMins": 10080}}},
              "rateLimitResetCredits": {"availableCount": 2}, "accountId": "account-a"}

    def run_codex(self, account, limits):
        script = executable(self.dir, "codex", CODEX)  # takes its answers as arguments after "app-server"
        real = pu.Rpc

        def rpc(argv, **kwargs):
            return real([*argv, json.dumps(account), json.dumps(limits)], **kwargs)
        with patch.object(pu, "Rpc", side_effect=rpc):
            return pu.codex_app_server(script)

    def test_app_server_limits_are_labeled_by_duration(self):
        result = self.run_codex({"type": "chatgpt", "planType": "pro"}, self.limits)
        report = pu._codex_report(result, pu._Source("codex", "Codex", ("cli",), list), "codex", "Codex", ("cli",))
        self.assertEqual([(w.label, w.used_percent) for w in report.windows],
                         [("Session (5 hours)", 12.0), ("Week", 40.0), ("Spark · Week", 5.0)])
        self.assertEqual(report.plan, "Pro")
        self.assertEqual(report.facts, [("Credits", "$4.50"), ("Banked resets", "2")])

    def test_app_server_without_a_login_asks_to_sign_in(self):
        with self.assertRaises(pu.Unavailable) as raised:
            self.run_codex(None, {})
        self.assertEqual(raised.exception.status, "signInNeeded")
        with self.assertRaises(pu.Unavailable) as raised:
            self.run_codex({"type": "apiKey"}, {})
        self.assertEqual(raised.exception.status, "notShared")

    def test_hermes_account_is_merged_when_same_and_listed_when_different(self):
        self.clis["codex"] = "/bin/codex"
        pu.active_provider.return_value = "openai-codex"
        raw = {"plan_type": "plus", "account_id": "account-a", "credits": {},
               "rate_limit": {"primary_window": {"used_percent": 7, "limit_window_seconds": 604800, "reset_at": 1791300000}}}
        with patch.object(pu, "codex_app_server", return_value=self.limits), \
                patch.object(pu, "hermes_account_usage", return_value=SimpleNamespace(raw=raw)):
            merged = pu.collect(deadline=10)
            self.assertEqual([(r["id"], r["detectedVia"], r["activeInHermes"]) for r in merged],
                             [("codex", ["cli", "hermes"], True)])
            raw["account_id"] = "account-b"
            pu._cache.clear()
            split = pu.collect(deadline=10)
        self.assertEqual([(r["id"], r["activeInHermes"]) for r in split], [("codex-hermes", True), ("codex", False)])
        self.assertEqual(split[0]["windows"][0]["label"], "Week")
        self.assertNotIn("account-", json.dumps(split))

    def test_hermes_only_codex(self):
        pu._hermes_has_codex.return_value = True
        raw = {"plan_type": "pro", "account_id": "a",
               "rate_limit": {"primary_window": {"used_percent": 1, "limit_window_seconds": 604800}}}
        with patch.object(pu, "hermes_account_usage", return_value=SimpleNamespace(raw=raw)):
            report = self.reports()["codex"]
        self.assertEqual((report["detectedVia"], report["windows"][0]["label"]), (["hermes"], "Week"))


class CopilotTests(Base):
    payload = {"copilot_plan": "enterprise", "token_based_billing": True, "quota_reset_date_utc": "2026-10-01T00:00:00Z",
               "login": "someone", "quota_snapshots": {"premium_interactions": {
                   "unlimited": False, "entitlement": 1_000_000, "credits_used": 250_000, "remaining": 750_000,
                   "percent_remaining": 75.0, "overage_count": 0}}}

    def test_exact_credits_from_github(self):
        self.env["COPILOT_GITHUB_TOKEN"] = "ghu_fixture"
        self.http[pu._COPILOT_URL] = self.payload
        report = self.reports()["copilot"]
        self.assertEqual((report["plan"], report["approximate"], report["detectedVia"]), ("Enterprise", False, ["hermes"]))
        self.assertEqual(report["windows"], [{"label": "Monthly credits", "usedPercent": 25.0,
                                              "resetsAt": "2026-10-01T00:00:00Z", "detail": "250,000 of 1,000,000 used"}])
        self.assertNotIn("someone", json.dumps(report))

    def test_classic_tokens_are_skipped_and_github_logins_without_copilot_are_dropped(self):
        self.env["GH_TOKEN"] = "ghp_classic"
        self.clis["gh"] = "/bin/gh"
        with patch.object(pu, "run", return_value="gho_fixture\n"):
            self.http[pu._COPILOT_URL] = 404
            self.assertEqual(self.reports(), {})
        self.assertEqual(self.seen[0][1]["Authorization"], "Bearer gho_fixture")

    def test_rejected_token_falls_back_to_the_cli_login(self):
        record = self.dir / "record.json"
        self.clis["copilot"] = executable(self.dir, "copilot", COPILOT)
        self.env["COPILOT_GITHUB_TOKEN"] = "ghu_expired"
        self.http[pu._COPILOT_URL] = 401
        with patch.dict(os.environ, {"FIXTURE_RECORD": str(record)}):
            report = self.reports()["copilot"]
        self.assertEqual((report["status"], report["approximate"]), ("ok", True))
        self.assertEqual(report["windows"][0]["detail"], "150 of 300 used")
        self.assertIsNone(json.loads(record.read_text())["token"])  # the CLI used its own login


class ApiKeyProviderTests(Base):
    def test_deepseek_balance(self):
        self.keys["deepseek"] = ("sk-fixture", "https://api.deepseek.com/v1")
        self.http["https://api.deepseek.com/user/balance"] = {"is_available": False, "balance_infos": [
            {"currency": "USD", "total_balance": "12.50", "granted_balance": "2.50", "topped_up_balance": "10.00"},
            {"currency": "CNY", "total_balance": "7", "granted_balance": "0", "topped_up_balance": "7"}]}
        report = self.reports()["deepseek"]
        self.assertEqual(report["facts"], [
            {"label": "Balance", "value": "$12.50"}, {"label": "Granted", "value": "$2.50"},
            {"label": "Topped up", "value": "$10.00"}, {"label": "Balance", "value": "¥7.00"},
            {"label": "Topped up", "value": "¥7.00"}])
        self.assertIn("too low", report["message"])

    def test_openrouter_key_limit_and_balance_even_when_credits_are_forbidden(self):
        self.keys["openrouter"] = ("sk-or-fixture", "https://example.invalid/proxy")  # never sent off OpenRouter
        self.http["https://openrouter.ai/api/v1/key"] = {"data": {
            "limit": 20, "limit_remaining": 15, "limit_reset": "monthly", "usage_daily": 0.5, "usage_monthly": 5,
            "is_free_tier": False}}
        self.http["https://openrouter.ai/api/v1/credits"] = 403
        report = self.reports()["openrouter"]
        self.assertEqual(report["windows"], [{"label": "Key limit (monthly)", "usedPercent": 25.0, "resetsAt": None,
                                              "detail": "$15.00 of $20.00 left"}])
        self.assertEqual([fact["label"] for fact in report["facts"]], ["Spent today", "Spent this month"])
        self.assertTrue(all(url.startswith("https://openrouter.ai/") for url, _ in self.seen))

    def test_gemini_key_is_checked_but_usage_is_not_shared(self):
        self.keys["gemini"] = ("AIza-fixture", "https://generativelanguage.googleapis.com/v1beta")
        self.http["https://generativelanguage.googleapis.com/v1beta/models"] = {"models": []}
        report = self.reports()["gemini"]
        self.assertEqual((report["name"], report["status"]), ("Google AI Studio", "notShared"))
        self.assertEqual(self.seen[0][1], {"x-goog-api-key": "AIza-fixture"})
        self.http["https://generativelanguage.googleapis.com/v1beta/models"] = 400
        pu._cache.clear()
        self.assertEqual(self.reports()["gemini"]["status"], "signInNeeded")


class OpenCodeTests(Base):
    def test_local_stats_from_the_cli(self):
        self.clis["opencode"] = executable(self.dir, "opencode", OPENCODE)
        report = self.reports()["opencode"]
        self.assertEqual(report["facts"], [
            {"label": "Last 7 days", "value": "$1.50 · 45 sessions"},
            {"label": "Last 30 days", "value": "$42.10 · 45 sessions"},
            {"label": "Tokens (30 days)", "value": "3.1M in · 120.5K out"}])

    def test_go_limits_through_hermes_and_zen_only(self):
        self.keys["opencode-go"] = ("key", "https://opencode.ai/zen/go")
        window = SimpleNamespace(label="Weekly", used_percent=30.0, reset_at=None, detail=None)
        with patch.object(pu, "hermes_account_usage", return_value=SimpleNamespace(windows=[window])):
            report = self.reports()["opencode"]
        self.assertEqual((report["plan"], report["windows"][0]["label"]), ("Go", "Weekly"))
        del self.keys["opencode-go"]
        self.keys["opencode-zen"] = ("key", "https://opencode.ai/zen")
        pu._cache.clear()
        self.assertEqual(self.reports()["opencode"]["status"], "notShared")


class CollectionTests(Base):
    def test_slow_or_broken_sources_do_not_hold_or_leak(self):
        def slow():
            time.sleep(5)
            return []

        def broken():
            raise RuntimeError("/Users/someone/.secret token=abc")
        sources = [pu._Source("slow", "Slow", ("cli",), slow), pu._Source("broken", "Broken", ("hermes",), broken)]
        with patch.object(pu, "detect", return_value=sources):
            started = time.monotonic()
            reports = pu.collect(deadline=0.5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual([(r["id"], r["status"]) for r in reports], [("slow", "error"), ("broken", "error")])
        self.assertIn("took too long", reports[0]["message"])
        self.assertNotIn("secret", json.dumps(reports))

    def test_active_hermes_provider_is_marked_and_first(self):
        self.keys["deepseek"] = ("sk", "")
        self.http["https://api.deepseek.com/user/balance"] = {"balance_infos": [{"currency": "USD", "total_balance": "1"}]}
        pu.claude_code_logins.return_value = [ClaudeTests.login]
        self.http[pu._CLAUDE_URL] = {"five_hour": {"utilization": 1}}
        pu.active_provider.return_value = "deepseek"
        reports = pu.collect(deadline=10)
        self.assertEqual([(r["id"], r["activeInHermes"]) for r in reports], [("deepseek", True), ("claude", False)])
        self.assertEqual(pu.usage_id("claude-subscription-directsdk-experimental"), "claude")

    def test_cache_and_refresh_throttle(self):
        now = [1000.0]
        with patch.object(pu, "collect", side_effect=lambda: [{"id": str(now[0])}]) as collect:
            first = pu.usage("default", clock=lambda: now[0])
            self.assertFalse(first["cached"])
            now[0] += 10
            self.assertTrue(pu.usage("default", refresh=True, clock=lambda: now[0])["cached"])  # too soon
            now[0] += 10
            self.assertFalse(pu.usage("default", refresh=True, clock=lambda: now[0])["cached"])
            now[0] += 100
            self.assertTrue(pu.usage("default", clock=lambda: now[0])["cached"])
            now[0] += pu.CACHE_SECONDS
            self.assertFalse(pu.usage("default", clock=lambda: now[0])["cached"])
            self.assertFalse(pu.usage("other", clock=lambda: now[0])["cached"])
        self.assertEqual(collect.call_count, 4)
        self.assertEqual(first["fetchedAt"], "1970-01-01T00:16:40Z")


class HelperTests(unittest.TestCase):
    def test_times_and_durations(self):
        self.assertEqual(pu._iso(pu._when(1791046775)), "2026-10-03T16:59:35Z")
        self.assertEqual(pu._iso(pu._when(1791046775000)), "2026-10-03T16:59:35Z")
        self.assertEqual(pu._iso(pu._when("1791046775")), "2026-10-03T16:59:35Z")
        self.assertEqual(pu._iso(pu._when("2026-10-09T04:46:04.000Z")), "2026-10-09T04:46:04Z")
        self.assertIsNone(pu._when("soon"))
        self.assertEqual([pu._duration(m) for m in (300, 60, 1440, 10080, 43200, 90)],
                         ["Session (5 hours)", "Hour", "Day", "Week", "30 days", "90 minutes"])

    def test_cli_search_includes_installer_locations(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin"}):
            path = pu._search_path().split(os.pathsep)
        self.assertEqual(path[0], "/usr/bin")
        self.assertIn(os.path.expanduser("~/.opencode/bin"), path)
        self.assertEqual(path.count("/usr/bin"), 1)

    def test_credentials_never_follow_redirects(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                received.append((self.path, self.headers.get("Authorization")))
                if self.path == "/start":
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{server.server_port}/elsewhere")
                    self.end_headers()
                    return
                body = b"{}"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with self.assertRaises(pu.HTTPStatus) as raised:
            pu.http_json(f"http://127.0.0.1:{server.server_port}/start", {"Authorization": "Bearer secret"})
        self.assertEqual(raised.exception.code, 302)
        self.assertEqual(received, [("/start", "Bearer secret")])


class RouteTests(unittest.TestCase):
    """POST /native/usage/list through Hermes' real auth middleware."""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
        from hermes_cli.dashboard_auth.registry import register_provider, unregister_global_provider
        from loopdy_plugin import native_api, room_activity
        from test_native_api import FixtureProvider

        for item in (patch.object(room_activity, "_HUB", room_activity.RoomActivityHub()),):
            item.start()
            self.addCleanup(item.stop)
        temporary = tempfile.TemporaryDirectory(prefix="bighelp-usage-route-")
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
        pu._cache.clear()

    def headers(self):
        context = self.client.get("/api/plugins/loopdy/native/context", headers={"Authorization": "Bearer fixture-alice"})
        self.assertIn(pu.CAPABILITY, context.json()["features"])
        return {"Authorization": "Bearer fixture-alice", "If-Match": context.headers["etag"],
                "X-Loopdy-Request-ID": str(uuid.uuid4())}

    def test_lists_usage_for_the_profile(self):
        from hermes_constants import get_hermes_home
        homes = []

        def collect():
            homes.append(str(get_hermes_home()))
            return [{"id": "claude", "status": "ok"}]
        with patch.object(pu, "collect", side_effect=collect):
            response = self.client.post("/api/plugins/loopdy/native/usage/list", headers=self.headers(),
                                        json={"agentId": "default", "refresh": True})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual((value["agentId"], value["providers"], value["cached"]), ("default", [{"id": "claude", "status": "ok"}], False))
        self.assertEqual(homes, [os.environ["HERMES_HOME"]])
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_rejects_unknown_profiles_bad_bodies_and_missing_auth(self):
        path = "/api/plugins/loopdy/native/usage/list"
        with patch.object(pu, "collect", side_effect=AssertionError("must not run")):
            self.assertEqual(self.client.post(path, headers=self.headers(), json={"agentId": "ghost"}).status_code, 404)
            self.assertEqual(self.client.post(path, headers=self.headers(),
                                              json={"agentId": "default", "refresh": "yes"}).status_code, 422)
            self.assertEqual(self.client.post(path, json={"agentId": "default"}).status_code, 401)
            headers = self.headers()
            del headers["If-Match"]
            self.assertEqual(self.client.post(path, headers=headers, json={"agentId": "default"}).status_code, 428)


if __name__ == "__main__":
    unittest.main()
