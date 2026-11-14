"""One item of the app's Today page, opened: what a subagent did step by step, the output of a command, or the
output of a scheduled run. The item is the one `agents_today` lists for the same ID; this adds the detail.

Read-only, from the records Hermes keeps (see `agents_background`):

- A subagent's steps come from its own saved chats in the agent's `state.db`. The delegation row does not
  name them, so they are found by what Hermes writes for a child: a chat with source `subagent` under the
  chat that started the work, begun when the work was dispatched, whose first message is one of the unit's
  goals. Compressed segments of a child carry the child's `_subagent_id` mark and are followed.
- A command that runs now has its output in `logs/process-output/<id>.log` (only commands that Hermes starts
  from a worker have one). A command that ended has the notice that Hermes saved in the chat.
- A scheduled run has the final answer of its chat, or else the output file that Hermes saved for it.

Text goes through the same redaction as `agents_today` and shows file names only, never folders of the
computer. When text cannot be redacted, it is left out.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from . import agents_activity, agents_background as background
from .agents_background import Work


CACHE_SECONDS = 2.0
GOAL_LIMIT = 4_000
TEXT_LIMIT = 16_000
ERROR_LIMIT = 300
COMMAND_LIMIT = 2_000
MAX_STEPS = 300
STEP_TEXT_LIMIT = 300
TOOL_PREVIEW_LIMIT = 120
COMMAND_PREVIEW_LIMIT = 80
# Messages read from one saved chat: newest first, so a long run keeps its end. 300 steps are about 600 rows.
MAX_MESSAGES = 800
MAX_CALLS_BYTES = 64 * 1024
MAX_SEGMENTS = 20
MAX_CHILDREN = 64
OUTPUT_READ_BYTES = 64 * 1024
DOC_READ_BYTES = 256 * 1024
# The route's answer holds at most 512 KiB: the oldest steps go first when text of many bytes makes it larger.
RESPONSE_BUDGET = 480_000
# Hermes starts a child's chat before it dispatches the delegation: a child can be a little older than the row.
CHILD_HEAD_START_SECONDS = 120
CHILD_TAIL_SECONDS = 60
DOC_GRACE_SECONDS = 60
PROCESS_HEAD = re.compile(
    r"\[IMPORTANT: Background process \S+ .*?\(exit code (-?\d{1,5}|\?)(?:, SIGTERM)?\)\.\n", re.DOTALL)
COMMAND_LINE = re.compile(r"(?:^|\n)Command: (.*)", re.DOTALL)
OUTPUT_CUT_NOTE = re.compile(r"\.\.\.\((?:first \d+ characters cut|output trimmed)[^\n]*\n")
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")
TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
# The argument that tells most about a call (Hermes' own list, in its order of preference).
PRIMARY_ARGUMENTS = {
    "terminal": "command", "execute_code": "code", "web_search": "query", "web_extract": "urls",
    "read_file": "path", "write_file": "path", "patch": "path", "search_files": "pattern",
    "browser_navigate": "url", "skill_view": "name",
}
FALLBACK_ARGUMENTS = ("query", "url", "urls", "path", "file_path", "filename", "pattern", "command", "name",
                      "prompt", "text", "goal", "code")
FAILED_STATUS = {"error", "failed", "failure", "timeout"}
MESSAGE_COLUMNS = ("active", "_compressed_summary", "reasoning_content", "reasoning")
logger = logging.getLogger("hermes.plugins.bighelp")

_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_cache_lock = threading.Lock()


class AgentNotFound(LookupError):
    """The agent is not a profile on this host."""


class ActivityNotFound(LookupError):
    """The agent has no such item (or it cannot be read now: the answer never says which)."""


# --- Text ------------------------------------------------------------------------------------------------

def _clean(text: str) -> str:
    """Terminal output as plain text: no colour or cursor codes, a progress line keeps its last state."""
    text = ANSI.sub("", text).replace("\r\n", "\n")
    return CONTROL.sub("", "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n")))


def _block(value: Any, limit: int, *, tail: bool = False) -> tuple[str | None, bool]:
    """Text of many lines, redacted and with file names only, cut to `limit` characters (the newest ones with
    `tail`). Returns the text, or None when there is none or it cannot be redacted, and whether it was cut."""
    if not isinstance(value, str):
        return None, False
    window = limit * 4
    cut = len(value) > window
    redacted = background._redact(_clean(value[-window:] if tail else value[:window]))
    if not isinstance(redacted, str):
        return None, False
    text = background._hide_paths(redacted).strip()
    if len(text) > limit:
        cut = True
        text = text[-limit:] if tail else background._cut(text, limit)
    return (text or None), cut


def _flat(value: Any, limit: int) -> str | None:
    """Text on one line (all whitespace collapsed), redacted, with file names only, cut to `limit`."""
    if not isinstance(value, str):
        return None
    redacted = background._redact(" ".join(_clean(value[:limit * 4]).split()))
    if not isinstance(redacted, str):
        return None
    return background._cut(background._hide_paths(redacted), limit) or None


def _error(value: Any) -> str | None:
    return background._line(value, ERROR_LIMIT) or None


def _epoch(value: Any) -> float | None:
    return background._epoch(value)


def _compact(value: dict) -> dict:
    """The object without its empty fields."""
    return {key: item for key, item in value.items() if item is not None and item != ""}


# --- Tool calls ------------------------------------------------------------------------------------------

def _url_text(value: str) -> str:
    """A web address as host and path: no scheme, credentials, query or fragment."""
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return value
    return f"{host}{port}{parts.path}".rstrip("/") if host else value


def _tool_preview(tool: str, arguments: Any) -> str | None:
    """The one argument that tells most about a call, in plain form: a query, a web address as host and path,
    a file name, the start of a command."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if len(arguments) <= MAX_CALLS_BYTES else None
        except ValueError:
            arguments = None
    if not isinstance(arguments, dict):
        return None
    key = PRIMARY_ARGUMENTS.get(tool)
    if key not in arguments:
        key = next((name for name in FALLBACK_ARGUMENTS if name in arguments), None)
    value = arguments.get(key) if key else None
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    limit = COMMAND_PREVIEW_LIMIT if key in ("command", "code") else TOOL_PREVIEW_LIMIT
    redacted = background._redact(" ".join(_clean(value[:2_000]).split()))
    if not isinstance(redacted, str):
        return None
    text = redacted if URL.match(redacted) is None else _url_text(redacted)
    return background._cut(background._hide_paths(text), limit) or None


