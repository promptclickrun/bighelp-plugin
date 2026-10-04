"""Workflow routes for the bighelp app: /api/plugins/loopdy/native/workflows/… (docs/WORKFLOWS.md)."""
from __future__ import annotations

import functools
import logging
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt
from starlette.concurrency import run_in_threadpool

from .native_api import _NativeRoute, _body, _precondition, _response
from .native_context import NativeAPIError, PROFILE_ID, native_context
from .workflow_runner import REPORT_ENV
from .workflow_store import HostFacts, Mutation, WorkflowError, WorkflowStore, root_for_home


logger = logging.getLogger(__name__)
CAPABILITY = "native-workflows-v1"
router = APIRouter(prefix="/native/workflows", route_class=_NativeRoute)
_PROBE_SECONDS = 30.0
_probe_cache: dict[str, tuple[float, str | None]] = {}
_WORKFLOW = r"^wf_[0-9a-f]{16}$"
_RUN = r"^run_[0-9a-f]{16}$"
_KEY = r"^[a-z][a-z0-9_]{0,31}$"
_SHA = r"^[0-9a-f]{64}$"
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


# MARK: Availability

def workflows_root() -> Path:
    from hermes_constants import get_default_hermes_root, get_process_hermes_home
    return root_for_home(Path(get_default_hermes_root(home=get_process_hermes_home())))


@functools.lru_cache(maxsize=1)
def _chat_flags_present() -> bool:
    """The worker command needs these Hermes chat flags (Hermes 0.21.4 and later)."""
    try:
        from hermes_cli._parser import build_top_level_parser
        parser, _subparsers, chat = build_top_level_parser()
    except Exception:
        return False
    top = {option for action in parser._actions for option in action.option_strings}
    flags = {option for action in chat._actions for option in action.option_strings}
    # `-p` is read before argparse (Hermes' PRE_ARGPARSE_INHERITED_FLAGS), so it isn't in either parser.
    return "--cli" in top | flags and {"--query-file", "--format", "--source", "--toolsets"} <= flags


def probe() -> str | None:
    """None when workflows can run on this computer, else a fixed reason code."""
    from .workflow_coordinator import service_manager
    if os.name != "posix":
        return "not_posix"
    if service_manager() is None:
        return "service_manager_missing"
    try:
        from hermes_cli.quiet_single_query import TURN_REPORT_FILE_ENV
    except ImportError:
        return "hermes_update_needed"
    if TURN_REPORT_FILE_ENV != REPORT_ENV or not _chat_flags_present():
        return "hermes_update_needed"
    try:
        from hermes_cli.profiles import profile_exists  # noqa: F401
        from hermes_constants import get_default_hermes_root, get_process_hermes_home  # noqa: F401
    except ImportError:
        return "profile_helpers_missing"
    try:
        root = workflows_root()
        cached = _probe_cache.get(str(root))
        if cached is not None and time.monotonic() - cached[0] < _PROBE_SECONDS:
            return cached[1]
        WorkflowStore(root).status()
        _probe_cache[str(root)] = (time.monotonic(), None)
    except Exception:
        return "store_unavailable"
    return None


def available() -> bool:
    return probe() is None


def host_name() -> str:
    name = socket.gethostname().split(".")[0].strip()
    return name[:64] or "Hermes"


def _host_facts() -> HostFacts:
    from hermes_cli.profiles import profile_exists
    known: Callable[[str], bool] | None = None
    try:
        from toolsets import validate_toolset
        known = validate_toolset
    except Exception:
        known = None
    return HostFacts(profile_exists=profile_exists, toolset_known=known)


def ensure_on_start() -> None:
    """From plugin registration: restart the coordinator after a reboot when runs are waiting for it."""
    def start() -> None:
        try:
            from .workflow_coordinator import ensure_coordinator
            ensure_coordinator(workflows_root())
        except Exception as error:
            logger.debug("bighelp workflows: coordinator check skipped (%s)", type(error).__name__)
    threading.Thread(target=start, name="bighelp-workflows-ensure", daemon=True).start()


