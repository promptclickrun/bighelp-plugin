"""Disposable observations from the public hosted-room hook, never room history."""
from __future__ import annotations

from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
import copy
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
import types
import uuid
from typing import Any, Callable

from .native_context import NativeAPIError, NativeContext
from .room_presence import RoomPresence, room_presence
from .sensitive import contains_sensitive_credential


HOOK = "on_room_member_activity"
CAPABILITY = "native-room-activity-v1"
# Feeds opened with `include: ["member.responding"]` also say which member is
# writing. Older apps never ask, so they never see the new kind.
TYPING_CAPABILITY = "native-room-typing-v1"
RESPONDING = "member.responding"
RESPONDING_SOURCES = frozenset({"message.delta", "message.interim", "reasoning.delta"})
# Deltas arrive per token; one observation per member turn this often is enough.
RESPONDING_INTERVAL_SECONDS = 2.0
# An agent on this computer in a room homed on another one (RoomLink): its
# turns run here through the API server, which fires the agent's stream hooks
# but not the room hook. Feeds for such a room say when it is writing.
PEER_TYPING_CAPABILITY = "native-room-peer-typing-v1"
STREAM_HOOK = "on_stream_delta"
MAX_FEEDS = 16
MAX_EVENTS = 128
MAX_FEED_BYTES = 256 * 1024
MAX_EVENT_BYTES = 20_480
MAX_DETAIL_BYTES = 8_192
LEASE_SECONDS = 60
_SECRET_KEY = re.compile(r"authorization|cookie|password|secret|credential|api.?key|token|^auth$", re.I)
_QUOTED_SECRET = re.compile(r"""["'](?:authorization|cookie|password|secret|credential|api[_-]?key|token|auth)["']\s*:\s*["']""", re.I)
logger = logging.getLogger(__name__)


def _integer(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= 9_007_199_254_740_991:
        raise ValueError("Invalid observation integer")
    return value


def _identifier(value: Any, maximum: int = 128) -> str:
    if not isinstance(value, str) or len(value) > maximum or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value) is None:
        raise ValueError("Invalid observation coordinate")
    return value


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _detail(value: Any, *, present: bool) -> dict:
    def omitted(state: str) -> dict:
        return {"state": state, "text": None}
    if not present:
        return omitted("unavailable")
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 8 or nodes > 512:
            return omitted("omitted_size")
        if isinstance(item, str):
            if len(item) > MAX_DETAIL_BYTES:
                return omitted("omitted_size")
            if contains_sensitive_credential(item) or _QUOTED_SECRET.search(item):
                return omitted("omitted_sensitive")
        elif isinstance(item, dict):
            if len(item) > 128:
                return omitted("omitted_size")
            for key, child in item.items():
                if not isinstance(key, str):
                    return omitted("unavailable")
                if _SECRET_KEY.search(key):
                    return omitted("omitted_sensitive")
                pending.extend(((key, depth + 1), (child, depth + 1)))
        elif isinstance(item, list):
            if len(item) > 128:
                return omitted("omitted_size")
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in (bool, int, float):
            return omitted("unavailable")
    try:
        text = value if isinstance(value, str) else _encoded(value).decode("utf-8")
        if len(text.encode("utf-8")) > MAX_DETAIL_BYTES:
            return omitted("omitted_size")
        if any(ord(character) < 32 and character not in "\r\n\t" for character in text):
            return omitted("unavailable")
    except (ValueError, UnicodeError):
        return omitted("unavailable")
    if contains_sensitive_credential(text):
        return omitted("omitted_sensitive")
    return {"state": "available", "text": text}


def _coordinates(value: dict) -> dict:
    if value.get("telemetry_schema_version") != "hermes.observer.v1":
        raise ValueError("Unsupported observation schema")
    fields = {"room_id": "roomId", "member_id": "memberId", "thread_id": "threadId",
              "turn_id": "turnId", "task_id": "taskId"}
    result = {target: _identifier(value.get(source)) for source, target in fields.items()}
    result["executionGeneration"] = _integer(value.get("execution_generation"), minimum=1)
    sequence = value.get("seq")
    result["sourceSequence"] = None if sequence is None else _integer(sequence)
    return result


def project_responding(value: dict) -> dict | None:
    """A member is writing: its coordinates only, never the text."""
    if value.get("kind") not in RESPONDING_SOURCES:
        return None
    return {**_coordinates(value), "kind": RESPONDING}


