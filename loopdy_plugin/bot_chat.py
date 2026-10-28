"""An agent's Bot Chat, made by Hermes itself as its own chat.

The app used to make each agent's Bot Chat over the dashboard's socket (``session.create`` with source
``bighelp``). Hermes' Bot Chat delivery (``message_agent``, peer messages, scheduled deliveries) keeps only a
"Bot Chat" that is its own local chat (``local-…``) or older local history (``cli``, ``tui``, ``desktop``…;
``gateway/session_bot.py`` ``displaceable_bot_chat``). So the first time another agent wrote to the app's Bot
Chat, Hermes renamed it ``Bot Chat (bighelp <id>)``, made it visible and started a new one.

Here the app asks, and the agent's own gateway makes the chat the way Hermes' delivery does
(``_create_bot_chat``): ``session.create`` titled "Bot Chat", source ``cli``, the home folder, following the
profile's config, born hidden as Hermes Desktop makes it. This goes through Hermes' public client
(``connect_gateway``), the path ``hermes -p <agent> chat -c "Bot Chat" --create-if-missing`` takes: Hermes
resolves or creates under its own lock and writes the row. The plugin writes nothing.

A chat that already holds the title is answered as it is, whatever made it: nothing is renamed, moved, hidden
or made twice. The request ID comes from the app's idempotency key, so a retry gets the same chat.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, closing
import logging
from pathlib import Path
import re
import sqlite3

from .workspace_capabilities import profile_scope

CAPABILITY = "native-bot-chat-v1"
TITLE = "Bot Chat"
# Under the app's 60-second wait for this route: Hermes may have to start the agent's gateway first.
START_SECONDS = 40.0
TOTAL_SECONDS = 50.0
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
# Hermes' own refusals that say "not now" (draining for an update, storage busy), not "no".
_BUSY = frozenset({"runtime_draining", "runtime_retired", "storage_unavailable", "stale_epoch", "queue_full"})
logger = logging.getLogger("hermes.plugins.bighelp")


class BotChatError(Exception):
    """A fixed failure the route turns into its error envelope. ``reason``: Hermes' own bounded code, if any."""

    def __init__(self, code: str, *, busy: bool = False, reason: str | None = None):
        super().__init__(code)
        self.code, self.busy, self.reason = code, busy, reason


def available() -> bool:
    # Hermes 0.22's gateway client and session store; an older Hermes keeps the app's own Bot Chat.
    try:
        from hermes_cli.gateway_client import GatewayClientError, connect_gateway  # noqa: F401
        from hermes_cli.gateway_runtime import attachable, ensure_gateway_runtime  # noqa: F401
        from hermes_cli.profiles import get_profile_dir, profile_exists  # noqa: F401
        from hermes_state import SessionDB  # noqa: F401
    except Exception:
        return False
    return True


def request_id(idempotency_key: str) -> str:
    return "bighelp-bot-chat:" + idempotency_key


def _existing(home: Path, request: str) -> dict | None:
    """The chat holding the title in the agent's own history (read-only), as the route answers it."""
    from hermes_state import SessionDB
    path = home / "state.db"
    if not path.is_file():
        return None
    with closing(SessionDB(db_path=path, read_only=True)) as db:
        row = db.get_session_by_title(TITLE)
        if not row or not isinstance(row.get("id"), str) or not SESSION_ID.fullmatch(row["id"]):
            return None
        return {"storedSessionId": row["id"], "created": _made_by(db, row["id"], request),
                "native": row["id"].startswith("local-")}


def _made_by(db, session_id: str, request: str) -> bool:
    """Whether Hermes made this chat for this request: its creation receipt names the request ID."""
    try:
        from hermes_state_local import local_receipt
        return local_receipt(db, session_id).get("request_id") == request
    except Exception:
        return False


async def _ask_gateway(home: Path, request: str) -> str:
    from hermes_cli.gateway_client import GatewayClientError, GatewayRPCError, connect_gateway
    from hermes_cli.gateway_runtime import attachable, ensure_gateway_runtime

    # The agent's own gateway, started if needed (it ends itself once idle, as for a CLI chat). Named
    # explicitly rather than read from HERMES_TUI_GATEWAY_URL, so the chat lands on this agent.
    found = await asyncio.to_thread(ensure_gateway_runtime, home, timeout=START_SECONDS, idle_exit=True)
    if not attachable(found):
        logger.warning("bighelp bot chat: gateway %s", getattr(found, "state", "unknown"))
        raise BotChatError("bot_chat_gateway_unavailable", busy=True)
    params = {"request_id": request, "title": TITLE, "source": "cli", "cwd": str(Path.home()),
              "follow_profile_config": True, "hidden": True}
    try:
        async with connect_gateway(endpoint=found.endpoint) as client:
            try:
                snapshot = await client.rpc("session.create", **params)
            except GatewayRPCError as error:
                # A gateway that doesn't take `hidden` yet refuses it before it writes anything. It makes the
                # chat visible then, as its own Bot Chat delivery does.
                if str(error) != "invalid_params" or error.data.get("field") != "params.hidden":
                    raise
                del params["hidden"]
                snapshot = await client.rpc("session.create", **params)
    except GatewayRPCError as error:
        reason = str(error)
        logger.warning("bighelp bot chat: gateway refused (%s)", reason)
        raise BotChatError("bot_chat_refused", busy=reason in _BUSY, reason=reason) from None
    except (GatewayClientError, ValueError, OSError):
        # A lost connection, or a ticket the gateway's control socket wouldn't mint (DiscoveryError).
        logger.warning("bighelp bot chat: gateway connection lost")
        raise BotChatError("bot_chat_gateway_unavailable", busy=True) from None
    stored = snapshot.get("stored_session_id") if isinstance(snapshot, dict) else None
    if not isinstance(stored, str) or not SESSION_ID.fullmatch(stored):
        raise BotChatError("bot_chat_refused")
    return stored


async def _read(home: Path, request: str) -> dict | None:
    try:
        return await asyncio.to_thread(_existing, home, request)
    except (OSError, sqlite3.Error):
        logger.warning("bighelp bot chat: history unreadable")
        raise BotChatError("bot_chat_storage_unavailable", busy=True) from None


async def create(profile: str, idempotency_key: str) -> dict:
    """The agent's Bot Chat: the one that holds the title, else one Hermes makes now. ``LookupError``: no
    such agent; ``BotChatError``: Hermes couldn't make it."""
    request = request_id(idempotency_key)
    with ExitStack() as stack:
        try:
            stack.enter_context(profile_scope(profile))
        except ValueError:
            raise LookupError(profile) from None
        from hermes_constants import get_hermes_home
        home = get_hermes_home()
        existing = await _read(home, request)
        if existing is not None:
            return existing
        try:
            async with asyncio.timeout(TOTAL_SECONDS):
                stored = await _ask_gateway(home, request)
        except TimeoutError:
            # Hermes may still finish it; a retry with the same key gets that chat.
            logger.warning("bighelp bot chat: gateway too slow")
            raise BotChatError("bot_chat_timeout", busy=True) from None
        answered = await _read(home, request)
    if answered is not None and answered["storedSessionId"] == stored:
        return answered
    # Hermes answered with a chat it doesn't list under the title (yet): report what it said.
    return {"storedSessionId": stored, "created": False, "native": stored.startswith("local-")}
