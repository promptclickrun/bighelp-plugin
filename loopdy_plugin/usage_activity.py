"""When an agent works, for the bighelp app's Usage page.

Hermes' dashboard already shares tokens and cost per day and per model
(`/api/analytics/usage`, `/api/analytics/models`); the app reads those. This adds
what they leave out, for one agent and the same period: sessions started in each
hour of the host's day, the messages in those sessions, and tokens and cost per
model per day (so one model can be charted).

Read-only, straight from the agent's own `state.db`, counted by when a session
started, as Hermes' analytics are. Nothing here calls a provider or a model.
"""
from __future__ import annotations

from contextlib import closing
import logging
import sqlite3
import time

CAPABILITY = "native-usage-activity-v1"
MAX_DAYS = 365
MAX_MODEL_DAYS = 4_000

logger = logging.getLogger("hermes.plugins.bighelp")


def available() -> bool:
    try:
        from hermes_cli.profiles import get_profile_dir, profile_exists
    except ImportError:
        return False
    return callable(get_profile_dir) and callable(profile_exists)


def activity(agent_id: str, days: int, now: float | None = None) -> dict:
    """Hours, messages and models per day for one agent's last `days` days.

    Raises LookupError for an agent the host doesn't have.
    """
    from hermes_cli.profiles import get_profile_dir, profile_exists
    if not profile_exists(agent_id):
        raise LookupError(agent_id)
    days = max(1, min(int(days), MAX_DAYS))
    cutoff = (time.time() if now is None else now) - days * 86_400
    result = {"days": days, "hours": [0] * 24, "messages": 0, "modelDays": [], "truncated": False}
    path = get_profile_dir(agent_id) / "state.db"
    if not path.is_file():
        return result
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as db:
            _hours(db, cutoff, result)
            _model_days(db, cutoff, result)
    except sqlite3.Error as error:
        # A locked or older database: say nothing rather than something wrong.
        logger.warning("bighelp usage activity unreadable: %s", type(error).__name__)
        return {"days": days, "hours": None, "messages": None, "modelDays": [], "truncated": False}
    return result


def _hours(db: sqlite3.Connection, cutoff: float, result: dict) -> None:
    # The host's local hour, like Hermes' own /insights.
    try:
        rows = db.execute(
            "SELECT CAST(strftime('%H', started_at, 'unixepoch', 'localtime') AS INTEGER) AS hour, "
            "COUNT(*), SUM(COALESCE(message_count, 0)) FROM sessions WHERE started_at > ? GROUP BY hour",
            (cutoff,),
        ).fetchall()
    except sqlite3.OperationalError:
        # No message counts on this database: hours only.
        result["messages"] = None
        rows = [(hour, count, 0) for hour, count in db.execute(
            "SELECT CAST(strftime('%H', started_at, 'unixepoch', 'localtime') AS INTEGER) AS hour, COUNT(*) "
            "FROM sessions WHERE started_at > ? GROUP BY hour", (cutoff,)).fetchall()]
    for hour, count, messages in rows:
        if isinstance(hour, int) and 0 <= hour < 24:
            result["hours"][hour] += int(count or 0)
            if result["messages"] is not None:
                result["messages"] += int(messages or 0)


def _model_days(db: sqlite3.Connection, cutoff: float, result: dict) -> None:
    rows = db.execute(
        "SELECT date(started_at, 'unixepoch', 'localtime') AS day, model, "
        "SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), COALESCE(SUM(estimated_cost_usd), 0) "
        "FROM sessions WHERE started_at > ? AND model IS NOT NULL AND model != '' "
        "GROUP BY day, model ORDER BY day, model LIMIT ?",
        (cutoff, MAX_MODEL_DAYS + 1),
    ).fetchall()
    result["truncated"] = len(rows) > MAX_MODEL_DAYS
    result["modelDays"] = [
        {"day": day, "model": str(model)[:160], "tokens": int(tokens or 0), "cost": round(float(cost or 0), 6)}
        for day, model, tokens, cost in rows[:MAX_MODEL_DAYS]
        if isinstance(day, str)
    ]