def _failed(tool: str | None, exit_code: Any, error: Any, success: Any, status: Any, head: Any) -> bool:
    """Whether a saved tool result is a failure, by Hermes' own signs: a terminal exit code, an `error`, a
    `success: false` or a failed status in a JSON result, or a first line that starts like an error."""
    if exit_code is not None or error is not None or success is not None or status is not None:
        if tool == "terminal" and exit_code not in (None, 0):
            return True
        if error not in (None, "", 0):
            return True
        return success == 0 or (isinstance(status, str) and status.strip().lower() in FAILED_STATUS)
    first = head.lstrip().splitlines()[:1] if isinstance(head, str) else []
    return bool(first) and first[0].strip().lower().startswith(("error:", "failed:", "traceback ", "exception:"))


# --- Subagents -------------------------------------------------------------------------------------------

def _norm(text: Any) -> str:
    return " ".join(text.split())[:200] if isinstance(text, str) else ""


def _unit_goals(task: dict, event: dict) -> list[str]:
    """The goals of this delegation: of a batch, the ones of its own tasks (a call can split into units)."""
    goals = task.get("goals") if isinstance(task.get("goals"), list) else event.get("goals")
    if isinstance(goals, list):
        indexes = task.get("task_indexes")
        picked = [goals[index] for index in indexes if isinstance(index, int) and 0 <= index < len(goals)] \
            if isinstance(indexes, list) else []
        return [goal for goal in (picked or goals) if isinstance(goal, str) and goal.strip()]
    single = task.get("goal") if isinstance(task.get("goal"), str) and task.get("goal").strip() else event.get("goal")
    return [single] if isinstance(single, str) and single.strip() else []


