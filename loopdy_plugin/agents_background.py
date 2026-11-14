"""The work an agent runs in the background, read from Hermes' own records, from any Hermes process.

Three kinds, each from the record Hermes keeps for it (all read-only, none of it written here):

- Subagents (`delegate_task` with `background`): the agent's `async_delegations` table in `state.db`.
  A row that says running counts only while the process that owns it is alive (Hermes' own check);
  a row whose owner is gone is `unknown`, as Hermes later marks it.
- Commands (`terminal(background=true)`): `processes.json` in the agent's home lists the running ones.
  An entry counts only while its process is alive with the start time Hermes wrote. A command that
  finished is not in that file; Hermes saves a one-line notice of it in the chat (`process_complete`).
  On Hermes 0.23, `message_agent` waits for the other agent in such a command (`bot_mode_dm.py
  --run-delivery`): it is the message to that agent, with the agent and the delivery ID it can be
  followed by (`agent_deliveries`), never a command line.
- Scheduled tasks: the cron execution ledger (`cron/executions.db`) books each run with its owner
  process, and `cron/jobs.json` names the job.

`agents_activity` uses `running` for the count on the app's home, and `agents_today` uses `window` for
the Today page. Both are cheap: indexed reads of small tables, bounded files, no history scans.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable

from .agent_talk import SESSION_ID


CAPABILITY = "native-agents-background-v1"
TITLE_LIMIT = 120
DETAIL_LIMIT = 140
ACTIVITY_TITLE_LIMIT = 60
MAX_ROWS = 300
MAX_LIVE = 64
MAX_SESSIONS = 100
MAX_SESSION_MESSAGES = 300
MAX_JSON_CHARS = 1_000_000
MAX_FILE_BYTES = 4 * 1024 * 1024
LIVE_DELEGATIONS = ("running", "stalling", "finalizing")
ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
# What Hermes writes in a chat when a background command ends (`process_completion_display_text`).
PROCESS_TEXT = re.compile(
    r"Background Process (Finished|Failed to Start|Failed|Terminated|Lost)(?: \(exit (-?\d{1,5}|\?)\))?(?:: (.+))?")
PROCESS_BATCH_TEXT = re.compile(r"(\d{1,6}) Background Processes Finished")
# Hermes 0.23's `message_agent` waiter: `python bot_mode_dm.py --run-delivery ... query-file <file>
# --profile-home <home> <hermes> -p <agent>`.
DELIVERY_RUNNER = re.compile(r"bot_mode_dm\.py\s+--run-delivery\b")
DELIVERY_AGENT = re.compile(r"(?:^|\s)-p\s+([a-z0-9][a-z0-9_-]{0,63})\s*$")
DELIVERY_FILE = re.compile(r"\squery-file\s+(?:'([^']{1,1024})'|(\S{1,1024}))")
# The waiter's JSON ends with the delivery ID, after any reply.
DELIVERY_ID_FIELD = re.compile(r'"delivery_id":\s*"([0-9a-f]{64})"')
PROCESS_STATES = {"Finished": "done", "Failed": "failed", "Failed to Start": "failed", "Lost": "failed",
                  "Terminated": "stopped"}
# The words for a delegation's final status (`status` in its completion event).
DELEGATION_STATES = {
    "completed": "done", "success": "done",
    "failed": "failed", "error": "failed", "timeout": "failed", "stalled": "failed", "rejected": "failed",
    "interrupted": "stopped", "cancelled": "stopped",
}
FALLBACK_TITLES = {"subagent": "Subagent", "command": "Command", "scheduled": "Scheduled task"}
logger = logging.getLogger("hermes.plugins.bighelp")

_jobs_cache: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}
_jobs_lock = threading.Lock()


def available() -> bool:
    # Feeds /native/context: the same Hermes profile helpers as the agents' activity.
    from . import agents_activity
    return agents_activity.available()


@dataclass(frozen=True)
class Work:
    """One piece of background work. `title` is empty when nothing usable is known."""
    id: str
    kind: str
    state: str
    title: str = ""
    started: float | None = None
    ended: float | None = None
    detail: str | None = None
    session: str | None = None
    # A `message_agent` message: the agent it went to, and its delivery ID when known.
    agent: str | None = None
    delivery: str | None = None
    agent_name: str | None = None

    def payload(self) -> dict:
        value: dict[str, Any] = {"id": self.id, "kind": self.kind, "title": self.title or FALLBACK_TITLES[self.kind],
                                 "state": self.state}
        if self.started is not None:
            value["startedAt"] = self.started
        if self.ended is not None:
            value["endedAt"] = self.ended
        if self.detail:
            value["detail"] = self.detail
        if self.session:
            value["sessionId"] = self.session
        if self.agent:
            value["agentId"] = self.agent
        if self.delivery:
            value["deliveryId"] = self.delivery
        if self.agent_name:
            value["agentName"] = self.agent_name
        return value


def _redact(text: str) -> str | None:
    """Hermes' own redaction, forced, with URL credentials too where this Hermes can: this text goes to a
    phone. None when it can't be redacted (no redaction here, or it failed): the caller sends no text."""
    try:
        from agent.redact import redact_sensitive_text
    except Exception:
        return None
    try:
        try:
            return redact_sensitive_text(text, force=True, redact_url_credentials=True)
        except TypeError:
            # An older Hermes without those options.
            return redact_sensitive_text(text, force=True)
    except Exception:
        return None


