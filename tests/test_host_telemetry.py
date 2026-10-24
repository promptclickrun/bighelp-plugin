"""Host telemetry: nothing private in a batch, every off switch, the queue, and one stream per process."""
from __future__ import annotations

import getpass
import importlib
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from loopdy_plugin import host_telemetry as telemetry
from loopdy_plugin import registration

ROOT = Path(__file__).resolve().parents[1]
UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

# The ingestion contract, written out again here on purpose: the module's own lists must agree with it.
MS = ("int", 0, 86_400_000)
COUNT = ("int", 0, 100_000)
ALERT_TYPES = {"session.completed", "session.failed", "scheduled.completed", "scheduled.failed",
               "approval.required", "clarification.required", "subagent.completed", "subagent.failed",
               "wake", "liveactivity"}
CONTRACT = {
    "host.started": {},
    "turn.completed": {
        "durationMs": MS, "firstTokenMs": MS, "toolCalls": COUNT,
        "outcome": {"ok", "failed", "cancelled"},
        "provider": re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$"),
        "model": re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,63}$"),
        "sessionSource": {"bighelp", "cli", "cron", "api", "telegram", "discord", "slack", "whatsapp",
                          "signal", "email", "webhook", "other"},
    },
    "tool.completed": {"durationMs": MS, "outcome": {"ok", "failed"},
                       "tool": re.compile(r"^[a-z][a-z0-9_]{0,63}$")},
    "alert.queued": {"eventType": ALERT_TYPES, "queueMs": MS},
    "alert.sent": {"eventType": ALERT_TYPES, "relayMs": MS, "result": re.compile(r"^[a-z0-9_]{1,64}$"),
                   "attempt": COUNT},
    "error": {"code": re.compile(r"^[a-z0-9_]{1,64}$"), "stage": re.compile(r"^[a-z0-9_]{1,64}$")},
}
CONTEXT = {
    "pluginVersion": re.compile(r"^[0-9]{1,4}(\.[0-9]{1,4}){0,3}$"),
    "hermesVersion": re.compile(r"^[0-9]{1,4}(\.[0-9]{1,4}){0,3}([.+-][0-9A-Za-z]{1,16})?$"),
    "osFamily": {"linux", "macos", "windows", "other"},
    "python": re.compile(r"^3\.[0-9]{1,2}$"),
    "agentCount": {"1", "2-5", "6-20", "21+"},
}


def fits(value, rule) -> bool:
    if isinstance(rule, set):
        return isinstance(value, str) and value in rule
    if isinstance(rule, re.Pattern):
        return isinstance(value, str) and rule.match(value) is not None
    kind, low, high = rule
    return type(value) is int and low <= value <= high


def assert_contract(test: unittest.TestCase, batch: dict) -> None:
    test.assertEqual(set(batch) - {"session"}, {"version", "source", "id", "context", "events"})
    test.assertEqual((batch["version"], batch["source"]), (1, "host"))
    test.assertRegex(batch["id"], UUID4)
    if "session" in batch:
        test.assertRegex(batch["session"], UUID4)
    for key, value in batch["context"].items():
        test.assertIn(key, CONTEXT)
        test.assertTrue(fits(value, CONTEXT[key]), (key, value))
    test.assertTrue(1 <= len(batch["events"]) <= 50)
    for event in batch["events"]:
        test.assertEqual(set(event), {"name", "t", "p"})
        test.assertIn(event["name"], CONTRACT)
        test.assertIs(type(event["t"]), int)
        allowed = CONTRACT[event["name"]]
        for key, value in event["p"].items():
            test.assertIn(key, allowed, event)
            test.assertTrue(fits(value, allowed[key]), (event["name"], key, value))
    for event in batch["events"]:
        if event["name"] == "error":
            test.assertIn("code", event["p"])


_original_ensure_sender = telemetry._ensure_sender


class TelemetryCase(unittest.TestCase):
    """A fresh stream, home and transport for each test; no sender thread unless a test asks for one."""

    start_sender = False

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bighelp-telemetry-", dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / ".hermes"
        self.home.mkdir()
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("DO_NOT_TRACK", "BIGHELP_TELEMETRY", "LOOPDY_TELEMETRY"):
            os.environ.pop(name, None)
        self.stream = telemetry._new_stream()
        self.addCleanup(lambda: (setattr(self.stream, "stopped", True), self.stream.wake.set()))
        modules = patch.dict(sys.modules, {telemetry._SHARED_STREAM: self.stream})
        modules.start()
        self.addCleanup(modules.stop)
        if not self.start_sender:
            sender = patch.object(telemetry, "_ensure_sender", lambda stream: None)
            sender.start()
            self.addCleanup(sender.stop)
        self.sent: list[dict] = []
        self.answers: list = []
        telemetry.set_transport(self.transport)

    def transport(self, body, timeout):
        self.sent.append(json.loads(body))
        return self.answers.pop(0) if self.answers else (202, None)

    def events(self):
        return [event for batch in self.sent for event in batch["events"]]

    def names(self):
        return [event["name"] for event in self.events()]

    def queued(self):
        return [(event["name"], event["p"]) for event in self.stream.events]


