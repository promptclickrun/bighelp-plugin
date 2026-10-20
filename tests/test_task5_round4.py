from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from loopdy_plugin.events import build_event
from loopdy_plugin.provider import DeliveryError, DeliveryReceipt
from loopdy_plugin.service import BighelpService
from loopdy_plugin.store import BighelpStore


class Task5Round4Tests(unittest.TestCase):
    def test_direct_live_activity_retry_attempts_become_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BighelpStore(Path(directory) / "loopdy.sqlite3")
            store.upsert_live_activity(
                session_id="stored", live_session_id="live", profile="default",
                activity_id="direct-attempts", push_token="a" * 64,
                token_environment="production",
            )
            for _ in range(6):
                store.defer_live_activity_update(
                    activity_id="direct-attempts", status="running", detail="", tool_name="",
                    active_session_count=1, delay_seconds=0, failure="temporary",
                )
            row = store.pending_live_activity_update("direct-attempts")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertTrue(row["terminal"])
            self.assertEqual(store.due_live_activity_updates(), [])

    def test_direct_end_cannot_end_activity_after_new_same_owner_request_is_inserted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BighelpStore(Path(directory) / "loopdy.sqlite3")
            store.upsert_live_activity(
                session_id="stored", live_session_id="live", profile="default",
                activity_id="direct-end-request-race", push_token="a" * 64,
                token_environment="production",
            )
            store.defer_live_activity_update(
                activity_id="direct-end-request-race", status="running", detail="old",
                tool_name="", active_session_count=1, delay_seconds=0, failure="",
            )
            old = store.pending_live_activity_update("direct-end-request-race")
            self.assertIsNotNone(old)
            assert old is not None
            store.defer_live_activity_update(
                activity_id="direct-end-request-race", status="waiting", detail="new",
                tool_name="", active_session_count=1, delay_seconds=0, failure="",
                expected_session_id="stored",
                expected_live_session_id="live",
                expected_profile="default",
                expected_push_token="a" * 64,
                expected_owner_generation=int(old["owner_generation"]),
                expected_request_id=str(old["request_id"]),
            )
            result = store.end_live_activity(
                "direct-end-request-race",
                expected_session_id="stored",
                expected_live_session_id="live",
                expected_profile="default",
                expected_push_token="a" * 64,
                expected_owner_generation=int(old["owner_generation"]),
                expected_request_id=str(old["request_id"]),
            )
            self.assertFalse(result)
            self.assertIsNotNone(store.active_live_activity("direct-end-request-race"))
            pending = store.pending_live_activity_update("direct-end-request-race")
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(pending["status"], "waiting")
            self.assertNotEqual(pending["request_id"], old["request_id"])


    def test_old_direct_live_activity_send_cannot_clear_new_owner_pending_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BighelpStore(Path(directory) / "loopdy.sqlite3")
            store.upsert_live_activity(
                session_id="stored-old", live_session_id="live-old", profile="default",
                activity_id="direct-owner-race", push_token="a" * 64,
                token_environment="production",
            )
            service = BighelpService(store)
            store.defer_live_activity_update(
                activity_id="direct-owner-race", status="running", detail="", tool_name="",
                active_session_count=1, delay_seconds=0, failure="temporary",
            )
            started = threading.Event()
            release = threading.Event()

            def send(_activity, **_kwargs):
                started.set()
                release.wait(timeout=3)
                return DeliveryReceipt(delivery_id="old")

            service._send_live_activity_update = send
            thread = threading.Thread(
                target=service._reconcile_live_activity_updates
            )
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            store.upsert_live_activity(
                session_id="stored-new", live_session_id="live-new", profile="default",
                activity_id="direct-owner-race", push_token="b" * 64,
                token_environment="production",
            )
            store.defer_live_activity_update(
                activity_id="direct-owner-race", status="waiting", detail="Approve", tool_name="",
                active_session_count=1, delay_seconds=0, failure="",
            )
            release.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            pending = store.pending_live_activity_update("direct-owner-race")
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(pending["status"], "waiting")
            service.close()

    def test_old_direct_terminal_send_cannot_end_exact_same_owner_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BighelpStore(Path(directory) / "loopdy.sqlite3")
            store.upsert_live_activity(
                session_id="stored", live_session_id="live", profile="default",
                activity_id="direct-same-owner", push_token="a" * 64,
                token_environment="production",
            )
            store.defer_live_activity_update(
                activity_id="direct-same-owner", status="completed", detail="old", tool_name="",
                active_session_count=0, delay_seconds=0, failure="temporary",
            )
            started = threading.Event()
            release = threading.Event()

            def send(_activity, **_kwargs):
                started.set()
                release.wait(timeout=3)
                return DeliveryReceipt(delivery_id="old")

            service = BighelpService(store)
            service._send_live_activity_update = send
            thread = threading.Thread(target=service._reconcile_live_activity_updates)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            store.upsert_live_activity(
                session_id="stored", live_session_id="live", profile="default",
                activity_id="direct-same-owner", push_token="a" * 64,
                token_environment="production",
            )
            store.defer_live_activity_update(
                activity_id="direct-same-owner", status="waiting", detail="new", tool_name="",
                active_session_count=1, delay_seconds=0, failure="",
            )
            release.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            active = store.active_live_activity("direct-same-owner")
            self.assertIsNotNone(active)
            pending = store.pending_live_activity_update("direct-same-owner")
            self.assertIsNotNone(pending)
            assert pending is not None
            self.assertEqual(pending["status"], "waiting")
            service.close()

    def test_old_direct_retryable_send_cannot_defer_changed_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BighelpStore(Path(directory) / "loopdy.sqlite3")
            store.upsert_live_activity(
                session_id="stored-old", live_session_id="live-old", profile="default",
                activity_id="direct-retry-owner", push_token="a" * 64,
                token_environment="production",
            )
            store.defer_live_activity_update(
                activity_id="direct-retry-owner", status="running", detail="old", tool_name="",
                active_session_count=1, delay_seconds=0, failure="temporary",
            )
            started = threading.Event()
            release = threading.Event()

            def send(_activity, **_kwargs):
                started.set()
                release.wait(timeout=3)
                raise DeliveryError("temporary", retryable=True)

            service = BighelpService(store)
            service._send_live_activity_update = send
            thread = threading.Thread(target=service._reconcile_live_activity_updates)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            store.upsert_live_activity(
                session_id="stored-new", live_session_id="live-new", profile="default",
                activity_id="direct-retry-owner", push_token="b" * 64,
                token_environment="production",
            )
            release.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertIsNone(store.pending_live_activity_update("direct-retry-owner"))
            self.assertIsNotNone(store.active_live_activity("direct-retry-owner"))
            service.close()

    def test_old_direct_retryable_send_cannot_defer_exact_same_owner_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BighelpStore(Path(directory) / "loopdy.sqlite3")
            store.upsert_live_activity(
                session_id="stored", live_session_id="live", profile="default",
                activity_id="direct-retry-same-owner", push_token="a" * 64,
                token_environment="production",
            )
            store.defer_live_activity_update(
                activity_id="direct-retry-same-owner", status="running", detail="old", tool_name="",
                active_session_count=1, delay_seconds=0, failure="temporary",
            )
            started = threading.Event()
            release = threading.Event()

            def send(_activity, **_kwargs):
                started.set()
                release.wait(timeout=3)
                raise DeliveryError("temporary", retryable=True)

            service = BighelpService(store)
            service._send_live_activity_update = send
            thread = threading.Thread(target=service._reconcile_live_activity_updates)
            thread.start()
            self.assertTrue(started.wait(timeout=2))
            store.upsert_live_activity(
                session_id="stored", live_session_id="live", profile="default",
                activity_id="direct-retry-same-owner", push_token="a" * 64,
                token_environment="production",
            )
            release.set()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())
            self.assertIsNone(store.pending_live_activity_update("direct-retry-same-owner"))
            self.assertIsNotNone(store.active_live_activity("direct-retry-same-owner"))
            service.close()


    def test_worker_contains_one_delivery_exception_and_processes_the_next(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = BighelpService(BighelpStore(Path(directory) / "loopdy.sqlite3"))
            first = build_event("channel.message", correlation=("worker", "first"))
            second = build_event("channel.message", correlation=("worker", "second"))
            calls: list[str] = []

            def deliver(event, *, target):
                if event.event_id == first.event_id:
                    raise RuntimeError("unexpected delivery bug")
                calls.append(event.event_id)
                return {"delivered": 1}

            service.deliver = deliver
            self.assertTrue(service.enqueue(first, target="all"))
            self.assertTrue(service.enqueue(second, target="all"))
            service._queue.join()
            self.assertEqual(calls, [second.event_id])
            self.assertIsNotNone(service._worker)
            assert service._worker is not None
            self.assertTrue(service._worker.is_alive())
            service.close()

    @staticmethod
    def _capture(function, failures: list[BaseException]) -> None:
        try:
            function()
        except BaseException as error:
            failures.append(error)


if __name__ == "__main__":
    unittest.main()
