"""Messages agents send each other with `message_agent`, as the agent asked keeps them (Hermes 0.23+).

Hermes 0.23 hands a `message_agent` message to the other agent's gateway, which runs it in that agent's
Bot Chat and keeps a receipt in the agent's home (`runtime/bot_live_delivery/<delivery id>.json`): its
status, the chat it ran in and, once it settles, the reply. The asking agent hears the reply only from a
waiting process that gives up after about half an hour, so a longer job's answer never reaches its chat.
The app reads the receipt instead, by the delivery ID the asking agent's tool call returned: whether the
other agent has the message, is on it (and which tool), answered (and what), or the message was withdrawn.
Read-only; nothing is stored.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable

from .agent_talk import PROFILE_ID, SESSION_ID


CAPABILITY = "native-agent-deliveries-v1"
MAX_DELIVERIES = 16
MAX_RECEIPT_BYTES = 512 * 1024
MAX_REPLY_CHARS = 32_000
DELIVERY_ID = re.compile(r"[0-9a-f]{64}\Z")
# Hermes' receipt statuses, in the app's words. `claimed` means the gateway took it: it runs now, or
# waits behind the turn that does.
STATES = {"queued": "queued", "claimed": "queued", "canonical": "queued", "settled": "answered",
          "cancelled": "withdrawn", "failed": "failed", "ambiguous": "failed"}
logger = logging.getLogger("hermes.plugins.bighelp")


def available() -> bool:
    # Feeds /native/context: the same Hermes profile helpers as the agents' activity.
    from . import agents_activity
    return agents_activity.available()


def _receipt(home: Path, delivery_id: str) -> tuple[dict, float] | None:
    path = home / "runtime" / "bot_live_delivery" / f"{delivery_id}.json"
    try:
        if path.is_symlink():
            return None
        stat = path.stat()
        if stat.st_size > MAX_RECEIPT_BYTES:
            return None
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return (record, stat.st_mtime) if isinstance(record, dict) else None


def deliveries(items: Iterable[tuple[str, str]], *, now: Callable[[], float] | None = None) -> list[dict[str, Any]]:
    """Each (agent asked, delivery ID) the agent still has a receipt for, in the order asked."""
    from hermes_cli.profiles import get_profile_dir, profile_exists
    from . import chat_live
    clock = (now or time.time)()
    result = []
    for agent, delivery in list(dict.fromkeys(items))[:MAX_DELIVERIES]:
        if not (isinstance(agent, str) and PROFILE_ID.fullmatch(agent) and isinstance(delivery, str)
                and DELIVERY_ID.fullmatch(delivery)) or not profile_exists(agent):
            continue
        home = Path(get_profile_dir(agent))
        found = _receipt(home, delivery)
        if found is None:
            continue
        record, updated = found
        state = STATES.get(record.get("status"), "unknown")
        value: dict[str, Any] = {"agentId": agent, "deliveryId": delivery, "state": state, "updatedAt": updated}
        session = record.get("session_id")
        if isinstance(session, str) and SESSION_ID.fullmatch(session):
            value["sessionId"] = session
            if state == "queued" and chat_live.available():
                try:
                    turn = chat_live.gateway_turn(home, session, clock)
                except Exception:
                    logger.debug("bighelp agent deliveries: turn unreadable")
                    turn = None
                if turn is not None:
                    value["state"] = "working"
                    if turn.get("tool"):
                        value["tool"] = turn["tool"]
        reply = record.get("reply")
        if state == "answered" and isinstance(reply, str) and reply.strip():
            value["reply"] = reply.strip()[:MAX_REPLY_CHARS]
        result.append(value)
    return result
