"""Bounded native phone transport for Hermes device tools.

Hermes owns the tool execution lifecycle.  This module only holds an ephemeral,
authenticated phone lease and forwards the already validated ``device.tool``
wire contracts to that phone.  The execution middleware receives the real
session, turn, and tool call coordinates from Hermes; none of those identities
are accepted from model arguments.

One Hermes process loads this plugin once for each profile it serves, and each
copy has its own module globals.  The phone's HTTP routes reach only the serving
profile's copy, so the hub lives in a process-wide registry (``_shared_hub``)
that every copy of the same hub version shares.  Each profile's middleware names
its own profile, and a channel is matched on ``(profile, session)``.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass, field
import hashlib
import json
import logging
import sqlite3
import sys
import threading
import time
import types
from typing import Any, Callable, Literal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from . import device_photos
from .device_tool_contract import (
    DEVICE_TOOL_LONG_OPERATIONS,
    DEVICE_TOOL_OPERATIONS,
    device_tool_request,
    parse_device_tool_result,
)


logger = logging.getLogger("hermes.plugins.bighelp")


CAPABILITY = "native-device-tools-v1"
# Phones may share their location (iphone_location) only with plugins that list this.
LOCATION_CAPABILITY = "native-device-location-v1"
# Phones may let agents change how bighelp looks (iphone_appearance) only with plugins that list this.
APPEARANCE_CAPABILITY = "native-device-appearance-v1"
# Likewise for each tool that came after: the phone sends its kind in `enabled` only when the plugin lists it.
KIND_CAPABILITIES = {
    "contacts": "native-device-contacts-v1",
    "photos": "native-device-photos-v1",
    "geofences": "native-device-geofences-v1",
    "alarms": "native-device-alarms-v1",
    "music": "native-device-music-v1",
    "focus": "native-device-focus-v1",
    "shortcuts": "native-device-shortcuts-v1",
}
# Every profile's agent can use the phone tools: a chat of any profile on this host may open a channel.
AGENTS_CAPABILITY = "native-device-tools-agents-v1"
# A call waits for the same phone to open the chat again, and a paused phone keeps what it never received.
WAIT_CAPABILITY = "native-device-tools-wait-v1"
LEASE_SECONDS = 30
# A call finds no channel but the chat had one this recently: it waits for that phone to come back.
LAPSE_MEMORY_SECONDS = 120
# A shortcut can run a minute, and photos.save has the phone download the file from this host.
LONG_OPERATION_SECONDS = 60
# The host waits this much longer than the request it sent allows. A late answer is then still taken, and a
# long operation that never answers is "outcome_unknown", not "nothing happened".
LONG_OPERATION_GRACE_SECONDS = 10
MAX_CHANNELS = 64
MAX_QUEUE = 128
MAX_COMPLETED = 256
MAX_LAPSES = MAX_CHANNELS
MAX_FINISHED_CALLS = 512
# One chat that waits for its phone can hold this many calls, so it can't make every other chat answer busy.
MAX_PARKED_PER_CHAT = 32
MAX_POLL_ITEMS = 8
# The only operations whose outcome the agent may simply ask for again. Any other operation, and any
# operation not listed here, can change something on the phone, so a lost answer is "outcome_unknown".
PURE_READ_OPERATIONS = frozenset({
    "health.read", "calendar.list", "reminders.list", "location.current", "appearance.get",
    "contacts.list", "photos.list", "photos.albums", "photos.get", "geofences.list", "alarms.list",
    "music.nowPlaying", "music.search", "music.library", "focus.current", "shortcuts.list",
})
_UUID = r"\A[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\z"
_SESSION = r"\A[A-Za-z0-9_-]{1,180}\z"
_PROFILE = r"\A[a-z0-9][a-z0-9_-]{0,63}\z"
_HOST = r"\A[A-Za-z0-9_-]{1,96}\z"
_TOOL_NAMES = frozenset({
    "iphone_health", "iphone_calendar", "iphone_reminders", "iphone_location", "iphone_appearance",
    *(f"iphone_{kind}" for kind in KIND_CAPABILITIES),
})


class NativeDeviceToolError(ValueError):
    """A bounded native channel rejection safe to return to the phone."""

    def __init__(self, code: str, message: str = "The native iPhone tool channel is unavailable.",
                 status: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


_Call = tuple[str, str, str, str]
_Phone = tuple[Any, str, str, int]


@dataclass
class _Pending:
    request: dict[str, Any]
    fingerprint: str
    future: Future[dict[str, Any]]
    waiters: int = 1
    # The call as Hermes names it (profile, session, turn, tool call). It stays the same when the request
    # moves to another channel, so a retry is still the same call.
    call: _Call = ("", "", "", "")
    # Every session coordinate the call may be matched to a channel by.
    chat: tuple[str, ...] = ()
    # When the host stops waiting: the request's expiry plus the long-operation grace.
    deadline: float = 0.0
    # Whether a poll handed the request to the phone. It may have run since, so it never moves or runs twice.
    delivered: bool = False
    channel: "_Channel | None" = None
    # While no channel holds the request: the phone whose next channel for this chat may take it.
    phone: _Phone | None = None


@dataclass
class _Channel:
    owner: Any
    channel_id: str
    device_id: str
    host_id: str
    authorization_epoch: int
    profile: str
    session_id: str
    enabled: frozenset[str]
    lease_expires: float
    # While a long call waits, the phone may be in another app and unable to poll.
    hold_until: float = 0.0
    sequence: int = 0
    requests: deque[tuple[int, dict[str, Any]]] = field(default_factory=deque)
    pending: dict[str, _Pending] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    completed_order: deque[str] = field(default_factory=deque)
    # Calls that reached the phone on this channel and ended. If the channel ends with the phone coming back,
    # a retry of one must still not run it again (hub._finished).
    ended_calls: deque[_Call] = field(default_factory=lambda: deque(maxlen=MAX_COMPLETED))
    closed: bool = False


@dataclass
class _Lapse:
    """A phone's channel for a chat ended without the phone saying it was done with the chat."""

    profile: str
    session_id: str
    phone: _Phone
    enabled: frozenset[str]
    ended_at: float


_CONTRACT_PREFIX = "bighelp device tool "
PHONE_UNAVAILABLE_NOTE = "bighelp isn't open on the phone right now. Ask the person to open bighelp, then try again."
# The phone refuses contacts.create and calendar.create in a chat holding the person's picture (docs/IMAGE_DRAFTS.md).
REVIEW_REQUIRED_NOTE = ("Something read from a picture is saved only after the person reviews it. Make a draft "
                        "with bighelp_draft_contact or bighelp_draft_event instead. Don't try again with this tool.")