def _child_chats(db: sqlite3.Connection, parents: list[str], dispatched: float, completed: float | None,
                 goals: list[str], now: float) -> list[tuple[list[str], str | None]]:
    """The saved chats of the delegation's children, each as its segments (a compressed child continues in a
    new chat) and model. A candidate is a child chat under the chat that started the work, begun in the
    work's window, that opened with one of the unit's goals: two delegations that began together with the
    very same goal cannot be told apart, and each shows both."""
    wanted = {_norm(goal) for goal in goals}
    if not wanted or not parents:
        return []
    marks = ",".join("?" * len(parents))
    roots = background._rows(db, f"""
        SELECT id, model, model_config FROM sessions
        WHERE parent_session_id IN ({marks}) AND (source = 'subagent' OR model_config LIKE '%_delegate_from%')
          AND started_at BETWEEN ? AND ? ORDER BY started_at, id LIMIT ?""",
        (*parents, dispatched - CHILD_HEAD_START_SECONDS, (completed or now) + CHILD_TAIL_SECONDS, MAX_CHILDREN))
    children = []
    for root, model, config in roots:
        first = background._rows(db, "SELECT substr(content, 1, 600) FROM messages WHERE session_id = ? "
                                     "AND role = 'user' ORDER BY id LIMIT 1", (root,))
        if not first or _norm(first[0][0]) not in wanted:
            continue
        mark = background._json(config).get("_subagent_id")
        segments = [root]
        while mark and len(segments) < MAX_SEGMENTS:
            following = next((row[0] for row in background._rows(
                db, "SELECT id, model_config FROM sessions WHERE parent_session_id = ? ORDER BY started_at LIMIT 8",
                (segments[-1],)) if background._json(row[1]).get("_subagent_id") == mark), None)
            if following is None or following in segments:
                break
            segments.append(following)
        children.append((segments, model if isinstance(model, str) else None))
    return children


