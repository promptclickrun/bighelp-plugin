"""The workflow coordinator: one supervised process per Hermes computer that runs every workflow stage.

It owns `coordinator.lock` (flock) while it runs, writes a heartbeat every second, and moves each run one stage at a
time. Nothing ever retries by itself: when a stage ends in a way nobody can know (a reboot, a crash), the run needs
attention and the person decides.

Start it with `ensure_coordinator()`: the plugin calls that at registration and after every workflow change. It
launches `python -m loopdy_plugin.workflow_coordinator` through launchd or systemd, or as a detached process where
neither exists (a container), so it outlives the dashboard.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import workflow_model as model
from . import workflow_runner as runner
from .workflow_contract import ContractError, check_rule, parse_outputs
from .workflow_store import (
    ACTIVE_STATES, LIVE_ATTEMPT_STATES, MAX_LIVE_LINES, MAX_RUN_ARTIFACT_BYTES, RETENTION_SECONDS,
    HEARTBEAT_FRESH_SECONDS, LAUNCH_GRACE_SECONDS, TERMINAL_STATES, StoreUnavailable, WorkflowError, WorkflowStore,
    add_event, clean_text, go_back, new_id, open_private, secure_dir, stage_state, update_run,
)


TICK_SECONDS = 1.0
STOP_GRACE_SECONDS = 30
REPORT_EXIT_GRACE_SECONDS = 5
IDLE_EXIT_SECONDS = 600
PRUNE_EVERY_SECONDS = 3600
MIN_FREE_MEMORY = 512 * 1024 * 1024
_LIVE = ",".join(f"'{state}'" for state in LIVE_ATTEMPT_STATES)
_ACTIVE = ",".join(f"'{state}'" for state in ACTIVE_STATES)


def _enough_memory() -> bool:
    try:
        import psutil  # type: ignore
        return psutil.virtual_memory().available >= MIN_FREE_MEMORY
    except Exception:
        return True


def _empty(path: Path) -> bool:
    try:
        return os.lstat(path).st_size == 0
    except OSError:
        return True


class Coordinator:
    def __init__(self, store: WorkflowStore, *, host: Any | None = None, clock: Callable[[], float] | None = None,
                 hermes: list[str] | None = None, environ: dict[str, str] | None = None,
                 hermes_root: Path | None = None, memory_ok: Callable[[], bool] = _enough_memory,
                 import_path: list[str] | None = None, features: frozenset[str] | None = None):
        self.store = store
        self.host = host if host is not None else runner.OSProcessHost()
        self.clock = clock or store.clock
        self.environ = dict(os.environ if environ is None else environ)
        self.hermes = hermes or runner.hermes_argv(self.environ, importable=bool(import_path) or None)
        self.hermes_root = Path(hermes_root) if hermes_root is not None else store.root.parents[2]
        self.memory_ok = memory_ok
        # Module-form workers need Hermes' import path pinned: the launching process hands it over, because a
        # bundled bare interpreter finds `hermes_cli` only in-process. A launcher (HERMES_BIN) owns its imports.
        if not runner.is_module_argv(self.hermes):
            self.import_path: list[str] = []
        elif import_path is not None:
            self.import_path = list(import_path)
        else:
            self.import_path = runner.hermes_import_path()
        self.features = runner.FULL_FEATURES if features is None else frozenset(features)
        self.mode = runner.runner_mode(self.features)
        self.epoch: int | None = None
        self.live: dict[str, runner.LiveText] = {}
        self.coarse: dict[str, int] = {}
        self.report_seen: dict[str, float] = {}
        self.last_prune = 0.0
        self.idle_since: float | None = None
        self._lock_fd: int | None = None

    # MARK: Ownership

    def acquire(self) -> bool:
        secure_dir(self.store.root)
        descriptor = open_private(self.store.lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            return False
        self._lock_fd = descriptor
        return True

    def release(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    def now(self) -> float:
        return float(self.clock())

    def begin(self) -> int:
        """Take a new epoch and settle every stage the last coordinator left running."""
        if self._lock_fd is None:
            raise RuntimeError("acquire the coordinator lock first")
        boot = self.host.boot_id()
        now = self.now()
        with self.store.transaction() as connection:
            row = connection.execute("SELECT * FROM coordinator WHERE id=1").fetchone()
            previous_boot = row["boot_id"]
            self.epoch = row["epoch"] + 1
            connection.execute("UPDATE coordinator SET epoch=?, pid=?, fingerprint=?, boot_id=?, heartbeat_at=? "
                               "WHERE id=1", (self.epoch, os.getpid(), self.host.fingerprint(os.getpid()), boot, now))
        rebooted = previous_boot is not None and not runner.same_value(previous_boot, boot)
        with self.store.read() as connection:
            attempts = connection.execute(
                f"SELECT * FROM attempts WHERE state IN ({_LIVE}) AND coordinator_epoch<?", (self.epoch,)).fetchall()
        for attempt in attempts:
            self._recover(attempt, rebooted)
        return self.epoch

    def _recover(self, attempt: sqlite3.Row, rebooted: bool) -> None:
        pid, directory = attempt["pid"], Path(attempt["dir"])
        alive = bool(pid) and not rebooted and self.host.alive(pid) and runner.same_process(
            attempt["pid_fingerprint"], self.host.fingerprint(pid))
        report = runner.read_report(directory / "report.json", pid) if pid else None
        if alive or report is not None:
            # A live match is adopted. A finished turn with its report is checked as usual.
            with self.store.transaction() as connection:
                connection.execute("UPDATE attempts SET coordinator_epoch=? WHERE id=?", (self.epoch, attempt["id"]))
                if alive:
                    add_event(connection, attempt["run_id"], self.now(), "adopted",
                              "Picked up the running stage again.", attempt["stage_key"], attempt["number"])
            return
        code = "host_restarted" if rebooted else "coordinator_restarted"

        def settle(connection: sqlite3.Connection, run: sqlite3.Row, definition: dict, now: float) -> None:
            stage = model.stage_by_key(definition, attempt["stage_key"])
            connection.execute("UPDATE attempts SET state='unknown', outcome_code='unknown', ended_at=?, "
                               "coordinator_epoch=? WHERE id=?", (now, self.epoch, attempt["id"]))
            self._attention(connection, run, now, code, f"We don't know how {stage['title']} ended.")

        self._with_run(attempt["run_id"], settle)

    # MARK: Loop

    def run(self, stop: threading.Event, idle_exit: float = IDLE_EXIT_SECONDS) -> None:
        while not stop.is_set():
            try:
                if not self.tick():
                    return
                if self._idle(idle_exit):
                    return
            except (StoreUnavailable, WorkflowError, OSError, sqlite3.Error, ValueError, KeyError):
                # One bad pass must not stop every run; the next pass starts from the ledger again.
                pass
            stop.wait(TICK_SECONDS)

    def _idle(self, idle_exit: float) -> bool:
        with self.store.read() as connection:
            busy = connection.execute(
                f"SELECT 1 FROM attempts WHERE state IN ({_LIVE}) LIMIT 1").fetchone() or connection.execute(
                f"SELECT 1 FROM runs WHERE state IN ('planned',{_ACTIVE}) AND pause_requested=0 LIMIT 1").fetchone()
        now = self.now()
        if busy:
            self.idle_since = None
            return False
        if self.idle_since is None:
            self.idle_since = now
        return now - self.idle_since >= idle_exit

    def tick(self) -> bool:
        """One pass. False when another coordinator took over this ledger."""
        now = self.now()
        with self.store.transaction() as connection:
            if connection.execute("UPDATE coordinator SET heartbeat_at=? WHERE id=1 AND epoch=?",
                                  (now, self.epoch)).rowcount != 1:
                return False
        with self.store.read() as connection:
            attempts = connection.execute(f"SELECT * FROM attempts WHERE state IN ({_LIVE}) AND coordinator_epoch=?",
                                          (self.epoch,)).fetchall()
        for attempt in attempts:
            self._watch(attempt)
        with self.store.read() as connection:
            accepted = connection.execute("SELECT id FROM runs WHERE state='accepted' ORDER BY seq").fetchall()
        for row in accepted:
            self._advance(row["id"])
        self._admit()
        if now - self.last_prune >= PRUNE_EVERY_SECONDS:
            self.last_prune = now
            self.prune()
        return True

    def _with_run(self, run_id: str, change: Callable[[sqlite3.Connection, sqlite3.Row, dict, float], Any]) -> Any:
        """Run `change` on a fresh copy of the run inside one write transaction."""
        with self.store.transaction() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                return None
            definition = self.store._definition(connection, run["workflow_id"], run["revision"])
            return change(connection, run, definition, self.now())

    # MARK: Attempts

    def _watch(self, attempt: sqlite3.Row) -> None:
        pid = attempt["pid"]
        directory = Path(attempt["dir"])
        if pid is None:
            # The launch never recorded a process (a crash between the two launch commits).
            def settle(connection, run, definition, now):
                stage = model.stage_by_key(definition, attempt["stage_key"])
                connection.execute("UPDATE attempts SET state='unknown', outcome_code='unknown', ended_at=? "
                                   "WHERE id=?", (now, attempt["id"]))
                self._attention(connection, run, now, "coordinator_restarted",
                                f"We don't know how {stage['title']} ended.")
            self._with_run(attempt["run_id"], settle)
            return
        stream = attempt["runner"] == "stream"
        offset = self._read_stream(attempt, attempt["stream_offset"]) if stream else 0
        own = self.host.owns(pid)
        exit_code = self.host.poll(pid) if own else None
        if own:
            alive = exit_code is None and self.host.alive(pid)
        else:
            alive = self.host.alive(pid) and runner.same_process(attempt["pid_fingerprint"],
                                                                 self.host.fingerprint(pid))
        now = self.now()
        report = runner.read_report(directory / "report.json", pid)
        if alive:
            runner.trim_stderr(directory / "stderr.log")
            if not stream:
                self._coarse(attempt, now)
            with self.store.read() as connection:
                run = connection.execute("SELECT cancel_requested FROM runs WHERE id=?",
                                         (attempt["run_id"],)).fetchone()
            if attempt["stop_reason"] is not None:
                if attempt["kill_at"] is not None and now >= attempt["kill_at"]:
                    self.host.signal_group(pid, runner.stop_signals()[1])
                    with self.store.transaction() as connection:
                        connection.execute("UPDATE attempts SET kill_at=? WHERE id=?",
                                           (now + STOP_GRACE_SECONDS, attempt["id"]))
            elif run is not None and run["cancel_requested"]:
                self._stop(attempt, "cancel")
            elif now >= attempt["deadline_at"]:
                self._stop(attempt, "timeout")
            elif report is not None:
                # The turn is over and Hermes lingers for nested replies a stage doesn't need.
                seen = self.report_seen.setdefault(attempt["id"], now)
                if now - seen >= REPORT_EXIT_GRACE_SECONDS:
                    self._stop(attempt, "linger")
            return
        self.report_seen.pop(attempt["id"], None)
        if stream:
            self._read_stream(attempt, offset, final=True)
        self._ended(attempt, exit_code if own else None, report)

    def _stop(self, attempt: sqlite3.Row, reason: str) -> None:
        self.host.signal_group(attempt["pid"], runner.stop_signals()[0])

        def change(connection, run, definition, now):
            connection.execute("UPDATE attempts SET stop_reason=?, kill_at=? WHERE id=?",
                               (reason, now + STOP_GRACE_SECONDS, attempt["id"]))
            if reason == "timeout":
                stage = model.stage_by_key(definition, attempt["stage_key"])
                add_event(connection, run["id"], now, "timed_out", f"{stage['title']} ran out of time.",
                          attempt["stage_key"], attempt["number"])
        self._with_run(attempt["run_id"], change)

    def _read_stream(self, attempt: sqlite3.Row, start: int, final: bool = False) -> int:
        path = Path(attempt["dir"]) / "stream.jsonl"
        offset = start
        live = self.live.setdefault(attempt["id"], runner.LiveText())
        lines: list[tuple[str, str]] = []
        counted: tuple[int, int] | None = None
        for _ in range(64 if final else 4):
            offset_after, records = runner.read_stream(path, offset)
            if offset_after == offset:
                break
            offset = offset_after
            for record in records:
                lines.extend(live.lines(record))
                counted = runner.tokens(record) or counted
        if offset == start and not lines:
            return offset
        now = self.now()
        with self.store.transaction() as connection:
            connection.execute("UPDATE attempts SET stream_offset=? WHERE id=?", (offset, attempt["id"]))
            if counted is not None:
                connection.execute("UPDATE attempts SET tokens_in=?, tokens_out=? WHERE id=?",
                                   (counted[0], counted[1], attempt["id"]))
            self._add_lines(connection, attempt["id"], lines, now)
        return offset

    @staticmethod
    def _add_lines(connection: sqlite3.Connection, attempt_id: str, lines: list[tuple[str, str]], now: float) -> None:
        if not lines:
            return
        last = connection.execute("SELECT COALESCE(MAX(seq), 0) FROM live_lines WHERE attempt_id=?",
                                  (attempt_id,)).fetchone()[0]
        connection.executemany(
            "INSERT INTO live_lines (attempt_id, seq, at, kind, text) VALUES (?, ?, ?, ?, ?)",
            [(attempt_id, last + index + 1, now, kind, text) for index, (kind, text) in enumerate(lines)])
        connection.execute("DELETE FROM live_lines WHERE attempt_id=? AND seq<=?",
                           (attempt_id, last + len(lines) - MAX_LIVE_LINES))

    def _coarse(self, attempt: sqlite3.Row, now: float) -> None:
        """The text runner has no stream: say it started, then once a minute that it still works."""
        minutes = max(0, int((now - attempt["launched_at"]) // 60))
        last = self.coarse.get(attempt["id"])
        if last is not None and minutes <= last:
            return
        self.coarse[attempt["id"]] = minutes
        with self.store.transaction() as connection:
            if last is None:
                if connection.execute("SELECT 1 FROM live_lines WHERE attempt_id=? LIMIT 1",
                                      (attempt["id"],)).fetchone() is not None:
                    return
                lines = [("init", "Started. This Hermes shows no live steps, so the result comes at the end.")]
            else:
                lines = [("text", f"Still working ({minutes} min).")]
            self._add_lines(connection, attempt["id"], lines, now)

    def _ended(self, attempt: sqlite3.Row, exit_code: int | None, report: dict | None) -> None:
        self.live.pop(attempt["id"], None)
        self.coarse.pop(attempt["id"], None)
        directory = Path(attempt["dir"])
        # Keep only the last 4 KB of what the worker wrote to stderr. It stays in the attempt folder.
        stderr_tail = runner.trim_stderr(directory / "stderr.log", above=runner.STDERR_TAIL_BYTES)
        stream = attempt["runner"] == "stream"
        output_path = directory / ("stream.jsonl" if stream else "reply.txt")
        reason = attempt["stop_reason"]
        if reason == "linger" and report is not None:
            exit_code, reason = report["exit_code"], None
        if exit_code is None and report is not None:
            exit_code = report["exit_code"]

        def stopped(connection, run, definition, now):
            stage = model.stage_by_key(definition, attempt["stage_key"])
            stages = json.loads(run["stages_json"])
            if reason == "timeout":
                connection.execute("UPDATE attempts SET state='timed_out', outcome_code='timed_out', ended_at=?, "
                                   "exit_code=? WHERE id=?", (now, exit_code, attempt["id"]))
                self._fail(connection, run, now, attempt["stage_key"], "timed_out",
                           f"{stage['title']} ran out of time.")
                return
            connection.execute("UPDATE attempts SET state='cancelled', outcome_code='cancelled', ended_at=?, "
                               "exit_code=? WHERE id=?", (now, exit_code, attempt["id"]))
            stage_state(stages, attempt["stage_key"], "cancelled", now, end=True)
            update_run(connection, run, now, state="cancelled", ended_at=now,
                       stages_json=model.canonical_json(stages))
            add_event(connection, run["id"], now, "cancelled", "The run was cancelled.", attempt["stage_key"])

        if reason in ("timeout", "cancel"):
            self._with_run(attempt["run_id"], stopped)
            return
        with self.store.read() as connection:
            cancelled = connection.execute("SELECT cancel_requested FROM runs WHERE id=?",
                                           (attempt["run_id"],)).fetchone()
        if cancelled is not None and cancelled["cancel_requested"]:
            self._with_run(attempt["run_id"], stopped)
            return
        if exit_code is None:
            def unknown(connection, run, definition, now):
                stage = model.stage_by_key(definition, attempt["stage_key"])
                connection.execute("UPDATE attempts SET state='unknown', outcome_code='unknown', ended_at=? "
                                   "WHERE id=?", (now, attempt["id"]))
                self._attention(connection, run, now, "coordinator_restarted",
                                f"We don't know how {stage['title']} ended.")
            self._with_run(attempt["run_id"], unknown)
            return

        def checking(connection, run, definition, now):
            stage = model.stage_by_key(definition, attempt["stage_key"])
            stages = json.loads(run["stages_json"])
            connection.execute("UPDATE attempts SET state='checking_output', exit_code=? WHERE id=?",
                               (exit_code, attempt["id"]))
            if not stream:
                self._add_lines(connection, attempt["id"],
                                [("result", "Finished." if exit_code == 0 else "Ended with an error.")], now)
            stage_state(stages, attempt["stage_key"], "checking_output", now)
            update_run(connection, run, now, state="checking_output", stages_json=model.canonical_json(stages))
            add_event(connection, run["id"], now, "stage_checking", f"Checking what {stage['title']} handed off.",
                      attempt["stage_key"], attempt["number"])
            return definition, stage

        result = self._with_run(attempt["run_id"], checking)
        if result is None:
            return
        definition, stage = result
        if exit_code != 0:
            message = f"{stage['title']} stopped with an error."
            explain = None
            if _empty(output_path):
                # It never got going: say why, from its error output, without quoting it.
                explain = f"{runner.stderr_summary(stderr_tail)} (exit code {exit_code})"
                message = f"{stage['title']} stopped before its turn began. {explain}"
            self._attempt_failed(attempt, "agent_exit", message, explain=explain)
            return
        reply = report.get("reply") if report is not None and isinstance(report.get("reply"), str) else None
        if reply is None and stream:
            reply = self._last_result_text(output_path)
        elif reply is None:
            data = runner.read_tail(output_path, runner.MAX_REPORT_BYTES)
            reply = data.decode("utf-8", "replace") if data is not None else None
        try:
            outputs = parse_outputs(reply or "", stage["outputs"], attempt_dir=Path(attempt["dir"]),
                                    stage_title=stage["title"])
        except ContractError as error:
            self._attempt_failed(attempt, error.code, error.message)
            return
        digests = [self.store.write_artifact(output.data) for output in outputs]

        def accept(connection, run, definition, now):
            total = sum(len(output.data) for output in outputs)
            if run["artifact_bytes"] + total > MAX_RUN_ARTIFACT_BYTES:
                connection.execute("UPDATE attempts SET state='failed', outcome_code='storage_full', ended_at=? "
                                   "WHERE id=?", (now, attempt["id"]))
                self._fail(connection, run, now, attempt["stage_key"], "storage_full",
                           "This run has used its 50 MB of files.")
                return
            for output, digest in zip(outputs, digests):
                value_json = None if output.type == "markdown_file" else model.canonical_json(output.value)
                connection.execute(
                    "INSERT INTO artifacts (sha256, run_id, stage_key, iteration, attempt_id, name, type, bytes, "
                    "word_count, number_value, value_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (digest, run["id"], attempt["stage_key"], attempt["iteration"], attempt["id"], output.name,
                     output.type, len(output.data), output.word_count, output.number_value, value_json))
            row = connection.execute("SELECT tokens_in, tokens_out FROM attempts WHERE id=?",
                                     (attempt["id"],)).fetchone()
            connection.execute("UPDATE attempts SET state='accepted', outcome_code='accepted', ended_at=? WHERE id=?",
                               (now, attempt["id"]))
            stages = json.loads(run["stages_json"])
            stage_state(stages, attempt["stage_key"], "accepted", now, end=True)
            update_run(connection, run, now, state="accepted", stages_json=model.canonical_json(stages),
                       next_stage=model.next_stage_key(definition, attempt["stage_key"]), change_notes_json=None,
                       artifact_bytes=run["artifact_bytes"] + total, tokens_in=run["tokens_in"] + row["tokens_in"],
                       tokens_out=run["tokens_out"] + row["tokens_out"])
            add_event(connection, run["id"], now, "stage_accepted", f"{stage['title']} is done.",
                      attempt["stage_key"], attempt["number"])

        self._with_run(attempt["run_id"], accept)

    @staticmethod
    def _last_result_text(path: Path) -> str | None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return None
        try:
            size = os.fstat(descriptor).st_size
            start = max(0, size - 4 * 1024 * 1024)
            os.lseek(descriptor, start, os.SEEK_SET)
            data = os.read(descriptor, size - start)
        finally:
            os.close(descriptor)
        for line in reversed(data.splitlines()):
            try:
                record = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("type") == "result":
                return record.get("text") if isinstance(record.get("text"), str) else None
        return None

    def _attempt_failed(self, attempt: sqlite3.Row, code: str, message: str, *, explain: str | None = None) -> None:
        def change(connection, run, definition, now):
            if explain is not None:
                add_event(connection, run["id"], now, "agent_error", explain, attempt["stage_key"], attempt["number"])
            row = connection.execute("SELECT tokens_in, tokens_out FROM attempts WHERE id=?",
                                     (attempt["id"],)).fetchone()
            connection.execute("UPDATE attempts SET state='failed', outcome_code=?, ended_at=? WHERE id=?",
                               (code, now, attempt["id"]))
            connection.execute("UPDATE runs SET tokens_in=tokens_in+?, tokens_out=tokens_out+? WHERE id=?",
                               (row["tokens_in"], row["tokens_out"], run["id"]))
            self._fail(connection, run, now, attempt["stage_key"], code, message)
        self._with_run(attempt["run_id"], change)

    def _fail(self, connection: sqlite3.Connection, run: sqlite3.Row, now: float, stage_key: str, code: str,
              message: str, retry_stage: str | None = None, stages: dict | None = None) -> None:
        stages = json.loads(run["stages_json"]) if stages is None else stages
        stage_state(stages, stage_key, "failed", now, end=True)
        update_run(connection, run, now, state="failed", stage_key=stage_key, failure_stage=stage_key,
                   failure_code=code, failure_message=clean_text(message, 300), retry_stage=retry_stage,
                   ended_at=now, stages_json=model.canonical_json(stages))
        add_event(connection, run["id"], now, "failed" if code != "check_failed" else "check_failed",
                  message, stage_key)

    def _attention(self, connection: sqlite3.Connection, run: sqlite3.Row, now: float, code: str, message: str,
                   *, stage_key: str | None = None, stages: dict | None = None, **extra: Any) -> None:
        stage_key = stage_key or run["stage_key"]
        stages = json.loads(run["stages_json"]) if stages is None else stages
        stage_state(stages, stage_key, "needs_attention", now)
        update_run(connection, run, now, state="needs_attention", stage_key=stage_key, attention_code=code,
                   attention_message=clean_text(message, 300), stages_json=model.canonical_json(stages), **extra)
        add_event(connection, run["id"], now, "needs_attention", message, stage_key)

    # MARK: Stages

    def _advance(self, run_id: str) -> None:
        def change(connection, run, definition, now):
            if run["state"] != "accepted" or run["pause_requested"]:
                return None
            if run["cancel_requested"]:
                update_run(connection, run, now, state="cancelled", ended_at=now)
                add_event(connection, run["id"], now, "cancelled", "The run was cancelled.")
                return None
            if run["next_stage"] is None:
                stages = json.loads(run["stages_json"])
                update_run(connection, run, now, state="succeeded", ended_at=now, next_stage=None,
                           stages_json=model.canonical_json(stages))
                add_event(connection, run["id"], now, "succeeded", "The run is done.")
                return None
            return run["next_stage"]
        target = self._with_run(run_id, change)
        if target is not None:
            self._start(run_id, target, from_states=("accepted",))

    def _admit(self) -> None:
        with self.store.read() as connection:
            slots = connection.execute("SELECT slots_total FROM coordinator WHERE id=1").fetchone()[0]
            used = connection.execute(f"SELECT COUNT(*) FROM runs WHERE state IN ({_ACTIVE})").fetchone()[0]
            planned = connection.execute("SELECT id, stage_key FROM runs WHERE state='planned' AND pause_requested=0 "
                                         "AND cancel_requested=0 ORDER BY seq").fetchall()
        for row in planned:
            if used >= slots or not self.memory_ok():
                return
            if self._start(row["id"], row["stage_key"], from_states=("planned",)):
                used += 1

    def _start(self, run_id: str, stage_key: str, *, from_states: tuple[str, ...]) -> bool:
        with self.store.read() as connection:
            run = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None or run["state"] not in from_states:
                return False
            definition = self.store._definition(connection, run["workflow_id"], run["revision"])
        stage = model.stage_by_key(definition, stage_key)
        if stage["kind"] == "agent":
            return self._launch(run, definition, stage, from_states)

        def change(connection, run, definition, now):
            if run["state"] not in from_states:
                return False
            stages = json.loads(run["stages_json"])
            iteration = run["iteration"]
            stage_state(stages, stage_key, "running", now, start=True, iteration=iteration)
            if stage["kind"] == "check":
                self._check(connection, run, definition, stage, stages, now)
            elif stage["kind"] == "decision":
                self._decide(connection, run, definition, stage, stages, now)
            else:
                self._wait_for_signoff(connection, run, definition, stage, stages, now)
            return True
        return bool(self._with_run(run_id, change))

    @staticmethod
    def _latest(connection: sqlite3.Connection, run_id: str, reference: str) -> sqlite3.Row | None:
        stage_key, name = model.split_reference(reference)
        return connection.execute("SELECT * FROM artifacts WHERE run_id=? AND stage_key=? AND name=? "
                                  "ORDER BY id DESC LIMIT 1", (run_id, stage_key, name)).fetchone()

    def _check(self, connection, run, definition, stage, stages, now) -> None:
        for rule in stage["rules"]:
            row = self._latest(connection, run["id"], rule["of"])
            if row is None:
                reason = f"{rule['of']} is missing."
            else:
                try:
                    data = self.store.read_artifact_bytes(row["sha256"])
                except WorkflowError:
                    data = b""
                reason = check_rule(rule, row["type"], data)
            if reason is not None:
                producer, _ = model.split_reference(rule["of"])
                self._fail(connection, run, now, stage["key"], "check_failed", f"{stage['title']}: {reason}",
                           retry_stage=producer, stages=stages)
                return
        stage_state(stages, stage["key"], "accepted", now, end=True)
        update_run(connection, run, now, state="accepted", stage_key=stage["key"],
                   stages_json=model.canonical_json(stages), next_stage=model.next_stage_key(definition, stage["key"]))
        add_event(connection, run["id"], now, "check_passed", f"{stage['title']} passed.", stage["key"])

    def _decide(self, connection, run, definition, stage, stages, now) -> None:
        row = self._latest(connection, run["id"], stage["on"])
        value = json.loads(row["value_json"]) if row is not None and row["value_json"] else None
        source, _ = model.split_reference(stage["on"])
        if value != "changes":
            stage_state(stages, stage["key"], "accepted", now, end=True)
            target = model.next_stage_key(definition, stage["key"]) if stage["pass"] == "next" else stage["pass"]
            update_run(connection, run, now, state="accepted", stage_key=stage["key"],
                       stages_json=model.canonical_json(stages), next_stage=target)
            add_event(connection, run["id"], now, "decision_pass", f"{stage['title']}: passed.", stage["key"])
            return
        notes = []
        for artifact in connection.execute(
                "SELECT value_json FROM artifacts WHERE run_id=? AND stage_key=? AND type='notes' AND iteration=? "
                "ORDER BY id", (run["id"], source, row["iteration"])):
            notes.extend(json.loads(artifact["value_json"] or "[]"))
        loops = json.loads(run["loops_json"] or "{}")
        used = int(loops.get(stage["key"], 0))
        limit = model.max_revisions(definition, stage)
        if used >= limit:
            self._attention(connection, run, now, "revision_limit",
                            f"{stage['title']} asked for changes more than {limit} times.", stage_key=stage["key"],
                            stages=stages, change_notes_json=model.canonical_json({"from": "review", "notes": notes}))
            return
        add_event(connection, run["id"], now, "decision_changes", f"{stage['title']}: sent back for changes.",
                  stage["key"])
        go_back(connection, run, definition, stage["changes"]["goTo"], {"from": "review", "notes": notes}, now,
                state="planned", count_loop=stage["key"], through=stage["key"], stages=stages)

    def _wait_for_signoff(self, connection, run, definition, stage, stages, now) -> None:
        row = self._latest(connection, run["id"], stage["file"])
        if row is None:
            self._fail(connection, run, now, stage["key"], "contract_missing_output",
                       f"{stage['title']} has no file to show.", stages=stages)
            return
        stage_state(stages, stage["key"], "waiting_for_you", now)
        update_run(connection, run, now, state="waiting_for_you", stage_key=stage["key"],
                   stages_json=model.canonical_json(stages), waiting_sha256=row["sha256"], waiting_since=now,
                   next_stage=None)
        add_event(connection, run["id"], now, "waiting_for_you", f"{stage['title']} is waiting for you.", stage["key"])

    def _uses(self, connection: sqlite3.Connection, run: sqlite3.Row, definition: dict, stage: dict,
              directory: Path) -> list[dict]:
        inputs = json.loads(run["inputs_json"])
        labels = {item["key"]: item["label"] for item in definition["inputs"]}
        result = []
        for reference in stage["uses"]:
            head, name = model.split_reference(reference)
            if head == "inputs":
                value = inputs.get(name)
                text = "" if value is None else (format(value, "g") if isinstance(value, float) else str(value))
                result.append({"reference": reference, "label": labels.get(name, name), "text": text})
                continue
            row = self._latest(connection, run["id"], reference)
            source = model.stage_by_key(definition, head)
            label = f"{source['title'] if source else head}: {name}"
            if row is None:
                result.append({"reference": reference, "label": label, "text": ""})
                continue
            data = self.store.read_artifact_bytes(row["sha256"])
            item: dict[str, Any] = {"reference": reference, "label": label}
            if row["type"] == "markdown_file":
                file_name = f"inputs/{head}.{name}.md"
                runner.write_private(directory / file_name, data, read_only=True)
                item["file"] = file_name
                item["text"] = data.decode("utf-8", "replace") if len(data) <= runner.MAX_INLINE_BYTES else None
            elif row["type"] == "notes":
                notes = json.loads(row["value_json"] or "[]")
                item["text"] = "\n".join(f"- [{note['severity']}] {note['text']}" for note in notes) or "(no notes)"
            elif row["type"] == "text":
                item["text"] = data.decode("utf-8", "replace")
            else:
                item["text"] = str(json.loads(row["value_json"]))
            result.append(item)
        return result

    def _launch(self, run: sqlite3.Row, definition: dict, stage: dict, from_states: tuple[str, ...]) -> bool:
        bindings = json.loads(run["bindings_json"])
        agent = bindings.get(stage["role"])
        home = runner.profile_home(self.hermes_root, agent) if agent else None
        if home is None:
            role = next((item["label"] for item in definition["roles"] if item["key"] == stage["role"]), stage["role"])

            def missing(connection, fresh, definition, now):
                if fresh["state"] in from_states:
                    self._attention(connection, fresh, now, "agent_missing",
                                    f"The agent for {role} no longer exists.", stage_key=stage["key"])
                return False
            self._with_run(run["id"], missing)
            return False
        with self.store.read() as connection:
            number = connection.execute("SELECT COUNT(*) FROM attempts WHERE run_id=? AND stage_key=?",
                                        (run["id"], stage["key"])).fetchone()[0] + 1
            iteration = run["iteration"]
            directory = runner.attempt_dir(self.store.runs_dir, run["id"], stage["key"], iteration, number)
            runner.prepare_attempt(directory)
            uses = self._uses(connection, run, definition, stage, directory)
            workflow = connection.execute("SELECT name FROM workflows WHERE id=?", (run["workflow_id"],)).fetchone()
        os.chmod(directory / "inputs", 0o500)
        notes = json.loads(run["change_notes_json"]) if run["change_notes_json"] else None
        brief = runner.render_brief(workflow_name=workflow["name"] if workflow else definition["name"],
                                    run_number=run["number"], stage=stage, iteration=iteration, uses=uses,
                                    change_notes=notes)
        runner.write_private(directory / "brief.md", brief.encode("utf-8"))
        attempt_id = new_id("att")
        minutes = model.stage_minutes(definition, stage)
        stream = self.mode == "stream"

        def launched(connection, fresh, definition, now):
            if fresh["state"] not in from_states or fresh["version"] != run["version"] or fresh["cancel_requested"]:
                return False
            connection.execute(
                "INSERT INTO attempts (id, run_id, stage_key, iteration, number, state, agent_id, coordinator_epoch, "
                "launched_at, deadline_at, dir, runner, tokens_known) "
                "VALUES (?, ?, ?, ?, ?, 'launched', ?, ?, ?, ?, ?, ?, ?)",
                (attempt_id, fresh["id"], stage["key"], iteration, number, agent, self.epoch, now,
                 now + minutes * 60, str(directory), self.mode, 1 if stream else 0))
            stages = json.loads(fresh["stages_json"])
            stage_state(stages, stage["key"], "launched", now, start=True, iteration=iteration)
            update_run(connection, fresh, now, state="launched", stage_key=stage["key"], next_stage=None,
                       stages_json=model.canonical_json(stages),
                       started_at=fresh["started_at"] if fresh["started_at"] is not None else now)
            add_event(connection, fresh["id"], now, "stage_launched", f"{stage['title']} started with {agent}.",
                      stage["key"], number)
            return True

        if not self._with_run(run["id"], launched):
            return False
        argv = runner.worker_argv(self.hermes, agent, stage["tools"], self.features, query=runner.text_query(brief))
        env = runner.worker_env(self.environ, home=home, profile=agent, attempt_dir=directory,
                                import_path=self.import_path, report=stream)
        try:
            if "toolsets" not in self.features:
                # Without --toolsets the agent would get every tool it has, messaging included.
                raise ValueError("this Hermes can't limit a stage's tools")
            pid = self.host.spawn(argv, env=env, cwd=directory,
                                  stdout_path=directory / ("stream.jsonl" if stream else "reply.txt"),
                                  stderr_path=directory / "stderr.log")
        except (OSError, ValueError, StoreUnavailable):
            def failed(connection, fresh, definition, now):
                connection.execute("UPDATE attempts SET state='failed', outcome_code='spawn_failed', ended_at=? "
                                   "WHERE id=?", (now, attempt_id))
                self._fail(connection, fresh, now, stage["key"], "spawn_failed", f"{stage['title']} couldn't start.")
            self._with_run(run["id"], failed)
            return True
        fingerprint = self.host.fingerprint(pid) or "unverified"

        def running(connection, fresh, definition, now):
            connection.execute("UPDATE attempts SET state='running', pid=?, pid_fingerprint=? WHERE id=?",
                               (pid, fingerprint, attempt_id))
            if fresh["state"] == "launched":
                stages = json.loads(fresh["stages_json"])
                stage_state(stages, stage["key"], "running", now)
                update_run(connection, fresh, now, state="running", stages_json=model.canonical_json(stages))
            add_event(connection, fresh["id"], now, "stage_running", f"{stage['title']} is running.",
                      stage["key"], number)
        self._with_run(run["id"], running)
        return True

    # MARK: Retention

    def prune(self) -> None:
        now = self.now()
        cutoff = now - RETENTION_SECONDS
        with self.store.transaction() as connection:
            states = ",".join(f"'{state}'" for state in TERMINAL_STATES)
            old = [row["id"] for row in connection.execute(
                f"SELECT id FROM runs WHERE state IN ({states}) AND COALESCE(ended_at, updated_at) < ?", (cutoff,))]
            for run_id in old:
                attempts = [row["id"] for row in connection.execute("SELECT id FROM attempts WHERE run_id=?",
                                                                    (run_id,))]
                for attempt_id in attempts:
                    connection.execute("DELETE FROM live_lines WHERE attempt_id=?", (attempt_id,))
                for table in ("attempts", "artifacts", "approvals", "events"):
                    connection.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
                connection.execute("DELETE FROM runs WHERE id=?", (run_id,))
            connection.execute("DELETE FROM requests WHERE at < ?", (now - 24 * 3600,))
            referenced = {row[0] for row in connection.execute("SELECT DISTINCT sha256 FROM artifacts")}
        for run_id in old:
            _remove_tree(self.store.runs_dir, run_id)
        try:
            names = os.listdir(self.store.artifacts)
        except OSError:
            names = []
        for name in names:
            if len(name) == 64 and name not in referenced:
                try:
                    os.unlink(self.store.artifacts / name)
                except OSError:
                    pass


def _remove_tree(parent: Path, name: str) -> None:
    """Delete runs/<run> without following links out of it."""
    target = parent / name
    try:
        info = os.lstat(target)
    except OSError:
        return
    if not os.path.isdir(target) or os.path.islink(target) or info.st_uid != os.geteuid():
        return
    for directory, folders, files in os.walk(target, topdown=False):
        for item in files + folders:
            path = os.path.join(directory, item)
            try:
                if os.path.islink(path) or not os.path.isdir(path):
                    os.unlink(path)
                else:
                    os.chmod(path, 0o700)
                    os.rmdir(path)
            except OSError:
                try:
                    os.chmod(directory, 0o700)
                    os.unlink(path) if not os.path.isdir(path) else os.rmdir(path)
                except OSError:
                    pass
    try:
        os.rmdir(target)
    except OSError:
        pass


# MARK: Starting the coordinator

def _lock_free(store: WorkflowStore) -> bool:
    try:
        descriptor = open_private(store.lock_path, os.O_RDWR | os.O_CREAT)
    except StoreUnavailable:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return False
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)
    return True


def service_label(root: Path) -> str:
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    return f"app.loopdy.workflows.{os.getuid()}.{digest}"


def _launcher_env() -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "HOME": str(Path.home()),
           "LANG": "C.UTF-8"}
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if os.path.isdir(runtime):
        env["XDG_RUNTIME_DIR"] = runtime
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        env["DBUS_SESSION_BUS_ADDRESS"] = os.environ["DBUS_SESSION_BUS_ADDRESS"]
    return env


def service_manager() -> str | None:
    """launchd or systemd where they exist; otherwise (a container, a hosted Hermes) a detached process."""
    if os.name != "posix":
        return None
    system = platform.system()
    if system == "Darwin" and shutil.which("launchctl"):
        return "launchd"
    if system == "Linux" and shutil.which("systemd-run") and _systemd_user_available():
        return "systemd"
    return "detached"


def _systemd_user_available() -> bool:
    """`systemd-run --user` needs a user manager; containers often ship the binary without one."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.exists(os.path.join(runtime, "systemd", "private")) or bool(
        os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


