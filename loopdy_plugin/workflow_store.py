"""The workflow ledger: one SQLite file and content-addressed artifacts for the whole Hermes computer.

The dashboard holds two copies of this module (see AGENTS.md), and the coordinator runs in its own process, so
nothing here lives in module globals: every call opens its own connection, and writes use BEGIN IMMEDIATE.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import workflow_model as model
from .sensitive import SENSITIVE_CREDENTIAL_RE


SCHEMA_VERSION = 3
DEFAULT_SLOTS = 2
MAX_PLANNED = 20
MAX_RUN_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_LIVE_LINES = 500
RETENTION_SECONDS = 30 * 24 * 3600
REQUEST_REPLAY_SECONDS = 24 * 3600
HEARTBEAT_FRESH_SECONDS = 10
LAUNCH_GRACE_SECONDS = 20
MAX_READ_LENGTH = 98_304
INLINE_TEXT_BYTES = 2048
MAX_ATTEMPTS_SHOWN = 10
MAX_TEMPLATES = 100
TEMPLATE_PREFIX = "tpl-"
DETAIL_BUDGET_BYTES = 160 * 1024
ACTIVE_STATES = ("launched", "running", "checking_output", "accepted")
LIVE_ATTEMPT_STATES = ("launched", "running", "checking_output")
TERMINAL_STATES = ("succeeded", "failed", "cancelled")
RUN_STATES = ("planned", "launched", "running", "checking_output", "accepted", "waiting_for_you",
              "needs_attention", "succeeded", "failed", "cancelled")
FILTERS = {
    "all": RUN_STATES,
    "active": ("planned",) + ACTIVE_STATES,
    "for_you": ("waiting_for_you",),
    "attention": ("needs_attention",),
}
_ID = re.compile(r"(?:wf|run|att)_[0-9a-f]{16}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_TEMPLATE_ID = re.compile(r"tpl-[0-9a-f]{16}\Z")
_TABLES = {"workflows", "revisions", "bindings", "runs", "attempts", "artifacts", "approvals", "events",
           "live_lines", "coordinator", "requests", "templates", "triggers", "sqlite_sequence"}


class WorkflowError(Exception):
    """An expected failure with a fixed code and a message that is safe to show."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class StoreUnavailable(WorkflowError):
    def __init__(self, message: str = "The workflow store can't be opened."):
        super().__init__(503, "store_unavailable", message)


@dataclass
class HostFacts:
    """What this computer has: used for the `host` half of validation."""
    profile_exists: Callable[[str], bool] | None = None
    toolset_known: Callable[[str], bool] | None = None
    # False when this Hermes can't limit a chat turn's tools (`--toolsets`): agent stages can't run then.
    tool_scope: bool = True


@dataclass
class Mutation:
    """A request's identity (for 24-hour replay) and a context check that runs before COMMIT."""
    request_id: str | None = None
    check: Callable[[], None] | None = None
    replayed: bool = field(default=False)


def iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def clean_text(value: str, maximum: int) -> str:
    """One safe line: secrets hidden, control characters dropped, length capped."""
    value = SENSITIVE_CREDENTIAL_RE.sub("[hidden]", value)
    home = os.path.expanduser("~")
    if len(home) > 1:
        value = value.replace(home, "~")
    value = "".join(character if character.isprintable() else " " for character in value)
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) <= maximum else value[: maximum - 1] + "…"


def _check_owned(info: os.stat_result, *, directory: bool) -> None:
    if (stat.S_ISDIR(info.st_mode) != directory or (not directory and not stat.S_ISREG(info.st_mode))
            or info.st_uid != os.geteuid()):
        raise StoreUnavailable()
    if not directory and info.st_nlink != 1:
        raise StoreUnavailable()


def secure_dir(path: Path) -> Path:
    """Create `path` owner-only (0700), refusing links and other owners."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = os.lstat(path)
    except OSError:
        raise StoreUnavailable() from None
    _check_owned(info, directory=True)
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def open_private(path: Path, flags: int, mode: int = 0o600) -> int:
    """os.open without following a final link, refusing hard links and other owners."""
    try:
        descriptor = os.open(path, flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode)
    except OSError:
        raise StoreUnavailable() from None
    try:
        _check_owned(os.fstat(descriptor), directory=False)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def root_for_home(hermes_root: Path) -> Path:
    return Path(hermes_root) / "plugin-data" / "loopdy" / "workflows"


# How a workflow starts: by hand (no row), or on a schedule through a Hermes cron job (workflow_trigger.py).
_TRIGGERS_TABLE = """CREATE TABLE IF NOT EXISTS triggers (
        workflow_id TEXT PRIMARY KEY, kind TEXT NOT NULL, schedule TEXT, inputs_json TEXT NOT NULL,
        job_id TEXT, updated_at REAL NOT NULL)"""
_TEMPLATES_TABLE = """CREATE TABLE IF NOT EXISTS templates (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, definition_json TEXT NOT NULL,
        stage_count INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)"""
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS workflows (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, draft_json TEXT NOT NULL, draft_version INTEGER NOT NULL,
        latest_revision INTEGER, archived INTEGER NOT NULL DEFAULT 0, run_counter INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, pinned INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS revisions (
        workflow_id TEXT NOT NULL, revision INTEGER NOT NULL, definition_json TEXT NOT NULL, sha256 TEXT NOT NULL,
        created_at REAL NOT NULL, PRIMARY KEY (workflow_id, revision))""",
    """CREATE TABLE IF NOT EXISTS bindings (
        workflow_id TEXT NOT NULL, role TEXT NOT NULL, agent_id TEXT NOT NULL, approved_at REAL NOT NULL,
        PRIMARY KEY (workflow_id, role))""",
    """CREATE TABLE IF NOT EXISTS runs (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE, number INTEGER NOT NULL,
        workflow_id TEXT NOT NULL, revision INTEGER NOT NULL, state TEXT NOT NULL, stage_key TEXT NOT NULL,
        iteration INTEGER NOT NULL, inputs_json TEXT NOT NULL, bindings_json TEXT NOT NULL,
        stages_json TEXT NOT NULL, loops_json TEXT NOT NULL DEFAULT '{}', client_run_token TEXT NOT NULL UNIQUE,
        version INTEGER NOT NULL, sample INTEGER NOT NULL DEFAULT 0, pause_requested INTEGER NOT NULL DEFAULT 0,
        cancel_requested INTEGER NOT NULL DEFAULT 0, next_stage TEXT, attention_code TEXT, attention_message TEXT,
        failure_stage TEXT, failure_code TEXT, failure_message TEXT, retry_stage TEXT, change_notes_json TEXT,
        waiting_sha256 TEXT, waiting_since REAL, tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0, artifact_bytes INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL, started_at REAL, updated_at REAL NOT NULL, ended_at REAL)""",
    "CREATE INDEX IF NOT EXISTS runs_state ON runs(state, seq)",
    "CREATE INDEX IF NOT EXISTS runs_workflow ON runs(workflow_id, seq)",
    """CREATE TABLE IF NOT EXISTS attempts (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage_key TEXT NOT NULL, iteration INTEGER NOT NULL,
        number INTEGER NOT NULL, state TEXT NOT NULL, agent_id TEXT NOT NULL, pid INTEGER, pid_fingerprint TEXT,
        coordinator_epoch INTEGER NOT NULL, launched_at REAL NOT NULL, deadline_at REAL NOT NULL, ended_at REAL,
        exit_code INTEGER, outcome_code TEXT, tokens_in INTEGER NOT NULL DEFAULT 0,
        tokens_out INTEGER NOT NULL DEFAULT 0, stream_offset INTEGER NOT NULL DEFAULT 0, stop_reason TEXT,
        kill_at REAL, dir TEXT NOT NULL, runner TEXT NOT NULL DEFAULT 'stream',
        tokens_known INTEGER NOT NULL DEFAULT 1)""",
    "CREATE INDEX IF NOT EXISTS attempts_run ON attempts(run_id, launched_at)",
    "CREATE INDEX IF NOT EXISTS attempts_state ON attempts(state)",
    """CREATE TABLE IF NOT EXISTS artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT NOT NULL, run_id TEXT NOT NULL, stage_key TEXT NOT NULL,
        iteration INTEGER NOT NULL, attempt_id TEXT, name TEXT NOT NULL, type TEXT NOT NULL, bytes INTEGER NOT NULL,
        word_count INTEGER, number_value REAL, value_json TEXT)""",
    "CREATE INDEX IF NOT EXISTS artifacts_run ON artifacts(run_id, stage_key, name)",
    "CREATE INDEX IF NOT EXISTS artifacts_sha ON artifacts(sha256)",
    """CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, stage_key TEXT NOT NULL,
        iteration INTEGER NOT NULL, artifact_sha256 TEXT NOT NULL, decision TEXT NOT NULL, notes TEXT NOT NULL,
        decided_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL,
        stage_key TEXT, attempt INTEGER, text TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq)",
    """CREATE TABLE IF NOT EXISTS live_lines (
        id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL, seq INTEGER NOT NULL, at REAL NOT NULL,
        kind TEXT NOT NULL, text TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS live_lines_attempt ON live_lines(attempt_id, seq)",
    """CREATE TABLE IF NOT EXISTS coordinator (
        id INTEGER PRIMARY KEY CHECK (id = 1), epoch INTEGER NOT NULL, pid INTEGER, fingerprint TEXT,
        boot_id TEXT, heartbeat_at REAL, slots_total INTEGER NOT NULL, launched_at REAL)""",
    """CREATE TABLE IF NOT EXISTS requests (
        request_id TEXT PRIMARY KEY, op TEXT NOT NULL, response_json TEXT NOT NULL, at REAL NOT NULL)""",
    _TEMPLATES_TABLE,
    _TRIGGERS_TABLE,
)
# Store version 1 (plugin 3.5.0) to 2: pins, your templates and the text runner's attempts.
MIGRATE_1_TO_2 = (
    "ALTER TABLE workflows ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE attempts ADD COLUMN runner TEXT NOT NULL DEFAULT 'stream'",
    "ALTER TABLE attempts ADD COLUMN tokens_known INTEGER NOT NULL DEFAULT 1",
    _TEMPLATES_TABLE,
)
# Store version 2 (plugin 3.6) to 3: triggers.
MIGRATE_2_TO_3 = (_TRIGGERS_TABLE,)


