"""Finite stock-serve HTTP adapters for existing bighelp domain services."""
from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager
import json
import logging
import re
import sys
from typing import Annotated, Any, Literal, TypeVar

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from .native_context import NativeAPIError, NativeContext, PROFILE_ID, log_native_feature_startup, native_context
from .store import CardTemplateConflict, CardTemplateLimit


logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 196_608
MAX_TEMPLATES = 500
_REQUEST_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_BodyT = TypeVar("_BodyT", bound=BaseModel)


def _error_response(error: NativeAPIError) -> JSONResponse:
    logger.warning("bighelp native request rejected: %s", error.code)
    return JSONResponse(status_code=error.status, content={"error": {
        "code": error.code, "message": error.message,
        "retryable": error.status == 503, "details": getattr(error, "details", None) or {},
    }}, headers={"Cache-Control": "no-store"})


def _report_error(status: int, code: str) -> None:
    """Host telemetry hears of a failure on the host's side (5xx) by its fixed code. Nothing else."""
    if status >= 500:
        try:
            from . import host_telemetry
            host_telemetry.error(code=code, stage="native_route")
        except Exception:
            pass


class _NativeRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            try:
                return await handler(request)
            except NativeAPIError as error:
                _report_error(error.status, error.code)
                response = _error_response(error)
                request_id = request.headers.get("x-loopdy-request-id", "")
                if len(request.headers.getlist("x-loopdy-request-id")) == 1 and _REQUEST_ID.fullmatch(request_id):
                    response.headers["X-Loopdy-Request-ID"] = request_id
                return response
            except ClientDisconnect:
                return _error_response(NativeAPIError(400, "request_disconnected",
                                                     "The request was disconnected."))
            except Exception as error:
                # Auth providers and storage may include secrets/paths in exception
                # text; the class name is fixed and safe to log.
                logger.error("bighelp native request failed: native_service_unavailable (%s)",
                             type(error).__name__)
                _report_error(503, "native_service_unavailable")
                return _error_response(NativeAPIError(503, "native_service_unavailable",
                                                     "The native plugin service is unavailable."))
        return guarded


@asynccontextmanager
async def _native_lifespan(app):
    # Hermes mounts this router separately from its profile-scoped plugin
    # registration module. Log the inventory from the namespace serving HTTP.
    log_native_feature_startup()
    from .host_restart import release_locks_left_by_restart
    release_locks_left_by_restart()
    yield


router = APIRouter(prefix="/native", route_class=_NativeRoute, lifespan=_native_lifespan)


@router.post("/voice/{operation}")
async def voice(operation: str, request: Request) -> Response:
    from .native_voice import request as voice_request
    return await voice_request(operation, request)


@router.post("/device-tools/{operation}")
async def device_tools(operation: str, request: Request) -> Response:
    from . import native_device_tools
    # The dashboard API is imported under ``loopdy_plugin`` while Hermes loads
    # the plugin under its profile-safe ``hermes_plugins.<slug>`` namespace.
    # Dispatch into the loader-owned module so HTTP and middleware share one
    # hub, and keep the profile boundary explicit for multiplexed dashboards.
    owner = native_context(request)
    implementation = native_device_tools._implementation_for_profile(owner.serving_profile_id)
    if implementation is not None and implementation is not native_device_tools:
        # The dashboard router is mounted from the bare plugin package while
        # Hermes middleware owns a profile-scoped package.  Reuse the bare
        # request's authenticated context and ETag for the scoped hub; asking
        # the scoped package to resolve context again would create a different
        # module runtime ID and reject every phone request as context_changed.
        return await implementation.request(
            operation, request, owner=owner, auth_module=sys.modules[__name__])
    return await native_device_tools.request(operation, request)


class _HostRestart(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    confirm: Literal[True]


@router.post("/host/restart")
async def host_restart(request: Request) -> Response:
    """Restart this Hermes process in place so an updated plugin loads (bighelp app Update)."""
    from .host_restart import CAPABILITY, schedule
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "host_restart_unavailable", "This host can't restart from the app.")
    await _body(request, _HostRestart)
    try:
        value = schedule()
    except RuntimeError:
        raise NativeAPIError(503, "host_restart_unavailable", "This host can't restart from the app.") from None
    return _response({**value, "runtimeId": owner.runtime_id}, owner, request_id)


@router.post("/attachments/{operation}")
async def agent_attachments(operation: str, request: Request) -> Response:
    from .native_attachments import request as attachments_request
    return await attachments_request(operation, request, auth_module=sys.modules[__name__])


@router.post("/workspace-files/{operation}")
async def workspace_files(operation: str, request: Request) -> Response:
    from .workspace_artifacts import request as workspace_files_request
    return await workspace_files_request(operation, request)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agentId: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")