def _grace(operation: str) -> int:
    return LONG_OPERATION_GRACE_SECONDS if operation in DEVICE_TOOL_LONG_OPERATIONS else 0


def _unanswered(operation: str, delivered: bool = False) -> str:
    """What the agent hears when the phone never answered.

    A request the phone received (``delivered``) may have changed something unless it only reads, and a
    shortcut or a saved picture may have happened anyway: the agent must not run those again. A request the
    phone never received did nothing, so the agent may try again.
    """
    if operation in DEVICE_TOOL_LONG_OPERATIONS or (delivered and operation not in PURE_READ_OPERATIONS):
        return "outcome_unknown"
    return "phone_unavailable"


_REVIEWED_CREATES = frozenset({"contacts.create", "calendar.create"})


def _holds_picture(profile: str, session_ids: list[str]) -> bool:
    from .image_drafts import holds_picture
    return holds_picture(profile, session_ids)


def _failed_result(request_id: str | None, code: str, note: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "version": 1,
        "type": "device.tool.result",
        "status": "failed",
        "code": code,
        "payload": {},
    }
    if code == "outcome_unknown":
        note = "The phone did not answer, so it may have run. Do not run it again; check first."
    elif code == "phone_unavailable" and not note:
        note = PHONE_UNAVAILABLE_NOTE
    elif code == "review_required":
        note = REVIEW_REQUIRED_NOTE
    if note:
        result["payload"] = {"note": note}
    if request_id:
        result["requestId"] = request_id
    return result