class PrivacyTests(TelemetryCase):
    SECRETS = (
        "alice@example.com", "10.0.0.7", "/Users/alice/Documents/tax-return.pdf",
        "alice-laptop.local", "Quarterly layoff plan", "my-secret-profile", "sk-live-abc123",
        "ft:gpt-4o:acme-corp:alice::A1b2", "https://internal.example.com/api/v1",
        "chat-8f3a-private-session", "acme-private-llm", "C:\\Users\\alice\\notes.txt",
    )

    def feed_everything(self):
        text, path, email = self.SECRETS[4], self.SECRETS[2], self.SECRETS[0]
        # Emitters, called with private values where a string is allowed.
        telemetry.host_started()
        telemetry.turn_completed(duration_ms=1200, first_token_ms=300, tool_calls=2, outcome="ok",
                                 provider=self.SECRETS[10], model=self.SECRETS[7], session_source=email)
        telemetry.turn_completed(duration_ms=path, first_token_ms=text, tool_calls=email, outcome=text,
                                 provider="openrouter", model=self.SECRETS[7], session_source=self.SECRETS[9])
        telemetry.turn_completed(provider="anthropic", model="anthropic/claude-sonnet-4.6", session_source="telegram")
        telemetry.tool_completed(tool=path, duration_ms=4, outcome="ok")
        telemetry.tool_completed(tool=self.SECRETS[5], duration_ms=email, outcome=text)
        telemetry.tool_completed(tool={"path": path}, duration_ms=None, outcome="failed")
        telemetry.alert_queued(event_type=text, queue_ms=5)
        telemetry.alert_queued(event_type="session.completed", queue_ms=path)
        telemetry.alert_sent(event_type="wake", relay_ms=40, result=path, attempt=email)
        telemetry.alert_sent(event_type=email, relay_ms=40, result="accepted", attempt=1)
        telemetry.error(code=email, stage="native_route")
        telemetry.error(code=path, stage="notification_send")
        telemetry.error(code="native_service_unavailable", stage=text)
        # The same through Hermes' hooks, with realistic payloads.
        hooks = {}
        context = SimpleNamespace(register_hook=lambda name, callback: hooks.setdefault(name, []).append(callback))
        with patch("hermes_cli.plugins.VALID_HOOKS", {"pre_api_request", "on_stream_start", "pre_llm_call"}):
            telemetry.register(context)
        history = [{"role": "user", "content": text}, {"role": "assistant", "content": f"Sent to {email}"}]
        session = self.SECRETS[9]
        call = lambda hook, **payload: [callback(**payload) for callback in hooks[hook]]
        call("pre_llm_call", session_id=session, task_id=session, turn_id="turn-1", user_message=text,
             conversation_history=history, model=self.SECRETS[7], platform=email, sender_id=email)
        call("pre_api_request", session_id=session, provider=self.SECRETS[10], model=self.SECRETS[7],
             base_url=self.SECRETS[8], request_messages=history, user_message=text, api_mode="chat")
        call("on_stream_start", session_id=session, provider=self.SECRETS[10], model=self.SECRETS[7], surface=email)
        call("post_tool_call", session_id=session, tool_name="/Users/alice/bin/" + self.SECRETS[5],
             args={"path": path, "to": email}, result=json.dumps({"error": f"cannot read {path}", "ip": self.SECRETS[1]}),
             tool_call_id="call-a", duration_ms=9, status="error", error_message=f"{path} for {email}")
        call("post_tool_call", session_id=session, tool_name="terminal", args={"command": f"cat {path}"},
             result=text, tool_call_id="call-b", duration_ms=11)
        call("post_llm_call", session_id=session, user_message=text, assistant_response=f"{text} {email}",
             conversation_history=history, model=self.SECRETS[7], platform=email)
        call("on_session_end", session_id=session, completed=True, failed=False, interrupted=False,
             model=self.SECRETS[7], platform=email)
        # And a second turn that fails, with a provider Hermes has not told us about.
        call("pre_llm_call", session_id=session, user_message=text, model=self.SECRETS[7], platform="cli")
        call("on_session_end", session_id=session, completed=False, failed=True, interrupted=False, platform="cli",
             error=f"{path} {email}")

    def test_no_private_text_reaches_a_batch(self):
        self.feed_everything()
        self.assertGreaterEqual(len(self.stream.events), 12)
        self.assertGreater(telemetry.flush(), 0)
        wire = json.dumps(self.sent)
        private = list(self.SECRETS) + [str(self.home), tempfile.gettempdir(), "layoff", "tax-return", "Users"]
        for name in (getpass.getuser(), socket.gethostname().split(".")[0]):
            if len(name) >= 5:
                private.append(name)
        for secret in private:
            self.assertNotIn(secret, wire, secret)
        for batch in self.sent:
            assert_contract(self, batch)
        self.assertIn("turn.completed", self.names())
        # Names that are not built-in tools or known providers became fixed words.
        tools = [event["p"]["tool"] for event in self.events() if event["name"] == "tool.completed"]
        self.assertEqual(set(tools) - {"terminal"}, {"custom"})
        providers = {event["p"].get("provider") for event in self.events() if event["name"] == "turn.completed"}
        self.assertLessEqual(providers, {None, "custom", "openrouter", "anthropic"})
        models = {event["p"].get("model") for event in self.events() if event["name"] == "turn.completed"}
        self.assertLessEqual(models, {None, "custom", "anthropic/claude-sonnet-4.6"})

    def test_a_value_out_of_range_or_of_the_wrong_type_is_left_out(self):
        telemetry.turn_completed(duration_ms=-1, first_token_ms=True, tool_calls=2.5, outcome="ok")
        telemetry.turn_completed(duration_ms=86_400_001, first_token_ms=float("nan"), tool_calls=10**9)
        telemetry.alert_sent(event_type="wake", relay_ms=float("inf"), result="Not_A_Code!", attempt=-3)
        telemetry.tool_completed(tool="read_file", duration_ms="12", outcome="maybe")
        telemetry.error(code="UPPER", stage="native_route")
        telemetry.error(code="x" * 65, stage="native_route")
        telemetry.error(code="fine_code", stage="a_stage_nobody_has")
        self.assertEqual(self.queued(), [
            ("turn.completed", {"toolCalls": 2, "outcome": "ok", "sessionSource": "other"}),
            ("turn.completed", {"sessionSource": "other"}),
            ("alert.sent", {"eventType": "wake", "result": "other"}),
            ("tool.completed", {"tool": "read_file"}),
        ])

    def test_tools_are_named_only_when_they_are_built_in_or_bighelp(self):
        for name in ("terminal", "read_file", "web_search", "delegate_task", "browser_navigate", "bighelp_render_card",
                     "iphone_calendar", "bighelp_board"):
            self.assertEqual(telemetry.classify_tool(name), name)
        for name in ("mcp_github_create_issue", "mcp__slack__post"):
            self.assertEqual(telemetry.classify_tool(name), "mcp")
        for name in ("my_private_tool", "bighelp_not_ours", "READ_FILE", "", None, 7, "terminal ", "../etc/passwd"):
            self.assertEqual(telemetry.classify_tool(name), "custom")

    def test_every_tool_this_plugin_registers_is_known(self):
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        provided = re.findall(r"^  - ((?:bighelp|iphone)_[a-z_]+)$", text, re.M)
        self.assertGreater(len(provided), 30)
        self.assertEqual(sorted(set(provided) - telemetry._BIGHELP_TOOLS), [])
        self.assertEqual(sorted(telemetry._BIGHELP_TOOLS - set(provided)), [])
        for name in telemetry._HERMES_TOOLS | telemetry._BIGHELP_TOOLS:
            self.assertRegex(name, r"^[a-z][a-z0-9_]{0,63}$")

    def test_a_provider_is_named_only_when_it_is_public_and_a_model_only_from_a_public_catalog(self):
        route = telemetry.classify_route
        self.assertEqual(route("anthropic", "Anthropic/Claude-Sonnet-4.6"), ("anthropic", "anthropic/claude-sonnet-4.6"))
        self.assertEqual(route("openrouter", "openai/gpt-5:free"), ("openrouter", "openai/gpt-5:free"))
        self.assertEqual(route("openai-codex", "gpt-5-codex"), ("openai-codex", "gpt-5-codex"))
        # A public service whose model IDs can name a person: provider only.
        self.assertEqual(route("huggingface", "alice/my-model"), ("huggingface", "custom"))
        self.assertEqual(route("bedrock", "arn:aws:bedrock:us-east-1:123456789012:x"), ("bedrock", "custom"))
        # Private or self-hosted: nothing but the word.
        for provider in ("custom", "custom:my-server", "lmstudio", "ollama", "acme-private-llm", "azure-foundry", "http://10.0.0.7"):
            self.assertEqual(route(provider, "llama-3-private"), ("custom", "custom"))
        # A model that could name a fine-tune, a deployment or an account.
        for model in ("ft:gpt-4o:acme::abc", "a/b/c", "accounts/alice/models/x", "projects/p/models/m", "x" * 80,
                      "model with space", "model@host", None, 7):
            self.assertEqual(route("openai", model), ("openai", "custom"), model)
        self.assertEqual(route(None, "gpt-5"), (None, None))
        self.assertEqual(route("", "gpt-5"), (None, None))
        self.assertEqual(route(7, "gpt-5"), (None, None))

    def test_session_sources_map_to_the_fixed_names(self):
        for given, expected in (("bighelp", "bighelp"), ("BigHelp", "bighelp"), ("loopdy", "bighelp"), ("cli", "cli"),
                                ("tui", "cli"), ("api_server", "api"), ("cron", "cron"), ("telegram", "telegram"),
                                ("webhook", "webhook"), ("alice@example.com", "other"), ("", "other"), (None, "other"),
                                ("my-custom-platform", "other")):
            self.assertEqual(telemetry.classify_source(given), expected)

    def test_agents_are_counted_in_buckets(self):
        self.assertEqual([telemetry.agent_bucket(n) for n in (0, 1, 2, 5, 6, 20, 21, 400)],
                         ["1", "1", "2-5", "2-5", "6-20", "6-20", "21+", "21+"])
        for name in ("alice", "bob", "My Secret Profile", ".deleted", "x"):
            (self.home / "profiles" / name).mkdir(parents=True, exist_ok=True)
        (self.home / "profiles" / "a-file").write_text("not a profile")
        context = telemetry.build_context()
        self.assertEqual(context["agentCount"], "2-5")
        self.assertNotIn("alice", json.dumps(context))
        for key, value in context.items():
            self.assertTrue(fits(value, CONTEXT[key]), (key, value))
        self.assertEqual(set(context), {"pluginVersion", "hermesVersion", "osFamily", "python", "agentCount"} - (
            set() if "hermesVersion" in context else {"hermesVersion"}))

    def test_the_context_matches_this_plugin(self):
        from loopdy_plugin.version import PLUGIN_VERSION
        context = telemetry.build_context()
        self.assertEqual(context["pluginVersion"], PLUGIN_VERSION)
        self.assertEqual(context["python"], f"{sys.version_info.major}.{sys.version_info.minor}")
        self.assertIn(context["osFamily"], {"linux", "macos", "windows"})


