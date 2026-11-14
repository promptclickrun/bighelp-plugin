"""A chat's turn, live, while the agent's gateway runs it.

Hermes 0.23 runs an agent's Bot Chat turns in the agent's gateway: the messages other agents send it
(`message_agent`), and anything else the gateway admits there. The dashboard the bighelp app talks to
sees none of that turn until Hermes saves it. The gateway's own socket streams every turn as numbered
`event` frames to a client attached to the chat (`session.resume`), and a process running as the same
user can dial it the way Hermes' own owner forward does (`tui_gateway.owner_forward`).

So while the app follows such a chat, this keeps one watch on it: attached to the gateway, holding the
frames the app draws (text, reasoning, tools, status) in a short numbered buffer, and stopping once no
app has asked for a while. The app asks for the frames after the last one it has, and a request waits a
little for new ones. Stop interrupts the turn on the same socket. Nothing is written.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any


CAPABILITY = "native-chat-live-v1"
# The frames a turn is drawn from. Hermes' others (usage, skins, platform lists) stay out.
EVENTS = frozenset({"message.start", "message.delta", "message.complete", "reasoning.delta", "thinking.delta",
                    "tool.generating", "tool.start", "tool.progress", "tool.complete", "status.update",
                    "session.info"})
MAX_EVENTS = 4_000
MAX_ANSWER_EVENTS = 600
# Bytes of one answer; the route allows 2 MiB.
MAX_ANSWER_BYTES = 1_500_000
MAX_TEXT = 8_000
MAX_ITEMS = 50
MAX_DEPTH = 4
MAX_WAIT_SECONDS = 15.0
# A watch nobody asked about for this long lets go of the gateway.
IDLE_SECONDS = 45.0
MAX_WATCHES = 8
# After a home had no gateway, how long before trying again.
RETRY_SECONDS = 5.0
LEASE_PID = re.compile(r"(?:^|:)pid=(\d{1,10})(?::|$)")
logger = logging.getLogger("hermes.plugins.bighelp")

_watches: dict[tuple[str, str], "_Watch"] = {}
_no_gateway: dict[str, float] = {}
_lock = threading.Lock()


def available() -> bool:
    # Feeds /native/context, which every app reads: an odd Hermes turns this off, never the plugin.
    try:
        from hermes_cli.profiles import get_profile_dir, profile_exists  # noqa: F401
        from tui_gateway.owner_forward import connect_owner, discover_owner  # noqa: F401
    except Exception:
        return False
    return True


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def gateway_pid(home: Path) -> int | None:
    """The gateway serving this agent: its own, or the default agent's, which can serve every agent."""
    folders = [home] + ([home.parent.parent] if home.parent.name == "profiles" else [])
    for folder in folders:
        try:
            lock = json.loads((folder / "gateway.lock").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = lock.get("pid") if isinstance(lock, dict) else None
        if lock.get("kind") == "hermes-gateway" and isinstance(pid, int) and not isinstance(pid, bool) \
                and pid > 1 and _alive(pid):
            return pid
    return None


def gateway_turn(home: Path, session_id: str, now: float) -> dict[str, Any] | None:
    """The turn the agent's gateway runs in this chat now (`since`, the `tool` it runs), or None.

    Hermes holds a turn lease for as long as a turn runs, under the first chat of its compression
    chain, naming the process that runs it. Only the gateway's own lease counts: a turn this dashboard
    runs streams to the app already, and another process's isn't on the gateway's socket."""
    from .agents_activity import _conversation, _holder_gone, _running_tool
    path = home / "state.db"
    gateway = gateway_pid(home)
    if gateway is None or gateway == os.getpid() or not path.is_file() or path.is_symlink():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)) as db:
        try:
            root = _conversation(db, session_id)
            leases = db.execute("SELECT holder, acquired_at FROM session_turn_leases "
                                "WHERE conversation_id = ? AND expires_at > ?", (root, now)).fetchall()
        except sqlite3.OperationalError:
            # A Hermes from before turn leases.
            return None
        for holder, acquired in leases:
            match = LEASE_PID.search(holder) if isinstance(holder, str) else None
            if match and int(match.group(1)) == gateway and not _holder_gone(holder):
                since = float(acquired) if isinstance(acquired, (int, float)) and not isinstance(acquired, bool) \
                    else None
                return {"since": since, "tool": _running_tool(db, session_id)}
    return None


