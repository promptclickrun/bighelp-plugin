"""The chat a live voice call continues, and the spoken turns it adds back to it.

A call opens in one chat. Before the voice model listens, the offer reads that chat from the agent's own
``state.db`` (``snapshot``): its newest messages with their roles, the summary of anything older that
Hermes compacted, and the request a turn is still working on. Every provider gets the same snapshot:
GPT-Live as conversation items (``context``), Gemini in the system instruction its token locks.

What the person and the voice model say to each other comes back as each line of it ends (``save``,
plugin ``native-voice-history-v1``): the app sends the finished transcripts and this appends them to the
chat as ordinary messages. Nothing runs: Hermes takes rows another writer saved into the chat's live
history at its next turn (``_adopt_out_of_band_turns``), so the next typed message knows the call. Rows
are written only while no turn runs in the chat, so a spoken line never lands between a turn's tool call
and its result; a busy chat answers ``voice_chat_busy`` and the app tries again. Each item is saved at
most once (``voice_history.sqlite3``, by call and item), so a retry after a lost answer or a reconnect
never doubles a line.

Every read and write stays in the one chat the app named, in the profile it named, on this computer.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import inspect
import json
import logging
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Iterable

CAPABILITY = "native-voice-history-v1"
# GPT-Live's own bounds for conversation items (`live_voice_provider._instructions`).
MAX_ENTRIES = 16
MAX_ENTRY_CHARS = 800
MAX_ENTRIES_BYTES = 8000
MAX_SUMMARY_CHARS = 1500
MAX_WORKING_CHARS = 300
# Enough of the chat's newest rows to find 16 messages among tool calls and their results.
READ_ROWS = 160
MAX_ITEMS = 32
MAX_ITEM_CHARS = 4000
# A spoken line is stamped with the time it was said, at most this long ago.
MAX_ITEM_AGE_SECONDS = 6 * 3600
KEEP_SAVED_SECONDS = 30 * 86_400
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,179}")
_JSON_PREFIX = "\x00json:"
_SUMMARY_MARK = "COMPACTION SUMMARY BELOW]"
# What a person wrote (or steered into a running turn); Hermes' notices about its own work are left out.
_PERSON_KINDS = frozenset({"", "steer"})
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
logger = logging.getLogger("hermes.plugins.bighelp")


class VoiceHistoryError(Exception):
    """A save that didn't happen, with the fixed code the app reads."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Snapshot:
    """What the call starts with: ``entries`` are ``{"role", "text"}`` in the chat's order, oldest first."""
    entries: tuple[dict[str, str], ...] = ()
    summary: str = ""
    working: str = ""


def available() -> bool:
    try:
        from hermes_cli.profiles import get_profile_dir, profile_exists  # noqa: F401
        from hermes_state import SessionDB  # noqa: F401
    except Exception:
        return False
    return True


def snapshot(profile: str, session_id: str, *, now: float | None = None) -> Snapshot:
    """The chat ``session_id`` of agent ``profile`` as the call starts. Empty when it isn't this agent's
    chat or can't be read: the call still starts, as a new conversation."""
    try:
        path = _state_db(profile)
        if path is None or not SESSION_ID.fullmatch(session_id):
            return Snapshot()
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)) as db:
            chat = _chat(db, profile, session_id)
            if chat is None:
                return Snapshot()
            root, tip = chat
            rows = _rows(db, tip)
            summary = _summary(db, tip)
            running = _running(db, (session_id, root, tip), now or time.time())
    except Exception as error:
        logger.warning("Bighelp voice history: chat unreadable (%s)", type(error).__name__)
        return Snapshot()
    entries = fit(rows)
    newest = rows[0] if rows else None
    working = newest["text"] if running and newest and newest["role"] == "user" else ""
    return Snapshot(entries, _cut(summary, MAX_SUMMARY_CHARS), _cut(" ".join(working.split()), MAX_WORKING_CHARS))


