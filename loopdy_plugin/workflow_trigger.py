"""How a workflow starts: by hand ("manual") or on a schedule.

A schedule is a Hermes cron job with no agent (`no_agent=True`): its script, one small file in this profile's
`scripts/` folder, starts one run of the workflow's newest version with the inputs saved with the trigger. The job
itself spends nothing; the stages spend what a run spends. Manual removes the job and its script.
"""
from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .workflow_store import HostFacts, WorkflowError, WorkflowStore, open_private

logger = logging.getLogger("hermes.plugins.bighelp")

SCRIPT_PREFIX = "bighelp-workflow-"
MAX_SCHEDULE = 200
_RUN_NAMESPACE = uuid.UUID("6c1d3f0e-7a52-4b0e-9a7e-2f6f3c1b8d41")


def script_name(workflow_id: str) -> str:
    return f"{SCRIPT_PREFIX}{workflow_id}.py"


def _scripts_dir() -> Path:
    from hermes_constants import get_process_hermes_home
    return Path(get_process_hermes_home()) / "scripts"


def _script(workflow_id: str, name: str) -> str:
    plugin_root = str(Path(__file__).resolve().parents[1])
    label = " ".join(name.split())[:80].replace('"', "'")
    return (
        f'# Made by bighelp: starts one run of the workflow "{label}" on its schedule.\n'
        "# Change or stop it in bighelp (Workflows, then this workflow, then Trigger).\n"
        "import sys\n"
        f"sys.path.insert(0, {plugin_root!r})\n"
        "from loopdy_plugin.workflow_trigger import run_scheduled\n"
        f"raise SystemExit(run_scheduled({workflow_id!r}))\n"
    )


def _write_script(workflow_id: str, name: str) -> None:
    directory = _scripts_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / script_name(workflow_id)
    temporary = directory / (script_name(workflow_id) + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    descriptor = open_private(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(_script(workflow_id, name))
    os.replace(temporary, path)


def _remove_script(workflow_id: str) -> None:
    try:
        (_scripts_dir() / script_name(workflow_id)).unlink()
    except FileNotFoundError:
        pass


def _remove_job(job_id: str | None) -> None:
    if not job_id:
        return
    try:
        from cron.jobs import remove_job
        remove_job(job_id)
    except Exception as error:
        logger.warning("bighelp workflows: the old schedule couldn't be removed (%s)", type(error).__name__)


def check_schedule(schedule: Any) -> str:
    if not isinstance(schedule, str) or not schedule.strip() or len(schedule) > MAX_SCHEDULE:
        raise WorkflowError(422, "schedule_invalid", "Choose when it should run.")
    schedule = schedule.strip()
    try:
        from cron.jobs import parse_schedule
        parsed = parse_schedule(schedule)
    except ImportError:
        raise WorkflowError(503, "schedules_unavailable", "This Hermes can't run scheduled jobs.") from None
    except Exception:
        raise WorkflowError(422, "schedule_invalid", "Hermes can't read that schedule.") from None
    if isinstance(parsed, dict) and parsed.get("kind") == "once":
        raise WorkflowError(422, "schedule_invalid", "A workflow schedule repeats. Run it now to run it once.")
    return schedule


def set_trigger(store: WorkflowStore, workflow_id: str, trigger: dict, host: HostFacts | None) -> dict:
    """Saves the trigger and sets up (or removes) its cron job. Returns `{"trigger": …}`."""
    kind = trigger.get("kind")
    old = store.trigger(workflow_id)
    if kind == "manual":
        saved = store.save_trigger(workflow_id, "manual", None, {}, None)
        _remove_job(old.get("jobId"))
        _remove_script(workflow_id)
        return {"trigger": saved}
    if kind != "schedule":
        raise WorkflowError(422, "trigger_invalid", "A trigger is manual or scheduled.")
    schedule = check_schedule(trigger.get("schedule"))
    name, _revision, inputs = store.scheduled_start(workflow_id, trigger.get("inputs") or {}, host)
    try:
        from cron.jobs import create_job
    except ImportError:
        raise WorkflowError(503, "schedules_unavailable", "This Hermes can't run scheduled jobs.") from None
    _write_script(workflow_id, name)
    try:
        job = create_job(prompt=None, schedule=schedule, name=f"Workflow: {' '.join(name.split())[:80]}",
                         script=script_name(workflow_id), no_agent=True, deliver="local")
    except Exception as error:
        if old.get("kind") != "schedule":
            _remove_script(workflow_id)
        logger.warning("bighelp workflows: the schedule couldn't be made (%s)", type(error).__name__)
        raise WorkflowError(422, "schedule_invalid", "Hermes couldn't make that schedule.") from None
    job_id = str(job.get("id") or "") or None
    try:
        saved = store.save_trigger(workflow_id, "schedule", schedule, inputs, job_id)
    except BaseException:
        _remove_job(job_id)
        raise
    # The new job runs from now on; the old one (a changed schedule) goes.
    if old.get("jobId") and old.get("jobId") != job_id:
        _remove_job(old["jobId"])
    return {"trigger": saved}


def run_scheduled(workflow_id: str, *, now: float | None = None) -> int:
    """The cron job's script: start one run. Prints only when it can't (cron keeps that as the job's output)."""
    from .workflow_api import _host_facts, probe, workflows_root
    from .workflow_coordinator import ensure_coordinator
    store = WorkflowStore(workflows_root())
    try:
        trigger = store.trigger(workflow_id)
        if trigger["kind"] != "schedule":
            print("This workflow isn't on a schedule any more.")
            return 0
        reason = probe()
        if reason is not None:
            print(f"Workflows can't run on this computer right now ({reason}).")
            return 1
        host = _host_facts()
        _name, revision, inputs = store.scheduled_start(workflow_id, trigger["inputs"], host)
        minute = int((now if now is not None else time.time()) // 60)
        # One run per scheduled minute, even if the job fires twice.
        token = str(uuid.uuid5(_RUN_NAMESPACE, f"{workflow_id}:{revision}:{minute}"))
        store.start_run(workflow_id, revision, inputs, token, False, host)
    except WorkflowError as error:
        print(f"The scheduled run didn't start: {error.message}")
        return 1
    try:
        ensure_coordinator(store.root)
    except Exception as error:
        print(f"The run is waiting: the workflow runner didn't start ({type(error).__name__}).")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - manual use: python -m loopdy_plugin.workflow_trigger <id>
    raise SystemExit(run_scheduled(sys.argv[1]))