def _bounded(value: Any, depth: int = 0) -> Any:
    """The value as the app draws it: long text cut, long lists and deep objects trimmed."""
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if depth >= MAX_DEPTH:
        return None
    if isinstance(value, list):
        return [_bounded(item, depth + 1) for item in value[:MAX_ITEMS]]
    if isinstance(value, dict):
        return {key: _bounded(item, depth + 1) for key, item in list(value.items())[:MAX_ITEMS]
                if isinstance(key, str)}
    return None


def _shape(kind: str, payload: Any) -> dict | None:
    """The frame's payload as the dashboard's socket would carry it, bounded; None to leave it out."""
    payload = payload if isinstance(payload, dict) else {}
    if kind == "session.info":
        # Its tool lists and settings are the gateway's; only whether a turn runs matters here.
        running = payload.get("running")
        return {"running": running} if isinstance(running, bool) else None
    if kind in ("message.delta", "reasoning.delta", "thinking.delta") and not (
            isinstance(payload.get("text"), str) and payload["text"]):
        return None
    return _bounded(payload)


class _Watch:
    """One chat on its agent's gateway, attached while an app follows it."""

    def __init__(self, home: Path, session_id: str):
        self.home, self.session_id = home, session_id
        self.changed = threading.Condition()
        self.events: deque[dict] = deque(maxlen=MAX_EVENTS)
        self.epoch: str | None = None
        # The gateway's newest frame number, of any kind: where "from now" starts.
        self.newest = 0
        self.running = False
        self.attached = False
        self.ended = False
        self.asked = time.monotonic()
        self.thread = threading.Thread(target=self._run, name=f"bighelp-chat-live-{session_id[:24]}", daemon=True)

    def _run(self) -> None:
        try:
            asyncio.run(self._follow())
        except Exception as error:  # thread boundary: the next request starts a new watch
            from tui_gateway.owner_forward import OwnerNotRunning
            if isinstance(error, OwnerNotRunning):
                with _lock:
                    _no_gateway[str(self.home)] = time.monotonic()
            logger.info("Bighelp chat live watch ended: %s", type(error).__name__)
        finally:
            with self.changed:
                self.ended = True
                self.changed.notify_all()

    async def _follow(self) -> None:
        from tui_gateway.owner_forward import connect_owner, discover_owner
        endpoint = await asyncio.to_thread(discover_owner, self.home)
        async with connect_owner(self.home, endpoint, max_size=8 * 2 ** 20) as client:
            attached = await client.rpc("session.resume", session_id=self.session_id, omit_messages=True)
            epoch = attached.get("replay_epoch")
            last = attached.get("last_sequence")
            with self.changed:
                self.epoch = epoch if isinstance(epoch, str) else None
                self.running = attached.get("running") is True
                self.newest = last if type(last) is int else 0
                self.changed.notify_all()
            # The frames the gateway still holds: the turn running now, from its start.
            replay = await client.rpc("session.events.since", session_id=self.session_id, last_sequence=0,
                                      replay_epoch=self.epoch)
            for frame in replay.get("events") or [] if isinstance(replay, dict) else []:
                self._add(frame, replayed=True)
            with self.changed:
                self.attached = True
                self.changed.notify_all()
            while True:
                with self.changed:
                    if time.monotonic() - self.asked > IDLE_SECONDS:
                        return
                try:
                    frame = await asyncio.wait_for(client.events.get(), 1.0)
                except TimeoutError:
                    continue
                if isinstance(frame, BaseException):
                    return
                params = frame.get("params") if isinstance(frame, dict) else None
                if isinstance(params, dict):
                    self._add(params)

    def _add(self, frame: Any, *, replayed: bool = False) -> None:
        if not isinstance(frame, dict):
            return
        kind, seq = frame.get("type"), frame.get("seq")
        if type(seq) is not int or frame.get("session_id") not in (None, self.session_id):
            return
        with self.changed:
            self.newest = max(self.newest, seq)
        if kind not in EVENTS:
            return
        payload = _shape(kind, frame.get("payload"))
        if payload is None:
            return
        with self.changed:
            if self.events and seq <= self.events[-1]["seq"]:
                return
            # A replayed frame is history: whether a turn runs now is what `session.resume` said.
            if not replayed:
                if kind == "message.start":
                    self.running = True
                elif kind == "message.complete":
                    self.running = False
                elif kind == "session.info":
                    self.running = payload["running"]
            self.events.append({"seq": seq, "type": kind, "payload": payload})
            self.changed.notify_all()