def _message_rows(db: sqlite3.Connection, session_id: str) -> tuple[list[tuple], bool]:
    """The newest saved messages of a chat, oldest first, and whether older ones were left out. A tool result
    carries only the signs of failure, read in the database, so a long output is never loaded."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
    try:
        db.execute("SELECT json_valid('{}')")
        failure = ("CASE WHEN role = 'tool' AND json_valid(content) THEN json_extract(content, '$.exit_code') END, "
                   "CASE WHEN role = 'tool' AND json_valid(content) THEN json_extract(content, '$.error') END, "
                   "CASE WHEN role = 'tool' AND json_valid(content) THEN json_extract(content, '$.success') END, "
                   "CASE WHEN role = 'tool' AND json_valid(content) THEN json_extract(content, '$.status') END, "
                   "CASE WHEN role = 'tool' AND NOT json_valid(content) THEN substr(content, 1, 200) END")
    except sqlite3.OperationalError:
        failure = "NULL, NULL, NULL, NULL, NULL"
    thinking = [f"NULLIF({name}, '')" for name in ("reasoning_content", "reasoning") if name in columns]
    where = "".join(f" AND {name} = {value}" for name, value in (("active", 1), ("_compressed_summary", 0))
                    if name in columns)
    rows = background._rows(db, f"""
        SELECT id, role, substr(content, 1, {STEP_TEXT_LIMIT * 4}),
               CASE WHEN length(tool_calls) > {MAX_CALLS_BYTES} THEN NULL ELSE tool_calls END, tool_call_id,
               timestamp, substr(COALESCE({", ".join(thinking) or "NULL"}), 1, {STEP_TEXT_LIMIT * 4}), {failure}
        FROM messages WHERE session_id = ?{where} ORDER BY id DESC LIMIT ?""", (session_id, MAX_MESSAGES + 1))
    return rows[:MAX_MESSAGES][::-1], len(rows) > MAX_MESSAGES


def _calls(value: Any) -> list[dict]:
    if not isinstance(value, str):
        return []
    try:
        calls = json.loads(value)
    except ValueError:
        return []
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def _steps(db: sqlite3.Connection, children: list[tuple[list[str], str | None]], running: bool) -> tuple[list, bool]:
    """The children's steps in time order: a tool call (done, failed, or running while the work runs), a
    message written between tools, and the thinking Hermes saved. The final answer is the summary, not a step."""
    rows, cut = [], False
    for segments, _model in children:
        for session_id in segments:
            part, more = _message_rows(db, session_id)
            rows += part
            cut = cut or more
    rows.sort(key=lambda row: (row[5] if isinstance(row[5], (int, float)) else 0, row[0]))
    steps: list[dict] = []
    calls: dict[str, dict] = {}
    for _id, role, content, tool_calls, call_id, stamp, reasoning, exit_code, error, success, status, head in rows:
        at = _epoch(stamp)
        if role == "assistant":
            if thought := _flat(reasoning, STEP_TEXT_LIMIT):
                steps.append(_compact({"at": at, "kind": "thinking", "preview": thought}))
            made = _calls(tool_calls)
            if made and (text := _flat(content, STEP_TEXT_LIMIT)):
                steps.append(_compact({"at": at, "kind": "message", "preview": text}))
            for call in made:
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = function.get("name")
                name = name if isinstance(name, str) and TOOL_NAME.fullmatch(name) else None
                step = _compact({"at": at, "kind": "tool", "tool": name,
                                 "preview": _tool_preview(name or "", function.get("arguments"))})
                steps.append(step)
                if isinstance(call.get("id"), str):
                    calls[call["id"]] = step
        elif role == "tool" and (step := calls.get(call_id)) is not None:
            step["state"] = "failed" if _failed(step.get("tool"), exit_code, error, success, status, head) else "done"
    if running:
        for step in steps:
            if step["kind"] == "tool" and "state" not in step:
                step["state"] = "running"
    return steps[-MAX_STEPS:], cut or len(steps) > MAX_STEPS


def _results(event: dict) -> list[dict]:
    results = [result for result in event.get("results") or [] if isinstance(result, dict)]
    return sorted(results, key=lambda result: result["task_index"] if isinstance(result.get("task_index"), int) else 0)


def _summary(event: dict) -> str | None:
    """The final answer: a subagent's summary, or a batch's summaries in order, each under its goal."""
    results = _results(event)
    if len(results) > 1:
        goals = event.get("goals") if isinstance(event.get("goals"), list) else []
        parts = []
        for result in results:
            index, body = result.get("task_index"), result.get("summary")
            if isinstance(body, str) and body.strip():
                goal = _flat(goals[index], 120) if isinstance(index, int) and 0 <= index < len(goals) else None
                parts.append(f"### {goal}\n\n{body}" if goal else body)
        return _block("\n\n".join(parts), TEXT_LIMIT)[0]
    return _block(results[0].get("summary") if results else event.get("summary"), TEXT_LIMIT)[0]


def _subagent(db: sqlite3.Connection, item: Work, now: float) -> dict:
    delegation_id = item.id.partition(":")[2]
    rows = background._rows(db, """SELECT parent_session_id, origin_session_id, dispatched_at, completed_at,
                                   task_json, event_json FROM async_delegations WHERE delegation_id = ?""",
                            (delegation_id,))
    if not rows:
        raise ActivityNotFound
    parent, origin, dispatched, completed, task_json, event_json = rows[0]
    task, event = background._json(task_json), background._json(event_json)
    goals, started = _unit_goals(task, event), _epoch(dispatched)
    parents = [value for value in dict.fromkeys((parent, origin)) if isinstance(value, str) and value]
    children = _child_chats(db, parents, started, _epoch(completed), goals, now) if started else []
    steps, cut = _steps(db, children, item.state == "running") if children else ([], False)
    failed = next((result for result in _results(event) if result.get("error")), None)
    model = next((model for _segments, model in children if model), None) or task.get("model")
    return _compact({
        "goal": _block("\n\n".join(goals), GOAL_LIMIT)[0],
        "model": _flat(model, 80),
        "summary": _summary(event),
        "error": _error((failed or event).get("error")),
        "steps": steps or None,
        "stepsTruncated": cut if steps else None,
    })


# --- Commands --------------------------------------------------------------------------------------------

def _output_log(home: Path, value: Any) -> Path | None:
    """The output file of a command: only a `.log` of this agent's own `logs/process-output` folder."""
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    try:
        inside = path.parent.resolve() == (home / "logs" / "process-output").resolve()
    except OSError:
        return None
    return path if inside and path.suffix == ".log" and not path.is_symlink() and path.is_file() else None