class HookTests(TelemetryCase):
    def register(self, newer=("pre_api_request", "on_stream_start")):
        hooks = {}
        context = SimpleNamespace(register_hook=lambda name, callback: hooks.setdefault(name, []).append(callback))
        with patch("hermes_cli.plugins.VALID_HOOKS", set(newer) | {"pre_llm_call", "post_llm_call"}):
            telemetry.register(context)
        return hooks

    @staticmethod
    def call(hooks, name, **payload):
        for callback in hooks[name]:
            callback(**payload)

    def turn(self, hooks, *, outcome=None, tools=2, session="s-1"):
        self.call(hooks, "pre_llm_call", session_id=session, user_message="hi", model="anthropic/claude-sonnet-4.6",
                  platform="bighelp")
        if "pre_api_request" in hooks:
            self.call(hooks, "pre_api_request", session_id=session, provider="anthropic",
                      model="anthropic/claude-sonnet-4.6")
        if "on_stream_start" in hooks:
            time.sleep(0.002)
            self.call(hooks, "on_stream_start", session_id=session)
        for number in range(tools):
            self.call(hooks, "post_tool_call", session_id=session, tool_name="read_file", tool_call_id=f"c{number}",
                      duration_ms=7 + number, status="ok")
        time.sleep(0.002)
        if outcome is None:
            self.call(hooks, "post_llm_call", session_id=session, assistant_response="done")
        else:
            self.call(hooks, "on_session_end", session_id=session, **outcome)

    def test_a_turn_reports_its_timing_tools_route_and_source(self):
        hooks = self.register()
        self.assertEqual(set(hooks), {"pre_llm_call", "post_llm_call", "post_tool_call", "on_session_end",
                                      "pre_api_request", "on_stream_start"})
        self.turn(hooks)
        self.assertEqual([name for name, _ in self.queued()], ["tool.completed", "tool.completed", "turn.completed"])
        tool = self.queued()[0][1]
        self.assertEqual(tool, {"tool": "read_file", "durationMs": 7, "outcome": "ok"})
        turn = self.queued()[2][1]
        self.assertEqual({key: turn[key] for key in ("outcome", "toolCalls", "provider", "model", "sessionSource")},
                         {"outcome": "ok", "toolCalls": 2, "provider": "anthropic",
                          "model": "anthropic/claude-sonnet-4.6", "sessionSource": "bighelp"})
        self.assertGreaterEqual(turn["durationMs"], turn["firstTokenMs"])
        self.assertGreater(turn["firstTokenMs"], 0)

    def test_a_turn_ends_once_whichever_hook_arrives_first(self):
        hooks = self.register()
        self.turn(hooks)
        self.call(hooks, "on_session_end", session_id="s-1", completed=True, failed=False, interrupted=False)
        self.call(hooks, "post_llm_call", session_id="s-1")
        self.assertEqual(self.names_queued().count("turn.completed"), 1)

    def names_queued(self):
        return [name for name, _ in self.queued()]

    def test_failed_and_cancelled_turns(self):
        hooks = self.register()
        self.turn(hooks, outcome={"completed": False, "failed": True, "interrupted": False}, tools=0, session="a")
        self.turn(hooks, outcome={"completed": False, "failed": False, "interrupted": True}, tools=0, session="b")
        self.turn(hooks, outcome={"completed": True, "failed": False, "interrupted": False}, tools=0, session="c")
        self.assertEqual([props["outcome"] for name, props in self.queued() if name == "turn.completed"],
                         ["failed", "cancelled", "ok"])

    def test_an_exit_with_no_turn_or_no_outcome_reports_nothing(self):
        hooks = self.register()
        self.call(hooks, "on_session_end", session_id="nobody", reason="cli_exit")
        self.call(hooks, "pre_llm_call", session_id="s", platform="cli")
        self.call(hooks, "on_session_end", session_id="s", reason="cli_exit")
        self.assertEqual(self.queued(), [])

    def test_an_older_hermes_gives_fewer_fields_and_no_errors(self):
        hooks = self.register(newer=())
        self.assertEqual(set(hooks), {"pre_llm_call", "post_llm_call", "post_tool_call", "on_session_end"})
        self.turn(hooks, tools=1)
        turn = [props for name, props in self.queued() if name == "turn.completed"][0]
        self.assertEqual(set(turn), {"durationMs", "toolCalls", "outcome", "sessionSource"})
        # No tool status or duration either: older Hermes gives only the result text.
        self.call(hooks, "post_tool_call", tool_name="terminal", args={}, result=json.dumps({"error": "x"}))
        self.call(hooks, "post_tool_call", tool_name="terminal", args={}, result=json.dumps({"output": "x"}))
        self.call(hooks, "post_tool_call", tool_name="terminal", args={}, result="not json at all")
        self.assertEqual([props["outcome"] for name, props in self.queued()[-3:]], ["failed", "ok", "ok"])

    def test_the_same_tool_call_seen_by_two_copies_counts_once(self):
        hooks = self.register()
        again = self.register()
        for registered in (hooks, again):
            self.call(registered, "post_tool_call", session_id="s", tool_name="terminal", tool_call_id="same", duration_ms=3)
        self.assertEqual(self.names_queued(), ["tool.completed"])

    def test_a_broken_hook_call_never_raises(self):
        hooks = self.register()
        awful = object()
        for name, callbacks in hooks.items():
            for payload in ({}, {"session_id": awful, "tool_name": awful, "duration_ms": awful, "result": awful,
                                 "platform": awful, "model": awful, "provider": awful, "status": awful},
                            {"session_id": 7, "tool_call_id": 7, "completed": awful}):
                for callback in callbacks:
                    self.assertIsNone(callback(**payload))

    def test_a_turn_announced_again_keeps_its_clock_and_a_new_one_starts_afresh(self):
        hooks = self.register()
        self.call(hooks, "pre_llm_call", session_id="s", turn_id="t1", platform="cli")
        self.call(hooks, "post_tool_call", session_id="s", tool_name="terminal", tool_call_id="a", duration_ms=1)
        start = self.stream.turns["s"]["start"]
        self.call(hooks, "pre_llm_call", session_id="s", turn_id="t1", platform="cli")
        self.assertEqual((self.stream.turns["s"]["start"], self.stream.turns["s"]["tools"]), (start, 1))
        self.call(hooks, "pre_llm_call", session_id="s", turn_id="t2", platform="cli")
        self.assertEqual(self.stream.turns["s"]["tools"], 0)
        self.call(hooks, "pre_llm_call", session_id="s", platform="cli")
        self.assertEqual(self.stream.turns["s"]["turn"], "")

    def test_turn_timers_are_bounded(self):
        hooks = self.register()
        for number in range(telemetry._MAX_TURNS * 2):
            self.call(hooks, "pre_llm_call", session_id=f"s-{number}", platform="cli")
        self.assertLessEqual(len(self.stream.turns), telemetry._MAX_TURNS)