def fit(rows: Iterable[dict[str, str]], budget: int = MAX_ENTRIES_BYTES) -> tuple[dict[str, str], ...]:
    """The newest of ``rows`` (newest first) that fit ``budget`` UTF-8 bytes and GPT-Live's item limits,
    oldest first."""
    kept, used = [], 0
    for row in rows:
        text = _cut(row["text"], MAX_ENTRY_CHARS)
        size = len(text.encode("utf-8"))
        if len(kept) >= MAX_ENTRIES or used + size > budget:
            break
        kept.append({"role": row["role"], "text": text})
        used += size
    return tuple(reversed(kept))


def save(profile: str, session_id: str, voice_id: str, items: list[dict[str, Any]],
         *, now: float | None = None) -> dict[str, list[str]]:
    """Append the finished spoken ``items`` of call ``voice_id`` to the chat, each at most once.

    Returns the IDs written now (``saved``) and those written before (``known``). Raises
    ``VoiceHistoryError``: ``voice_chat_busy`` while a turn runs in the chat (nothing is written;
    try again when it ends), ``voice_chat_unavailable`` when it isn't this agent's chat."""
    now = now or time.time()
    path = _state_db(profile)
    if path is None or not SESSION_ID.fullmatch(session_id):
        raise VoiceHistoryError("voice_chat_unavailable")
    from hermes_state import SessionDB
    with _lock_for(path):
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)) as db:
            chat = _chat(db, profile, session_id)
            if chat is None:
                raise VoiceHistoryError("voice_chat_unavailable")
            root, tip = chat
            if _running(db, (session_id, root, tip), now):
                raise VoiceHistoryError("voice_chat_busy")
        ledger = _Ledger(path.parent / "plugin-data" / "loopdy" / "voice_history.sqlite3")
        saved, known = [], []
        with closing(SessionDB(db_path=path)) as store:
            stamps = "timestamp" in inspect.signature(store.append_message).parameters
            for item in items:
                if not ledger.claim(root, voice_id, item["id"], now):
                    known.append(item["id"])
                    continue
                text = item["text"].strip()
                if item["role"] == "assistant" and item.get("interrupted") and not text.endswith("…"):
                    text += "…"  # The person cut the reply off here.
                extra = {}
                if stamps and isinstance(item.get("at"), (int, float)):
                    extra["timestamp"] = min(now, max(now - MAX_ITEM_AGE_SECONDS, float(item["at"])))
                try:
                    store.append_message(tip, item["role"], text, **extra)
                except Exception:
                    ledger.release(root, voice_id, item["id"])
                    raise
                saved.append(item["id"])
        ledger.prune(now)
    return {"saved": saved, "known": known}


def _state_db(profile: str) -> Path | None:
    from hermes_cli.profiles import get_profile_dir, profile_exists
    if not profile_exists(profile):
        return None
    path = Path(get_profile_dir(profile)) / "state.db"
    return path if path.is_file() and not path.is_symlink() else None


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _chat(db: sqlite3.Connection, profile: str, session_id: str) -> tuple[str, str] | None:
    """The chat's compression root (where its turn lease is kept) and tip (where it writes), or None
    when this store has no such chat for this agent."""
    from .agents_activity import _conversation, _tip
    profiled = "profile_name" in _columns(db, "sessions")
    row = db.execute(f"SELECT {'profile_name' if profiled else 'NULL'} FROM sessions WHERE id = ?",
                     (session_id,)).fetchone()
    if row is None or row[0] not in (None, "", profile):
        return None
    root = _conversation(db, session_id)
    return root, _tip(db, root)