class _TemplateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    version: StrictInt = Field(gt=0, le=9_007_199_254_740_991)
    name: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1_000)
    author: str = Field(min_length=1, max_length=120)
    license: str = Field(min_length=1, max_length=120)
    minimum_card_version: StrictInt = Field(ge=1, le=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _TemplateBundle(_TemplateSummary):
    parameters_schema: dict[str, Any]
    document: dict[str, Any]


class _Install(_Body):
    template: _TemplateBundle


class _Remove(_Body):
    templateId: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    version: StrictInt = Field(gt=0, le=9_007_199_254_740_991)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _invalid_number(_value):
    raise ValueError("Non-finite JSON number")


async def _body(request: Request, model: type[_BodyT]) -> _BodyT:
    if request.query_params:
        raise NativeAPIError(422, "invalid_request", "Query fields are not supported.")
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise NativeAPIError(422, "invalid_request", "A JSON request is required.")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_BODY_BYTES:
            raise NativeAPIError(413, "payload_too_large", "The request exceeds the byte limit.")
        content.extend(chunk)
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_invalid_number)
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 24:
                raise ValueError("JSON nesting limit")
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return model.model_validate(value)
    except (ValueError, UnicodeError, RecursionError, ValidationError):
        raise NativeAPIError(422, "invalid_request", "The native request is invalid.") from None


def _headers(context: NativeContext, request_id: str | None = None) -> dict[str, str]:
    result = {"Cache-Control": "no-store", "ETag": context.etag}
    if request_id is not None:
        result["X-Loopdy-Request-ID"] = request_id
    return result


def _response(value: dict, context: NativeContext, request_id: str | None = None,
              maximum: int | None = None) -> Response:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=True, allow_nan=False).encode("utf-8")
    maximum = maximum or (16_384 if request_id is None else MAX_BODY_BYTES)
    if len(encoded) > maximum:
        raise NativeAPIError(413, "payload_too_large", "The response exceeds the byte limit.")
    return Response(encoded, media_type="application/json", headers=_headers(context, request_id))


def _precondition(request: Request, context: NativeContext) -> str:
    if request.headers.get("if-match") is None:
        raise NativeAPIError(428, "context_required", "Load native context before this request.")
    if len(request.headers.getlist("if-match")) != 1 or request.headers["if-match"] != context.etag:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    request_id = request.headers.get("x-loopdy-request-id", "")
    if len(request.headers.getlist("x-loopdy-request-id")) != 1 or _REQUEST_ID.fullmatch(request_id) is None:
        raise NativeAPIError(422, "invalid_request", "A canonical request ID is required.")
    return request_id


# Set once this process has reported the plugin bytes it runs (`_record_serving_runtime`).
_runtime_recorded = False


def _record_serving_runtime(profile: str | None) -> None:
    """The first app request in a process proves it runs these plugin bytes. A plugin update started
    with `hermes bighelp update` completes when the restarted server loads them; hosts without the
    old push gateway never report it any other way."""
    global _runtime_recorded
    if _runtime_recorded or not profile:
        return
    _runtime_recorded = True
    try:
        from .plugin_update import production_manager
        production_manager(profile).record_runtime_loaded()
    except Exception:  # update status can fail closed; the app's connection never does
        logger.warning("bighelp plugin update status unavailable")


@router.get("/context")
async def context(request: Request) -> Response:
    owner = native_context(request)
    _record_serving_runtime(owner.serving_profile_id)
    # Each app connection starts here, in the process that serves its chats.
    from . import durable_sessions, plugin_links
    try:
        durable_sessions.install()
    except Exception:  # never let it fail the context
        logger.warning("Durable bighelp sessions unavailable")
    try:
        plugin_links.refresh_in_background()
    except Exception:
        logger.warning("bighelp plugin links unavailable")
    if request.query_params:
        raise NativeAPIError(422, "invalid_request", "Native context takes no query or body.")
    async for chunk in request.stream():
        if chunk:
            raise NativeAPIError(422, "invalid_request", "Native context takes no query or body.")
    return _response(owner.payload(), owner)


class _TelemetryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: StrictBool


async def _telemetry_request(request: Request) -> tuple[NativeContext, str]:
    from .host_telemetry import CAPABILITY
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "host_telemetry_unavailable", "Host telemetry isn't available on this host.")
    return owner, request_id


@router.get("/telemetry")
async def host_telemetry_state(request: Request) -> Response:
    """Whether this host sends usage numbers (bighelp app: Settings > Permissions > Device access)."""
    from . import host_telemetry
    owner, request_id = await _telemetry_request(request)
    if request.query_params:
        raise NativeAPIError(422, "invalid_request", "Host telemetry takes no query or body.")
    async for chunk in request.stream():
        if chunk:
            raise NativeAPIError(422, "invalid_request", "Host telemetry takes no query or body.")
    return _response(await run_in_threadpool(host_telemetry.status), owner, request_id)


@router.put("/telemetry")
async def host_telemetry_set(request: Request) -> Response:
    """The app's own switch. It can turn telemetry off or back on; env and config settings still win."""
    from . import host_telemetry
    owner, request_id = await _telemetry_request(request)
    body = await _body(request, _TelemetryBody)
    try:
        value = await run_in_threadpool(host_telemetry.set_app_enabled, body.enabled)
    except OSError:
        raise NativeAPIError(503, "host_telemetry_unavailable", "Host telemetry can't be saved on this host.") from None
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response(value, owner, request_id)