# Detached coordinators this process started, so their exits are collected (no zombies).
_detached: list[subprocess.Popen] = []


def coordinator_command(root: Path, *, environ: dict[str, str] | None = None) -> list[str]:
    """`env -i … python -m loopdy_plugin.workflow_coordinator …` with what the coordinator can't find itself.

    Hermes may run on a bundled bare interpreter that knows Hermes' folder only in-process, so this process
    resolves how to start Hermes, its import path and the chat flags it has, and hands them over.
    """
    environ = dict(os.environ if environ is None else environ)
    plugin_root = str(Path(__file__).resolve().parents[1])
    import_path = runner.hermes_import_path()
    hermes = runner.hermes_argv(environ, importable=bool(import_path) or runner.hermes_import_root() is not None)
    features = runner.detect_hermes_features(hermes, import_path)
    env_tool = shutil.which("env") or "/usr/bin/env"
    python_path = os.pathsep.join(dict.fromkeys([plugin_root, *import_path]))
    command = [env_tool, "-i", f"PATH={environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
               f"HOME={Path.home()}", f"HERMES_HOME={root.parents[2]}", "LANG=C.UTF-8", "PYTHONUTF8=1",
               f"PYTHONPATH={python_path}"]
    for key in ("HERMES_BIN", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if environ.get(key):
            command.append(f"{key}={environ[key]}")
    command += [sys.executable, "-m", "loopdy_plugin.workflow_coordinator", "--data-root", str(root),
                "--hermes-features", runner.encode_features(features)]
    if runner.is_module_argv(hermes):
        command += ["--hermes-path", os.pathsep.join(import_path)]
    else:
        command += ["--hermes-bin", hermes[0]]
    return command


def launch_service(root: Path, *, manager: str | None = None) -> bool:
    """Start the coordinator under launchd or systemd, or as a detached process, so it outlives us."""
    manager = manager or service_manager()
    if manager is None:
        return False
    label = service_label(root)
    try:
        command = coordinator_command(root)
    except Exception:
        return False
    if manager == "detached":
        return _launch_detached(command)
    if manager == "launchd":
        launchctl = shutil.which("launchctl")
        subprocess.run([launchctl, "remove", label], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, check=False, env=_launcher_env())
        full = [launchctl, "submit", "-l", label, "-o", "/dev/null", "-e", "/dev/null", "--", *command,
                "--service-label", label]
    else:
        full = [shutil.which("systemd-run"), "--user", f"--unit=loopdy-workflows-{label.rsplit('.', 1)[-1]}",
                "--collect", "--no-block", "--service-type=exec", "--property=KillMode=process", "--", *command]
    try:
        result = subprocess.run(full, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=15, check=False, env=_launcher_env())
        if result.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    # The service manager refused (no user session, for example): a detached process still works.
    return _launch_detached(command)


def _launch_detached(command: list[str]) -> bool:
    """No service manager here: a child in its own session, so it lives on when the dashboard restarts.

    The coordinator's flock and the ledger's launch time stop a second one, and it exits after ten idle
    minutes like the managed ones. A container restart ends it; its running stage then needs attention.
    """
    for process in list(_detached):
        if process.poll() is not None:
            _detached.remove(process)
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, cwd="/")
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    _detached.append(process)
    return True


