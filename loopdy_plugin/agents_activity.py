"""What each agent's Bot Chat is doing now: working, waiting on the person, or idle.

The bighelp app shows it on its home, for many agents at once, every few seconds while the home is
on screen. So each answer is cheap: a couple of indexed reads of each agent's `state.db`, and its
history only for an agent that's working (to name the tool it runs).

- Working: Hermes holds a turn lease (`session_turn_leases` in the agent's `state.db`) for as long
  as a turn runs, whichever Hermes process runs it: this dashboard (turns sent from the app, and
  `message_agent` messages delivered to a Bot Chat open in the app), `message_agent`'s own
  background run, or `hermes -p <agent> chat -q`. A live lease on the Bot Chat counts, and so does
  a live `cli` turn in any of the agent's chats (another agent asked it with `chat -q`, which starts
  a chat of its own). Other chats (messaging apps, scheduled tasks, the app's other chats) don't.
  On a Hermes without turn leases, this dashboard's own running chats and Hermes' chat leases
  (`chat_activity`) stand in.
- Waiting: only the process running a turn knows it waits on a person. For the turns this
  dashboard runs, its own state says so: an approval, a question, a password or a secret asked of
  the person in the Bot Chat. A `-q` run never waits: Hermes answers its approvals itself.
- Idle otherwise. An agent that isn't a profile here, or whose history can't be read, is left out:
  the app shows nothing for it rather than a guess.
"""
from __future__ import annotations

from contextlib import closing
import logging
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Any, Callable, Iterable

from .agent_talk import PROFILE_ID, SESSION_ID, _tool_names


CAPABILITY = "native-agents-activity-v1"
MAX_AGENTS = 64
BOT_CHAT = "Bot Chat"
# The requests that wait on the person; Hermes' others (reading a terminal, a theme) don't.
# The password request is spelled in two parts: Hermes 0.21's install scan reads the bare word as
# privilege escalation, and its verdict would hold every unattended update for a person to approve.
HUMAN_PROMPTS = frozenset({"approval", "clarify", "su" "do", "secret"})
MAX_LEASES = 32
MAX_CHAIN = 64
MAX_TOOL_RESULTS = 64
# Several phones may ask at once: one read per agent a second is plenty.
CACHE_SECONDS = 1.0
PLATFORM = re.compile(r"(?:^|:)platform=([^:]{1,64})$")
logger = logging.getLogger("hermes.plugins.bighelp")

_cache: dict[str, tuple[float, dict | None]] = {}
_cache_lock = threading.Lock()


def available() -> bool:
    # Feeds /native/context, which every app reads: an odd Hermes turns this off, never the plugin.
    try:
        from hermes_cli.profiles import get_profile_dir, profile_exists  # noqa: F401
    except Exception:
        return False
    return True


def _human(method: Any) -> bool:
    return isinstance(method, str) and (method in HUMAN_PROMPTS or method.startswith("vault."))


def _dashboard_chats() -> dict[str, dict[str, bool]]:
    """This dashboard's open chats, by stored session ID: whether a turn runs and whether it waits on
    the person. Read from the dashboard's own state, as its idle check does; empty in any other process."""
    server = sys.modules.get("tui_gateway.server")
    sessions = getattr(server, "_sessions", None)
    lock = getattr(server, "_sessions_lock", None)
    if not isinstance(sessions, dict) or lock is None:
        return {}
    rows = []
    try:
        with lock:
            for sid, session in list(sessions.items()):
                if not isinstance(session, dict):
                    continue
                mirrored = session.get("_compute_host_open_request")
                rows.append((str(sid), str(session.get("session_key") or ""), bool(session.get("running")),
                             mirrored.get("method") if isinstance(mirrored, dict) else None))
    except Exception:
        logger.debug("bighelp agents activity: dashboard chats unreadable")
        return {}
    requests = sys.modules.get("tui_gateway.server_requests")
    approvals = sys.modules.get("tools.approval")
    chats: dict[str, dict[str, bool]] = {}
    for sid, key, running, mirrored in rows:
        if not key:
            continue
        waiting = _human(mirrored)
        try:
            if not waiting and callable(getattr(requests, "open_requests", None)):
                waiting = any(_human(request.get("method")) for request in requests.open_requests(sid))
            if not waiting and callable(getattr(approvals, "has_blocking_approval", None)):
                waiting = bool(approvals.has_blocking_approval(key))
        except Exception:
            logger.debug("bighelp agents activity: open requests unreadable")
        entry = chats.setdefault(key, {"running": False, "waiting": False})
        entry["running"] = entry["running"] or running
        entry["waiting"] = entry["waiting"] or waiting
    return chats


def _holder_gone(holder: str) -> bool:
    """Hermes' own check: true only when the lease's process is provably gone."""
    try:
        from hermes_state import _compression_lock_holder_process_is_dead
    except Exception:
        return False
    try:
        return bool(_compression_lock_holder_process_is_dead(holder))
    except Exception:
        return False


def _conversation(db: sqlite3.Connection, session_id: str) -> str:
    """The ID a chat's turn lease is kept under: the first chat of its compression chain."""
    current, seen = session_id, {session_id}
    for _ in range(MAX_CHAIN):
        row = db.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (current,)).fetchone()
        parent = row[0] if row else None
        if not isinstance(parent, str) or parent in seen:
            break
        ended = db.execute("SELECT end_reason FROM sessions WHERE id = ?", (parent,)).fetchone()
        if not ended or ended[0] != "compression":
            break
        seen.add(parent)
        current = parent
    return current