_TEMPLATE_SUMMARY_KEYS = (
    "id", "version", "name", "summary", "author", "license", "minimum_card_version", "sha256",
)


def _summary(template: dict) -> dict:
    if not isinstance(template, dict) or any(key not in template for key in _TEMPLATE_SUMMARY_KEYS):
        raise ValueError("Card template projection is invalid")
    return _TemplateSummary.model_validate({key: template[key] for key in _TEMPLATE_SUMMARY_KEYS}).model_dump()


def _templates(request: Request, owner: NativeContext, body: _Body, operation: str) -> dict:
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    from hermes_cli.profiles import profile_exists
    from hermes_constants import (
        get_process_hermes_home, set_hermes_home_override, reset_hermes_home_override,
    )
    from .adapter import get_service

    token = set_hermes_home_override(get_process_hermes_home())
    try:
        if not profile_exists(body.agentId):
            raise NativeAPIError(404, "profile_not_found", "The selected profile no longer exists.")
        store = get_service().store
        try:
            if operation == "list":
                templates = store.list_card_templates(profile=body.agentId, limit=MAX_TEMPLATES)
                result = {"templates": [_summary(item) for item in templates]}
            elif operation == "install" and isinstance(body, _Install):
                installed = store.install_card_template(profile=body.agentId, template=body.template.model_dump())
                result = {"changed": installed["changed"],
                          "template": _summary(installed["template"])}
            elif operation == "remove" and isinstance(body, _Remove):
                result = store.remove_card_template(profile=body.agentId, template_id=body.templateId,
                                                    version=body.version, sha256=body.sha256)
            else:
                raise NativeAPIError(422, "invalid_request", "The template request is invalid.")
        except CardTemplateConflict:
            raise NativeAPIError(409, "template_conflict", "The template version or content changed.") from None
        except CardTemplateLimit:
            raise NativeAPIError(413, "catalog_too_large", "The template catalog exceeds the row limit.") from None
        except ValueError:
            raise NativeAPIError(422, "invalid_template", "The template data is invalid.") from None
        if native_context(request) != owner:
            raise NativeAPIError(412, "context_changed", "The native context changed; reconcile the outcome.")
        return {"agentId": body.agentId, **result}
    finally:
        reset_hermes_home_override(token)


async def _template_request(request: Request, model: type[_Body], operation: str) -> Response:
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if "native-card-templates-v1" not in owner.features:
        raise NativeAPIError(503, "templates_unavailable", "Native card templates are unavailable.")
    body = await _body(request, model)
    if PROFILE_ID.fullmatch(body.agentId) is None:
        raise NativeAPIError(422, "invalid_request", "The profile is invalid.")
    result = await run_in_threadpool(_templates, request, owner, body, operation)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; reconcile the outcome.")
    return _response(result, owner, request_id)


@router.post("/cards/templates/list")
async def list_templates(request: Request) -> Response:
    return await _template_request(request, _Body, "list")


@router.post("/cards/templates/install")
async def install_template(request: Request) -> Response:
    return await _template_request(request, _Install, "install")


@router.post("/cards/templates/remove")
async def remove_template(request: Request) -> Response:
    return await _template_request(request, _Remove, "remove")


# MARK: Agent board (Feed, Ideas, Goals, Activity, Approvals history)

_BOARD_LIST_BYTES = 2 * 1024 * 1024
_BOARD_MEDIA_BYTES = 12 * 1024 * 1024
_BOARD_ITEM_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"


class _BoardList(_Body):
    kinds: list[Literal["feed", "idea", "goal"]] = Field(default_factory=lambda: ["feed", "idea", "goal"],
                                                         min_length=1, max_length=3)
    limit: StrictInt = Field(default=100, ge=1, le=200)
    includeDismissed: StrictBool = False


class _BoardUpdate(_Body):
    itemId: str = Field(pattern=_BOARD_ITEM_ID)
    liked: StrictBool | None = None
    dismissed: StrictBool | None = None
    status: Literal["active", "done"] | None = None
    rating: Literal["up", "down", "none"] | None = None
    reason: str | None = Field(default=None, max_length=120)
    read: StrictBool | None = None


class _BoardRead(_Body):
    itemIds: list[Annotated[str, Field(pattern=_BOARD_ITEM_ID)]] = Field(min_length=1, max_length=200)
    read: StrictBool = True


class _BoardItem(_Body):
    itemId: str = Field(pattern=_BOARD_ITEM_ID)


class _BoardMedia(_Body):
    itemId: str = Field(pattern=_BOARD_ITEM_ID)
    index: StrictInt = Field(ge=0, le=5)


class _BoardLog(_Body):
    limit: StrictInt = Field(default=100, ge=1, le=200)