# A URL is left whole. A host path starts with `/` or `~` (not after `.`, a word or `/`, which make it
# relative) and ends at a space, a quote or shell punctuation.
URL_OR_HOST_PATH = re.compile(
    r"""(?P<url>[A-Za-z][A-Za-z0-9+.-]*://[^\s'"`<>]*)|(?<![\w.:/~])(?P<path>(?:~[\w.-]*)?/[^\s'"`;|&<>()\[\]{},]*)""")


def _hide_paths(text: str) -> str:
    """Each absolute or home-relative path becomes its last component, so no folder of the computer
    reaches a phone: `python /home/sam/build.py` is `python build.py`. Relative paths and URLs stay. A
    token with nothing after its slashes (`/`, `~/`) stays as it is."""
    def last(match: re.Match) -> str:
        path = match.group("path")
        if path is None:
            return match.group()
        parts = [part for part in path.split("/") if part]
        return path if not parts or (path.startswith("~") and len(parts) == 1) else parts[-1]
    return URL_OR_HOST_PATH.sub(last, text)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def _line(value: Any, limit: int) -> str:
    """The first line with words in it, whitespace collapsed, redacted, shown with file names only (no
    folders of the computer) and cut to `limit` characters."""
    if not isinstance(value, str):
        return ""
    for raw in value[:4_000].splitlines():
        text = " ".join(raw.split())
        if text:
            redacted = _redact(text)
            return _cut(_hide_paths(redacted), limit) if isinstance(redacted, str) else ""
    return ""


def _command(value: Any) -> str:
    """Hermes' `_short_command`: one line, 80 characters."""
    text = " ".join(str(value or "").split())
    return _line(text[:77] + "..." if len(text) > 80 else text, TITLE_LIMIT)


def _delivery(command: Any) -> tuple[str, str | None] | None:
    """The agent a `message_agent` waiter waits on, and the delivery ID Hermes derives from its message file
    (`_dm_delivery_id`) while that file is still there; None for any other command."""
    if not isinstance(command, str) or not DELIVERY_RUNNER.search(command):
        return None
    agent = DELIVERY_AGENT.search(command)
    if agent is None:
        return None
    delivery = None
    if (found := DELIVERY_FILE.search(command)) is not None:
        path = Path(found.group(1) or found.group(2))
        try:
            if path.is_file():
                delivery = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
        except OSError:
            delivery = None
    return agent.group(1), delivery


def _agent_name(agent: str) -> str:
    from .adapter import profile_display_name
    try:
        return _cut(profile_display_name(agent), 80) or agent
    except Exception:
        return agent


