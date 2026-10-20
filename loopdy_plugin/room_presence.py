"""Who is writing in a group chat right now, shared by every Hermes process here.

A member's turn can run in any Hermes process on this computer (the gateway,
a `hermes serve` backend, or the API server for an agent that joins a room on
another computer), while the app's room feed is served by the dashboard. The
hooks write a short note to one file at the Hermes root and the feed reads it.
Notes hold coordinates only, never text, and expire within seconds.
"""
from __future__ import annotations

from contextlib import closing
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Callable


# One note per member turn this often is enough; deltas arrive per token.
NOTE_INTERVAL_SECONDS = 2.0
NOTE_LIFETIME_SECONDS = 30.0
MAX_KEYS = 256
MAX_ROWS = 64
logger = logging.getLogger(__name__)


def default_path() -> Path:
    """The Hermes root, not a profile's home: every process on this computer shares it."""
    try:
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
    except ImportError:
        from hermes_constants import get_hermes_home
        root = get_hermes_home()
    return Path(root) / "plugin-data" / "loopdy" / "room-presence.sqlite3"


class RoomPresence:
    COLUMNS = ("room_id", "member_id", "thread_id", "turn_id", "task_id", "execution_generation", "session_id")

    def __init__(self, path: Callable[[], Path] = default_path, *, clock: Callable = time.time,
                 monotonic: Callable = time.monotonic):
        self._path, self._clock, self._monotonic = path, clock, monotonic
        self._lock = threading.Lock()
        self._noted: dict[str, float] = {}

    def _connect(self) -> sqlite3.Connection:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=1)
        db.execute("""CREATE TABLE IF NOT EXISTS presence (
            key TEXT PRIMARY KEY, room_id TEXT, member_id TEXT, thread_id TEXT, turn_id TEXT,
            task_id TEXT, execution_generation INTEGER, session_id TEXT, seen_at REAL NOT NULL)""")
        return db

    def note(self, key: str, **coordinates) -> bool:
        """Record that `key` is writing now; at most once per interval per key."""
        now = self._monotonic()
        with self._lock:
            last = self._noted.get(key)
            if last is not None and now - last < NOTE_INTERVAL_SECONDS:
                return False
            self._noted[key] = now
            while len(self._noted) > MAX_KEYS:
                del self._noted[next(iter(self._noted))]
        values = [coordinates.get(column) for column in self.COLUMNS]
        seen_at = self._clock()
        try:
            with closing(self._connect()) as db, db:
                db.execute(
                    f"""INSERT INTO presence (key, {", ".join(self.COLUMNS)}, seen_at)
                        VALUES (?, {", ".join("?" for _ in self.COLUMNS)}, ?)
                        ON CONFLICT(key) DO UPDATE SET turn_id=excluded.turn_id, seen_at=excluded.seen_at""",
                    (key, *values, seen_at))
                db.execute("DELETE FROM presence WHERE seen_at < ?", (seen_at - NOTE_LIFETIME_SECONDS,))
        except (OSError, sqlite3.Error):
            logger.warning("bighelp room presence note failed: storage")
            return False
        return True

    def since(self, after: float, *, room_id: str | None = None, session_ids: tuple[str, ...] = ()) -> list[dict]:
        """Notes newer than `after` for one hosted room or for these sessions, oldest first."""
        if room_id is None and not session_ids:
            return []
        path = self._path()
        if not path.exists():
            return []
        if room_id is not None:
            where, params = "room_id = ?", [room_id]
        else:
            where, params = f"session_id IN ({', '.join('?' for _ in session_ids)})", list(session_ids)
        try:
            with closing(self._connect()) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    f"""SELECT {", ".join(self.COLUMNS)}, seen_at FROM presence
                        WHERE seen_at > ? AND seen_at > ? AND {where} ORDER BY seen_at LIMIT ?""",
                    (after, self._clock() - NOTE_LIFETIME_SECONDS, *params, MAX_ROWS)).fetchall()
        except (OSError, sqlite3.Error):
            logger.warning("bighelp room presence read failed: storage")
            return []
        return [dict(row) for row in rows]


_PRESENCE = RoomPresence()


def room_presence() -> RoomPresence:
    return _PRESENCE