def _fingerprint(operation: str, arguments: dict[str, Any]) -> str:
    encoded = json.dumps(
        {"operation": operation, "arguments": arguments},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _request_id(channel: _Channel, session_id: str, turn_id: str, tool_call_id: str) -> str:
    # The authentic Hermes call identity is the idempotency key.  Arguments are
    # deliberately excluded so a duplicate invocation coalesces, while its
    # fingerprint below rejects a reused call ID with changed work.
    value = json.dumps(
        [channel.channel_id, channel.device_id, channel.host_id,
         channel.authorization_epoch, session_id, turn_id, tool_call_id,
        ],
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parked_request_id(call: _Call) -> str:
    """A request ID for a call that waits for its phone. The channel that takes it gives it the real one."""
    value = json.dumps(["waiting", *call], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _owner_key(owner: Any) -> tuple[Any, ...]:
    return (
        getattr(owner, "provider", None),
        getattr(owner, "user_id", None),
        getattr(owner, "serving_profile_id", None),
        getattr(owner, "runtime_id", None),
    )


def _phone_of(owner: Any, device_id: str, host_id: str, authorization_epoch: int) -> _Phone:
    """Who a channel belongs to: the signed-in person and the device, host and grant it speaks for."""
    return (_owner_key(owner), device_id, host_id, authorization_epoch)


def _channel_phone(channel: _Channel) -> _Phone:
    return _phone_of(channel.owner, channel.device_id, channel.host_id, channel.authorization_epoch)


def _same_phone(channel: _Channel, owner: Any, fields: dict[str, Any]) -> bool:
    """Whether a connect comes from the phone that holds this channel, for the same person."""
    return _channel_phone(channel) == _phone_of(
        owner, fields["deviceId"], fields["hostId"], fields["authorizationEpoch"])


def _set_future(pending: _Pending, result: dict[str, Any]) -> None:
    try:
        pending.future.set_result(result)
    except InvalidStateError:
        return


def _mark_completed(channel: _Channel, request_id: str) -> None:
    """Keep the newest bounded result tombstones in insertion order."""
    if request_id in channel.completed:
        return
    channel.completed.add(request_id)
    channel.completed_order.append(request_id)
    while len(channel.completed_order) > MAX_COMPLETED:
        channel.completed.discard(channel.completed_order.popleft())


def _default_profile_exists(profile: str) -> bool:
    """Whether Hermes has a profile of this name on this host."""
    try:
        from hermes_cli.profiles import profile_exists
        return bool(profile_exists(profile))
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return False


def _default_profile_session_validator(profile: str, session_id: str) -> bool:
    """Verify a stored or currently live Hermes session with official helpers.

    The session is looked up in the requested profile's own store (read-only), never in another profile's.
    """
    try:
        from hermes_cli.profiles import get_profile_dir, profile_exists
        from hermes_constants import (
            get_process_hermes_home,
            reset_hermes_home_override,
            set_hermes_home_override,
        )
        from hermes_state import SessionDB
    except ImportError:
        return False
    try:
        if not profile_exists(profile):
            return False
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    try:
        token = set_hermes_home_override(get_process_hermes_home())
        try:
            db_path = get_profile_dir(profile) / "state.db"
            with SessionDB(db_path=db_path, read_only=True) as db:
                if db.get_session(session_id) is not None:
                    return True
        finally:
            reset_hermes_home_override(token)
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        # A profile that has not had a chat yet has no store. Its running chat is still in Hermes' live map.
        pass

    # A live gateway may have a runtime UI ID before its durable session row is
    # visible to a reader.  Hermes' own session gateway map is the only allowed
    # runtime-to-stored resolution here; do not accept arbitrary prefixes.
    try:
        # ``methods_session`` bodies are rebound onto the running gateway
        # module. Read that canonical live map instead of guessing a runtime
        # ID from a prefix or from arbitrary request data.
        sessions = _live_sessions_snapshot()
        row = sessions.get(session_id)
        if isinstance(row, dict) and isinstance(row.get("session_key"), str) and _live_profile_matches(row, profile):
            return True
        return any(
            isinstance(row, dict)
            and row.get("session_key") == session_id
            and _live_profile_matches(row, profile)
            for row in sessions.values()
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return False


def _live_profile_matches(row: dict[str, Any], profile: str) -> bool:
    """Require a live runtime row to belong to the requested Hermes profile."""
    try:
        from hermes_constants import get_process_hermes_home, profile_name_for_home
        profile_home = row.get("profile_home")
        home = Path(profile_home) if isinstance(profile_home, str) and profile_home else get_process_hermes_home()
        return profile_name_for_home(home) == profile
    except (ImportError, OSError, TypeError, ValueError):
        return False


def _live_sessions_snapshot() -> dict[str, Any]:
    """Read Hermes' live runtime map under its own lock when available."""
    try:
        # Do not import the gateway lazily.  Hermes' gateway module performs
        # startup/config work at import time; the native plugin may be loaded
        # by the dashboard before that module exists.  A live runtime map is
        # usable only when the official gateway has already published it.
        gateway_server = sys.modules.get("tui_gateway.server")
        if gateway_server is None:
            return {}
        sessions = getattr(gateway_server, "_sessions", {})
        if not isinstance(sessions, dict):
            return {}
        lock = getattr(gateway_server, "_sessions_lock", None)
        if lock is None:
            return dict(sessions)
        with lock:
            return dict(sessions)
    except (ImportError, AttributeError, RuntimeError, TypeError):
        return {}


def _same_live_session(left: str, right: str) -> bool:
    """Match Hermes' runtime sid to its durable session key using its live map."""
    if left == right:
        return True
    for runtime_id, row in _live_sessions_snapshot().items():
        if not isinstance(row, dict):
            continue
        stored_id = row.get("session_key")
        if not isinstance(runtime_id, str) or not isinstance(stored_id, str):
            continue
        if (runtime_id == left and stored_id == right) or (runtime_id == right and stored_id == left):
            return True
    return False


class NativeDeviceToolHub:
    """Own native device leases, queued requests, and one-shot results."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        lease_seconds: int = LEASE_SECONDS,
        profile_session_validator: Callable[[str, str], bool] | None = None,
        profile_validator: Callable[[str], bool] | None = None,
        photos_directory: Callable[[], Path] | None = None,
        picture_chat: Callable[[str, list[str]], bool] | None = None,
    ) -> None:
        # Without one, pictures go to the Hermes home of the profile whose agent asked.
        self.photos_directory = photos_directory
        # Whether a profile's chats hold a picture from the person (image_drafts.holds_picture).
        self.picture_chat = picture_chat or _holds_picture
        self.clock = clock or time.time
        self.lease_seconds = max(20, min(LEASE_SECONDS, int(lease_seconds)))
        self.profile_session_validator = profile_session_validator or _default_profile_session_validator
        # None: only the session check runs (the shared hub sets the real one).
        self.profile_validator = profile_validator
        self.channels: dict[str, _Channel] = {}
        # Calls the phone has not received that wait for its next channel, by call.
        self._parked: dict[_Call, _Pending] = {}
        # Chats whose channel ended without a goodbye in the last LAPSE_MEMORY_SECONDS.
        self._lapses: list[_Lapse] = []
        # Calls that reached the phone (or finished). A retry of one never runs it again.
        self._finished: OrderedDict[_Call, None] = OrderedDict()
        self._retired_ids: deque[str] = deque(maxlen=MAX_COMPLETED)
        self._lock = threading.RLock()

    def _expired(self, channel: _Channel) -> bool:
        return channel.closed or self.clock() >= max(channel.lease_expires, channel.hold_until)

    def _purge_expired(self) -> None:
        for channel_id, channel in tuple(self.channels.items()):
            if self._expired(channel):
                self._retire(channel, keep=True)
                self.channels.pop(channel_id, None)

    def _retire(self, channel: _Channel, *, keep: bool = False) -> None:
        """End a channel.

        With ``keep`` (the lease lapsed, the same phone opened the chat again, or the phone paused), a request
        the phone never received waits for that phone's next channel for the chat, and the chat is remembered.
        Every other pending call fails, as it does when the channel closes for good.
        """
        if channel.closed:
            return
        now = self.clock()
        ended_at = min(now, max(channel.lease_expires, channel.hold_until))
        channel.closed = True
        self._retired_ids.append(channel.channel_id)
        channel.requests.clear()
        phone = _channel_phone(channel)
        for request_id, pending in tuple(channel.pending.items()):
            if keep and not pending.delivered and not pending.future.done():
                pending.channel, pending.phone = None, phone
                self._parked[pending.call] = pending
                continue
            _mark_completed(channel, request_id)
            if pending.delivered:
                channel.ended_calls.append(pending.call)
            _set_future(pending, _failed_result(
                request_id, _unanswered(pending.request["operation"], pending.delivered)))
        channel.pending.clear()
        self._forget_lapses(channel.profile, channel.session_id)
        if keep:
            # The phone comes back, and a retry of a call it received must not run that call again.
            for call in channel.ended_calls:
                self._remember_call(call)
            self._lapses.append(_Lapse(channel.profile, channel.session_id, phone, channel.enabled, ended_at))
            del self._lapses[:max(0, len(self._lapses) - MAX_LAPSES)]

    def _remember_call(self, call: _Call) -> None:
        self._finished[call] = None
        self._finished.move_to_end(call)
        while len(self._finished) > MAX_FINISHED_CALLS:
            self._finished.popitem(last=False)

    def _forget_lapses(self, profile: str, session_id: str) -> None:
        self._lapses = [
            lapse for lapse in self._lapses
            if not (lapse.profile == profile and _same_live_session(lapse.session_id, session_id))
        ]

    def _recent_lapse(self, profile: str, session_id: str) -> _Lapse | None:
        """The phone that had a channel for this chat a moment ago, if there is no channel now."""
        horizon = self.clock() - LAPSE_MEMORY_SECONDS
        self._lapses = [lapse for lapse in self._lapses if lapse.ended_at > horizon]
        found = [
            lapse for lapse in self._lapses
            if lapse.profile == profile and _same_live_session(lapse.session_id, session_id)
        ]
        return max(found, key=lambda lapse: lapse.ended_at) if found else None

    def _get(self, owner: Any, channel_id: str) -> _Channel:
        channel = self.channels.get(channel_id)
        if channel is None:
            if channel_id in self._retired_ids:
                raise NativeDeviceToolError("phone_unavailable")
            raise NativeDeviceToolError("channel_not_found")
        if _owner_key(channel.owner) != _owner_key(owner):
            raise NativeDeviceToolError("channel_owner_changed", status=403)
        if self._expired(channel):
            self._retire(channel, keep=True)
            raise NativeDeviceToolError("phone_unavailable")
        return channel

    def connect(self, owner: Any, fields: dict[str, Any]) -> dict[str, Any]:
        channel_id = fields["channelId"]
        profile = fields["agentId"]
        session_id = fields["sessionId"]
        # Any profile of this host may open a channel, but only for a chat that is in its own session store.
        if self.profile_validator is not None:
            try:
                known = self.profile_validator(profile)
            except Exception:
                known = False
            if not known:
                raise NativeDeviceToolError("profile_not_found", status=404)
        try:
            verified = self.profile_session_validator(profile, session_id)
        except Exception:
            verified = False
        if not verified:
            raise NativeDeviceToolError("session_not_found", status=404)
        with self._lock:
            if channel_id in self.channels or channel_id in self._retired_ids:
                raise NativeDeviceToolError("channel_id_consumed")
            self._purge_expired()
            # A second phone for one live Hermes session is ambiguous.  It is
            # never selected by recency or by whichever request polls first.
            # The same phone opening the chat again (the same signed-in person,
            # device and host scope) replaces its own earlier channel: a phone
            # whose app was closed, or whose close never arrived, would
            # otherwise be shut out of its own chat until that lease ran out.
            held = [
                channel for channel in self.channels.values()
                if channel.profile == profile and _same_live_session(channel.session_id, session_id)
            ]
            if any(not _same_phone(channel, owner, fields) for channel in held):
                raise NativeDeviceToolError("channel_ambiguous")
            for channel in held:
                # What that channel never handed to the phone moves to the new one.
                self._retire(channel, keep=True)
                self.channels.pop(channel.channel_id, None)
            if held:
                logger.info("bighelp native device channel replaced: the same phone reconnected")
            if len(self.channels) >= MAX_CHANNELS:
                raise NativeDeviceToolError("channel_capacity", status=503)
            channel = _Channel(
                owner=owner,
                channel_id=channel_id,
                device_id=fields["deviceId"],
                host_id=fields["hostId"],
                authorization_epoch=fields["authorizationEpoch"],
                profile=profile,
                session_id=session_id,
                enabled=frozenset(fields["enabled"]),
                lease_expires=self.clock() + self.lease_seconds,
            )
            self.channels[channel_id] = channel
            # The chat has a channel again, so the memory of the old one has done its work.
            self._forget_lapses(profile, session_id)
            self._adopt(channel)
        return {"channelId": channel_id, "connected": True}

    def _adopt(self, channel: _Channel) -> None:
        """Hand the calls that waited for this chat to its new channel, if the same phone opened it."""
        phone = _channel_phone(channel)
        for call, pending in tuple(self._parked.items()):
            if call[0] != channel.profile or not any(
                _same_live_session(channel.session_id, session) for session in pending.chat
            ):
                continue
            del self._parked[call]
            if pending.future.done():
                continue
            if pending.phone != phone:
                # Another phone (or person) has the chat now. The one the call waited for is not coming back.
                _set_future(pending, _failed_result(pending.request["requestId"], "phone_unavailable"))
                continue
            self._attach(channel, pending)

    def _attach(self, channel: _Channel, pending: _Pending) -> None:
        """Queue a request the phone never received on its new channel. It keeps its deadline."""
        old = pending.request
        operation = old["operation"]
        profile, session_id, turn_id, tool_call_id = pending.call
        refusal = None
        request = None
        if operation.split(".", 1)[0] not in channel.enabled:
            refusal = "authorization_required"
        elif len(channel.pending) >= MAX_QUEUE:
            refusal = "busy"
        else:
            try:
                request = device_tool_request(
                    request_id=_request_id(channel, session_id, turn_id, tool_call_id),
                    device_id=channel.device_id,
                    host_id=channel.host_id,
                    authorization_epoch=channel.authorization_epoch,
                    session_id=channel.session_id,
                    agent_id=profile,
                    turn_id=turn_id,
                    operation=operation,
                    arguments=old["arguments"],
                    sent_at=old["sentAt"],
                    expires_at=old["expiresAt"],
                )
            except ValueError:
                refusal = "invalid_arguments"
        if request is None:
            _set_future(pending, _failed_result(old["requestId"], refusal or "invalid_arguments"))
            return
        if operation == "photos.save":
            device_photos.grant_save_path(
                profile, self._stored_ids(session_id, channel.session_id), request["arguments"]["path"])
        if operation in DEVICE_TOOL_LONG_OPERATIONS:
            channel.hold_until = max(channel.hold_until, pending.deadline)
        pending.request, pending.channel, pending.phone, pending.delivered = request, channel, None, False
        channel.pending[request["requestId"]] = pending
        channel.sequence += 1
        channel.requests.append((channel.sequence, request))
        while len(channel.requests) > MAX_QUEUE:
            channel.requests.popleft()

    def _matching(self, profile: str, session_id: str) -> list[_Channel]:
        matches: list[_Channel] = []
        with self._lock:
            for channel in tuple(self.channels.values()):
                if self._expired(channel):
                    self._retire(channel, keep=True)
                    self.channels.pop(channel.channel_id, None)
                    continue
                if channel.profile == profile and _same_live_session(channel.session_id, session_id):
                    matches.append(channel)
        return matches

    def native_channel_count(self, profile: str, session_id: str) -> int:
        """Return the active native-channel count for middleware fallback routing."""
        return len(self._matching(profile, session_id))

    def _target(self, profile: str, session_id: str) -> tuple[_Channel | None, _Lapse | None]:
        """Where a call for this chat goes: its one channel, or the phone it waits for."""
        with self._lock:
            matches = self._matching(profile, session_id)
            if len(matches) > 1:
                raise NativeDeviceToolError("channel_ambiguous")
            if matches:
                return matches[0], None
            lapse = self._recent_lapse(profile, session_id)
            if lapse is None:
                raise NativeDeviceToolError("phone_unavailable")
            return None, lapse

    def poll(self, owner: Any, fields: dict[str, Any], *, after: int | None = None) -> dict[str, Any]:
        with self._lock:
            channel = self._get(owner, fields["channelId"])
            if after is None:
                after = fields["after"]
            if after > channel.sequence or (
                channel.requests and after < channel.requests[0][0] - 1
            ):
                raise NativeDeviceToolError("feed_gap")
            while channel.requests and channel.requests[0][0] <= after:
                channel.requests.popleft()
            rows = list(channel.requests)[:MAX_POLL_ITEMS]
            # A call nobody waits for any more (its time ran out) is skipped, but still acknowledged by `next`,
            # so the phone never runs a request whose agent was already told it did not arrive.
            live = [(sequence, request) for sequence, request in rows if request["requestId"] in channel.pending]
            for _sequence, request in live:
                # From here the phone may have run it, even if the answer never comes back.
                channel.pending[request["requestId"]].delivered = True
            channel.lease_expires = self.clock() + self.lease_seconds
            return {
                "channelId": channel.channel_id,
                "next": rows[-1][0] if rows else after,
                "requests": [
                    {"sequence": sequence, "request": request}
                    for sequence, request in live
                ],
            }

    def accept_result(self, owner: Any, fields: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            channel = self._get(owner, fields["channelId"])
            request_id = result.get("requestId") if isinstance(result, dict) else None
            if request_id in channel.completed:
                raise NativeDeviceToolError("result_replay")
            pending = channel.pending.get(request_id)
            if pending is None:
                raise NativeDeviceToolError("result_unknown")
            try:
                parsed = parse_device_tool_result(
                    result,
                    sender_device_id=channel.device_id,
                    sender_epoch=channel.authorization_epoch,
                )
            except ValueError as exc:
                raise NativeDeviceToolError("result_invalid") from exc
            if any(
                parsed.get(key) != pending.request.get(key)
                for key in (
                    "requestId", "deviceId", "hostId", "authorizationEpoch", "sessionId",
                    "agentId", "turnId", "operation",
                )
            ):
                raise NativeDeviceToolError("result_coordinates_mismatch")
            if parsed["operation"].split(".", 1)[0] not in channel.enabled:
                raise NativeDeviceToolError("authorization_required")
            if parsed["sentAt"] < pending.request["sentAt"]:
                raise NativeDeviceToolError("result_timestamp_invalid")
            if self.clock() >= pending.request["expiresAt"] + _grace(parsed["operation"]):
                _mark_completed(channel, request_id)
                channel.ended_calls.append(pending.call)
                _set_future(pending, _failed_result(request_id, _unanswered(parsed["operation"], True)))
                raise NativeDeviceToolError("result_expired")
            _mark_completed(channel, request_id)
            channel.ended_calls.append(pending.call)
            _set_future(pending, parsed)
            channel.lease_expires = self.clock() + self.lease_seconds
        return {"accepted": True}

    def close(self, owner: Any, fields: dict[str, Any]) -> dict[str, Any]:
        """Close a channel. ``reason: "paused"`` says the phone will be back (the app went to the background),
        so what it never received keeps waiting. Any other close fails every pending call."""
        with self._lock:
            channel = self._get(owner, fields["channelId"])
            self._retire(channel, keep=fields.get("reason") == "paused")
            self.channels.pop(channel.channel_id, None)
        return {"closed": True}

    def close_profile(self, profile: str) -> None:
        """Retire every phone lease of one profile when Hermes unloads this plugin for it."""
        with self._lock:
            for channel_id, channel in tuple(self.channels.items()):
                if channel.profile == profile:
                    self._retire(channel)
                    self.channels.pop(channel_id, None)
            for call, pending in tuple(self._parked.items()):
                if call[0] == profile:
                    del self._parked[call]
                    _set_future(pending, _failed_result(pending.request["requestId"], "phone_unavailable"))
            self._lapses = [lapse for lapse in self._lapses if lapse.profile != profile]
            for call in [call for call in self._finished if call[0] == profile]:
                del self._finished[call]

    def close_all(self) -> None:
        """Retire every phone lease of every profile."""
        with self._lock:
            profiles = (
                {channel.profile for channel in self.channels.values()}
                | {call[0] for call in self._parked} | {lapse.profile for lapse in self._lapses}
                | {call[0] for call in self._finished}
            )
            for profile in profiles:
                self.close_profile(profile)

    async def execute(
        self,
        *,
        profile: str,
        session_id: str,
        turn_id: str,
        tool_call_id: str,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        if not all(isinstance(value, str) and value for value in (profile, session_id, turn_id, tool_call_id)):
            return _failed_result(None, "call_identity_missing")
        try:
            if not self.profile_session_validator(profile, session_id):
                return _failed_result(None, "phone_unavailable")
        except Exception:
            return _failed_result(None, "phone_unavailable")
        if operation in _REVIEWED_CREATES and self._holds_picture(profile, session_id):
            # Something read from a picture is saved only after the person reviews it: answered here, so the
            # call never reaches the phone, whose own fence says the same (docs/IMAGE_DRAFTS.md).
            return _failed_result(None, "review_required")
        call: _Call = (profile, session_id, turn_id, tool_call_id)
        capability = operation.split(".", 1)[0]
        try:
            # No channel now is not the end if the chat's phone had one a moment ago: the call then waits
            # for that phone to open the chat again (_target). With no recent channel it fails at once.
            channel, lapse = self._target(profile, session_id)
            if capability not in (channel.enabled if channel is not None else lapse.enabled):
                return _failed_result(None, "authorization_required")
            if not isinstance(arguments, dict):
                raise ValueError
            if operation == "photos.save":
                # Checked here, before anything is queued: the phone gets only a path the host will serve.
                # In the asking agent's own home: the gateway's delivery rules are read from there.
                with device_photos.profile_scope(profile):
                    arguments = {**arguments, "path": device_photos.check_save_path(arguments.get("path"))}
            timeout = LONG_OPERATION_SECONDS if operation in DEVICE_TOOL_LONG_OPERATIONS else self.lease_seconds
            fingerprint = _fingerprint(operation, arguments)
            with self._lock:
                # Look again: the channel may have ended, or come back, while the arguments were checked.
                channel, lapse = self._target(profile, session_id)
                if capability not in (channel.enabled if channel is not None else lapse.enabled):
                    return _failed_result(None, "authorization_required")
                request_id = _request_id(channel, session_id, turn_id, tool_call_id) if channel is not None else None
                existing = self._parked.get(call)
                if existing is None and channel is not None:
                    existing = channel.pending.get(request_id)
                if existing is not None:
                    if existing.fingerprint != fingerprint:
                        return _failed_result(request_id, "request_conflict")
                    existing.waiters += 1
                    pending = existing
                elif call in self._finished or (channel is not None and request_id in channel.completed):
                    return _failed_result(request_id, "result_replay")
                else:
                    sent_at = int(self.clock())
                    request = device_tool_request(
                        request_id=request_id or _parked_request_id(call),
                        device_id=channel.device_id if channel is not None else lapse.phone[1],
                        host_id=channel.host_id if channel is not None else lapse.phone[2],
                        authorization_epoch=(
                            channel.authorization_epoch if channel is not None else lapse.phone[3]),
                        # Echo the channel's validated coordinate.  If the
                        # phone connected with Hermes' runtime sid, the
                        # middleware's durable sid is used only for matching.
                        session_id=channel.session_id if channel is not None else lapse.session_id,
                        agent_id=profile,
                        turn_id=turn_id,
                        operation=operation,
                        arguments=arguments,
                        sent_at=sent_at,
                        expires_at=sent_at + timeout,
                    )
                    if channel is not None:
                        crowded = len(channel.pending) >= MAX_QUEUE
                    else:
                        crowded = len(self._parked) >= MAX_QUEUE or sum(
                            1 for other in self._parked if other[0] == profile and other[1] == session_id
                        ) >= MAX_PARKED_PER_CHAT
                    if crowded:
                        return _failed_result(request_id, "busy")
                    coordinate = channel.session_id if channel is not None else lapse.session_id
                    if operation == "photos.save":
                        device_photos.grant_save_path(
                            profile, self._stored_ids(session_id, coordinate), arguments["path"])
                    pending = _Pending(
                        request, fingerprint, Future(), call=call, chat=(session_id, coordinate),
                        deadline=request["expiresAt"] + _grace(operation),
                    )
                    if channel is not None:
                        if operation in DEVICE_TOOL_LONG_OPERATIONS:
                            channel.hold_until = max(channel.hold_until, self.clock() + timeout + _grace(operation))
                        pending.channel = channel
                        channel.pending[request_id] = pending
                        channel.sequence += 1
                        channel.requests.append((channel.sequence, request))
                        while len(channel.requests) > MAX_QUEUE:
                            channel.requests.popleft()
                    else:
                        pending.phone = lapse.phone
                        self._parked[call] = pending
                deadline = pending.deadline
            remaining = max(0.001, min(float(deadline - int(self.clock())), timeout + _grace(operation)))
            try:
                # A middleware call may run through Hermes' persistent main
                # loop or a per-worker loop.  Wrap the thread-safe shared
                # future on this caller's loop so duplicates can coalesce
                # across either execution path without cross-loop awaits.
                shared_result = asyncio.wrap_future(pending.future, loop=asyncio.get_running_loop())
                result = await asyncio.wait_for(asyncio.shield(shared_result), timeout=remaining)
                return self._for_agent(operation, result, profile, session_id)
            except asyncio.TimeoutError:
                with self._lock:
                    result = self._give_up(pending)
                return result
            finally:
                with self._lock:
                    pending.waiters -= 1
                    if pending.waiters <= 0 or pending.future.done():
                        self._release(pending)
        except NativeDeviceToolError as exc:
            return _failed_result(None, exc.code)
        except device_photos.PhotoRefusal as exc:
            return _failed_result(None, exc.code)
        except (TypeError, ValueError, KeyError) as exc:
            # The contract's own messages name the field and never repeat a value, so they're safe to show
            # the agent (the tool schemas it sees leave out most bounds).
            text = str(exc)
            return _failed_result(None, "invalid_arguments", text.removeprefix(_CONTRACT_PREFIX)
                                  if isinstance(exc, ValueError) and text.startswith(_CONTRACT_PREFIX) else None)

    def _give_up(self, pending: _Pending) -> dict[str, Any]:
        """The call's own deadline came and the phone did not answer."""
        operation = pending.request["operation"]
        request_id = pending.request["requestId"]
        if pending.channel is None and not pending.delivered:
            # It never reached a phone, so nothing happened and the agent may try again.
            result = _failed_result(request_id, "phone_unavailable")
        else:
            result = _failed_result(request_id, _unanswered(operation, pending.delivered))
        if pending.channel is not None:
            _mark_completed(pending.channel, request_id)
            if pending.delivered:
                pending.channel.ended_calls.append(pending.call)
        _set_future(pending, result)
        return result

    def _release(self, pending: _Pending) -> None:
        """Forget a call nobody waits for any more, wherever it is held."""
        channel = pending.channel
        if channel is not None and channel.pending.get(pending.request["requestId"]) is pending:
            channel.pending.pop(pending.request["requestId"], None)
        if self._parked.get(pending.call) is pending:
            del self._parked[pending.call]

    def _holds_picture(self, profile: str, session_id: str) -> bool:
        try:
            return bool(self.picture_chat(profile, self._stored_ids(session_id)))
        except Exception as exc:
            # An unreadable history doesn't block a create: the phone's own fence decides.
            logger.warning("bighelp picture fence skipped: %s", type(exc).__name__)
            return False

    def _stored_ids(self, *session_ids: str) -> list[str]:
        """These session coordinates and the stored sessions Hermes' live map pairs them with."""
        ids = list(dict.fromkeys(session_ids))
        for runtime_id, row in _live_sessions_snapshot().items():
            stored_id = row.get("session_key") if isinstance(row, dict) else None
            if isinstance(stored_id, str) and runtime_id in ids and stored_id not in ids:
                ids.append(stored_id)
        return ids

    def _for_agent(self, operation: str, result: dict[str, Any], profile: str | None = None,
                   session_id: str | None = None) -> dict[str, Any]:
        """What the agent reads. A photo from photos.get becomes a file on this host; the bytes end here."""
        if result.get("status") == "failed" and result.get("code") == "review_required":
            kept = result.get("payload") if isinstance(result.get("payload"), dict) else {}
            return {**result, "payload": {**kept, "note": REVIEW_REQUIRED_NOTE}}
        payload = result.get("payload")
        if operation != "photos.get" or not isinstance(payload, dict) or "image" not in payload:
            return result
        image = payload["image"]
        rest = {key: value for key, value in payload.items() if key != "image"}
        if result.get("status") != "completed":
            # Whatever else came back, never the picture itself.
            kept = {key: value for key, value in image.items() if key != "data"} if isinstance(image, dict) else None
            return {**result, "payload": {**rest, **({"image": kept} if kept else {})}}
        try:
            directory = self.photos_directory() if self.photos_directory else device_photos.photos_directory(profile)
            picture = device_photos.store_picture(image, directory)
        except (device_photos.PhotoRefusal, OSError, ImportError, ValueError) as exc:
            # Only a fixed code is logged: the picture's bytes and its path are the user's.
            logger.warning("bighelp phone photo not stored: %s", getattr(exc, "code", type(exc).__name__))
            return _failed_result(result.get("requestId"), getattr(exc, "code", "photo_unavailable"))
        if session_id:
            try:
                # The chat that read it may make a photo draft from it (image_drafts.py).
                device_photos.record_fetched(directory, self._stored_ids(session_id), picture["path"])
            except (OSError, sqlite3.Error) as exc:
                logger.warning("bighelp phone photo not recorded: %s", type(exc).__name__)
        return {**result, "payload": {**rest, "image": picture}}


def _operation_for_tool(tool_name: str, args: Any) -> tuple[str, dict[str, Any]]:
    # These coordinates belong to the authenticated channel and Hermes
    # middleware.  A model supplied value is discarded before the existing
    # device-tool contract validates the operation arguments.
    reserved = {
        "channelId", "deviceId", "hostId", "authorizationEpoch", "sessionId",
        "agentId", "turnId", "toolCallId", "requestId",
    }
    if tool_name == "iphone_health":
        if not isinstance(args, dict):
            raise NativeDeviceToolError("invalid_arguments", status=422)
        return "health.read", {key: value for key, value in args.items() if key not in reserved}
    if tool_name not in _TOOL_NAMES or not isinstance(args, dict):
        raise NativeDeviceToolError("invalid_arguments", status=422)
    values = {key: value for key, value in args.items() if key not in reserved}
    selected = values.pop("operation", None)
    if tool_name == "iphone_location":
        if selected != "current":
            raise NativeDeviceToolError("invalid_arguments", status=422)
        return "location.current", values
    if tool_name == "iphone_appearance":
        if selected not in {"get", "set"}:
            raise NativeDeviceToolError("invalid_arguments", status=422)
        return f"appearance.{selected}", values
    kind = tool_name.removeprefix("iphone_")
    if kind in KIND_CAPABILITIES:
        # Each of these tools lists its own operations in the contract (contacts.list, photos.createAlbum ...).
        operation = f"{kind}.{selected}" if isinstance(selected, str) else ""
        if operation not in DEVICE_TOOL_OPERATIONS:
            raise NativeDeviceToolError("invalid_arguments", status=422)
        return operation, values
    if selected not in {"list", "create", "update", "delete"}:
        raise NativeDeviceToolError("invalid_arguments", status=422)
    return f"{kind}.{selected}", values


def register_middleware(
    ctx: Any,
    *,
    hub: NativeDeviceToolHub | None = None,
) -> bool:
    """Register native tool execution on Hermes' public middleware surface.

    Hermes invokes execution middleware from synchronous tool dispatch.  The
    callback therefore resolves the native hub coroutine through Hermes'
    supported ``model_tools._run_async`` bridge before returning a JSON tool
    result.  An absent native lease remains a truthful phone-unavailable
    result.

    Hermes capability probes may expose middleware registration without the
    host-owned ``on_unload`` lifecycle.  Those probes can inspect the callback,
    but native availability remains false until cleanup ownership is present.
    """
    register = getattr(ctx, "register_middleware", None)
    if not callable(register):
        logger.warning(
            "bighelp native feature %r NOT advertised: the plugin host does not "
            "expose a callable register_middleware surface, so the native tool "
            "execution middleware could not be installed. The iOS capability "
            "probe will report the feature as missing even though the plugin "
            "is installed.", CAPABILITY)
        return False
    try:
        from hermes_cli.middleware import TOOL_EXECUTION_MIDDLEWARE, VALID_MIDDLEWARE
    except ImportError as exc:
        logger.warning(
            "bighelp native feature %r NOT advertised: could not import "
            "hermes_cli.middleware (%s). The host Hermes version predates the "
            "public execution-middleware surface; native device tools are "
            "unavailable until the host is upgraded.", CAPABILITY, exc)
        return False
    if TOOL_EXECUTION_MIDDLEWARE not in VALID_MIDDLEWARE:
        logger.warning(
            "bighelp native feature %r NOT advertised: %r is not in the host's "
            "VALID_MIDDLEWARE %r. The host's middleware surface does not "
            "include tool execution.", CAPABILITY, TOOL_EXECUTION_MIDDLEWARE,
            tuple(VALID_MIDDLEWARE))
        return False
    selected_hub = hub or _HUB
    profile = str(getattr(ctx, "profile_name", "") or "")

    def execute(**kwargs: Any) -> str | None:
        tool_name = kwargs.get("tool_name")
        if tool_name not in _TOOL_NAMES:
            # Execution middleware is a chain.  Unrelated tools belong to the
            # host's downstream executor; returning None would be interpreted
            # as a successful middleware result and leave the agent with no
            # tool output.  Keep the direct-probe fallback when no chain hook
            # was supplied, but never swallow an official downstream result or
            # exception.
            next_call = kwargs.get("next_call")
            return next_call() if callable(next_call) else None
        session_id = kwargs.get("session_id", "")
        try:
            operation, arguments = _operation_for_tool(tool_name, kwargs.get("args"))
            import model_tools
            result = model_tools._run_async(selected_hub.execute(
                profile=profile,
                session_id=session_id,
                turn_id=kwargs.get("turn_id", ""),
                tool_call_id=kwargs.get("tool_call_id", ""),
                operation=operation,
                arguments=arguments,
            ))
        except NativeDeviceToolError as exc:
            result = _failed_result(None, exc.code)
        except (TypeError, ValueError, KeyError):
            result = _failed_result(None, "invalid_arguments")
        except Exception:
            # Hermes' execution chain falls through when a middleware callback
            # raises.  An active native lease must never silently reroute into
            # the legacy handler after its async bridge fails.
            result = _failed_result(None, "phone_unavailable")
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    try:
        register(TOOL_EXECUTION_MIDDLEWARE, execute)
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning(
            "bighelp native feature %r NOT advertised: the host rejected the "
            "middleware callback registration (%s).", CAPABILITY, exc)
        return False
    on_unload = getattr(ctx, "on_unload", None)
    if callable(on_unload):
        _set_registered(True, profile)

        def unload() -> None:
            # The hub is shared with the other profiles' copies of this plugin: end only this profile's leases.
            retire_profile = getattr(selected_hub, "close_profile", None)
            retire = getattr(selected_hub, "close_all", None)
            try:
                if callable(retire_profile):
                    retire_profile(profile)
                elif callable(retire):
                    retire()
            finally:
                _set_registered(False, profile)

        on_unload(unload)
        logger.info(
            "bighelp native feature %r advertised for profile %r: native tool "
            "execution middleware is registered with the host lifecycle.",
            CAPABILITY, profile)
    else:
        # A host without lifecycle cleanup is deliberately unsupported for native
        # phone leases; ``available()`` remains false and the HTTP gate will fail
        # closed.  Keep the callback registration result true for probe contexts.
        logger.warning(
            "bighelp native feature %r will NOT be advertised to the iOS "
            "capability probe: the host exposed register_middleware but no "
            "callable on_unload lifecycle, so available() remains false and "
            "native device-tool HTTP requests will fail closed.", CAPABILITY)
    return True


# Bump when the hub's state or methods change in a way another copy of this module can't use. Copies of
# different versions each keep their own hub.
HUB_PROTOCOL_VERSION = 1
_HUB_REGISTRY_NAME = "_bighelp_device_tool_hubs"


def _source_digest() -> str:
    """A fingerprint of this file, so a copy with different code never takes a hub another copy made."""
    try:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:32]
    except OSError:
        from .version import PLUGIN_VERSION
        return hashlib.sha256(PLUGIN_VERSION.encode("utf-8")).hexdigest()[:32]


_SOURCE_DIGEST = _source_digest()


def _shared_hub(*, version: int = HUB_PROTOCOL_VERSION, digest: str | None = None) -> NativeDeviceToolHub:
    """The process-wide hub for this protocol version and this exact code.

    Hermes imports the plugin once for each profile it serves, and module globals are not shared between
    those copies. The hub is kept in a plain module registered in ``sys.modules`` so every copy finds the
    same one. Only plain containers and locks live in the registry, and callers use the hub only by its
    methods and attributes, never by ``isinstance``.
    """
    registry = sys.modules.get(_HUB_REGISTRY_NAME)
    if registry is None:
        candidate = types.ModuleType(_HUB_REGISTRY_NAME)
        candidate.lock, candidate.hubs = threading.Lock(), {}
        registry = sys.modules.setdefault(_HUB_REGISTRY_NAME, candidate)
    with registry.lock:
        key = (version, digest or _SOURCE_DIGEST)
        hub = registry.hubs.get(key)
        if hub is None:
            hub = registry.hubs[key] = NativeDeviceToolHub(profile_validator=_default_profile_exists)
        return hub


_HUB = _shared_hub()
_registered = False
_registered_profile: str | None = None
_MODULE_ORIGIN = Path(__file__).resolve()


def _set_registered(value: bool, profile: str | None = None) -> None:
    global _registered, _registered_profile
    _registered = bool(value)
    _registered_profile = profile if _registered else None


def _implementation_for_profile(profile: str | None) -> Any | None:
    """Find the middleware module owned by the active Hermes plugin loader.

    Hermes intentionally imports directory plugins under a profile-safe
    ``hermes_plugins.<slug>`` namespace.  Dashboard API modules are mounted
    from disk and import the same package under its bare name.  Keep the
    dashboard side pointed at the loader-owned implementation so its requests
    use the middleware's hub, while matching the serving profile to prevent a
    multiplexed process from crossing profile state.  Multiple matching
    generations are treated as unavailable until Hermes retires the stale one.
    """
    if not isinstance(profile, str) or not profile:
        return None
    current = sys.modules.get(__name__)
    candidates: list[Any] = []
    if (
        current is not None
        and _registered
        and _registered_profile == profile
    ):
        candidates.append(current)
    for name, module in tuple(sys.modules.items()):
        if module is current or not name.endswith(".native_device_tools"):
            continue
        try:
            origin = Path(getattr(module, "__file__", "")).resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            continue
        if origin != _MODULE_ORIGIN:
            continue
        if getattr(module, "_registered", False) is not True:
            continue
        if getattr(module, "_registered_profile", None) != profile:
            continue
        candidates.append(module)
    # A forced reload can briefly leave two registered generations in one
    # process.  Refuse to route by import order until the host's lifecycle has
    # retired one of them.
    return candidates[0] if len(candidates) == 1 else None


def available(profile: str | None = None) -> bool:
    """Whether registration installed a working native middleware path.

    With a profile, resolve the loader-owned namespace used by the current
    dashboard process.  The no-argument form remains a local registration
    probe for compatibility with focused tests and host feature probes.
    """
    if profile is None:
        return _registered
    return _implementation_for_profile(profile) is not None


class _Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    channelId: str = Field(pattern=_UUID)


class _Connect(_Scope):
    deviceId: str = Field(pattern=_UUID)
    hostId: str = Field(pattern=_HOST)
    authorizationEpoch: StrictInt = Field(ge=1, le=1)
    agentId: str = Field(pattern=_PROFILE)
    sessionId: str = Field(pattern=_SESSION)
    enabled: list[Literal[
        "health", "calendar", "reminders", "location", "appearance",
        "contacts", "photos", "geofences", "alarms", "music", "focus", "shortcuts",
    ]] = Field(min_length=0, max_length=12)

    @field_validator("enabled")
    @classmethod
    def unique_enabled(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("enabled capabilities must be unique")
        return value


class _Close(_Scope):
    # "paused": the app went to the background and will be back. Nothing else is accepted.
    reason: Literal["paused"] | None = None


class _Poll(_Scope):
    after: StrictInt = Field(ge=0, le=9_007_199_254_740_991)


class _Result(_Scope):
    result: dict[str, Any]


def _is_hub_refusal(error: BaseException) -> bool:
    return (
        isinstance(error, ValueError) and type(error).__name__ == NativeDeviceToolError.__name__
        and isinstance(getattr(error, "code", None), str) and isinstance(getattr(error, "status", None), int)
    )


async def request(
    operation: str,
    request: Any,
    *,
    owner: Any | None = None,
    auth_module: Any | None = None,
) -> Any:
    """Serve one native device-tool operation through the shared native auth gate.

    The dashboard API can dispatch into a profile-scoped copy of this module.
    In that case it passes the already authenticated bare-router owner and its
    request helpers so the scoped hub does not manufacture a second runtime
    identity or ETag for the same HTTP request.
    """
    if auth_module is None:
        from .native_api import _body, _precondition, _response
        from .native_context import NativeAPIError, native_context
    else:
        _body = auth_module._body
        _precondition = auth_module._precondition
        _response = auth_module._response
        NativeAPIError = auth_module.NativeAPIError
        native_context = auth_module.native_context

    models: dict[str, type[BaseModel]] = {
        "connect": _Connect,
        "poll": _Poll,
        "result": _Result,
        "close": _Close,
    }
    if operation not in models:
        raise NativeAPIError(404, "unknown_device_tool_operation", "The device-tool operation is unknown.")
    if owner is None:
        owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features or not available():
        raise NativeAPIError(503, "device_tools_unavailable", "Native device tools are unavailable.")
    body = await _body(request, models[operation])
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; reconnect before retrying.")
    fields = body.model_dump()
    try:
        if operation == "connect":
            # It reads the profile's session store (SQLite), so it must not run on the event loop.
            value = await asyncio.to_thread(_HUB.connect, owner, fields)
        elif operation == "poll":
            value = _HUB.poll(owner, fields)
        elif operation == "result":
            value = _HUB.accept_result(owner, fields, fields["result"])
        else:
            value = _HUB.close(owner, fields)
    except Exception as error:
        # The hub may belong to another profile's copy of this module, so its error class is not ours.
        if not _is_hub_refusal(error):
            raise
        raise NativeAPIError(error.status, error.code, str(error)) from None
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; reconcile the outcome.")
    return _response(value, owner, request_id)


__all__ = [
    "AGENTS_CAPABILITY", "CAPABILITY", "WAIT_CAPABILITY", "NativeDeviceToolError", "NativeDeviceToolHub", "available",
    "register_middleware", "request",
]