def _board_response(value: dict, context: NativeContext, request_id: str, maximum: int) -> Response:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=True, allow_nan=False).encode("utf-8")
    if len(encoded) > maximum:
        raise NativeAPIError(413, "payload_too_large", "The response exceeds the byte limit.")
    return Response(encoded, media_type="application/json", headers=_headers(context, request_id))


def _board(request: Request, owner: NativeContext, body: _Body, operation: str) -> dict:
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    from hermes_cli.profiles import profile_exists
    from .agent_board import BoardError, identity, media_payload, session_titles, store_for_profile

    if not profile_exists(body.agentId):
        raise NativeAPIError(404, "profile_not_found", "The selected profile no longer exists.")
    store = store_for_profile(body.agentId)
    try:
        if operation == "list" and isinstance(body, _BoardList):
            result = {"items": store.items(tuple(body.kinds), limit=body.limit,
                                           include_dismissed=body.includeDismissed)}
        elif operation == "update" and isinstance(body, _BoardUpdate):
            result = {"item": store.set_flags(body.itemId, liked=body.liked, dismissed=body.dismissed,
                                              status=body.status, rating=body.rating, reason=body.reason,
                                              read=body.read)}
        elif operation == "read" and isinstance(body, _BoardRead):
            result = {"updated": store.mark_read(body.itemIds, read=body.read)}
        elif operation == "promote" and isinstance(body, _BoardItem):
            result = {"item": store.promote_idea(body.itemId)}
        elif operation == "accept" and isinstance(body, _BoardItem):
            result = {"item": store.accept_idea(body.itemId)}
        elif operation == "media" and isinstance(body, _BoardMedia):
            mime, data = store.image(body.itemId, body.index)
            result = media_payload(mime, data)
        elif operation == "activity" and isinstance(body, _BoardLog):
            rows = store.activity(body.limit)
            titles = session_titles(body.agentId, [row["sessionId"] for row in rows])
            result = {"activity": [{**row, "title": titles.get(row["sessionId"], "")} for row in rows]}
        elif operation == "identity":
            result = identity(body.agentId)
        elif operation == "approvals" and isinstance(body, _BoardLog):
            rows = store.approvals(body.limit)
            titles = session_titles(body.agentId, [row["sessionId"] for row in rows])
            result = {"approvals": [{**row, "sessionTitle": titles.get(row["sessionId"], "")} for row in rows]}
        else:
            raise NativeAPIError(422, "invalid_request", "The board request is invalid.")
    except BoardError:
        raise NativeAPIError(404, "board_item_unavailable", "That item is no longer available.") from None
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; reconcile the outcome.")
    return {"agentId": body.agentId, **result}


async def _board_request(request: Request, model: type[_Body], operation: str, maximum: int,
                         feature: str | None = None) -> Response:
    from .agent_board import CAPABILITY
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features or (feature is not None and feature not in owner.features):
        raise NativeAPIError(503, "board_unavailable", "The agent board is unavailable.")
    body = await _body(request, model)
    result = await run_in_threadpool(_board, request, owner, body, operation)
    return _board_response(result, owner, request_id, maximum)


@router.post("/board/list")
async def board_list(request: Request) -> Response:
    return await _board_request(request, _BoardList, "list", _BOARD_LIST_BYTES)


@router.post("/board/update")
async def board_update(request: Request) -> Response:
    return await _board_request(request, _BoardUpdate, "update", MAX_BODY_BYTES)


@router.post("/board/read")
async def board_read(request: Request) -> Response:
    return await _board_request(request, _BoardRead, "read", MAX_BODY_BYTES)


@router.post("/board/promote")
async def board_promote(request: Request) -> Response:
    return await _board_request(request, _BoardItem, "promote", MAX_BODY_BYTES)


@router.post("/board/accept")
async def board_accept(request: Request) -> Response:
    from .agent_board import ANSWERS_CAPABILITY
    return await _board_request(request, _BoardItem, "accept", MAX_BODY_BYTES, ANSWERS_CAPABILITY)


@router.post("/board/media")
async def board_media(request: Request) -> Response:
    return await _board_request(request, _BoardMedia, "media", _BOARD_MEDIA_BYTES)


@router.post("/board/activity")
async def board_activity(request: Request) -> Response:
    return await _board_request(request, _BoardLog, "activity", _BOARD_LIST_BYTES)


@router.post("/board/approvals")
async def board_approvals(request: Request) -> Response:
    return await _board_request(request, _BoardLog, "approvals", _BOARD_LIST_BYTES)


@router.post("/board/identity")
async def board_identity(request: Request) -> Response:
    return await _board_request(request, _Body, "identity", _BOARD_LIST_BYTES)


class _Speaking(_Body):
    sessionId: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    personId: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    name: str = Field(default="", max_length=400)
    # The card components this chat's app draws (native-card-objects-v1). An older
    # app sends none, which means the basic components only; names this plugin
    # doesn't know yet are ignored.
    renders: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")]] | None = Field(default=None, max_length=8)