def ensure_coordinator(root: Path, *, launcher: Callable[[Path], bool] | None = None,
                       clock: Callable[[], float] = time.time, create: bool = False) -> str:
    """online, starting or offline. Starts the coordinator when there is work and nobody runs it."""
    store = WorkflowStore(root, clock=clock)
    if not create and not store.exists():
        return "offline"
    now = float(clock())
    with store.read() as connection:
        row = connection.execute("SELECT heartbeat_at, launched_at FROM coordinator WHERE id=1").fetchone()
    if row["heartbeat_at"] is not None and now - row["heartbeat_at"] <= HEARTBEAT_FRESH_SECONDS:
        return "online"
    if not store.has_work():
        return "offline"
    if not _lock_free(store):
        return "starting"
    with store.transaction() as connection:
        row = connection.execute("SELECT launched_at FROM coordinator WHERE id=1").fetchone()
        if row["launched_at"] is not None and now - row["launched_at"] <= LAUNCH_GRACE_SECONDS:
            return "starting"
        connection.execute("UPDATE coordinator SET launched_at=? WHERE id=1", (now,))
    return "starting" if (launcher or launch_service)(root) else "offline"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bighelp-workflow-coordinator")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--service-label")
    parser.add_argument("--hermes-path")
    parser.add_argument("--hermes-bin")
    parser.add_argument("--hermes-features")
    args = parser.parse_args(argv)
    stop = threading.Event()
    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signal.signal(getattr(signal, name), lambda *_: stop.set())
    import_path = None if args.hermes_path is None else [item for item in args.hermes_path.split(os.pathsep) if item]
    coordinator = Coordinator(
        WorkflowStore(Path(args.data_root)), hermes=[args.hermes_bin] if args.hermes_bin else None,
        import_path=import_path,
        features=None if args.hermes_features is None else runner.decode_features(args.hermes_features))
    try:
        if coordinator.acquire():
            try:
                coordinator.begin()
                coordinator.run(stop)
            finally:
                coordinator.release()
    finally:
        if args.service_label and shutil.which("launchctl"):
            # A launchd submit job is kept alive; remove it so an idle exit stays an exit.
            subprocess.run([shutil.which("launchctl"), "remove", args.service_label], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
