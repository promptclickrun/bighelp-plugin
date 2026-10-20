"""Scheduled-task names and results for the completion events on the dashboard /events routes."""

from __future__ import annotations

import asyncio
import re
from typing import Any


class CompletionEventError(RuntimeError):
    """A bounded, user-safe failure to read completion metadata."""


_AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CRON_SESSION = re.compile(r"^cron_(.+)_\d{8}_\d{6}$")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompletionEventError(f"{label} is invalid")
    return value


def _optional_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise CompletionEventError("Hermes text is invalid")
    normalized = value.strip()
    if not normalized or len(value.encode("utf-8")) > maximum:
        raise CompletionEventError("Hermes text is invalid")
    if any(ord(character) < 32 and character not in "\n\t\r" for character in value):
        raise CompletionEventError("Hermes text is invalid")
    return normalized


def _utf8_prefix(value: str, maximum: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return encoded[:maximum].decode("utf-8", errors="ignore").rstrip()


def _agent_id(value: Any) -> str:
    candidate = _text(value, 64)
    if not _AGENT_ID.fullmatch(candidate):
        raise CompletionEventError("Agent identifier is invalid")
    return candidate


def _coordinate(value: Any, maximum: int) -> str:
    candidate = _text(value, maximum)
    if any(character.isspace() for character in candidate):
        raise CompletionEventError("Hermes coordinate is invalid")
    return candidate


def _optional_coordinate(value: Any, maximum: int) -> str | None:
    if value is None or value == "":
        return None
    return _coordinate(value, maximum)


def _timestamp(value: Any) -> int:
    if isinstance(value, bool):
        raise CompletionEventError("Hermes timestamp is invalid")
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError as exc:
            raise CompletionEventError("Hermes timestamp is invalid") from exc
    if not isinstance(value, (int, float)) or value < 0 or value > 4_102_444_800:
        raise CompletionEventError("Hermes timestamp is invalid")
    return int(value)
_DASHBOARD_EVENT_LIMIT = 200
_COMPLETION_EVENT_TYPES = frozenset({"job.completed", "job.failed"})
_COMPLETION_SUMMARY_MAX_BYTES = 2_000


def _completion_catalog(value: Any, *, profile: str) -> dict[str, str]:
    if not isinstance(value, list) or len(value) > 500:
        raise CompletionEventError("Hermes scheduled task catalog is invalid")
    catalog: dict[str, str] = {}
    for item in value:
        source = _object(item, "Hermes scheduled task")
        item_profile = source.get("profile")
        if item_profile not in {None, ""} and _agent_id(item_profile) != profile:
            raise CompletionEventError("Hermes scheduled task profile is invalid")
        task_id = _optional_coordinate(source.get("id"), 180) or _coordinate(
            source.get("job_id"), 180
        )
        name = _text(source.get("name"), 240)
        if task_id in catalog:
            raise CompletionEventError("Hermes scheduled task identity is ambiguous")
        catalog[task_id] = name
    return catalog


def _cron_job_id(session_id: str) -> str:
    match = _CRON_SESSION.fullmatch(session_id)
    return match.group(1) if match else ""


def _final_assistant_result(value: dict[str, Any]) -> str:
    rows = value.get("messages")
    if not isinstance(rows, list) or not rows or len(rows) > 500:
        raise CompletionEventError("Hermes session history is invalid")
    final = _object(rows[-1], "Hermes final assistant result")
    if final.get("role") != "assistant":
        raise CompletionEventError("Hermes final assistant result is unavailable")
    display_content = final.get("display_content")
    if isinstance(display_content, str) and display_content != "":
        content = _text(display_content, 1_000_000)
    else:
        requires_display_projection = (
            final.get("display_kind") == "hidden"
            or final.get("_compressed_summary") is True
            or final.get("compacted") is True
        )
        if requires_display_projection:
            raise CompletionEventError("Hermes final assistant result is unavailable")
        content = _text(final.get("content"), 1_000_000)
    summary = _utf8_prefix(content, _COMPLETION_SUMMARY_MAX_BYTES)
    if not summary:
        raise CompletionEventError("Hermes final assistant result is unavailable")
    return summary


async def _cron_list(agent_id: str | None) -> list[dict[str, Any]]:
    from hermes_cli.web_routers.cron import _list_cron_jobs_sync

    return await asyncio.to_thread(_list_cron_jobs_sync, agent_id or "all")


async def _session_messages(stored_id: str, agent_id: str, *, include_compacted: bool = True) -> dict[str, Any]:
    from hermes_cli.web_routers.sessions import get_session_messages

    return await get_session_messages(
        stored_id,
        profile=agent_id,
        limit=500,
        offset=0,
        order="latest",
        include_compacted=include_compacted,
    )


async def _session_detail(session_id: str, agent_id: str) -> dict[str, Any]:
    from hermes_cli.web_routers.sessions import get_session_detail

    return await get_session_detail(session_id, profile=agent_id)


async def enrich_completion_events(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add each scheduled task's name and result, and never drop a saved event."""
    if not isinstance(rows, list) or len(rows) > _DASHBOARD_EVENT_LIMIT:
        raise CompletionEventError("bighelp event catalog is invalid")
    catalogs: dict[str, dict[str, str] | None] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in _COMPLETION_EVENT_TYPES:
            continue
        try:
            profile = _agent_id(row.get("profile"))
        except (TypeError, ValueError, CompletionEventError):
            continue
        if profile in catalogs:
            continue
        try:
            catalogs[profile] = _completion_catalog(
                await _cron_list(profile),
                profile=profile,
            )
        except Exception:
            catalogs[profile] = None

    enriched: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in _COMPLETION_EVENT_TYPES:
            enriched.append(row)
            continue
        try:
            profile = _agent_id(row.get("profile"))
            session_id = _coordinate(row.get("session_id"), 180)
            task_id = await _canonical_cron_job_id(
                session_id,
                profile,
            )
            catalog = catalogs.get(profile)
            if catalog is None or task_id not in catalog:
                enriched.append(row)
                continue
            history = _object(
                await _session_messages(
                    session_id,
                    profile,
                    include_compacted=True,
                ),
                "Hermes session history",
            )
            detail = dict(_optional_object(row.get("detail")))
            detail.update(
                {
                    "title": catalog[task_id],
                    "summary": _final_assistant_result(history),
                    "status": (
                        "completed"
                        if row.get("type") == "job.completed"
                        else "failed"
                    ),
                }
            )
            enriched.append(
                {
                    **row,
                    "job_id": task_id,
                    "task_id": task_id,
                    "detail": detail,
                }
            )
        except Exception:
            # The saved event stays useful even when the host's current
            # catalogs cannot prove richer completion metadata.
            enriched.append(row)
            continue
    return enriched


async def _canonical_cron_job_id(session_id: str, agent_id: str) -> str:
    """Resolve only cron-owned identity through verified compression edges."""
    direct = _cron_job_id(session_id)
    if direct:
        return direct

    current = session_id
    visited: set[str] = set()
    for _ in range(32):
        if current in visited:
            break
        visited.add(current)
        child = _object(
            await _session_detail(current, agent_id),
            "Hermes session detail",
        )
        if _coordinate(child.get("id"), 180) != current:
            break
        parent_id = _optional_coordinate(child.get("parent_session_id"), 180)
        if parent_id is None or parent_id in visited:
            break
        parent = _object(
            await _session_detail(parent_id, agent_id),
            "Hermes parent session detail",
        )
        child_source = _coordinate(child.get("source"), 80)
        parent_source = _coordinate(parent.get("source"), 80)
        child_model = _optional_object(child.get("model_config"))
        if (
            _coordinate(parent.get("id"), 180) != parent_id
            or child_source != "cron"
            or parent_source != child_source
            or any(
                child_model.get(marker) is not None
                for marker in ("_branched_from", "_delegate_from", "_reset_from")
            )
            or parent.get("end_reason") != "compression"
            or _timestamp(child.get("started_at"))
            < _timestamp(parent.get("ended_at"))
        ):
            break
        canonical = _cron_job_id(parent_id)
        if canonical:
            return canonical
        current = parent_id
    raise CompletionEventError("Hermes cron session identity is unavailable")