def _speaking(agent_id: str, body: _Speaking) -> dict:
    from hermes_cli.profiles import profile_exists
    from .people import clean_name, store_for_profile
    if not profile_exists(agent_id):
        raise NativeAPIError(404, "profile_not_found", "The selected profile no longer exists.")
    store = store_for_profile(agent_id)
    store.note(body.sessionId, body.personId, body.name)
    renders = store.set_renders(body.sessionId, body.renders or ())
    value = {"agentId": agent_id, "sessionId": body.sessionId, "name": clean_name(body.name)}
    # Echoed only to an app that sent the list, so older apps get the answer they always got.
    return value if body.renders is None else {**value, "renders": renders}


@router.post("/people/speaking")
async def people_speaking(request: Request) -> Response:
    """The bighelp app is about to send a message in this chat for this person."""
    from .people import CAPABILITY
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "people_unavailable", "Names aren't available on this host.")
    body = await _body(request, _Speaking)
    value = await run_in_threadpool(_speaking, body.agentId, body)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response(value, owner, request_id)


class _AgentTalkCall(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agentId: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    message: str = Field(pattern=r"^[0-9a-f]{64}$")


class _AgentTalk(_Body):
    sessionId: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    calls: list[_AgentTalkCall] = Field(max_length=16)


def _agent_talk(body: _AgentTalk) -> dict:
    from hermes_cli.profiles import profile_exists
    from .agent_talk import find_runs
    if not profile_exists(body.agentId):
        raise NativeAPIError(404, "profile_not_found", "The selected profile no longer exists.")
    return {"runs": find_runs((call.agentId, call.message) for call in body.calls)}


@router.post("/agents/talk")
async def agents_talk(request: Request) -> Response:
    """What the agents this chat's agent asked are doing: IDs, times and a tool name, read-only."""
    from .agent_talk import CAPABILITY
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "agent_talk_unavailable", "Agent conversations aren't available on this host.")
    body = await _body(request, _AgentTalk)
    value = await run_in_threadpool(_agent_talk, body)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response(value, owner, request_id)


def _share_plugins(body: _Body) -> dict:
    from hermes_cli.profiles import profile_exists
    from .plugin_links import link_agent
    if not profile_exists(body.agentId):
        raise NativeAPIError(404, "profile_not_found", "The selected profile no longer exists.")
    try:
        return link_agent(body.agentId)
    except TimeoutError:
        raise NativeAPIError(409, "plugins_busy", "A plugin install is running for this agent; try again.") from None


@router.post("/agents/plugins/share")
async def agents_plugins_share(request: Request) -> Response:
    """An agent cloned from the default one shares its plugins instead of keeping copies of them."""
    from .plugin_links import CAPABILITY
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "plugin_links_unavailable", "Shared plugins aren't available on this host.")
    body = await _body(request, _Body)
    value = await run_in_threadpool(_share_plugins, body)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response(value, owner, request_id)


class _ChatActivity(_Body):
    sessionId: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _chat_activity(body: _ChatActivity, stopping: bool) -> dict:
    from hermes_cli.profiles import profile_exists
    from . import chat_activity
    if not profile_exists(body.agentId):
        raise NativeAPIError(404, "profile_not_found", "The selected profile no longer exists.")
    if stopping:
        return {"stopped": chat_activity.stop(body.agentId, body.sessionId)}
    return chat_activity.activity(body.agentId, body.sessionId)


async def _chat_activity_route(request: Request, stopping: bool) -> Response:
    from .chat_activity import CAPABILITY
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "chat_activity_unavailable", "Chat activity isn't available on this host.")
    body = await _body(request, _ChatActivity)
    value = await run_in_threadpool(_chat_activity, body, stopping)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response(value, owner, request_id)


@router.post("/chat/activity")
async def chat_activity(request: Request) -> Response:
    """Whether another Hermes process is running this chat now (an agent another one asked), read-only."""
    return await _chat_activity_route(request, stopping=False)


@router.post("/chat/stop")
async def chat_stop(request: Request) -> Response:
    """Stops the run another Hermes process has on this chat, as Ctrl-C would."""
    return await _chat_activity_route(request, stopping=True)


class _AgentsActivity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agentIds: list[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]] = Field(min_length=1, max_length=64)


def _agents_activity(body: _AgentsActivity) -> dict:
    from .agents_activity import activity
    return {"agents": activity(body.agentIds)}


@router.post("/agents/activity")
async def agents_activity(request: Request) -> Response:
    """What each agent's Bot Chat is doing now (working, waiting on the person, idle), read-only."""
    from .agents_activity import CAPABILITY
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "agents_activity_unavailable", "Agent activity isn't available on this host.")
    body = await _body(request, _AgentsActivity)
    value = await run_in_threadpool(_agents_activity, body)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response(value, owner, request_id, maximum=32_768)


class _Team(_Body):
    enabled: StrictBool | None = None


@router.post("/team")
async def team_settings(request: Request) -> Response:
    """Teamwork: whether every agent gets the team brief, and who's on the team."""
    from .team import CAPABILITY, team
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "team_unavailable", "Teamwork isn't available on this host.")
    body = await _body(request, _Team)
    value = await run_in_threadpool(team().set_enabled, body.enabled)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response(value, owner, request_id, maximum=65_536)


