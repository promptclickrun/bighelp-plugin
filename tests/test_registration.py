from __future__ import annotations
import telemetry_off  # noqa: F401  (no test run posts host telemetry)

import importlib.util
import argparse
import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


PLUGIN_ROOT = Path(__file__).resolve().parents[1]


class _Context:
    profile_name = "personal"

    def __init__(self):
        self.platform = None
        self.approval = None
        self.hooks = {}
        self.cli = None
        self.clis = {}
        self.unload = None
        self.tools = {}
        self.skills = {}
        self.state = type(
            "State",
            (),
            {
                "values": {},
                "get": lambda self, key, default=None: self.values.get(key, default),
                "set": lambda self, key, value: self.values.__setitem__(key, value),
            },
        )()

    def register_tool(self, **kwargs):
        self.tools[kwargs["name"]] = kwargs

    def register_platform(self, **kwargs):
        self.platform = kwargs

    def register_skill(self, name, path, **kwargs):
        self.skills[name] = {"path": Path(path), **kwargs}

    def register_approval_transport(self, name, present_fn):
        self.approval = (name, present_fn)

    def register_hook(self, name, callback):
        previous = self.hooks.get(name)
        if previous is None:
            self.hooks[name] = callback
            return
        def dispatch(*args, **kwargs):
            first = previous(*args, **kwargs)
            second = callback(*args, **kwargs)
            return second if second is not None else first
        self.hooks[name] = dispatch

    def register_cli_command(self, **kwargs):
        self.cli = self.cli or kwargs
        self.clis[kwargs["name"]] = kwargs

    def on_unload(self, callback):
        previous = self.unload
        if previous is None:
            self.unload = callback
        else:
            def unload():
                try:
                    callback()
                finally:
                    previous()
            self.unload = unload


class _Service:
    def __init__(self):
        self.events = []
        self.dismissed_requests = []
        self.dismissed_sessions = []
        service = self

        class Store:
            def __init__(self):
                self.records = {}

            def record_event(self, event, *, target="all"):
                if event.event_id in self.records:
                    return False
                self.records[event.event_id] = {
                    "event_id": event.event_id, "type": event.type,
                    "profile": event.profile, "session_id": event.session_id,
                    "detail": dict(event.detail),
                }
                return True

            def get_event(self, event_id):
                return self.records.get(event_id)

            def dismiss_attention_request(
                self, request_id, *, session_id="", dismissed_at=None
            ):
                service.dismissed_requests.append((request_id, session_id))
                return 1

            def dismiss_attention_for_session(self, session_id, *, dismissed_at=None):
                service.dismissed_sessions.append(session_id)
                return 1

        self.store = Store()

    def enqueue(self, event, *, target):
        self.store.record_event(event, target=target)
        self.events.append((event, target))
        return True

    def close(self):
        pass

    def enqueue_live_activity_update(self, **update):
        self.events.append(("live-activity", update))
        return True