def _message_title(agent: str) -> str:
    return _cut(f"Message to {_agent_name(agent)}", TITLE_LIMIT)


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def _stamp(value: Any) -> float | None:
    """An ISO time as the cron ledger writes it, as epoch seconds."""
    if not isinstance(value, str):
        return None
    try:
        return _epoch(datetime.fromisoformat(value).timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def _json(text: Any) -> dict:
    if not isinstance(text, str) or len(text) > MAX_JSON_CHARS:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list:
    """The rows, or none on a Hermes that has no such table or column yet."""
    try:
        return db.execute(sql, params).fetchall()
    except sqlite3.OperationalError as error:
        if str(error).startswith("no such "):
            return []
        raise


def _alive(pid: Any, started: Any, *, unreadable_is_alive: bool, unrecorded_is_own_pid: bool = False) -> bool:
    """Whether the process is alive and still the one Hermes recorded (a recycled PID has another start
    time). Two answers are each Hermes check's own: `unreadable_is_alive` when the start time can't be read
    now, and `unrecorded_is_own_pid` (the cron ledger's) when none was recorded: then only the Hermes
    process this code runs in counts. Unlike Hermes, which fails safe to alive, False when its liveness
    helpers can't be imported: here, unsure is not running."""
    try:
        from gateway.status import _pid_exists, get_process_start_time, start_time_fingerprints_match
    except Exception:
        return False
    try:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not _pid_exists(pid):
            return False
        if started is None:
            return pid == os.getpid() if unrecorded_is_own_pid else True
        current = get_process_start_time(pid)
        if current is None:
            return unreadable_is_alive
        return bool(start_time_fingerprints_match(started, current))
    except Exception:
        return False


def _session(db: sqlite3.Connection | None, *candidates: Any) -> str | None:
    """The first candidate that is a stored chat of this agent."""
    if db is None:
        return None
    for candidate in candidates:
        if isinstance(candidate, str) and SESSION_ID.fullmatch(candidate) and _rows(
                db, "SELECT 1 FROM sessions WHERE id = ?", (candidate,)):
            return candidate
    return None


def _state_db(home: Path) -> sqlite3.Connection | None:
    path = home / "state.db"
    if path.is_symlink() or not path.is_file():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)


# --- Subagents -------------------------------------------------------------------------------------------

def _goal(task: dict, event: dict) -> str:
    """A subagent's plain title: a batch's group label, else its first goal."""
    goals = task.get("goals") if isinstance(task.get("goals"), list) else event.get("goals")
    first = goals[0] if isinstance(goals, list) and goals else None
    for candidate in (event.get("group"), first, task.get("goal"), event.get("goal")):
        if line := _line(candidate, TITLE_LIMIT):
            return line
    return ""


def _delegation_detail(event: dict) -> str:
    """One line on how a finished subagent went: its summary, or why it failed."""
    results = [result for result in event.get("results") or [] if isinstance(result, dict)]
    failed = next((result for result in results if result.get("error")), None)
    if event.get("is_batch") or results:
        order = [failed.get("error")] if failed else []
        order += [event.get("error")] + [result.get("summary") for result in results]
    elif DELEGATION_STATES.get(event.get("status")) == "done":
        order = [event.get("summary")]
    else:
        order = [event.get("error"), event.get("summary")]
    return next((line for text in order if (line := _line(text, DETAIL_LIMIT))), "")


def delegations(db: sqlite3.Connection, since: float | None, *, with_session: bool = False) -> list[Work]:
    """Subagents running now (owner alive); with `since`, the ones that started or ended at or after it too."""
    where = "state IN (?, ?, ?)"
    params: tuple = LIVE_DELEGATIONS
    if since is not None:
        where += " OR dispatched_at >= ? OR completed_at >= ?"
        params += (since, since)
    rows = _rows(db, f"""SELECT delegation_id, state, dispatched_at, completed_at, updated_at, owner_pid,
                         owner_started_at, task_json, event_json, parent_session_id, origin_session_id, origin_session
                         FROM async_delegations WHERE {where}
                         ORDER BY COALESCE(completed_at, dispatched_at) DESC LIMIT ?""",
                 params + (MAX_ROWS if since is not None else MAX_LIVE,))
    work = []
    for (delegation_id, state, dispatched, completed, updated, pid, owner_started,
         task_json, event_json, parent, origin_session_id, origin) in rows:
        if not isinstance(delegation_id, str) or not ITEM_ID.fullmatch(delegation_id):
            continue
        started, task, event = _epoch(dispatched), _json(task_json), _json(event_json)
        if state in LIVE_DELEGATIONS:
            if _alive(pid, owner_started, unreadable_is_alive=False):
                final, ended, detail = "running", None, ""
            elif since is None or not any((_epoch(value) or 0) >= since for value in (dispatched, updated)):
                continue
            else:
                # Hermes marks it unknown when it finds the owner gone.
                final, ended, detail = "unknown", None, ""
        else:
            final, ended = DELEGATION_STATES.get(state, "unknown"), _epoch(completed)
            detail = _delegation_detail(event)
        session = _session(db, parent, origin_session_id, origin) if with_session else None
        work.append(Work(f"delegation:{delegation_id}", "subagent", final, _goal(task, event), started, ended,
                         detail or None, session))
    return work