# MARK: Bodies

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Empty(_Strict):
    pass


class _List(_Strict):
    includeArchived: StrictBool = False


class _WorkflowBody(_Strict):
    workflowId: str = Field(pattern=_WORKFLOW)


class _Get(_WorkflowBody):
    revision: StrictInt | Literal["draft"] = "draft"


class _Save(_Strict):
    workflowId: str | None = Field(default=None, pattern=_WORKFLOW)
    baseDraftVersion: StrictInt = Field(ge=0, le=2**53)
    definition: dict[str, Any]


class _Publish(_WorkflowBody):
    draftVersion: StrictInt = Field(ge=1, le=2**53)


class _Bind(_WorkflowBody):
    role: str = Field(pattern=_KEY)
    agentId: str | None = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")


class _Start(_WorkflowBody):
    revision: StrictInt = Field(ge=1, le=2**31)
    inputs: dict[str, Any]
    clientRunToken: str = Field(pattern=_UUID)
    sample: StrictBool = False


class _Runs(_Strict):
    workflowId: str | None = Field(default=None, pattern=_WORKFLOW)
    filter: Literal["all", "active", "for_you", "attention"] = "all"
    before: str | None = Field(default=None, pattern=r"^[0-9]{1,18}$")
    limit: StrictInt = Field(default=20, ge=1, le=50)


class _RunBody(_Strict):
    runId: str = Field(pattern=_RUN)


class _Events(_RunBody):
    after: StrictInt = Field(ge=0, le=2**62)
    limit: StrictInt = Field(default=100, ge=1, le=200)


class _Control(_RunBody):
    action: Literal["pause", "resume", "cancel", "retry"]
    expectedVersion: StrictInt = Field(ge=1, le=2**62)


class _Signoff(_RunBody):
    stageKey: str = Field(pattern=_KEY)
    decision: Literal["approve", "changes"]
    artifactSha256: str = Field(pattern=_SHA)
    notes: str = Field(default="", max_length=2000)


class _Read(_RunBody):
    sha256: str = Field(pattern=_SHA)
    offset: StrictInt = Field(ge=0, le=2**40)
    length: StrictInt = Field(ge=1, le=98_304)


class _Use(_Strict):
    templateId: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    name: str | None = Field(default=None, min_length=1, max_length=80)


# MARK: Operations

def _status(store: WorkflowStore, body: _Empty, mutation: Mutation) -> dict:
    from .workflow_coordinator import ensure_coordinator
    ensure_coordinator(store.root)
    value = store.status()
    reason = probe()
    value.update({"survivesAppClose": True,
                  "runner": {"available": True} if reason is None else {"available": False, "reason": reason},
                  "hostName": host_name()})
    return value


def _list(store, body: _List, mutation):
    return store.list_workflows(body.includeArchived, _host_facts())


def _get(store, body: _Get, mutation):
    return store.get_workflow(body.workflowId, body.revision, _host_facts())


def _save(store, body: _Save, mutation):
    return store.save_draft(body.workflowId, body.baseDraftVersion, body.definition, _host_facts(), mutation)


def _validate(store, body: _WorkflowBody, mutation):
    return store.validate(body.workflowId, _host_facts())


def _publish(store, body: _Publish, mutation):
    return store.publish(body.workflowId, body.draftVersion, _host_facts(), mutation)


def _bind(store, body: _Bind, mutation):
    if body.agentId is not None:
        from hermes_cli.profiles import profile_exists
        if PROFILE_ID.fullmatch(body.agentId) is None or not profile_exists(body.agentId):
            raise WorkflowError(404, "profile_not_found", "The selected agent no longer exists.")
    return store.bind(body.workflowId, body.role, body.agentId, mutation)


def _archive(store, body: _WorkflowBody, mutation):
    return store.archive(body.workflowId, mutation)