def _live_alerts_call(operation, *args, **kwargs):
    from .live_alerts import LiveAlertError
    from .managed_notifications import ManagedNotificationError
    try:
        return operation(*args, **kwargs)
    except LiveAlertError as error:
        raise NativeAPIError(error.status, error.code, error.message) from None
    except ManagedNotificationError as error:
        # The app only hears "unavailable"; the host owner needs the fixed cause (a folder anyone else
        # can open, a key that isn't valid), so it goes to the log and to host telemetry.
        logger.warning("bighelp live alerts unavailable: %s", error.code)
        _report_error(503, error.code)
        raise NativeAPIError(503, "live_alerts_unavailable", "Instant alerts are unavailable.") from None


async def _live_alerts_request(request: Request, model):
    from . import live_alerts
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if live_alerts.CAPABILITY not in owner.features:
        raise NativeAPIError(503, "live_alerts_unavailable", "Instant alerts are unavailable.")
    body = await _body(request, model)
    from .managed_notifications import get_process_notifications
    service = await run_in_threadpool(_live_alerts_call, get_process_notifications)
    return owner, request_id, body, service


@router.post("/alerts/listen")
async def live_alerts_listen(request: Request) -> Response:
    """bighelp is open on this device: hold until an alert for it is there (or the wait ends)."""
    from . import live_alerts
    owner, request_id, body, service = await _live_alerts_request(request, live_alerts.ListenBody)
    value = await live_alerts.listen(
        body, service=service, is_disconnected=request.is_disconnected,
        run=lambda operation, *args, **kwargs: run_in_threadpool(_live_alerts_call, operation, *args, **kwargs))
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response(value, owner, request_id, maximum=live_alerts.MAX_RESPONSE_BYTES + 16_384)


@router.post("/alerts/stop")
async def live_alerts_stop(request: Request) -> Response:
    """bighelp went to the background on this device: push its alerts again right away."""
    from . import live_alerts
    owner, request_id, body, service = await _live_alerts_request(request, live_alerts.StopBody)
    await run_in_threadpool(_live_alerts_call, live_alerts.stop, service, body)
    return _response({"stopped": True}, owner, request_id)


@router.post("/alerts/ack")
async def live_alerts_ack(request: Request) -> Response:
    """The device showed these alerts, or chose not to: they never go out as pushes."""
    from . import live_alerts
    owner, request_id, body, service = await _live_alerts_request(request, live_alerts.AckBody)
    settled = await run_in_threadpool(_live_alerts_call, live_alerts.ack, service, body)
    return _response({"settled": settled}, owner, request_id)


class _QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grantId: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    enabled: StrictBool
    startMinute: StrictInt = Field(ge=0, le=1439)
    endMinute: StrictInt = Field(ge=0, le=1439)
    timeZone: str = Field(min_length=1, max_length=64)


def _quiet_hours(body: _QuietHours) -> dict:
    from .managed_notifications import ManagedNotificationError, get_process_notifications
    try:
        return get_process_notifications().set_quiet_hours(
            body.grantId, enabled=body.enabled, start_minute=body.startMinute,
            end_minute=body.endMinute, time_zone=body.timeZone)
    except ManagedNotificationError as error:
        raise NativeAPIError(error.status, error.code, "The Quiet Hours request was refused.") from None


@router.post("/notifications/quiet-hours")
async def notification_quiet_hours(request: Request) -> Response:
    """This device's Quiet Hours for its notification grant (bighelp app: Settings › Notifications)."""
    from .quiet_hours import CAPABILITY
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "quiet_hours_unavailable", "Quiet Hours aren't available on this host.")
    body = await _body(request, _QuietHours)
    value = await run_in_threadpool(_quiet_hours, body)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response(value, owner, request_id)


class _Usage(_Body):
    refresh: StrictBool = False


def _usage_for_profile(agent_id: str, refresh: bool) -> dict:
    from .provider_usage import usage
    from .workspace_capabilities import profile_scope
    with ExitStack() as stack:
        try:
            stack.enter_context(profile_scope(agent_id))
        except ValueError:
            raise NativeAPIError(404, "profile_not_found", "The selected profile no longer exists.") from None
        return usage(agent_id, refresh=refresh)


@router.post("/usage/list")
async def provider_usage(request: Request) -> Response:
    """Usage and limits for the AI tools and providers on this host (bighelp app: Usage)."""
    from .provider_usage import CAPABILITY
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "usage_unavailable", "Usage isn't available on this host.")
    body = await _body(request, _Usage)
    value = await run_in_threadpool(_usage_for_profile, body.agentId, body.refresh)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response({"agentId": body.agentId, **value}, owner, request_id)


class _UsageActivity(_Body):
    days: StrictInt = Field(ge=1, le=365)


def _usage_activity(agent_id: str, days: int) -> dict:
    from .usage_activity import activity
    try:
        return activity(agent_id, days)
    except LookupError:
        raise NativeAPIError(404, "profile_not_found", "The selected profile no longer exists.") from None


