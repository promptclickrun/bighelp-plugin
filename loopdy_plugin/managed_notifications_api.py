"""Mounted only by Hermes' supported authenticated plugin router loader."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from . import host_telemetry
from .managed_notifications import ManagedNotificationError, get_process_notifications

logger = logging.getLogger("hermes.plugins.bighelp.notifications")
router = APIRouter(prefix="/notifications")
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
_PROFILE = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"


class VersionedBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt = Field(ge=1, le=1)


class EnrollmentBody(VersionedBody):
    idempotencyKey: str = Field(pattern=_UUID)
    grantId: str = Field(pattern=_UUID)


class RecipientKeyBody(VersionedBody):
    publicKey: str = Field(pattern=r"^[A-Za-z0-9_-]{87}$")


class SessionBody(VersionedBody):
    profile: str = Field(pattern=_PROFILE)
    sessionId: str = Field(pattern=_ID)
    enabled: StrictBool


class WorkflowAlertsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    needsYou: StrictBool
    succeeded: StrictBool
    failed: StrictBool
    cancelled: StrictBool


class ChannelRulesBody(BaseModel):
    """Which channels alert this phone (notification_channels.py): a choice for each channel it has seen,
    and ``other`` for the rest. bighelp itself always alerts."""
    model_config = ConfigDict(extra="forbid", strict=True)
    other: StrictBool
    choices: dict[str, StrictBool] = Field(max_length=64)


class PreferencesBody(VersionedBody):
    peerChats: StrictBool
    workflows: WorkflowAlertsBody | None = None
    # The phone opens a scheduled task's alert in the job's chat or on the job (scheduled_links.py).
    scheduledLinks: StrictBool | None = None
    channels: ChannelRulesBody | None = None


class WorkBody(VersionedBody):
    profile: str = Field(pattern=_PROFILE)
    sessionId: str = Field(pattern=_ID)


class ActivityBody(VersionedBody):
    profile: str = Field(pattern=_PROFILE)
    sessionId: str = Field(pattern=_ID)
    sessionReference: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")
    leaseExpires: StrictInt = Field(gt=0, le=9_999_999_999)
    turnId: str | None = Field(default=None, pattern=_ID)


def _call(operation, *args):
    try:
        return operation(*args)
    except ManagedNotificationError as error:
        if error.status >= 500:
            # The app gets the code; the host owner's log and host telemetry get it too.
            logger.warning("bighelp notification request failed: %s", error.code)
            host_telemetry.error(code=error.code, stage="native_route")
        raise HTTPException(status_code=error.status, detail={"code": error.code}) from None
    except (OSError, ValueError):
        raise HTTPException(status_code=503, detail={"code": "notification_service_unavailable"}) from None


# Synchronous handlers run in FastAPI's threadpool: no blocking HTTPS on the
# Hermes event loop. Authentication is the stock host middleware, not an account
# bearer forwarded from the app. No secret authority is accepted in these bodies.
@router.get("/capabilities")
def capabilities():
    return _call(lambda: get_process_notifications().capabilities())


@router.get("/provider")
def provider():
    """Safe readiness contract; never returns a BuzzKit key or subscriber id."""
    return _call(lambda: get_process_notifications().provider_contract())


@router.post("/enroll")
def enroll(body: EnrollmentBody):
    return _call(lambda: get_process_notifications().enroll(body.grantId, body.idempotencyKey))


@router.get("/enrollments/{grant_id}")
def enrollment(grant_id: str):
    return _call(lambda: get_process_notifications().enrollment(grant_id))


@router.delete("/enrollments/{grant_id}")
def remove(grant_id: str):
    return _call(lambda: get_process_notifications().remove(grant_id))


@router.put("/enrollments/{grant_id}/recipient-key")
def recipient_key(grant_id: str, body: RecipientKeyBody):
    """The phone's own content key, sent directly to this host so notification text is end-to-end sealed."""
    return _call(lambda: get_process_notifications().register_recipient(grant_id, body.publicKey))


@router.put("/enrollments/{grant_id}/sessions")
def subscribe(grant_id: str, body: SessionBody):
    return _call(lambda: get_process_notifications().subscribe(grant_id, body.profile, body.sessionId, body.enabled))


@router.get("/enrollments/{grant_id}/preferences")
def preferences(grant_id: str):
    return _call(lambda: get_process_notifications().preferences(grant_id))


@router.put("/enrollments/{grant_id}/preferences")
def set_preferences(grant_id: str, body: PreferencesBody):
    """This phone's alert choices for the host: peer chats (agents talking to each other) start off,
    workflow alerts (needs you, succeeded, failed, cancelled) start on, scheduled links start off."""
    workflows = body.workflows.model_dump() if body.workflows is not None else None
    channels = body.channels.model_dump() if body.channels is not None else None
    return _call(lambda: get_process_notifications().set_preferences(grant_id, peer_chats=body.peerChats,
                                                                      workflows=workflows,
                                                                      scheduled_links=body.scheduledLinks,
                                                                      channels=channels))


@router.get("/enrollments/{grant_id}/channels")
def channels(grant_id: str):
    """The channels this phone's agent has talked on or delivers to, for Settings › Notifications › Channels."""
    return _call(lambda: get_process_notifications().channels(grant_id))


@router.post("/enrollments/{grant_id}/work")
def work_snapshot(grant_id: str, body: WorkBody):
    return _call(lambda: get_process_notifications().work_snapshot(grant_id, body.profile, body.sessionId))


@router.get("/enrollments/{grant_id}/events/{event_id}")
def event(grant_id: str, event_id: str):
    return _call(lambda: get_process_notifications().event(grant_id, event_id))


@router.put("/enrollments/{grant_id}/live-activities/{activity_id}")
def activity(grant_id: str, activity_id: str, body: ActivityBody):
    return _call(lambda: get_process_notifications().subscribe_activity(grant_id, activity_id, body.profile, body.sessionId, body.sessionReference, body.leaseExpires, body.turnId))


@router.delete("/enrollments/{grant_id}/live-activities/{activity_id}")
def remove_activity(grant_id: str, activity_id: str):
    return _call(lambda: get_process_notifications().remove_activity(grant_id, activity_id))