# --- Commands --------------------------------------------------------------------------------------------

def _read_json_file(path: Path) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError:
        return None


def running_command_entries(home: Path, db: sqlite3.Connection | None, *,
                            with_session: bool = False) -> list[tuple[Work, dict]]:
    """Background commands running now, each with its `processes.json` entry: entries whose process is alive
    with the recorded start time. Commands a subagent started (`sa-` task IDs) are the subagent's work, as
    Hermes treats them."""
    entries = _read_json_file(home / "processes.json")
    work = []
    for entry in (entries if isinstance(entries, list) else [])[:MAX_LIVE]:
        if not isinstance(entry, dict):
            continue
        process_id = entry.get("session_id")
        if not isinstance(process_id, str) or not ITEM_ID.fullmatch(process_id):
            continue
        if entry.get("pid_scope", "host") != "host" or any(
                isinstance(entry.get(key), str) and entry[key].startswith("sa-") for key in ("owner_task_id", "task_id")):
            continue
        # Every profile's checkpoint can list a shared process's commands: a chat that isn't this agent's
        # means the command isn't either.
        parent = entry.get("parent_session_id")
        if db is not None and isinstance(parent, str) and parent and not _rows(
                db, "SELECT 1 FROM sessions WHERE id = ?", (parent,)):
            continue
        if not _alive(entry.get("pid"), entry.get("host_start_time"), unreadable_is_alive=True):
            continue
        session = _session(db, parent) if with_session else None
        message = _delivery(entry.get("command"))
        title = _message_title(message[0]) if message else _command(entry.get("command"))
        work.append((Work(f"process:{process_id}", "command", "running", title, _epoch(entry.get("started_at")),
                          None, None, session, *(message or (None, None)),
                          _agent_name(message[0]) if message else None), entry))
    return work


def running_commands(home: Path, db: sqlite3.Connection | None, *, with_session: bool = False) -> list[Work]:
    """Background commands running now."""
    return [item for item, _entry in running_command_entries(home, db, with_session=with_session)]


def finished_commands(db: sqlite3.Connection, since: float) -> list[Work]:
    """Background commands that ended at or after `since`, from the notice Hermes saved in the chat. Only
    the chats active in the window are read, newest messages first, never the whole history."""
    window = since - 300
    sessions = _rows(db, """SELECT id FROM sessions WHERE COALESCE(last_activity_at, started_at) >= ?
                            ORDER BY COALESCE(last_activity_at, started_at) DESC LIMIT ?""", (window, MAX_SESSIONS))
    work = []
    for (session_id,) in sessions:
        if not isinstance(session_id, str) or not SESSION_ID.fullmatch(session_id):
            continue
        for message_id, stamp, metadata, head, tail in _rows(db, """
                SELECT id, timestamp, display_metadata, substr(content, 1, 1200), substr(content, -400) FROM messages
                WHERE display_kind = 'process_complete' AND id IN (
                    SELECT id FROM messages INDEXED BY idx_messages_session
                    WHERE session_id = ? AND timestamp >= ? ORDER BY timestamp DESC LIMIT ?)""",
                (session_id, since, MAX_SESSION_MESSAGES)):
            ended = _epoch(stamp)
            item = _finished_command(message_id, session_id, ended, _json(metadata).get("display_text"), head, tail)
            if item is not None and ended is not None and ended >= since:
                work.append(item)
    return work


