"""Whether a chat is busy in another Hermes process, and stopping that run.

An agent asked by another answers in its own `hermes -p <agent> chat` process. Its steps go to that
agent's state.db but never through the dashboard the bighelp app talks to, so a chat open in the app
sees nothing until it's read again. Hermes keeps a lease for each chat a process holds
(`<agent home>/runtime/active_sessions.json`: the pid, its start time and the surface). A live lease
from anything but the dashboard's own chats ("tui", "desktop") means another process has that chat.
It's busy when that process started after the chat's last message, or the chat's latest turn is
still going.
"""
from __future__ import annotations

from contextlib import closing
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import time
from typing import Any, Callable

from .agent_talk import _progress


CAPABILITY = "native-chat-activity-v1"
# The dashboard's own chats: the app's chat itself, never "another process".
OWN_SURFACES = frozenset({"tui", "desktop"})
MAX_LEASE_BYTES = 262_144
logger = logging.getLogger("hermes.plugins.bighelp")


def available() -> bool:
    # Feeds /native/context, which every app reads: an odd Hermes turns this off, never the plugin.
    try:
        from hermes_cli.profiles import get_profile_dir, profile_exists  # noqa: F401
        from hermes_cli.active_sessions import _pid_liveness  # noqa: F401
    except Exception:
        return False
    return hasattr(signal, "SIGINT") and hasattr(os, "kill")


def _home(profile: str) -> Path:
    from hermes_cli.profiles import get_profile_dir
    return Path(get_profile_dir(profile))


def _holders(home: Path, session_id: str) -> list[dict[str, Any]]:
    """Live leases on the chat from processes other than the dashboard's own chats."""
    from hermes_cli.active_sessions import _pid_liveness
    path = home / "runtime" / "active_sessions.json"
    try:
        if path.stat().st_size > MAX_LEASE_BYTES:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = data.get("entries") if isinstance(data, dict) else data
    holders = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("session_id") != session_id:
            continue
        pid = entry.get("pid")
        surface = str(entry.get("surface") or "").strip().lower()
        if surface in OWN_SURFACES or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 \
                or pid == os.getpid():
            continue
        # Hermes' own check: the pid is alive and is still the process that took the lease.
        if _pid_liveness(pid, entry.get("process_start_time")) is True:
            holders.append(entry)
    return holders


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def activity(profile: str, session_id: str, *, now: Callable[[], float] | None = None) -> dict[str, Any]:
    """`working`, the Hermes tool running now, when the run started, and whether Stop can reach it."""
    idle = {"working": False, "tool": None, "since": None, "canStop": False}
    home = _home(profile)
    holders = _holders(home, session_id)
    if not holders:
        return idle
    clock = (now or time.time)()
    started = min((_number(holder.get("started_at")) or clock) for holder in holders)
    working, tool = False, None
    try:
        with closing(sqlite3.connect(f"file:{home / 'state.db'}?mode=ro", uri=True, timeout=2)) as db:
            last = db.execute("SELECT id, timestamp FROM messages WHERE session_id = ? AND role = 'user' "
                              "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
            newest = db.execute("SELECT max(timestamp) FROM messages WHERE session_id = ?",
                                (session_id,)).fetchone()
            if last:
                working, tool = _progress(db, session_id, int(last[0]), False, _number(last[1]) or started, clock)
            # Its message isn't saved yet: the process took the chat after everything in it.
            if not working and (newest is None or newest[0] is None or started >= float(newest[0])):
                working = True
    except sqlite3.Error:
        working = True
    if not working:
        return idle
    return {"working": True, "tool": tool, "since": started,
            # Stop needs the process's start time, so a reused pid can't be signalled.
            "canStop": any(holder.get("process_start_time") is not None for holder in holders)}


def stop(profile: str, session_id: str) -> bool:
    """Interrupts the run as Ctrl-C would: Hermes stops the turn and the process ends."""
    stopped = False
    for holder in _holders(_home(profile), session_id):
        if holder.get("process_start_time") is None:
            continue
        try:
            os.kill(int(holder["pid"]), signal.SIGINT)
            stopped = True
        except (ProcessLookupError, PermissionError) as error:
            logger.debug("bighelp chat stop skipped: %s", type(error).__name__)
    return stopped
