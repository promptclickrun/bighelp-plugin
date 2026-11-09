"""What an agent's background work did today, for the app's Today page: the subagents, commands and
scheduled tasks that run now or started or ended since the phone's start of the day.

The records and their rules are in `agents_background`. This module picks the day's items, orders them
(running first, newest start first; then the rest, newest end first), and keeps the list to 200. A
`message_agent` message also says where it is with the agent asked (`agent_deliveries`): `delivery` with
its `state` and, while that agent works, its `tool`.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from . import agents_activity, agents_background
from .agents_background import Work


CAPABILITY = "native-agents-today-v1"
# One item opened for its steps or output (`agents_today_detail`).
DETAIL_CAPABILITY = "native-agents-today-detail-v1"
MAX_ITEMS = 200
# The app lists a week, grouped by day: `since` may be up to 31 days back. In the future it may be up to
# 48 hours ahead (around midnight a fast phone clock is a day ahead) and is read as now.
MAX_PAST_SECONDS = 31 * 24 * 60 * 60
MAX_FUTURE_SECONDS = 48 * 60 * 60
# The page asks every few seconds while it is open: one read per agent and day every two seconds.
CACHE_SECONDS = 2.0

_cache: dict[tuple[str, int], tuple[float, dict]] = {}
_cache_lock = threading.Lock()


def available() -> bool:
    return agents_background.available()


def valid_since(since: float, now: float) -> bool:
    """Whether `since` is a start of day the route accepts."""
    return -MAX_PAST_SECONDS <= since - now <= MAX_FUTURE_SECONDS


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
    value = {"items": _with_deliveries([item.payload() for item in items[:MAX_ITEMS]]),
             "truncated": len(items) > MAX_ITEMS}
    with _cache_lock:
        if len(_cache) >= 256:
            _cache.clear()
        _cache[key] = (moment, value)
    return value


def _with_deliveries(rows: list[dict]) -> list[dict]:
    """Each message's delivery as the agent asked keeps it; the reply itself stays out of the list."""
    from . import agent_deliveries
    asked = [(row["agentId"], row["deliveryId"]) for row in rows if row.get("agentId") and row.get("deliveryId")]
    if not asked or not agent_deliveries.available():
        return rows
    found: dict[str, dict] = {}
    for start in range(0, len(asked), agent_deliveries.MAX_DELIVERIES):
        try:
            batch = agent_deliveries.deliveries(asked[start:start + agent_deliveries.MAX_DELIVERIES])
        except Exception:
            continue
        found.update({item["deliveryId"]: item for item in batch})
    for row in rows:
        if (delivery := found.get(row.get("deliveryId") or "")) is not None:
            row["delivery"] = {key: delivery[key] for key in ("state", "tool") if key in delivery}
    return rows


def forget() -> None:
    """Drops the short cache (tests)."""
    with _cache_lock:
        _cache.clear()
