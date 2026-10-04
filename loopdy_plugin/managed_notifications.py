"""Notification-only native enrollment and durable, scoped BuzzKit delivery.

This module owns no Hermes runtime internals. Stock registered observers provide
lifecycle facts; a plugin-owned worker drains frozen requests over HTTPS. Chat,
Link pairing, the legacy provider and LOOPDY_HOME_TARGET are not prerequisites.
"""
from __future__ import annotations

try:
    import fcntl
except ImportError:  # Unsupported private-store locking must not break legacy APIs.
    fcntl = None
import hashlib
import base64
import importlib
import json
import logging
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import types
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from . import live_alerts
from .card_previews import reply_preview
from .reactions import REACTION_SCHEMA
from .relay_crypto import b64url_decode, b64url_encode, canonical_json_bytes, key_id, public_key_bytes, public_key_from_x963, sign_p1363
from .sealed_alerts import seal_alert, seal_avatar
from .session_state import open_profile_store

ORIGIN = "https://link.loopdy.app"
ROOT = "/v1/notifications/host-grants"
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CRON_SESSION = re.compile(r"^cron_.+_\d{8}_\d{6}$")
_EVENT_TYPES = {
    "session.completed", "session.failed", "scheduled.completed", "scheduled.failed",
    "approval.required", "clarification.required", "subagent.completed", "subagent.failed",
}
_APPROVAL_EVENT = "approval.required"
_CLARIFICATION_EVENT = "clarification.required"
# The messaging adapter presents a question just after the clarify tool hook saw it.
_ASKED_WINDOW_SECONDS = 600
_MAX_RICH_TEXT = 1_600
_MAX_AVATAR_BYTES = 524_288
_APPROVAL_GRACE_SECONDS = 3
_APPROVAL_TTL_SECONDS = 60
_APPROVAL_LIMIT = 4096
_APPROVAL_HOOKS = ("pre_approval_request", "post_approval_response")
_ACTIVITY_REFRESH_SECONDS = 60
# A rotating sign-in (the Nous Portal's lasts a day) runs out if bighelp stays closed.
# Waking the phone this often gives it more than one chance to renew before then.
_SIGN_IN_WAKE_SECONDS = 8 * 3600
_SIGN_IN_WAKE_PATH = "/wake"
_ACTIONS = {
    "thinking": "Your agent is working", "waiting": "Your agent needs attention",
    "using_tool": "Your agent is working", "delegating": "Agents are working",
    "responding": "Your agent is responding", "completed": "Your agent finished",
    "failed": "Your agent could not finish",
}
logger = logging.getLogger("hermes.plugins.bighelp.notifications")


class ManagedNotificationError(ValueError):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


def _identifier(value: Any, pattern: re.Pattern = _ID) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ManagedNotificationError("notification_coordinate_invalid", 400)
    return value


# A reply alert carries only what Hermes itself would deliver (gateway/response_filters.py).
# Scheduled and webhook runs use the loose rule the cron scheduler and webhook adapter use. Any
# other turn stays silent only on an exact marker, and only when no person is waiting for the
# answer; a person who got a bare marker sees the notice Hermes' gateway sends instead.
_AUTONOMOUS_PLATFORMS = frozenset({"cron", "webhook"})
# The one kind prompt.submit lets a client author: an off-screen send nobody sees (a widget tap,
# or an older bighelp reaction note). Hermes history drops these rows, so for silence they are
# machinery too.
_OFF_SCREEN_DISPLAY_KIND = "hidden"
# gateway/run_turn.py's _UNEXPECTED_SILENCE_REPLY, word for word.
_UNEXPECTED_SILENCE_REPLY = ("⚠️ The model returned only a silence marker for a message that needed "
                             "a reply. Try again or rephrase.")
_LEGACY_SILENCE_MARKERS = frozenset({"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"})
# Hermes runs each group chat member's turn in a session of this source, and its rules there tell an
# agent with nothing new to add to reply "(pass)". Hermes publishes no message for a pass, so it
# sends no alert either. The pattern is gateway/hosted_room_discussion.py's is_pass_text.
_GROUP_CHAT_PLATFORM = "bot_room"
# Where `hermes peer dm` and agent-to-agent DMs land: Hermes' canonical "Bot Chat" session.
_PEER_CHAT_TITLE = "Bot Chat"
# Replies and helper results from a peer chat alert only phones that asked for them;
# a question or approval there still needs the person, so it always alerts.
_PEER_QUIET_EVENTS = frozenset({"session.completed", "session.failed", "subagent.completed", "subagent.failed"})
_GROUP_CHAT_PASS = re.compile(r"\(?\s*pass\s*\)?\.?", re.IGNORECASE)
# Hermes' turn-end file-mutation verifier footer (run_agent.py): a "⚠️ File-mutation verifier:"
# line plus indented "•" bullets, appended AFTER the model's final line. It is runtime machinery,
# not the model's answer, so a run that ended on a silence marker must stay silent with it attached.
_VERIFIER_FOOTER = re.compile(r"\n\s*⚠️?\s*File-mutation verifier:.*\Z", re.DOTALL)


def _without_runtime_footer(response: Any) -> Any:
    return _VERIFIER_FOOTER.sub("", response).rstrip() if isinstance(response, str) else response


def _turn_prompt(history: Any) -> tuple[Any, Any]:
    """The current turn's user-row display kind and ``reply_expected`` flag. Hermes appends the
    turn's user row, typed at turn start, before the model runs."""
    for message in reversed(history if isinstance(history, list) else ()):
        if isinstance(message, dict) and message.get("role") == "user":
            metadata = message.get("display_metadata")
            return (message.get("display_kind"),
                    metadata.get("reply_expected") if isinstance(metadata, dict) else None)
    return None, None


def _turn_reacted(history: Any) -> bool:
    """Whether this turn's agent reacted to the person's message (``bighelp_react_to_message``).
    Hermes retries a reply with no text, so a reaction that says it all ends on a bare marker."""
    for message in reversed(history if isinstance(history, list) else ()):
        if not isinstance(message, dict) or message.get("role") == "user":
            return False
        content = message.get("content")
        if message.get("role") != "tool" or not isinstance(content, str) or len(content) > 65_536:
            continue
        try:
            result = json.loads(content)
        except ValueError:
            continue
        if (isinstance(result, dict) and result.get("schema") == REACTION_SCHEMA and result.get("success") is True
                and any(isinstance(item, dict) and item.get("author") == "agent"
                        for item in result.get("reactions") or ())):
            return True
    return False


def _delivered_reply(response: Any, *, autonomous: bool, history: Any, group_chat: bool = False) -> Any:
    """``response`` as Hermes would deliver it: ``""`` when the turn stays silent, Hermes' notice
    when a person got a bare marker, else unchanged. Never raises; unsure means unchanged."""
    answer = _without_runtime_footer(response)
    if group_chat and isinstance(answer, str) and _GROUP_CHAT_PASS.fullmatch(answer.strip()):
        return ""
    try:
        rules = importlib.import_module("gateway.response_filters")
    except ImportError:  # Every supported Hermes has it; a bare marker still stays quiet.
        text = answer.strip().upper() if isinstance(answer, str) else ""
        return "" if text in _LEGACY_SILENCE_MARKERS or (autonomous and text.startswith("[SILENT]")) else response
    try:
        if autonomous:
            return "" if rules.is_autonomous_silence_response(answer) else response
        if not rules.is_intentional_silence_response(answer):
            return response
        silence_allowed = getattr(rules, "silence_allowed", None)
        if silence_allowed is None:  # Before the human-turn notice, Hermes kept every bare marker quiet.
            return ""
        kind, reply_expected = _turn_prompt(history)
        if kind == _OFF_SCREEN_DISPLAY_KIND or silence_allowed(kind, reply_expected) or _turn_reacted(history):
            return ""
        return _UNEXPECTED_SILENCE_REPLY
    except Exception:
        logger.warning("Notification silence check unavailable; the reply alerts unchanged")
        return response


def session_reference(profile: str, session_id: str) -> str:
    return b64url_encode(hashlib.sha256(f"{profile}\0{session_id}".encode()).digest())


def host_request_transcript(method: str, path: str, grant_id: str, timestamp: int, nonce: str, raw: bytes) -> bytes:
    return "\n".join(("loopdy-notification-host-v1", method, path, grant_id, str(timestamp), nonce, hashlib.sha256(raw).hexdigest())).encode()


def _send_in_background(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="loopdy-managed-notifications-now", daemon=True).start()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ManagedNotificationError("notification_redirect_rejected", 502)


def _https_request(method: str, path: str, raw: bytes, headers: dict[str, str]) -> dict[str, Any]:
    if not path.startswith(ROOT + "/") or "?" in path or "#" in path:
        raise ManagedNotificationError("notification_path_invalid", 400)
    request = Request(ORIGIN + path, data=raw if raw else None, headers=headers, method=method)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=12) as response:
            content = response.read(65537)
            if len(content) > 65536:
                raise ManagedNotificationError("notification_response_too_large", 502)
            value = json.loads(content)
            if not isinstance(value, dict) or value.get("version") != 1:
                raise ManagedNotificationError("notification_response_invalid", 502)
            return value
    except HTTPError as error:
        # Do not copy server bodies, tokens, grant contents or headers into logs.
        raise ManagedNotificationError("notification_remote_rejected", error.code) from None
    except (OSError, json.JSONDecodeError) as error:
        raise ManagedNotificationError("notification_service_unavailable", 503) from error


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ManagedNotificationError("notification_private_storage_required", 503)