def project_observation(value: dict) -> dict | None:
    if value.get("kind") not in {"tool.started", "tool.completed"}:
        return None
    result = _coordinates(value)
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Invalid observation payload")
    duration = payload.get("duration_s")
    if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or not 0 <= duration <= 86400):
        raise ValueError("Invalid observation duration")
    result.update({
        "kind": value["kind"],
        "tool": {"id": _identifier(payload.get("tool_id"), 512),
                 "name": _identifier(payload.get("name")),
                 "durationMs": None if duration is None else int(duration * 1000)},
        "arguments": _detail(payload.get("args"), present="args" in payload),
        "result": _detail(payload.get("result"), present="result" in payload),
    })
    for field in sorted(("arguments", "result"), key=lambda field: len(_encoded(result[field])), reverse=True):
        if len(_encoded(result)) <= MAX_EVENT_BYTES - 256:
            break
        result[field] = {"state": "omitted_size", "text": None}
    return result


@dataclass(frozen=True)
class RoomScope:
    room_id: str
    gateway_id: str
    epoch: int
    members: tuple[tuple[str, str], ...]
    # Hermes' session path (a gateway's own listener) never fires the room hook. Its
    # members write in their own hidden room sessions, so writing is read from those
    # sessions' stream deltas. A member's session appears on its first turn, so this
    # is read again on every poll and never makes a scope differ.
    member_sessions: tuple[tuple[str, str], ...] = field(default=(), compare=False)

    @property
    def sessions(self) -> dict[str, str]:
        return dict(self.member_sessions)

    @classmethod
    def from_state(cls, value: dict, room_id: str) -> "RoomScope":
        room = value.get("room")
        if not isinstance(room, dict) or room.get("room_id") != room_id or room.get("disbanded_at") is not None:
            raise NativeAPIError(404, "room_unavailable", "The room is unavailable.")
        try:
            roster = room["members"]
            if not isinstance(roster, list) or not 2 <= len(roster) <= 6:
                raise ValueError("Invalid roster")
            members = tuple((_identifier(member["member_id"]), _identifier(member["profile"]))
                            for member in roster)
            if len({member[0] for member in members}) != len(members):
                raise ValueError("Duplicate member")
            return cls(_identifier(room_id), _identifier(room["authority_gateway_id"]),
                       _integer(room["authority_epoch"], minimum=1), members)
        except (KeyError, TypeError, ValueError):
            raise NativeAPIError(503, "room_state_invalid", "The room state could not be verified.") from None


