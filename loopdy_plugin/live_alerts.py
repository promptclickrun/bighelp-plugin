"""Instant alerts for a device that has bighelp open (``native-live-alerts-v1``).

While bighelp is in front, the device holds a long-poll on ``/native/alerts/listen``
for its notification grants. The device is marked live in the notification journal,
not in memory: the hook that queues an alert can run in another Hermes process than
the dashboard holding the request ("How the plugin loads" in AGENTS.md).

An alert for a live device is offered to it first, as the same sealed envelope the
push carries. The device decides what to show (it drops the chat on screen, which the
host never learns) and acks. An ack inside the window settles the alert, and it never
goes to the push service. Without one, today's push goes out unchanged.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Annotated, Any, Callable

from pydantic import BaseModel, ConfigDict, Field, StrictInt

CAPABILITY = "native-live-alerts-v1"
# How long a device has to ack, from the moment the alert is there for it.
ACK_WINDOW_SECONDS = 1.0
MAX_WAIT_SECONDS = 25
# A device between two polls (it just got an answer) stays live this long.
LEASE_GRACE_SECONDS = 10
MAX_LIVE_GRANTS = 32
MAX_GRANTS_PER_REQUEST = 8
MAX_ACKS_PER_REQUEST = 32
MAX_ALERTS_PER_RESPONSE = 8
MAX_RESPONSE_BYTES = 1_000_000
# An agent picture the device hasn't cached rides along when it fits.
MAX_INLINE_AVATAR_CHARS = 393_216
POLL_SECONDS = 0.2
_KEEP_SECONDS = 3_600
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
_SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_EVENT_ID = Annotated[str, Field(pattern=_UUID[:-1] + r":[0-9a-f]{64}$")]

SCHEMA = """
CREATE TABLE IF NOT EXISTS live_listeners(grant_id TEXT PRIMARY KEY REFERENCES grants(grant_id) ON DELETE CASCADE, listener TEXT NOT NULL, lease_expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS live_alerts(event_id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(grant_id) ON DELETE CASCADE, available_at REAL NOT NULL, deadline REAL NOT NULL, state TEXT NOT NULL, handed_to TEXT);
"""


class LiveAlertError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


class LiveGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grantId: str = Field(pattern=_UUID)
    recipientKeyId: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")


class ListenBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    listenerId: str = Field(pattern=_UUID)
    grants: list[LiveGrant] = Field(min_length=1, max_length=MAX_GRANTS_PER_REQUEST)
    waitSeconds: StrictInt = Field(ge=0, le=MAX_WAIT_SECONDS)
    knownAvatars: list[_SHA256] = Field(default_factory=list, max_length=32)


class StopBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    listenerId: str = Field(pattern=_UUID)
    grants: list[LiveGrant] = Field(min_length=1, max_length=MAX_GRANTS_PER_REQUEST)


class AckBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grants: list[LiveGrant] = Field(min_length=1, max_length=MAX_GRANTS_PER_REQUEST)
    eventIds: list[_EVENT_ID] = Field(min_length=1, max_length=MAX_ACKS_PER_REQUEST)


def available() -> bool:
    from .managed_notifications import fcntl
    if fcntl is None:
        return False
    try:
        from hermes_constants import get_hermes_home  # noqa: F401
    except ImportError:
        return False
    return True


# Send side: called by ManagedNotifications in whichever process queues the alert.

def offer(db, grant_id: str, event_id: str, due: int, now: float) -> int:
    """Offers a just-queued alert to its device when that device is live. Returns
    when the push may go: unchanged without a live device, else after the window."""
    db.execute("DELETE FROM live_alerts WHERE available_at<?", (now - _KEEP_SECONDS,))
    if not db.execute("SELECT 1 FROM live_listeners WHERE grant_id=? AND lease_expires>?",
                      (grant_id, now)).fetchone():
        return due
    available_at = max(float(due), now)
    deadline = available_at + ACK_WINDOW_SECONDS
    db.execute("INSERT OR IGNORE INTO live_alerts(event_id,grant_id,available_at,deadline,state) "
               "VALUES(?,?,?,?,'offered')", (event_id, grant_id, available_at, deadline))
    return math.ceil(deadline)


def offered(service, intent_ids: list[str]) -> list[str]:
    """The alerts among these that wait for a live device's ack."""
    marks = ",".join("?" * len(intent_ids))
    with service._db() as db:
        rows = db.execute(f"SELECT event_id FROM live_alerts WHERE state='offered' AND event_id IN ({marks})",
                          intent_ids).fetchall()
    found = {row["event_id"] for row in rows}
    return [intent for intent in intent_ids if intent in found]


def await_acks(service, intent_ids: list[str], *, sleep: Callable[[float], None] = time.sleep) -> None:
    """Waits (at most the window) for the device to ack the alerts just offered to it.
    The ones it didn't ack become due for the push right away."""
    marks = ",".join("?" * len(intent_ids))
    for _ in range(int((ACK_WINDOW_SECONDS + 1) / 0.05) + 1):
        now = service.clock()
        with service._db() as db:
            rows = db.execute(f"SELECT event_id,deadline FROM live_alerts WHERE event_id IN ({marks}) "
                              "AND state='offered'", intent_ids).fetchall()
            missed = [row["event_id"] for row in rows if row["deadline"] <= now]
            if missed:
                late = ",".join("?" * len(missed))
                db.execute(f"UPDATE live_alerts SET state='missed' WHERE state='offered' AND event_id IN ({late})", missed)
                db.execute(f"UPDATE pending SET next_attempt=? WHERE state='pending' AND intent_id IN ({late})",
                           (int(now), *missed))
        if len(missed) == len(rows) or service._stop.is_set():
            return
        sleep(0.05)


# Device side: the dashboard's native routes.

def _active_grants(db, grants: list[LiveGrant], now: float) -> list[str]:
    valid = []
    for grant in grants:
        if db.execute("SELECT 1 FROM grants g JOIN recipients r USING(grant_id) WHERE g.grant_id=? "
                      "AND g.state='active' AND g.expires>? AND r.key_id=?",
                      (grant.grantId, int(now), grant.recipientKeyId)).fetchone():
            valid.append(grant.grantId)
    return valid


def register(service, body: ListenBody) -> list[str]:
    """Marks the device live for its grants until the poll ends (plus a short grace)."""
    now = service.clock()
    with service._db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM live_listeners WHERE lease_expires<?", (now - _KEEP_SECONDS,))
        grants = _active_grants(db, body.grants, now)
        if not grants:
            raise LiveAlertError(404, "live_alerts_not_enrolled", "This device has no notifications on this host.")
        marks = ",".join("?" * len(grants))
        live = db.execute(f"SELECT COUNT(*) FROM live_listeners WHERE lease_expires>? AND grant_id NOT IN ({marks})",
                          (now, *grants)).fetchone()[0]
        if live + len(grants) > MAX_LIVE_GRANTS:
            raise LiveAlertError(429, "live_alerts_busy", "Too many devices are listening.")
        lease = now + body.waitSeconds + LEASE_GRACE_SECONDS
        for grant_id in grants:
            db.execute("INSERT INTO live_listeners VALUES(?,?,?) ON CONFLICT(grant_id) DO UPDATE SET "
                       "listener=excluded.listener,lease_expires=excluded.lease_expires",
                       (grant_id, body.listenerId, lease))
    return grants


def poll(service, grants: list[str], listener: str, known_avatars: set[str]) -> tuple[list[dict[str, Any]], bool]:
    """Alerts there for this device now, and whether this poll still holds its grants
    (a newer poll from the same device takes them over)."""
    now = service.clock()
    marks = ",".join("?" * len(grants))
    with service._db() as db:
        owned = db.execute(f"SELECT COUNT(*) FROM live_listeners WHERE listener=? AND grant_id IN ({marks})",
                           (listener, *grants)).fetchone()[0] > 0
        rows = db.execute(
            "SELECT l.event_id,l.grant_id,p.raw,e.detail_json FROM live_alerts l "
            "JOIN pending p ON p.intent_id=l.event_id AND p.grant_id=l.grant_id AND p.state='pending' "
            "JOIN events e ON e.event_id=l.event_id "
            "JOIN grants g ON g.grant_id=l.grant_id AND g.state='active' AND g.expires>? "
            f"WHERE l.grant_id IN ({marks}) AND l.state='offered' AND l.available_at<=? AND l.deadline>? "
            "AND (l.handed_to IS NULL OR l.handed_to!=?) ORDER BY l.available_at,l.event_id LIMIT ?",
            (int(now), *grants, now, now, listener, MAX_ALERTS_PER_RESPONSE)).fetchall() if owned else []
        alerts, size = [], 0
        for row in rows:
            alert = _alert(row, known_avatars)
            encoded = len(json.dumps(alert, separators=(",", ":")))
            if size + encoded > MAX_RESPONSE_BYTES and "data" in alert["avatar"]:
                alert["avatar"].pop("data")
                encoded = len(json.dumps(alert, separators=(",", ":")))
            if size + encoded > MAX_RESPONSE_BYTES:
                break
            size += encoded
            alerts.append(alert)
            db.execute("UPDATE live_alerts SET handed_to=? WHERE event_id=?", (listener, row["event_id"]))
    return alerts, owned


def _alert(row, known_avatars: set[str]) -> dict[str, Any]:
    raw = json.loads(bytes(row["raw"]))
    detail = json.loads(row["detail_json"])
    avatar = {"sha256": raw["avatar"]["sha256"]}
    data = raw["avatar"].get("data")
    if (detail["agent"]["avatarSha256"] not in known_avatars and isinstance(data, str)
            and len(data) <= MAX_INLINE_AVATAR_CHARS):
        avatar["data"] = data
    return {"grantId": row["grant_id"], "agentId": detail["profile"], "eventId": raw["eventId"],
            "eventType": raw["eventType"], "sessionReference": raw["sessionReference"],
            "turnId": raw["turnId"], "occurredAt": raw["occurredAt"], "sealed": raw["sealed"],
            "avatar": avatar}


def release(service, grants: list[str], listener: str, *, disconnected: bool) -> None:
    """The poll ended. A device that went away is no longer live; one that got an
    answer stays live for a moment while it polls again."""
    now = service.clock()
    marks = ",".join("?" * len(grants))
    with service._db() as db:
        if disconnected:
            db.execute(f"DELETE FROM live_listeners WHERE listener=? AND grant_id IN ({marks})", (listener, *grants))
        else:
            db.execute(f"UPDATE live_listeners SET lease_expires=MIN(lease_expires,?) WHERE listener=? "
                       f"AND grant_id IN ({marks})", (now + LEASE_GRACE_SECONDS, listener, *grants))


def stop(service, body: StopBody) -> None:
    """bighelp left the front: the device isn't live, so its alerts push at once.
    (Hermes' dashboard doesn't always report a dropped request.)"""
    grants = [grant.grantId for grant in body.grants]
    release(service, grants, body.listenerId, disconnected=True)


def ack(service, body: AckBody) -> list[str]:
    """The device showed (or deliberately dropped) these alerts. Each one not yet
    claimed by the push sender is settled and never pushed."""
    now = service.clock()
    settled = []
    with service._db() as db:
        db.execute("BEGIN IMMEDIATE")
        grants = set(_active_grants(db, body.grants, now))
        if not grants:
            raise LiveAlertError(404, "live_alerts_not_enrolled", "This device has no notifications on this host.")
        for event_id in body.eventIds:
            grant_id = event_id.partition(":")[0]
            if grant_id not in grants:
                continue
            db.execute("UPDATE live_alerts SET state='acked' WHERE event_id=? AND grant_id=? "
                       "AND state IN ('offered','missed')", (event_id, grant_id))
            if db.execute("UPDATE pending SET state='delivered_live' WHERE intent_id=? AND grant_id=? "
                          "AND state='pending'", (event_id, grant_id)).rowcount == 1:
                settled.append(event_id)
    return settled


async def listen(body: ListenBody, *, service, is_disconnected: Callable, run: Callable,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable = asyncio.sleep) -> dict[str, Any]:
    """Holds the request until an alert is there, a newer poll takes over, the device
    goes away, or ``waitSeconds`` pass. ``run`` moves journal work off the event loop."""
    grants = await run(register, service, body)
    known = set(body.knownAvatars)
    end = clock() + body.waitSeconds
    disconnected = False
    alerts: list[dict[str, Any]] = []
    try:
        while True:
            alerts, owned = await run(poll, service, grants, body.listenerId, known)
            if alerts or not owned or clock() >= end:
                break
            if await is_disconnected():
                disconnected = True
                break
            await sleep(POLL_SECONDS)
    finally:
        await run(release, service, grants, body.listenerId, disconnected=disconnected)
    return {"alerts": alerts, "grantIds": grants, "ackWindowMilliseconds": int(ACK_WINDOW_SECONDS * 1000)}