class SwitchTests(TelemetryCase):
    def emit(self):
        telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")

    def assert_off(self):
        self.assertFalse(telemetry.enabled())
        self.emit()
        self.assertEqual(self.queued(), [])
        self.assertEqual(telemetry.flush(), 0)
        self.assertEqual(self.sent, [])
        self.assertFalse(telemetry.status()["enabled"])

    def test_on_by_default(self):
        self.assertEqual(telemetry.status(), {"version": 1, "enabled": True, "appEnabled": True, "lockedOff": False})
        self.emit()
        self.assertEqual(len(self.queued()), 1)

    def test_do_not_track(self):
        with patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            self.assert_off()
            self.assertTrue(telemetry.status()["lockedOff"])
        with patch.dict(os.environ, {"DO_NOT_TRACK": "0"}):
            self.assertTrue(telemetry.enabled())

    def test_environment_variable_in_both_names(self):
        for name in ("BIGHELP_TELEMETRY", "LOOPDY_TELEMETRY"):
            for value in ("0", "false", "off", "no", "OFF", " False "):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    self.assert_off()
                    self.assertTrue(telemetry.status()["lockedOff"])
            for value in ("1", "true", "on", "yes", ""):
                with self.subTest(name=name, on=value), patch.dict(os.environ, {name: value}):
                    self.assertTrue(telemetry.enabled())
        # The bighelp name wins over the old one, as everywhere else.
        with patch.dict(os.environ, {"BIGHELP_TELEMETRY": "1", "LOOPDY_TELEMETRY": "0"}):
            self.assertTrue(telemetry.enabled())

    def test_config_file(self):
        config = self.home / "config.yaml"
        for text in ("bighelp:\n  telemetry: false\n", "bighelp:\n  telemetry: 'off'\n", "bighelp: {telemetry: no}\n",
                     "bighelp:\n  telemetry: 0\n"):
            with self.subTest(text=text):
                config.write_text(text, encoding="utf-8")
                os.utime(config, ns=(time.time_ns() + len(text), time.time_ns() + len(text)))
                self.stream.settings = None
                self.assert_off()
                self.assertTrue(telemetry.status()["lockedOff"])
        for text in ("bighelp:\n  telemetry: true\n", "other: 1\n", "bighelp: yes\n", ": : not yaml [", "bighelp:\n  other: false\n"):
            with self.subTest(text=text):
                config.write_text(text, encoding="utf-8")
                os.utime(config, ns=(time.time_ns() + 10 * len(text), time.time_ns() + 10 * len(text)))
                self.stream.settings = None
                self.assertTrue(telemetry.enabled())

    def test_the_config_of_a_profile_this_process_runs_as_counts_too(self):
        profile = self.home / "profiles" / "work"
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("bighelp:\n  telemetry: false\n", encoding="utf-8")
        with patch.dict(os.environ, {"HERMES_HOME": str(profile)}):
            self.stream.settings = None
            self.assert_off()

    def test_cli_flag(self):
        telemetry.set_cli_enabled(False)
        self.assert_off()
        self.assertFalse(telemetry.status()["lockedOff"])
        telemetry.set_cli_enabled(True)
        self.assertTrue(telemetry.enabled())

    def test_app_flag(self):
        self.assertEqual(telemetry.set_app_enabled(False), {"version": 1, "enabled": False, "appEnabled": False, "lockedOff": False})
        self.assert_off()
        self.assertEqual(telemetry.set_app_enabled(True), {"version": 1, "enabled": True, "appEnabled": True, "lockedOff": False})
        self.emit()
        self.assertEqual(len(self.queued()), 1)

    def test_the_app_cannot_override_a_forced_off(self):
        with patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            self.assertEqual(telemetry.set_app_enabled(True), {"version": 1, "enabled": False, "appEnabled": True, "lockedOff": True})
            self.assert_off()
        telemetry.set_cli_enabled(False)
        self.assertEqual(telemetry.set_app_enabled(True), {"version": 1, "enabled": False, "appEnabled": True, "lockedOff": False})

    def test_flags_are_kept_in_plugin_data_and_survive_a_restart(self):
        telemetry.set_app_enabled(False)
        path = self.home / "plugin-data" / "loopdy" / "telemetry-settings.json"
        self.assertEqual(json.loads(path.read_text()), {"cli": True, "app": False})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.stream.settings = None
        self.assertFalse(telemetry.enabled())
        path.write_text("{not json")
        self.stream.settings = None
        self.assertTrue(telemetry.enabled(), "a damaged file means on, not a crash")

    def test_another_process_turning_it_off_is_seen_and_drops_the_queue(self):
        self.emit()
        self.assertEqual(len(self.queued()), 1)
        path = self.home / "plugin-data" / "loopdy"
        path.mkdir(parents=True)
        (path / "telemetry-settings.json").write_text(json.dumps({"cli": False, "app": True}))
        self.stream.settings_at = -1000.0
        self.assertFalse(telemetry.enabled())
        self.assertEqual(self.queued(), [])

    def test_turning_it_off_drops_the_queue_and_the_id_at_once(self):
        self.emit()
        telemetry.telemetry_id()
        self.stream.turns["s"] = {}
        telemetry.set_app_enabled(False)
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.stream.turns, {})
        self.assertFalse((self.home / "plugin-data" / "loopdy" / "telemetry-id").exists())

    def test_a_switch_that_cannot_be_saved_still_holds_for_this_process(self):
        self.emit()
        with patch.object(telemetry, "_write_flags", side_effect=OSError):
            with self.assertRaises(OSError):
                telemetry.set_app_enabled(False)
        self.assertEqual(self.queued(), [])
        self.assertFalse(telemetry.enabled())

    def test_nothing_is_sent_after_the_switch_goes_off_between_queueing_and_sending(self):
        self.emit()
        with patch.dict(os.environ, {"BIGHELP_TELEMETRY": "off"}):
            self.assertEqual(telemetry.flush(), 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.queued(), [])

    def test_a_muted_process_sends_nothing(self):
        self.emit()
        telemetry.mute_process()
        self.emit()
        self.assertEqual(self.queued(), [])
        self.assertFalse(telemetry.enabled())
        telemetry.host_started()
        self.assertEqual(self.queued(), [])


