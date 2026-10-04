"""Quiet Hours: a daily window when the host sends a device no alerts.

The bighelp app sends each device's window with its grant: a start and an end
minute of the day and the device's IANA time zone. The host works out "now" in
that zone, so the window follows daylight saving time. A window can cross
midnight (22:00 to 07:00). The start is inside the window and the end is not.
Equal start and end make an empty window.

An alert that falls in the window is never sent and never kept for later: the
reply is in the chat when the person opens it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any

CAPABILITY = "native-notification-quiet-hours-v1"
MINUTES_PER_DAY = 1440
_TIME_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+){0,3}\Z")
_MAX_TIME_ZONE = 64


class QuietHoursError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _zone(name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise QuietHoursError("quiet_hours_time_zone_invalid") from None


def available() -> bool:
    """Managed notifications can run here and the host can read time zones."""
    try:
        import fcntl  # noqa: F401  (managed notifications need it)
        _zone("UTC")
    except (ImportError, QuietHoursError):
        return False
    return True


def window(*, enabled: Any, start_minute: Any, end_minute: Any, time_zone: Any) -> dict[str, Any]:
    """A checked window, as stored and returned to the app."""
    if type(enabled) is not bool:
        raise QuietHoursError("quiet_hours_invalid")
    for minute in (start_minute, end_minute):
        if type(minute) is not int or not 0 <= minute < MINUTES_PER_DAY:
            raise QuietHoursError("quiet_hours_invalid")
    if (not isinstance(time_zone, str) or len(time_zone) > _MAX_TIME_ZONE
            or _TIME_ZONE.fullmatch(time_zone) is None):
        raise QuietHoursError("quiet_hours_time_zone_invalid")
    _zone(time_zone)
    return {"enabled": enabled, "startMinute": start_minute, "endMinute": end_minute, "timeZone": time_zone}


def contains(start_minute: int, end_minute: int, minute: int) -> bool:
    """Whether a minute of the day is in the window [start, end), across midnight too."""
    if start_minute == end_minute:
        return False
    if start_minute < end_minute:
        return start_minute <= minute < end_minute
    return minute >= start_minute or minute < end_minute


def is_quiet(value: dict[str, Any] | None, now: float) -> bool:
    """Whether the window holds at ``now`` (Unix seconds) on the device's own clock."""
    if not value or not value.get("enabled"):
        return False
    try:
        local = datetime.fromtimestamp(now, timezone.utc).astimezone(_zone(value["timeZone"]))
    except (QuietHoursError, KeyError, OverflowError, OSError, ValueError):
        # A zone the host can no longer read: send rather than lose the alert silently.
        return False
    return contains(value["startMinute"], value["endMinute"], local.hour * 60 + local.minute)
