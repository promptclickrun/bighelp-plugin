"""bighelp's chats outlive a dropped connection, as Hermes Desktop's do.

When an app's connection has been gone a while, Hermes reclaims the session's runtime (``ws_orphan_reap``,
``ws_disconnect``, ``idle_timeout``, …). For a ``desktop`` session it keeps the saved conversation open and
resumable (tui_gateway ``_finalize_session``, Hermes #105588); for any other app's it ends the row. A phone
drops its connection each time someone leaves the app, so bighelp's chats were ended over and over. For
those automatic reasons only, a ``bighelp`` session is finalized the way a Desktop one is. Closing a chat,
archiving or deleting it, and everything else, are unchanged.
"""
from __future__ import annotations

import functools
import logging
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)

BIGHELP_SOURCE = "bighelp"
_DESKTOP_SOURCE = "desktop"
_MARK = "_loopdy_keeps_bighelp_sessions"
_lock = threading.Lock()


def install(server: Any | None = None) -> bool:
    """Idempotent. True once the dashboard's session lifecycle keeps bighelp's chats. Only the process
    serving the apps' connections has ``tui_gateway.server`` loaded; elsewhere, or on a Hermes whose
    lifecycle differs, nothing changes (bighelp then resumes ended chats as before)."""
    server = server or sys.modules.get("tui_gateway.server")
    if server is None:
        return False
    with _lock:
        original = getattr(server, "_finalize_session", None)
        reasons = getattr(server, "_AUTOMATIC_SESSION_END_REASONS", None)
        if not callable(original) or not isinstance(reasons, (set, frozenset)):
            return False
        if getattr(original, _MARK, False):
            return True

        @functools.wraps(original)
        def finalize(session: Any = None, end_reason: str = "tui_close", *args: Any, **kwargs: Any) -> Any:
            if (isinstance(session, dict) and end_reason in reasons
                    and str(session.get("source") or "").strip().lower() == BIGHELP_SOURCE):
                session["source"] = _DESKTOP_SOURCE
                try:
                    return original(session, end_reason, *args, **kwargs)
                finally:
                    session["source"] = BIGHELP_SOURCE
            return original(session, end_reason, *args, **kwargs)

        setattr(finalize, _MARK, True)
        server._finalize_session = finalize
        logger.info("bighelp chats keep their sessions when the app's connection drops")
        return True