class CliTests(TelemetryCase):
    def run_cli(self, state=None):
        args = SimpleNamespace(bighelp_action="telemetry", **({} if state is None else {"state": state}))
        with patch("builtins.print") as printed:
            registration.handle_cli(args, service=SimpleNamespace(), profile="default")
        return json.loads(printed.call_args.args[0])

    def test_status_names_each_source_and_the_field_list(self):
        shown = self.run_cli("status")
        self.assertTrue(shown["enabled"])
        self.assertEqual(set(shown["sources"]), {"DO_NOT_TRACK", "BIGHELP_TELEMETRY", "config bighelp.telemetry",
                                                 "hermes bighelp telemetry", "bighelp app"})
        self.assertEqual(shown["fields"], "docs/HOST_TELEMETRY.md")
        self.assertTrue(shown["fieldsUrl"].endswith("/docs/HOST_TELEMETRY.md"))
        with patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            shown = self.run_cli()
        self.assertFalse(shown["enabled"])
        self.assertEqual(shown["sources"]["DO_NOT_TRACK"], "off (=1)")

    def test_off_sets_the_cli_flag_and_on_clears_both(self):
        telemetry.set_app_enabled(False)
        self.stream.muted = False
        shown = self.run_cli("off")
        self.assertEqual(shown["sources"]["hermes bighelp telemetry"], "off")
        self.assertFalse(shown["enabled"])
        self.stream.muted = False
        self.assertEqual(json.loads((self.home / "plugin-data/loopdy/telemetry-settings.json").read_text()),
                         {"cli": False, "app": False})
        shown = self.run_cli("on")
        self.assertTrue(shown["enabled"])
        self.assertEqual(json.loads((self.home / "plugin-data/loopdy/telemetry-settings.json").read_text()),
                         {"cli": True, "app": True})

    def test_on_does_not_beat_a_setting_the_command_cannot_change(self):
        with patch.dict(os.environ, {"BIGHELP_TELEMETRY": "0"}):
            shown = self.run_cli("on")
        self.assertFalse(shown["enabled"])
        self.assertEqual(shown["sources"]["BIGHELP_TELEMETRY"], "off")

    def test_the_command_sends_nothing_itself(self):
        telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
        self.run_cli("status")
        self.assertEqual(self.queued(), [])
        telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
        self.assertEqual(self.queued(), [])

    def test_the_parser_takes_off_on_and_status(self):
        import argparse
        parser = argparse.ArgumentParser()
        registration.setup_cli(parser)
        self.assertEqual(parser.parse_args(["telemetry", "off"]).state, "off")
        self.assertEqual(parser.parse_args(["telemetry"]).state, "status")
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(["telemetry", "maybe"])



class RegistrationTests(TelemetryCase):
    def register(self):
        from test_registration import _Context, _Service
        context = _Context()
        registration.register(context, service=_Service())
        return context

    def test_the_plugin_observes_turns_and_tools_with_its_other_hooks(self):
        context = self.register()
        context.hooks["post_tool_call"](tool_name="terminal", tool_call_id="c1", session_id="s", duration_ms=4, status="ok")
        context.hooks["pre_llm_call"](session_id="s", platform="bighelp", model="gpt-5")
        context.hooks["post_llm_call"](session_id="s", assistant_response="Done.")
        self.assertEqual([name for name, _ in self.queued()], ["tool.completed", "turn.completed"])

    def test_both_command_names_take_telemetry(self):
        import argparse
        context = self.register()
        self.assertEqual(set(context.clis), {"bighelp", "loopdy"})
        for name, command in context.clis.items():
            parser = argparse.ArgumentParser()
            command["setup_fn"](parser)
            args = parser.parse_args(["telemetry", "off"])
            with patch("builtins.print"):
                command["handler_fn"](args)
            self.assertFalse(telemetry.enabled(), name)
            self.stream.muted = False
            telemetry.set_cli_enabled(True)

    def test_plugin_yaml_asks_for_no_new_capabilities_and_only_baseline_hooks(self):
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertNotIn("capabilities:", text)
        self.assertNotIn("pre_api_request", text)
        self.assertNotIn("on_stream_start", text)


