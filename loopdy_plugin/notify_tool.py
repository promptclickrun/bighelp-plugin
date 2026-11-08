"""The ``bighelp_send_notification`` tool: an agent tells the person something now.

A normal reply already notifies the person, and a scheduled task notifies with its run's reply. This is
for the rest: a long job that finished while they were away, a reminder they asked for. The alert goes
through the same sealed path as every other alert, to the phones enrolled for the calling agent, and
files under "Scheduled tasks and deliveries" in the notification service. A phone can turn it off
(Deliveries, ``kinds`` in docs/NOTIFICATIONS.md). Each agent can send ten an hour.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import tool_session
from .managed_notifications import ManagedNotificationError, get_managed_notifications
from .naming import TOOLSET

logger = logging.getLogger("hermes.plugins.bighelp")

TOOL_NAME = "bighelp_send_notification"
MAX_TITLE, MAX_MESSAGE = 80, 1_000
PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": MAX_TITLE,
                  "description": "What this is about, in a few words."},
        "message": {"type": "string", "minLength": 1, "maxLength": MAX_MESSAGE,
                    "description": "What the person needs to know. Put the important words first."},
    },
    "required": ["title", "message"],
}
DESCRIPTION = (
    "Send the person a notification on their phone now. Use it to tell them something when they aren't in "
    "the chat: a long job that finished, a reminder they asked for. Don't use it for a normal reply, which "
    "already notifies them, and never to ask questions or to nag. At most 10 an hour. Tapping it opens "
    "this chat. The result says how many phones got it, or why none did."
)


def _refused(reason: str) -> str:
    return json.dumps({"sent": 0, "reason": reason}, separators=(",", ":"))


def _text(value: Any, maximum: int) -> str | None:
    text = value.strip() if isinstance(value, str) else ""
    return text if 0 < len(text) <= maximum else None


def handle_tool(args: Any, context: dict[str, Any] | None = None, *, profile: str) -> str:
    if not isinstance(args, dict) or set(args) != {"title", "message"}:
        return _refused("Give a title and a message, and nothing else.")
    title, message = _text(args["title"], MAX_TITLE), _text(args["message"], MAX_MESSAGE)
    if title is None:
        return _refused(f"The title must be 1 to {MAX_TITLE} characters.")
    if message is None:
        return _refused(f"The message must be 1 to {MAX_MESSAGE:,} characters.")
    session = tool_session.session_id(context, "HERMES_SESSION_KEY", "HERMES_SESSION_ID")
    try:
        return json.dumps(get_managed_notifications().notify(profile, session, title, message), separators=(",", ":"))
    except (ManagedNotificationError, OSError, ValueError) as error:
        logger.warning("bighelp_send_notification unavailable: %s", getattr(error, "code", type(error).__name__))
        return _refused("Notifications aren't available on this computer right now.")


def register(ctx: Any) -> None:
    profile = str(getattr(ctx, "profile_name", "default") or "default")
    ctx.register_tool(
        name=TOOL_NAME, toolset=TOOLSET,
        schema={"name": TOOL_NAME, "description": DESCRIPTION, "parameters": PARAMETERS},
        # PluginContext resolves the profile in scope when the call runs, not the one at load time.
        handler=lambda args, **context: handle_tool(args, context, profile=str(getattr(ctx, "profile_name", profile) or profile)),
        emoji="🔔",
    )