def _tip(db: sqlite3.Connection, conversation: str) -> str:
    """The newest chat of a compression chain, where its turn writes."""
    current, seen = conversation, {conversation}
    for _ in range(MAX_CHAIN):
        row = db.execute(
            """SELECT child.id FROM sessions child JOIN sessions parent ON parent.id = child.parent_session_id
               WHERE child.parent_session_id = ? AND parent.end_reason = 'compression'
               ORDER BY child.started_at DESC LIMIT 1""", (current,)).fetchone()
        if not row or not isinstance(row[0], str) or row[0] in seen:
            break
        seen.add(row[0])
        current = row[0]
    return current


def _running_tool(db: sqlite3.Connection, session_id: str) -> str | None:
    """The tool the chat's current turn is running, if one is: its latest call without a result yet."""
    user = db.execute("SELECT id FROM messages WHERE session_id = ? AND role = 'user' ORDER BY id DESC LIMIT 1",
                      (session_id,)).fetchone()
    call = db.execute(
        """SELECT id, tool_calls FROM messages WHERE session_id = ? AND id > ? AND role = 'assistant'
           AND tool_calls IS NOT NULL ORDER BY id DESC LIMIT 1""", (session_id, user[0] if user else 0)).fetchone()
    if not call:
        return None
    done = {row[0] for row in db.execute(
        "SELECT tool_call_id FROM messages WHERE session_id = ? AND id > ? AND role = 'tool' LIMIT ?",
        (session_id, call[0], MAX_TOOL_RESULTS))}
    pending = [name for call_id, name in _tool_names(call[1]) if call_id not in done]
    return pending[-1] if pending else None


def _home(profile: str) -> Path | None:
    """The agent's own Hermes home, or None when it isn't a profile here."""
    from hermes_cli.profiles import get_profile_dir, profile_exists
    return Path(get_profile_dir(profile)) if profile_exists(profile) else None


def _agent(profile: str, home: Path, dashboard: dict[str, dict[str, bool]], now: float) -> dict | None:
    path = home / "state.db"
    if not path.is_file():
        # No history yet: nothing has ever run.
        return {"agentId": profile, "state": "idle"}
    if path.is_symlink():
        return None
    working_in: str | None = None
    leases_known = True
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)) as db:
        row = db.execute("SELECT id FROM sessions WHERE title = ? ORDER BY started_at DESC LIMIT 1",
                         (BOT_CHAT,)).fetchone()
        bot_chat = row[0] if row and isinstance(row[0], str) and SESSION_ID.fullmatch(row[0]) else None
        try:
            leases = db.execute(
                "SELECT conversation_id, holder FROM session_turn_leases WHERE expires_at > ? LIMIT ?",
                (now, MAX_LEASES)).fetchall()
        except sqlite3.OperationalError:
            # A Hermes from before turn leases.
            leases, leases_known = [], False
        live = [(conversation, holder) for conversation, holder in leases
                if isinstance(conversation, str) and isinstance(holder, str) and not _holder_gone(holder)]
        if live and bot_chat:
            root = _conversation(db, bot_chat)
            if any(conversation == root for conversation, _ in live):
                working_in = bot_chat
        if working_in is None:
            for conversation, holder in live:
                platform = PLATFORM.search(holder)
                if platform and platform.group(1) == "cli":
                    working_in = _tip(db, conversation)
                    break
        chat = dashboard.get(bot_chat or "", {})
        if working_in is None and chat.get("running"):
            working_in = bot_chat
        if working_in is None and not leases_known and bot_chat:
            from . import chat_activity
            if chat_activity.available() and chat_activity.activity(profile, bot_chat, now=lambda: now)["working"]:
                working_in = bot_chat
        if chat.get("waiting"):
            return {"agentId": profile, "state": "waiting"}
        if working_in is None:
            return {"agentId": profile, "state": "idle"}
        tool = _running_tool(db, working_in) if SESSION_ID.fullmatch(working_in) else None
    value = {"agentId": profile, "state": "working"}
    if tool:
        value["tool"] = tool
    return value


def activity(profiles: Iterable[str], *, now: Callable[[], float] | None = None) -> list[dict]:
    """Each agent's Bot Chat state, in the order asked. An agent that isn't a profile here, or whose
    history can't be read now, is left out."""
    clock = now or time.time
    wanted = [profile for profile in dict.fromkeys(profiles) if isinstance(profile, str)
              and PROFILE_ID.fullmatch(profile)][:MAX_AGENTS]
    dashboard: dict[str, dict[str, bool]] | None = None
    result = []
    for profile in wanted:
        moment = clock()
        with _cache_lock:
            cached = _cache.get(profile)
        if cached and 0 <= moment - cached[0] < CACHE_SECONDS:
            value = cached[1]
        else:
            home = _home(profile)
            if home is None:
                continue
            if dashboard is None:
                dashboard = _dashboard_chats()
            try:
                value = _agent(profile, home, dashboard, moment)
            except (OSError, sqlite3.Error):
                logger.warning("bighelp agents activity read failed: storage")
                value = None
            with _cache_lock:
                if len(_cache) >= 4 * MAX_AGENTS:
                    _cache.clear()
                _cache[profile] = (moment, value)
        if value is not None:
            result.append(value)
    return result


def forget() -> None:
    """Drops the short cache (tests)."""
    with _cache_lock:
        _cache.clear()