class IdentityTests(TelemetryCase):
    def test_a_random_uuid_v4_kept_in_plugin_data(self):
        first = telemetry.telemetry_id()
        self.assertRegex(first, UUID4)
        path = self.home / "plugin-data" / "loopdy" / "telemetry-id"
        self.assertEqual(path.read_text().strip(), first)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(telemetry.telemetry_id(), first)

    def test_a_bad_file_is_replaced(self):
        path = self.home / "plugin-data" / "loopdy" / "telemetry-id"
        path.parent.mkdir(parents=True)
        path.write_text("alice@example.com")
        value = telemetry.telemetry_id()
        self.assertRegex(value, UUID4)
        self.assertEqual(path.read_text().strip(), value)

    def test_it_is_not_derived_from_the_notification_key_or_anything_else(self):
        ids = set()
        for _ in range(5):
            (self.home / "plugin-data" / "loopdy").mkdir(parents=True, exist_ok=True)
            telemetry._forget_identity()
            ids.add(telemetry.telemetry_id())
        self.assertEqual(len(ids), 5)
        source = (ROOT / "loopdy_plugin" / "host_telemetry.py").read_text(encoding="utf-8")
        for word in ("managed_notifications", "key_id", "public_key", "hashlib", "sha256", "gethostname", "getuser", "node()"):
            self.assertNotIn(word, source)

    def test_it_lives_at_the_hermes_root_not_in_a_profile_home(self):
        profile = self.home / "profiles" / "work"
        profile.mkdir(parents=True)
        with patch.dict(os.environ, {"HERMES_HOME": str(profile)}):
            self.stream.settings = None
            value = telemetry.telemetry_id()
        self.assertEqual((self.home / "plugin-data" / "loopdy" / "telemetry-id").read_text().strip(), value)
        self.assertFalse((profile / "plugin-data").exists())

    def test_it_is_made_only_when_something_is_sent(self):
        telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
        self.assertFalse((self.home / "plugin-data").exists())
        telemetry.flush()
        self.assertTrue((self.home / "plugin-data" / "loopdy" / "telemetry-id").exists())
        self.assertEqual(self.sent[0]["id"], telemetry.telemetry_id())
        self.assertRegex(self.sent[0]["session"], UUID4)
        self.assertNotEqual(self.sent[0]["session"], self.sent[0]["id"])


