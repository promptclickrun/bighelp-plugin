"""Application service for local bighelp devices and push providers."""

from __future__ import annotations

import base64
import hashlib
import logging
import queue
import random
import threading
import time
from datetime import datetime
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .events import BighelpEvent
from .presentation import shape_notification
from .provider import DeliveryError, PushProvider
from .providers.apns import ApnsPushProvider, load_apns_config
from .store import BighelpStore
from .targets import validate_target


logger = logging.getLogger("hermes.plugins.bighelp")


class _ProviderLease:
    def __init__(self, service: "BighelpService", provider: Any) -> None:
        self._service = service
        self.provider = provider
        self._released = False

    def __enter__(self) -> Any:
        return self.provider

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.release()

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._service._release_provider(self.provider)


class BighelpService:
    def __init__(
        self,
        store: BighelpStore,
        *,
        providers: Mapping[str, Any] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        jitter_fn: Callable[[], float] = random.random,
        now_fn: Callable[[], Any] = lambda: datetime.now().astimezone(),
        timestamp_fn: Callable[[], float] = time.time,
        max_attempts: int = 3,
        queue_size: int = 256,
    ):
        self.store = store
        self.store.retire_legacy_relay()
        self.managed_alert_owner: Callable[[BighelpEvent, str], bool] | None = None
        self._providers = {
            key: value for key, value in dict(providers or {}).items()
            if key in {"managed", "direct"}
        }
        self._sleep = sleep_fn
        self._jitter = jitter_fn
        self._now = now_fn
        self._timestamp = timestamp_fn
        self._max_attempts = min(5, max(1, int(max_attempts)))
        self._queue: queue.Queue[tuple[BighelpEvent, str] | None] = queue.Queue(
            maxsize=min(4096, max(1, int(queue_size)))
        )
        self._live_activity_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(
            maxsize=min(4096, max(1, int(queue_size)))
        )
        self._worker: threading.Thread | None = None
        self._live_activity_worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._recovery_wakeup = threading.Event()
        self._live_activity_worker_lock = threading.Lock()
        self._live_activity_lifecycle_lock = threading.Lock()
        self._provider_lock = threading.RLock()
        self._provider_inflight: dict[int, int] = {}
        self._provider_retired: set[int] = set()
        self._provider_close_events: dict[int, threading.Event] = {}
        self._provider_closed: set[int] = set()
        self._provider_identity: dict[int, Any] = {
            id(provider): provider for provider in self._providers.values()
        }
        self._closed = False
        self._closing = False
        if self.store.has_pending_live_activity_updates():
            self._ensure_live_activity_worker()

    def health(self) -> dict[str, Any]:
        mode = self.store.provider_mode()
        try:
            self._provider(mode)
            configured = True
            configuration_error = ""
        except (ValueError, DeliveryError) as error:
            configured = False
            configuration_error = _safe_error(error)
        compatible_devices = len(self.store.resolve_devices("all", mode))
        ready = configured and compatible_devices > 0
        if not configured:
            detail = configuration_error
        elif compatible_devices == 0:
            detail = f"No {mode} bighelp devices are registered"
        else:
            detail = f"{mode.capitalize()} provider ready"
        return {
            "mode": mode,
            "configured": configured,
            "ready": ready,
            "detail": detail,
            "compatible_devices": compatible_devices,
        }

    def set_provider_mode(self, mode: str) -> dict[str, Any]:
        self._assert_open()
        normalized = str(mode or "").strip().lower()
        if normalized not in {"managed", "direct"}:
            raise ValueError("bighelp provider mode must be managed or direct")
        retire: Any | None = None
        with self._provider_lock:
            if normalized == "direct":
                try:
                    config = load_apns_config(self.store.load_apns_config() or {})
                except ValueError as error:
                    raise ValueError("Configure APNs before selecting the direct provider") from error
                previous = self._providers.get("direct")
                replacement = ApnsPushProvider(config)
                self._reset_provider_bookkeeping_locked(replacement)
                self._providers["direct"] = replacement
                retire = previous
            elif normalized == "managed":
                previous = self._providers.pop("direct", None)
                retire = previous
            self.store.set_provider_mode(normalized)
        if retire is not None:
            self._retire_provider(retire)
        return self.health()

    def register_device(
        self,
        *,
        device_id: str,
        endpoint_id: str,
        provider: str | None = None,
        token_environment: str = "production",
        label: str = "",
        groups: list[str] | None = None,
        preferences: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._assert_open()
        active_mode = self.store.provider_mode()
        selected_provider = str(provider or active_mode).strip().lower()
        if selected_provider not in {"managed", "direct"}:
            raise ValueError("Legacy Cloudflare relay is retired; use managed or direct notifications")
        normalized_groups = list(groups or [])
        _validate_device_routing(device_id, normalized_groups)
        _validate_endpoint(selected_provider, endpoint_id)
        if selected_provider == "direct":
            try:
                load_apns_config(self.store.load_apns_config() or {})
            except ValueError as error:
                raise ValueError("Configure APNs before registering direct devices") from error
        normalized_environment = str(token_environment or "production").strip().lower()
        if normalized_environment not in {"production", "sandbox"}:
            raise ValueError("Token environment must be production or sandbox")
        self.store.upsert_device(
            device_id=device_id,
            endpoint_id=endpoint_id,
            provider=selected_provider,
            token_environment=normalized_environment,
            label=label,
            groups=normalized_groups,
            preferences=preferences,
        )
        return {
            "registered": True,
            "device_id": device_id,
            "provider": selected_provider,
        }

    def register_live_activity(
        self,
        *,
        session_id: str,
        live_session_id: str,
        profile: str,
        activity_id: str,
        push_token: str,
        token_environment: str,
    ) -> dict[str, Any]:
        self._assert_open()
        try:
            load_apns_config(self.store.load_apns_config() or {}, environ={})
            provider = self._provider("direct")
        except (ValueError, DeliveryError) as error:
            raise ValueError(
                "Configure direct APNs before registering Live Activities"
            ) from error
        if not isinstance(provider, ApnsPushProvider):
            raise ValueError("Direct APNs is required for Live Activities")
        self.store.upsert_live_activity(
            session_id=session_id,
            live_session_id=live_session_id,
            profile=profile,
            activity_id=activity_id,
            push_token=push_token,
            token_environment=token_environment,
        )
        return {"registered": True, "activity_id": activity_id}

    def enqueue_live_activity_update(self, **update: Any) -> bool:
        with self._live_activity_lifecycle_lock:
            if self._closed or self._closing:
                return False
            self._ensure_live_activity_worker()
            try:
                self._live_activity_queue.put_nowait(dict(update))
                return True
            except queue.Full:
                logger.warning("bighelp Live Activity update queue is full")
                return False

    def update_live_activities(
        self,
        *,
        session_id: str,
        profile: str,
        status: str = "",
        detail: str = "",
        phase: str = "",
        tool_name: str = "",
        active_session_count: int = 1,
    ) -> dict[str, Any]:
        self._assert_open()
        normalized_phase = _live_activity_phase(phase or status)
        activities = self.store.active_live_activities(session_id, profile)
        if not activities:
            return {"matched": 0, "delivered": 0, "failed": 0}
        delivered = 0
        failed = 0
        for activity in activities:
            activity_id = str(activity["activity_id"])
            try:
                with self.store.live_activity_send_lock(activity_id):
                    current_activity = self.store.active_live_activity(
                        activity_id,
                        expected_session_id=str(activity["session_id"]),
                        expected_live_session_id=str(activity["live_session_id"]),
                        expected_profile=str(activity["profile"]),
                        expected_push_token=str(activity["push_token"]),
                    )
                    if current_activity is None:
                        continue
                    owner = {
                        "expected_session_id": str(current_activity["session_id"]),
                        "expected_live_session_id": str(current_activity["live_session_id"]),
                        "expected_profile": str(current_activity["profile"]),
                        "expected_push_token": str(current_activity["push_token"]),
                        "expected_owner_generation": int(current_activity.get("owner_generation") or 0),
                    }
                    pending = self.store.pending_live_activity_update(activity_id)
                    if pending is not None:
                        pending_status = str(pending["status"])
                        pending_terminal = bool(int(pending.get("terminal") or 0)) or pending_status in {
                            "completed", "failed"
                        }
                        current_terminal = normalized_phase in {"completed", "failed"}
                        routine = {"thinking", "running"}
                        replace_exhausted_routine = (
                            pending_terminal
                            and pending_status in routine
                            and normalized_phase in {"waiting", "completed", "failed"}
                        )
                        replace_routine_with_waiting = (
                            not pending_terminal
                            and pending_status in routine
                            and normalized_phase == "waiting"
                        )
                        if (current_terminal and not pending_terminal) or replace_exhausted_routine or replace_routine_with_waiting:
                            self.store.clear_pending_live_activity_update(
                                activity_id, **owner,
                                expected_request_id=str(pending.get("request_id") or ""),
                            )
                            pending = None
                        else:
                            continue
                    request_id = str((pending or {}).get("request_id") or "")
                    try:
                        self._send_live_activity_update(
                            current_activity,
                            status=normalized_phase,
                            detail="",
                            tool_name="",
                            active_session_count=active_session_count,
                        )
                        delivered += 1
                        if pending is not None and normalized_phase not in {"completed", "failed"}:
                            self.store.clear_pending_live_activity_update(
                                activity_id, **owner, expected_request_id=request_id,
                            )
                        if normalized_phase in {"completed", "failed"}:
                            self.store.end_live_activity(
                                activity_id, **owner, expected_request_id=request_id,
                            )
                    except Exception as error:
                        failed += 1
                        logger.warning(
                            "bighelp Live Activity update failed for %s: %s",
                            activity_id,
                            _safe_error(error),
                        )
                        if isinstance(error, DeliveryError) and error.invalid_token:
                            self.store.end_live_activity(
                                activity_id, **owner, expected_request_id=request_id,
                            )
                        elif isinstance(error, DeliveryError) and error.retryable:
                            self.store.defer_live_activity_update(
                                activity_id=activity_id,
                                status=normalized_phase,
                                detail="",
                                tool_name="",
                                active_session_count=active_session_count,
                                delay_seconds=1,
                                failure=_safe_error(error),
                                **owner,
                                expected_request_id=request_id,
                            )
                            if not self._closed:
                                self._ensure_live_activity_worker()
                        elif pending is not None:
                            self.store.clear_pending_live_activity_update(
                                activity_id, **owner, expected_request_id=request_id,
                            )
            except Exception as error:
                failed += 1
                logger.warning(
                    "bighelp Live Activity serialization failed for %s: %s",
                    activity_id,
                    _safe_error(error),
                )
        return {"matched": len(activities), "delivered": delivered, "failed": failed}

    def update_device_preferences(
        self,
        device_id: str,
        preferences: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not self.store.update_preferences(device_id, preferences):
            raise ValueError("Unknown or revoked bighelp device")
        return {"updated": True, "device_id": device_id}

    def revoke_device(self, device_id: str) -> dict[str, Any]:
        self._assert_open()
        if not self.store.revoke_device(device_id):
            raise ValueError("Unknown or already revoked bighelp device")
        return {"revoked": True, "device_id": device_id}

    def test_notification(self, target: str = "all", *, profile: str = "default") -> dict[str, Any]:
        from .events import build_event

        return self.deliver(
            build_event(
                "attention.required",
                profile=profile,
                detail={"message": "bighelp test notification"},
            ),
            target=target,
        )

    def managed_notification_policy(self, event: BighelpEvent, device_id: str) -> dict[str, Any]:
        """Reuse existing explicit device preferences without provisioning a legacy sender."""
        device = self.store.get_device(device_id)
        preferences = (device or {}).get("preferences") or {}
        return {"suppression": _suppression_reason(event, preferences, self._now()),
                "sound": preferences.get("priority_sound") is not False}

    def enqueue(self, event: BighelpEvent, *, target: str) -> bool:
        if self._closed or self._closing:
            return False
        self.store.record_event(event, target=target)
        self._ensure_worker()
        try:
            self._queue.put_nowait((event, target))
            return True
        except queue.Full:
            self.store.mark_event_failed(event.event_id, "bighelp delivery queue is full")
            logger.warning("bighelp delivery queue is full; dropped %s", event.event_id)
            return False

    def deliver(self, event: BighelpEvent, *, target: str) -> dict[str, Any]:
        self._assert_open()
        verdict = validate_target(target)
        if verdict is not True:
            return {"error": str(verdict), "event_id": event.event_id}
        self.store.record_event(event, target=target)
        devices = self.store.resolve_devices(target)
        if not devices:
            message = f"No compatible bighelp devices match {target}"
            self.store.mark_event_failed(event.event_id, message)
            return {"error": message, "event_id": event.event_id}

        existing = {
            item["device_id"]: item
            for item in self.store.list_event_deliveries(event.event_id)
        }
        delivered = 0
        failed = 0
        suppressed = 0
        delivery_ids: list[str] = []
        for device in devices:
            mode = str(device["provider"])
            previous = existing.get(device["device_id"])
            if previous is not None:
                if previous["status"] == "sent":
                    delivered += 1
                    if previous.get("delivery_id"):
                        delivery_ids.append(str(previous["delivery_id"]))
                elif previous["status"] == "suppressed":
                    suppressed += 1
                else:
                    failed += 1
                continue
            preferences = device.get("preferences") or {}
            owner = getattr(self, "managed_alert_owner", None)
            managed_owned = callable(owner) and owner(event, str(device["device_id"]))
            suppression = "managed_notification_owner" if managed_owned else _suppression_reason(event, preferences, self._now())
            if suppression:
                self.store.record_device_delivery(
                    event_id=event.event_id,
                    device_id=device["device_id"],
                    provider=mode,
                    status="suppressed",
                    failure=suppression,
                )
                suppressed += 1
                continue
            lease: _ProviderLease | None = None
            try:
                provider = self._provider(mode)
                lease = self._provider_lease_for(provider)
                message = shape_notification(event, preferences)
            except Exception as error:
                if lease is not None:
                    lease.release()
                self.store.record_device_delivery(
                    event_id=event.event_id,
                    device_id=device["device_id"],
                    provider=mode,
                    status="failed",
                    failure=_safe_error(error),
                )
                failed += 1
                continue
            try:
                self.store.record_device_delivery(
                    event_id=event.event_id, device_id=device["device_id"], provider=mode,
                    status="queued",
                )
            except Exception:
                lease.release()
                raise
            try:
                receipt = self._send_with_retry(
                    provider,
                    device["endpoint_id"],
                    message,
                    environment=device.get("token_environment") or "",
                )
            except Exception as error:
                failure = _safe_error(error)
                self.store.record_device_delivery(
                    event_id=event.event_id, device_id=device["device_id"], provider=mode,
                    status="failed", failure=failure,
                )
                if isinstance(error, DeliveryError) and error.invalid_token:
                    self.store.revoke_device(device["device_id"])
                logger.warning(
                    "bighelp %s delivery failed for event %s device %s: %s",
                    mode,
                    event.event_id,
                    device["device_id"],
                    failure,
                )
                failed += 1
                continue
            finally:
                lease.release()
            self.store.record_device_delivery(
                event_id=event.event_id, device_id=device["device_id"], provider=mode,
                status="sent", delivery_id=receipt.delivery_id,
            )
            if receipt.pending_receipt_id:
                self.store.record_provider_receipt(
                    receipt_id=receipt.pending_receipt_id,
                    event_id=event.event_id,
                    device_id=device["device_id"],
                    provider=mode,
                )
            delivered += 1
            delivery_ids.append(receipt.delivery_id)

        success = delivered > 0 or (suppressed > 0 and failed == 0)
        if success:
            self.store.mark_event_delivered(
                event.event_id,
                delivery_ids[0] if delivery_ids else event.event_id,
            )
        else:
            self.store.mark_event_failed(event.event_id, "All bighelp device deliveries failed")
        result: dict[str, Any] = {
            "success": success,
            "event_id": event.event_id,
            "message_id": delivery_ids[0] if delivery_ids else event.event_id,
            "delivered": delivered,
            "failed": failed,
            "suppressed": suppressed,
            "queued": 0,
        }
        if not success:
            result["error"] = "All bighelp device deliveries failed"
        return result

    def reconcile_receipts(self) -> dict[str, int]:
        self._assert_open()
        totals = {"checked": 0, "delivered": 0, "failed": 0}
        for mode, provider in list(self._providers.items()):
            receipts_fn = getattr(provider, "receipts", None)
            if not callable(receipts_fn):
                continue
            try:
                pending = self.store.pending_provider_receipts(mode, limit=1000)
            except Exception as error:
                logger.warning(
                    "bighelp %s receipt ledger read failed: %s", mode, _safe_error(error)
                )
                continue
            if not pending:
                continue
            try:
                lease = self._provider_lease_for(provider)
            except DeliveryError:
                continue
            try:
                with lease:
                    results = receipts_fn([item["receipt_id"] for item in pending])
            except Exception as error:
                logger.warning("bighelp %s receipt reconciliation failed: %s", mode, _safe_error(error))
                continue
            by_id = {item["receipt_id"]: item for item in pending}
            for receipt_id, receipt in results.items():
                item = by_id.get(receipt_id)
                if item is None:
                    continue
                if not self.store.complete_provider_receipt(
                    receipt_id,
                    status=receipt.status,
                    error=receipt.error_code,
                ):
                    continue
                totals["checked"] += 1
                totals[receipt.status] += 1
                if receipt.status == "failed":
                    self.store.record_device_delivery(
                        event_id=item["event_id"],
                        device_id=item["device_id"],
                        provider=mode,
                        status="failed",
                        delivery_id=receipt_id,
                        failure=receipt.error_code,
                    )
                if receipt.invalid_token:
                    self.store.revoke_device(item["device_id"])
        return totals

    def close(self) -> None:
        with self._live_activity_lifecycle_lock:
            if self._closed:
                return
            self._closing = True
        worker = self._worker
        if worker is not None:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            worker.join(timeout=2)
        live_activity_worker = self._live_activity_worker
        if live_activity_worker is not None and live_activity_worker.is_alive():
            while live_activity_worker.is_alive():
                try:
                    self._live_activity_queue.put(None, timeout=0.25)
                    break
                except queue.Full:
                    continue
            live_activity_worker.join()
        with self._provider_lock:
            providers = list(self._providers.values())
            self._providers.clear()
        for provider in providers:
            self._retire_provider(provider)
        with self._live_activity_lifecycle_lock:
            self._closed = True

    def _provider(self, mode: str) -> Any:
        self._assert_open()
        with self._provider_lock:
            self._assert_open()
            provider = self._providers.get(mode)
            if provider is not None:
                return provider
            if mode == "managed":
                raise ValueError("Complete BuzzKit notification setup in bighelp before sending alerts")
            elif mode == "direct":
                provider = ApnsPushProvider(load_apns_config(self.store.load_apns_config() or {}))
            else:
                raise ValueError("bighelp provider mode must be managed or direct")
            self._reset_provider_bookkeeping_locked(provider)
            self._providers[mode] = provider
            return provider

    def _reset_provider_bookkeeping_locked(self, provider: Any) -> None:
        """Clear stale identity state before installing a newly-created provider.

        Provider leases use object identities because providers are not part of
        the public contract and may not be hashable.  An identity can be reused
        by Python after an old provider is closed, so a new provider must never
        inherit that old provider's lifecycle state.
        """
        identifier = id(provider)
        self._provider_identity[identifier] = provider
        self._provider_inflight.pop(identifier, None)
        self._provider_retired.discard(identifier)
        self._provider_close_events.pop(identifier, None)
        self._provider_closed.discard(identifier)

    def _provider_lease(self, mode: str) -> "_ProviderLease":
        self._assert_open()
        with self._provider_lock:
            provider = self._provider(mode)
            identifier = id(provider)
            if identifier in self._provider_closed or identifier in self._provider_retired:
                raise DeliveryError("provider_closing", retryable=True)
            self._provider_inflight[identifier] = self._provider_inflight.get(identifier, 0) + 1
        return _ProviderLease(self, provider)

    def _provider_lease_for(self, provider: Any) -> "_ProviderLease":
        self._assert_open()
        with self._provider_lock:
            if self._provider_identity.get(id(provider)) is not provider:
                raise DeliveryError("provider_closing", retryable=True)
            if (
                not any(candidate is provider for candidate in self._providers.values())
                or id(provider) in self._provider_closed
                or id(provider) in self._provider_retired
            ):
                raise DeliveryError("provider_closing", retryable=True)
            self._provider_inflight[id(provider)] = self._provider_inflight.get(id(provider), 0) + 1
        return _ProviderLease(self, provider)

    def _release_provider(self, provider: Any) -> None:
        should_close = False
        event: threading.Event | None = None
        identifier = id(provider)
        with self._provider_lock:
            count = self._provider_inflight.get(identifier, 0)
            if count <= 1:
                self._provider_inflight.pop(identifier, None)
                event = self._provider_close_events.pop(identifier, None)
                should_close = identifier in self._provider_retired
            else:
                self._provider_inflight[identifier] = count - 1
        if event is not None:
            event.set()
        # A retirement waiter owns close when it has an event.  The final
        # lease only wakes that waiter; both paths must never close the same
        # provider independently.
        if should_close and event is None:
            self._close_provider(provider)

    def _retire_provider(self, provider: Any) -> None:
        identifier = id(provider)
        event: threading.Event | None = None
        with self._provider_lock:
            if self._provider_identity.get(identifier) is not provider:
                return
            if identifier in self._provider_closed or identifier in self._provider_retired:
                return
            self._provider_retired.add(identifier)
            if self._provider_inflight.get(identifier, 0) > 0:
                event = self._provider_close_events.setdefault(identifier, threading.Event())
        if event is not None:
            event.wait()
        self._close_provider(provider)

    def _close_provider(self, provider: Any) -> None:
        identifier = id(provider)
        with self._provider_lock:
            if self._provider_identity.get(identifier) is not provider:
                return
            if identifier in self._provider_closed:
                return
            self._provider_closed.add(identifier)
        close = getattr(provider, "close", None)
        try:
            if callable(close):
                close()
        finally:
            with self._provider_lock:
                self._provider_inflight.pop(identifier, None)
                self._provider_close_events.pop(identifier, None)
                self._provider_retired.discard(identifier)
                self._provider_closed.discard(identifier)
                self._provider_identity.pop(identifier, None)

    def _assert_open(self) -> None:
        with self._live_activity_lifecycle_lock:
            if self._closed:
                raise DeliveryError("service_closed", retryable=False)
            if self._closing:
                # close() drains work already admitted to the Live Activity
                # queue before retiring providers. New callers remain gated.
                if threading.current_thread() is not self._live_activity_worker:
                    raise DeliveryError("service_closing", retryable=False)

    def _send_with_retry(
        self,
        provider: PushProvider,
        token: str,
        message: Any,
        *,
        environment: str,
    ) -> Any:
        for attempt in range(1, self._max_attempts + 1):
            try:
                return provider.send(token, message, environment=environment)
            except DeliveryError as error:
                if not error.retryable or attempt >= self._max_attempts:
                    raise
                delay = 0.25 * (2 ** (attempt - 1)) + min(0.25, max(0.0, self._jitter()) * 0.1)
                self._sleep(delay)
        raise DeliveryError("retry_exhausted", retryable=False)

    def _send_live_activity_update(
        self,
        activity: Mapping[str, Any],
        *,
        status: str,
        detail: str,
        tool_name: str,
        active_session_count: int,
    ) -> Any:
        phase = _live_activity_phase(status)
        with self._provider_lease("direct") as provider:
            if not isinstance(provider, ApnsPushProvider):
                raise ValueError("Direct APNs is required for Live Activities")
            return self._send_live_activity_with_retry(
                provider,
                str(activity["push_token"]),
                activity_id=str(activity["activity_id"]),
                phase=phase,
                active_session_count=active_session_count,
                session_ref=_session_reference(
                    str(activity.get("live_session_id") or activity.get("session_id") or "")
                ),
                environment=str(activity["token_environment"]),
                _expected_session_id=str(activity["session_id"]),
                _expected_live_session_id=str(activity["live_session_id"]),
                _expected_profile=str(activity["profile"]),
                _expected_owner_generation=int(activity.get("owner_generation") or 0),
            )

    def _send_live_activity_with_retry(
        self,
        provider: ApnsPushProvider,
        token: str,
        **update: Any,
    ) -> Any:
        expected_session_id = str(update.pop("_expected_session_id", ""))
        expected_live_session_id = str(update.pop("_expected_live_session_id", ""))
        expected_profile = str(update.pop("_expected_profile", ""))
        expected_owner_generation = int(update.pop("_expected_owner_generation", 0) or 0)
        for attempt in range(1, self._max_attempts + 1):
            try:
                timestamp = self.store.allocate_live_activity_timestamp(
                    str(update["activity_id"]),
                    int(self._timestamp()),
                    expected_session_id=expected_session_id,
                    expected_live_session_id=expected_live_session_id,
                    expected_profile=expected_profile,
                    expected_push_token=token,
                    expected_owner_generation=expected_owner_generation,
                )
                phase = str(update["phase"])
                return provider.send_live_activity(
                    token,
                    timestamp=timestamp,
                    expires=timestamp + 120,
                    progress=100 if phase in {"completed", "failed"} else 0,
                    **update,
                )
            except DeliveryError as error:
                if not error.retryable or attempt >= self._max_attempts:
                    raise
                delay = 0.25 * (2 ** (attempt - 1)) + min(
                    0.25, max(0.0, self._jitter()) * 0.1
                )
                self._sleep(delay)
        raise DeliveryError("retry_exhausted", retryable=False)

    def _ensure_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run_worker,
                name="loopdy-delivery",
                daemon=True,
            )
            self._worker.start()
            self._recovery_wakeup.set()

    def _ensure_live_activity_worker(self) -> None:
        if self._live_activity_worker is not None and self._live_activity_worker.is_alive():
            return
        with self._live_activity_worker_lock:
            if self._live_activity_worker is not None and self._live_activity_worker.is_alive():
                return
            self._live_activity_worker = threading.Thread(
                target=self._run_live_activity_worker,
                name="loopdy-live-activity",
                daemon=True,
            )
            self._live_activity_worker.start()

    def _run_worker(self) -> None:
        last_recovery_scan = 0.0
        while True:
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                if self._closed:
                    return
                now = time.monotonic()
                # The wake-up event is an accelerator; the periodic scan is
                # a bounded safety net for due durable rows created by a
                # different process.  Neither path scans on every idle poll.
                if not self._recovery_wakeup.is_set() and now - last_recovery_scan < 10.0:
                    continue
                self._recovery_wakeup.clear()
                try:
                    # The database is the durable source of truth. The
                    # in-memory queue is only a wake-up accelerator, so this
                    # bounded scan also drains rows beyond the startup page.
                    self.reconcile_receipts()
                    last_recovery_scan = now
                except Exception as error:
                    if not self._closing:
                        logger.warning("bighelp background reconciliation failed: %s", _safe_error(error))
                continue
            try:
                if item is None:
                    return
                event, target = item
                try:
                    self.deliver(event, target=target)
                except Exception as error:
                    # A malformed or otherwise unexpected event must not
                    # kill the sole durable recovery owner.  Delivery errors
                    # are persisted by deliver where possible; this catch is
                    # the final containment boundary for worker continuity.
                    logger.warning(
                        "bighelp queued delivery failed: %s", _safe_error(error)
                    )
            finally:
                self._queue.task_done()

    def _reconcile_live_activity_updates(self) -> None:
        pending = self.store.due_live_activity_updates(limit=100)
        if not pending:
            return
        for item in pending:
            activity_id = str(item["activity_id"])
            try:
                with self.store.live_activity_send_lock(activity_id):
                    activity = self.store.active_live_activity(activity_id)
                    current = self.store.pending_live_activity_update(activity_id)
                    if activity is None or current is None:
                        continue
                    try:
                        self._send_live_activity_update(
                            activity,
                            status=str(current["status"]),
                            detail=str(current["detail"]),
                            tool_name=str(current["tool_name"]),
                            active_session_count=int(current["active_session_count"]),
                        )
                        if str(current["status"]) in {"completed", "failed"}:
                            self.store.end_live_activity(
                                activity_id,
                                expected_session_id=str(activity["session_id"]),
                                expected_live_session_id=str(activity["live_session_id"]),
                                expected_profile=str(activity["profile"]),
                                expected_push_token=str(activity["push_token"]),
                                expected_owner_generation=int(activity.get("owner_generation") or 0),
                                expected_request_id=str(current.get("request_id") or ""),
                            )
                        else:
                            self.store.clear_pending_live_activity_update(
                                activity_id,
                                expected_session_id=str(activity["session_id"]),
                                expected_live_session_id=str(activity["live_session_id"]),
                                expected_profile=str(activity["profile"]),
                                expected_push_token=str(activity["push_token"]),
                                expected_owner_generation=int(activity.get("owner_generation") or 0),
                                expected_request_id=str(current.get("request_id") or ""),
                            )
                    except Exception as error:
                        if isinstance(error, DeliveryError) and error.invalid_token:
                            self.store.end_live_activity(
                                activity_id,
                                expected_session_id=str(activity["session_id"]),
                                expected_live_session_id=str(activity["live_session_id"]),
                                expected_profile=str(activity["profile"]),
                                expected_push_token=str(activity["push_token"]),
                                expected_owner_generation=int(activity.get("owner_generation") or 0),
                                expected_request_id=str(current.get("request_id") or ""),
                            )
                            continue
                        if isinstance(error, DeliveryError) and error.retryable:
                            attempts = max(1, int(current["attempts"]))
                            self.store.defer_live_activity_update(
                                activity_id=activity_id,
                                status=str(current["status"]),
                                detail=str(current["detail"]),
                                tool_name=str(current["tool_name"]),
                                active_session_count=int(current["active_session_count"]),
                                delay_seconds=min(300, 2 ** min(8, attempts)),
                                failure=_safe_error(error),
                                expected_session_id=str(activity["session_id"]),
                                expected_live_session_id=str(activity["live_session_id"]),
                                expected_profile=str(activity["profile"]),
                                expected_push_token=str(activity["push_token"]),
                                expected_owner_generation=int(activity.get("owner_generation") or 0),
                                expected_request_id=str(current.get("request_id") or ""),
                            )
                        else:
                            self.store.clear_pending_live_activity_update(
                                activity_id,
                                expected_session_id=str(activity["session_id"]),
                                expected_live_session_id=str(activity["live_session_id"]),
                                expected_profile=str(activity["profile"]),
                                expected_push_token=str(activity["push_token"]),
                                expected_owner_generation=int(activity.get("owner_generation") or 0),
                                expected_request_id=str(current.get("request_id") or ""),
                            )
            except Exception as error:
                logger.warning(
                    "bighelp deferred Live Activity serialization failed for %s: %s",
                    activity_id,
                    _safe_error(error),
                )

    def _run_live_activity_worker(self) -> None:
        while True:
            try:
                item = self._live_activity_queue.get(timeout=0.25)
            except queue.Empty:
                if self._closed:
                    return
                self._reconcile_live_activity_updates()
                continue
            try:
                if item is None:
                    return
                try:
                    self.update_live_activities(**item)
                except Exception as error:
                    logger.warning(
                        "bighelp Live Activity update skipped: %s",
                        _safe_error(error),
                    )
            finally:
                self._live_activity_queue.task_done()


def _safe_error(error: Exception) -> str:
    if isinstance(error, DeliveryError):
        return f"Push provider error {error.status}: {error.code}"
    if isinstance(error, ValueError):
        return str(error)[:500]
    return f"bighelp delivery failed: {type(error).__name__}"


def _live_activity_phase(value: Any) -> str:
    normalized = str(value or "").strip()
    mapping = {
        "reasoning": "thinking",
        "needsResponse": "waiting",
        "runningCommand": "running",
        "callingTool": "running",
        "replying": "running",
    }
    phase = mapping.get(normalized, normalized)
    if phase not in {"thinking", "waiting", "running", "completed", "failed"}:
        raise ValueError("Live Activity phase is invalid")
    return phase


def _session_reference(value: str) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _suppression_reason(event: BighelpEvent, preferences: Mapping[str, Any], now: Any) -> str:
    if preferences.get("notifications_enabled") is False:
        return "notifications_disabled"
    enabled_types = preferences.get("enabled_types")
    if isinstance(enabled_types, list) and event.type not in enabled_types:
        return "event_type_disabled"
    quiet_hours = preferences.get("quiet_hours")
    if isinstance(quiet_hours, Mapping) and _in_quiet_hours(
        quiet_hours,
        now,
        preferences.get("timezone"),
    ):
        return "quiet_hours"
    return ""


def _in_quiet_hours(
    quiet_hours: Mapping[str, Any],
    now: Any,
    timezone_name: Any = None,
) -> bool:
    start = _clock_minutes(quiet_hours.get("start"))
    end = _clock_minutes(quiet_hours.get("end"))
    if start is None or end is None or start == end:
        return False
    zone = None
    if str(timezone_name or "").strip():
        try:
            zone = ZoneInfo(str(timezone_name).strip())
        except ZoneInfoNotFoundError:
            zone = None
    if isinstance(now, datetime):
        current = now
        if zone is not None:
            current = current.astimezone(zone) if current.tzinfo else current.replace(tzinfo=zone)
    else:
        current = (
            datetime.fromtimestamp(float(now), tz=zone)
            if zone is not None
            else datetime.fromtimestamp(float(now)).astimezone()
        )
    minute = current.hour * 60 + current.minute
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _clock_minutes(value: Any) -> int | None:
    text = str(value or "")
    parts = text.split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    hour, minute = (int(part) for part in parts)
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _validate_endpoint(provider: str, endpoint_id: str) -> None:
    value = str(endpoint_id or "").strip()
    if provider == "managed":
        if not (
            value.startswith("ExponentPushToken[") or value.startswith("ExpoPushToken[")
        ) or not value.endswith("]"):
            raise ValueError("Managed devices require an Expo push token")
    elif provider == "direct":
        if len(value) < 64 or len(value) > 200 or any(character not in "0123456789abcdefABCDEF" for character in value):
            raise ValueError("Direct devices require a native APNs token")
    else:
        raise ValueError("bighelp device provider must be managed or direct")


def _validate_device_routing(device_id: str, groups: list[str]) -> None:
    verdict = validate_target(f"device:{device_id}")
    if verdict is not True:
        raise ValueError(f"Invalid bighelp device ID: {verdict}")
    for group in groups:
        verdict = validate_target(f"group:{group}")
        if verdict is not True:
            raise ValueError(f"Invalid bighelp group ID: {verdict}")