def _finished_message(head: Any, tail: Any) -> tuple[str, str | None] | None:
    """A finished `message_agent` waiter's agent and delivery ID, from the start of Hermes' notice (its
    `Command:` line: the short title cuts it before the script's name) and its end (the JSON the waiter
    printed ends with the delivery ID, after a reply of any length)."""
    if not isinstance(head, str):
        return None
    command = re.search(r"^Command: (.+)$", head, re.MULTILINE)
    message = _delivery(command.group(1).strip()) if command else None
    if message is None:
        return None
    found = DELIVERY_ID_FIELD.search(tail if isinstance(tail, str) else "") or DELIVERY_ID_FIELD.search(head)
    return message[0], found.group(1) if found else None


def _finished_command(message_id: Any, session_id: str, ended: float | None, text: Any,
                      head: Any = None, tail: Any = None) -> Work | None:
    if not isinstance(text, str) or len(text) > 4_096 or not isinstance(message_id, int):
        return None
    text = " ".join(text.split())
    if batch := PROCESS_BATCH_TEXT.fullmatch(text):
        return Work(f"message:{message_id}", "command", "done", f"{batch.group(1)} commands", None, ended,
                    None, session_id)
    match = PROCESS_TEXT.fullmatch(text)
    if match is None:
        return None
    outcome, exit_code, command = match.groups()
    if (message := _finished_message(head, tail)) is not None:
        # The waiter's own end says nothing of the answer: the app reads the delivery for that.
        return Work(f"message:{message_id}", "command", "done", _message_title(message[0]), None, ended, None,
                    session_id, *message, _agent_name(message[0]))
    detail = f"Exit {exit_code}" if exit_code and exit_code != "?" else None
    return Work(f"message:{message_id}", "command", PROCESS_STATES[outcome], _command(command), None, ended,
                detail, session_id)


# --- Scheduled tasks -------------------------------------------------------------------------------------

def _job_names(home: Path) -> dict[str, str]:
    """Job names by ID from `cron/jobs.json`, reread only when the file changes."""
    path = home / "cron" / "jobs.json"
    try:
        stat = path.stat()
    except OSError:
        return {}
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _jobs_lock:
        cached = _jobs_cache.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    data = _read_json_file(path)
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = [{**job, "id": job.get("id") or key} for key, job in jobs.items() if isinstance(job, dict)]
    names = {}
    for job in jobs if isinstance(jobs, list) else []:
        if isinstance(job, dict) and isinstance(job.get("id"), str):
            names[job["id"]] = _line(job.get("name"), TITLE_LIMIT) or _line(job.get("prompt"), TITLE_LIMIT)
    with _jobs_lock:
        if len(_jobs_cache) >= 128:
            _jobs_cache.clear()
        _jobs_cache[str(path)] = (stamp, names)
    return names


def _cron_session(db: sqlite3.Connection | None, job_id: str, started: float) -> str | None:
    """The chat a scheduled run made: Hermes names it `cron_<job>_<date>_<time>`, started with the run."""
    if db is None:
        return None
    prefix = f"cron_{job_id}_"
    name = re.compile(re.escape(prefix) + r"\d{8}_\d{6}\Z")
    best = None
    for session_id, begun in _rows(db, "SELECT id, started_at FROM sessions WHERE id >= ? AND id < ? ORDER BY id DESC LIMIT 200",
                                   (prefix, prefix[:-1] + "`")):
        gap = abs((_epoch(begun) or 0) - started)
        if isinstance(session_id, str) and name.fullmatch(session_id) and gap <= 600 and (best is None or gap < best[0]):
            best = (gap, session_id)
    return best[1] if best else None