def _rows(db: sqlite3.Connection, tip: str) -> list[dict[str, str]]:
    """The chat's newest messages that people read, newest first: the person's words and the agent's
    replies, never tool calls, tool results or Hermes' own notices."""
    columns = _columns(db, "messages")
    kind = "display_kind" if "display_kind" in columns else "NULL"
    where = " AND active = 1" if "active" in columns else ""
    where += " AND compacted = 0" if "compacted" in columns else ""
    where += " AND _compressed_summary = 0" if "_compressed_summary" in columns else ""
    rows = []
    for role, content, tool_calls, display_kind in db.execute(
            f"SELECT role, content, tool_calls, {kind} FROM messages WHERE session_id = ?{where} "
            "ORDER BY id DESC LIMIT ?", (tip, READ_ROWS)):
        if role == "user" and (display_kind or "") not in _PERSON_KINDS:
            continue
        if role == "assistant" and (display_kind or tool_calls):
            continue
        if role not in ("user", "assistant"):
            continue
        text = _text(content)
        if text:
            rows.append({"role": role, "text": text})
    return rows


def _summary(db: sqlite3.Connection, tip: str) -> str:
    """What Hermes wrote when it compacted the chat's older part, without its preamble."""
    if "_compressed_summary" not in _columns(db, "messages"):
        return ""
    row = db.execute("SELECT content FROM messages WHERE session_id = ? AND _compressed_summary = 1 "
                     "ORDER BY id DESC LIMIT 1", (tip,)).fetchone()
    text = _text(row[0]) if row else ""
    return text.split(_SUMMARY_MARK, 1)[-1].strip()


def _running(db: sqlite3.Connection, ids: tuple[str, ...], now: float) -> bool:
    """A turn runs in the chat: Hermes holds its turn lease in any process, or this dashboard runs it."""
    from .agents_activity import _dashboard_chats, _holder_gone
    try:
        leases = db.execute(
            f"SELECT holder FROM session_turn_leases WHERE expires_at > ? AND conversation_id IN "
            f"({','.join('?' * len(ids))})", (now, *ids)).fetchall()
    except sqlite3.OperationalError:
        leases = []  # A Hermes from before turn leases: this dashboard's own chats stand in.
    if any(isinstance(holder, str) and not _holder_gone(holder) for (holder,) in leases):
        return True
    chats = _dashboard_chats()
    return any(chats.get(identifier, {}).get("running") for identifier in ids)


def _text(content: Any) -> str:
    """A stored message's words: plain text, or the text parts of a multimodal message."""
    if not isinstance(content, str):
        return ""
    if content.startswith(_JSON_PREFIX):
        try:
            value = json.loads(content[len(_JSON_PREFIX):])
        except ValueError:
            return ""
        if isinstance(value, str):
            content = value
        elif isinstance(value, list):
            parts = []
            for part in value:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                elif isinstance(part, dict) and "image" in str(part.get("type", "")):
                    parts.append("[picture]")
            content = "\n".join(parts)
        else:
            return ""
    return _CONTROL.sub("", content).strip()


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    with _locks_lock:
        return _locks.setdefault(str(path), threading.Lock())


class _Ledger:
    """Which spoken items each chat holds already, by call and item. A claim comes before the write and
    goes only when the write fails, so a crash between them loses a line rather than doubling it."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS saved(
                conversation TEXT NOT NULL, voice TEXT NOT NULL, item TEXT NOT NULL, saved REAL NOT NULL,
                PRIMARY KEY (conversation, voice, item))""")

    def _db(self) -> closing[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.isolation_level = None  # Each statement commits on its own.
        return closing(db)

    def claim(self, conversation: str, voice: str, item: str, now: float) -> bool:
        with self._db() as db:
            return db.execute("INSERT OR IGNORE INTO saved VALUES (?, ?, ?, ?)",
                              (conversation, voice, item, now)).rowcount == 1

    def release(self, conversation: str, voice: str, item: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM saved WHERE conversation = ? AND voice = ? AND item = ?",
                       (conversation, voice, item))

    def prune(self, now: float) -> None:
        with self._db() as db:
            db.execute("DELETE FROM saved WHERE saved < ?", (now - KEEP_SAVED_SECONDS,))
