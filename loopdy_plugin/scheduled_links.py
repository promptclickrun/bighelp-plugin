"""Where a tap on a scheduled task's alert goes.

A scheduled run happens in a session of its own (``cron_<job>_<time>``, or ``local-<sha256>`` in
Hermes' unified runtime) that no person opened, so naming it sends the phone nowhere useful. When
the agent made or changed the job in a chat, through Hermes' cron tool, that chat is the job's
chat: the link is recorded then, never guessed later. A run's alert names that chat while it still
exists for the same agent, else the job itself, which the app opens in Scheduled tasks.

Alerts carry these places as coordinates in their own namespace (``scheduled.chat.<session>`` and
``scheduled.job.<job>``, hashed with the agent like any chat), so a phone never takes a scheduled
alert for a reply in the chat it shows. Only phones that said they open them get them
(``scheduledLinks`` in the phone's preferences); others keep the run's own session.
"""
from __future__ import annotations

import importlib
import json
import re
from typing import Any

# `cronjob_manage`, and `cronjob` on older Hermes (newer Hermes maps the old name to the new one).
CRON_TOOLS = frozenset({"cronjob_manage", "cronjob"})
CHAT_PREFIX = "scheduled.chat."
JOB_PREFIX = "scheduled.job."
# Per agent; the oldest link goes first. Jobs removed outside the cron tool leave theirs behind.
MAX_LINKS = 512
_MAX_RESULT_CHARS = 262_144
# Turns no person reads as a chat: a job made there has no chat to open.
_UNATTENDED_SOURCES = frozenset({"cron", "workflow", "subagent", "tool", "kanban", "bot_room", "webhook",
                                 "api_server", "delegate", "worker"})
_JOB = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LEGACY_RUN = re.compile(r"^cron_(.+)_\d{8}_\d{6}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_chats(profile TEXT NOT NULL, job_id TEXT NOT NULL, session_id TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(profile,job_id));
"""


def _object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value) > _MAX_RESULT_CHARS:
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _job(value: Any) -> str | None:
    return value if isinstance(value, str) and _JOB.fullmatch(value) else None


def is_cron_tool(tool_name: Any) -> bool:
    return isinstance(tool_name, str) and tool_name in CRON_TOOLS


def cron_tool_change(tool_name: Any, args: Any, result: Any) -> tuple[str, str] | None:
    """What one finished cron tool call did to a job: ``("link", job)`` when it made or changed
    the job, ``("unlink", job)`` when it removed it, else None. Reads only Hermes' own answer, so a
    refused or failed call changes nothing."""
    if not is_cron_tool(tool_name):
        return None
    arguments, answer = _object(args), _object(result)
    if arguments is None or answer is None or answer.get("success") is not True:
        return None
    action = arguments.get("action")
    action = action.strip().lower() if isinstance(action, str) else ""
    job = answer.get("job") if isinstance(answer.get("job"), dict) else {}
    if action == "create":
        found = _job(answer.get("job_id")) or _job(job.get("job_id"))
        return ("link", found) if found else None
    if action == "update":
        found = _job(job.get("job_id"))
        return ("link", found) if found else None
    if action == "remove":
        removed = answer.get("removed_job")
        found = _job(removed.get("id")) if isinstance(removed, dict) else None
        return ("unlink", found) if found else None
    return None


def job_of_run(profile: str, session_id: str) -> str | None:
    """The job whose scheduled run this is. Hermes says so to plugins in the run itself
    (``ctx.current_cron_execution()``); older runs name the job in their session ID."""
    try:
        execution = importlib.import_module("cron.execution_identity").current_cron_execution()
    except Exception:  # Hermes without the API, or one that can't say here.
        execution = None
    job = _job(getattr(execution, "job_id", None))
    if job and getattr(execution, "profile", profile) == profile:
        return job
    match = _LEGACY_RUN.fullmatch(session_id) if isinstance(session_id, str) else None
    return _job(match.group(1)) if match else None


def is_unattended(row: dict[str, Any]) -> bool:
    source = row.get("source")
    return isinstance(source, str) and source.strip().lower() in _UNATTENDED_SOURCES


def link(db, profile: str, job: str, session_id: str, now: int) -> None:
    db.execute("INSERT INTO scheduled_chats VALUES(?,?,?,?) ON CONFLICT(profile,job_id) DO UPDATE SET "
               "session_id=excluded.session_id,created_at=excluded.created_at", (profile, job, session_id, now))
    db.execute("DELETE FROM scheduled_chats WHERE profile=? AND rowid NOT IN (SELECT rowid FROM scheduled_chats "
               "WHERE profile=? ORDER BY created_at DESC, rowid DESC LIMIT ?)", (profile, profile, MAX_LINKS))


def unlink(db, profile: str, job: str, session_id: str | None = None) -> None:
    if session_id is None:
        db.execute("DELETE FROM scheduled_chats WHERE profile=? AND job_id=?", (profile, job))
    else:
        db.execute("DELETE FROM scheduled_chats WHERE profile=? AND job_id=? AND session_id=?", (profile, job, session_id))


def linked_chat(db, profile: str, job: str) -> str | None:
    row = db.execute("SELECT session_id FROM scheduled_chats WHERE profile=? AND job_id=?", (profile, job)).fetchone()
    return row[0] if row else None