@router.post("/usage/activity")
async def usage_activity(request: Request) -> Response:
    """When an agent works: hours, messages and models per day (bighelp app: Usage)."""
    from .usage_activity import CAPABILITY
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "usage_activity_unavailable", "Usage details aren't available on this host.")
    body = await _body(request, _UsageActivity)
    value = await run_in_threadpool(_usage_activity, body.agentId, body.days)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response({"agentId": body.agentId, **value}, owner, request_id)


_SESSION_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


class _SignInStart(_Body):
    providerId: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")


class _SignInSession(_Body):
    sessionId: str = Field(pattern=_SESSION_ID)


class _SignInCode(_SignInSession):
    code: str = Field(min_length=1, max_length=2048)


_SIGN_IN_BODIES: dict[str, type[_Body]] = {
    "list": _Body, "start": _SignInStart, "status": _SignInSession, "submit": _SignInCode, "cancel": _SignInSession,
}
_SIGN_IN_STATUS = {
    "sign_in_unavailable": 404, "sign_in_not_found": 404, "sign_in_tool_missing": 409, "sign_in_not_waiting": 409,
    "sign_in_retired": 410, "sign_in_code_invalid": 422, "sign_in_busy": 429, "sign_in_start_failed": 503,
}


def _sign_in(operation: str, body: _Body) -> dict:
    from hermes_cli.profiles import profile_exists
    from . import provider_sign_in as sign_in
    if not profile_exists(body.agentId):
        raise NativeAPIError(404, "profile_not_found", "The selected profile no longer exists.")
    try:
        if operation == "list":
            return {"providers": sign_in.providers(body.agentId)}
        if operation == "start":
            return sign_in.start(body.agentId, body.providerId)
        if operation == "status":
            return sign_in.status(body.agentId, body.sessionId)
        if operation == "submit":
            return sign_in.submit(body.agentId, body.sessionId, body.code)
        return sign_in.cancel(body.agentId, body.sessionId)
    except sign_in.SignInError as error:
        raise NativeAPIError(_SIGN_IN_STATUS.get(error.code, 409), error.code, error.message) from None


@router.post("/provider-sign-in/{operation}")
async def provider_sign_in(operation: str, request: Request) -> Response:
    """Sign in to a provider account with that provider's own sign-in tool (bighelp app: Provider Keys)."""
    from .provider_sign_in import CAPABILITY
    model = _SIGN_IN_BODIES.get(operation)
    if model is None:
        raise NativeAPIError(404, "unknown_operation", "The sign-in operation is unknown.")
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "sign_in_host_unavailable", "This host can't run sign-ins for the app.")
    body = await _body(request, model)
    value = await run_in_threadpool(_sign_in, operation, body)
    return _response({"agentId": body.agentId, **value}, owner, request_id)


def _composio(operation: str, agent_id: str) -> dict:
    from . import composio
    try:
        value = composio.install(agent_id) if operation == "install" else composio.status(agent_id)
    except composio.ComposioError as error:
        raise NativeAPIError(error.status, error.code, error.message) from None
    return {"agentId": agent_id, **value}


async def _composio_request(request: Request, operation: str) -> Response:
    from .composio import CAPABILITY
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "composio_unavailable", "This Hermes can't set up Composio from the app.")
    body = await _body(request, _Body)
    if PROFILE_ID.fullmatch(body.agentId) is None:
        raise NativeAPIError(422, "invalid_request", "The profile is invalid.")
    value = await run_in_threadpool(_composio, operation, body.agentId)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response(value, owner, request_id)


@router.post("/composio/status")
async def composio_status(request: Request) -> Response:
    """Composio MCP servers in the agent's Hermes config (bighelp app: Settings › Apps)."""
    return await _composio_request(request, "status")


@router.post("/composio/install")
async def composio_install(request: Request) -> Response:
    """Turn on or add Composio Connect's MCP server for the agent; the app then signs it in with Hermes' MCP OAuth."""
    return await _composio_request(request, "install")