def _tail(path: Path, size: int) -> tuple[str, bool]:
    """The end of a file, at most `size` bytes, from the start of a line, and whether the start was left out."""
    with open(path, "rb") as stream:
        length = os.fstat(stream.fileno()).st_size
        start = max(0, length - size)
        stream.seek(start)
        text = stream.read(size).decode("utf-8", errors="replace")
    if start and "\n" in text:
        text = text[text.index("\n") + 1:]
    return text, start > 0


def _running_command(home: Path, db: sqlite3.Connection | None, item_id: str) -> tuple[Work, dict]:
    for item, entry in background.running_command_entries(home, db, with_session=True):
        if item.id == item_id:
            log = _output_log(home, entry.get("output_log"))
            text, cut = _tail(log, OUTPUT_READ_BYTES) if log else (None, False)
            output, more = _block(text, TEXT_LIMIT, tail=True)
            return item, _compact({
                "command": _block(entry.get("command"), COMMAND_LIMIT)[0],
                "output": output,
                "outputTruncated": (cut or more) if output else None,
            })
    raise ActivityNotFound


def _finished_command(db: sqlite3.Connection, item_id: str) -> tuple[Work, dict]:
    """A command that ended, from the notice Hermes saved in the chat: `[IMPORTANT: Background process <id>
    completed (exit code N).` then the command and its output."""
    if not item_id[len("message:"):].isdigit():
        raise ActivityNotFound
    rows = background._rows(db, """SELECT id, session_id, timestamp, display_metadata, content FROM messages
                                   WHERE id = ? AND display_kind = 'process_complete'""",
                            (int(item_id[len("message:"):]),))
    if not rows:
        raise ActivityNotFound
    message_id, session_id, stamp, metadata, content = rows[0]
    item = background._finished_command(message_id, session_id, _epoch(stamp),
                                        background._json(metadata).get("display_text"))
    if item is None:
        raise ActivityNotFound
    content = content if isinstance(content, str) else ""
    head = PROCESS_HEAD.match(content)
    if head is None:
        # Several commands ended together: one notice, with no one command or exit code.
        text, cut = _block(content, TEXT_LIMIT, tail=True)
        return item, _compact({"output": text, "outputTruncated": cut if text else None})
    command, _marker, output = content[head.end():].partition("\nOutput:\n")
    line = COMMAND_LINE.search(command)
    output = output[:-1] if output.endswith("]") else output
    text, cut = _block(OUTPUT_CUT_NOTE.sub("", output, count=1), TEXT_LIMIT, tail=True)
    return item, _compact({
        "command": _block(line.group(1) if line else None, COMMAND_LIMIT)[0],
        "exitCode": int(head.group(1)) if head.group(1) != "?" else None,
        "output": text,
        "outputTruncated": (cut or OUTPUT_CUT_NOTE.search(output) is not None) if text else None,
    })


# --- Scheduled tasks -------------------------------------------------------------------------------------

def _run_answer(db: sqlite3.Connection | None, session: str | None) -> Any:
    """The final answer of a run's chat: its last assistant message without tool calls."""
    if db is None or not session:
        return None
    rows = background._rows(db, f"""
        SELECT substr(content, 1, {TEXT_LIMIT * 4}) FROM messages
        WHERE session_id = ? AND role = 'assistant' AND content IS NOT NULL AND content != ''
          AND (tool_calls IS NULL OR tool_calls = '') ORDER BY id DESC LIMIT 1""", (session,))
    return rows[0][0] if rows else None