class WorkflowStore:
    def __init__(self, root: Path, *, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.clock = clock
        self.database = self.root / "workflows.sqlite3"
        self.artifacts = self.root / "artifacts"
        self.runs_dir = self.root / "runs"
        self.lock_path = self.root / "coordinator.lock"

    # MARK: Infrastructure

    def exists(self) -> bool:
        return self.database.is_file()

    def now(self) -> float:
        return float(self.clock())

    def _open(self) -> sqlite3.Connection:
        secure_dir(self.root)
        secure_dir(self.artifacts)
        secure_dir(self.runs_dir)
        os.close(open_private(self.database, os.O_RDWR | os.O_CREAT))
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(self.database) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                os.close(open_private(sidecar, os.O_RDONLY))
        try:
            connection = sqlite3.connect(str(self.database), timeout=5, isolation_level=None,
                                         check_same_thread=False)
        except sqlite3.Error:
            raise StoreUnavailable() from None
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA trusted_schema=OFF")
            self._use_wal(connection)
            connection.execute("PRAGMA synchronous=FULL")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, SCHEMA_VERSION):
                raise StoreUnavailable("This workflow store needs a newer bighelp plugin.")
            if version != SCHEMA_VERSION:
                self._migrate(connection)
        except sqlite3.Error:
            connection.close()
            raise StoreUnavailable() from None
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def _use_wal(connection: sqlite3.Connection) -> None:
        """Switch a new store to WAL. The switch ignores busy_timeout, so wait here while another connection
        (the other module copy, the coordinator) is creating the store; WAL is stored in the file once set."""
        for _ in range(50):
            if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
                return
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError:
                time.sleep(0.1)
        raise StoreUnavailable("The workflow store is busy. Try again.")

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Read again under the write lock: another process may have done this meanwhile.
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables - _TABLES:
                    raise StoreUnavailable()
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute("INSERT OR IGNORE INTO coordinator (id, epoch, slots_total) VALUES (1, 0, ?)",
                                   (DEFAULT_SLOTS,))
            elif version in (1, 2):
                for statement in (MIGRATE_1_TO_2 if version == 1 else ()) + MIGRATE_2_TO_3:
                    connection.execute(statement)
            if version != SCHEMA_VERSION:
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._open()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, mutation: Mutation | None = None) -> Iterator[sqlite3.Connection]:
        connection = self._open()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                raise StoreUnavailable("The workflow store is busy. Try again.") from None
            try:
                yield connection
                if mutation is not None and mutation.check is not None:
                    mutation.check()
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def _replay(self, connection: sqlite3.Connection, mutation: Mutation | None, op: str) -> dict | None:
        if mutation is None or mutation.request_id is None:
            return None
        row = connection.execute("SELECT op, response_json FROM requests WHERE request_id=?",
                                 (mutation.request_id,)).fetchone()
        if row is None:
            return None
        if row["op"] != op:
            raise WorkflowError(409, "request_reused", "That request ID was already used for another request.")
        mutation.replayed = True
        return json.loads(row["response_json"])

    def _remember(self, connection: sqlite3.Connection, mutation: Mutation | None, op: str, response: dict) -> dict:
        if mutation is not None and mutation.request_id is not None:
            now = self.now()
            connection.execute("DELETE FROM requests WHERE at < ?", (now - REQUEST_REPLAY_SECONDS,))
            connection.execute("INSERT INTO requests (request_id, op, response_json, at) VALUES (?, ?, ?, ?)",
                               (mutation.request_id, op, model.canonical_json(response), now))
        return response

    @contextmanager
    def mutate(self, op: str, mutation: Mutation | None) -> Iterator[tuple[sqlite3.Connection, dict | None]]:
        with self.transaction(mutation) as connection:
            yield connection, self._replay(connection, mutation, op)

    # MARK: Artifacts

    def write_artifact(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        target = self.artifacts / digest
        secure_dir(self.artifacts)
        try:
            info = os.lstat(target)
        except FileNotFoundError:
            info = None
        if info is not None:
            _check_owned(info, directory=False)
            return digest
        temporary = self.artifacts / f".{digest}.{secrets.token_hex(4)}.tmp"
        descriptor = open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        try:
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            os.fchmod(descriptor, 0o400)
        finally:
            os.close(descriptor)
        try:
            os.rename(temporary, target)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise StoreUnavailable() from None
        return digest

    def read_artifact_bytes(self, digest: str) -> bytes:
        if _SHA.fullmatch(digest) is None:
            raise WorkflowError(404, "artifact_not_found", "That file isn't part of this run.")
        try:
            descriptor = open_private(self.artifacts / digest, os.O_RDONLY)
        except StoreUnavailable:
            raise WorkflowError(404, "artifact_not_found", "That file isn't part of this run.") from None
        try:
            chunks = []
            while True:
                chunk = os.read(descriptor, 1 << 20)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    # MARK: Definitions

    @staticmethod
    def _workflow(connection: sqlite3.Connection, workflow_id: str) -> sqlite3.Row:
        row = None
        if isinstance(workflow_id, str) and _ID.fullmatch(workflow_id) and workflow_id.startswith("wf_"):
            row = connection.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        if row is None:
            raise WorkflowError(404, "workflow_not_found", "That workflow no longer exists.")
        return row

    @staticmethod
    def _definition(connection: sqlite3.Connection, workflow_id: str, revision: int, cache: dict | None = None) -> dict:
        key = (workflow_id, revision)
        if cache is not None and key in cache:
            return cache[key]
        row = connection.execute("SELECT definition_json FROM revisions WHERE workflow_id=? AND revision=?",
                                 key).fetchone()
        if row is None:
            raise WorkflowError(404, "revision_not_found", "That version of the workflow doesn't exist.")
        value = json.loads(row["definition_json"])
        if cache is not None:
            cache[key] = value
        return value

    @staticmethod
    def _bindings(connection: sqlite3.Connection, workflow_id: str) -> dict[str, sqlite3.Row]:
        return {row["role"]: row for row in connection.execute(
            "SELECT role, agent_id, approved_at FROM bindings WHERE workflow_id=?", (workflow_id,))}

    def _binding_list(self, connection: sqlite3.Connection, workflow_id: str, definition: dict) -> list[dict]:
        rows = self._bindings(connection, workflow_id)
        return [{"role": role["key"],
                 "agentId": rows[role["key"]]["agent_id"] if role["key"] in rows else None,
                 "approvedAt": iso(rows[role["key"]]["approved_at"]) if role["key"] in rows else None}
                for role in definition["roles"]]

    def _validation(self, connection: sqlite3.Connection, workflow_id: str, definition: dict,
                    host: HostFacts | None) -> dict:
        bindings = {role: row["agent_id"] for role, row in self._bindings(connection, workflow_id).items()}
        host = host or HostFacts()
        return model.validate(definition, bindings=bindings, profile_exists=host.profile_exists,
                              toolset_known=host.toolset_known, tool_scope=host.tool_scope)

    @staticmethod
    def _trigger(connection: sqlite3.Connection, workflow_id: str) -> dict:
        row = connection.execute("SELECT * FROM triggers WHERE workflow_id=?", (workflow_id,)).fetchone()
        if row is None or row["kind"] != "schedule":
            return {"kind": "manual"}
        return {"kind": "schedule", "schedule": row["schedule"], "inputs": json.loads(row["inputs_json"]),
                "jobId": row["job_id"]}

    def trigger(self, workflow_id: str) -> dict:
        with self.read() as connection:
            self._workflow(connection, workflow_id)
            return self._trigger(connection, workflow_id)

    def scheduled_start(self, workflow_id: str, inputs: Any, host: HostFacts | None) -> tuple[str, int, dict]:
        """(name, latest revision, parsed inputs) for a run on a schedule; refuses what couldn't run."""
        with self.read() as connection:
            row = self._workflow(connection, workflow_id)
            if row["archived"]:
                raise WorkflowError(409, "workflow_archived", "This workflow is archived.")
            if not row["latest_revision"]:
                raise WorkflowError(409, "not_published", "Run this workflow once before you schedule it.")
            definition = self._definition(connection, workflow_id, row["latest_revision"])
            validation = self._validation(connection, workflow_id, definition, host)
            if not (validation["valid"] and validation["host"]):
                raise WorkflowError(409, "not_valid", "Fix the problems in this workflow first.")
            try:
                parsed = model.parse_inputs(definition, inputs)
            except model.InputsError as error:
                raise WorkflowError(422, "inputs_invalid", str(error)) from None
            return row["name"], row["latest_revision"], parsed

    def save_trigger(self, workflow_id: str, kind: str, schedule: str | None, inputs: dict,
                     job_id: str | None) -> dict:
        with self.transaction() as connection:
            self._workflow(connection, workflow_id)
            if kind == "manual":
                connection.execute("DELETE FROM triggers WHERE workflow_id=?", (workflow_id,))
            else:
                connection.execute(
                    "INSERT OR REPLACE INTO triggers (workflow_id, kind, schedule, inputs_json, job_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (workflow_id, kind, schedule, model.canonical_json(inputs), job_id, self.now()))
            return self._trigger(connection, workflow_id)

    def list_workflows(self, include_archived: bool, host: HostFacts | None) -> dict:
        with self.read() as connection:
            rows = connection.execute(
                "SELECT * FROM workflows WHERE archived=0 OR ? ORDER BY pinned DESC, updated_at DESC, id",
                (1 if include_archived else 0,)).fetchall()
            workflows = []
            for row in rows[:200]:
                draft = json.loads(row["draft_json"])
                current = (self._definition(connection, row["id"], row["latest_revision"])
                           if row["latest_revision"] else draft)
                validation = self._validation(connection, row["id"], draft, host)
                bindings = {role: binding["agent_id"] for role, binding in
                            self._bindings(connection, row["id"]).items()}
                used = {stage["role"] for stage in current["stages"] if stage["kind"] == "agent"}
                missing = [role["key"] for role in current["roles"] if role["key"] in used and (
                    not bindings.get(role["key"]) or (host is not None and host.profile_exists is not None
                                                      and not host.profile_exists(bindings[role["key"]])))]
                last = connection.execute("SELECT MAX(created_at) FROM runs WHERE workflow_id=?",
                                          (row["id"],)).fetchone()[0]
                has_draft = row["latest_revision"] is None or model.definition_sha256(draft) != connection.execute(
                    "SELECT sha256 FROM revisions WHERE workflow_id=? AND revision=?",
                    (row["id"], row["latest_revision"])).fetchone()[0]
                workflows.append({
                    "id": row["id"], "name": row["name"], "revision": row["latest_revision"],
                    "draftVersion": row["draft_version"], "hasDraft": has_draft,
                    "stageCount": len(current["stages"]), "needsSetupRoles": missing,
                    "valid": validation["valid"], "archived": bool(row["archived"]), "lastRunAt": iso(last),
                    "pinned": bool(row["pinned"]), "trigger": self._trigger(connection, row["id"]),
                })
            cache: dict = {}
            waiting = [self._summary(connection, run, cache) for run in connection.execute(
                "SELECT * FROM runs WHERE state='waiting_for_you' ORDER BY waiting_since, seq LIMIT 50")]
            active = [self._summary(connection, run, cache) for run in connection.execute(
                "SELECT * FROM runs WHERE state IN ('planned','launched','running','checking_output','accepted',"
                "'needs_attention') ORDER BY seq DESC LIMIT 50")]
        return {"workflows": workflows, "waiting": waiting, "active": active}

    def get_workflow(self, workflow_id: str, revision: int | str, host: HostFacts | None) -> dict:
        with self.read() as connection:
            row = self._workflow(connection, workflow_id)
            if revision == "draft":
                definition, number = json.loads(row["draft_json"]), None
            else:
                definition, number = self._definition(connection, workflow_id, revision), revision
            return {"workflow": {
                "id": row["id"], "name": row["name"], "revision": number, "latestRevision": row["latest_revision"],
                "draftVersion": row["draft_version"], "archived": bool(row["archived"]),
                "pinned": bool(row["pinned"]), "definition": definition,
                "bindings": self._binding_list(connection, workflow_id, definition),
                "trigger": self._trigger(connection, workflow_id),
            }, "validation": self._validation(connection, workflow_id, definition, host)}

    @staticmethod
    def _parse(definition: Any) -> dict:
        try:
            return model.parse_definition(definition)
        except model.DefinitionError as error:
            if str(error) == "The workflow is too large.":
                raise WorkflowError(413, "definition_too_large", str(error)) from None
            raise WorkflowError(422, "invalid_definition", str(error)) from None

    def _save(self, connection: sqlite3.Connection, workflow_id: str | None, base_version: int, parsed: dict,
              host: HostFacts | None) -> dict:
        now = self.now()
        if workflow_id is None:
            if base_version != 0:
                raise WorkflowError(409, "draft_conflict", "The draft changed. Load it again.")
            if connection.execute("SELECT COUNT(*) FROM workflows").fetchone()[0] >= 200:
                raise WorkflowError(409, "not_allowed", "This computer already has 200 workflows.")
            workflow_id = new_id("wf")
            connection.execute(
                "INSERT INTO workflows (id, name, draft_json, draft_version, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?)", (workflow_id, parsed["name"], model.canonical_json(parsed), now, now))
            version = 1
        else:
            row = self._workflow(connection, workflow_id)
            if row["archived"]:
                raise WorkflowError(409, "workflow_archived", "This workflow is archived.")
            if row["draft_version"] != base_version:
                raise WorkflowError(409, "draft_conflict", "The draft changed. Load it again.")
            version = base_version + 1
            connection.execute("UPDATE workflows SET name=?, draft_json=?, draft_version=?, updated_at=? WHERE id=?",
                               (parsed["name"], model.canonical_json(parsed), version, now, workflow_id))
        return {"workflowId": workflow_id, "draftVersion": version,
                "validation": self._validation(connection, workflow_id, parsed, host)}

    def save_draft(self, workflow_id: str | None, base_version: int, definition: Any, host: HostFacts | None,
                   mutation: Mutation | None = None) -> dict:
        parsed = self._parse(definition)
        with self.mutate("draft/save", mutation) as (connection, replay):
            if replay is not None:
                return replay
            response = self._save(connection, workflow_id, base_version, parsed, host)
            return self._remember(connection, mutation, "draft/save", response)

    def validate(self, workflow_id: str, host: HostFacts | None) -> dict:
        with self.read() as connection:
            row = self._workflow(connection, workflow_id)
            return {"validation": self._validation(connection, workflow_id, json.loads(row["draft_json"]), host)}

    def publish(self, workflow_id: str, draft_version: int, host: HostFacts | None,
                mutation: Mutation | None = None) -> dict:
        with self.mutate("publish", mutation) as (connection, replay):
            if replay is not None:
                return replay
            row = self._workflow(connection, workflow_id)
            if row["archived"]:
                raise WorkflowError(409, "workflow_archived", "This workflow is archived.")
            if row["draft_version"] != draft_version:
                raise WorkflowError(409, "draft_conflict", "The draft changed. Load it again.")
            definition = json.loads(row["draft_json"])
            if not self._validation(connection, workflow_id, definition, host)["valid"]:
                raise WorkflowError(409, "not_valid", "Fix the problems in this workflow first.")
            digest = model.definition_sha256(definition)
            latest = row["latest_revision"]
            if latest is not None and connection.execute(
                    "SELECT sha256 FROM revisions WHERE workflow_id=? AND revision=?",
                    (workflow_id, latest)).fetchone()[0] == digest:
                revision = latest
            else:
                revision = (latest or 0) + 1
                now = self.now()
                connection.execute("INSERT INTO revisions (workflow_id, revision, definition_json, sha256, created_at) "
                                   "VALUES (?, ?, ?, ?, ?)",
                                   (workflow_id, revision, model.canonical_json(definition), digest, now))
                connection.execute("UPDATE workflows SET latest_revision=?, updated_at=? WHERE id=?",
                                   (revision, now, workflow_id))
            return self._remember(connection, mutation, "publish", {"workflowId": workflow_id, "revision": revision})

    def bind(self, workflow_id: str, role: str, agent_id: str | None, mutation: Mutation | None = None) -> dict:
        with self.mutate("bind", mutation) as (connection, replay):
            if replay is not None:
                return replay
            row = self._workflow(connection, workflow_id)
            if row["archived"]:
                raise WorkflowError(409, "workflow_archived", "This workflow is archived.")
            definition = json.loads(row["draft_json"])
            roles = {item["key"] for item in definition["roles"]}
            if row["latest_revision"]:
                roles |= {item["key"] for item in self._definition(
                    connection, workflow_id, row["latest_revision"])["roles"]}
            if role not in roles:
                raise WorkflowError(404, "role_not_found", "That role isn't in this workflow.")
            if agent_id is None:
                connection.execute("DELETE FROM bindings WHERE workflow_id=? AND role=?", (workflow_id, role))
            else:
                connection.execute("INSERT INTO bindings (workflow_id, role, agent_id, approved_at) VALUES (?, ?, ?, ?) "
                                   "ON CONFLICT(workflow_id, role) DO UPDATE SET agent_id=excluded.agent_id, "
                                   "approved_at=excluded.approved_at", (workflow_id, role, agent_id, self.now()))
            connection.execute("UPDATE workflows SET updated_at=? WHERE id=?", (self.now(), workflow_id))
            response = {"workflowId": workflow_id,
                        "bindings": self._binding_list(connection, workflow_id, definition)}
            return self._remember(connection, mutation, "bind", response)

    def archive(self, workflow_id: str, mutation: Mutation | None = None) -> dict:
        with self.mutate("archive", mutation) as (connection, replay):
            if replay is not None:
                return replay
            self._workflow(connection, workflow_id)
            connection.execute("UPDATE workflows SET archived=1, updated_at=? WHERE id=?", (self.now(), workflow_id))
            return self._remember(connection, mutation, "archive", {"workflowId": workflow_id, "archived": True})

    def unarchive(self, workflow_id: str, mutation: Mutation | None = None) -> dict:
        with self.mutate("unarchive", mutation) as (connection, replay):
            if replay is not None:
                return replay
            self._workflow(connection, workflow_id)
            connection.execute("UPDATE workflows SET archived=0, updated_at=? WHERE id=?", (self.now(), workflow_id))
            return self._remember(connection, mutation, "unarchive", {"workflowId": workflow_id, "archived": False})

    def pin(self, workflow_id: str, pinned: bool, mutation: Mutation | None = None) -> dict:
        with self.mutate("pin", mutation) as (connection, replay):
            if replay is not None:
                return replay
            self._workflow(connection, workflow_id)
            # Pinning only reorders the list, so it leaves updated_at alone.
            connection.execute("UPDATE workflows SET pinned=? WHERE id=?", (1 if pinned else 0, workflow_id))
            return self._remember(connection, mutation, "pin", {"workflowId": workflow_id, "pinned": pinned})

    # MARK: Templates

    @staticmethod
    def _your_template(connection: sqlite3.Connection, template_id: str) -> sqlite3.Row:
        row = None
        if isinstance(template_id, str) and _TEMPLATE_ID.fullmatch(template_id):
            row = connection.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
        if row is None:
            raise WorkflowError(404, "template_not_found", "That template doesn't exist.")
        return row

    def list_templates(self) -> dict:
        with self.read() as connection:
            rows = connection.execute("SELECT * FROM templates ORDER BY updated_at DESC, id LIMIT ?",
                                      (MAX_TEMPLATES,)).fetchall()
        yours = []
        for row in rows:
            definition = json.loads(row["definition_json"])
            yours.append({"id": row["id"], "name": row["name"], "description": row["description"],
                          "source": "yours", "stageCount": row["stage_count"],
                          "roles": definition.get("roles", []), "updatedAt": iso(row["updated_at"])})
        builtin = [dict(item, source="builtin", updatedAt=None) for item in model.template_summaries()]
        return {"templates": yours + builtin}

    def save_template(self, workflow_id: str, name: str, description: str | None,
                      mutation: Mutation | None = None) -> dict:
        """Your template: a copy of the workflow's latest saved draft, without who does each role."""
        name = name.strip()
        if not name or len(name) > model.MAX_TITLE or "\x00" in name:
            raise WorkflowError(422, "invalid_request", "Give the template a name of at most 80 characters.")
        if description is not None and (len(description) > model.MAX_DESCRIPTION or "\x00" in description):
            raise WorkflowError(422, "invalid_request", "The description is too long.")
        with self.mutate("templates/save", mutation) as (connection, replay):
            if replay is not None:
                return replay
            row = self._workflow(connection, workflow_id)
            if connection.execute("SELECT COUNT(*) FROM templates").fetchone()[0] >= MAX_TEMPLATES:
                raise WorkflowError(409, "not_allowed", f"You already have {MAX_TEMPLATES} templates. Delete one first.")
            definition = json.loads(row["draft_json"])
            definition["name"] = name
            if description is not None:
                definition["description"] = description
            now = self.now()
            template_id = TEMPLATE_PREFIX + secrets.token_hex(8)
            connection.execute(
                "INSERT INTO templates (id, name, description, definition_json, stage_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (template_id, name, definition.get("description", ""), model.canonical_json(definition),
                 len(definition["stages"]), now, now))
            return self._remember(connection, mutation, "templates/save", {"templateId": template_id})

    def delete_template(self, template_id: str, mutation: Mutation | None = None) -> dict:
        if model.template(template_id) is not None:
            raise WorkflowError(409, "not_allowed", "Built-in templates can't be deleted.")
        with self.mutate("templates/delete", mutation) as (connection, replay):
            if replay is not None:
                return replay
            self._your_template(connection, template_id)
            connection.execute("DELETE FROM templates WHERE id=?", (template_id,))
            return self._remember(connection, mutation, "templates/delete", {"deleted": True})

    def use_template(self, template_id: str, name: str | None, host: HostFacts | None,
                     mutation: Mutation | None = None) -> dict:
        definition = model.template(template_id)
        if definition is None and not template_id.startswith(TEMPLATE_PREFIX):
            raise WorkflowError(404, "template_not_found", "That template doesn't exist.")
        with self.mutate("templates/use", mutation) as (connection, replay):
            if replay is not None:
                return replay
            if definition is None:
                definition = json.loads(self._your_template(connection, template_id)["definition_json"])
            if name:
                definition["name"] = name
            response = self._save(connection, None, 0, self._parse(definition), host)
            return self._remember(connection, mutation, "templates/use", response)

    # MARK: Runs

    @staticmethod
    def _run(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = None
        if isinstance(run_id, str) and _ID.fullmatch(run_id) and run_id.startswith("run_"):
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise WorkflowError(404, "run_not_found", "That run no longer exists.")
        return row

    def start_run(self, workflow_id: str, revision: int, inputs: Any, client_run_token: str, sample: bool,
                  host: HostFacts | None, mutation: Mutation | None = None) -> dict:
        if sample:
            raise WorkflowError(422, "sample_unsupported", "Runs with sample inputs aren't available yet.")
        with self.mutate("runs/start", mutation) as (connection, replay):
            if replay is not None:
                return replay
            existing = connection.execute("SELECT * FROM runs WHERE client_run_token=?",
                                          (client_run_token,)).fetchone()
            if existing is not None:
                return {"run": self._summary(connection, existing, {})}
            row = self._workflow(connection, workflow_id)
            if row["archived"]:
                raise WorkflowError(409, "workflow_archived", "This workflow is archived.")
            definition = self._definition(connection, workflow_id, revision)
            validation = self._validation(connection, workflow_id, definition, host)
            if not (validation["valid"] and validation["host"]):
                raise WorkflowError(409, "not_valid", "Fix the problems in this workflow first.")
            try:
                parsed = model.parse_inputs(definition, inputs)
            except model.InputsError as error:
                raise WorkflowError(422, "inputs_invalid", str(error)) from None
            planned = connection.execute("SELECT COUNT(*) FROM runs WHERE state='planned'").fetchone()[0]
            if planned >= MAX_PLANNED:
                raise WorkflowError(429, "queue_full", "Too many runs are waiting. Try again later.")
            now = self.now()
            number = row["run_counter"] + 1
            connection.execute("UPDATE workflows SET run_counter=? WHERE id=?", (number, workflow_id))
            run_id = new_id("run")
            bindings = {role: binding["agent_id"] for role, binding in self._bindings(connection, workflow_id).items()}
            first = definition["stages"][0]["key"]
            connection.execute(
                "INSERT INTO runs (id, number, workflow_id, revision, state, stage_key, iteration, inputs_json, "
                "bindings_json, stages_json, client_run_token, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'planned', ?, 1, ?, ?, ?, ?, 1, ?, ?)",
                (run_id, number, workflow_id, revision, first, model.canonical_json(parsed),
                 model.canonical_json(bindings), model.canonical_json(initial_stages(definition)),
                 client_run_token, now, now))
            add_event(connection, run_id, now, "run_planned", f"Run {number} is waiting for a free slot.")
            response = {"run": self._summary(connection, self._run(connection, run_id), {})}
            return self._remember(connection, mutation, "runs/start", response)

    def list_runs(self, workflow_id: str | None, filter_name: str, before: str | None, limit: int) -> dict:
        states = FILTERS[filter_name]
        query = f"SELECT * FROM runs WHERE state IN ({','.join('?' * len(states))})"
        values: list[Any] = list(states)
        if workflow_id is not None:
            query += " AND workflow_id=?"
            values.append(workflow_id)
        if before is not None:
            if not before.isdigit() or len(before) > 18:
                raise WorkflowError(422, "invalid_request", "The cursor is invalid.")
            query += " AND seq < ?"
            values.append(int(before))
        query += " ORDER BY seq DESC LIMIT ?"
        values.append(limit + 1)
        with self.read() as connection:
            if workflow_id is not None:
                self._workflow(connection, workflow_id)
            rows = connection.execute(query, values).fetchall()
            cache: dict = {}
            runs = [self._summary(connection, row, cache) for row in rows[:limit]]
        return {"runs": runs, "hasMore": len(rows) > limit,
                "cursor": str(rows[min(len(rows), limit) - 1]["seq"]) if rows else None}

    def get_run(self, run_id: str) -> dict:
        with self.read() as connection:
            return {"run": self._detail(connection, self._run(connection, run_id))}

    def events(self, run_id: str, after: int, limit: int) -> dict:
        with self.read() as connection:
            self._run(connection, run_id)
            rows = connection.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                                      (run_id, after, limit + 1)).fetchall()
        events = [{"seq": row["seq"], "at": iso(row["at"]), "kind": row["kind"], "stageKey": row["stage_key"],
                   "attempt": row["attempt"], "text": row["text"]} for row in rows[:limit]]
        return {"events": events, "cursor": events[-1]["seq"] if events else after, "hasMore": len(rows) > limit}

    def control(self, run_id: str, action: str, expected_version: int, mutation: Mutation | None = None) -> dict:
        with self.mutate("runs/control", mutation) as (connection, replay):
            if replay is not None:
                return replay
            run = self._run(connection, run_id)
            if run["version"] != expected_version:
                raise WorkflowError(409, "run_conflict", "The run changed. Load it again.")
            if action not in allowed_actions(run):
                raise WorkflowError(409, "not_allowed", "That isn't possible for this run now.")
            now = self.now()
            cache: dict = {}
            definition = self._definition(connection, run["workflow_id"], run["revision"], cache)
            if action == "pause":
                update_run(connection, run, now, pause_requested=1)
                add_event(connection, run_id, now, "paused", "The run will hold after the current stage.")
            elif action == "resume":
                update_run(connection, run, now, pause_requested=0)
                add_event(connection, run_id, now, "resumed", "The run goes on.")
            elif action == "cancel":
                live = connection.execute(
                    f"SELECT COUNT(*) FROM attempts WHERE run_id=? AND state IN ({_LIVE})", (run_id,)).fetchone()[0]
                if live:
                    update_run(connection, run, now, cancel_requested=1)
                    add_event(connection, run_id, now, "cancel_requested", "Stopping the run.", run["stage_key"])
                else:
                    stages = json.loads(run["stages_json"])
                    stage_state(stages, run["stage_key"], "cancelled", now, end=True)
                    update_run(connection, run, now, state="cancelled", cancel_requested=1, ended_at=now,
                               stages_json=model.canonical_json(stages), waiting_sha256=None, waiting_since=None)
                    add_event(connection, run_id, now, "cancelled", "The run was cancelled.", run["stage_key"])
            else:
                retry(connection, self, run, definition, now)
            response = {"run": self._summary(connection, self._run(connection, run_id), cache)}
            return self._remember(connection, mutation, "runs/control", response)

    def signoff(self, run_id: str, stage_key: str, decision: str, artifact_sha256: str, notes: str,
                mutation: Mutation | None = None) -> dict:
        with self.mutate("runs/signoff", mutation) as (connection, replay):
            if replay is not None:
                return replay
            run = self._run(connection, run_id)
            if run["state"] != "waiting_for_you" or run["stage_key"] != stage_key or not run["waiting_sha256"]:
                raise WorkflowError(409, "not_waiting", "This stage isn't waiting for you.")
            given = run["waiting_sha256"]
            try:
                stored = hashlib.sha256(self.read_artifact_bytes(given)).hexdigest()
            except WorkflowError:
                stored = None
            if artifact_sha256 != given or stored != given:
                raise WorkflowError(409, "approval_stale", "The file changed since you opened it. Look at it again.")
            now = self.now()
            cache: dict = {}
            definition = self._definition(connection, run["workflow_id"], run["revision"], cache)
            connection.execute("INSERT INTO approvals (run_id, stage_key, iteration, artifact_sha256, decision, notes, "
                               "decided_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                               (run_id, stage_key, run["iteration"], given, decision, notes, now))
            stage = model.stage_by_key(definition, stage_key)
            stages = json.loads(run["stages_json"])
            if decision == "approve":
                stage_state(stages, stage_key, "accepted", now, end=True)
                add_event(connection, run_id, now, "approved", "You approved the file.", stage_key)
                following = model.next_stage_key(definition, stage_key)
                if following is None:
                    finish(connection, run, now, stages)
                else:
                    update_run(connection, run, now, state="planned", stage_key=following,
                               stages_json=model.canonical_json(stages), waiting_sha256=None, waiting_since=None)
            else:
                producer, _ = model.split_reference(stage["file"])
                add_event(connection, run_id, now, "changes_requested", "You asked for changes.",
                          stage_key)
                notes_value = [{"severity": "major", "text": notes.strip()}] if notes.strip() else []
                go_back(connection, run, definition, producer, {"from": "you", "notes": notes_value}, now,
                        state="planned")
            response = {"run": self._summary(connection, self._run(connection, run_id), cache)}
            return self._remember(connection, mutation, "runs/signoff", response)

    def read_artifact(self, run_id: str, digest: str, offset: int, length: int) -> dict:
        with self.read() as connection:
            self._run(connection, run_id)
            if connection.execute("SELECT 1 FROM artifacts WHERE run_id=? AND sha256=? LIMIT 1",
                                  (run_id, digest)).fetchone() is None:
                raise WorkflowError(404, "artifact_not_found", "That file isn't part of this run.")
        data = self.read_artifact_bytes(digest)
        if hashlib.sha256(data).hexdigest() != digest:
            raise WorkflowError(404, "artifact_not_found", "That file isn't part of this run.")
        chunk = data[offset: offset + min(length, MAX_READ_LENGTH)]
        return {"sha256": digest, "offset": offset, "total": len(data),
                "data": base64.b64encode(chunk).decode("ascii"), "done": offset + len(chunk) >= len(data)}

    # MARK: Coordinator status

    def status(self) -> dict:
        with self.read() as connection:
            row = connection.execute("SELECT * FROM coordinator WHERE id=1").fetchone()
            used = connection.execute(
                f"SELECT COUNT(*) FROM runs WHERE state IN ({_ACTIVE})").fetchone()[0]
        now = self.now()
        if row["heartbeat_at"] is not None and now - row["heartbeat_at"] <= HEARTBEAT_FRESH_SECONDS:
            state = "online"
        elif row["launched_at"] is not None and now - row["launched_at"] <= LAUNCH_GRACE_SECONDS:
            state = "starting"
        else:
            state = "offline"
        return {"coordinator": {"state": state, "heartbeatAt": iso(row["heartbeat_at"]), "epoch": row["epoch"]},
                "slots": {"used": used, "total": row["slots_total"]}}

    def has_work(self) -> bool:
        with self.read() as connection:
            return connection.execute(
                f"SELECT 1 FROM runs WHERE (state='planned' AND pause_requested=0) OR state IN ({_ACTIVE}) "
                "LIMIT 1").fetchone() is not None

    # MARK: Projections

    def _summary(self, connection: sqlite3.Connection, run: sqlite3.Row, cache: dict) -> dict:
        definition = self._definition(connection, run["workflow_id"], run["revision"], cache)
        name_key = ("name", run["workflow_id"])
        if name_key not in cache:
            row = connection.execute("SELECT name FROM workflows WHERE id=?", (run["workflow_id"],)).fetchone()
            cache[name_key] = row["name"] if row else definition["name"]
        stages = json.loads(run["stages_json"])
        stage = model.stage_by_key(definition, run["stage_key"]) or {"title": run["stage_key"]}
        value = {
            "id": run["id"], "number": run["number"], "workflowId": run["workflow_id"],
            "workflowName": cache[name_key], "revision": run["revision"], "state": run["state"],
            "stageKey": run["stage_key"], "stageTitle": stage["title"],
            "stageState": stages.get(run["stage_key"], {}).get("state", "pending"),
            "stagesDone": sum(1 for item in stages.values() if item["state"] == "accepted"),
            "stageCount": len(definition["stages"]), "iteration": run["iteration"],
            "startedAt": iso(run["started_at"]), "updatedAt": iso(run["updated_at"]), "endedAt": iso(run["ended_at"]),
            "paused": bool(run["pause_requested"]), "version": run["version"], "sample": bool(run["sample"]),
        }
        if run["state"] == "needs_attention" and run["attention_code"]:
            value["attention"] = {"code": run["attention_code"], "message": run["attention_message"] or ""}
        if run["state"] == "failed" and run["failure_code"]:
            value["failure"] = {"stageKey": run["failure_stage"] or run["stage_key"], "code": run["failure_code"],
                                "message": run["failure_message"] or ""}
        if run["state"] == "waiting_for_you":
            value["waiting"] = {"kind": "signoff", "stageKey": run["stage_key"], "since": iso(run["waiting_since"])}
        return value

    @staticmethod
    def _output(row: sqlite3.Row) -> dict:
        value = {"stageKey": row["stage_key"], "iteration": row["iteration"], "name": row["name"],
                 "type": row["type"], "sha256": row["sha256"], "bytes": row["bytes"]}
        if row["type"] in ("markdown_file", "text") and row["word_count"] is not None:
            value["wordCount"] = row["word_count"]
        if row["value_json"] is not None and (row["type"] != "text" or row["bytes"] <= INLINE_TEXT_BYTES):
            value["value"] = json.loads(row["value_json"])
        return value

    def _detail(self, connection: sqlite3.Connection, run: sqlite3.Row) -> dict:
        cache: dict = {}
        definition = self._definition(connection, run["workflow_id"], run["revision"], cache)
        value = self._summary(connection, run, cache)
        stages = json.loads(run["stages_json"])
        bindings = json.loads(run["bindings_json"])
        attempts: dict[str, list] = {}
        for row in connection.execute("SELECT * FROM attempts WHERE run_id=? ORDER BY launched_at, number",
                                      (run["id"],)):
            ended = row["ended_at"]
            attempts.setdefault(row["stage_key"], []).append({
                "id": row["id"], "number": row["number"], "iteration": row["iteration"], "state": row["state"],
                "agentId": row["agent_id"], "launchedAt": iso(row["launched_at"]), "endedAt": iso(ended),
                "durationMs": int(round((ended - row["launched_at"]) * 1000)) if ended is not None else None,
                # The text runner can't count tokens: null means unknown, not zero.
                "tokens": {"in": row["tokens_in"], "out": row["tokens_out"]} if row["tokens_known"] else None,
                "outcomeCode": row["outcome_code"]})
        value["inputs"] = json.loads(run["inputs_json"])
        decisions: dict[str, list] = {}
        for row in connection.execute("SELECT * FROM approvals WHERE run_id=? ORDER BY id", (run["id"],)):
            decisions.setdefault(row["stage_key"], []).append({
                "iteration": row["iteration"], "decision": row["decision"], "notes": row["notes"][:2_000],
                "decidedAt": iso(row["decided_at"])})
        value["stages"] = []
        for stage in definition["stages"]:
            current = stages.get(stage["key"], {})
            agent = stage["kind"] == "agent"
            entry = {
                "key": stage["key"], "kind": stage["kind"], "title": stage["title"],
                "role": stage.get("role"), "agentId": bindings.get(stage["role"]) if agent else None,
                "iteration": current.get("iteration", 1), "state": current.get("state", "pending"),
                "minutes": model.stage_minutes(definition, stage) if agent else None,
                "startedAt": iso(current.get("startedAt")), "endedAt": iso(current.get("endedAt")),
                "attempts": attempts.get(stage["key"], [])[-MAX_ATTEMPTS_SHOWN:],
                # What the stage read: run inputs ("inputs.topic") and earlier outputs ("draft.file").
                "uses": stage_reads(stage),
            }
            if stage["kind"] == "signoff":
                # The person's sign-offs, newest last.
                entry["decisions"] = decisions.get(stage["key"], [])[-20:]
            value["stages"].append(entry)
        latest: dict[tuple[str, str], sqlite3.Row] = {}
        history: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in connection.execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY id", (run["id"],)):
            latest[(row["stage_key"], row["name"])] = row
            history.setdefault((row["stage_key"], row["name"]), []).append(row)
        value["outputs"] = [self._output(row) for row in latest.values()]
        if run["state"] == "waiting_for_you":
            stage = model.stage_by_key(definition, run["stage_key"])
            producer, name = model.split_reference(stage["file"])
            rows = history.get((producer, name), [])
            current_row = next((row for row in reversed(rows) if row["sha256"] == run["waiting_sha256"]), None)
            if current_row is not None:
                signoff: dict[str, Any] = {"stageKey": stage["key"], "artifact": self._output(current_row)}
                previous = next((row for row in reversed(rows) if row["iteration"] < current_row["iteration"]), None)
                if previous is not None:
                    signoff["previous"] = self._output(previous)
                notes = []
                for row in latest.values():
                    if row["type"] == "notes" and row["value_json"]:
                        notes.extend(json.loads(row["value_json"]))
                signoff["reviewNotes"] = notes[:40]
                signoff["history"] = [{
                    "iteration": row["iteration"], "decision": row["decision"], "notes": row["notes"],
                    "artifactSha256": row["artifact_sha256"], "decidedAt": iso(row["decided_at"]),
                } for row in connection.execute(
                    "SELECT * FROM approvals WHERE run_id=? AND stage_key=? ORDER BY id", (run["id"], stage["key"]))][-20:]
                value["signoff"] = signoff
        value["tokens"] = {"in": run["tokens_in"], "out": run["tokens_out"]}
        value["allowedActions"] = allowed_actions(run)
        if len(model.canonical_json(value).encode("utf-8")) > DETAIL_BUDGET_BYTES:
            # Values stay readable through artifacts/read; the detail must fit one response.
            for output in value["outputs"]:
                output.pop("value", None)
            if "signoff" in value:
                value["signoff"]["reviewNotes"] = [
                    {"severity": note["severity"], "text": note["text"][:280]}
                    for note in value["signoff"]["reviewNotes"][:20]]
        return value


_LIVE = ",".join(f"'{state}'" for state in LIVE_ATTEMPT_STATES)
_ACTIVE = ",".join(f"'{state}'" for state in ACTIVE_STATES)


# MARK: Run transitions shared by the routes and the coordinator. Each runs inside a caller's transaction.

def initial_stages(definition: dict) -> dict:
    return {stage["key"]: {"state": "pending", "iteration": 1, "startedAt": None, "endedAt": None}
            for stage in definition["stages"]}


def stage_state(stages: dict, key: str, state: str, now: float, *, start: bool = False, end: bool = False,
                iteration: int | None = None) -> None:
    item = stages.setdefault(key, {"state": "pending", "iteration": 1, "startedAt": None, "endedAt": None})
    item["state"] = state
    if iteration is not None:
        item["iteration"] = iteration
    if start:
        item["startedAt"], item["endedAt"] = now, None
    if end:
        item["endedAt"] = now


def stage_reads(stage: dict) -> list[str]:
    """The references one stage reads, in definition order, without repeats."""
    kind = stage["kind"]
    if kind == "agent":
        reads = list(stage.get("uses") or [])
    elif kind == "check":
        reads = [rule["of"] for rule in stage.get("rules") or [] if isinstance(rule.get("of"), str)]
    elif kind == "decision":
        reads = [stage["on"]] if isinstance(stage.get("on"), str) else []
    elif kind == "signoff":
        reads = [stage["file"]] if isinstance(stage.get("file"), str) else []
    else:
        reads = []
    return list(dict.fromkeys(reads))[:32]


def add_event(connection: sqlite3.Connection, run_id: str, now: float, kind: str, text: str,
              stage_key: str | None = None, attempt: int | None = None) -> None:
    connection.execute("INSERT INTO events (run_id, at, kind, stage_key, attempt, text) VALUES (?, ?, ?, ?, ?, ?)",
                       (run_id, now, kind, stage_key, attempt, clean_text(text, 200)))


def update_run(connection: sqlite3.Connection, run: sqlite3.Row, now: float, **changes: Any) -> None:
    """Write `changes`, bump the version and refuse to overwrite a run someone else changed meanwhile."""
    names = sorted(changes)
    assignments = "".join(f"{name}=?, " for name in names)
    cursor = connection.execute(
        f"UPDATE runs SET {assignments}version=version+1, updated_at=? WHERE id=? AND version=?",
        [changes[name] for name in names] + [now, run["id"], run["version"]])
    if cursor.rowcount != 1:
        raise WorkflowError(409, "run_conflict", "The run changed. Load it again.")


def allowed_actions(run: sqlite3.Row) -> list[str]:
    state = run["state"]
    actions: list[str] = []
    if state in ("planned",) + ACTIVE_STATES + ("waiting_for_you", "needs_attention") and not (
            state in LIVE_ATTEMPT_STATES and run["cancel_requested"]):
        actions.append("cancel")
    if state in ("failed", "needs_attention"):
        actions.append("retry")
    if state in ("planned",) + ACTIVE_STATES + ("waiting_for_you",):
        actions.append("resume" if run["pause_requested"] else "pause")
    return actions


def finish(connection: sqlite3.Connection, run: sqlite3.Row, now: float, stages: dict) -> None:
    update_run(connection, run, now, state="succeeded", stages_json=model.canonical_json(stages), ended_at=now,
               waiting_sha256=None, waiting_since=None, next_stage=None)
    add_event(connection, run["id"], now, "succeeded", "The run is done.")


def go_back(connection: sqlite3.Connection, run: sqlite3.Row, definition: dict, target: str, notes: dict,
            now: float, *, state: str, count_loop: str | None = None, through: str | None = None,
            stages: dict | None = None) -> None:
    """Send the run back to `target` with the next iteration; stages on the way from there to `through` start
    over."""
    stages = json.loads(run["stages_json"]) if stages is None else stages
    iteration = run["iteration"] + 1
    for key in model.stages_between(definition, target, through or run["stage_key"]):
        stage_state(stages, key, "pending", now, iteration=iteration)
        stages[key]["startedAt"] = stages[key]["endedAt"] = None
    loops = json.loads(run["loops_json"] or "{}")
    if count_loop is not None:
        loops[count_loop] = int(loops.get(count_loop, 0)) + 1
    update_run(connection, run, now, state=state, stage_key=target, iteration=iteration,
               stages_json=model.canonical_json(stages), loops_json=model.canonical_json(loops),
               change_notes_json=model.canonical_json(notes), waiting_sha256=None, waiting_since=None,
               attention_code=None, attention_message=None, failure_stage=None, failure_code=None,
               failure_message=None, retry_stage=None, next_stage=None)


def retry(connection: sqlite3.Connection, store: WorkflowStore, run: sqlite3.Row, definition: dict, now: float) -> None:
    """A new attempt, never a silent re-run: back to planned at the right stage, with fresh role bindings."""
    bindings = {role: row["agent_id"] for role, row in store._bindings(connection, run["workflow_id"]).items()}
    add_event(connection, run["id"], now, "retried", "Trying again.", run["stage_key"])
    if run["state"] == "needs_attention" and run["attention_code"] == "revision_limit":
        decision = model.stage_by_key(definition, run["stage_key"])
        connection.execute("UPDATE runs SET bindings_json=? WHERE id=?", (model.canonical_json(bindings), run["id"]))
        notes = json.loads(run["change_notes_json"] or "null") or {"from": "review", "notes": []}
        go_back(connection, run, definition, decision["changes"]["goTo"], notes, now, state="planned",
                count_loop=decision["key"])
        return
    target = run["retry_stage"] or run["failure_stage"] or run["stage_key"]
    stages = json.loads(run["stages_json"])
    for key in model.stages_between(definition, target, run["stage_key"]):
        stage_state(stages, key, "pending", now)
        stages[key]["startedAt"] = stages[key]["endedAt"] = None
    update_run(connection, run, now, state="planned", stage_key=target, bindings_json=model.canonical_json(bindings),
               stages_json=model.canonical_json(stages), attention_code=None, attention_message=None,
               failure_stage=None, failure_code=None, failure_message=None, retry_stage=None, cancel_requested=0,
               ended_at=None, next_stage=None)