def _start(store, body: _Start, mutation):
    if probe() is not None:
        raise WorkflowError(503, "runner_unavailable", "This computer can't run workflow stages right now.")
    return store.start_run(body.workflowId, body.revision, body.inputs, body.clientRunToken, body.sample,
                           _host_facts(), mutation)


def _runs(store, body: _Runs, mutation):
    return store.list_runs(body.workflowId, body.filter, body.before, body.limit)


def _run(store, body: _RunBody, mutation):
    return store.get_run(body.runId)


def _events(store, body: _Events, mutation):
    return store.events(body.runId, body.after, body.limit)


def _control(store, body: _Control, mutation):
    return store.control(body.runId, body.action, body.expectedVersion, mutation)


def _signoff(store, body: _Signoff, mutation):
    return store.signoff(body.runId, body.stageKey, body.decision, body.artifactSha256, body.notes, mutation)


def _read(store, body: _Read, mutation):
    return store.read_artifact(body.runId, body.sha256, body.offset, body.length)


def _templates(store, body: _Empty, mutation):
    from .workflow_model import template_summaries
    return {"templates": template_summaries()}


def _use(store, body: _Use, mutation):
    return store.use_template(body.templateId, body.name, _host_facts(), mutation)


# path: (body, operation, starts work)
_ROUTES: dict[str, tuple[type[BaseModel], Callable[..., dict], bool]] = {
    "status": (_Empty, _status, False),
    "list": (_List, _list, False),
    "get": (_Get, _get, False),
    "draft/save": (_Save, _save, False),
    "validate": (_WorkflowBody, _validate, False),
    "publish": (_Publish, _publish, False),
    "bind": (_Bind, _bind, False),
    "archive": (_WorkflowBody, _archive, False),
    "runs/start": (_Start, _start, True),
    "runs/list": (_Runs, _runs, False),
    "runs/get": (_RunBody, _run, False),
    "runs/events": (_Events, _events, False),
    "runs/control": (_Control, _control, True),
    "runs/signoff": (_Signoff, _signoff, True),
    "artifacts/read": (_Read, _read, False),
    "templates/list": (_Empty, _templates, False),
    "templates/use": (_Use, _use, False),
}


def _execute(request: Request, owner: Any, operation: Callable[..., dict], body: BaseModel, request_id: str,
             starts_work: bool) -> dict:
    from hermes_constants import get_process_hermes_home, reset_hermes_home_override, set_hermes_home_override

    def check() -> None:
        if native_context(request) != owner:
            raise NativeAPIError(412, "context_changed", "The native context changed; reconcile the outcome.")

    check()
    token = set_hermes_home_override(get_process_hermes_home())
    try:
        store = WorkflowStore(workflows_root())
        try:
            result = operation(store, body, Mutation(request_id=request_id, check=check))
        except WorkflowError as error:
            logger.info("bighelp workflows request refused: %s", error.code)
            raise NativeAPIError(error.status, error.code, error.message) from None
        if starts_work:
            try:
                from .workflow_coordinator import ensure_coordinator
                ensure_coordinator(store.root)
            except Exception as error:
                logger.warning("bighelp workflows: coordinator start failed (%s)", type(error).__name__)
        return result
    finally:
        reset_hermes_home_override(token)


def _handler(path: str):
    model, operation, starts_work = _ROUTES[path]

    async def handle(request: Request) -> Response:
        owner = native_context(request)
        request_id = _precondition(request, owner)
        if CAPABILITY not in owner.features:
            raise NativeAPIError(503, "workflows_unavailable", "Workflows aren't available on this computer.")
        body = await _body(request, model)
        result = await run_in_threadpool(_execute, request, owner, operation, body, request_id, starts_work)
        if native_context(request) != owner:
            raise NativeAPIError(412, "context_changed", "The native context changed; reconcile the outcome.")
        return _response(result, owner, request_id)
    return handle


for _path in _ROUTES:
    router.add_api_route("/" + _path, _handler(_path), methods=["POST"],
                         name="native_workflows_" + _path.replace("/", "_"))