def _private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ManagedNotificationError("notification_private_storage_required", 503)


class _AlreadyAlerted(Exception):
    """The turn's reply alert already went out from post_llm_call."""


class ManagedNotifications:
    """One process-owned observer/worker; SQLite serializes other API processes."""
    def __init__(self, directory: Path, *, transport: Callable = _https_request,
                 clock: Callable = time.time, session_opener: Callable = open_profile_store,
                 observations: dict[str, Any] | None = None,
                 send_on_queue: Callable[[Callable[[], None]], None] | None = None):
        if fcntl is None:
            raise ManagedNotificationError("notification_platform_unavailable", 503)
        self._fcntl = fcntl
        _private_directory(directory)
        self.directory, self.transport, self.clock = directory, transport, clock
        # A new alert goes out from the process that queued it, on its own thread,
        # instead of waiting for whichever Hermes process owns the sender to poll.
        # Tests with an injected transport send only when they drain (or opt in).
        self._send_on_queue = send_on_queue if send_on_queue is not None else (
            _send_in_background if transport is _https_request else None)
        self.session_opener = session_opener
        self.preference_policy: Callable | None = None
        # Live hook state. A fresh process starts empty; get_managed_notifications
        # passes the process-wide store so every import of this module shares it.
        state = observations if observations is not None else _new_observations()
        self._lock = state["lock"]
        self._wake, self._stop = state["wake"], threading.Event()
        self._worker: threading.Thread | None = None
        self._worker_lock = None
        self._loaded_profiles: set[str] = state["loaded_profiles"]
        self._approval_profiles: set[str] = state["approval_profiles"]
        self._clarification_profiles: set[str] = state["clarification_profiles"]
        # An observation belongs to this producer lifetime, never a recovered prompt.
        self._approval_owner = str(uuid.uuid4())
        self._work: OrderedDict[tuple[str, str, str], dict[str, Any]] = state["work"]
        self._responses: OrderedDict[tuple[str, str, str], str] = state["responses"]
        # Turns whose reply alert already went out from post_llm_call (setdefault: a
        # state dict made by an older copy of this module in the same process).
        self._alerted: OrderedDict[tuple[str, str, str], bool] = state.setdefault("alerted", OrderedDict())
        # Turns whose question alert already went out from the clarify tool hook, so the
        # messaging adapter's own presentation of the same question isn't a second alert.
        self._asked: OrderedDict[tuple[str, str, str], float] = state.setdefault("asked", OrderedDict())
        self._child_owners: dict[tuple[str, str, str], str] = state["child_owners"]
        self._child_goals: dict[tuple[str, str, str], str] = state["child_goals"]
        self._key = self._identity()
        self.public_key = b64url_encode(public_key_bytes(self._key.public_key()))
        self.key_id = key_id(public_key_bytes(self._key.public_key()))
        self.db_path = directory / "journal.sqlite3"
        descriptor = os.open(self.db_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        _private_file(self.db_path)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS grants(grant_id TEXT PRIMARY KEY, public_json TEXT NOT NULL, state TEXT NOT NULL, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS subscriptions(grant_id TEXT NOT NULL REFERENCES grants(grant_id) ON DELETE CASCADE, profile TEXT NOT NULL, session_id TEXT NOT NULL, session_ref TEXT NOT NULL, PRIMARY KEY(grant_id,profile,session_id));
                CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(grant_id) ON DELETE CASCADE, detail_json TEXT NOT NULL, occurred_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS pending(intent_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(grant_id) ON DELETE CASCADE, path TEXT NOT NULL, raw BLOB NOT NULL, expires INTEGER NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt INTEGER NOT NULL, session_ref TEXT NOT NULL, activity_id TEXT);
                CREATE INDEX IF NOT EXISTS pending_due ON pending(state,next_attempt);
                CREATE TABLE IF NOT EXISTS approval_attention(event_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(grant_id) ON DELETE CASCADE, profile TEXT NOT NULL, session_id TEXT NOT NULL, turn_id TEXT NOT NULL, tool_call_id TEXT NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL, expires INTEGER NOT NULL, reason TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS approval_scope ON approval_attention(grant_id,profile,session_id,turn_id);
                CREATE TABLE IF NOT EXISTS recipients(grant_id TEXT PRIMARY KEY REFERENCES grants(grant_id) ON DELETE CASCADE, public_key TEXT NOT NULL, key_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS avatar_keys(grant_id TEXT NOT NULL REFERENCES grants(grant_id) ON DELETE CASCADE, sha256 TEXT NOT NULL, key BLOB NOT NULL, nonce BLOB NOT NULL, PRIMARY KEY(grant_id,sha256));
                CREATE TABLE IF NOT EXISTS preferences(grant_id TEXT PRIMARY KEY REFERENCES grants(grant_id) ON DELETE CASCADE, peer_chats INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS activities(activity_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(grant_id) ON DELETE CASCADE, profile TEXT NOT NULL, session_id TEXT NOT NULL, session_ref TEXT NOT NULL, lease_expires INTEGER NOT NULL, work_turn TEXT, state TEXT NOT NULL, last_timestamp INTEGER NOT NULL DEFAULT 0, last_signature TEXT, last_queued_at INTEGER NOT NULL DEFAULT 0);
            """)
            db.executescript(live_alerts.SCHEMA)

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _identity(self):
        # A separate sidecar lock avoids locking SQLite's own inode. Never place
        # private key material inside the replaceable plugin install directory.
        lock_path = self.directory / "identity.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a+b") as lock:
            _private_file(lock_path)
            self._fcntl.flock(lock, self._fcntl.LOCK_EX)
            path = self.directory / "host-key.pem"
            if not path.exists():
                key = ec.generate_private_key(ec.SECP256R1())
                encoded = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
                temp = self.directory / f".host-key-{uuid.uuid4()}.tmp"
                try:
                    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(fd, "wb") as output:
                        output.write(encoded); output.flush(); os.fsync(output.fileno())
                    os.replace(temp, path)
                finally:
                    temp.unlink(missing_ok=True)
            _private_file(path)
            if path.stat().st_size > 4096:
                raise ManagedNotificationError("notification_identity_invalid", 503)
            key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
                raise ManagedNotificationError("notification_identity_invalid", 503)
            return key

    def capabilities(self) -> dict[str, Any]:
        with self._lock:
            loaded = bool(self._loaded_profiles)
            approval_loaded = bool(self._approval_profiles)
            clarification_loaded = bool(self._clarification_profiles)
        return {"version": 1, "hostKeyId": self.key_id, "hostPublicKey": self.public_key,
                "managedEnrollmentSupported": True, "supportedEventTypes": sorted(_EVENT_TYPES),
                "sealedAlerts": {"version": 2}, "preferences": {"peerChats": True},
                "richLiveActivitySupported": True, "producerCapabilities": {
                    "sessionCompletion": loaded, "sessionFailure": loaded, "richLiveActivity": loaded,
                    "nativeApproval": approval_loaded, "nativeClarification": clarification_loaded}}

    def provider_contract(self) -> dict[str, Any]:
        return {"version": 1, "provider": "buzzkit",
                "subscriberScopes": ["notification-instance", "account"],
                "preferences": "buzzkit-topics", "richContentRequired": True,
                "serverSendingAuthorityOnHost": False,
                "eventTypes": sorted(_EVENT_TYPES), "maximumAvatarBytes": _MAX_AVATAR_BYTES,
                "maximumTextCharacters": _MAX_RICH_TEXT,
                "topics": {
                    "chat-replies-completions": ["session.completed", "session.failed"],
                    "scheduled-tasks-deliveries": ["scheduled.completed", "scheduled.failed"],
                    "questions-approvals": ["approval.required", "clarification.required"],
                    "subagent-completions": ["subagent.completed", "subagent.failed"],
                }}

    def _request(self, method: str, grant_id: str, suffix: str = "", raw: bytes = b"",
                 *, before_transport: Callable | None = None):
        _identifier(grant_id, _UUID)
        path = ROOT + "/" + grant_id + suffix
        timestamp, nonce = int(self.clock()), b64url_encode(os.urandom(32))
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "Loopdy-Managed-Notifications/1", "x-loopdy-host-key-id": self.key_id,
                   "x-loopdy-timestamp": str(timestamp), "x-loopdy-nonce": nonce,
                   "x-loopdy-signature": b64url_encode(sign_p1363(self._key, host_request_transcript(method, path, grant_id, timestamp, nonce, raw)))}
        if before_transport is not None and not before_transport():
            raise ManagedNotificationError("notification_attention_retired", 410)
        return self.transport(method, path, raw, headers)

    def _validate_grant(self, value: Any, grant_id: str) -> dict[str, Any]:
        if (not isinstance(value, dict) or value.get("grantId") != grant_id
                or value.get("hostKeyId") != self.key_id
                or value.get("hostPublicKey") != self.public_key
                or value.get("provider") != "buzzkit"
                or value.get("subscriberScope") not in {"notification-instance", "account"}
                or value.get("state") != "active"):
            raise ManagedNotificationError("notification_grant_identity_mismatch", 403)
        for field in ("revision", "authorizationEpoch", "createdAt", "expiresAt"):
            if type(value.get(field)) is not int or value[field] < 1:
                raise ManagedNotificationError("notification_grant_invalid", 502)
        if (not value["createdAt"] < value["expiresAt"] <= value["createdAt"] + 2592000
                or value["expiresAt"] <= int(self.clock())):
            raise ManagedNotificationError("notification_grant_expired", 403)
        _identifier(value.get("profile"), _PROFILE)
        event_types = value.get("eventTypes")
        if (not isinstance(event_types, list) or not event_types
                or any(not isinstance(event_type, str) for event_type in event_types)
                or len(set(event_types)) != len(event_types) or not set(event_types) <= _EVENT_TYPES):
            raise ManagedNotificationError("notification_grant_invalid", 502)
        if value["subscriberScope"] == "notification-instance":
            _identifier(value.get("instanceId"), _UUID)
        elif "instanceId" in value:
            _identifier(value.get("instanceId"), _UUID)
        fields = ("grantId", "hostKeyId", "hostPublicKey", "authorizationEpoch", "profile",
                  "eventTypes", "createdAt", "expiresAt", "revision", "provider",
                  "subscriberScope", "state")
        expected = set(fields) | ({"instanceId"} if "instanceId" in value else set())
        if set(value) != expected:
            raise ManagedNotificationError("notification_grant_invalid", 502)
        result = {field: value[field] for field in fields}
        if "instanceId" in value:
            result["instanceId"] = value["instanceId"]
        return result

    def enroll(self, grant_id: str, idempotency_key: str):
        _identifier(grant_id, _UUID); _identifier(idempotency_key, _UUID)
        value = self._request("POST", grant_id, "/claim", canonical_json_bytes({"version": 2, "idempotencyKey": idempotency_key}))
        grant = self._validate_grant(value.get("grant"), grant_id)
        with self._db() as db:
            if db.execute("SELECT COUNT(*) FROM grants").fetchone()[0] >= 256 and not db.execute("SELECT 1 FROM grants WHERE grant_id=?", (grant_id,)).fetchone():
                raise ManagedNotificationError("notification_enrollment_limit")
            previous = db.execute("SELECT * FROM grants WHERE grant_id=?", (grant_id,)).fetchone()
            encoded = canonical_json_bytes(grant).decode()
            if previous and (previous["public_json"] != encoded or previous["state"] != "active"):
                raise ManagedNotificationError("notification_enrollment_conflict")
            db.execute("INSERT OR IGNORE INTO grants VALUES(?,?,'active',?)", (grant_id, encoded, grant["expiresAt"]))
        return {"version": 1, "grant": grant}

    def register_recipient(self, grant_id: str, public_key: str):
        """The phone's content key for this enrollment, sent directly by the phone.
        Later alerts for the grant are sealed so only that phone can read them."""
        self._grant(grant_id)
        try:
            raw = b64url_decode(public_key, expected_length=65)
            public_key_from_x963(raw)
        except ValueError as error:
            raise ManagedNotificationError("notification_recipient_key_invalid", 422) from error
        recipient_id = key_id(raw)
        with self._db() as db:
            db.execute("INSERT INTO recipients VALUES(?,?,?) ON CONFLICT(grant_id) DO UPDATE SET public_key=excluded.public_key, key_id=excluded.key_id",
                       (grant_id, public_key, recipient_id))
        return {"version": 1, "recipientKeyId": recipient_id}

    def _sealed_event(self, db, grant_id: str, recipient_public_key: str, *, event_id: str, event_type: str,
                      session_reference: str, turn_id: str, occurred_at: int, title: str, text: str,
                      avatar: dict[str, Any]) -> bytes:
        image = base64.b64decode(avatar["data"].split(",", 1)[1], validate=True)
        row = db.execute("SELECT key,nonce FROM avatar_keys WHERE grant_id=? AND sha256=?", (grant_id, avatar["sha256"])).fetchone()
        if row:
            avatar_key, avatar_nonce = bytes(row["key"]), bytes(row["nonce"])
        else:
            # One key per image keeps the encrypted avatar identical, so the service stores it once.
            avatar_key, avatar_nonce = os.urandom(32), os.urandom(12)
            db.execute("INSERT INTO avatar_keys VALUES(?,?,?,?)", (grant_id, avatar["sha256"], avatar_key, avatar_nonce))
        sealed_avatar = seal_avatar(grant_id=grant_id, image=image, key=avatar_key, nonce=avatar_nonce)
        cipher_sha256 = hashlib.sha256(sealed_avatar).hexdigest()
        envelope = seal_alert(
            grant_id=grant_id, event_id=event_id, event_type=event_type, title=title, body=text,
            avatar={"mimeType": avatar["mimeType"], "sha256": avatar["sha256"], "cipherSha256": cipher_sha256,
                    "key": b64url_encode(avatar_key), "nonce": b64url_encode(avatar_nonce)},
            recipient_public_key=b64url_decode(recipient_public_key, expected_length=65),
            sender_private_key=self._key, issued=occurred_at)
        return canonical_json_bytes({
            "version": 3, "eventId": event_id, "eventType": event_type, "sessionReference": session_reference,
            "turnId": turn_id, "occurredAt": occurred_at, "sealed": envelope,
            "avatar": {"sha256": cipher_sha256,
                       "data": "data:application/octet-stream;base64," + base64.b64encode(sealed_avatar).decode("ascii")},
            "sound": True})

    def _grant(self, grant_id: str) -> dict[str, Any]:
        _identifier(grant_id, _UUID)
        with self._db() as db:
            row = db.execute("SELECT * FROM grants WHERE grant_id=? AND state='active' AND expires>?", (grant_id, int(self.clock()))).fetchone()
        if not row:
            raise ManagedNotificationError("notification_enrollment_inactive", 404)
        return json.loads(row["public_json"])

    def enrollment(self, grant_id: str):
        local = self._grant(grant_id)
        try:
            remote = self._validate_grant(self._request("GET", grant_id).get("grant"), grant_id)
        except ManagedNotificationError as error:
            if error.status in (403, 404): self.remove(grant_id)
            raise
        if remote != local:
            self.remove(grant_id)
            raise ManagedNotificationError("notification_enrollment_changed", 403)
        return {"version": 1, "grant": remote}

    def remove(self, grant_id: str):
        _identifier(grant_id, _UUID)
        with self._db() as db:
            db.execute("UPDATE grants SET state='removed' WHERE grant_id=?", (grant_id,))
            db.execute("UPDATE approval_attention SET state='retired',reason='removed' WHERE grant_id=?", (grant_id,))
            db.execute("DELETE FROM subscriptions WHERE grant_id=?", (grant_id,))
            db.execute("DELETE FROM pending WHERE grant_id=?", (grant_id,))
            db.execute("DELETE FROM activities WHERE grant_id=?", (grant_id,))
            db.execute("DELETE FROM recipients WHERE grant_id=?", (grant_id,))
            db.execute("DELETE FROM avatar_keys WHERE grant_id=?", (grant_id,))
        return {"version": 1, "state": "removed", "grantId": grant_id}

    def preferences(self, grant_id: str):
        """This phone's alert preferences for the host. Peer chats start off."""
        self._grant(grant_id)
        with self._db() as db:
            row = db.execute("SELECT peer_chats FROM preferences WHERE grant_id=?", (grant_id,)).fetchone()
        return {"version": 1, "peerChats": bool(row and row["peer_chats"])}

    def set_preferences(self, grant_id: str, *, peer_chats: Any):
        self._grant(grant_id)
        if type(peer_chats) is not bool:
            raise ManagedNotificationError("notification_preferences_invalid", 422)
        with self._db() as db:
            db.execute("INSERT INTO preferences VALUES(?,?) ON CONFLICT(grant_id) DO UPDATE SET peer_chats=excluded.peer_chats",
                       (grant_id, int(peer_chats)))
        return self.preferences(grant_id)

    def _is_peer_chat(self, profile: str, session_id: str) -> bool:
        """Agents talking to each other: Hermes' canonical Bot Chat session."""
        try:
            row = self._session(profile, session_id)
        except ManagedNotificationError:
            return False
        title = row.get("title")
        return isinstance(title, str) and title.strip() == _PEER_CHAT_TITLE

    def _session(self, profile: str, session_id: str):
        _identifier(profile, _PROFILE); _identifier(session_id)
        def read(db):
            row = db.get_session(session_id)
            if not isinstance(row, dict) or row.get("id") != session_id:
                raise ManagedNotificationError("notification_session_unknown", 404)
            # Public store profile metadata, never current selected UI state.
            owner = row.get("profile_name")
            if owner != profile or row.get("deleted_at") or row.get("archived_at"):
                raise ManagedNotificationError("notification_session_forbidden", 403)
            return row
        try:
            return self.session_opener(profile, read, read_only=True)
        except (LookupError, OSError, sqlite3.Error) as error:
            raise ManagedNotificationError("notification_session_unavailable", 503) from error

    def subscribe(self, grant_id: str, profile: str, session_id: str, enabled: bool):
        grant = self.enrollment(grant_id)["grant"]
        if profile != grant["profile"] or type(enabled) is not bool:
            raise ManagedNotificationError("notification_scope_forbidden", 403)
        self._session(profile, session_id)
        reference = session_reference(profile, session_id)
        with self._db() as db:
            if enabled:
                count = db.execute("SELECT COUNT(*) FROM subscriptions WHERE grant_id=?", (grant_id,)).fetchone()[0]
                if count >= 128 and not db.execute("SELECT 1 FROM subscriptions WHERE grant_id=? AND profile=? AND session_id=?", (grant_id, profile, session_id)).fetchone():
                    raise ManagedNotificationError("notification_session_limit")
                db.execute("INSERT OR IGNORE INTO subscriptions VALUES(?,?,?,?)", (grant_id, profile, session_id, reference))
            else:
                db.execute("UPDATE approval_attention SET state='retired',reason='unsubscribed' WHERE grant_id=? AND profile=? AND session_id=?", (grant_id, profile, session_id))
                db.execute("DELETE FROM subscriptions WHERE grant_id=? AND profile=? AND session_id=?", (grant_id, profile, session_id))
                db.execute("DELETE FROM pending WHERE grant_id=? AND session_ref=?", (grant_id, reference))
                db.execute("DELETE FROM activities WHERE grant_id=? AND session_ref=?", (grant_id, reference))
        return {"version": 1, "grantId": grant_id, "profile": profile, "sessionId": session_id, "sessionReference": reference, "enabled": enabled}

    def work_snapshot(self, grant_id: str, profile: str, session_id: str):
        """Read only this process's public-hook observations, never DB starts.

        A current session observation is not correlation with a mobile frame.
        Retained older turns remain addressable by explicit activity registration.
        """
        grant = self.enrollment(grant_id)["grant"]
        if profile != grant["profile"]:
            raise ManagedNotificationError("notification_scope_forbidden", 403)
        self._session(profile, session_id)
        with self._lock, self._db() as db:
            self._require_subscription(db, grant_id, profile, session_id)
            work = next((value for key, value in reversed(self._work.items())
                         if key[:2] == (profile, session_id)), None)
            snapshot = None
            if work is not None:
                phase, count, terminal = self._work_projection(work)
                snapshot = {"profile": profile, "sessionId": session_id,
                            "turnId": work["turn"], "phase": phase,
                            "activeSubagentCount": count, "terminal": terminal,
                            "outcome": work["outcome"], "observedAt": work["observed_at"]}
            return {"version": 1, "grantId": grant_id, "work": snapshot}

    def _require_subscription(self, db, grant_id: str, profile: str, session_id: str):
        # Recheck local authority after cloud/SessionDB awaits, in the transaction
        # used by the read/write. A concurrent removal cannot resurrect authority.
        # An active grant covers every session in its profile; per-session
        # rows remain as explicit opt-ins but are no longer required.
        if not db.execute("SELECT 1 FROM grants g WHERE g.grant_id=? AND g.state='active' "
                          "AND g.expires>? AND json_extract(g.public_json,'$.profile')=?",
                          (grant_id, int(self.clock()), profile)).fetchone():
            raise ManagedNotificationError("notification_session_not_subscribed")

    @staticmethod
    def _work_projection(work):
        count = sum(state == "active" for state in work["children"].values())
        outcome = work["outcome"]
        phase = "delegating" if count else ("completed" if outcome == "cancelled" else (outcome if outcome in ("completed", "failed") else work["phase"]))
        return phase, min(count, 99), outcome is not None and count == 0

    def event(self, grant_id: str, event_id: str):
        self.enrollment(grant_id)
        if not re.fullmatch(re.escape(grant_id) + r":[0-9a-f]{64}", event_id):
            raise ManagedNotificationError("notification_event_unknown", 404)
        with self._db() as db:
            row = db.execute("SELECT detail_json FROM events WHERE event_id=? AND grant_id=?", (event_id, grant_id)).fetchone()
        if not row:
            raise ManagedNotificationError("notification_event_unknown", 404)
        event = json.loads(row["detail_json"])
        self._session(event["profile"], event["sessionId"])
        return {"version": 1, "event": event}

    def subscribe_activity(self, grant_id: str, activity_id: str, profile: str, session_id: str, reference: str, lease_expires: int, turn_id: str | None = None):
        _identifier(activity_id)
        if turn_id is not None: _identifier(turn_id)
        grant = self.enrollment(grant_id)["grant"]
        self._session(profile, session_id)
        if profile != grant["profile"] or reference != session_reference(profile, session_id):
            raise ManagedNotificationError("notification_scope_forbidden", 403)
        receipt = self._request("GET", grant_id, "/live-activities/" + activity_id).get("activity")
        if not isinstance(receipt, dict) or receipt.get("grantId") != grant_id or receipt.get("activityId") != activity_id or receipt.get("sessionReference") != reference or receipt.get("status") != "active" or receipt.get("leaseExpires") != lease_expires or type(lease_expires) is not int or lease_expires <= int(self.clock()):
            raise ManagedNotificationError("notification_activity_unconfirmed")
        with self._lock:
            with self._db() as db:
                self._require_subscription(db, grant_id, profile, session_id)
                prior = db.execute("SELECT * FROM activities WHERE activity_id=?", (activity_id,)).fetchone()
                if prior and (prior["grant_id"] != grant_id or prior["session_ref"] != reference
                              or prior["profile"] != profile or prior["session_id"] != session_id
                              or prior["state"] not in ("active", "terminal_pending", "terminal_accepted")
                              or (turn_id is not None and prior["work_turn"] not in (None, turn_id))):
                    raise ManagedNotificationError("notification_activity_conflict")
                if not prior and db.execute("SELECT COUNT(*) FROM activities WHERE lease_expires>?", (int(self.clock()),)).fetchone()[0] >= 128:
                    raise ManagedNotificationError("notification_activity_limit")
                # Explicit delayed registration must select that retained observed
                # turn, never today's current turn. A retry preserves its owner.
                turn = prior["work_turn"] if prior and prior["work_turn"] is not None else turn_id
                if turn is not None:
                    work = self._work.get((profile, session_id, turn))
                    if work is None:
                        raise ManagedNotificationError("notification_work_unobserved")
                else:
                    work = next((value for key, value in reversed(self._work.items())
                                 if key[:2] == (profile, session_id) and not value["terminal"]
                                 and value["outcome"] is None), None)
                    turn = work["turn"] if work else None
                db.execute("INSERT INTO activities(activity_id,grant_id,profile,session_id,session_ref,lease_expires,work_turn,state) VALUES(?,?,?,?,?,?,?,'active') ON CONFLICT(activity_id) DO UPDATE SET lease_expires=excluded.lease_expires,work_turn=COALESCE(activities.work_turn,excluded.work_turn)", (activity_id, grant_id, profile, session_id, reference, lease_expires, turn))
            # Commit the owner first, then enqueue under the same observation lock.
            # No later hook is needed (including a turn ending before token arrival).
            # Terminal pending/accepted retries keep their original frozen request.
            if work is not None:
                phase, count, terminal = self._work_projection(work)
                self._queue_activity(profile, session_id, work["turn"], phase, count, terminal, stopped=work["outcome"] == "cancelled" and terminal)
        return {"version": 1, "activityId": activity_id, "grantId": grant_id, "sessionReference": reference, "state": "subscribed"}

    def remove_activity(self, grant_id: str, activity_id: str):
        self._grant(grant_id); _identifier(activity_id)
        with self._db() as db:
            db.execute("DELETE FROM activities WHERE activity_id=? AND grant_id=?", (activity_id, grant_id))
            db.execute("DELETE FROM pending WHERE activity_id=? AND grant_id=?", (activity_id, grant_id))
        return {"version": 1, "activityId": activity_id, "grantId": grant_id, "state": "removed"}

    def owns_alert(self, event: Any, device_id: str) -> bool:
        # Native observer attention never steals the legacy approval transport.
        if getattr(event, "type", None) not in {"session.completed", "session.failed"}: return False
        with self._db() as db:
            rows = db.execute("SELECT g.public_json FROM grants g WHERE g.state='active' AND g.expires>? AND json_extract(g.public_json,'$.profile')=?", (int(self.clock()), event.profile)).fetchall()
        return any(event.type in json.loads(row["public_json"])["eventTypes"] for row in rows)

    @staticmethod
    def _approval_event_id(grant_id: str, profile: str, session_id: str, turn_id: str, tool_call_id: str):
        # Tool-scoped attention, NOT the identity of a native approval request.
        digest = hashlib.sha256(canonical_json_bytes(
            [profile, session_id, turn_id, tool_call_id, _APPROVAL_EVENT])).hexdigest()
        return grant_id + ":" + digest

    def _retire_approval_scope(self, profile: str, session_id: str, turn_id: str,
                               tool_call_id: str, reason: str):
        """Empty tool is an exact turn-end tombstone; never an invented turn."""
        now = int(self.clock())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT g.* FROM grants g WHERE g.state='active' AND g.expires>? AND json_extract(g.public_json,'$.profile')=?", (now, profile)).fetchall()
            for row in rows:
                grant = json.loads(row["public_json"])
                if _APPROVAL_EVENT not in grant["eventTypes"]: continue
                scope = (grant["grantId"], profile, session_id, turn_id)
                where = "grant_id=? AND profile=? AND session_id=? AND turn_id=?"
                if tool_call_id:
                    where += " AND tool_call_id=?"
                    scope += (tool_call_id,)
                db.execute(f"UPDATE approval_attention SET state='retired',reason=? WHERE {where}", (reason, *scope))
                db.execute(f"UPDATE pending SET state='retired' WHERE state IN ('pending','sending') AND intent_id IN (SELECT event_id FROM approval_attention WHERE {where})", scope)
                # Also fence response-before-pre and delayed pre after turn end.
                event_id = self._approval_event_id(grant["grantId"], profile, session_id, turn_id, tool_call_id)
                db.execute("INSERT OR IGNORE INTO approval_attention SELECT ?,?,?,?,?,?,?,'retired',?,? WHERE (SELECT COUNT(*) FROM approval_attention WHERE grant_id=?)<?",
                           (event_id, grant["grantId"], profile, session_id, turn_id, tool_call_id,
                            self._approval_owner, now, reason, grant["grantId"], _APPROVAL_LIMIT))
        self._wake.set()

    def _observe_approval(self, hook: str, *, profile: str, **payload: Any):
        # The native gateway observer's session_id is the stored observability
        # identity. session_key is routing identity and must NEVER substitute.
        if payload.get("surface") != "gateway" or payload.get("coalesced", False) is not False:
            return
        if payload.get("parent_session_id") or payload.get("platform") == "subagent": return
        profile = _identifier(profile, _PROFILE)
        if "profile_name" in payload and payload["profile_name"] != profile: return
        session_id = _identifier(payload.get("session_id"))
        turn_id = _identifier(payload.get("turn_id"))
        tool_call_id = _identifier(payload.get("tool_call_id"))
        if hook == "post_approval_response":
            # Every gateway disposition retires; never persist command/choice text.
            self._retire_approval_scope(profile, session_id, turn_id, tool_call_id, "response")
            return
        self._session(profile, session_id)
        content = self._rich_text(payload.get("description") or payload.get("command"))
        self._queue_event(profile, session_id, turn_id, _APPROVAL_EVENT,
                          tool_call_id=tool_call_id, content_text=content)

    def _observe_question(self, profile: str, session_id: str, payload: dict[str, Any]):
        """The agent asked a question with the clarify tool.

        Chats in the bighelp app run on the dashboard, where no adapter presents
        the question, so this is the only place that sees it. Each question alerts
        once, as it is asked; it is shown in the chat, not in the alert's actions.
        """
        turn, call = payload.get("turn_id"), payload.get("tool_call_id")
        if not isinstance(turn, str) or not _ID.fullmatch(turn): return
        if not isinstance(call, str) or not _ID.fullmatch(call): return
        question = _clarify_question(payload.get("args"))
        if not question: return
        try:
            self._queue_event(profile, session_id, turn, _CLARIFICATION_EVENT,
                              event_key="tool:" + call, content_text=question)
        except ManagedNotificationError:
            # No avatar or no text: the question still waits in the chat.
            logger.warning("Question notification unavailable")
            return
        with self._lock:
            self._asked[(profile, session_id, turn)] = self.clock()
            self._asked.move_to_end((profile, session_id, turn))
            while len(self._asked) > 256:
                self._asked.popitem(last=False)

    @staticmethod
    def _rich_text(value: Any) -> str:
        if isinstance(value, str):
            text = " ".join(value.split())
        elif isinstance(value, dict):
            text = " ".join(" ".join(str(value.get(key) or "").split())
                            for key in ("text", "content", "message", "error"))
            text = " ".join(text.split())
        else:
            text = ""
        if not text:
            raise ManagedNotificationError("notification_rich_content_required", 422)
        return text if len(text) <= _MAX_RICH_TEXT else text[:_MAX_RICH_TEXT - 1].rstrip() + "…"

    @staticmethod
    def _agent_presentation(profile: str) -> tuple[str, dict[str, Any]]:
        from .adapter import profile_display_name
        from tui_gateway.server import handle_request

        name = profile_display_name(profile)
        response = handle_request({"jsonrpc": "2.0", "id": "loopdy-notification-avatar",
                                   "method": "profiles.get_asset",
                                   "params": {"name": profile, "asset": "avatar"}})
        result = response.get("result") if isinstance(response, dict) else None
        if not isinstance(result, dict) or result.get("found") is not True:
            raise ManagedNotificationError("notification_agent_avatar_required", 422)
        mime = result.get("mime")
        size = result.get("size")
        data_url = result.get("data")
        if (mime not in {"image/png", "image/jpeg", "image/webp"}
                or type(size) is not int or not 1 <= size <= _MAX_AVATAR_BYTES
                or not isinstance(data_url, str) or not data_url.startswith(f"data:{mime};base64,")):
            raise ManagedNotificationError("notification_agent_avatar_too_large", 422)
        try:
            blob = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        except (ValueError, TypeError) as error:
            raise ManagedNotificationError("notification_agent_avatar_invalid", 422) from error
        if len(blob) != size:
            raise ManagedNotificationError("notification_agent_avatar_invalid", 422)
        return name, {"mimeType": mime, "sha256": hashlib.sha256(blob).hexdigest(), "data": data_url}

    def publish_observed_clarification(self, *, profile: str, session_id: str,
                                       request_id: str, question: str) -> None:
        """Bind a presented prompt to the one exact live Hermes hook turn.

        The adapter has already resolved ``session_id`` from Hermes' public
        session index. The turn comes only from the matching ``pre_llm_call``
        observation; no routing key, request id, timestamp, or generated value is
        accepted as a substitute.
        """
        profile = _identifier(profile, _PROFILE)
        session_id = _identifier(session_id)
        self._session(profile, session_id)
        with self._lock:
            turns = [key[2] for key, work in self._work.items()
                     if key[:2] == (profile, session_id)
                     and not work["terminal"] and work["outcome"] is None]
        if len(turns) != 1:
            raise ManagedNotificationError("notification_clarification_turn_unavailable", 409)
        self.publish_clarification(
            profile=profile,
            session_id=session_id,
            turn_id=turns[0],
            request_id=request_id,
            question=question,
        )

    def publish_clarification(self, *, profile: str, session_id: str, turn_id: str,
                              request_id: str, question: str) -> None:
        """Producer seam for the existing clarification presenter.

        The parent adapter wiring calls this only after Hermes emitted an actionable
        native clarification for the exact stored session. It performs no network I/O.
        """
        profile = _identifier(profile, _PROFILE)
        with self._lock:
            if profile not in self._clarification_profiles:
                raise ManagedNotificationError("notification_clarification_producer_unavailable", 503)
        session_id = _identifier(session_id)
        turn_id = _identifier(turn_id)
        request_id = _identifier(request_id)
        with self._lock:
            if self._asked.get((profile, session_id, turn_id), 0) > self.clock() - _ASKED_WINDOW_SECONDS:
                return
        self._session(profile, session_id)
        self._queue_event(profile, session_id, turn_id, _CLARIFICATION_EVENT,
                          event_key=request_id, content_text=self._rich_text(question))

    def _queue_event(self, profile: str, session_id: str, turn_id: str, event_type: str,
                     *, tool_call_id: str | None = None, event_key: str = "",
                     content_text: str):
        approval = event_type == _APPROVAL_EVENT
        tool_call_id = _identifier(tool_call_id) if approval else ""
        content_text = self._rich_text(content_text)
        agent_name, avatar = self._agent_presentation(profile)
        now = int(self.clock())
        queued: list[str] = []
        peer_chat = event_type in _PEER_QUIET_EVENTS and self._is_peer_chat(profile, session_id)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT g.* FROM grants g WHERE g.state='active' AND g.expires>? AND json_extract(g.public_json,'$.profile')=?", (now, profile)).fetchall()
            for row in rows:
                grant = json.loads(row["public_json"])
                if event_type not in grant["eventTypes"]: continue
                if peer_chat and not db.execute("SELECT 1 FROM preferences WHERE grant_id=? AND peer_chats=1", (grant["grantId"],)).fetchone():
                    continue
                digest = hashlib.sha256(canonical_json_bytes(
                    [profile, session_id, turn_id, event_type, event_key])).hexdigest()
                event_id = self._approval_event_id(grant["grantId"], profile, session_id, turn_id, tool_call_id) if approval else grant["grantId"] + ":" + digest
                if db.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone(): continue
                if approval:
                    turn_end = self._approval_event_id(grant["grantId"], profile, session_id, turn_id, "")
                    if db.execute("SELECT 1 FROM approval_attention WHERE event_id IN (?,?)", (event_id, turn_end)).fetchone(): continue
                    if db.execute("SELECT COUNT(*) FROM approval_attention WHERE grant_id=?", (grant["grantId"],)).fetchone()[0] >= _APPROVAL_LIMIT: continue
                if db.execute("SELECT COUNT(*) FROM events WHERE grant_id=?", (grant["grantId"],)).fetchone()[0] >= 4096: continue
                if db.execute("SELECT COUNT(*) FROM pending WHERE grant_id=? AND state IN ('pending','sending')", (grant["grantId"],)).fetchone()[0] >= 32: continue
                # Alerts leave this host only sealed for the phone. Until the phone has
                # registered its key (it does right after enrolling), there's no alert.
                recipient = db.execute("SELECT public_key FROM recipients WHERE grant_id=?", (grant["grantId"],)).fetchone()
                if not recipient:
                    logger.info("Managed notification skipped: the phone's content key isn't registered yet")
                    continue
                content_kind = ("approval" if approval else "clarification" if event_type == _CLARIFICATION_EVENT
                                else "scheduled" if event_type.startswith("scheduled.")
                                else "subagent" if event_type.startswith("subagent.")
                                else "failure" if event_type == "session.failed" else "reply")
                detail = {"eventId": event_id, "eventType": event_type, "profile": profile,
                          "sessionId": session_id, "turnId": turn_id, "occurredAt": now,
                          "agent": {"id": profile, "name": agent_name,
                                    "avatarSha256": avatar["sha256"]},
                          "content": {"kind": content_kind, "text": content_text}}
                expires = min(now + (_APPROVAL_TTL_SECONDS if approval else 300 if event_type == _CLARIFICATION_EVENT else 900), grant["expiresAt"])
                if approval:
                    db.execute("INSERT INTO approval_attention VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (event_id, grant["grantId"], profile, session_id, turn_id, tool_call_id,
                                self._approval_owner, "pending", expires, "observed"))
                reference = session_reference(profile, session_id)
                # End to end: only the enrolled phone can read the name, text and avatar.
                raw = self._sealed_event(db, grant["grantId"], recipient["public_key"], event_id=event_id,
                    event_type=event_type, session_reference=reference, turn_id=turn_id, occurred_at=now,
                    title=agent_name, text=content_text, avatar=avatar)
                db.execute("INSERT INTO events VALUES(?,?,?,?)",
                           (event_id, grant["grantId"], canonical_json_bytes(detail).decode(), now))
                due = now + _APPROVAL_GRACE_SECONDS if approval else now
                # A device with bighelp open gets it directly first; the push waits for its ack.
                due = live_alerts.offer(db, grant["grantId"], event_id, due, self.clock())
                db.execute("INSERT INTO pending(intent_id,grant_id,path,raw,expires,state,next_attempt,session_ref) VALUES(?,?,?,?,?,'pending',?,?)",
                           (event_id, grant["grantId"], "/events", raw, expires, due, reference))
                if not approval:
                    queued.append(event_id)
        self._wake.set()
        # Approvals wait their grace period and stay with the sender that owns them.
        if queued and self._send_on_queue is not None:
            self._send_on_queue(lambda: self._send_now(queued))

    def observe(self, hook: str, *, profile: str, **payload: Any):
        """Synchronous stock hook: persist only; never perform network I/O here."""
        try:
            self._observe(hook, profile=profile, **payload)
        except (ValueError, OSError, sqlite3.Error):
            logger.warning("Managed notification lifecycle observation unavailable")

    def _observe(self, hook: str, *, profile: str, **payload: Any):
        if self._stop.is_set(): return
        if hook in _APPROVAL_HOOKS:
            self._observe_approval(hook, profile=profile, **payload)
            return
        if hook not in ("pre_llm_call", "post_llm_call", "pre_tool_call", "post_tool_call",
                        "on_session_end", "subagent_start", "subagent_stop"):
            return
        profile = _identifier(payload.get("profile_name") or profile, _PROFILE)
        child_hook = hook in ("subagent_start", "subagent_stop")
        session_id = payload.get("parent_session_id") if child_hook else payload.get("session_id")
        if not isinstance(session_id, str) or not _ID.fullmatch(session_id): return
        if not child_hook and (payload.get("parent_session_id") or payload.get("platform") == "subagent"): return
        with self._db() as db:
            if not db.execute("SELECT 1 FROM grants g WHERE g.state='active' AND g.expires>? AND json_extract(g.public_json,'$.profile')=?", (int(self.clock()), profile)).fetchone(): return
        if hook == "pre_tool_call" and payload.get("tool_name") == "clarify" and not child_hook:
            self._observe_question(profile, session_id, payload)
        turn = payload.get("turn_id")
        if hook == "post_llm_call" and isinstance(turn, str) and _ID.fullmatch(turn):
            coordinate = (profile, session_id, turn)
            scheduled = payload.get("platform") == "cron" or _CRON_SESSION.fullmatch(session_id) is not None
            try:
                # A turn Hermes keeps silent keeps no reply here either, so neither this alert
                # nor the on_session_end fallback pushes a marker. Failures still alert. The
                # check reads the raw reply: _rich_text collapses the lines the loose rule reads.
                reply = _delivered_reply(
                    payload.get("assistant_response"), history=payload.get("conversation_history"),
                    autonomous=scheduled or payload.get("platform") in _AUTONOMOUS_PLATFORMS,
                    group_chat=payload.get("platform") == _GROUP_CHAT_PLATFORM)
                # Cards in the reply read as words, never as their JSON (card_previews.py).
                response_text = (self._rich_text(reply_preview(reply, payload.get("conversation_history")))
                                 if reply else "")
            except ManagedNotificationError:
                response_text = ""
            if response_text:
                with self._lock:
                    self._responses[coordinate] = response_text
                    self._responses.move_to_end(coordinate)
                    while len(self._responses) > 256:
                        self._responses.popitem(last=False)
            # Hermes fires post_llm_call once per finished, uninterrupted turn, right after the
            # reply is saved. on_session_end comes only after post-turn work (external memory
            # sync, reviews) that can take several seconds, so the reply alert goes out now.
            if response_text and not child_hook:
                try:
                    self._queue_event(profile, session_id, turn,
                                      "scheduled.completed" if scheduled else "session.completed",
                                      content_text=response_text)
                except (ValueError, OSError):
                    logger.warning("Notification presentation unavailable; work state remains authoritative")
                else:
                    with self._lock:
                        self._alerted[coordinate] = True
                        while len(self._alerted) > 256:
                            self._alerted.popitem(last=False)
        if not child_hook and isinstance(turn, str) and _ID.fullmatch(turn):
            if hook == "on_session_end":
                self._retire_approval_scope(profile, session_id, turn, "", "turn_end")
            elif hook == "post_tool_call":
                tool = payload.get("tool_call_id")
                if isinstance(tool, str) and _ID.fullmatch(tool):
                    self._retire_approval_scope(profile, session_id, turn, tool, "tool_end")
        if (hook == "on_session_end" and isinstance(turn, str) and _ID.fullmatch(turn)
                and payload.get("interrupted") is not True):
            with self._lock:
                content = self._responses.pop((profile, session_id, turn), "")
                alerted = self._alerted.pop((profile, session_id, turn), False)
            # Alert presentation must not prevent the authoritative work/Activity
            # terminal below, including turns with no text or unavailable avatars.
            # A turn already alerted from post_llm_call gets no second alert, not even
            # "failed": that alert already carried the reply Hermes wrote about it.
            try:
                if alerted: raise _AlreadyAlerted()
                scheduled = payload.get("platform") == "cron" or _CRON_SESSION.fullmatch(session_id) is not None
                if payload.get("failed") is True:
                    content = self._rich_text(payload.get("error") or content)
                    self._queue_event(profile, session_id, turn,
                                      "scheduled.failed" if scheduled else "session.failed",
                                      content_text=content)
                elif payload.get("completed") is True and content:
                    self._queue_event(profile, session_id, turn,
                                      "scheduled.completed" if scheduled else "session.completed",
                                      content_text=content)
            except _AlreadyAlerted:
                pass
            except (ValueError, OSError):
                logger.warning("Notification presentation unavailable; work state remains authoritative")
        with self._lock:
            if child_hook:
                child = payload.get("child_session_id") or payload.get("child_subagent_id")
                turn = self._child_owners.get((profile, session_id, child)) if isinstance(child, str) else None
                if turn is None and hook == "subagent_start": turn = payload.get("parent_turn_id")
            if not isinstance(turn, str) or not _ID.fullmatch(turn): return
            coordinate = (profile, session_id, turn)
            work = self._work.get(coordinate)
            if hook == "pre_llm_call" and isinstance(turn, str) and _ID.fullmatch(turn):
                if work is None or work["turn"] != turn:
                    if len(self._work) >= 128:
                        # Never discard a live cohort or an activity's retained
                        # canonical owner merely because newer work appeared.
                        with self._db() as db:
                            bound = {(row["profile"], row["session_id"], row["work_turn"])
                                     for row in db.execute("SELECT profile,session_id,work_turn FROM activities WHERE lease_expires>?", (int(self.clock()),))}
                        evicted = next((key for key, value in self._work.items()
                                        if value["terminal"] and key not in bound), None)
                        if evicted is None: return
                        self._work.pop(evicted)
                        for child_key in tuple(self._child_owners):
                            if child_key[:2] == evicted[:2] and self._child_owners[child_key] == evicted[2]:
                                self._child_owners.pop(child_key, None)
                                self._child_goals.pop(child_key, None)
                    work = {"turn": turn, "phase": "thinking", "outcome": None, "children": {}, "terminal": False,
                            "observed_at": int(self.clock())}
                    self._work[coordinate] = work
                elif work["terminal"]: return
            if work is None or work["terminal"]: return
            if child_hook:
                child = payload.get("child_session_id") or payload.get("child_subagent_id")
                parent_turn = payload.get("parent_turn_id")
                if not isinstance(child, str) or not _ID.fullmatch(child): return
                owner_key = (profile, session_id, child)
                if hook == "subagent_start":
                    if not isinstance(parent_turn, str) or parent_turn != work["turn"]: return
                    if len(work["children"]) >= 128 and child not in work["children"]: return
                    self._child_owners.setdefault(owner_key, parent_turn)
                    if self._child_owners[owner_key] != work["turn"]: return
                    goal = payload.get("child_goal")
                    if isinstance(goal, str):
                        goal = " ".join(goal.split())
                        if goal:
                            self._child_goals.setdefault(owner_key, goal[:400])
                    # A repeated start after stop cannot resurrect this child.
                    work["children"].setdefault(child, "active")
                else:
                    if self._child_owners.get(owner_key) != work["turn"] or work["children"].get(child) != "active": return
                    work["children"][child] = "ended"
                    status = payload.get("child_status")
                    goal = payload.get("child_goal")
                    if not isinstance(goal, str) or not " ".join(goal.split()):
                        goal = self._child_goals.get(owner_key, "")
                    goal = " ".join(goal.split()) if isinstance(goal, str) else ""
                    if status in ("completed", "failed") and goal:
                        self._queue_event(
                            profile, session_id, turn,
                            "subagent.completed" if status == "completed" else "subagent.failed",
                            event_key=child,
                            content_text=f"{goal} — {status}",
                        )
                    self._child_goals.pop(owner_key, None)
            elif turn != work["turn"] or work["terminal"] or work["outcome"] is not None: return
            elif hook == "on_session_end":
                if payload.get("interrupted") is True:
                    work["outcome"] = "cancelled"
                elif payload.get("failed") is True:
                    work["outcome"] = "failed"
                    # Significant alert already persisted independently of activity state.
                elif payload.get("completed") is True:
                    work["outcome"] = "completed"
                    # Significant alert already persisted independently of activity state.
                else: return
            elif hook == "post_llm_call":
                work["phase"] = "responding"
            elif hook in ("pre_tool_call", "post_tool_call"):
                work["phase"] = "using_tool"
            work["observed_at"] = int(self.clock())
            phase, active_count, terminal = self._work_projection(work)
            work["terminal"] = terminal
            # Rich-v1 uses completed as the terminal transport state. Stopped
            # remains explicit fixed copy; cancellation never queues an alert.
            self._queue_activity(profile, session_id, work["turn"], phase, active_count, terminal,
                                 allow_bind=hook == "pre_llm_call", stopped=work["outcome"] == "cancelled" and terminal)

    def _queue_activity(self, profile: str, session_id: str, turn: str, phase: str, count: int, terminal: bool, *, allow_bind: bool = False, stopped: bool = False):
        now = int(self.clock())
        with self._db() as db:
            rows = db.execute("SELECT a.*,g.expires AS grant_expires FROM activities a JOIN grants g USING(grant_id) WHERE a.profile=? AND a.session_id=? AND a.state='active' AND a.lease_expires>? AND g.state='active' AND g.expires>?", (profile, session_id, now, now)).fetchall()
            for row in rows:
                if row["work_turn"] not in (None, turn) or (row["work_turn"] is None and not allow_bind): continue
                action = "Stopped" if stopped and terminal and phase == "completed" else _ACTIONS[phase]
                signature = json.dumps([turn, phase, count, action])
                # A real hook re-sends an unchanged state once a minute, inside the
                # 120-second stale window, so a long run of tool calls stays current.
                if row["last_signature"] == signature and row["last_queued_at"] > now - _ACTIVITY_REFRESH_SECONDS: continue
                # Queue latest significant state, with relay's existing 30s budget.
                pending = db.execute("SELECT raw,next_attempt FROM pending WHERE activity_id=? AND state='pending' ORDER BY next_attempt LIMIT 1", (row["activity_id"],)).fetchone()
                timestamp = max(now, json.loads(bytes(pending["raw"]))["timestamp"]) if pending else max(now, row["last_timestamp"] + 1)
                expires = min(timestamp + 120, row["grant_expires"], row["lease_expires"])
                if expires <= timestamp: continue
                update_id = b64url_encode(hashlib.sha256(canonical_json_bytes([row["grant_id"], row["activity_id"], turn, signature, timestamp])).digest())
                update = {"version": 2, "updateId": update_id, "sessionReference": row["session_ref"], "phase": phase,
                          "currentAction": action, "progress": 100 if terminal else 0, "completedSteps": 0,
                          "activeSubagentCount": count, "latestTool": None, "timestamp": timestamp, "expires": expires}
                due = now if terminal or phase == "waiting" else max(now, pending["next_attempt"] if pending else row["last_queued_at"] + 30)
                # Only nonterminal updates may be superseded; terminal is durable.
                db.execute("DELETE FROM pending WHERE activity_id=? AND state='pending'", (row["activity_id"],))
                db.execute("INSERT INTO pending(intent_id,grant_id,path,raw,expires,state,next_attempt,session_ref,activity_id) VALUES(?,?,?,?,?,'pending',?,?,?)", (update_id, row["grant_id"], "/live-activities/" + row["activity_id"] + "/updates", canonical_json_bytes(update), expires, due, row["session_ref"], row["activity_id"]))
                db.execute("UPDATE activities SET work_turn=?,state=?,last_timestamp=?,last_signature=?,last_queued_at=? WHERE activity_id=?", (turn, "terminal_pending" if terminal else "active", timestamp, signature, due, row["activity_id"]))
        self._wake.set()

    def producer_loaded(self, profile: str, *, start_worker: bool = True,
                        approval_hooks_loaded: bool = False,
                        clarification_producer_loaded: bool = False):
        with self._lock:
            self._loaded_profiles.add(profile)
            if approval_hooks_loaded:
                self._approval_profiles.add(profile)
            if clarification_producer_loaded:
                self._clarification_profiles.add(profile)
            if start_worker and self._worker is None:
                lock_path = self.directory / "worker.lock"
                fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                lock = os.fdopen(fd, "a+b")
                try: self._fcntl.flock(lock, self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)
                except BlockingIOError:
                    lock.close(); return
                self._worker_lock = lock
                self._worker = threading.Thread(target=self._run, name="loopdy-managed-notifications", daemon=True)
                self._worker.start()

    def close(self):
        self._stop.set(); self._wake.set()
        if self._worker is not None: self._worker.join(timeout=15)
        with self._lock:
            self._loaded_profiles.clear()
            self._approval_profiles.clear()
            self._clarification_profiles.clear()
            self._child_goals.clear()
            if self._worker_lock is not None and (self._worker is None or not self._worker.is_alive()):
                self._worker_lock.close(); self._worker_lock = None

    def _run(self):
        while not self._stop.is_set():
            try: self.queue_sign_in_wakes(); self.drain_pending()
            except (ValueError, OSError, sqlite3.Error): logger.warning("Managed notification journal unavailable")
            # Short wait: another Hermes process may have queued the alert (this process
            # owns the sender), and its wake-up doesn't reach this thread.
            self._wake.wait(1); self._wake.clear()

    def _approval_transport_ready(self, row) -> bool:
        """Last local fence after signing, immediately before the HTTPS call.

        Do not hold a lock across network I/O: observers must not delay the
        native decision. A decision after this fence can still race admission;
        an accepted push is not recallable and only states a past-tense fact.
        """
        if self._stop.is_set(): return False
        with self._db() as db:
            attention = db.execute("SELECT * FROM approval_attention WHERE event_id=? AND grant_id=?",
                                   (row["intent_id"], row["grant_id"])).fetchone()
        if not attention or attention["owner"] != self._approval_owner or attention["state"] != "pending": return False
        try:
            self._session(attention["profile"], attention["session_id"])
        except (ValueError, OSError, sqlite3.Error):
            return False
        now = int(self.clock())
        with self._db() as db:
            current = db.execute("SELECT g.public_json,p.raw,p.session_ref FROM pending p "
                "JOIN approval_attention a ON a.event_id=p.intent_id AND a.grant_id=p.grant_id "
                "JOIN grants g ON g.grant_id=a.grant_id "
                "JOIN subscriptions s ON s.grant_id=a.grant_id AND s.profile=a.profile AND s.session_id=a.session_id "
                "WHERE p.intent_id=? AND p.state='sending' AND p.expires>? AND a.state='pending' AND a.owner=? AND a.expires>? "
                "AND g.state='active' AND g.expires>?",
                (row["intent_id"], now, self._approval_owner, now, now)).fetchone()
        if not current or self._stop.is_set(): return False
        grant = json.loads(current["public_json"])
        return (grant["profile"] == attention["profile"] and _APPROVAL_EVENT in grant["eventTypes"]
                and bytes(current["raw"]) == bytes(row["raw"])
                and current["session_ref"] == session_reference(attention["profile"], attention["session_id"]))

    def queue_sign_in_wakes(self):
        """A quiet push every few hours asks each enrolled phone to renew its sign-in.
        One per grant per interval: the interval is in the intent ID."""
        now = int(self.clock())
        interval = now // _SIGN_IN_WAKE_SECONDS
        raw = canonical_json_bytes({"version": 1, "reason": "renew-sign-in"})
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO pending(intent_id,grant_id,path,raw,expires,state,next_attempt,session_ref) "
                       "SELECT 'wake:'||g.grant_id||':'||?,g.grant_id,?,?,?,'pending',?,'' FROM grants g "
                       "JOIN recipients r USING(grant_id) WHERE g.state='active' AND g.expires>?",
                       (interval, _SIGN_IN_WAKE_PATH, raw, now + 3600, now, now))

    def drain_pending(self):
        """Bounded durable retry, exposed for main-owned no-send composition tests."""
        now = int(self.clock())
        with self._db() as db:
            # Never replay a previous producer's assertion of human waiting.
            # API-only construction is read-only here: retirement happens only in
            # the process-owned drain (not when another API reader opens the DB).
            db.execute("UPDATE approval_attention SET state='retired',reason=CASE WHEN owner!=? THEN 'recovery' ELSE 'timeout' END WHERE state='pending' AND (owner!=? OR expires<=?)", (self._approval_owner, self._approval_owner, now))
            db.execute("UPDATE pending SET state='retired' WHERE state IN ('pending','sending') AND intent_id IN (SELECT event_id FROM approval_attention WHERE state='retired')")
            db.execute("UPDATE pending SET state='pending' WHERE state='sending' AND next_attempt<=?", (now,))
            db.execute("UPDATE pending SET state='expired' WHERE state='pending' AND expires<=?", (now,))
            db.execute("DELETE FROM pending WHERE expires<?", (now - 86400,))
            db.execute("DELETE FROM events WHERE occurred_at<?", (now - 2592000,))
            db.execute("DELETE FROM grants WHERE expires<?", (now - 86400,))
            db.execute("DELETE FROM activities WHERE lease_expires<?", (now - 86400,))
            rows = db.execute("SELECT p.* FROM pending p JOIN grants g USING(grant_id) WHERE p.state='pending' AND p.next_attempt<=? AND p.expires>? AND g.state='active' AND g.expires>? ORDER BY p.next_attempt,p.intent_id LIMIT 32", (now, now, now)).fetchall()
        for row in rows:
            if self._stop.is_set(): return
            self._send_row(row)

    def _send_now(self, intent_ids: list[str]):
        """Sends alerts this process just queued. The claim below lets exactly one
        sender (this one or the owner's drain) deliver each; approvals never come here."""
        try:
            # Alerts offered to a device with bighelp open wait for its ack; the rest go first.
            waiting = live_alerts.offered(self, intent_ids)
            if waiting and len(waiting) < len(intent_ids):
                self._send_now([intent for intent in intent_ids if intent not in waiting])
                intent_ids = waiting
            live_alerts.await_acks(self, intent_ids)
            now = int(self.clock())
            with self._db() as db:
                marks = ",".join("?" * len(intent_ids))
                rows = db.execute(f"SELECT p.* FROM pending p JOIN grants g USING(grant_id) WHERE p.intent_id IN ({marks}) AND p.state='pending' AND p.next_attempt<=? AND p.expires>? AND g.state='active' AND g.expires>? ORDER BY p.intent_id", (*intent_ids, now, now, now)).fetchall()
            for row in rows:
                if self._stop.is_set(): return
                self._send_row(row)
        except (ValueError, OSError, sqlite3.Error):
            logger.warning("Managed notification immediate send unavailable; the sender retries")

    def _send_row(self, row):
        now = int(self.clock())
        with self._db() as db:
            claimed = db.execute("UPDATE pending SET state='sending',next_attempt=? WHERE intent_id=? AND state='pending' AND expires>? AND EXISTS(SELECT 1 FROM grants WHERE grants.grant_id=pending.grant_id AND grants.state='active' AND grants.expires>?)", (now + 30, row["intent_id"], now, now)).rowcount
        if claimed != 1: return
        if row["path"] == "/events" and json.loads(bytes(row["raw"])).get("version") != 3:
            # An unsealed alert queued by an older plugin before the update: never send it.
            with self._db() as db:
                db.execute("UPDATE pending SET state='failed' WHERE intent_id=? AND state='sending'", (row["intent_id"],))
                db.execute("UPDATE approval_attention SET state='retired',reason='send_failed' WHERE event_id=? AND state='pending'", (row["intent_id"],))
            return
        try:
            approval = row["path"] == "/events" and json.loads(bytes(row["raw"])).get("eventType") == _APPROVAL_EVENT
            result = self._request("POST", row["grant_id"], row["path"], bytes(row["raw"]),
                                   before_transport=(lambda: self._approval_transport_ready(row)) if approval else None)
            if result.get("status") not in ("accepted", "duplicate") or not isinstance(result.get("deliveryId"), str):
                raise ManagedNotificationError("notification_delivery_unconfirmed", 503)
        except ManagedNotificationError as error:
            if row["path"] == _SIGN_IN_WAKE_PATH:
                # A wake is a nicety: one refused (a service without wakes answers 404)
                # waits for the next interval and never retires the phone's alerts.
                with self._db() as db:
                    db.execute("UPDATE pending SET state='failed',attempts=attempts+1 WHERE intent_id=? AND state='sending'", (row["intent_id"],))
                return
            if error.status in (403, 404):
                self.remove(row["grant_id"])
            else:
                with self._db() as db:
                    state = "failed" if error.status in (400, 401, 410, 422) else "pending"
                    # A post-hook/removal racing an in-flight failed request
                    # must not resurrect the retired intent.
                    db.execute("UPDATE pending SET state=?,attempts=attempts+1,next_attempt=? WHERE intent_id=? AND state='sending'", (state, now + min(60, 2 ** min(row["attempts"] + 1, 6)), row["intent_id"]))
                    if state == "failed":
                        db.execute("UPDATE approval_attention SET state='retired',reason='send_failed' WHERE event_id=? AND state='pending'", (row["intent_id"],))
            return
        with self._db() as db:
            db.execute("UPDATE pending SET state='accepted' WHERE intent_id=? AND state='sending'", (row["intent_id"],))
            if row["activity_id"] and json.loads(bytes(row["raw"])).get("phase") in ("completed", "failed"):
                db.execute("UPDATE activities SET state='terminal_accepted' WHERE activity_id=? AND state='terminal_pending'", (row["activity_id"],))

_instances: dict[str, ManagedNotifications] = {}
_instances_lock = threading.Lock()
_SHARED_OBSERVATIONS = "_loopdy_managed_notification_observations"


def _clarify_question(args: Any) -> str:
    """What the clarify tool asks: one question, or the first of a batch."""
    if not isinstance(args, dict): return ""
    questions = args.get("questions")
    if isinstance(questions, list) and questions:
        texts = [str((item.get("question") if isinstance(item, dict) else item) or "").strip()
                 for item in questions[:64]]
        texts = [text for text in texts if text]
        if not texts: return ""
        return texts[0] if len(texts) == 1 else f"{texts[0]} (+{len(texts) - 1} more)"
    question = args.get("question")
    return question.strip() if isinstance(question, str) else ""


def _new_observations() -> dict[str, Any]:
    return {"lock": threading.RLock(), "wake": threading.Event(),
            "work": OrderedDict(), "responses": OrderedDict(), "alerted": OrderedDict(), "asked": OrderedDict(),
            "child_owners": {}, "child_goals": {},
            "loaded_profiles": set(), "approval_profiles": set(), "clarification_profiles": set()}


def _shared_observations(directory: Path) -> dict[str, Any]:
    """Live hook state for ``directory``, shared by every copy of this module.

    A dashboard imports this package twice: Hermes' plugin loader as
    ``hermes_plugins.<slug>.loopdy_plugin`` (lifecycle hooks) and the dashboard
    router loader as top-level ``loopdy_plugin`` (the /notifications routes).
    Each copy has its own ``_instances``, so the router's /work read never saw
    the running turn and phones kept their Live Activity local-only. Only plain
    containers, locks and events live here; neither copy sees the other's classes.
    """
    registry = sys.modules.get(_SHARED_OBSERVATIONS)
    if registry is None:
        candidate = types.ModuleType(_SHARED_OBSERVATIONS)
        candidate.lock, candidate.directories = threading.Lock(), {}
        registry = sys.modules.setdefault(_SHARED_OBSERVATIONS, candidate)
    with registry.lock:
        return registry.directories.setdefault(os.path.realpath(directory), _new_observations())


def get_managed_notifications() -> ManagedNotifications:
    from hermes_constants import get_hermes_home
    directory = get_hermes_home() / "plugin-data" / "loopdy" / "managed-notifications"
    with _instances_lock:
        key = str(directory)
        if key not in _instances:
            _instances[key] = ManagedNotifications(directory, observations=_shared_observations(directory))
        return _instances[key]