class _MyTemplatesList(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _MyTemplatesSave(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    template: dict[str, Any]


class _MyTemplatesDelete(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def _my_templates(operation: str, body: BaseModel) -> dict:
    from . import my_templates
    try:
        if operation == "save":
            return {"template": my_templates.store().save(body.template)}
        if operation == "delete":
            return {"id": body.id, "deleted": my_templates.store().delete(body.id)}
        return my_templates.store().list()
    except my_templates.MyTemplateError as error:
        raise NativeAPIError(error.status, error.code, error.message) from None


@router.post("/my-templates/{operation}")
async def my_templates_request(operation: str, request: Request) -> Response:
    """My Templates: bots saved as templates on this computer, listed in the app's template picker."""
    from .my_templates import CAPABILITY, MAX_RESPONSE_BYTES
    models = {"list": _MyTemplatesList, "save": _MyTemplatesSave, "delete": _MyTemplatesDelete}
    if operation not in models:
        raise NativeAPIError(404, "operation_not_found", "The operation is not supported.")
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "my_templates_unavailable", "Saved templates aren't available on this host.")
    body = await _body(request, models[operation])
    value = await run_in_threadpool(_my_templates, operation, body)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return _response(value, owner, request_id, maximum=MAX_RESPONSE_BYTES)


# MARK: Photo actions (docs/IMAGE_DRAFTS.md)

_DRAFT_ID = r"^d_[0-9a-f]{32}$"
_DECISION_ID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_CHAT_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"


class _ImageActionsSave(_Body):
    expectedRevision: StrictInt = Field(ge=1, le=9_007_199_254_740_991)
    settings: dict[str, Any]


class _Draft(_Body):
    sessionId: str = Field(pattern=_CHAT_ID)
    draftId: str = Field(pattern=_DRAFT_ID)


class _DraftStates(_Body):
    sessionId: str = Field(pattern=_CHAT_ID)
    draftIds: list[Annotated[str, Field(pattern=_DRAFT_ID)]] = Field(min_length=1, max_length=50)


class _DraftSource(_Draft):
    offset: StrictInt = Field(ge=0, le=25 * 1024 * 1024)


class _DraftClaim(_Draft):
    decisionId: str = Field(pattern=_DECISION_ID)
    fieldsHash: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination: str = Field(min_length=1, max_length=100)


class _DraftFinish(_Draft):
    decisionId: str = Field(pattern=_DECISION_ID)
    outcome: Literal["saved", "failed", "outcome_unknown"]
    destination: str | None = Field(default=None, min_length=1, max_length=100)
    reason: Literal["permission_denied", "destination_unavailable", "invalid_fields", "write_failed"] | None = None
    verified: StrictBool | None = None


class _DraftReopen(_Draft):
    decisionId: str = Field(pattern=_DECISION_ID)


_IMAGE_ACTIONS = {"get": _Body, "save": _ImageActionsSave}
_IMAGE_DRAFTS = {"states": _DraftStates, "source": _DraftSource, "claim": _DraftClaim, "finish": _DraftFinish,
                 "discard": _Draft, "reopen": _DraftReopen}


def _image_drafts(kind: str, operation: str, body: BaseModel) -> dict:
    from . import image_drafts
    try:
        home = image_drafts.profile_home(body.agentId)
        store = image_drafts.store_for_home(home)
        if kind == "actions":
            if operation == "save":
                revision, overrides = store.save_settings(body.expectedRevision, body.settings)
                return {"settings": image_drafts.settings_document(revision, overrides)}
            revision, overrides = store.settings()
            return {"settings": image_drafts.settings_document(revision, overrides),
                    "defaults": image_drafts.defaults_document()}
        # A draft belongs to one agent and one chat, through compaction.
        lineage = image_drafts.session_lineage(home / "state.db", body.sessionId)
        if operation == "states":
            return {"drafts": store.states(body.draftIds, lineage)}
        if operation == "source":
            return image_drafts.read_source(store.source(body.draftId, lineage), body.offset, home)
        if operation == "claim":
            return store.claim(body.draftId, lineage, decision_id=body.decisionId, fields_hash=body.fieldsHash,
                               destination=body.destination)
        if operation == "finish":
            if (body.outcome == "failed") != (body.reason is not None):
                raise NativeAPIError(422, "invalid_request", "A failed outcome takes a reason; others don't.")
            return store.finish(body.draftId, lineage, decision_id=body.decisionId, outcome=body.outcome,
                                destination=body.destination, reason=body.reason, verified=body.verified)
        if operation == "discard":
            return store.discard(body.draftId, lineage)
        return store.reopen(body.draftId, lineage, decision_id=body.decisionId)
    except image_drafts.DraftError as error:
        raise NativeAPIError(error.status, error.code, error.message, error.details) from None


async def _image_drafts_request(kind: str, operation: str, request: Request) -> Response:
    from .image_drafts import CAPABILITY, MAX_SOURCE_RESPONSE_BYTES
    models = _IMAGE_ACTIONS if kind == "actions" else _IMAGE_DRAFTS
    if operation not in models:
        raise NativeAPIError(404, "operation_not_found", "The operation is not supported.")
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "image_drafts_unavailable", "Photo actions aren't available on this host.")
    body = await _body(request, models[operation])
    value = await run_in_threadpool(_image_drafts, kind, operation, body)
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    maximum = MAX_SOURCE_RESPONSE_BYTES if operation == "source" else None
    return _response(value, owner, request_id, maximum=maximum)


@router.post("/image-actions/{operation}")
async def image_actions(operation: str, request: Request) -> Response:
    """An agent's Photo actions settings (bighelp app: the agent's Photo actions page)."""
    return await _image_drafts_request("actions", operation, request)


@router.post("/image-drafts/{operation}")
async def image_drafts_route(operation: str, request: Request) -> Response:
    """Contact and event drafts from the person's pictures: their states, their picture and the person's decision."""
    return await _image_drafts_request("drafts", operation, request)