def cron_runs(home: Path, db: sqlite3.Connection | None, since: float | None, *,
              with_session: bool = False) -> list[Work]:
    """Scheduled task runs going now (the ledger says running and its owner process is alive); with `since`,
    the ones that started or ended at or after it too. A run Hermes never started (`missed`) isn't work."""
    path = home / "cron" / "executions.db"
    if path.is_symlink() or not path.is_file():
        return []
    columns = "id, job_id, status, claimed_at, started_at, finished_at, pid, process_started_at"
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)) as ledger:
        rows = _rows(ledger, f"SELECT {columns} FROM executions WHERE status = 'running' LIMIT ?", (MAX_LIVE,))
        if since is not None:
            rows += _rows(ledger, f"""SELECT {columns} FROM executions
                                      WHERE status IN ('completed', 'failed', 'unknown', 'cancelled')
                                      ORDER BY claimed_at DESC LIMIT ?""", (MAX_ROWS,))
    names = _job_names(home)
    outcomes = {"completed": "done", "failed": "failed", "cancelled": "stopped", "unknown": "unknown"}
    work = []
    for _id, job_id, status, claimed, begun, finished, pid, owner_started in rows:
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            continue
        started, ended = _stamp(begun) or _stamp(claimed), _stamp(finished)
        if started is None:
            continue
        if status == "running":
            if _alive(pid, owner_started, unreadable_is_alive=True, unrecorded_is_own_pid=True):
                final = "running"
            elif since is not None and started >= since:
                final = "unknown"
            else:
                continue
        elif since is not None and max(started, ended or 0) >= since:
            final = outcomes.get(status, "unknown")
        else:
            continue
        session = _cron_session(db, job_id, started) if with_session else None
        work.append(Work(f"cron:{job_id}:{int(started)}", "scheduled", final, names.get(job_id, ""), started,
                         ended if final != "running" else None, None, session))
    return work


def cron_job_runs(home: Path, job_id: str) -> list[dict[str, Any]]:
    """One job's newest ledger rows as `{start, end, error}`: `start` (whole epoch seconds) is the one in the
    run's item ID, `end` is when it finished and `error` is the ledger's own word, as written."""
    path = home / "cron" / "executions.db"
    if path.is_symlink() or not path.is_file() or not JOB_ID.fullmatch(job_id):
        return []
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)) as ledger:
        rows = _rows(ledger, """SELECT claimed_at, started_at, finished_at, error FROM executions
                                WHERE job_id = ? AND status != 'missed' ORDER BY claimed_at DESC LIMIT ?""",
                     (job_id, MAX_ROWS))
    runs = []
    for claimed, begun, finished, error in rows:
        if (started := _stamp(begun) or _stamp(claimed)) is not None:
            runs.append({"start": int(started), "end": _stamp(finished), "error": error})
    return runs


# --- Together --------------------------------------------------------------------------------------------

def _safe(name: str, read: Callable[[], list[Work]]) -> list[Work]:
    """One source that can't be read now adds nothing, and the others still answer."""
    try:
        return read()
    except (OSError, sqlite3.Error, ValueError):
        logger.warning("Bighelp agents background read failed: %s", name)
        return []


def work(home: Path, since: float | None, *, with_session: bool = False) -> list[Work]:
    """Everything running now, and with `since`, what started or ended at or after it, in no set order."""
    items: list[Work] = []
    with ExitStack() as stack:
        try:
            db = _state_db(home)
        except sqlite3.Error:
            logger.warning("Bighelp agents background read failed: state")
            db = None
        if db is not None:
            stack.callback(db.close)
            items += _safe("subagents", lambda: delegations(db, since, with_session=with_session))
            if since is not None:
                items += _safe("finished commands", lambda: finished_commands(db, since))
        items += _safe("running commands", lambda: running_commands(home, db, with_session=with_session))
        items += _safe("scheduled tasks", lambda: cron_runs(home, db, since, with_session=with_session))
    return items


def running(home: Path) -> list[Work]:
    """The work an agent runs in the background now, newest start first."""
    items = work(home, None)
    return sorted(items, key=lambda item: item.started or 0, reverse=True)


def summary(home: Path) -> dict | None:
    """The agent row's `background`: how many pieces of work run now, and the newest one's title. None
    when nothing runs."""
    items = running(home)
    if not items:
        return None
    value: dict[str, Any] = {"count": len(items)}
    if items[0].title:
        # Shorter than Today's: 64 rows of it must fit the route's answer cap, also in a script of 3-byte letters.
        value["title"] = _cut(items[0].title, ACTIVITY_TITLE_LIMIT)
    return value


def forget() -> None:
    """Drops the job names cache (tests)."""
    with _jobs_lock:
        _jobs_cache.clear()