class QueueTests(TelemetryCase):
    def test_the_queue_is_bounded_and_drops_the_oldest(self):
        for number in range(telemetry.MAX_QUEUE + 25):
            telemetry.tool_completed(tool="terminal", duration_ms=number, outcome="ok")
        durations = [event["p"]["durationMs"] for event in self.stream.events]
        self.assertEqual(len(durations), telemetry.MAX_QUEUE)
        self.assertEqual(durations[0], 25)
        self.assertEqual(durations[-1], telemetry.MAX_QUEUE + 24)

    def test_a_flush_sends_batches_of_fifty(self):
        for number in range(120):
            telemetry.tool_completed(tool="terminal", duration_ms=number, outcome="ok")
        self.assertEqual(telemetry.flush(), 120)
        self.assertEqual([len(batch["events"]) for batch in self.sent], [50, 50, 20])
        for batch in self.sent:
            assert_contract(self, batch)
        self.assertEqual([e["p"]["durationMs"] for e in self.events()], list(range(120)))

    def test_a_flush_stops_after_three_batches(self):
        for number in range(200):
            telemetry.tool_completed(tool="terminal", duration_ms=number, outcome="ok")
        self.assertEqual(telemetry.flush(), 150)
        self.assertEqual(len(self.stream.events), 50)

    def test_a_failure_is_dropped_without_a_word(self):
        for status in (0, 400, 413, 500, 503):
            self.sent.clear()
            telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
            self.answers = [(status, None)]
            with self.assertNoLogs(level="WARNING"):
                self.assertEqual(telemetry.flush(), 0)
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(self.queued(), [], status)
            self.assertEqual(self.stream.retry_at, 0.0)

    def test_a_transport_that_raises_is_dropped_without_a_word(self):
        def explode(body, timeout):
            raise RuntimeError("alice@example.com /Users/alice/secret")
        telemetry.set_transport(explode)
        telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
        with self.assertNoLogs(level="WARNING"):
            self.assertEqual(telemetry.flush(), 0)
        self.assertEqual(self.queued(), [])

    def test_429_backs_off_and_keeps_the_events(self):
        for number in range(3):
            telemetry.tool_completed(tool="terminal", duration_ms=number, outcome="ok")
        self.answers = [(429, None)]
        self.assertEqual(telemetry.flush(), 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.stream.events), 3, "kept for later")
        wait = self.stream.retry_at - time.monotonic()
        self.assertTrue(telemetry._DEFAULT_BACKOFF - 5 < wait <= telemetry._DEFAULT_BACKOFF)
        # Inside the backoff nothing is posted.
        self.assertEqual(telemetry.flush(), 0)
        self.assertEqual(len(self.sent), 1)
        # After it, the same events go out, in order.
        self.stream.retry_at = 0.0
        self.assertEqual(telemetry.flush(), 3)
        self.assertEqual([e["p"]["durationMs"] for e in self.sent[-1]["events"]], [0, 1, 2])
        self.assertEqual(self.stream.backoff, 0)

    def test_429_keeps_to_retry_after_and_doubles_when_it_repeats(self):
        telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
        self.answers = [(429, 30)]
        telemetry.flush()
        self.assertTrue(25 < self.stream.retry_at - time.monotonic() <= 30)
        for expected in (240, 480, 960, 1920, 3600, 3600):
            self.stream.retry_at = 0.0
            self.answers = [(429, None)]
            telemetry.flush()
            self.assertEqual(self.stream.backoff, expected if expected != 240 else 240)
        self.answers = [(429, 10**9)]
        self.stream.retry_at = 0.0
        telemetry.flush()
        self.assertLessEqual(self.stream.retry_at - time.monotonic(), 3600)

    def test_the_queue_stays_bounded_while_backing_off(self):
        for number in range(telemetry.MAX_QUEUE):
            telemetry.tool_completed(tool="terminal", duration_ms=number, outcome="ok")
        self.answers = [(429, None)]
        telemetry.flush()
        for number in range(30):
            telemetry.tool_completed(tool="terminal", duration_ms=1000 + number, outcome="ok")
        self.assertEqual(len(self.stream.events), telemetry.MAX_QUEUE)
        self.assertEqual(self.stream.events[-1]["p"]["durationMs"], 1029)

    def test_emitters_never_raise(self):
        class Awful:
            def __str__(self):
                raise RuntimeError("no")
            __repr__ = __str__
            def __eq__(self, other):
                raise RuntimeError("no")
            __hash__ = None
        awful = Awful()
        with patch.object(telemetry, "_enqueue", side_effect=RuntimeError("alice@example.com")):
            with self.assertNoLogs(level="WARNING"):
                telemetry.host_started()
                telemetry.turn_completed(duration_ms=1)
                telemetry.tool_completed(tool="terminal")
                telemetry.alert_queued(event_type="wake")
                telemetry.alert_sent(event_type="wake")
                telemetry.error(code="a_code", stage="native_route")
        telemetry.turn_completed(duration_ms=awful, first_token_ms=awful, tool_calls=awful, outcome=awful,
                                 provider=awful, model=awful, session_source=awful)
        telemetry.tool_completed(tool=awful, duration_ms=awful, outcome=awful)
        telemetry.alert_queued(event_type=awful, queue_ms=awful)
        telemetry.alert_sent(event_type=awful, relay_ms=awful, result=awful, attempt=awful)
        telemetry.error(code=awful, stage=awful)
        with patch.object(telemetry, "enabled", side_effect=RuntimeError):
            telemetry.tool_completed(tool="terminal")
        with patch.object(telemetry, "telemetry_id", side_effect=OSError):
            telemetry.tool_completed(tool="terminal")
            self.assertEqual(telemetry.flush(), 0)

    def test_the_same_error_is_reported_once_a_minute(self):
        for _ in range(5):
            telemetry.error(code="native_service_unavailable", stage="native_route")
        telemetry.error(code="notification_remote_rejected", stage="notification_send")
        self.assertEqual(self.queued(), [("error", {"code": "native_service_unavailable", "stage": "native_route"}),
                                         ("error", {"code": "notification_remote_rejected", "stage": "notification_send"})])

    def test_the_real_transport_never_reaches_the_network_in_a_test_run(self):
        telemetry.set_transport(None)
        with patch.object(telemetry, "build_opener", side_effect=AssertionError("network")):
            telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
            self.assertEqual(telemetry.flush(), 0)
        self.assertEqual(telemetry._post(b"{}", 1), (0, None))

    def test_the_real_transport_posts_json_to_the_fixed_endpoint_and_follows_no_redirect(self):
        seen = {}

        class Opener:
            def open(self, request, timeout):
                seen.update(url=request.full_url, method=request.get_method(), timeout=timeout, body=request.data,
                            type=request.get_header("Content-type"))
                return SimpleNamespace(status=202, read=lambda n: b"{}", __enter__=None)

        class Response:
            status = 202
            def read(self, n): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class Opener2(Opener):
            def open(self, request, timeout):
                super().open(request, timeout)
                return Response()

        with patch.object(telemetry, "_under_test_runner", return_value=False), \
                patch.object(telemetry, "build_opener", return_value=Opener2()) as build:
            self.assertEqual(telemetry._post(b'{"a":1}', 5), (202, None))
        self.assertEqual(seen, {"url": "https://telemetry.bighelp.app/v1/batch", "method": "POST", "timeout": 5,
                                "body": b'{"a":1}', "type": "application/json"})
        self.assertIsInstance(build.call_args.args[0], telemetry._NoRedirect)
        self.assertEqual(telemetry.TIMEOUT_SECONDS, 5)

    def test_the_real_transport_reads_retry_after_and_swallows_errors(self):
        from urllib.error import HTTPError

        class Failing:
            def __init__(self, error): self.error = error
            def open(self, request, timeout): raise self.error

        limited = HTTPError(telemetry.ENDPOINT, 429, "slow", {"Retry-After": "45"}, None)
        with patch.object(telemetry, "_under_test_runner", return_value=False):
            with patch.object(telemetry, "build_opener", return_value=Failing(limited)):
                self.assertEqual(telemetry._post(b"{}", 5), (429, 45))
            with patch.object(telemetry, "build_opener", return_value=Failing(HTTPError(telemetry.ENDPOINT, 503, "x", {}, None))):
                self.assertEqual(telemetry._post(b"{}", 5), (503, None))
            with patch.object(telemetry, "build_opener", return_value=Failing(OSError("alice@example.com"))):
                self.assertEqual(telemetry._post(b"{}", 5), (0, None))


