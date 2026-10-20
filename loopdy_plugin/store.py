"""SQLite-backed bighelp device, event, and approval state."""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .events import BighelpEvent
from .loopdy_cards import canonical_json as canonical_card_json
from .loopdy_cards import validate_card_input


_MAX_SAFE_REVISION = 9_007_199_254_740_991
_MAX_TIMESTAMP = 9_999_999_999
_MAX_AUTOMATIC_ATTEMPTS = 5
_RETIRED_PROVIDERS = frozenset({"relay", "legacy_relay"})
_GATEWAY_LIFECYCLE_PREFIXES = (
    "♻️ Gateway online",
    "♻ Gateway online",
    "♻️ Gateway restarted",
    "♻ Gateway restarted",
    "⚠️ Gateway restarting",
    "⚠ Gateway restarting",
    "⚠️ Gateway shutting down",
    "⚠ Gateway shutting down",
)


class CardTemplateConflict(ValueError):
    """A template changed under the caller's expected version/content."""


class CardTemplateLimit(ValueError):
    """A bounded template catalog cannot represent all stored rows."""


class CardTemplateSaveRejected(ValueError):
    """An agent's template save was refused, with a code and details it can act on."""

    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.details = details


# Agents save their own templates; the app installs the rest. Only saved ones can be
# updated by an agent, so a save never overwrites a template someone installed.
CARD_TEMPLATE_ORIGIN_INSTALLED = "installed"
CARD_TEMPLATE_ORIGIN_SAVED = "saved"
SAVED_CARD_TEMPLATE_AUTHOR = "Your agent"
SAVED_CARD_TEMPLATE_LICENSE = "Private"


class BighelpStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    def upsert_device(
        self,
        *,
        device_id: str,
        endpoint_id: str,
        provider: str = "managed",
        token_environment: str = "production",
        label: str = "",
        groups: Iterable[str] = (),
        preferences: Mapping[str, Any] | None = None,
    ) -> None:
        now = int(time.time())
        normalized_groups = sorted({str(value).strip() for value in groups if str(value).strip()})
        normalized_provider = _device_provider(provider)
        normalized_environment = _text(token_environment, 32) or "production"
        normalized_preferences = None if preferences is None else dict(preferences)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO devices (
                    device_id, endpoint_id, provider, token_environment, label,
                    groups_json, preferences_json, created_at, updated_at, revoked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                ON CONFLICT(device_id) DO UPDATE SET
                    endpoint_id=excluded.endpoint_id,
                    provider=excluded.provider,
                    token_environment=excluded.token_environment,
                    label=excluded.label,
                    groups_json=excluded.groups_json,
                    preferences_json=CASE WHEN ? IS NULL THEN devices.preferences_json ELSE excluded.preferences_json END,
                    updated_at=excluded.updated_at,
                    revoked_at=NULL
                """,
                (
                    _identifier(device_id, "device_id"),
                    _required_text(endpoint_id, "endpoint_id", 512),
                    normalized_provider,
                    normalized_environment,
                    _text(label, 120),
                    _json(normalized_groups),
                    _json(normalized_preferences if normalized_preferences is not None else {}),
                    now,
                    now,
                    _json(normalized_preferences) if normalized_preferences is not None else None,
                ),
            )

    def update_preferences(self, device_id: str, preferences: Mapping[str, Any]) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT preferences_json FROM devices "
                "WHERE device_id=? AND revoked_at IS NULL",
                (_identifier(device_id, "device_id"),),
            ).fetchone()
            if row is None:
                return False
            current = _load_json(row["preferences_json"], {})
            merged = dict(current) if isinstance(current, dict) else {}
            merged.update(dict(preferences))
            cursor = connection.execute(
                "UPDATE devices SET preferences_json=?, updated_at=? "
                "WHERE device_id=? AND revoked_at IS NULL",
                (_json(merged), int(time.time()), _identifier(device_id, "device_id")),
            )
            return cursor.rowcount == 1

    def revoke_device(self, device_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE devices SET revoked_at=?, updated_at=? "
                "WHERE device_id=? AND revoked_at IS NULL",
                (
                    int(time.time()),
                    int(time.time()),
                    _identifier(device_id, "device_id"),
                ),
            )
            return cursor.rowcount == 1

    def list_devices(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT device_id, endpoint_id, provider, token_environment, label, "
                "groups_json, preferences_json, revoked_at, recipient_public_key, "
                "recipient_key_id, revision, lease_expires, sender_key_revision, "
                "acknowledged_sender_key_ids_json, relay_generation "
                "FROM devices ORDER BY device_id"
            ).fetchall()
        devices = [_device_row(row) for row in rows]
        for device in devices:
            # Startup deletes retired relay devices; any left are never delivered to.
            if device["provider"] in _RETIRED_PROVIDERS:
                device["revoked"] = True
        return devices

    def resolve_devices(self, target: str, provider: str | None = None) -> list[dict[str, Any]]:
        normalized_provider = None if provider is None else _device_provider(provider)
        value = str(target or "").strip()
        if value == "all":
            device_id = ""
            group = ""
        elif value.startswith("device:"):
            device_id = _identifier(value.split(":", 1)[1], "device_id")
            group = ""
        elif value.startswith("group:"):
            device_id = ""
            group = _identifier(value.split(":", 1)[1], "group_id")
        else:
            raise ValueError("bighelp target must be all, device:<id>, or group:<id>")
        devices = [
            item
            for item in self.list_devices()
            if not item["revoked"]
            and (normalized_provider is None or item["provider"] == normalized_provider)
        ]
        if device_id:
            return [item for item in devices if item["device_id"] == device_id]
        if group:
            return [item for item in devices if group in item["groups"]]
        return devices

    def install_card_template(
        self,
        *,
        profile: str,
        template: Mapping[str, Any],
    ) -> dict[str, Any]:
        owner = _identifier(profile, "profile")
        normalized = _card_template(template)
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT version, sha256, template_json FROM card_templates "
                "WHERE profile=? AND template_id=?",
                (owner, normalized["id"]),
            ).fetchone()
            if existing is not None:
                current_version = int(existing["version"])
                if normalized["version"] < current_version:
                    raise CardTemplateConflict("Card template version cannot decrease")
                if normalized["version"] == current_version:
                    if (
                        hmac.compare_digest(str(existing["sha256"]), normalized["sha256"])
                        and hmac.compare_digest(
                            str(existing["template_json"]),
                            canonical_card_json(normalized),
                        )
                    ):
                        return {"changed": False, "template": normalized}
                    raise CardTemplateConflict("Card template version conflict")
            connection.execute(
                """
                INSERT INTO card_templates (
                    profile, template_id, version, name, summary, sha256,
                    template_json, created_at, updated_at, origin, usage_guidance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
                ON CONFLICT(profile, template_id) DO UPDATE SET
                    version=excluded.version,
                    name=excluded.name,
                    summary=excluded.summary,
                    sha256=excluded.sha256,
                    template_json=excluded.template_json,
                    updated_at=excluded.updated_at,
                    origin=excluded.origin,
                    usage_guidance=excluded.usage_guidance
                """,
                (
                    owner,
                    normalized["id"],
                    normalized["version"],
                    normalized["name"],
                    normalized["summary"],
                    normalized["sha256"],
                    canonical_card_json(normalized),
                    now,
                    now,
                    CARD_TEMPLATE_ORIGIN_INSTALLED,
                ),
            )
        return {"changed": True, "template": normalized}

    def save_card_template(
        self,
        *,
        profile: str,
        name: str,
        summary: str,
        usage_guidance: str,
        parameters_schema: Mapping[str, Any],
        document: Mapping[str, Any],
        template_id: str | None = None,
        expected_version: int | None = None,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Create or explicitly update one agent-saved template in one profile.

        Creating never replaces anything: a matching name or layout is reported
        back so the agent can reuse or update it. Updating names the template and
        the version the agent last saw, and only touches agent-saved templates.
        """
        owner = _identifier(profile, "profile")
        guidance = _required_text(usage_guidance, "usage guidance", 1_000)
        name_key = _card_template_name_key(name)
        document_hash = hashlib.sha256(canonical_card_json(dict(document)).encode("utf-8")).hexdigest()
        schema_json = canonical_card_json(dict(parameters_schema))
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT template_id, version, name, sha256, template_json, origin, usage_guidance "
                "FROM card_templates WHERE profile=? ORDER BY template_id",
                (owner,),
            ).fetchall()
            by_id = {str(row["template_id"]): row for row in rows}

            def same_content(row: sqlite3.Row) -> bool:
                stored = json.loads(str(row["template_json"]))
                return (
                    str(row["origin"]) == CARD_TEMPLATE_ORIGIN_SAVED
                    and stored.get("name") == name.strip()
                    and stored.get("summary") == summary.strip()
                    and str(row["usage_guidance"]) == guidance
                    and str(row["sha256"]) == document_hash
                    and canonical_card_json(stored.get("parameters_schema")) == schema_json
                )

            def unchanged(row: sqlite3.Row) -> dict[str, Any]:
                return {
                    "status": "unchanged",
                    "template": json.loads(str(row["template_json"])),
                    "usage_guidance": guidance,
                }

            def name_taken(except_id: str | None) -> None:
                for row in rows:
                    if row["template_id"] != except_id and _card_template_name_key(row["name"]) == name_key:
                        raise CardTemplateSaveRejected(
                            "duplicate_name",
                            "Another template already has this name. Reuse it, update it with "
                            "template_id and expected_version, or choose a different name.",
                            existing_template_id=str(row["template_id"]),
                            existing_version=int(row["version"]),
                            existing_origin=str(row["origin"]),
                        )

            if template_id is None:
                for row in rows:
                    if _card_template_name_key(row["name"]) == name_key and same_content(row):
                        return unchanged(row)
                name_taken(None)
                for row in rows:
                    stored = json.loads(str(row["template_json"]))
                    if (
                        str(row["sha256"]) == document_hash
                        and canonical_card_json(stored.get("parameters_schema")) == schema_json
                    ):
                        raise CardTemplateSaveRejected(
                            "duplicate_layout",
                            "This layout and its parameters are already saved under another name. "
                            "Render that template, or update it with template_id and expected_version.",
                            existing_template_id=str(row["template_id"]),
                            existing_version=int(row["version"]),
                            existing_name=str(row["name"]),
                        )
                if len(rows) >= limit:
                    raise CardTemplateSaveRejected(
                        "catalog_full",
                        f"This agent already has {limit} card templates. Ask the user which to "
                        "remove in the app before saving another.",
                    )
                identifier = _saved_card_template_id(name, taken=set(by_id))
                version = 1
                status = "created"
                created_at = now
            else:
                identifier = _card_template_id(template_id)
                current = by_id.get(identifier)
                if current is None:
                    raise CardTemplateSaveRejected(
                        "template_not_found",
                        "No template with this id belongs to this agent. Search for it first, or "
                        "leave out template_id to save a new one.",
                    )
                if str(current["origin"]) != CARD_TEMPLATE_ORIGIN_SAVED:
                    raise CardTemplateSaveRejected(
                        "not_a_saved_template",
                        "This template was installed in the app, so it can't be changed here. "
                        "Save your version under a new name instead.",
                    )
                current_version = int(current["version"])
                if expected_version != current_version:
                    raise CardTemplateSaveRejected(
                        "version_conflict",
                        "The template changed since you read it. Get it again and retry with its "
                        "current version.",
                        current_version=current_version,
                    )
                name_taken(identifier)
                if same_content(current):
                    return unchanged(current)
                version = current_version + 1
                status = "updated"
                created_at = None
            template = _card_template({
                "id": identifier,
                "version": version,
                "name": name,
                "summary": summary,
                "author": SAVED_CARD_TEMPLATE_AUTHOR,
                "license": SAVED_CARD_TEMPLATE_LICENSE,
                "minimum_card_version": 1,
                "parameters_schema": dict(parameters_schema),
                "document": dict(document),
                "sha256": document_hash,
            })
            connection.execute(
                """
                INSERT INTO card_templates (
                    profile, template_id, version, name, summary, sha256,
                    template_json, created_at, updated_at, origin, usage_guidance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile, template_id) DO UPDATE SET
                    version=excluded.version,
                    name=excluded.name,
                    summary=excluded.summary,
                    sha256=excluded.sha256,
                    template_json=excluded.template_json,
                    updated_at=excluded.updated_at,
                    origin=excluded.origin,
                    usage_guidance=excluded.usage_guidance
                """,
                (
                    owner,
                    template["id"],
                    template["version"],
                    template["name"],
                    template["summary"],
                    template["sha256"],
                    canonical_card_json(template),
                    created_at if created_at is not None else now,
                    now,
                    CARD_TEMPLATE_ORIGIN_SAVED,
                    guidance,
                ),
            )
        return {"status": status, "template": template, "usage_guidance": guidance}

    def card_template_metadata(self, *, profile: str) -> dict[str, dict[str, str]]:
        """Where each of a profile's templates came from, and when to pick saved ones."""
        owner = _identifier(profile, "profile")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT template_id, origin, usage_guidance FROM card_templates WHERE profile=?",
                (owner,),
            ).fetchall()
        return {
            str(row["template_id"]): {
                "origin": str(row["origin"]),
                "usage_guidance": str(row["usage_guidance"]),
            }
            for row in rows
        }

    def list_card_templates(self, *, profile: str, limit: int | None = None) -> list[dict[str, Any]]:
        owner = _identifier(profile, "profile")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 500):
            raise ValueError("Card template catalog limit is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT template_json FROM card_templates WHERE profile=? "
                "ORDER BY name COLLATE NOCASE, template_id" + (" LIMIT ?" if limit is not None else ""),
                (owner, limit + 1) if limit is not None else (owner,),
            ).fetchall()
        if limit is not None and len(rows) > limit:
            raise CardTemplateLimit("Card template catalog exceeds the row limit")
        return [json.loads(str(row["template_json"])) for row in rows]

    def get_card_template(
        self,
        *,
        profile: str,
        template_id: str,
    ) -> dict[str, Any] | None:
        owner = _identifier(profile, "profile")
        identifier = _card_template_id(template_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT template_json FROM card_templates WHERE profile=? AND template_id=?",
                (owner, identifier),
            ).fetchone()
        return None if row is None else json.loads(str(row["template_json"]))

    def remove_card_template(
        self,
        *,
        profile: str,
        template_id: str,
        version: int,
        sha256: str,
    ) -> dict[str, Any]:
        owner = _identifier(profile, "profile")
        identifier = _card_template_id(template_id)
        expected_version = _positive_revision(version)
        expected_hash = _card_template_hash(sha256)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version, sha256 FROM card_templates WHERE profile=? AND template_id=?",
                (owner, identifier),
            ).fetchone()
            if row is None:
                return {"changed": False, "templateId": identifier}
            if int(row["version"]) != expected_version or not hmac.compare_digest(
                str(row["sha256"]), expected_hash
            ):
                raise CardTemplateConflict("Card template removal conflict")
            connection.execute(
                "DELETE FROM card_templates WHERE profile=? AND template_id=?",
                (owner, identifier),
            )
        return {"changed": True, "templateId": identifier}

    def provider_mode(self) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='provider_mode'"
            ).fetchone()
        return _provider_mode(row["value"] if row is not None else "managed")

    def set_provider_mode(self, mode: str) -> None:
        requested = str(mode or "").strip().lower()
        if requested not in {"managed", "direct"}:
            raise ValueError("bighelp provider mode must be managed or direct")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('provider_mode', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (requested,),
            )

    def retire_legacy_relay(self) -> bool:
        """Atomically remove persisted Cloudflare relay delivery state."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            mode = connection.execute(
                "SELECT value FROM metadata WHERE key='provider_mode'"
            ).fetchone()
            has_config = connection.execute(
                "SELECT 1 FROM metadata WHERE key='relay_config_v1'"
            ).fetchone() is not None
            has_devices = connection.execute(
                "SELECT 1 FROM devices WHERE provider IN ('relay', 'legacy_relay') LIMIT 1"
            ).fetchone() is not None
            has_activity = connection.execute(
                "SELECT 1 FROM relay_live_activities LIMIT 1"
            ).fetchone() is not None
            has_operations = connection.execute(
                "SELECT 1 FROM pending_relay_operations LIMIT 1"
            ).fetchone() is not None
            has_updates = connection.execute(
                "SELECT 1 FROM pending_relay_live_activity_updates LIMIT 1"
            ).fetchone() is not None
            has_deliveries = connection.execute(
                "SELECT 1 FROM event_deliveries WHERE provider='relay' LIMIT 1"
            ).fetchone() is not None
            if not any((has_config, has_devices, has_activity, has_operations, has_updates,
                        has_deliveries, mode is not None and str(mode["value"]) == "relay")):
                return False
            generation = self._metadata_integer(connection, "relay_config_generation", 0) + 1
            connection.execute("DELETE FROM event_deliveries WHERE provider='relay'")
            connection.execute("DELETE FROM provider_receipts WHERE provider='relay'")
            connection.execute("DELETE FROM pending_relay_live_activity_updates")
            connection.execute("DELETE FROM pending_relay_operations")
            connection.execute("DELETE FROM relay_live_activities")
            connection.execute("DELETE FROM devices WHERE provider IN ('relay', 'legacy_relay')")
            connection.execute("DELETE FROM metadata WHERE key='relay_config_v1'")
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('provider_mode', 'managed') "
                "ON CONFLICT(key) DO UPDATE SET value='managed'"
            )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('relay_config_generation', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(generation),),
            )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('relay_config_state', 'disabled') "
                "ON CONFLICT(key) DO UPDATE SET value='disabled'"
            )
            return True

    def save_apns_config(self, config: Mapping[str, Any]) -> None:
        allowed = {"team_id", "key_id", "topic", "environment", "key_path"}
        value = {key: str(config.get(key) or "").strip() for key in sorted(allowed)}
        if any(not value[key] for key in allowed):
            raise ValueError("APNs configuration is incomplete")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('apns_config', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_json(value),),
            )

    def load_apns_config(self) -> dict[str, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='apns_config'"
            ).fetchone()
        if row is None:
            return None
        value = _load_json(row["value"], None)
        if not isinstance(value, dict):
            return None
        return {str(key): str(item) for key, item in value.items()}

    def clear_apns_config(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM metadata WHERE key='apns_config'")

    def has_pending_live_activity_updates(self) -> bool:
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM pending_live_activity_updates WHERE terminal=0 LIMIT 1"
            ).fetchone() is not None

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        identifier = _identifier(device_id, "device_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT device_id, endpoint_id, provider, token_environment, label, "
                "groups_json, preferences_json, revoked_at, recipient_public_key, "
                "recipient_key_id, revision, lease_expires, sender_key_revision, "
                "acknowledged_sender_key_ids_json, relay_generation FROM devices WHERE device_id=?",
                (identifier,),
            ).fetchone()
        if row is None:
            return None
        result = _device_row(row)
        if result["provider"] in _RETIRED_PROVIDERS:
            result["revoked"] = True
        return result

    def upsert_live_activity(
        self,
        *,
        session_id: str,
        live_session_id: str,
        profile: str,
        activity_id: str,
        push_token: str,
        token_environment: str,
    ) -> None:
        environment = str(token_environment or "production").strip().lower()
        if environment not in {"production", "sandbox"}:
            raise ValueError("Live Activity environment must be production or sandbox")
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO live_activities (
                    activity_id, session_id, live_session_id, profile, push_token,
                    token_environment, created_at, updated_at, ended_at,
                    owner_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1)
                ON CONFLICT(activity_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    live_session_id=excluded.live_session_id,
                    profile=excluded.profile,
                    push_token=excluded.push_token,
                    token_environment=excluded.token_environment,
                    updated_at=excluded.updated_at,
                    owner_generation=live_activities.owner_generation + 1,
                    ended_at=NULL
                """,
                (
                    _identifier(activity_id, "activity_id"),
                    _identifier(session_id, "session_id"),
                    _identifier(live_session_id or session_id, "live_session_id"),
                    _required_text(profile or "default", "profile", 80),
                    _required_text(push_token, "push_token", 512),
                    environment,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM pending_live_activity_updates WHERE activity_id=?",
                (_identifier(activity_id, "activity_id"),),
            )

    def active_live_activities(self, session_id: str, profile: str = "") -> list[dict[str, Any]]:
        identifier = _identifier(session_id, "session_id")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM live_activities WHERE ended_at IS NULL "
                "AND (session_id=? OR live_session_id=?) "
                "AND (?='' OR profile=?) ORDER BY updated_at DESC",
                (identifier, identifier, str(profile or ""), str(profile or "")),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_live_activity(
        self,
        activity_id: str,
        *,
        expected_session_id: str = "",
        expected_live_session_id: str = "",
        expected_profile: str = "",
        expected_push_token: str = "",
    ) -> dict[str, Any] | None:
        identifier = _identifier(activity_id, "activity_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM live_activities WHERE activity_id=? AND ended_at IS NULL "
                "AND (?='' OR session_id=?) AND (?='' OR live_session_id=?) "
                "AND (?='' OR profile=?) AND (?='' OR push_token=?)",
                (
                    identifier,
                    expected_session_id,
                    expected_session_id,
                    expected_live_session_id,
                    expected_live_session_id,
                    expected_profile,
                    expected_profile,
                    expected_push_token,
                    expected_push_token,
                ),
            ).fetchone()
        return None if row is None else dict(row)

    @contextmanager
    def live_activity_send_lock(self, activity_id: str):
        identifier = _identifier(activity_id, "activity_id")
        lock_directory = self.path.parent / f".{self.path.name}.live-activity-locks"
        lock_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_name = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
        descriptor = os.open(lock_directory / lock_name, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _lock_file_descriptor(descriptor)
            yield
        finally:
            _unlock_file_descriptor(descriptor)
            os.close(descriptor)

    def allocate_live_activity_timestamp(
        self,
        activity_id: str,
        current_time: int,
        *,
        expected_session_id: str = "",
        expected_live_session_id: str = "",
        expected_profile: str = "",
        expected_push_token: str = "",
        expected_owner_generation: int = 0,
    ) -> int:
        identifier = _identifier(activity_id, "activity_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_push_timestamp FROM live_activities "
                "WHERE activity_id=? AND ended_at IS NULL "
                "AND (?='' OR session_id=?) AND (?='' OR live_session_id=?) "
                "AND (?='' OR profile=?) AND (?='' OR push_token=?) "
                "AND (?=0 OR owner_generation=?)",
                (
                    identifier,
                    expected_session_id,
                    expected_session_id,
                    expected_live_session_id,
                    expected_live_session_id,
                    expected_profile,
                    expected_profile,
                    expected_push_token,
                    expected_push_token,
                    max(0, int(expected_owner_generation)),
                    max(0, int(expected_owner_generation)),
                ),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown or ended Live Activity")
            timestamp = max(int(current_time), int(row["last_push_timestamp"]) + 1)
            connection.execute(
                "UPDATE live_activities SET last_push_timestamp=?, updated_at=? "
                "WHERE activity_id=? AND ended_at IS NULL "
                "AND (?='' OR session_id=?) AND (?='' OR live_session_id=?) "
                "AND (?='' OR profile=?) AND (?='' OR push_token=?) "
                "AND (?=0 OR owner_generation=?)",
                (
                    timestamp,
                    int(time.time()),
                    identifier,
                    expected_session_id,
                    expected_session_id,
                    expected_live_session_id,
                    expected_live_session_id,
                    expected_profile,
                    expected_profile,
                    expected_push_token,
                    expected_push_token,
                    max(0, int(expected_owner_generation)),
                    max(0, int(expected_owner_generation)),
                ),
            )
            return timestamp

    def end_live_activity(
        self,
        activity_id: str,
        *,
        expected_session_id: str = "",
        expected_live_session_id: str = "",
        expected_profile: str = "",
        expected_push_token: str = "",
        expected_owner_generation: int = 0,
        expected_request_id: str = "",
    ) -> bool:
        identifier = _identifier(activity_id, "activity_id")
        owner_generation = max(0, int(expected_owner_generation))
        request_id = str(expected_request_id or "")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if request_id:
                pending = connection.execute(
                    "SELECT 1 FROM pending_live_activity_updates "
                    "WHERE activity_id=? AND request_id=? "
                    "AND (?=0 OR owner_generation=?) LIMIT 1",
                    (identifier, _required_text(request_id, "expected_request_id", 512),
                     owner_generation, owner_generation),
                ).fetchone()
                if pending is None:
                    return False
            ended = connection.execute(
                "UPDATE live_activities SET ended_at=?, updated_at=? WHERE activity_id=? "
                "AND (?='' OR session_id=?) AND (?='' OR live_session_id=?) "
                "AND (?='' OR profile=?) AND (?='' OR push_token=?) "
                "AND (?=0 OR owner_generation=?)",
                (
                    int(time.time()),
                    int(time.time()),
                    identifier,
                    expected_session_id,
                    expected_session_id,
                    expected_live_session_id,
                    expected_live_session_id,
                    expected_profile,
                    expected_profile,
                    expected_push_token,
                    expected_push_token,
                    owner_generation,
                    owner_generation,
                ),
            )
            if ended.rowcount:
                connection.execute(
                    "DELETE FROM pending_live_activity_updates WHERE activity_id=? "
                    "AND (?='' OR owner_session_id=?) "
                    "AND (?='' OR owner_live_session_id=?) "
                    "AND (?='' OR owner_profile=?) "
                    "AND (?='' OR owner_push_token=?) "
                    "AND (?=0 OR owner_generation=?) "
                    "AND (?='' OR request_id=?)",
                    (
                        identifier,
                        expected_session_id, expected_session_id,
                        expected_live_session_id, expected_live_session_id,
                        expected_profile, expected_profile,
                        expected_push_token, expected_push_token,
                        owner_generation, owner_generation,
                        request_id, request_id,
                    ),
                )
            return ended.rowcount == 1

    def defer_live_activity_update(
        self,
        *,
        activity_id: str,
        status: str,
        detail: str,
        tool_name: str,
        active_session_count: int,
        delay_seconds: int,
        failure: str,
        expected_session_id: str = "",
        expected_live_session_id: str = "",
        expected_profile: str = "",
        expected_push_token: str = "",
        expected_owner_generation: int = 0,
        expected_request_id: str = "",
    ) -> bool:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute(
                "SELECT session_id, live_session_id, profile, push_token, owner_generation "
                "FROM live_activities WHERE activity_id=? AND ended_at IS NULL",
                (_identifier(activity_id, "activity_id"),),
            ).fetchone()
            if owner is None:
                raise ValueError("Unknown or ended Live Activity")
            owner_values = {
                "session_id": str(owner["session_id"]),
                "live_session_id": str(owner["live_session_id"]),
                "profile": str(owner["profile"]),
                "push_token": str(owner["push_token"]),
                "owner_generation": int(owner["owner_generation"] or 0),
            }
            expected_owner = max(0, int(expected_owner_generation))
            if (
                (expected_session_id and str(expected_session_id) != owner_values["session_id"])
                or (expected_live_session_id and str(expected_live_session_id) != owner_values["live_session_id"])
                or (expected_profile and str(expected_profile) != owner_values["profile"])
                or (expected_push_token and str(expected_push_token) != owner_values["push_token"])
                or (expected_owner and expected_owner != owner_values["owner_generation"])
            ):
                return False
            normalized_status = _required_text(status, "status", 40)
            existing = connection.execute(
                "SELECT status, terminal, attempts, owner_session_id, owner_live_session_id, "
                "owner_profile, owner_push_token, owner_generation, request_id "
                "FROM pending_live_activity_updates "
                "WHERE activity_id=?",
                (_identifier(activity_id, "activity_id"),),
            ).fetchone()
            if expected_request_id and (
                existing is None
                or str(existing["request_id"] or "") != str(expected_request_id)
                or int(existing["owner_generation"] or 0) != owner_values["owner_generation"]
            ):
                return False
            reset_attempts = False
            if existing is not None:
                previous_status = str(existing["status"])
                previous_terminal = bool(int(existing["terminal"] or 0)) or previous_status in {
                    "completed", "failed"
                }
                routine = {"thinking", "running"}
                if previous_terminal:
                    if previous_status in {"completed", "failed"} or normalized_status not in {
                        "waiting", "completed", "failed"
                    }:
                        # A non-exhausted terminal update may be retried with
                        # the same phase; a different terminal remains
                        # authoritative.  Exhausted terminal rows are never
                        # automatically retried.
                        if not (
                            previous_status == normalized_status
                            and not bool(int(existing["terminal"] or 0))
                        ):
                            return
                    else:
                        reset_attempts = True
                elif previous_status == "waiting" and normalized_status in routine:
                    return
                elif previous_status in routine and normalized_status in {
                    "waiting", "completed", "failed"
                }:
                    reset_attempts = True
                owner_changed = any(
                    str(existing[key] or "") != owner_values[value]
                    for key, value in (
                        ("owner_session_id", "session_id"),
                        ("owner_live_session_id", "live_session_id"),
                        ("owner_profile", "profile"),
                        ("owner_push_token", "push_token"),
                    )
                )
                reset_attempts = reset_attempts or owner_changed
            else:
                reset_attempts = True
            attempts = 1 if reset_attempts else int(existing["attempts"]) + 1
            terminal = int(attempts >= _MAX_AUTOMATIC_ATTEMPTS)
            request_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO pending_live_activity_updates (
                    activity_id, status, detail, tool_name, active_session_count,
                    attempts, next_attempt_at, last_error, updated_at, terminal,
                    owner_session_id, owner_live_session_id, owner_profile,
                    owner_push_token, owner_generation, request_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(activity_id) DO UPDATE SET
                    status=excluded.status,
                    detail=excluded.detail,
                    tool_name=excluded.tool_name,
                    active_session_count=excluded.active_session_count,
                    attempts=excluded.attempts,
                    next_attempt_at=excluded.next_attempt_at,
                    last_error=excluded.last_error,
                    updated_at=excluded.updated_at,
                    terminal=excluded.terminal,
                    owner_session_id=excluded.owner_session_id,
                    owner_live_session_id=excluded.owner_live_session_id,
                    owner_profile=excluded.owner_profile,
                    owner_push_token=excluded.owner_push_token,
                    owner_generation=excluded.owner_generation,
                    request_id=excluded.request_id
                """,
                (
                    _identifier(activity_id, "activity_id"),
                    normalized_status,
                    _text(detail, 180),
                    _text(tool_name, 80),
                    max(0, int(active_session_count)),
                    attempts,
                    now + max(0, int(delay_seconds)),
                    _text(failure, 500),
                    now,
                    terminal,
                    owner_values["session_id"],
                    owner_values["live_session_id"],
                    owner_values["profile"],
                    owner_values["push_token"],
                    owner_values["owner_generation"],
                    request_id,
                ),
            )
            return True

    def due_live_activity_updates(self, *, limit: int = 100) -> list[dict[str, Any]]:
        bounded = min(1000, max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT pending.*, activity.push_token, activity.token_environment
                FROM pending_live_activity_updates AS pending
                JOIN live_activities AS activity USING (activity_id)
                WHERE pending.terminal=0 AND pending.next_attempt_at <= ? AND activity.ended_at IS NULL
                ORDER BY pending.next_attempt_at, pending.updated_at
                LIMIT ?
                """,
                (int(time.time()), bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_pending_live_activity_update(
        self,
        activity_id: str,
        *,
        expected_session_id: str = "",
        expected_live_session_id: str = "",
        expected_profile: str = "",
        expected_push_token: str = "",
        expected_owner_generation: int = 0,
        expected_request_id: str = "",
    ) -> bool:
        clauses = ["activity_id=?"]
        values: list[Any] = [_identifier(activity_id, "activity_id")]
        for column, value, label in (
            ("owner_session_id", expected_session_id, "expected_session_id"),
            ("owner_live_session_id", expected_live_session_id, "expected_live_session_id"),
            ("owner_profile", expected_profile, "expected_profile"),
            ("owner_push_token", expected_push_token, "expected_push_token"),
            ("owner_generation", expected_owner_generation, "expected_owner_generation"),
            ("request_id", expected_request_id, "expected_request_id"),
        ):
            if value:
                clauses.append(f"{column}=?")
                values.append(_required_text(value, label, 512))
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM pending_live_activity_updates WHERE " + " AND ".join(clauses),
                tuple(values),
            )
            return cursor.rowcount == 1

    def pending_live_activity_update(self, activity_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_live_activity_updates WHERE activity_id=?",
                (_identifier(activity_id, "activity_id"),),
            ).fetchone()
        return dict(row) if row is not None else None

    def pending_live_activity_updates(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pending_live_activity_updates ORDER BY updated_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def record_device_delivery(
        self,
        *,
        event_id: str,
        device_id: str,
        provider: str,
        status: str,
        delivery_id: str = "",
        failure: str = "",
        target_revision: int = 0,
        target_generation: int = 0,
        target_key_id: str = "",
        target_sender_key_id: str = "",
        relay_request_body: Mapping[str, Any] | None = None,
    ) -> None:
        now = int(time.time())
        normalized_relay_body = "" if relay_request_body is None else _json(relay_request_body)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO event_deliveries (
                    event_id, device_id, provider, status, attempts, delivery_id,
                    failure, created_at, updated_at, target_revision, target_key_id,
                    target_generation, target_sender_key_id, relay_request_body_json
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id, device_id) DO UPDATE SET
                    provider=excluded.provider,
                    status=excluded.status,
                    attempts=event_deliveries.attempts + 1,
                    delivery_id=excluded.delivery_id,
                    failure=excluded.failure,
                    updated_at=excluded.updated_at,
                    target_generation=CASE
                        WHEN event_deliveries.target_generation != excluded.target_generation
                        THEN excluded.target_generation
                        WHEN event_deliveries.target_generation=0 THEN excluded.target_generation
                        ELSE event_deliveries.target_generation
                    END,
                    target_revision=CASE
                        WHEN event_deliveries.target_generation != excluded.target_generation
                          OR event_deliveries.target_revision != excluded.target_revision
                        THEN excluded.target_revision
                        WHEN event_deliveries.target_revision=0 THEN excluded.target_revision
                        ELSE event_deliveries.target_revision
                    END,
                    target_key_id=CASE
                        WHEN event_deliveries.target_generation != excluded.target_generation
                          OR event_deliveries.target_revision != excluded.target_revision
                        THEN excluded.target_key_id
                        WHEN event_deliveries.target_key_id='' THEN excluded.target_key_id
                        ELSE event_deliveries.target_key_id
                    END,
                    target_sender_key_id=CASE
                        WHEN event_deliveries.target_generation != excluded.target_generation
                          OR event_deliveries.target_revision != excluded.target_revision
                        THEN excluded.target_sender_key_id
                        WHEN event_deliveries.target_sender_key_id='' THEN excluded.target_sender_key_id
                        ELSE event_deliveries.target_sender_key_id
                    END,
                    relay_request_body_json=CASE
                        WHEN event_deliveries.target_generation != excluded.target_generation
                          OR event_deliveries.target_revision != excluded.target_revision
                        THEN excluded.relay_request_body_json
                        WHEN excluded.relay_request_body_json='' THEN event_deliveries.relay_request_body_json
                        ELSE excluded.relay_request_body_json
                    END,
                    next_attempt_at=0,
                    claim_token='',
                    claim_expires=0
                """,
                (
                    _identifier(event_id, "event_id"),
                    _identifier(device_id, "device_id"),
                    _device_provider(provider),
                    _delivery_status(status),
                    _text(delivery_id, 180),
                    _text(failure, 500),
                    now,
                    now,
                    max(0, int(target_revision)),
                    _text(target_key_id, 64),
                    max(0, int(target_generation)),
                    _text(target_sender_key_id, 64),
                    normalized_relay_body,
                ),
            )

    def list_event_deliveries(self, event_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM event_deliveries WHERE event_id=? ORDER BY device_id",
                (_identifier(event_id, "event_id"),),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_provider_receipt(
        self,
        *,
        receipt_id: str,
        event_id: str,
        device_id: str,
        provider: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO provider_receipts (
                    receipt_id, event_id, device_id, provider, status,
                    created_at, checked_at, error
                ) VALUES (?, ?, ?, ?, 'pending', ?, NULL, '')
                """,
                (
                    _identifier(receipt_id, "receipt_id"),
                    _identifier(event_id, "event_id"),
                    _identifier(device_id, "device_id"),
                    _device_provider(provider),
                    int(time.time()),
                ),
            )

    def pending_provider_receipts(
        self,
        provider: str,
        *,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        bounded = min(1000, max(1, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM provider_receipts "
                "WHERE provider=? AND status='pending' ORDER BY created_at LIMIT ?",
                (_device_provider(provider), bounded),
            ).fetchall()
        return [dict(row) for row in rows]

    def complete_provider_receipt(
        self,
        receipt_id: str,
        *,
        status: str,
        error: str = "",
    ) -> bool:
        normalized = str(status or "").strip().lower()
        if normalized not in {"delivered", "failed"}:
            raise ValueError("Provider receipt status must be delivered or failed")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE provider_receipts SET status=?, checked_at=?, error=? "
                "WHERE receipt_id=? AND status='pending'",
                (
                    normalized,
                    int(time.time()),
                    _text(error, 500),
                    _identifier(receipt_id, "receipt_id"),
                ),
            )
            return cursor.rowcount == 1

    def create_approval(
        self,
        *,
        approval_id: str,
        request_digest: str,
        allowed_choices: Iterable[str],
        event_id: str,
        expires_at: int,
    ) -> None:
        choices = [str(choice) for choice in allowed_choices]
        if (
            not choices
            or len(choices) > 4
            or len(set(choices)) != len(choices)
            or not set(choices).issubset({"once", "session", "always", "deny"})
        ):
            raise ValueError("bighelp approval choices are invalid")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    approval_id, request_digest, allowed_choices_json, event_id,
                    status, choice, expires_at, created_at, responded_at
                ) VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?, NULL)
                ON CONFLICT(approval_id) DO NOTHING
                """,
                (
                    _identifier(approval_id, "approval_id"),
                    _identifier(request_digest, "request_digest"),
                    _json(choices),
                    _identifier(event_id, "event_id"),
                    int(expires_at),
                    int(time.time()),
                ),
            )

    def respond_approval(self, approval_id: str, choice: str) -> bool:
        normalized = str(choice or "").strip().lower()
        if normalized not in {"once", "session", "always", "deny"}:
            raise ValueError("bighelp approval response is invalid")
        now = int(time.time())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT allowed_choices_json FROM approvals "
                "WHERE approval_id=? AND status='pending' AND expires_at>?",
                (str(approval_id), now),
            ).fetchone()
            if row is None:
                return False
            allowed = _load_json(row["allowed_choices_json"], [])
            if normalized not in allowed:
                raise ValueError("Approval choice was not offered by Hermes")
            cursor = connection.execute(
                "UPDATE approvals SET status='responded', choice=?, responded_at=? "
                "WHERE approval_id=? AND status='pending' AND expires_at>?",
                (normalized, now, str(approval_id), now),
            )
            return cursor.rowcount == 1

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self._connect() as connection:
            connection.execute(
                "UPDATE approvals SET status='expired' "
                "WHERE approval_id=? AND status='pending' AND expires_at<=?",
                (str(approval_id), now),
            )
            row = connection.execute(
                "SELECT * FROM approvals WHERE approval_id=?", (str(approval_id),)
            ).fetchone()
        if row is None:
            return None
        return {
            "approval_id": row["approval_id"],
            "request_digest": row["request_digest"],
            "allowed_choices": _load_json(row["allowed_choices_json"], []),
            "event_id": row["event_id"],
            "status": row["status"],
            "choice": row["choice"],
            "expires_at": row["expires_at"],
        }

    def create_form_request(
        self,
        *,
        request_id: str,
        profile: str,
        session_id: str,
        form_schema: Mapping[str, Any],
        content_hash: str,
        created_at: int,
        expires_at: int,
    ) -> None:
        request = _form_request_id(request_id)
        owner_profile = _required_text(profile, "profile", 80)
        owner_session = _required_text(session_id, "session_id", 180)
        schema_json = _json(dict(form_schema))
        if len(schema_json.encode("utf-8")) > 32_768:
            raise ValueError("Form schema exceeds the byte limit")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO generative_ui_forms (
                    request_id, profile, session_id, form_schema_json, content_hash,
                    state, idempotency_key, request_digest, values_json,
                    response_json, created_at, expires_at, submitted_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL, NULL, NULL, ?, ?, NULL, NULL)
                ON CONFLICT(request_id) DO NOTHING
                """,
                (
                    request,
                    owner_profile,
                    owner_session,
                    schema_json,
                    _content_hash(content_hash),
                    int(created_at),
                    int(expires_at),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Form request ID collision")

    def get_form_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM generative_ui_forms WHERE request_id=?",
                (_form_request_id(request_id),),
            ).fetchone()
        return _form_row(row) if row is not None else None

    def submit_form_request(
        self,
        *,
        request_id: str,
        profile: str,
        session_id: str,
        idempotency_key: str,
        values: Mapping[str, Any],
        now: int | None = None,
    ) -> dict[str, Any]:
        request = _form_request_id(request_id)
        key = _idempotency_key(idempotency_key)
        owner_profile = _required_text(profile, "profile", 80)
        owner_session = _required_text(session_id, "session_id", 180)
        values_json = _json(dict(values))
        if len(values_json.encode("utf-8")) > 8_192:
            return _form_response(request, key, "error", "payload_too_large")
        digest = hashlib.sha256(
            _json(
                {
                    "kind": "submit_form",
                    "owner": {"profile": owner_profile, "session_id": owner_session},
                    "request_id": request,
                    "values": dict(values),
                }
            ).encode("utf-8")
        ).hexdigest()
        current = int(time.time()) if now is None else int(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM generative_ui_forms WHERE request_id=?", (request,)
            ).fetchone()
            if row is None:
                return _form_response(request, key, "error", "request_not_found")
            if not _same_owner(row, owner_profile, owner_session):
                return _form_response(request, key, "error", "owner_mismatch")
            if row["state"] == "pending" and int(row["expires_at"]) <= current:
                connection.execute(
                    "UPDATE generative_ui_forms SET state='expired' WHERE request_id=? AND state='pending'",
                    (request,),
                )
                return _form_response(request, key, "error", "request_expired")
            stored_key = str(row["idempotency_key"] or "")
            stored_digest = str(row["request_digest"] or "")
            if stored_key and hmac.compare_digest(stored_key, key):
                if hmac.compare_digest(stored_digest, digest):
                    replay = _load_json(row["response_json"], None)
                    return replay if isinstance(replay, dict) else _form_response(request, key, "success", "accepted")
                return _form_response(request, key, "error", "idempotency_conflict")
            if row["state"] == "submitted":
                return _form_response(request, key, "error", "already_submitted")
            if row["state"] == "consumed":
                return _form_response(request, key, "error", "already_consumed")
            if row["state"] == "expired":
                return _form_response(request, key, "error", "request_expired")
            response = _form_response(request, key, "success", "accepted")
            cursor = connection.execute(
                """
                UPDATE generative_ui_forms
                SET state='submitted', idempotency_key=?, request_digest=?, values_json=?,
                    response_json=?, submitted_at=?
                WHERE request_id=? AND state='pending' AND expires_at>?
                """,
                (key, digest, values_json, _json(response), current, request, current),
            )
            if cursor.rowcount != 1:
                return _form_response(request, key, "error", "already_submitted")
            return response

    def consume_form_response(
        self,
        *,
        request_id: str,
        profile: str,
        session_id: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        request = _form_request_id(request_id)
        owner_profile = _required_text(profile, "profile", 80)
        owner_session = _required_text(session_id, "session_id", 180)
        current = int(time.time()) if now is None else int(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM generative_ui_forms WHERE request_id=?", (request,)
            ).fetchone()
            if row is None:
                return _form_response(request, "", "error", "request_not_found")
            if not _same_owner(row, owner_profile, owner_session):
                return _form_response(request, "", "error", "owner_mismatch")
            if row["state"] == "pending" and int(row["expires_at"]) <= current:
                connection.execute(
                    "UPDATE generative_ui_forms SET state='expired' WHERE request_id=? AND state='pending'",
                    (request,),
                )
                return _form_response(request, "", "error", "request_expired")
            if row["state"] == "pending":
                return _form_response(request, "", "pending", "accepted")
            if row["state"] == "expired":
                return _form_response(request, "", "error", "request_expired")
            if row["state"] == "consumed":
                return _form_response(request, str(row["idempotency_key"] or ""), "error", "already_consumed")
            values = _load_json(row["values_json"], None)
            if not isinstance(values, dict):
                return _form_response(request, str(row["idempotency_key"] or ""), "error", "internal_error")
            cursor = connection.execute(
                """
                UPDATE generative_ui_forms
                SET state='consumed', values_json=NULL, consumed_at=?
                WHERE request_id=? AND state='submitted'
                """,
                (current, request),
            )
            if cursor.rowcount != 1:
                return _form_response(request, str(row["idempotency_key"] or ""), "error", "already_consumed")
            return {
                **_form_response(request, str(row["idempotency_key"] or ""), "success", "accepted"),
                "values": values,
            }

    def form_request_status(
        self,
        request_id: str,
        *,
        profile: str,
        session_id: str,
        now: int | None = None,
    ) -> dict[str, Any]:
        request = _form_request_id(request_id)
        current = int(time.time()) if now is None else int(now)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM generative_ui_forms WHERE request_id=?", (request,)
            ).fetchone()
            if row is None:
                return _form_response(request, "", "error", "request_not_found")
            if not _same_owner(row, str(profile), str(session_id)):
                return _form_response(request, "", "error", "owner_mismatch")
            if row["state"] == "pending" and int(row["expires_at"]) <= current:
                connection.execute(
                    "UPDATE generative_ui_forms SET state='expired' WHERE request_id=? AND state='pending'",
                    (request,),
                )
                return _form_response(request, "", "error", "request_expired")
            code = {
                "pending": "accepted",
                "submitted": "accepted",
                "consumed": "already_consumed",
                "expired": "request_expired",
            }[row["state"]]
            state = "pending" if row["state"] == "pending" else ("success" if row["state"] == "submitted" else "error")
            return _form_response(request, str(row["idempotency_key"] or ""), state, code)

    def record_event(self, event: BighelpEvent, *, target: str = "all") -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_id, type, status, target, profile, session_id, job_id, task_id,
                    approval_id, delegation_id, detail_json, push_json, created_at
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.type,
                    target,
                    event.profile,
                    event.session_id,
                    event.job_id,
                    event.task_id,
                    event.approval_id,
                    event.delegation_id,
                    _json(dict(event.detail)),
                    _json(event.push_payload),
                    int(time.time()),
                ),
            )
            return cursor.rowcount == 1

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE event_id=?", (str(event_id),)
            ).fetchone()
        return _event_row(row) if row is not None else None

    def mark_event_delivered(self, event_id: str, delivery_id: str = "") -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE events SET status='sent', delivered_at=?, delivery_id=?, failure=NULL "
                "WHERE event_id=?",
                (int(time.time()), _text(delivery_id, 180), str(event_id)),
            )
            return cursor.rowcount == 1

    def mark_event_failed(self, event_id: str, failure: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE events SET status='failed', failure=? WHERE event_id=?",
                (_text(failure, 500), str(event_id)),
            )
            return cursor.rowcount == 1

    def list_events(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        bounded = min(200, max(1, int(limit)))
        bounded_offset = max(0, int(offset))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE dismissed_at IS NULL "
                "ORDER BY (pinned_at IS NOT NULL) DESC, pinned_at DESC, "
                "created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (bounded, bounded_offset),
            ).fetchall()
        return [_event_row(row) for row in rows]

    def set_event_state(
        self,
        event_id: str,
        *,
        is_read: bool,
        is_pinned: bool,
        changed_at: int | None = None,
    ) -> bool:
        changed = int(time.time()) if changed_at is None else int(changed_at)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE events SET "
                "read_at=CASE WHEN ? THEN COALESCE(read_at, ?) ELSE NULL END, "
                "pinned_at=CASE WHEN ? THEN COALESCE(pinned_at, ?) ELSE NULL END "
                "WHERE event_id=? AND dismissed_at IS NULL",
                (bool(is_read), changed, bool(is_pinned), changed, str(event_id)),
            )
            return cursor.rowcount == 1

    def dismiss_event(self, event_id: str, *, dismissed_at: int | None = None) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE events SET dismissed_at=COALESCE(dismissed_at, ?) WHERE event_id=?",
                (
                    int(time.time()) if dismissed_at is None else int(dismissed_at),
                    str(event_id),
                ),
            )
            return cursor.rowcount == 1

    def dismiss_events(
        self,
        *,
        event_ids: Iterable[str] = (),
        event_types: Iterable[str] = (),
        created_before: int | None = None,
        dismissed_at: int | None = None,
    ) -> int:
        ids = tuple(dict.fromkeys(_text(value, 220) for value in event_ids if _text(value, 220)))
        types = tuple(dict.fromkeys(_text(value, 80) for value in event_types if _text(value, 80)))
        if bool(ids) == bool(types):
            raise ValueError("Choose event_ids or event_types")
        values: list[Any] = [
            int(time.time()) if dismissed_at is None else int(dismissed_at)
        ]
        if ids:
            selector = "event_id IN ({})".format(",".join("?" for _ in ids))
            values.extend(ids)
        else:
            selector = "type IN ({})".format(",".join("?" for _ in types))
            values.extend(types)
        cutoff = ""
        if created_before is not None:
            cutoff = " AND created_at<=?"
            values.append(int(created_before))
        with self._connect() as connection:
            cursor = connection.execute(
                f"UPDATE events SET dismissed_at=? WHERE dismissed_at IS NULL "
                f"AND {selector}{cutoff}",
                tuple(values),
            )
            return cursor.rowcount

    def dismiss_attention_request(
        self,
        request_id: str,
        *,
        session_id: str = "",
        dismissed_at: int | None = None,
    ) -> int:
        request = _text(request_id, 180)
        session = _text(session_id, 180)
        if not request:
            return 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, detail_json FROM events WHERE type='attention.required' "
                "AND dismissed_at IS NULL AND (?='' OR session_id=?)",
                (session, session),
            ).fetchall()
            event_ids = [
                str(row["event_id"])
                for row in rows
                if _text(_load_json(row["detail_json"], {}).get("request_id"), 180)
                == request
            ]
            return self._dismiss_event_ids(
                connection,
                event_ids,
                dismissed_at=dismissed_at,
            )

    def dismiss_attention_for_session(
        self,
        session_id: str,
        *,
        dismissed_at: int | None = None,
    ) -> int:
        session = _text(session_id, 180)
        if not session:
            return 0
        dismissed = int(time.time()) if dismissed_at is None else int(dismissed_at)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, detail_json FROM events "
                "WHERE type='attention.required' AND dismissed_at IS NULL "
                "AND session_id=?",
                (session,),
            ).fetchall()
            event_ids = []
            for row in rows:
                detail = _load_json(row["detail_json"], {})
                expiry = _text(detail.get("expires_at"), 64)
                is_expired_clarify = (
                    _text(detail.get("kind"), 80) == "clarify"
                    and expiry.isdigit()
                    and int(expiry) <= dismissed
                )
                if not is_expired_clarify:
                    event_ids.append(str(row["event_id"]))
            return self._dismiss_event_ids(
                connection,
                event_ids,
                dismissed_at=dismissed,
            )

    def dismiss_expired_attention(
        self,
        created_before: int,
        *,
        dismissed_at: int | None = None,
    ) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, detail_json FROM events "
                "WHERE type='attention.required' AND dismissed_at IS NULL "
                "AND created_at<=?",
                (int(created_before),),
            ).fetchall()
            event_ids = [
                str(row["event_id"])
                for row in rows
                if _text(_load_json(row["detail_json"], {}).get("kind"), 80)
                != "clarify"
            ]
            return self._dismiss_event_ids(
                connection,
                event_ids,
                dismissed_at=dismissed_at,
            )

    def dismiss_inactive_approval_events(
        self,
        *,
        now: int | None = None,
        dismissed_at: int | None = None,
    ) -> int:
        current = int(time.time()) if now is None else int(now)
        dismissed = current if dismissed_at is None else int(dismissed_at)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE events SET dismissed_at=? WHERE type='approval.required' "
                "AND dismissed_at IS NULL AND approval_id<>'' AND EXISTS ("
                "SELECT 1 FROM approvals WHERE approvals.approval_id=events.approval_id "
                "AND (approvals.status<>'pending' OR approvals.expires_at<=?))",
                (dismissed, current),
            )
            return cursor.rowcount

    def dismiss_gateway_lifecycle_events(
        self,
        *,
        dismissed_at: int | None = None,
    ) -> int:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, detail_json FROM events WHERE type='channel.message' "
                "AND dismissed_at IS NULL"
            ).fetchall()
            event_ids = []
            for row in rows:
                detail = _load_json(row["detail_json"], {})
                message = str(detail.get("message") or "").strip()
                if any(message.startswith(prefix) for prefix in _GATEWAY_LIFECYCLE_PREFIXES):
                    event_ids.append(str(row["event_id"]))
            return self._dismiss_event_ids(
                connection,
                event_ids,
                dismissed_at=dismissed_at,
            )

    @staticmethod
    def _dismiss_event_ids(
        connection: sqlite3.Connection,
        event_ids: Iterable[str],
        *,
        dismissed_at: int | None = None,
    ) -> int:
        ids = tuple(dict.fromkeys(str(event_id) for event_id in event_ids if event_id))
        if not ids:
            return 0
        cursor = connection.execute(
            "UPDATE events SET dismissed_at=? WHERE dismissed_at IS NULL AND event_id IN ({})".format(
                ",".join("?" for _ in ids)
            ),
            (
                int(time.time()) if dismissed_at is None else int(dismissed_at),
                *ids,
            ),
        )
        return cursor.rowcount

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._connect(initialize=False) as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS turn_durations (
                        session_id TEXT NOT NULL,
                        turn_id TEXT NOT NULL,
                        final_timestamp REAL NOT NULL,
                        duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
                        PRIMARY KEY (session_id, turn_id)
                    );
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS devices (
                        device_id TEXT PRIMARY KEY,
                        endpoint_id TEXT NOT NULL UNIQUE,
                        provider TEXT NOT NULL DEFAULT 'managed',
                        token_environment TEXT NOT NULL DEFAULT 'production',
                        label TEXT NOT NULL,
                        groups_json TEXT NOT NULL,
                        preferences_json TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        revoked_at INTEGER,
                        recipient_public_key TEXT NOT NULL DEFAULT '',
                        recipient_key_id TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 0,
                        lease_expires INTEGER NOT NULL DEFAULT 0,
                        normalized_body_digest TEXT NOT NULL DEFAULT '',
                        sender_key_revision INTEGER NOT NULL DEFAULT 0,
                        acknowledged_sender_key_ids_json TEXT NOT NULL DEFAULT '[]',
                        sender_ack_body_digest TEXT NOT NULL DEFAULT '',
                        relay_generation INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        event_id TEXT PRIMARY KEY,
                        type TEXT NOT NULL,
                        status TEXT NOT NULL,
                        target TEXT NOT NULL,
                        profile TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        approval_id TEXT NOT NULL,
                        delegation_id TEXT NOT NULL,
                        detail_json TEXT NOT NULL,
                        push_json TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        delivered_at INTEGER,
                        delivery_id TEXT,
                        failure TEXT,
                        dismissed_at INTEGER,
                        read_at INTEGER,
                        pinned_at INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS approvals (
                        approval_id TEXT PRIMARY KEY,
                        request_digest TEXT NOT NULL,
                        allowed_choices_json TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        choice TEXT,
                        expires_at INTEGER NOT NULL,
                        created_at INTEGER NOT NULL,
                        responded_at INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS event_deliveries (
                        event_id TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempts INTEGER NOT NULL,
                        delivery_id TEXT NOT NULL,
                        failure TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        target_revision INTEGER NOT NULL DEFAULT 0,
                        target_generation INTEGER NOT NULL DEFAULT 0,
                        target_key_id TEXT NOT NULL DEFAULT '',
                        target_sender_key_id TEXT NOT NULL DEFAULT '',
                        relay_request_body_json TEXT NOT NULL DEFAULT '',
                        next_attempt_at INTEGER NOT NULL DEFAULT 0,
                        claim_token TEXT NOT NULL DEFAULT '',
                        claim_expires INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (event_id, device_id)
                    );
                    CREATE TABLE IF NOT EXISTS provider_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        event_id TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        checked_at INTEGER,
                        error TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS live_activities (
                        activity_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        live_session_id TEXT NOT NULL,
                        profile TEXT NOT NULL,
                        push_token TEXT NOT NULL,
                        token_environment TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        last_push_timestamp INTEGER NOT NULL DEFAULT 0,
                        owner_generation INTEGER NOT NULL DEFAULT 0,
                        ended_at INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS live_activities_session_idx
                    ON live_activities(live_session_id, session_id, ended_at);
                    CREATE TABLE IF NOT EXISTS pending_live_activity_updates (
                        activity_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        active_session_count INTEGER NOT NULL,
                        attempts INTEGER NOT NULL,
                        next_attempt_at INTEGER NOT NULL,
                        last_error TEXT NOT NULL,
                        updated_at INTEGER NOT NULL,
                        terminal INTEGER NOT NULL DEFAULT 0,
                        owner_session_id TEXT NOT NULL DEFAULT '',
                        owner_live_session_id TEXT NOT NULL DEFAULT '',
                        owner_profile TEXT NOT NULL DEFAULT '',
                        owner_push_token TEXT NOT NULL DEFAULT '',
                        owner_generation INTEGER NOT NULL DEFAULT 0,
                        request_id TEXT NOT NULL DEFAULT ''
                    );
                    CREATE INDEX IF NOT EXISTS pending_live_activity_updates_due_idx
                    ON pending_live_activity_updates(next_attempt_at);
                    CREATE TABLE IF NOT EXISTS pending_relay_live_activity_updates (
                        activity_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        active_session_count INTEGER NOT NULL,
                        attempts INTEGER NOT NULL,
                        next_attempt_at INTEGER NOT NULL,
                        last_error TEXT NOT NULL,
                        updated_at INTEGER NOT NULL,
                        timestamp INTEGER NOT NULL DEFAULT 0,
                        delivery_id TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT NOT NULL DEFAULT '',
                        request_body_json TEXT NOT NULL DEFAULT '',
                        device_id TEXT NOT NULL DEFAULT '',
                        session_ref TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL DEFAULT 0,
                        lease_expires INTEGER NOT NULL DEFAULT 0,
                        relay_generation INTEGER NOT NULL DEFAULT 0,
                        terminal INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE INDEX IF NOT EXISTS pending_relay_live_activity_updates_due_idx
                    ON pending_relay_live_activity_updates(next_attempt_at);
                    CREATE TABLE IF NOT EXISTS pending_relay_operations (
                        operation TEXT NOT NULL,
                        device_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        body_json TEXT NOT NULL,
                        response_json TEXT NOT NULL DEFAULT '',
                        relay_generation INTEGER NOT NULL DEFAULT 0,
                        request_digest TEXT NOT NULL DEFAULT '',
                        attempts INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        next_attempt_at INTEGER NOT NULL DEFAULT 0,
                        claim_token TEXT NOT NULL DEFAULT '',
                        claim_expires INTEGER NOT NULL DEFAULT 0,
                        terminal INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT NOT NULL DEFAULT '',
                        row_version INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY (operation, device_id)
                    );
                    CREATE TABLE IF NOT EXISTS relay_live_activities (
                        activity_id TEXT PRIMARY KEY,
                        device_id TEXT NOT NULL,
                        session_ref TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        source_timestamp INTEGER NOT NULL,
                        lease_expires INTEGER NOT NULL,
                        normalized_body_digest TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        last_push_timestamp INTEGER NOT NULL DEFAULT 0,
                        ended_at INTEGER,
                        revoked_at INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS generative_ui_forms (
                        request_id TEXT PRIMARY KEY,
                        profile TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        form_schema_json TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        state TEXT NOT NULL,
                        idempotency_key TEXT,
                        request_digest TEXT,
                        values_json TEXT,
                        response_json TEXT,
                        created_at INTEGER NOT NULL,
                        expires_at INTEGER NOT NULL,
                        submitted_at INTEGER,
                        consumed_at INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS generative_ui_forms_owner_idx
                    ON generative_ui_forms(profile, session_id, state, expires_at);
                    CREATE TABLE IF NOT EXISTS card_templates (
                        profile TEXT NOT NULL,
                        template_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        template_json TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        origin TEXT NOT NULL DEFAULT 'installed',
                        usage_guidance TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (profile, template_id)
                    );
                    CREATE INDEX IF NOT EXISTS card_templates_profile_name_idx
                    ON card_templates(profile, name, template_id);
                    CREATE TABLE IF NOT EXISTS marketplace_skill_installs (
                        profile TEXT NOT NULL,
                        item_id TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        sha256 TEXT NOT NULL,
                        skill_name TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        installed_content_sha256 TEXT,
                        verification_mode TEXT NOT NULL DEFAULT 'legacy_exact',
                        files_json TEXT NOT NULL,
                        request_id TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        updated_at INTEGER NOT NULL,
                        PRIMARY KEY (profile, item_id),
                        UNIQUE (profile, skill_name)
                    );
                    """
                )
                card_template_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(card_templates)").fetchall()
                }
                if "origin" not in card_template_columns:
                    connection.execute(
                        "ALTER TABLE card_templates ADD COLUMN origin TEXT NOT NULL "
                        "DEFAULT 'installed'"
                    )
                if "usage_guidance" not in card_template_columns:
                    connection.execute(
                        "ALTER TABLE card_templates ADD COLUMN usage_guidance TEXT NOT NULL "
                        "DEFAULT ''"
                    )
                marketplace_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(marketplace_skill_installs)"
                    ).fetchall()
                }
                if "installed_content_sha256" not in marketplace_columns:
                    connection.execute(
                        "ALTER TABLE marketplace_skill_installs "
                        "ADD COLUMN installed_content_sha256 TEXT"
                    )
                if "verification_mode" not in marketplace_columns:
                    connection.execute(
                        "ALTER TABLE marketplace_skill_installs ADD COLUMN verification_mode "
                        "TEXT NOT NULL DEFAULT 'legacy_exact'"
                    )
                event_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(events)").fetchall()
                }
                if "task_id" not in event_columns:
                    connection.execute(
                        "ALTER TABLE events ADD COLUMN task_id TEXT NOT NULL DEFAULT ''"
                    )
                if "dismissed_at" not in event_columns:
                    connection.execute(
                        "ALTER TABLE events ADD COLUMN dismissed_at INTEGER"
                    )
                if "read_at" not in event_columns:
                    connection.execute("ALTER TABLE events ADD COLUMN read_at INTEGER")
                if "pinned_at" not in event_columns:
                    connection.execute("ALTER TABLE events ADD COLUMN pinned_at INTEGER")
                device_columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(devices)").fetchall()
                }
                migrated_legacy_devices = "provider" not in device_columns
                if "provider" not in device_columns:
                    connection.execute(
                        "ALTER TABLE devices ADD COLUMN provider TEXT NOT NULL "
                        "DEFAULT 'legacy_relay'"
                    )
                if "token_environment" not in device_columns:
                    connection.execute(
                        "ALTER TABLE devices ADD COLUMN token_environment TEXT NOT NULL DEFAULT ''"
                    )
                for column, declaration in (
                    ("recipient_public_key", "TEXT NOT NULL DEFAULT ''"),
                    ("recipient_key_id", "TEXT NOT NULL DEFAULT ''"),
                    ("revision", "INTEGER NOT NULL DEFAULT 0"),
                    ("lease_expires", "INTEGER NOT NULL DEFAULT 0"),
                    ("normalized_body_digest", "TEXT NOT NULL DEFAULT ''"),
                    ("sender_key_revision", "INTEGER NOT NULL DEFAULT 0"),
                    ("acknowledged_sender_key_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
                    ("sender_ack_body_digest", "TEXT NOT NULL DEFAULT ''"),
                    ("relay_generation", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if column not in device_columns:
                        connection.execute(
                            f"ALTER TABLE devices ADD COLUMN {column} {declaration}"
                        )
                if migrated_legacy_devices:
                    now = int(time.time())
                    connection.execute(
                        "UPDATE devices SET revoked_at=COALESCE(revoked_at, ?), updated_at=? "
                        "WHERE provider='legacy_relay'",
                        (now, now),
                    )
                live_activity_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(live_activities)").fetchall()
                }
                if "last_push_timestamp" not in live_activity_columns:
                    connection.execute(
                        "ALTER TABLE live_activities ADD COLUMN "
                        "last_push_timestamp INTEGER NOT NULL DEFAULT 0"
                    )
                if "owner_generation" not in live_activity_columns:
                    connection.execute(
                        "ALTER TABLE live_activities ADD COLUMN "
                        "owner_generation INTEGER NOT NULL DEFAULT 0"
                    )
                relay_activity_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(relay_live_activities)"
                    ).fetchall()
                }
                for column, declaration in (
                    ("last_push_timestamp", "INTEGER NOT NULL DEFAULT 0"),
                    ("ended_at", "INTEGER"),
                ):
                    if column not in relay_activity_columns:
                        connection.execute(
                            f"ALTER TABLE relay_live_activities ADD COLUMN {column} {declaration}"
                        )
                delivery_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(event_deliveries)").fetchall()
                }
                for column, declaration in (
                    ("target_revision", "INTEGER NOT NULL DEFAULT 0"),
                    ("target_generation", "INTEGER NOT NULL DEFAULT 0"),
                    ("target_key_id", "TEXT NOT NULL DEFAULT ''"),
                    ("target_sender_key_id", "TEXT NOT NULL DEFAULT ''"),
                    ("relay_request_body_json", "TEXT NOT NULL DEFAULT ''"),
                    ("next_attempt_at", "INTEGER NOT NULL DEFAULT 0"),
                    ("claim_token", "TEXT NOT NULL DEFAULT ''"),
                    ("claim_expires", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if column not in delivery_columns:
                        connection.execute(
                            f"ALTER TABLE event_deliveries ADD COLUMN {column} {declaration}"
                        )
                connection.execute(
                    "DELETE FROM metadata WHERE key IN "
                    "('relay_url', 'credential', 'installation_id')"
                )
                relay_pending_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(pending_relay_live_activity_updates)"
                    ).fetchall()
                }
                for column, declaration in (
                    ("timestamp", "INTEGER NOT NULL DEFAULT 0"),
                    ("delivery_id", "TEXT NOT NULL DEFAULT ''"),
                    ("idempotency_key", "TEXT NOT NULL DEFAULT ''"),
                    ("request_body_json", "TEXT NOT NULL DEFAULT ''"),
                    ("device_id", "TEXT NOT NULL DEFAULT ''"),
                    ("session_ref", "TEXT NOT NULL DEFAULT ''"),
                    ("revision", "INTEGER NOT NULL DEFAULT 0"),
                    ("lease_expires", "INTEGER NOT NULL DEFAULT 0"),
                    ("relay_generation", "INTEGER NOT NULL DEFAULT 0"),
                    ("terminal", "INTEGER NOT NULL DEFAULT 0"),
                ):
                    if column not in relay_pending_columns:
                        connection.execute(
                            f"ALTER TABLE pending_relay_live_activity_updates ADD COLUMN {column} {declaration}"
                        )
                direct_pending_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(pending_live_activity_updates)"
                    ).fetchall()
                }
                for column, declaration in (
                    ("terminal", "INTEGER NOT NULL DEFAULT 0"),
                    ("owner_session_id", "TEXT NOT NULL DEFAULT ''"),
                    ("owner_live_session_id", "TEXT NOT NULL DEFAULT ''"),
                    ("owner_profile", "TEXT NOT NULL DEFAULT ''"),
                    ("owner_push_token", "TEXT NOT NULL DEFAULT ''"),
                    ("owner_generation", "INTEGER NOT NULL DEFAULT 0"),
                    ("request_id", "TEXT NOT NULL DEFAULT ''"),
                ):
                    if column not in direct_pending_columns:
                        connection.execute(
                            f"ALTER TABLE pending_live_activity_updates ADD COLUMN {column} {declaration}"
                        )
                # Existing direct rows predate owner/request CAS.  Rebind
                # only rows whose activity is still active; re-registration
                # already removes its old pending row before creating a new
                # owner, so this cannot revive stale work.
                connection.execute(
                    "UPDATE pending_live_activity_updates SET "
                    "owner_session_id=(SELECT session_id FROM live_activities WHERE live_activities.activity_id=pending_live_activity_updates.activity_id), "
                    "owner_live_session_id=(SELECT live_session_id FROM live_activities WHERE live_activities.activity_id=pending_live_activity_updates.activity_id), "
                    "owner_profile=(SELECT profile FROM live_activities WHERE live_activities.activity_id=pending_live_activity_updates.activity_id), "
                    "owner_push_token=(SELECT push_token FROM live_activities WHERE live_activities.activity_id=pending_live_activity_updates.activity_id) "
                    "WHERE owner_session_id='' AND EXISTS (SELECT 1 FROM live_activities "
                    "WHERE live_activities.activity_id=pending_live_activity_updates.activity_id "
                    "AND live_activities.ended_at IS NULL)"
                )
                connection.execute(
                    "UPDATE live_activities SET owner_generation=1 WHERE owner_generation=0"
                )
                connection.execute(
                    "UPDATE pending_live_activity_updates SET owner_generation=("
                    "SELECT owner_generation FROM live_activities "
                    "WHERE live_activities.activity_id=pending_live_activity_updates.activity_id"
                    ") WHERE owner_generation=0 AND EXISTS (SELECT 1 FROM live_activities "
                    "WHERE live_activities.activity_id=pending_live_activity_updates.activity_id)"
                )
                connection.execute(
                    "INSERT INTO metadata(key, value) VALUES ('schema_version', '8') "
                    "ON CONFLICT(key) DO UPDATE SET value='8'"
                )
                operation_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(pending_relay_operations)"
                    ).fetchall()
                }
                for column, declaration in (
                    ("response_json", "TEXT NOT NULL DEFAULT ''"),
                    ("relay_generation", "INTEGER NOT NULL DEFAULT 0"),
                    ("request_digest", "TEXT NOT NULL DEFAULT ''"),
                    ("next_attempt_at", "INTEGER NOT NULL DEFAULT 0"),
                    ("claim_token", "TEXT NOT NULL DEFAULT ''"),
                    ("claim_expires", "INTEGER NOT NULL DEFAULT 0"),
                    ("terminal", "INTEGER NOT NULL DEFAULT 0"),
                    ("last_error", "TEXT NOT NULL DEFAULT ''"),
                    ("row_version", "INTEGER NOT NULL DEFAULT 1"),
                ):
                    if column not in operation_columns:
                        connection.execute(
                            f"ALTER TABLE pending_relay_operations ADD COLUMN {column} {declaration}"
                        )
                # Backfill the immutable request digest for valid legacy rows;
                # malformed rows remain quarantinable by the recovery owner.
                for legacy in connection.execute(
                    "SELECT operation, device_id, body_json FROM pending_relay_operations "
                    "WHERE request_digest=''"
                ).fetchall():
                    try:
                        parsed = json.loads(str(legacy["body_json"]))
                        digest = hashlib.sha256(_json(parsed).encode("utf-8")).hexdigest()
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    connection.execute(
                        "UPDATE pending_relay_operations SET request_digest=? "
                        "WHERE operation=? AND device_id=? AND request_digest=''",
                        (digest, legacy["operation"], legacy["device_id"]),
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO metadata(key, value) VALUES ('provider_mode', ?)",
                    ("managed" if migrated_legacy_devices else "relay",),
                )
                # v2.1 used both spellings for the device revoke route.  Keep
                # one durable key so aliases cannot create two operations.
                connection.execute(
                    "UPDATE pending_relay_operations SET operation='revoke_device' "
                    "WHERE operation='device_revoke' AND NOT EXISTS ("
                    "SELECT 1 FROM pending_relay_operations newer "
                    "WHERE newer.operation='revoke_device' "
                    "AND newer.device_id=pending_relay_operations.device_id)"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO metadata(key, value) "
                    "VALUES ('relay_config_generation', '0')"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO metadata(key, value) "
                    "VALUES ('relay_config_state', 'enabled')"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS pending_relay_operations_due_idx "
                    "ON pending_relay_operations(terminal, next_attempt_at, claim_expires)"
                )
                connection.execute(
                    "CREATE INDEX IF NOT EXISTS event_deliveries_relay_due_idx "
                    "ON event_deliveries(provider, status, next_attempt_at, claim_expires)"
                )
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
            self._initialized = True

    @contextmanager
    def _connect(self, *, initialize: bool = True):
        if initialize:
            self._ensure_schema()
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _metadata_integer(connection: sqlite3.Connection, key: str, default: int) -> int:
        row = connection.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            value = int(row["value"])
        except (TypeError, ValueError):
            return default
        return max(0, value)


def _lock_file_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                return
            except OSError as error:
                if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                time.sleep(0.05)

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX)


def _unlock_file_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _event_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "event_id": row["event_id"],
        "type": row["type"],
        "status": row["status"],
        "target": row["target"],
        "profile": row["profile"],
        "session_id": row["session_id"] or None,
        "job_id": row["job_id"] or None,
        "task_id": row["task_id"] or None,
        "approval_id": row["approval_id"] or None,
        "delegation_id": row["delegation_id"] or None,
        "detail": _load_json(row["detail_json"], {}),
        "push": _load_json(row["push_json"], {}),
        "created_at": row["created_at"],
        "delivered_at": row["delivered_at"],
        "delivery_id": row["delivery_id"] or None,
        "failure": row["failure"],
        "dismissed_at": row["dismissed_at"],
        "is_read": row["read_at"] is not None,
        "is_pinned": row["pinned_at"] is not None,
    }


def _device_row(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "device_id": row["device_id"],
        "endpoint_id": row["endpoint_id"],
        "provider": row["provider"],
        "token_environment": row["token_environment"],
        "label": row["label"],
        "groups": _load_json(row["groups_json"], []),
        "preferences": _load_json(row["preferences_json"], {}),
        "revoked": row["revoked_at"] is not None,
    }
    if row["provider"] == "relay":
        result.update(
            {
                "recipient_public_key": row["recipient_public_key"],
                "recipient_key_id": row["recipient_key_id"],
                "revision": row["revision"],
                "lease_expires": row["lease_expires"],
                "sender_key_revision": row["sender_key_revision"],
                "relay_generation": row["relay_generation"],
                "acknowledged_sender_key_ids": _load_json(
                    row["acknowledged_sender_key_ids_json"], []
                ),
            }
        )
    return result


def _form_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "request_id": row["request_id"],
        "profile": row["profile"],
        "session_id": row["session_id"],
        "form_schema": _load_json(row["form_schema_json"], {}),
        "content_hash": row["content_hash"],
        "state": row["state"],
        "idempotency_key": row["idempotency_key"],
        "request_digest": row["request_digest"],
        "values": _load_json(row["values_json"], None),
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "submitted_at": row["submitted_at"],
        "consumed_at": row["consumed_at"],
    }


def _same_owner(row: sqlite3.Row, profile: str, session_id: str) -> bool:
    return hmac.compare_digest(str(row["profile"]), str(profile)) and hmac.compare_digest(
        str(row["session_id"]), str(session_id)
    )


def _form_request_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if len(normalized) != 32 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("request_id must be 32 lowercase hexadecimal characters")
    return normalized


def _content_hash(value: Any) -> str:
    normalized = str(value or "").strip()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError("content_hash must be 64 lowercase hexadecimal characters")
    return normalized


def _idempotency_key(value: Any) -> str:
    normalized = str(value or "").strip()
    try:
        parsed = uuid.UUID(normalized)
    except (ValueError, AttributeError) as error:
        raise ValueError("idempotency_key must be a lowercase UUID") from error
    if str(parsed) != normalized:
        raise ValueError("idempotency_key must be a lowercase UUID")
    return normalized


_FORM_MESSAGES = {
    "accepted": "Form response accepted.",
    "request_not_found": "Form request was not found.",
    "request_expired": "Form request has expired.",
    "owner_mismatch": "Form request belongs to another session.",
    "invalid_value": "One or more form values are invalid.",
    "already_submitted": "Form response was already submitted.",
    "already_consumed": "Form response was already consumed.",
    "idempotency_conflict": "Idempotency key was reused with different values.",
    "payload_too_large": "Form response exceeds the byte limit.",
    "internal_error": "Form response could not be processed.",
}


def _form_response(request_id: str, idempotency_key: str, state: str, code: str) -> dict[str, Any]:
    return {
        "schema": "loopdy.generative_ui.action_response",
        "version": 2,
        "request_id": request_id,
        "idempotency_key": idempotency_key,
        "state": state,
        "code": code,
        "message": _FORM_MESSAGES[code],
    }


def form_action_response(request_id: str, idempotency_key: str, state: str, code: str) -> dict[str, Any]:
    """Build a fixed public envelope without reflecting malformed identifiers."""
    try:
        safe_request_id = _form_request_id(request_id)
    except ValueError:
        safe_request_id = "0" * 32
    return _form_response(safe_request_id, str(idempotency_key or ""), state, code)


def _card_template(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Card template must be an object")
    expected = {
        "id",
        "version",
        "name",
        "summary",
        "author",
        "license",
        "minimum_card_version",
        "parameters_schema",
        "document",
        "sha256",
    }
    if set(value) != expected:
        raise ValueError("Card template schema is invalid")
    normalized = json.loads(canonical_card_json(dict(value)))
    normalized["id"] = _card_template_id(normalized["id"])
    normalized["version"] = _positive_revision(normalized["version"])
    normalized["name"] = _required_text(normalized["name"], "name", 120)
    normalized["summary"] = _required_text(normalized["summary"], "summary", 1_000)
    normalized["author"] = _required_text(normalized["author"], "author", 120)
    normalized["license"] = _required_text(normalized["license"], "license", 120)
    if normalized["minimum_card_version"] != 1:
        raise ValueError("Card template minimum card version is unsupported")
    _card_template_parameters_schema(normalized["parameters_schema"])
    validate_card_input(normalized["document"], now=datetime.now(timezone.utc))
    supplied_hash = _card_template_hash(normalized["sha256"])
    expected_hash = hashlib.sha256(
        canonical_card_json(normalized["document"]).encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(supplied_hash, expected_hash):
        raise ValueError("Card template hash does not match its bundle")
    normalized["sha256"] = expected_hash
    return normalized


def _card_template_id(value: Any) -> str:
    if type(value) is not str or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value) is None:
        raise ValueError("Card template id is invalid")
    return value


def _card_template_name_key(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _saved_card_template_id(name: str, *, taken: set[str]) -> str:
    """A readable id from the name; it never changes once saved, even on rename."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")[:48].strip("-")
    digest = hashlib.sha256(_card_template_name_key(name).encode("utf-8")).hexdigest()
    base = f"saved-{slug or digest[:8]}"
    for candidate in (base, f"{base}-{digest[:6]}", *(f"{base}-{digest[:6]}-{n}" for n in range(2, 100))):
        if candidate not in taken:
            return candidate
    raise CardTemplateSaveRejected("catalog_full", "No free template id is left for this name.")


def _card_template_hash(value: Any) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("Card template hash is invalid")
    return value


def _card_template_parameters_schema(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "type", "properties", "required", "additionalProperties"
    }:
        raise ValueError("Card template parameters schema is invalid")
    if value["type"] != "object" or value["additionalProperties"] is not False:
        raise ValueError("Card template parameters schema must be a strict object")
    properties = value["properties"]
    required = value["required"]
    if not isinstance(properties, dict):
        raise ValueError("Card template parameter properties are invalid")
    if (
        not isinstance(required, list)
        or len(required) != len(set(required))
        or any(type(item) is not str or item not in properties for item in required)
    ):
        raise ValueError("Card template required parameters are invalid")
    for name, schema in properties.items():
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name) is None:
            raise ValueError("Card template parameter name is invalid")
        if (
            not isinstance(schema, dict)
            or set(schema) - {"type", "title", "description", "default", "enum"}
            or schema.get("type") not in {"string", "integer", "number", "boolean"}
        ):
            raise ValueError("Card template parameter schema is invalid")
        if "title" in schema:
            _required_text(schema["title"], "parameter title", 120)
        if "description" in schema:
            _required_text(schema["description"], "parameter description", 500)
        if "enum" in schema:
            enum = schema["enum"]
            if not isinstance(enum, list) or not enum:
                raise ValueError("Card template parameter enum is invalid")
            if len({canonical_card_json(item) for item in enum}) != len(enum):
                raise ValueError("Card template parameter enum is invalid")
            for item in enum:
                _card_template_parameter_value(item, schema["type"])
        if "default" in schema:
            _card_template_parameter_value(schema["default"], schema["type"])
            if "enum" in schema and schema["default"] not in schema["enum"]:
                raise ValueError("Card template parameter default is invalid")


def _card_template_parameter_value(value: Any, kind: str) -> None:
    valid = (
        (kind == "string" and isinstance(value, str))
        or (kind == "integer" and type(value) is int)
        or (kind == "number" and type(value) in {int, float})
        or (kind == "boolean" and type(value) is bool)
    )
    if not valid:
        raise ValueError("Card template parameter value is invalid")


def _identifier(value: str, name: str) -> str:
    return _required_text(value, name, 180)


def _required_text(value: str, name: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum} characters")
    return normalized


def _provider_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "relay":
        return "managed"
    if normalized not in {"managed", "direct"}:
        raise ValueError("bighelp provider mode must be managed or direct")
    return normalized


def _device_provider(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"managed", "direct", "relay", "legacy_relay"}:
        raise ValueError("bighelp device provider must be managed, direct, or relay")
    return normalized


def _delivery_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in {"queued", "sent", "failed", "suppressed"}:
        raise ValueError("Invalid device delivery status")
    return normalized


def _text(value: Any, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Value is not canonical JSON") from error


def _positive_revision(value: Any) -> int:
    if (
        type(value) is not int
        or value <= 0
        or value > _MAX_SAFE_REVISION
    ):
        raise ValueError("Relay revision must be a positive integer")
    return value


def _load_json(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default
