"""The bighelp gateway platform: alerts through the legacy push provider."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.config import PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platform_registry import PlatformEntry, platform_registry
from loopdy_plugin.adapter import BighelpAdapter, standalone_send


class _EventStore:
    def __init__(self):
        self.events = []
        self.delivered = []
        self.failed = []
        self.records = {}

    def record_event(self, event, *, target):
        if event.event_id in self.records:
            return False
        self.events.append((event, target))
        self.records[event.event_id] = {
            "event_id": event.event_id,
            "status": "queued",
        }
        return True

    def get_event(self, event_id):
        return self.records.get(event_id)

    def mark_event_delivered(self, event_id, delivery_id=""):
        self.delivered.append((event_id, delivery_id))
        self.records[event_id]["status"] = "sent"
        return True

    def mark_event_failed(self, event_id, failure):
        self.failed.append((event_id, failure))
        self.records[event_id]["status"] = "failed"
        return True


class _Service:
    def __init__(self):
        self.deliveries = []
        self.store = _EventStore()

    def health(self):
        return {
            "configured": True,
            "ready": True,
            "detail": "Managed provider ready",
            "compatible_devices": 1,
        }

    def deliver(self, event, *, target):
        self.deliveries.append((event, target))
        return {"success": True, "message_id": "delivery-123"}

    def enqueue(self, event, *, target):
        self.deliveries.append((event, target))
        return True


def signing_fixture(device_id="host-fixture"):
    import secrets
    from cryptography.hazmat.primitives.asymmetric import ec
    from loopdy_plugin.link_client import LinkRuntimeConfig
    return LinkRuntimeConfig("https://link.example", device_id, 1, ec.generate_private_key(ec.SECP256R1()),
                             secrets.token_bytes(32))


class AdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        platform_registry.register(
            PlatformEntry(
                name="loopdy",
                label="Loopdy",
                adapter_factory=lambda config: None,
                check_fn=lambda: True,
            )
        )

    def test_outbound_channel_ledgers_content_and_pushes_only_a_wakeup(self) -> None:
        service = _Service()
        adapter = BighelpAdapter(
            PlatformConfig(enabled=True, extra={"home_target": "all"}),
            service=service,
        )

        self.assertTrue(asyncio.run(adapter.connect()))
        result = asyncio.run(
            adapter.send(
                "device:phone-123",
                "private channel content",
                metadata={"profile": "dora", "agent_name": "Dora"},
            )
        )

        self.assertTrue(result.success)
        self.assertEqual(result.message_id, "delivery-123")
        event, target = service.deliveries[0]
        self.assertEqual(target, "device:phone-123")
        self.assertEqual(event.detail["message"], "private channel content")
        self.assertEqual(event.type, "channel.message")
        self.assertEqual(event.profile, "dora")
        self.assertEqual(event.detail["agent_name"], "Dora")
        self.assertFalse(event.session_id)
        self.assertNotIn("private channel content", str(event.push_payload))

    def test_invalid_cron_card_safely_falls_back_to_text(self) -> None:
        service = _Service()
        adapter = BighelpAdapter(
            PlatformConfig(enabled=True, extra={"home_target": "all"}),
            service=service,
        )
        wrapped = (
            "Cronjob Response: Morning briefing\n"
            "(job_id: morning-briefing)\n"
            "-------------\n\n"
            '{"schema":"loopdy.generative_ui","version":1,"component":"summary"}\n\n'
            "To stop or manage this job, send me a new message."
        )

        self.assertTrue(asyncio.run(adapter.connect()))
        result = asyncio.run(
            adapter.send(
                "device:phone-123",
                wrapped,
                metadata={"job_id": "morning-briefing"},
            )
        )

        self.assertTrue(result.success)
        event, _ = service.deliveries[0]
        self.assertEqual(event.job_id, "morning-briefing")
        self.assertNotIn("generative_ui", event.detail)
        self.assertIn("Cronjob Response: Morning briefing", event.detail["message"])

    def test_standalone_sender_uses_the_registered_adapter_contract_and_closes_it(self) -> None:
        service = _Service()
        adapter = AsyncMock()
        adapter.connect.return_value = True
        adapter.send.return_value = SendResult(
            success=True,
            message_id="event-fixture-0001",
        )

        async def scenario():
            return await standalone_send(
                PlatformConfig(enabled=True, extra={"home_target": "all"}),
                "all",
                "The forecast is ready.",
                thread_id="session-coordinate-0001",
                service=service,
                adapter_factory=lambda *_args, **_kwargs: adapter,
            )

        result = asyncio.run(scenario())

        self.assertEqual(result, {
            "success": True,
            "message_id": "event-fixture-0001",
        })
        adapter.send.assert_awaited_once_with(
            "all",
            "The forecast is ready.",
            metadata={"thread_id": "session-coordinate-0001"},
        )
        adapter.disconnect.assert_awaited_once()

    def test_loopdy_platform_suppresses_gateway_lifecycle_notifications(self) -> None:
        config = PlatformConfig(enabled=True, gateway_restart_notification=True)

        adapter = BighelpAdapter(config, service=_Service())

        self.assertFalse(config.gateway_restart_notification)
        self.assertFalse(adapter.config.gateway_restart_notification)

    def test_connects_a_configured_provider_before_the_first_device_registers(self) -> None:
        service = _Service()
        service.health = lambda: {
            "configured": True,
            "ready": False,
            "detail": "No managed bighelp devices are registered",
            "compatible_devices": 0,
        }
        adapter = BighelpAdapter(PlatformConfig(enabled=True), service=service)

        self.assertTrue(asyncio.run(adapter.connect()))

    def test_native_connect_does_not_require_an_optional_notification_provider(self) -> None:
        service = _Service()
        service.health = lambda: {
            "configured": False,
            "ready": False,
            "detail": "Provider configuration is invalid",
            "compatible_devices": 0,
        }
        adapter = BighelpAdapter(PlatformConfig(enabled=True), service=service)

        self.assertTrue(asyncio.run(adapter.connect()))

    def test_channel_message_resolves_the_active_profile_display_name(self) -> None:
        service = _Service()
        adapter = BighelpAdapter(
            PlatformConfig(enabled=True), service=service
        )  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "profile.yaml").write_text(
                "ui_meta:\n  displayName: Atlas\n",
                encoding="utf-8",
            )
            with patch("loopdy_plugin.adapter.get_hermes_home", return_value=home):
                result = asyncio.run(adapter.send("all", "Household update"))

        self.assertTrue(result.success)
        event, _ = service.deliveries[0]
        self.assertEqual(event.profile, "default")
        self.assertEqual(event.detail["agent_name"], "Atlas")

    def test_profile_name_is_the_bot_title_without_pyyaml(self) -> None:
        """Alerts say the agent's name, as the app shows it, on Hermes 0.22 and later (no PyYAML)."""
        from loopdy_plugin.adapter import profile_display_name

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "profile.yaml").write_text(
                "display_name: Hermes\nui_meta:\n  hermes-bots:\n    title: Alfie\n", encoding="utf-8")
            with patch("loopdy_plugin.adapter.get_hermes_home", return_value=home), \
                    patch.dict(sys.modules, {"yaml": None}):
                self.assertEqual(profile_display_name("default"), "Alfie")
            (home / "profile.yaml").write_text("description: no name here\n", encoding="utf-8")
            with patch("loopdy_plugin.adapter.get_hermes_home", return_value=home):
                self.assertEqual(profile_display_name("default"), "Default")

    def test_non_channel_event_resolves_the_configured_profile_display_name(
        self,
    ) -> None:
        service = _Service()
        adapter = BighelpAdapter(
            PlatformConfig(enabled=True), service=service
        )  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "profile.yaml").write_text(
                "ui_meta:\n  displayName: Atlas\n",
                encoding="utf-8",
            )
            with patch("loopdy_plugin.adapter.get_hermes_home", return_value=home):
                result = asyncio.run(
                    adapter.send(
                        "all",
                        "Completed",
                        metadata={
                            "event_type": "session.completed",
                            "profile": "default",
                        },
                    )
                )

        self.assertTrue(result.success)
        event, _ = service.deliveries[0]
        self.assertEqual(event.profile, "default")
        self.assertEqual(event.detail["agent_name"], "Atlas")

    def test_cron_wrapped_renderer_envelope_persists_the_card(self) -> None:
        service = _Service()
        adapter = BighelpAdapter(PlatformConfig(enabled=True, extra={"home_target": "all"}), service=service)
        card = {
            "schema": "loopdy.generative_ui",
            "version": 1,
            "component": "summary",
            "title": "Morning briefing",
            "body": "Three priorities are ready.",
        }
        wrapped = (
            "Cronjob Response: Morning briefing\n"
            "(job_id: morning-briefing)\n"
            "-------------\n\n"
            f"{json.dumps(card)}\n\n"
            "To stop or manage this job, send me a new message "
            '(e.g. "stop reminder Morning briefing").'
        )

        self.assertTrue(asyncio.run(adapter.connect()))
        result = asyncio.run(adapter.send(
            "home", wrapped,
            metadata={"job_id": "morning-briefing", "profile": "default", "agent_name": "Gordie"},
        ))

        self.assertTrue(result.success)
        event, target = service.deliveries[0]
        self.assertEqual(target, "home")
        self.assertEqual(event.job_id, "morning-briefing")
        self.assertEqual(event.detail["message"], "Morning briefing")
        self.assertEqual(event.detail["generative_ui"], card)

    def test_a_failed_delivery_is_reported_to_hermes(self) -> None:
        service = _Service()
        service.deliver = lambda event, *, target: {"error": "No compatible bighelp devices match all"}
        adapter = BighelpAdapter(PlatformConfig(enabled=True), service=service)

        result = asyncio.run(adapter.send("all", "Update"))

        self.assertFalse(result.success)
        self.assertIn("No compatible bighelp devices", result.error)

    def test_connect_records_the_gateway_lifecycle_for_plugin_updates(self) -> None:
        loaded = []
        manager = SimpleNamespace(record_runtime_loaded=lambda: loaded.append(True))
        adapter = BighelpAdapter(PlatformConfig(enabled=True), service=_Service(), plugin_update_manager=manager)

        self.assertTrue(asyncio.run(adapter.connect()))
        self.assertEqual(loaded, [True])

    def test_corrupt_update_journal_does_not_break_the_connection(self) -> None:
        def broken_journal():
            raise RuntimeError("corrupt fixture")
        manager = SimpleNamespace(record_runtime_loaded=broken_journal)
        adapter = BighelpAdapter(PlatformConfig(enabled=True), service=_Service(), plugin_update_manager=manager)

        self.assertTrue(asyncio.run(adapter.connect()))
        self.assertTrue(adapter.is_connected)
        asyncio.run(adapter.disconnect())
        self.assertFalse(adapter.is_connected)

    def test_old_link_keys_never_enable_the_platform(self) -> None:
        from loopdy_plugin import adapter as adapter_module

        unconfigured = SimpleNamespace(health=lambda: {"configured": False})
        old_keys = {
            "LOOPDY_LINK_BASE_URL": "https://link.loopdy.example",
            "LOOPDY_LINK_DEVICE_ID": "host-fixture",
            "LOOPDY_LINK_AUTHORIZATION_EPOCH": "3",
            "LOOPDY_LINK_SIGNING_PRIVATE_KEY": "not-a-real-key",
            "LOOPDY_LINK_ACCOUNT_KEY": "not-a-real-key",
        }
        with patch.dict(os.environ, old_keys), patch.object(adapter_module, "get_service", return_value=unconfigured):
            self.assertFalse(adapter_module.validate_config(PlatformConfig(enabled=True)))
            self.assertFalse(adapter_module.is_connected(PlatformConfig(enabled=True)))
            self.assertIsNone(adapter_module.env_enablement())

    def test_the_legacy_push_provider_enables_the_platform_with_its_home_target(self) -> None:
        from loopdy_plugin import adapter as adapter_module

        configured = SimpleNamespace(health=lambda: {"configured": True})
        with patch.dict(os.environ, {"LOOPDY_HOME_TARGET": "device:phone"}), \
                patch.object(adapter_module, "get_service", return_value=configured):
            self.assertTrue(adapter_module.is_connected(PlatformConfig(enabled=True)))
            self.assertFalse(adapter_module.is_connected(PlatformConfig(enabled=False)))
            self.assertEqual(adapter_module.env_enablement(), {
                "home_target": "device:phone",
                "home_channel": {"chat_id": "device:phone", "name": "bighelp"},
            })

    def test_media_sends_never_reach_a_phone_as_attachments(self) -> None:
        service = _Service()
        adapter = BighelpAdapter(PlatformConfig(enabled=True), service=service)

        result = asyncio.run(standalone_send(
            PlatformConfig(enabled=True), "all", "Picture", media_files=["/tmp/fixture.png"], service=service))

        self.assertEqual(result, {"error": "bighelp proactive notifications do not accept attachments"})
        self.assertEqual(asyncio.run(adapter.get_chat_info("all")), {"name": "all", "type": "notification"})
        self.assertEqual(service.deliveries, [])



if __name__ == "__main__":
    unittest.main()