class SenderTests(TelemetryCase):
    start_sender = True

    def tearDown(self):
        self.stream.thread = None

    def wait_for(self, condition, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.01)
        return False

    def test_fifty_events_are_sent_at_once_by_a_daemon_thread(self):
        arrived = threading.Event()
        telemetry.set_transport(lambda body, timeout: (self.sent.append(json.loads(body)), arrived.set(), (202, None))[2])
        for number in range(telemetry.BATCH_EVENTS):
            telemetry.tool_completed(tool="terminal", duration_ms=number, outcome="ok")
        self.assertTrue(arrived.wait(5))
        self.assertEqual(len(self.sent[0]["events"]), 50)
        self.assertTrue(self.stream.thread.daemon)
        self.assertEqual(self.stream.thread.name, "bighelp-host-telemetry")

    def test_fewer_events_wait_for_the_interval(self):
        with patch.object(telemetry, "FLUSH_SECONDS", 0.3):
            telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
            self.assertEqual(self.sent, [])
            self.assertTrue(self.wait_for(lambda: self.sent))
        self.assertEqual(len(self.events()), 1)

    def test_a_turn_is_never_blocked_by_a_slow_network(self):
        release = threading.Event()
        telemetry.set_transport(lambda body, timeout: (release.wait(10), (202, None))[1])
        started = time.monotonic()
        for number in range(telemetry.BATCH_EVENTS * 2):
            telemetry.tool_completed(tool="terminal", duration_ms=number, outcome="ok")
        self.assertLess(time.monotonic() - started, 1.0)
        release.set()

    def test_one_thread_for_the_process(self):
        telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
        first = self.stream.thread
        for _ in range(10):
            telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
        self.assertIs(self.stream.thread, first)
        self.assertEqual([t.name for t in threading.enumerate()].count("bighelp-host-telemetry") >= 1, True)

    def test_off_at_runtime_is_seen_by_the_thread(self):
        with patch.object(telemetry, "FLUSH_SECONDS", 0.2):
            telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
            telemetry.set_app_enabled(False)
            time.sleep(0.6)
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.stream.events), 0)

    def test_the_rest_is_flushed_at_exit_with_a_short_timeout(self):
        timeouts = []
        telemetry.set_transport(lambda body, timeout: (timeouts.append(timeout), (202, None))[1])
        with patch.object(telemetry, "FLUSH_SECONDS", 3600):
            telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
            telemetry._flush_at_exit()
        self.assertEqual(timeouts, [telemetry.EXIT_TIMEOUT_SECONDS])
        self.stream.muted = True
        telemetry._flush_at_exit()
        self.assertEqual(len(timeouts), 1)

    def test_an_exit_with_telemetry_off_sends_nothing(self):
        telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
        with patch.dict(os.environ, {"DO_NOT_TRACK": "1"}):
            telemetry._flush_at_exit()
        self.assertEqual(self.sent, [])

    def test_a_start_is_counted_after_the_process_has_been_up_a_while(self):
        with patch.object(telemetry, "START_DELAY_SECONDS", 0.05):
            telemetry.announce_host_started()
            telemetry.announce_host_started()
            self.assertTrue(self.wait_for(lambda: self.names_queued_or_sent()))
        time.sleep(0.1)
        self.assertEqual([name for name, _ in self.queued()].count("host.started"), 1)

    def names_queued_or_sent(self):
        return any(name == "host.started" for name, _ in self.queued()) or "host.started" in self.names()

    def test_a_start_is_counted_once_per_process(self):
        telemetry.host_started()
        telemetry.host_started()
        self.assertEqual([name for name, _ in self.queued()], ["host.started"])
        self.assertEqual(self.queued()[0][1], {})


class SharedStreamTests(TelemetryCase):
    """The dashboard imports this package twice, and Hermes loads it again for each profile."""

    def setUp(self):
        super().setUp()
        package = types.ModuleType("loopdy_copy_for_test")
        package.__path__ = [str(ROOT / "loopdy_plugin")]
        modules = patch.dict(sys.modules, {"loopdy_copy_for_test": package})
        modules.start()
        self.addCleanup(modules.stop)
        self.addCleanup(lambda: [sys.modules.pop(name, None) for name in list(sys.modules)
                                 if name.startswith("loopdy_copy_for_test.")])
        self.copy = importlib.import_module("loopdy_copy_for_test.host_telemetry")
        self.copy_real_ensure_sender = self.copy._ensure_sender
        self.assertIsNot(self.copy, telemetry)
        self.assertIsNot(self.copy.classify_tool, telemetry.classify_tool)
        sender = patch.object(self.copy, "_ensure_sender", lambda stream: None)
        sender.start()
        self.addCleanup(sender.stop)

    def test_both_copies_use_the_one_stream(self):
        self.assertIs(self.copy._stream(), telemetry._stream())
        self.assertIs(telemetry._stream(), self.stream)
        self.assertEqual(self.copy._SHARED_STREAM, telemetry._SHARED_STREAM)

    def test_the_stream_is_made_once_even_if_the_two_copies_race(self):
        del sys.modules[telemetry._SHARED_STREAM]
        found = []
        barrier = threading.Barrier(2)

        def ask(module):
            barrier.wait()
            found.append(module._stream())
        threads = [threading.Thread(target=ask, args=(module,)) for module in (telemetry, self.copy)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertIs(found[0], found[1])
        self.assertIs(sys.modules[telemetry._SHARED_STREAM], found[0])

    def test_events_from_either_copy_go_in_one_queue_and_one_batch(self):
        telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
        self.copy.tool_completed(tool="read_file", duration_ms=2, outcome="ok")
        self.copy.set_transport(self.transport)
        self.assertEqual(telemetry.flush(), 2)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual([event["p"]["tool"] for event in self.sent[0]["events"]], ["terminal", "read_file"])

    def test_one_sender_thread_serves_both_copies(self):
        def senders():
            return [thread for thread in threading.enumerate() if thread.name == "bighelp-host-telemetry"]
        before = len(senders())
        _original_ensure_sender(self.stream)
        self.copy_real_ensure_sender(self.stream)
        _original_ensure_sender(self.stream)
        self.copy_real_ensure_sender(self.stream)
        self.assertEqual(len(senders()) - before, 1)
        self.assertTrue(self.stream.thread.is_alive())
        self.assertTrue(self.stream.atexit_registered)

    def test_a_turn_started_in_one_copy_ends_in_the_other(self):
        self.copy.on_pre_llm_call(session_id="s", platform="bighelp", model="gpt-5")
        self.copy.on_pre_api_request(session_id="s", provider="openai", model="gpt-5")
        telemetry.on_post_tool_call(session_id="s", tool_name="terminal", tool_call_id="x", duration_ms=1)
        telemetry.on_post_llm_call(session_id="s")
        self.copy.on_session_end(session_id="s", completed=True)
        turns = [props for name, props in self.queued() if name == "turn.completed"]
        self.assertEqual(len(turns), 1)
        self.assertEqual((turns[0]["toolCalls"], turns[0]["provider"], turns[0]["sessionSource"]), (1, "openai", "bighelp"))

    def test_a_tool_call_both_copies_see_counts_once(self):
        for module in (telemetry, self.copy):
            module.on_post_tool_call(session_id="s", tool_name="terminal", tool_call_id="same", duration_ms=1)
        self.assertEqual([name for name, _ in self.queued()], ["tool.completed"])

    def test_a_switch_set_through_one_copy_holds_for_the_other(self):
        self.copy.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
        self.copy.set_app_enabled(False)
        self.assertFalse(telemetry.enabled())
        self.assertEqual(self.queued(), [])
        telemetry.tool_completed(tool="terminal", duration_ms=1, outcome="ok")
        self.assertEqual(self.queued(), [])
        self.assertEqual(telemetry.status()["appEnabled"], False)

    def test_the_stream_holds_only_plain_containers(self):
        for name, value in vars(self.stream).items():
            if name.startswith("__"):
                continue
            module = type(value).__module__
            self.assertNotIn("loopdy", module, name)
            self.assertNotIn("hermes_plugins", module, name)


if __name__ == "__main__":
    unittest.main()