class RegistrationTests(unittest.TestCase):
    def test_clarify_tool_cleanup_does_not_allow_session_end_to_clear_newer_attention(self) -> None:
        from loopdy_plugin.registration import register

        service = _Service()
        context = _Context()
        register(context, service=service)

        context.hooks["post_tool_call"](
            tool_name="clarify",
            tool_call_id="request-1",
            session_id="session-1",
        )
        context.hooks["on_session_end"](
            session_id="session-2",
            completed=True,
            failed=False,
            interrupted=False,
        )

        self.assertEqual(service.dismissed_requests, [])
        self.assertEqual(service.dismissed_sessions, ["session-1"])

    def test_post_llm_preserves_pending_attention_for_a_newer_turn(self) -> None:
        from loopdy_plugin.registration import register

        service = _Service()
        context = _Context()
        register(context, service=service)

        context.hooks["post_llm_call"](session_id="session-1")

        self.assertEqual(service.dismissed_sessions, [])

    def test_interrupted_session_ends_live_activity_as_failed(self) -> None:
        from loopdy_plugin.registration import _live_activity_from_hook

        service = _Service()
        _live_activity_from_hook(
            "on_session_end",
            service=service,
            profile="default",
            payload={"session_id": "session-1", "interrupted": True},
        )
        _, update = service.events[0]
        self.assertEqual(update["phase"], "failed")
        self.assertEqual(update["active_session_count"], 0)
        self.assertNotIn("detail", update)

    def test_successful_turn_completes_live_activity_from_post_llm_only(self) -> None:
        from loopdy_plugin.registration import register

        service = _Service()
        context = _Context()
        register(context, service=service)

        context.hooks["post_llm_call"](
            profile_name="personal",
            session_id="session-1",
            turn_id="turn-1",
            assistant_response="Finished",
        )
        context.hooks["on_session_end"](
            profile_name="personal",
            session_id="session-1",
            turn_id="turn-1",
            completed=True,
            failed=False,
            interrupted=False,
        )

        live_updates = [value for kind, value in service.events if kind == "live-activity"]
        self.assertEqual(len(live_updates), 1)
        self.assertEqual(live_updates[0]["phase"], "completed")
        self.assertEqual(live_updates[0]["active_session_count"], 0)

    def test_provider_and_apns_commands_store_only_host_configuration(self) -> None:
        from loopdy_plugin.registration import handle_cli, setup_cli

        class Store:
            def __init__(self):
                self.mode = "managed"
                self.config = None

            def provider_mode(self):
                return self.mode

            def set_provider_mode(self, mode):
                self.mode = mode

            def save_apns_config(self, config):
                self.config = dict(config)

            def load_apns_config(self):
                return self.config

            def clear_apns_config(self):
                self.config = None

            def list_devices(self):
                return [
                    {
                        "device_id": "phone-1",
                        "endpoint_id": "ExponentPushToken[fixture-phone-1]",
                        "provider": "managed",
                        "token_environment": "production",
                        "label": "iPhone",
                        "groups": [],
                        "preferences": {},
                        "revoked": False,
                    }
                ]

        class Service:
            store = Store()

            def health(self):
                return {"mode": self.store.mode, "ready": True, "configured": True}

            def set_provider_mode(self, mode):
                self.store.set_provider_mode(mode)
                return self.health()

        service = Service()
        parser = argparse.ArgumentParser()
        setup_cli(parser)
        with tempfile.TemporaryDirectory() as temporary:
            key_path = Path(temporary) / "AuthKey_FIXTURE01.p8"
            key_path.write_bytes(
                ec.generate_private_key(ec.SECP256R1()).private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            args = parser.parse_args(
                [
                    "configure-apns",
                    "--team-id",
                    "TEAMFIX001",
                    "--key-id",
                    "KEYFIX0001",
                    "--topic",
                    "app.loopdy.personal",
                    "--environment",
                    "production",
                    "--key-path",
                    str(key_path),
                ]
            )
            with contextlib.redirect_stdout(io.StringIO()):
                handle_cli(args, service=service, profile="default")

            self.assertEqual(
                set(service.store.config),
                {"team_id", "key_id", "topic", "environment", "key_path"},
            )
            self.assertEqual(service.store.config["key_path"], str(key_path.resolve()))
            self.assertEqual(service.store.mode, "direct")

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                handle_cli(
                    parser.parse_args(["status"]),
                    service=service,
                    profile="default",
                )
            status = json.loads(output.getvalue())
            self.assertEqual(status["provider"], "direct")
            self.assertEqual(status["apns"]["key_file"], "AuthKey_FIXTURE01.p8")
            self.assertNotIn(str(key_path), output.getvalue())
            self.assertNotIn("PRIVATE KEY", output.getvalue())
            self.assertNotIn("ExponentPushToken", output.getvalue())
            self.assertEqual(status["devices"][0]["token_fingerprint"], "4242918ebfa3")

            with contextlib.redirect_stdout(io.StringIO()):
                handle_cli(
                    parser.parse_args(["provider", "managed"]),
                    service=service,
                    profile="default",
                )
            self.assertEqual(service.store.mode, "managed")

            with contextlib.redirect_stdout(io.StringIO()):
                handle_cli(
                    parser.parse_args(["remove-apns", "--yes"]),
                    service=service,
                    profile="default",
                )
            self.assertIsNone(service.store.config)

    def test_status_does_not_ask_for_buzzkit_setup_when_the_app_set_up_notifications(self) -> None:
        # The legacy push store's "managed" provider is never built any more; the app's
        # notifications run through managed_notifications.py instead.
        from loopdy_plugin.registration import handle_cli, setup_cli
        from loopdy_plugin.service import BighelpService
        from loopdy_plugin.store import BighelpStore

        parser = argparse.ArgumentParser()
        setup_cli(parser)
        with tempfile.TemporaryDirectory() as directory:
            service = BighelpService(BighelpStore(Path(directory) / "loopdy.sqlite3"))
            self.addCleanup(service.close)
            for grants in (2, None):
                managed = SimpleNamespace(active_grant_count=lambda grants=grants: grants)
                if grants is None:
                    managed = SimpleNamespace()
                output = io.StringIO()
                with (
                    patch("loopdy_plugin.managed_notifications.get_managed_notifications", return_value=managed),
                    contextlib.redirect_stdout(output),
                ):
                    handle_cli(parser.parse_args(["status"]), service=service, profile="default")
                status = json.loads(output.getvalue())
                self.assertNotIn("Complete BuzzKit", output.getvalue())
                self.assertEqual(status["provider"], "managed")
                self.assertFalse(status["provider_health"]["configured"])
                if grants is None:
                    self.assertEqual(status["notifications"], {"available": False})
                else:
                    self.assertEqual(status["notifications"], {"available": True, "active_grants": 2})

    def test_a_producer_that_cannot_start_says_why_without_a_path(self) -> None:
        from loopdy_plugin.managed_notifications import ManagedNotificationError
        from loopdy_plugin.registration import register

        for failure, expected in (
            (ManagedNotificationError("notification_private_storage_required", 503),
             "notification_private_storage_required"),
            (OSError("/home/alice/.hermes/plugin-data/loopdy"), "OSError"),
        ):
            with self.subTest(expected=expected):
                with (
                    patch("loopdy_plugin.managed_notifications.get_managed_notifications", side_effect=failure),
                    self.assertLogs("hermes.plugins.bighelp", level="WARNING") as logs,
                ):
                    register(_Context(), service=_Service())
                text = "\n".join(logs.output)
                self.assertIn("Managed notification producer unavailable: " + expected, text)
                self.assertNotIn("alice", text)

    def test_retired_marketplace_tool_and_skill_are_not_registered(self) -> None:
        # The bighelp Marketplace is gone: its service has no draft routes and the app has no Marketplace,
        # so an agent that called the tool always failed.
        import yaml
        from loopdy_plugin.registration import register

        context = _Context()
        register(context, service=_Service())

        self.assertNotIn("bighelp_marketplace_prepare_upload", context.tools)
        self.assertNotIn("bighelp-marketplace-publish", context.skills)
        manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("bighelp_marketplace_prepare_upload", manifest["provides_tools"])
        self.assertFalse((PLUGIN_ROOT / "skills" / "bighelp-marketplace-publish").exists())

    def test_native_surfaces_and_nonblocking_hooks_are_registered(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "loopdy_entry_for_test",
            PLUGIN_ROOT / "__init__.py",
            submodule_search_locations=[str(PLUGIN_ROOT)],
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        import sys

        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        service = _Service()
        context = _Context()

        module.register(context, service=service)

        self.assertEqual(context.platform["name"], "loopdy")
        self.assertIn("hermes bighelp configure-apns", context.platform["install_hint"])
        self.assertNotIn("link", context.platform["install_hint"].lower())
        self.assertEqual(context.platform["cron_deliver_env_var"], "LOOPDY_HOME_TARGET")
        from loopdy_plugin.loopdy_cards import MAX_DOCUMENT_BYTES
        self.assertGreater(context.platform["max_message_length"], MAX_DOCUMENT_BYTES)
        self.assertNotIn("platform_hint", context.platform or {})
        self.assertEqual(
            context.platform["parse_target_ref_fn"]("device:phone"),
            ("device:phone", None),
        )
        self.assertEqual(context.approval[0], "loopdy")
        self.assertEqual(context.cli["name"], "bighelp")
        # `hermes loopdy …` from before 3.0.0 still runs the same handler, unlisted.
        import argparse
        self.assertEqual(set(context.clis), {"bighelp", "loopdy"})
        self.assertIs(context.clis["loopdy"]["help"], argparse.SUPPRESS)
        self.assertIs(context.clis["loopdy"]["handler_fn"], context.clis["bighelp"]["handler_fn"])
        from hermes_cli.plugins import VALID_HOOKS
        self.assertEqual(
            set(context.hooks),
            {
                "pre_approval_request",
                "post_approval_response",
                "pre_tool_call",
                "post_tool_call",
                "pre_llm_call",
                "post_llm_call",
                "on_session_end",
                "subagent_start",
                "subagent_stop",
                "kanban_task_claimed",
                "kanban_task_completed",
                "kanban_task_blocked",
            } | ({"on_room_member_activity"} if "on_room_member_activity" in VALID_HOOKS else set())
              | ({"on_stream_delta"} if {"on_room_member_activity", "on_stream_delta"} <= VALID_HOOKS else set())
              # Host telemetry: the provider and the start of the answer, where Hermes has these hooks.
              | ({"pre_api_request", "on_stream_start"} & VALID_HOOKS),
        )
        self.assertEqual(set(context.skills), {"bighelp-feed-and-ideas", "bighelp", "generative-ui",
                                               "custom-theme-authoring"})
        self.assertTrue(context.skills["bighelp-feed-and-ideas"]["path"].is_file())

        self.assertIsNone(
            context.hooks["pre_llm_call"](
                is_first_turn=True,
                user_message="Show this as a native card",
            )
        )
        self.assertIsNone(
            context.hooks["pre_llm_call"](
                is_first_turn=False,
                user_message="Continue",
            )
        )

        manifest = (PLUGIN_ROOT / "plugin.yaml").read_text()
        self.assertIn("  - pre_llm_call\n", manifest)
        self.assertIn("platforms:\n  - linux\n  - macos\n  - windows\n", manifest)
        self.assertNotIn("Hermes" + "Link", manifest)

    def test_clarify_pre_tool_hook_only_updates_live_activity(
        self,
    ) -> None:
        from loopdy_plugin.registration import register

        service = _Service()
        context = _Context()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "profiles" / "personal").mkdir(parents=True)
            (home / "profiles" / "personal" / "profile.yaml").write_text(
                "ui_meta:\n  displayName: Atlas\n",
                encoding="utf-8",
            )
            with patch("loopdy_plugin.adapter.get_hermes_home", return_value=home):
                register(context, service=service)
                context.hooks["pre_tool_call"](
                    profile_name="personal",
                    session_id="stored-1",
                    turn_id="turn-1",
                    tool_name="clarify",
                    tool_call_id="clarify-1",
                    args={"question": "Choose one"},
                )

        kind, update = service.events[0]
        self.assertEqual(kind, "live-activity")
        self.assertEqual(update["profile"], "personal")
        self.assertEqual(update["phase"], "waiting")

    def test_clarify_pre_tool_hook_does_not_duplicate_adapter_notification(
        self,
    ) -> None:
        from loopdy_plugin.registration import register

        service = _Service()
        context = _Context()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "profiles" / "personal").mkdir(parents=True)
            (home / "profiles" / "personal" / "profile.yaml").write_text(
                "ui_meta:\n  displayName: Atlas\n",
                encoding="utf-8",
            )
            with (
                patch("loopdy_plugin.adapter.get_hermes_home", return_value=home),
                patch.dict("os.environ", {"LOOPDY_HOME_TARGET": "device:home"}),
            ):
                register(context, service=service)
                context.hooks["pre_tool_call"](
                    profile_name="personal",
                    session_id="stored-1",
                    turn_id="turn-1",
                    tool_name="clarify",
                    tool_call_id="clarify-1",
                    args={"question": "Choose one"},
                )

        self.assertEqual([record[0] for record in service.events], ["live-activity"])
        _, update = service.events[0]
        self.assertEqual(update["profile"], "personal")
        self.assertEqual(update["session_id"], "stored-1")
        self.assertEqual(update["phase"], "waiting")
        self.assertNotIn("detail", update)
        self.assertNotIn("tool_name", update)



    def test_retired_link_direct_and_wiki_commands_are_gone(self) -> None:
        from loopdy_plugin import registration
        from loopdy_plugin.registration import setup_cli

        parser = argparse.ArgumentParser()
        setup_cli(parser)
        for command in (["link", "status"], ["link", "unpair", "--yes"], ["direct", "status"], ["wiki", "roots"]):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit):
                parser.parse_args(command)
        self.assertEqual(parser.parse_args(["update-status"]).bighelp_action, "update-status")
        self.assertFalse(hasattr(registration, "_handle_link_cli"))

    def test_registration_removes_only_the_retired_link_keys(self) -> None:
        from loopdy_plugin.registration import register

        removed: list[str] = []
        with (
            patch.dict("os.environ", {"LOOPDY_LINK_DEVICE_ID": "host-fixture", "LOOPDY_HOME_TARGET": "all"}),
            patch("hermes_cli.config.remove_env_value", side_effect=lambda key: removed.append(key) or True),
        ):
            register(_Context(), service=_Service())
        self.assertEqual(removed, ["LOOPDY_LINK_DEVICE_ID"])

    def test_pre_llm_hook_adds_no_context_for_any_platform(self) -> None:
        from loopdy_plugin.registration import register

        context, service = _Context(), _Service()
        register(context, service=service)
        for platform in ("loopdy", "tui", ""):
            with self.subTest(platform=platform):
                self.assertIsNone(context.hooks["pre_llm_call"](
                    session_id="session-1", turn_id="turn-1", platform=platform, sender_id="phone-1"))

    def test_relay_cli_has_no_secret_value_arguments_or_public_enrollment_command(self) -> None:
        from loopdy_plugin.registration import setup_cli

        parser = argparse.ArgumentParser()
        setup_cli(parser)
        help_text = parser.format_help()
        self.assertNotIn("enroll", help_text.lower())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "configure-relay",
                    "--hmac-secret",
                    "fake-secret-value",
                    "--signing-private-key",
                    "fake-private-value",
                ]
            )



    def test_public_delegation_hooks_keep_distinct_lifecycle_and_friendly_metadata(
        self,
    ) -> None:
        from loopdy_plugin.registration import register

        service = _Service()
        context = _Context()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "profiles" / "personal").mkdir(parents=True)
            (home / "profiles" / "personal" / "profile.yaml").write_text(
                "ui_meta:\n  displayName: Atlas\n",
                encoding="utf-8",
            )
            with patch("loopdy_plugin.adapter.get_hermes_home", return_value=home):
                register(context, service=service)
                context.hooks["subagent_start"](
                    profile_name="personal",
                    parent_session_id="stored-parent",
                    child_session_id="stored-child",
                )
                context.hooks["subagent_stop"](
                    profile_name="personal",
                    parent_session_id="stored-parent",
                    child_session_id="stored-child",
                    child_status="running",
                )
                context.hooks["subagent_stop"](
                    profile_name="personal",
                    parent_session_id="stored-parent",
                    child_session_id="stored-child",
                    child_status="completed",
                )

        events = [event for event, _target in service.events]
        self.assertEqual(
            [event.type for event in events],
            ["delegation.started", "delegation.updated", "delegation.completed"],
        )
        self.assertTrue(all(event.profile == "personal" for event in events))
        self.assertTrue(all(event.session_id == "stored-parent" for event in events))
        self.assertTrue(all(event.detail["agent_name"] == "Atlas" for event in events))
        self.assertTrue(all("agent_name" not in event.push_payload for event in events))


if __name__ == "__main__":
    unittest.main()
