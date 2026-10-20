"""The bighelp gateway platform: scheduled-task and approval alerts for the legacy push setup.

The app talks to Hermes directly, so this platform carries no chat. Its sends reach
phones registered with the legacy push provider (`hermes bighelp configure-apns`).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from hermes_constants import get_hermes_home

from .events import EVENT_TYPES, build_event
from .generative_ui import GenerativeUIError, parse_v2_json, validate_rendered_envelope
from .service import BighelpService
from .store import BighelpStore
from .targets import parse_target, validate_target


_services: dict[str, BighelpService] = {}
_services_lock = threading.Lock()


def data_path() -> Path:
    return get_hermes_home() / "plugin-data" / "loopdy" / "loopdy.sqlite3"


def get_service() -> BighelpService:
    key = str(data_path().resolve())
    with _services_lock:
        service = _services.get(key)
        if service is None:
            service = BighelpService(BighelpStore(Path(key)))
            _services[key] = service
        return service


def release_service(service: BighelpService) -> None:
    service.close()
    with _services_lock:
        for key, cached in list(_services.items()):
            if cached is service:
                del _services[key]


class BighelpAdapter(BasePlatformAdapter):
    """Turns Hermes outbound messages into bighelp alerts for the legacy push provider."""

    supports_async_delivery = True
    interactive_resume = False
    # Own message sizing: card envelopes must reach validation intact, while
    # ordinary text is split below rather than cut by Hermes cron delivery.
    splits_long_messages = True

    def __init__(
        self,
        config: PlatformConfig,
        *,
        service: BighelpService | None = None,
        plugin_update_manager: Any | None = None,
    ):
        # Restart and shutdown pings are operator lifecycle signals, not alerts.
        config.gateway_restart_notification = False
        super().__init__(config=config, platform=Platform("loopdy"))
        self.service = service or get_service()
        self.plugin_update_manager = plugin_update_manager
        self.home_target = str((config.extra or {}).get("home_target") or "all").strip()

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if self.plugin_update_manager is not None:
            # Adapter connection is a real gateway lifecycle. Plugin Doctor and
            # CLI discovery register the plugin but never reach here.
            try:
                self.plugin_update_manager.record_runtime_loaded()
            except Exception:
                # Update status can fail closed without stopping alerts.
                pass
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        target = str(chat_id or self.home_target or "all").strip()
        event = _channel_event(content, metadata=metadata, target=target)
        # A validated card is atomic even when its JSON exceeds the text limit.
        if "generative_ui" not in event.detail and len(content) > 50_000:
            result = SendResult(success=True)
            for offset in range(0, len(content), 50_000):
                result = await self.send(
                    chat_id, content[offset:offset + 50_000],
                    reply_to=reply_to, metadata=metadata,
                )
                if not result.success:
                    return result
            return result
        result = await asyncio.to_thread(self.service.deliver, event, target=target)
        if result.get("success"):
            message_id = str(result.get("message_id") or event.event_id)
            return SendResult(success=True, message_id=message_id)
        return SendResult(
            success=False,
            error=str(result.get("error") or "bighelp delivery failed"),
        )

    async def get_chat_info(self, chat_id: str) -> dict[str, str]:
        return {"name": str(chat_id), "type": "notification"}


async def standalone_send(
    pconfig: PlatformConfig,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
    service: BighelpService | Any | None = None,
    adapter_factory: Callable[..., BighelpAdapter | Any] = BighelpAdapter,
) -> dict[str, Any]:
    if media_files:
        return {"error": "bighelp proactive notifications do not accept attachments"}
    del force_document
    target = str(
        chat_id
        or (getattr(pconfig, "extra", {}) or {}).get("home_target")
        or os.getenv("LOOPDY_HOME_TARGET", "all")
    ).strip()
    adapter = adapter_factory(pconfig, service=service or get_service())
    try:
        if not await adapter.connect():
            return {"error": "bighelp delivery is unavailable"}
        result = await adapter.send(
            target,
            message,
            metadata={"thread_id": thread_id} if thread_id else None,
        )
        if result.success:
            return {
                "success": True,
                "message_id": str(result.message_id or ""),
            }
        return {"error": str(result.error or "bighelp delivery failed")}
    finally:
        await adapter.disconnect()


def check_requirements() -> bool:
    return True


def validate_config(_config: PlatformConfig) -> bool:
    return bool(get_service().health().get("configured"))


def is_connected(config: PlatformConfig) -> bool:
    return bool(config.enabled and validate_config(config))


def env_enablement() -> dict[str, Any] | None:
    if not get_service().health().get("configured"):
        return None
    target = os.getenv("LOOPDY_HOME_TARGET", "all").strip() or "all"
    if validate_target(target) is not True:
        return None
    return {
        "home_target": target,
        "home_channel": {"chat_id": target, "name": "bighelp"},
    }


def _channel_event(
    content: str, *, metadata: Optional[Dict[str, Any]], target: str = "all"
) -> Any:
    values = metadata or {}
    requested_type = str(values.get("event_type") or "")
    job_id = _text(values.get("job_id"), 180)
    kind = requested_type if requested_type in EVENT_TYPES else "channel.message"
    profile = (
        _text(values.get("profile") or values.get("profile_name"), 80)
        or _active_profile_id()
    )
    agent_name = _text(values.get("agent_name") or values.get("sender_name"), 80)
    if not agent_name:
        agent_name = profile_display_name(profile)
    message = _text(content, 50_000)
    detail: dict[str, Any] = {
        "message": message,
        **({"agent_name": agent_name} if agent_name else {}),
    }
    correlation: tuple[str, ...] = ()
    card_content = _channel_card_content(content, job_id=job_id)
    if card_content is not None:
        try:
            card = validate_rendered_envelope(parse_v2_json(card_content))
            detail["message"] = _text(card.get("title"), 120) or "Agent update"
            detail["generative_ui"] = card
            # Standalone cron fallback drops job metadata. Use the complete
            # validated card instance, including creation time, so both paths
            # retain one Inbox identity without hiding later identical updates.
            correlation = (
                "card-instance-v1", profile, target,
                json.dumps(card, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
            )
        except (GenerativeUIError, TypeError, ValueError):
            pass
    return build_event(
        kind,
        correlation=correlation,
        profile=profile,
        session_id=_text(values.get("session_id"), 180),
        job_id=job_id,
        detail=detail,
    )


def _channel_card_content(content: Any, *, job_id: str) -> str | None:
    """Return only a complete renderer envelope from an official send boundary.

    Hermes platform adapters receive final text, not tool-result metadata. Cron
    normally wraps that final text with its stable response header/footer and
    supplies the matching ``job_id`` in adapter metadata. Unwrap only that
    authenticated-by-correlation shape; renderer validation remains the final
    authority and every other value falls back to ordinary text.
    """
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if stripped.startswith("{"):
        return stripped
    if not job_id or not content.startswith("Cronjob Response: "):
        return None
    boundary = f"\n(job_id: {job_id})\n-------------\n\n"
    prefix, found, remainder = content.partition(boundary)
    if not found or not prefix.startswith("Cronjob Response: "):
        return None
    payload, footer, _ = remainder.partition(
        "\n\nTo stop or manage this job, send me a new message "
    )
    if not footer:
        return None
    candidate = payload.strip()
    return candidate if candidate.startswith("{") else None


def _active_profile_id() -> str:
    home = get_hermes_home()
    return home.name if home.parent.name == "profiles" else "default"


def profile_display_name(profile: str) -> str:
    home = get_hermes_home()
    if home.parent.name == "profiles" and home.name != profile:
        root = home.parent.parent
        home = root if profile == "default" else root / "profiles" / profile
    elif home.parent.name != "profiles" and profile != "default":
        home = home / "profiles" / profile
    try:
        from .yaml_compat import profile_name, safe_load

        resolved = _text(profile_name(safe_load((home / "profile.yaml").read_text(encoding="utf-8"))), 80)
        if resolved:
            return resolved
    except Exception:
        pass
    return " ".join(
        part.capitalize()
        for part in profile.replace("-", "_").split("_")
        if part
    )


def _text(value: Any, maximum: int) -> str:
    return " ".join(value.split())[:maximum] if isinstance(value, str) else ""


__all__ = [
    "BighelpAdapter",
    "profile_display_name",
    "check_requirements",
    "env_enablement",
    "get_service",
    "is_connected",
    "parse_target",
    "release_service",
    "standalone_send",
    "validate_config",
    "validate_target",
]
