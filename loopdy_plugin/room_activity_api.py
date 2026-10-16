"""Fixed authenticated endpoints for disposable hosted-room tool observations."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import Response
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .native_api import _NativeRoute, _body, _precondition, _response
from .native_context import NativeAPIError, native_context
from .room_activity import CAPABILITY, PEER_TYPING_CAPABILITY, TYPING_CAPABILITY, PeerScope, RoomScope, activity_hub


router = APIRouter(prefix="/groups/activity", route_class=_NativeRoute)


class _Room(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    roomId: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class _PeerMember(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    memberId: str = Field(pattern=_IDENTIFIER)
    profile: str = Field(pattern=_IDENTIFIER)


class _Peer(BaseModel):
    """Agents on this computer in a room homed on another computer (RoomLink)."""
    model_config = ConfigDict(extra="forbid", strict=True)
    homeInstallId: str = Field(pattern=_IDENTIFIER)
    members: list[_PeerMember] = Field(min_length=1, max_length=6)


class _Open(_Room):
    include: list[Literal["member.responding"]] = Field(default_factory=list, max_length=1)
    peer: _Peer | None = None


class _Stream(_Room):
    streamId: str = Field(pattern=r"^[0-9a-f]{32}$")


class _Poll(_Stream):
    after: StrictInt = Field(ge=0, le=9_007_199_254_740_991)
    limit: StrictInt = Field(ge=1, le=8)


async def read_room_scope(room_id: str) -> RoomScope:
    from hermes_constants import (
        get_process_hermes_home, set_hermes_home_override, reset_hermes_home_override,
    )
    from .adapter import get_service
    from .workspace_control import HermesWorkspaceBackend, WorkspaceControlError
    token = set_hermes_home_override(get_process_hermes_home())
    try:
        backend = HermesWorkspaceBackend(service=get_service())
        try:
            state = await backend.groups_state({"room_id": room_id})
        except WorkspaceControlError:
            raise NativeAPIError(503, "room_unavailable", "The native room could not be verified.") from None
        return RoomScope.from_state(state, room_id)
    finally:
        reset_hermes_home_override(token)


async def read_peer_scope(room_id: str, peer: _Peer) -> PeerScope:
    """This computer must hold a live RoomLink reservation for each agent."""
    try:
        from gateway.hosted_rooms import default_db_path, probe_peer_room_reservation
    except ImportError:
        raise NativeAPIError(503, "activity_unavailable", "This Hermes can't join rooms on other computers.") from None
    members = tuple((member.memberId, member.profile) for member in peer.members)
    if len({member_id for member_id, _ in members}) != len(members):
        raise NativeAPIError(422, "invalid_request", "Each agent is listed once.")

    def reserved() -> bool:
        path = default_db_path()
        return all(probe_peer_room_reservation(path, room_id=room_id, target_profile=profile)
                   for _member, profile in members)
    try:
        ok = await asyncio.to_thread(reserved)
    except Exception:
        raise NativeAPIError(503, "room_unavailable", "The room could not be verified.") from None
    if not ok:
        raise NativeAPIError(404, "room_unavailable", "No agent here is linked to that room.")
    return PeerScope(room_id, peer.homeInstallId, members)


def _same_owner(request: Request, owner) -> None:
    if native_context(request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; discard the observation feed.")


@router.post("/open")
async def open_feed(request: Request) -> Response:
    owner = native_context(request)
    request_id = _precondition(request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "activity_unavailable", "The public room observer is unavailable.")
    body = await _body(request, _Open)
    if body.include and TYPING_CAPABILITY not in owner.features:
        raise NativeAPIError(503, "activity_unavailable", "The public room observer is unavailable.")
    if body.peer is not None:
        # Only who is writing: tool details of a room homed elsewhere stay there.
        if PEER_TYPING_CAPABILITY not in owner.features or not body.include:
            raise NativeAPIError(503, "activity_unavailable", "The public room observer is unavailable.")
        _same_owner(request, owner)
        scope = await read_peer_scope(body.roomId, body.peer)
    else:
        _same_owner(request, owner)
        scope = await read_room_scope(body.roomId)
    _same_owner(request, owner)
    page = activity_hub().open(owner, scope, responding=bool(body.include))
    return _response(page, owner, request_id)


@router.post("/poll")
async def poll_feed(request: Request) -> Response:
    owner = native_context(request)
    request_id = _precondition(request, owner)
    body = await _body(request, _Poll)
    _same_owner(request, owner)
    hub = activity_hub()
    scope = hub.scope(body.streamId, owner, body.roomId)
    if not hasattr(scope, "home_install_id"):  # a room hosted here: verify it again
        scope = await read_room_scope(body.roomId)
    _same_owner(request, owner)
    page = hub.poll(body.streamId, owner, scope, body.after, body.limit)
    return _response(page, owner, request_id)


@router.post("/close")
async def close_feed(request: Request) -> Response:
    owner = native_context(request)
    request_id = _precondition(request, owner)
    body = await _body(request, _Stream)
    _same_owner(request, owner)
    return _response(activity_hub().close(body.streamId, owner, body.roomId), owner, request_id)