def _run_document(home: Path, job_id: str, runs: list[dict], run: dict) -> str | None:
    """The output file Hermes saved for a run. A file never names its run, so it is the run's only when the
    run is the one that finished nearest to it: a neighbour's file is never taken."""
    folder = home / "cron" / "output" / job_id
    if run["end"] is None or folder.is_symlink() or not folder.is_dir():
        return None
    best: tuple[float, Path] | None = None
    with os.scandir(folder) as entries:
        for index, entry in enumerate(entries):
            if index >= background.MAX_ROWS:
                break
            if not entry.name.endswith(".md") or entry.is_symlink() or not entry.is_file():
                continue
            gap = abs(entry.stat().st_mtime - run["end"])
            if gap <= DOC_GRACE_SECONDS and (best is None or gap < best[0]):
                best = (gap, Path(entry.path))
    if best is None:
        return None
    stamp = best[1].stat().st_mtime
    if any(other is not run and other["end"] is not None and abs(stamp - other["end"]) < best[0] for other in runs):
        return None
    with open(best[1], "rb") as stream:
        text = stream.read(DOC_READ_BYTES).decode("utf-8", errors="replace")
    # Hermes' file holds the job's header and prompt, then `## Response`: the run's output is what follows.
    return text.rpartition("## Response")[2] if "## Response" in text else text


def _scheduled(home: Path, db: sqlite3.Connection | None, item: Work) -> dict:
    _kind, job_id, start = item.id.split(":")
    runs = background.cron_job_runs(home, job_id)
    run = next((run for run in runs if str(run["start"]) == start), None)
    if run is None:
        raise ActivityNotFound
    output = None
    if item.state != "running":
        output = _run_answer(db, item.session)
        if not isinstance(output, str) or not output.strip():
            output = _run_document(home, job_id, runs, run)
    return _compact({
        "job": item.title or None,
        "status": item.state,
        "output": _block(output, TEXT_LIMIT)[0],
        "error": _error(run["error"]),
    })


# --- Together --------------------------------------------------------------------------------------------

def _find(items: list[Work], item_id: str) -> Work:
    item = next((item for item in items if item.id == item_id), None)
    if item is None:
        raise ActivityNotFound
    return item


def _read(home: Path, item_id: str, now: float) -> dict:
    kind = item_id.partition(":")[0]
    with ExitStack() as stack:
        try:
            db = background._state_db(home)
        except sqlite3.Error:
            db = None
        if db is not None:
            stack.callback(db.close)
        if kind == "process":
            item, detail = _running_command(home, db, item_id)
            return {"item": item.payload(), "command": detail}
        if kind == "cron":
            if len(item_id.split(":")) != 3 or not item_id.rsplit(":", 1)[1].isdigit():
                raise ActivityNotFound
            return {"item": (item := _find(background.cron_runs(home, db, 0.0, with_session=True), item_id)).payload(),
                    "scheduled": _scheduled(home, db, item)}
        if db is None:
            raise ActivityNotFound
        if kind == "message":
            item, detail = _finished_command(db, item_id)
            return {"item": item.payload(), "command": detail}
        item = _find(background.delegations(db, 0.0, with_session=True), item_id)
        return {"item": item.payload(), "subagent": _subagent(db, item, now)}


def _fit(value: dict) -> dict:
    """Drops the oldest steps until the answer fits the route's size."""
    steps = value.get("subagent", {}).get("steps")
    size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    while steps and size > RESPONSE_BUDGET:
        size -= len(json.dumps(steps.pop(0), ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        value["subagent"]["stepsTruncated"] = True
    return value


def detail(profile: str, item_id: str, *, now: Callable[[], float] | None = None) -> dict:
    """`{"item": ..., "subagent" | "command" | "scheduled": ...}` for an item that `agents_today` lists.
    Raises `AgentNotFound` when the agent isn't a profile here and `ActivityNotFound` when it has no such item."""
    clock = now or time.time
    moment = clock()
    key = (profile, item_id)
    with _cache_lock:
        cached = _cache.get(key)
    if cached and 0 <= moment - cached[0] < CACHE_SECONDS:
        return cached[1]
    home = agents_activity._home(profile)
    if home is None:
        raise AgentNotFound
    try:
        value = _fit(_read(home, item_id, moment))
    except (OSError, sqlite3.Error):
        # A record that can't be read now is not an item the phone can open: the answer is the same as for a gone one.
        logger.warning("Bighelp agents today detail read failed")
        raise ActivityNotFound from None
    with _cache_lock:
        if len(_cache) >= 256:
            _cache.clear()
        _cache[key] = (moment, value)
    return value


def forget() -> None:
    """Drops the short cache (tests)."""
    with _cache_lock:
        _cache.clear()
