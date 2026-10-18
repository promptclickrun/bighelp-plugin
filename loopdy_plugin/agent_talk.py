"""Agents messaging each other: what the agent that was asked is doing.

When an agent runs `hermes -p <agent> chat -q ...`, or Hermes' `message_agent`
delivers a message, the other agent answers in its own chat. Hermes saves each
step there before it runs, so this reads that agent's own history, read-only,
to tell the bighelp app which chat the exchange ran in and what the agent is
doing now ("Nova is searching the web..."). The app sends a hash of each
message it saw in its tool call, never the words, and gets back IDs, times
and a tool name. Nothing is stored.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Iterable


CAPABILITY = "native-agent-talk-v1"
MAX_CALLS = 16
MAX_RUNS = 32
MAX_RUNS_PER_MESSAGE = 4
# How far back a run can have started, and how many of an agent's chats are looked at.
WINDOW_SECONDS = 3 * 24 * 3600
MAX_SESSIONS = 64
MAX_USER_ROWS = 24
MAX_STEP_ROWS = 400
# Longest message the app can match: `message_agent` allows 16,000 characters, plus the signature.
MAX_MESSAGE_CHARS = 20_000
# A run whose chat saw nothing this long stopped without an answer (a stopped process).
STALE_SECONDS = 15 * 60
BOT_CHAT = "Bot Chat"

PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MESSAGE_HASH = re.compile(r"^[0-9a-f]{64}$")
# Hermes signs `message_agent` messages "Message from <robot> Name (@handle): ...".
SIGNATURE = re.compile(r"^Message from [^\n()]{1,80}\(@[A-Za-z0-9][A-Za-z0-9_./@-]{0,127}\):")
logger = logging.getLogger(__name__)


def message_hash(text: str) -> str:
    """SHA-256 of the message with its signature removed and the ends trimmed.

    The app hashes the words in its tool call the same way
    (`AgentTalkMessageHash`), so the two sides meet without sharing the text.
    """
    body = str(text or "").strip()
    signature = SIGNATURE.match(body)
    if signature:
        body = body[signature.end():].strip()
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def available() -> bool:
    # Feeds /native/context, which every app reads: an odd Hermes turns this off, never the plugin.
    try:
        from hermes_cli.profiles import get_profile_dir, profile_exists  # noqa: F401
    except Exception:
        return False
    return True


def _state_db(profile: str) -> Path | None:
    from hermes_cli.profiles import get_profile_dir, profile_exists
    if not profile_exists(profile):
        return None
    path = Path(get_profile_dir(profile)) / "state.db"
    return path if path.is_file() and not path.is_symlink() else None


def _text(content: Any) -> str | None:
    if isinstance(content, str):
        return content if len(content) <= MAX_MESSAGE_CHARS else None
    return None


def _tool_names(raw: Any) -> list[tuple[str, str]]:
    """(call ID, tool name) for an assistant row's tool calls."""
    try:
        calls = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, RecursionError):
        return []
    result = []
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name, call_id = function.get("name"), call.get("id") or call.get("call_id")
        if isinstance(name, str) and TOOL_NAME.fullmatch(name) and isinstance(call_id, str):
            result.append((call_id, name))
    return result


def _progress(db: sqlite3.Connection, session_id: str, after_row: int, ended: bool, started: float,
              now: float) -> tuple[bool, str | None]:
    """Whether the agent is still on the message at `after_row`, and the tool it's running."""
    pending: dict[str, str] = {}
    last_seen = started
    answered = False
    rows = db.execute(
        """SELECT role, tool_calls, tool_call_id, content, timestamp FROM messages
           WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?""",
        (session_id, after_row, MAX_STEP_ROWS)).fetchall()
    for role, tool_calls, tool_call_id, content, stamp in rows:
        if isinstance(stamp, (int, float)):
            last_seen = max(last_seen, float(stamp))
        if role == "user":
            # The next message started a new turn: this one is over.
            answered = True
            break
        if role == "assistant":
            calls = _tool_names(tool_calls)
            if calls:
                pending.update(calls)
            elif isinstance(content, str) and content.strip():
                answered = True
                break
        elif role == "tool" and isinstance(tool_call_id, str):
            pending.pop(tool_call_id, None)
    working = not answered and not ended and now - last_seen < STALE_SECONDS
    tool = next(reversed(pending.values()), None) if working and pending else None
    return working, tool


def _runs_for_agent(path: Path, hashes: set[str], now: float) -> list[dict]:
    since = now - WINDOW_SECONDS
    found: list[dict] = []
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)) as db:
        # Runs started for a message (`chat -q`), and the Bot Chat `message_agent` delivers to.
        sessions = db.execute(
            """SELECT id, ended_at FROM sessions WHERE started_at > ? ORDER BY started_at DESC LIMIT ?""",
            (since, MAX_SESSIONS)).fetchall()
        sessions += db.execute(
            """SELECT id, ended_at FROM sessions WHERE title = ? AND archived = 0
               ORDER BY started_at DESC LIMIT 2""", (BOT_CHAT,)).fetchall()
        seen: set[str] = set()
        for session_id, ended_at in sessions:
            if session_id in seen or not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
                continue
            seen.add(session_id)
            rows = db.execute(
                """SELECT id, content, timestamp FROM messages
                   WHERE session_id = ? AND role = 'user' AND timestamp > ? ORDER BY timestamp DESC LIMIT ?""",
                (session_id, since, MAX_USER_ROWS)).fetchall()
            for row_id, content, stamp in rows:
                text = _text(content)
                if text is None:
                    continue
                digest = message_hash(text)
                if digest not in hashes:
                    continue
                started = float(stamp) if isinstance(stamp, (int, float)) else now
                working, tool = _progress(db, session_id, int(row_id), ended_at is not None, started, now)
                found.append({"sessionId": session_id, "message": digest, "startedAt": started,
                              "working": working, "tool": tool})
    return found


def find_runs(calls: Iterable[tuple[str, str]], *, now: Callable[[], float] | None = None) -> list[dict]:
    """Runs of other agents on this computer started by these messages, newest first.

    `calls` are (agent ID, message hash) pairs. An agent that isn't a profile
    here (another computer's, or one since removed) is skipped.
    """
    wanted: dict[str, set[str]] = {}
    for agent_id, digest in list(calls)[:MAX_CALLS]:
        if PROFILE_ID.fullmatch(agent_id) and MESSAGE_HASH.fullmatch(digest):
            wanted.setdefault(agent_id, set()).add(digest)
    clock = (now or time.time)()
    runs: list[dict] = []
    for agent_id, hashes in wanted.items():
        path = _state_db(agent_id)
        if path is None:
            continue
        try:
            found = _runs_for_agent(path, hashes, clock)
        except (OSError, sqlite3.Error):
            logger.warning("bighelp agent talk read failed: storage")
            continue
        per_message: dict[str, int] = {}
        for run in sorted(found, key=lambda value: value["startedAt"], reverse=True):
            if per_message.get(run["message"], 0) >= MAX_RUNS_PER_MESSAGE:
                continue
            per_message[run["message"]] = per_message.get(run["message"], 0) + 1
            runs.append({"agentId": agent_id, **run})
    runs.sort(key=lambda value: value["startedAt"], reverse=True)
    return runs[:MAX_RUNS]