def _watch(home: Path, session_id: str) -> _Watch | None:
    key = (str(home), session_id)
    now = time.monotonic()
    with _lock:
        watch = _watches.get(key)
        if watch is None or watch.ended:
            refused = _no_gateway.get(str(home))
            if refused is not None and now - refused < RETRY_SECONDS:
                return None
            for stale in [k for k, w in _watches.items() if w.ended]:
                del _watches[stale]
            if len(_watches) >= MAX_WATCHES:
                # The one asked about longest ago lets go; it ends at its next frame or second.
                oldest = min(_watches, key=lambda k: _watches[k].asked)
                _watches.pop(oldest).asked = float("-inf")
            watch = _Watch(home, session_id)
            _watches[key] = watch
            watch.thread.start()
        watch.asked = now
        return watch


def follow(home: Path, session_id: str, after: int | None, epoch: str | None, wait: float,
           latest: bool = False) -> dict[str, Any]:
    """The frames after `after` (the turn running now from its start when None or when the gateway's
    numbering started again), waiting up to `wait` seconds for new ones. With `latest`, no frames: only where
    the gateway is now (`last`), for an app that already shows what Hermes saved of the turn."""
    watch = _watch(home, session_id)
    if watch is None:
        return {"attached": False, "running": False, "events": [], "reset": False}
    deadline = time.monotonic() + max(0.0, min(wait, MAX_WAIT_SECONDS))
    fresh: list[dict] = []
    reset = False
    with watch.changed:
        while True:
            watch.asked = time.monotonic()
            if latest and (watch.attached or watch.ended):
                break
            if watch.attached or watch.ended:
                events = list(watch.events)
                reset = epoch is not None and watch.epoch is not None and epoch != watch.epoch
                # The buffer let go of frames the app hasn't seen: start again from the turn's start.
                if after is not None and not reset and len(events) == MAX_EVENTS and events[0]["seq"] > after + 1:
                    reset = True
                if after is None or reset:
                    starts = [index for index, event in enumerate(events) if event["type"] == "message.start"]
                    fresh = events[starts[-1]:] if starts and watch.running else []
                else:
                    fresh = [event for event in events if event["seq"] > after]
                if fresh or watch.ended:
                    break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            watch.changed.wait(remaining)
        running, attached, current_epoch = watch.running, watch.attached and not watch.ended, watch.epoch
        newest = watch.newest
    answer, size = [], 0
    for event in fresh[:MAX_ANSWER_EVENTS]:
        size += len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
        if answer and size > MAX_ANSWER_BYTES:
            break
        answer.append(event)
    value: dict[str, Any] = {"attached": attached, "running": running, "events": answer, "reset": reset}
    if current_epoch:
        value["epoch"] = current_epoch
    if latest:
        value["last"] = newest
    elif answer:
        value["last"] = answer[-1]["seq"]
    elif after is not None and not reset:
        value["last"] = after
    return value


def interrupt(home: Path, session_id: str) -> bool:
    """Stops the turn the gateway runs in this chat, as its own Stop does. False when none ran."""
    from tui_gateway.owner_forward import connect_owner, discover_owner

    async def stop() -> bool:
        endpoint = await asyncio.to_thread(discover_owner, home)
        async with connect_owner(home, endpoint) as client:
            attached = await client.rpc("session.resume", session_id=session_id, omit_messages=True)
            if attached.get("running") is not True:
                return False
            await client.rpc("session.interrupt", session_id=attached.get("session_id") or session_id,
                             execution_generation=attached.get("execution_generation"))
            return True

    try:
        return asyncio.run(stop())
    except Exception as error:
        logger.warning("Bighelp chat live stop failed: %s", type(error).__name__)
        return False


def forget() -> None:
    """Lets every watch go (tests)."""
    with _lock:
        for watch in _watches.values():
            watch.asked = float("-inf")
        _watches.clear()
        _no_gateway.clear()
