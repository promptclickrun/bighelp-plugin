"""No-send regressions through Hermes delivery and bighelp's real adapter."""
from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.delivery import DeliveryRouter, DeliveryTarget
from gateway.platform_registry import PlatformEntry, platform_registry
from loopdy_plugin.adapter import BighelpAdapter
from loopdy_plugin.loopdy_cards import render_card
from loopdy_plugin.store import BighelpStore


class CronCardTransportTests(unittest.TestCase):
    def setUp(self):
        platform_registry.register(PlatformEntry(name="loopdy", label="Loopdy",
                                                adapter_factory=lambda config: None,
                                                check_fn=lambda: True))
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = BighelpStore(Path(self.folder.name) / "events.sqlite3")
        self.events = []

        def deliver(event, *, target):
            self.events.append(event)
            self.store.record_event(event, target=target)
            return {"success": True, "message_id": event.event_id}

        self.service = SimpleNamespace(store=self.store, deliver=deliver)
        self.adapter = BighelpAdapter(PlatformConfig(enabled=True), service=self.service)
        self.router = DeliveryRouter(GatewayConfig(), {Platform("loopdy"): self.adapter})
        self.router.output_dir = Path(self.folder.name)

    def send(self, text):
        result = asyncio.run(self.router.deliver(
            text, [DeliveryTarget(platform=Platform("loopdy"), chat_id="all")],
            job_id="fixture-brief", metadata={"job_id": "fixture-brief", "profile": "default"}))
        self.assertTrue(result["loopdy:all"]["success"], result)

    def test_large_card_survives_scheduler_persistence(self):
        now = datetime.now(timezone.utc)
        fixture = Path(__file__).resolve().parents[1] / "fixtures/loopdy_card_v1/static-metrics.json"
        payload = json.loads(fixture.read_text())
        for i in range(3):
            key = f"details_{i}"
            payload["elements"]["card"]["children"].append(key)
            payload["elements"][key] = {"type": "text", "props": {
                "value": {"literal": "Full synthetic briefing details. " * 45},
                "typography": "body"}, "children": []}
        card = render_card(payload, now=now)
        raw = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        self.assertGreater(len(raw), 4000)
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped):
                content = ("Cronjob Response: Brief\n(job_id: fixture-brief)\n-------------\n\n"
                           + raw + "\n\nTo stop or manage this job, send me a new message asking to pause it."
                           if wrapped else raw)
                self.send(content)
                self.assertEqual(self.events[-1].detail.get("generative_ui"), card)
        self.assertEqual(len(self.events), 2)
        # The wrapped and bare finals are one card, so the saved history keeps it once.
        saved = self.store.list_events()
        self.assertEqual([event["event_id"] for event in saved], [self.events[0].event_id])
        self.assertEqual(self.events[1].event_id, self.events[0].event_id)
        self.assertEqual(saved[0]["detail"]["generative_ui"], card)

    def test_registered_standalone_route_preserves_a_complete_card(self):
        from test_registration import _Context
        from loopdy_plugin.registration import register
        from tools.send_message_tool import _send_to_platform

        context = _Context()
        register(context, service=self.service)

        async def capture(_config, chat_id, message, **_kwargs):
            result = await self.adapter.send(chat_id, message)
            return {"success": result.success, "message_id": result.message_id}

        platform_registry.register(PlatformEntry(**{
            **context.platform, "standalone_sender_fn": capture,
        }))
        fixture = Path(__file__).resolve().parents[1] / "fixtures/loopdy_card_v1/static-metrics.json"
        payload = json.loads(fixture.read_text())
        for index in range(3):
            key = f"long_text_{index}"
            payload["elements"]["card"]["children"].append(key)
            payload["elements"][key] = {"type": "text", "props": {
                "value": {"literal": "Synthetic full-text details " * 50}}, "children": []}
        card = render_card(payload, now=datetime.now(timezone.utc))
        raw = json.dumps(card, ensure_ascii=False)
        self.assertGreater(len(raw), 4096)
        result = asyncio.run(_send_to_platform(
            Platform("loopdy"), PlatformConfig(enabled=True), "all", raw))
        self.assertTrue(result.get("success"), result)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].detail.get("generative_ui"), card)

    def test_ordinary_oversized_text_preserves_the_last_character(self):
        content = "A" * 50000 + "END_OF_BRIEFING"
        self.send(content)
        self.assertEqual("".join(e.detail["message"] for e in self.events), content)
        self.assertTrue(all(len(e.detail["message"]) <= 50000 for e in self.events))


if __name__ == "__main__":
    unittest.main()
