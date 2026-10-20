from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loopdy_plugin.events import build_event
from loopdy_plugin.presentation import shape_notification
from loopdy_plugin.provider import DeliveryError
from loopdy_plugin.service import BighelpService
from loopdy_plugin.store import BighelpStore


class Task5Round3Tests(unittest.TestCase):
    def test_closing_service_rejects_provider_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = BighelpService(BighelpStore(Path(directory) / "loopdy.sqlite3"))
            service._closing = True
            with self.assertRaisesRegex(DeliveryError, "service_closing"):
                service._provider("managed")
            self.assertNotIn("managed", service._providers)
            service._closing = False
            service.close()


    def test_startup_pending_existence_checks_are_bounded(self) -> None:
        class BoundedStore(BighelpStore):
            def __init__(self, path):
                self.calls = []
                super().__init__(path)

            def has_pending_live_activity_updates(self):
                self.calls.append(("direct", 1))
                return super().has_pending_live_activity_updates()

        with tempfile.TemporaryDirectory() as directory:
            store = BoundedStore(Path(directory) / "loopdy.sqlite3")
            service = BighelpService(store)
            self.assertEqual(
                store.calls,
                [("direct", 1)],
            )
            service.close()


    def test_notification_copy_normalizes_nfc_and_truncates_utf8_safely(self) -> None:
        event = build_event(
            "channel.message",
            correlation=("nfc-round3",),
            detail={"agent_name": "Cafe\u0301", "message": "😀" * 500},
        )
        message = shape_notification(event, {"detail_mode": "detailed"})
        self.assertEqual(message.title, "Café just messaged you!")
        self.assertEqual(message.body, "Message: " + "😀" * 197)
        self.assertLessEqual(len(message.title.encode("utf-8")), 120)
        self.assertLessEqual(len(message.body.encode("utf-8")), 800)


if __name__ == "__main__":
    unittest.main()