def room_member_sessions(room_id: str, members: tuple[tuple[str, str], ...],
                         homes: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """`(session ID, member ID)` of each member's hidden room session on Hermes' session path:
    titled `Group: <room>` with source `bot_room` in that member's own store, plus the sessions
    compression continued it in. Read-only; a store that can't be read leaves its member out."""
    try:
        from tui_gateway.hosted_room_driver import ROOM_SESSION_SOURCE, room_session_title
        title, source = room_session_title(room_id), ROOM_SESSION_SOURCE
    except ImportError:
        title, source = f"Group: {room_id}", "bot_room"
    found: list[tuple[str, str]] = []
    for member_id, profile in members:
        home = homes.get(profile)
        path = Path(home) / "state.db" if home is not None else None
        if path is None or not path.is_file():
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
                ids = [row[0] for row in db.execute(
                    "SELECT id FROM sessions WHERE title=? AND source=? LIMIT 4", (title, source))]
                frontier = list(ids)
                for _ in range(8):
                    if not frontier or len(ids) >= 16:
                        break
                    marks = ",".join("?" for _ in frontier)
                    frontier = [row[0] for row in db.execute(
                        f"SELECT id FROM sessions WHERE parent_session_id IN ({marks}) LIMIT 8", frontier)
                        if row[0] not in ids]
                    ids += frontier
        except (OSError, sqlite3.Error):
            continue
        found += [(session_id, member_id) for session_id in ids
                  if isinstance(session_id, str) and 0 < len(session_id) <= 128]
    return tuple(found[:64])


def peer_session_id(home_install_id: str, room_id: str, member_id: str, target_profile: str) -> str:
    """Hermes' hidden session on this computer for a RoomLink member's turns
    (`api_server_room_dispatch._ensure_hosted_member_session`)."""
    seed = f"{home_install_id}\0{room_id}\0{member_id}\0{target_profile}"
    return f"room_{hashlib.sha256(seed.encode()).hexdigest()[:32]}"


def _is_peer(scope: Any) -> bool:
    """By shape: each copy of this module has its own scope classes."""
    return hasattr(scope, "home_install_id")


@dataclass(frozen=True)
class PeerScope:
    """Agents on this computer in a room whose home is another computer."""
    room_id: str
    home_install_id: str
    members: tuple[tuple[str, str], ...]

    @property
    def sessions(self) -> dict[str, str]:
        return {peer_session_id(self.home_install_id, self.room_id, member_id, profile): member_id
                for member_id, profile in self.members}


@dataclass
class _Feed:
    owner: NativeContext
    scope: RoomScope | PeerScope
    opened_at: int
    deadline: float
    events: deque = field(default_factory=deque)
    seen: deque = field(default_factory=deque)
    size: int = 0
    high_water: int = 0
    lost_through: int = 0
    dropped: int = 0
    projection_drops: int = 0
    source_state: str = "registered_unobserved"
    responding: bool = False
    # Presence notes newer than this (wall clock seconds) are still to show.
    presence_after: float = 0.0


_SHARED_STATE = "_loopdy_room_activity_state"


def _shared_state() -> types.ModuleType:
    """Registrations and feeds shared by every copy of this module.

    A dashboard imports this package twice: Hermes' plugin loader (hooks) and
    the dashboard router (the /groups/activity routes). Without one shared
    state, the routes' copy never saw a registration, so native context never
    listed `native-room-activity-v1`. Only plain containers and a lock live here.
    """
    state = sys.modules.get(_SHARED_STATE)
    if state is None:
        candidate = types.ModuleType(_SHARED_STATE)
        candidate.lock, candidate.registrations, candidate.stream_registrations = threading.Lock(), set(), set()
        candidate.feeds = {}
        state = sys.modules.setdefault(_SHARED_STATE, candidate)
    return state


class RoomActivityHub:
    def __init__(self, *, clock: Callable = time.monotonic, wall_clock: Callable = time.time,
                 presence: RoomPresence | None = None, shared: types.ModuleType | None = None):
        self._clock, self._wall_clock = clock, wall_clock
        self._presence = presence or room_presence()
        if shared is None:
            shared = types.SimpleNamespace(lock=threading.Lock(), registrations=set(), stream_registrations=set(),
                                           feeds={})
        self._lock = shared.lock
        self._registrations: set[str] = shared.registrations
        self._stream_registrations: set[str] = shared.stream_registrations
        self._feeds: dict[str, _Feed] = shared.feeds

    @property
    def peer_typing_available(self) -> bool:
        with self._lock:
            return bool(self._registrations) and bool(self._stream_registrations)

    def attach_stream(self, lease: str) -> None:
        with self._lock:
            self._stream_registrations.add(lease)

    def detach_stream(self, lease: str) -> None:
        with self._lock:
            self._stream_registrations.discard(lease)

    @property
    def available(self) -> bool:
        with self._lock:
            return bool(self._registrations)

    def attach(self, lease: str | None = None) -> str:
        with self._lock:
            lease = lease or uuid.uuid4().hex
            self._registrations.add(lease)
            self._feeds.clear()
            return lease

    def detach(self, lease: str) -> None:
        with self._lock:
            if lease in self._registrations:
                self._registrations.remove(lease)
                self._feeds.clear()

    def _purge(self) -> None:
        for key in [key for key, feed in self._feeds.items() if feed.deadline <= self._clock()]:
            del self._feeds[key]

    def _feed(self, stream_id: str, owner: NativeContext, room_id: str) -> _Feed:
        self._purge()
        feed = self._feeds.get(stream_id)
        if feed is None:
            raise NativeAPIError(410, "activity_reset_required", "The observation feed expired or was retired.")
        if feed.owner != owner or feed.scope.room_id != room_id:
            raise NativeAPIError(404, "activity_not_found", "The observation feed was not found.")
        return feed

    def check(self, stream_id: str, owner: NativeContext, room_id: str) -> None:
        with self._lock:
            self._feed(stream_id, owner, room_id)

    def scope(self, stream_id: str, owner: NativeContext, room_id: str) -> RoomScope | PeerScope:
        with self._lock:
            return self._feed(stream_id, owner, room_id).scope

    def open(self, owner: NativeContext, scope: RoomScope | PeerScope, *, responding: bool = False) -> dict:
        with self._lock:
            self._purge()
            if not self._registrations:
                raise NativeAPIError(503, "activity_unavailable", "The public room observer is unavailable.")
            if len(self._feeds) >= MAX_FEEDS:
                raise NativeAPIError(429, "activity_capacity", "The observation feed limit was reached.")
            stream_id = uuid.uuid4().hex
            feed = _Feed(owner, scope, int(self._wall_clock() * 1000), self._clock() + LEASE_SECONDS,
                         responding=responding,
                         # A member already writing shows within one note interval.
                         presence_after=self._wall_clock() - RESPONDING_INTERVAL_SECONDS)
            self._feeds[stream_id] = feed
            return self._page(stream_id, feed, 0, 8)

    def poll(self, stream_id: str, owner: NativeContext, scope: RoomScope | PeerScope,
             after: int, limit: int) -> dict:
        with self._lock:
            feed = self._feed(stream_id, owner, scope.room_id)
            if feed.scope != scope:
                del self._feeds[stream_id]
                raise NativeAPIError(410, "activity_reset_required", "The room authority or roster changed.")
            feed.scope = scope  # the same room, with the member sessions read just now
            if type(after) is not int or not 0 <= after <= feed.high_water or type(limit) is not int or not 1 <= limit <= 8:
                raise NativeAPIError(422, "invalid_request", "The observation cursor or limit is invalid.")
            feed.deadline = self._clock() + LEASE_SECONDS
            if feed.responding:
                self._pull_presence(feed)
            return self._page(stream_id, feed, after, limit)

    def _pull_presence(self, feed: _Feed) -> None:
        """Adds who is writing, from notes any Hermes process here wrote."""
        sessions = feed.scope.sessions
        if sessions:
            notes = self._presence.since(feed.presence_after, session_ids=tuple(sessions))
        else:
            notes = self._presence.since(feed.presence_after, room_id=feed.scope.room_id)
        members = {member for member, _profile in feed.scope.members}
        for note in notes:
            feed.presence_after = max(feed.presence_after, note["seen_at"])
            try:
                if sessions:
                    observation = {"roomId": feed.scope.room_id, "memberId": sessions[note["session_id"]],
                                   # A session's stream doesn't carry the room's thread and turn.
                                   "threadId": "peer", "turnId": _peer_turn(note.get("turn_id")),
                                   "taskId": note["session_id"], "executionGeneration": 1}
                else:
                    observation = {"roomId": feed.scope.room_id, "memberId": _identifier(note["member_id"]),
                                   "threadId": _identifier(note["thread_id"]), "turnId": _identifier(note["turn_id"]),
                                   "taskId": _identifier(note["task_id"]),
                                   "executionGeneration": _integer(note["execution_generation"], minimum=1)}
            except (KeyError, TypeError, ValueError):
                continue
            if observation["memberId"] not in members:
                continue
            self._record(feed, {**observation, "sourceSequence": None, "kind": RESPONDING})

    def _page(self, stream_id: str, feed: _Feed, after: int, limit: int) -> dict:
        reason = ("projection_loss" if feed.projection_drops else
                  "buffer_loss" if after < feed.lost_through else None)
        events = [] if reason else [event for event, _size in feed.events
                                   if event["observationSequence"] > after][:limit]
        cursor = feed.high_water if reason else events[-1]["observationSequence"] if events else after
        return {
            "schemaVersion": 1, "runtimeId": feed.owner.runtime_id, "roomId": feed.scope.room_id,
            "streamId": stream_id, "sourceState": feed.source_state, "upstreamLoss": "unobservable",
            "openedAt": feed.opened_at,
            "expiresAt": int((self._wall_clock() + max(0, feed.deadline - self._clock())) * 1000),
            "cursor": cursor, "highWater": feed.high_water, "hasMore": not reason and cursor < feed.high_water,
            "resetRequired": reason is not None, "resetReason": reason,
            "droppedTotal": feed.dropped, "projectionDrops": feed.projection_drops,
            "events": copy.deepcopy(events),
        }

    def close(self, stream_id: str, owner: NativeContext, room_id: str) -> dict:
        with self._lock:
            self._feed(stream_id, owner, room_id)
            del self._feeds[stream_id]
            return {"schemaVersion": 1, "roomId": room_id, "streamId": stream_id, "closed": True}

    def observe(self, lease: str, value: dict) -> None:
        room_id = value.get("room_id")
        if value.get("kind") in RESPONDING_SOURCES:
            # The turn may run in another process than the feed: note it for all.
            if lease in self._registrations and isinstance(room_id, str):
                try:
                    observation = project_responding(value)
                except (ValueError, TypeError, UnicodeError, OverflowError):
                    return
                if observation is not None:
                    self._presence.note(
                        "\0".join(("room", observation["roomId"], observation["memberId"], observation["taskId"],
                                    str(observation["executionGeneration"]))),
                        room_id=observation["roomId"], member_id=observation["memberId"],
                        thread_id=observation["threadId"], turn_id=observation["turnId"],
                        task_id=observation["taskId"], execution_generation=observation["executionGeneration"])
            return
        with self._lock:
            self._purge()
            if lease not in self._registrations or not isinstance(room_id, str):
                return
            feeds = [feed for feed in self._feeds.values()
                     if not _is_peer(feed.scope) and feed.scope.room_id == room_id]
            if not feeds:
                return
            try:
                observation = project_observation(value)
                if observation is None:
                    return
            except (ValueError, TypeError, UnicodeError, OverflowError):
                logger.warning("bighelp room observation rejected: invalid_projection")
                for feed in feeds:
                    feed.projection_drops += 1
                    feed.source_state = "unsupported_payload"
                return
            for feed in feeds:
                if observation["memberId"] not in {member[0] for member in feed.scope.members}:
                    feed.projection_drops += 1
                    feed.source_state = "unsupported_payload"
                    continue
                fingerprint = hashlib.sha256(_encoded(observation)).digest()
                if observation["sourceSequence"] is not None and fingerprint in feed.seen:
                    continue
                feed.seen.append(fingerprint)
                while len(feed.seen) > MAX_EVENTS:
                    feed.seen.popleft()
                self._record(feed, observation)

    def observe_stream(self, lease: str, value: dict) -> None:
        """An agent's own writing; only room member sessions matter here: RoomLink's, and
        the hidden ones Hermes' session path runs room turns in (`surface: bot_room`)."""
        session_id = value.get("session_id")
        if (lease not in self._stream_registrations or not isinstance(session_id, str)
                or not (session_id.startswith("room_") or value.get("surface") == "bot_room")
                or len(session_id) > 128):
            return
        self._presence.note("\0".join(("session", session_id)), session_id=session_id,
                            turn_id=_peer_turn(value.get("turn_id")))

    def _record(self, feed: _Feed, observation: dict) -> None:
        feed.high_water += 1
        event = {**observation, "observationSequence": feed.high_water,
                 "observedAt": int(self._wall_clock() * 1000)}
        size = len(_encoded(event))
        feed.events.append((event, size))
        feed.size += size
        feed.source_state = "observed"
        while len(feed.events) > MAX_EVENTS or feed.size > MAX_FEED_BYTES:
            discarded, count = feed.events.popleft()
            feed.size -= count
            feed.lost_through = discarded["observationSequence"]
            feed.dropped += 1


def _peer_turn(value: Any) -> str:
    """The agent's own turn ID when it is a usable identifier."""
    try:
        return _identifier(value)
    except ValueError:
        return "peer"


_HUB = RoomActivityHub(shared=_shared_state())


def activity_hub() -> RoomActivityHub:
    return _HUB


def register_room_activity(ctx: Any) -> Callable[[], None]:
    try:
        from hermes_cli.plugins import VALID_HOOKS
    except ImportError:
        return lambda: None
    if HOOK not in VALID_HOOKS:
        return lambda: None
    hub = activity_hub()
    lease = uuid.uuid4().hex
    ctx.register_hook(HOOK, lambda **payload: hub.observe(lease, payload))
    hub.attach(lease)
    if STREAM_HOOK not in VALID_HOOKS:
        return lambda: hub.detach(lease)
    ctx.register_hook(STREAM_HOOK, lambda **payload: hub.observe_stream(lease, payload))
    hub.attach_stream(lease)

    def stop() -> None:
        hub.detach_stream(lease)
        hub.detach(lease)
    return stop
