"""Deterministic startup/context parity; no host configuration or service I/O."""
from __future__ import annotations
import telemetry_off  # noqa: F401  (no test run posts host telemetry)

from contextlib import ExitStack
import asyncio
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest
from unittest.mock import Mock, patch

from fastapi import Request

from loopdy_plugin import native_api, native_context, registration


LOG = "hermes.plugins.bighelp"
SUMMARY = "bighelp native features advertised at startup: "
PROCESS_FEATURES = (
    "native-context-v1", "serving-profile-v1", "native-card-templates-v1",
    "native-voice-v1", "native-device-tools-v1", "native-device-location-v1", "native-device-appearance-v1",
    "native-device-contacts-v1", "native-device-photos-v1", "native-device-geofences-v1",
    "native-device-alarms-v1", "native-device-music-v1", "native-device-focus-v1", "native-device-shortcuts-v1",
    "native-device-navigation-v1",
    "native-device-tools-agents-v1", "native-device-tools-wait-v1",
    "native-room-activity-v1", "native-room-typing-v1", "native-room-peer-typing-v1",
    "native-project-git-read-v1", "native-agent-templates-v1",
    "native-workspace-files-v1", "native-workspace-recent-v1", "native-workspace-hermes-home-v1",
    "native-workspace-files-agents-v1",
    "native-host-restart-v1",
    "native-provider-usage-v1", "native-provider-sign-in-v1", "native-workflows-v1", "native-workflows-edit-v1",
    "native-workflows-parallel-v1", "native-workflows-delivery-v1", "native-workflows-outcomes-v1",
    "native-workflows-trigger-v1", "native-workflows-code-v1",
    "native-live-alerts-v1", "native-notification-quiet-hours-v1", "native-composio-v1",
    "native-agent-talk-v1",
    "native-chat-activity-v1",
    "native-agents-activity-v1",
    "native-team-v1",
    "native-my-templates-v1",
    "native-agent-plugin-links-v1",
    "native-image-drafts-v1",
    "native-host-telemetry-v1",
    "native-bot-chat-v1",
    "native-voice-catalog-v1",
    "native-skills-import-v1",
    "native-vault-link-v1",
    "native-voice-gemini-v1",
    "native-voice-context-v1",
    "native-uploads-v1",
)


NEW_KINDS = ("contacts", "photos", "geofences", "alarms", "music", "focus", "shortcuts", "navigation")


class Session(SimpleNamespace):
    pass


def module(name, **attributes) -> Any:
    value = ModuleType(name)
    value.__dict__.update(attributes)
    return value


class NativeStartupLoggingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.constants = module(
            "hermes_constants",
            get_process_hermes_home=Mock(return_value="synthetic-home"),
            profile_name_for_home=Mock(return_value="research"),
            set_hermes_home_override=Mock(), reset_hermes_home_override=Mock(),
        )
        self.profiles = module("hermes_cli.profiles", profile_exists=Mock(return_value=True))
        self.hub = SimpleNamespace(available=True, peer_typing_available=True)
        self.device = module("loopdy_plugin.native_device_tools", CAPABILITY=PROCESS_FEATURES[4],
                             LOCATION_CAPABILITY=PROCESS_FEATURES[5], APPEARANCE_CAPABILITY=PROCESS_FEATURES[6],
                             KIND_CAPABILITIES=dict(zip(NEW_KINDS, PROCESS_FEATURES[7:15])),
                             AGENTS_CAPABILITY=PROCESS_FEATURES[15], WAIT_CAPABILITY=PROCESS_FEATURES[16],
                             available=Mock(return_value=True), register_middleware=Mock(return_value=True))
        self.room = module("loopdy_plugin.room_activity", CAPABILITY=PROCESS_FEATURES[17],
                           TYPING_CAPABILITY=PROCESS_FEATURES[18], PEER_TYPING_CAPABILITY=PROCESS_FEATURES[19],
                           activity_hub=Mock(return_value=self.hub), register_room_activity=Mock())
        self.git = module("loopdy_plugin.native_project_git", CAPABILITY=PROCESS_FEATURES[20],
                          supported=Mock(return_value=True))
        self.templates = module("loopdy_plugin.agent_templates", CAPABILITY=PROCESS_FEATURES[21],
                                available=Mock(return_value=True))
        self.files = module("loopdy_plugin.workspace_artifacts", CAPABILITY=PROCESS_FEATURES[22],
                            RECENT_CAPABILITY=PROCESS_FEATURES[23], HERMES_HOME_CAPABILITY=PROCESS_FEATURES[24],
                            AGENTS_CAPABILITY=PROCESS_FEATURES[25], available=Mock(return_value=True))
        self.restart = module("loopdy_plugin.host_restart", CAPABILITY=PROCESS_FEATURES[26],
                              available=Mock(return_value=True), release_locks_left_by_restart=Mock())
        self.usage = module("loopdy_plugin.provider_usage", CAPABILITY=PROCESS_FEATURES[27],
                            available=Mock(return_value=True))
        self.sign_in = module("loopdy_plugin.provider_sign_in", CAPABILITY=PROCESS_FEATURES[28],
                              available=Mock(return_value=True))
        self.workflows = module("loopdy_plugin.workflow_api", CAPABILITY=PROCESS_FEATURES[29],
                                EDIT_CAPABILITY=PROCESS_FEATURES[30], PARALLEL_CAPABILITY=PROCESS_FEATURES[31],
                                DELIVERY_CAPABILITY=PROCESS_FEATURES[32], OUTCOMES_CAPABILITY=PROCESS_FEATURES[33],
                                TRIGGER_CAPABILITY=PROCESS_FEATURES[34], CODE_CAPABILITY=PROCESS_FEATURES[35],
                                probe=Mock(return_value=None), triggers_available=Mock(return_value=True),
                                code_available=Mock(return_value=True))
        self.live_alerts = module("loopdy_plugin.live_alerts", CAPABILITY=PROCESS_FEATURES[36],
                                  available=Mock(return_value=True))
        self.quiet = module("loopdy_plugin.quiet_hours", CAPABILITY=PROCESS_FEATURES[37],
                            available=Mock(return_value=True))
        self.composio = module("loopdy_plugin.composio", CAPABILITY=PROCESS_FEATURES[38],
                               available=Mock(return_value=True))
        self.agent_talk = module("loopdy_plugin.agent_talk", CAPABILITY=PROCESS_FEATURES[39],
                                 available=Mock(return_value=True))
        self.chat_activity = module("loopdy_plugin.chat_activity", CAPABILITY=PROCESS_FEATURES[40],
                                    available=Mock(return_value=True))
        self.agents_activity = module("loopdy_plugin.agents_activity", CAPABILITY=PROCESS_FEATURES[41],
                                      available=Mock(return_value=True))
        self.team = module("loopdy_plugin.team", CAPABILITY=PROCESS_FEATURES[42],
                           available=Mock(return_value=True), register=Mock())
        self.my_templates = module("loopdy_plugin.my_templates", CAPABILITY=PROCESS_FEATURES[43],
                                   available=Mock(return_value=True))
        self.plugin_links = module("loopdy_plugin.plugin_links", CAPABILITY=PROCESS_FEATURES[44],
                                   available=Mock(return_value=True))
        self.image_drafts = module("loopdy_plugin.image_drafts", CAPABILITY=PROCESS_FEATURES[45],
                                   available=Mock(return_value=True), register=Mock())
        self.telemetry = module("loopdy_plugin.host_telemetry", CAPABILITY=PROCESS_FEATURES[46],
                                available=Mock(return_value=True), register=Mock())
        self.bot_chat = module("loopdy_plugin.bot_chat", CAPABILITY=PROCESS_FEATURES[47],
                               available=Mock(return_value=True))
        self.voice_catalog = module("loopdy_plugin.voice_catalog", CAPABILITY=PROCESS_FEATURES[48],
                                    available=Mock(return_value=True))
        self.skills_import = module("loopdy_plugin.native_skills_import", CAPABILITY=PROCESS_FEATURES[49],
                                    available=Mock(return_value=True))
        self.vault_link = module("loopdy_plugin.vault_link", CAPABILITY=PROCESS_FEATURES[50],
                                 available=Mock(return_value=True))
        self.uploads = module("loopdy_plugin.native_uploads", CAPABILITY=PROCESS_FEATURES[53],
                              available=Mock(return_value=True))
        modules = [self.constants, self.profiles, self.device, self.room, self.git,
                   self.templates, self.files, self.restart, self.usage, self.sign_in, self.workflows,
                   self.live_alerts, self.quiet, self.composio, self.agent_talk, self.chat_activity, self.agents_activity, self.team,
                   self.my_templates, self.plugin_links, self.image_drafts, self.telemetry,
                   self.bot_chat, self.voice_catalog, self.skills_import, self.vault_link,
                   self.uploads, module("hermes_cli.dashboard_auth.base", Session=Session)]
        self.stack.enter_context(patch.dict(sys.modules, {value.__name__: value for value in modules}))
        self.stack.enter_context(patch.object(native_context, "_startup_advertisement_logged", False))
        self.stack.enter_context(patch.object(native_context, "_journal_problem", return_value=None))
        self.stack.enter_context(patch.object(native_context.time, "time", return_value=1000))

    def request(self, *, principal=False):
        session = Session(provider="fixture", user_id="alice", display_name="Alice", expires_at=2000)
        return Request({
            "type": "http", "path": "/api/plugins/loopdy/native/context", "headers": [],
            "state": {"session": session if principal else None, "token_authenticated": False},
            "app": SimpleNamespace(state=SimpleNamespace(auth_required=principal)),
        })

    def logged_features(self, records):
        summaries = [record.getMessage()[len(SUMMARY):] for record in records
                     if record.getMessage().startswith(SUMMARY)]
        self.assertEqual(len(summaries), 1)
        return tuple(summaries[0].split(", "))

    def assert_warning(self, records, feature):
        self.assertTrue(any(record.levelname == "WARNING" and feature in record.getMessage()
                            and "NOT advertised" in record.getMessage() for record in records), feature)

    def test_complete_process_inventory_matches_context_in_order(self):
        with self.assertLogs(LOG, level="INFO") as logs:
            native_context.log_native_feature_startup("research")
        context = native_context.native_context(self.request())
        self.assertEqual(self.logged_features(logs.records), PROCESS_FEATURES)
        self.assertEqual(context.features, PROCESS_FEATURES)
        self.assertEqual(context.serving_profile_id, "research")
        self.device.available.assert_called_with("research")

    def test_unavailable_capabilities_are_omitted_and_each_is_warned(self):
        self.profiles.profile_exists = None
        self.device.available.return_value = False
        self.hub.available = False
        self.hub.peer_typing_available = False
        self.git.supported.return_value = False
        self.templates.available.return_value = False
        self.files.available.return_value = False
        self.restart.available.return_value = False
        self.usage.available.return_value = False
        self.sign_in.available.return_value = False
        self.workflows.probe.return_value = "chat_runner_missing"
        self.live_alerts.available.return_value = False
        self.quiet.available.return_value = False
        self.composio.available.return_value = False
        self.agent_talk.available.return_value = False
        self.chat_activity.available.return_value = False
        self.agents_activity.available.return_value = False
        self.team.available.return_value = False
        self.my_templates.available.return_value = False
        self.plugin_links.available.return_value = False
        self.image_drafts.available.return_value = False
        self.telemetry.available.return_value = False
        self.bot_chat.available.return_value = False
        self.voice_catalog.available.return_value = False
        self.skills_import.available.return_value = False
        self.vault_link.available.return_value = False
        self.uploads.available.return_value = False
        with self.assertLogs(LOG, level="INFO") as logs:
            native_context.log_native_feature_startup("research")
        expected = PROCESS_FEATURES[:2]
        self.assertEqual(self.logged_features(logs.records), expected)
        self.assertEqual(native_context.native_context(self.request()).features, expected)
        for feature in PROCESS_FEATURES[2:]:
            self.assert_warning(logs.records, feature)

    def test_optional_import_failures_match_context_and_warn(self):
        missing = {
            "hermes_cli.profiles": ("native-card-templates-v1", "native-voice-v1", "native-voice-gemini-v1",
                                    "native-voice-context-v1"),
            "loopdy_plugin.native_device_tools": ("native-device-tools-v1", "native-device-location-v1",
                                                  "native-device-appearance-v1", *PROCESS_FEATURES[7:17]),
            "loopdy_plugin.agent_templates": ("native-agent-templates-v1",),
            "loopdy_plugin.host_telemetry": ("native-host-telemetry-v1",),
            "loopdy_plugin.workspace_artifacts": ("native-workspace-files-v1", "native-workspace-recent-v1",
                                                  "native-workspace-hermes-home-v1",
                                                  "native-workspace-files-agents-v1"),
        }
        for name, omitted in missing.items():
            with self.subTest(module=name), patch.dict(sys.modules, {name: None}), \
                    patch.object(native_context, "_startup_advertisement_logged", False):
                with self.assertLogs(LOG, level="INFO") as logs:
                    native_context.log_native_feature_startup("research")
                expected = tuple(feature for feature in PROCESS_FEATURES if feature not in omitted)
                self.assertEqual(self.logged_features(logs.records), expected)
                self.assertEqual(native_context.native_context(self.request()).features, expected)
                for feature in omitted:
                    self.assert_warning(logs.records, feature)

    def test_missing_profile_resolver_does_not_claim_serving_profile(self):
        del self.constants.profile_name_for_home
        with self.assertLogs(LOG, level="INFO") as logs:
            native_context.log_native_feature_startup("research")
        context = native_context.native_context(self.request())
        expected = tuple(feature for feature in PROCESS_FEATURES
                         if feature not in ("serving-profile-v1", "native-device-tools-v1",
                                            "native-device-location-v1", "native-device-appearance-v1",
                                            *PROCESS_FEATURES[7:17]))
        self.assertEqual(self.logged_features(logs.records), expected)
        self.assertEqual(context.features, expected)
        self.assertIsNone(context.serving_profile_id)
        self.assert_warning(logs.records, "serving-profile-v1")
        self.device.available.assert_not_called()

    def test_unresolved_profile_does_not_use_registration_profile(self):
        self.constants.profile_name_for_home.return_value = None
        with self.assertLogs(LOG, level="INFO") as logs:
            native_context.log_native_feature_startup("research")
        context = native_context.native_context(self.request())
        self.assertEqual(self.logged_features(logs.records), context.features)
        self.assertNotIn("serving-profile-v1", context.features)
        self.assertNotIn("native-device-tools-v1", context.features)
        self.assert_warning(logs.records, "serving-profile-v1")
        self.device.available.assert_not_called()

    def test_registration_profile_mismatch_uses_verified_process_profile(self):
        self.device.available.side_effect = lambda profile: profile == "personal"
        with self.assertLogs(LOG, level="INFO") as logs:
            native_context.log_native_feature_startup("personal")
        context = native_context.native_context(self.request())
        self.assertEqual(self.logged_features(logs.records), context.features)
        self.assertNotIn("native-device-tools-v1", context.features)
        self.assertEqual(context.serving_profile_id, "research")
        self.assertTrue(all(call.args == ("research",) for call in self.device.available.call_args_list))
        self.assert_warning(logs.records, "native-device-tools-v1")
        self.assertIn("Restart `hermes serve`", " ".join(logs.output))

    def test_invalid_serving_profile_keeps_api_error_and_no_success_inventory(self):
        for profile in ("Bad Profile", "", 7):
            with self.subTest(profile=profile), patch.object(
                    native_context, "_startup_advertisement_logged", False):
                self.constants.profile_name_for_home.return_value = profile
                with self.assertLogs(LOG, level="WARNING") as logs:
                    native_context.log_native_feature_startup("research")
                self.assertFalse(any(record.getMessage().startswith(SUMMARY) for record in logs.records))
                self.assertIn("native_context_unavailable", " ".join(logs.output))
                with self.assertRaises(native_context.NativeAPIError) as error:
                    native_context.native_context(self.request())
                self.assertEqual((error.exception.status, error.exception.code),
                                 (503, "native_context_unavailable"))

    def test_a_signed_in_person_sees_the_same_features_and_no_wiki(self):
        with self.assertLogs(LOG, level="INFO") as logs:
            native_context.log_native_feature_startup("research")
        self.assertEqual(self.logged_features(logs.records), PROCESS_FEATURES)
        self.assertFalse(any("wiki" in record.getMessage() for record in logs.records))
        self.assertEqual(native_context.native_context(self.request(principal=True)).features, PROCESS_FEATURES)

    def test_startup_is_logged_once_without_reprobing_and_api_stays_live(self):
        with self.assertLogs(LOG, level="INFO") as logs:
            native_context.log_native_feature_startup("research")
            self.files.available.return_value = False
            native_context.log_native_feature_startup("personal")
        self.assertEqual(self.logged_features(logs.records), PROCESS_FEATURES)
        self.files.available.assert_called_once_with()
        self.assertNotIn("native-workspace-files-v1", native_context.native_context(self.request()).features)

    def test_failed_probe_logs_once_without_leaking_details_or_changing_api_error(self):
        self.constants.profile_name_for_home.side_effect = RuntimeError("private-fixture-detail")
        with self.assertLogs(LOG, level="INFO") as logs:
            native_context.log_native_feature_startup("research")
            native_context.log_native_feature_startup("research")
        self.constants.profile_name_for_home.assert_called_once_with("synthetic-home")
        warnings = [record for record in logs.records if record.levelname == "WARNING"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("RuntimeError", warnings[0].getMessage())
        self.assertNotIn("private-fixture-detail", " ".join(logs.output))
        self.assertFalse(any(record.getMessage().startswith(SUMMARY) for record in logs.records))
        with self.assertRaisesRegex(RuntimeError, "private-fixture-detail"):
            native_context.native_context(self.request())

    def test_identity_gate_still_precedes_capability_probes(self):
        request = self.request(principal=True)
        for session, code in ((None, "native_identity_required"),
                              (Session(provider="fixture", user_id="alice", display_name="Alice",
                                       expires_at=0), "native_identity_invalid")):
            with self.subTest(code=code):
                request.state.session = session
                with self.assertRaises(native_context.NativeAPIError) as error:
                    native_context.native_context(request)
                self.assertEqual((error.exception.status, error.exception.code), (401, code))
        self.constants.get_process_hermes_home.assert_not_called()
        self.device.available.assert_not_called()

    def test_router_lifespan_logs_http_namespace_inventory_once(self):
        self.hub.available = False
        self.hub.peer_typing_available = False

        async def start():
            async with native_api.router.lifespan_context(None):
                pass
            async with native_api.router.lifespan_context(None):
                pass

        with self.assertLogs(LOG, level="INFO") as logs:
            asyncio.run(start())
        self.assertEqual(self.logged_features(logs.records),
                         native_context.native_context(self.request()).features)
        self.assertNotIn("native-room-activity-v1", self.logged_features(logs.records))
        self.assertNotIn("native-room-typing-v1", self.logged_features(logs.records))
        self.assertNotIn("native-room-peer-typing-v1", self.logged_features(logs.records))

    def test_registration_does_not_claim_http_namespace_inventory(self):
        self.hub.available = False
        self.hub.peer_typing_available = False
        self.device.available.return_value = False  # callback accepted, no lifecycle ownership

        def register_room(ctx):
            self.hub.available = True
            self.hub.peer_typing_available = True
            return Mock()

        self.room.register_room_activity.side_effect = register_room
        managed = Mock()
        self.stack.enter_context(patch.dict(sys.modules, {
            "loopdy_plugin.managed_notifications": module(
                "loopdy_plugin.managed_notifications", get_managed_notifications=lambda: managed),
            "hermes_cli.plugins": module("hermes_cli.plugins", VALID_HOOKS=set()),
        }))
        for name in ("production_manager", "register_tools", "register_device_tools",
                     "BighelpApprovalTransport", "profile_display_name", "_home_target"):
            self.stack.enter_context(patch.object(registration, name))
        ctx = Mock(profile_name="research", state=None)
        with patch.object(native_context.logger, "info") as info:
            registration.register(ctx, service=SimpleNamespace(store=object()))
        self.room.register_room_activity.assert_called_once_with(ctx)
        self.device.register_middleware.assert_called_once_with(ctx)
        info.assert_not_called()
        self.assertFalse(native_context._startup_advertisement_logged)


if __name__ == "__main__":
    unittest.main()
