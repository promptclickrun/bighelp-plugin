"""What an agent's background work did today, for the app's Today page: the subagents, commands and
scheduled tasks that run now or started or ended since the phone's start of the day.

The records and their rules are in `agents_background`. This module picks the day's items, orders them
(running first, newest start first; then the rest, newest end first), and keeps the list to 100.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from . import agents_activity, agents_background
from .agents_background import Work


CAPABILITY = "native-agents-today-v1"
MAX_ITEMS = 100
# The phone's "today" starts at most this long ago, and not in the future (a clock that is a little fast).
MAX_AGE_SECONDS = 48 * 60 * 60
FUTURE_SLACK_SECONDS = 60
# The page asks every few seconds while it is open: one read per agent and day every two seconds.
CACHE_SECONDS = 2.0

_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def available() -> bool:
    return agents_background.available()


def valid_since(since: float, now: float) -> bool:
    """Whether `since` is a start of day the route accepts."""
    return now - MAX_AGE_SECONDS <= since <= now + FUTURE_SLACK_SECONDS


def _order(item: Work) -> tuple:
    if item.state == "running":
        return (0, -(item.started or 0), item.id)
    return (1, -(item.ended or item.started or 0), item.id)


def today(profile: str, since: float, *, now: Callable[[], float] | None = None) -> dict | None:
    """`{"items": [...], "truncated": bool}` for the agent, or None when it isn't a profile here."""
    clock = now or time.time
    moment = clock()
    key = (profile, int(since))
    with _cache_lock:
        cached = _cache.get(key)
    if cached and 0 <= moment - cached[0] < CACHE_SECONDS:
        return cached[1]
    home = agents_activity._home(profile)
    if home is None:
        return None
    items = sorted(agents_background.work(home, since, with_session=True), key=_order)
    value = {"items": [item.payload() for item in items[:MAX_ITEMS]], "truncated": len(items) > MAX_ITEMS}
    with _cache_lock:
        if len(_cache) >= 256:
            _cache.clear()
        _cache[key] = (moment, value)
    return value


def forget() -> None:
    """Drops the short cache (tests)."""
    with _cache_lock:
        _cache.clear()
