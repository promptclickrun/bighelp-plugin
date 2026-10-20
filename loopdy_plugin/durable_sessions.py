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
_reopened = False
# Ends Hermes stamps on its own when an app's connection goes away (tui_gateway session_lifecycle).
AUTOMATIC_END_REASONS = ("ws_orphan_reap", "ws_disconnect", "idle_timeout", "lru_evict")


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
    _reopen_in_background()
    return True


def _reopen_in_background() -> None:
    global _reopened
    with _lock:
        if _reopened:
            return
        _reopened = True
    threading.Thread(target=reopen_automatically_ended, name="loopdy-reopen-bighelp-chats", daemon=True).start()


def reopen_automatically_ended(stores: Any | None = None) -> int:
    """Reopens bighelp chats Hermes ended only because the app's connection went away, before this plugin
    kept them open: ended, they're deleted by Hermes' prune (``sessions.auto_prune``) once idle past
    ``retention_days``, a fate Desktop's chats never meet. Once per process; returns how many."""
    reopened = 0
    for db in stores if stores is not None else _profile_stores():
        try:
            rows = db._read_all(
                "SELECT id FROM sessions WHERE source = ? AND ended_at IS NOT NULL AND end_reason IN (%s)"
                % ",".join("?" * len(AUTOMATIC_END_REASONS)), (BIGHELP_SOURCE, *AUTOMATIC_END_REASONS))
            for row in rows:
                db.reopen_session(row["id"])
                reopened += 1
        except Exception:
            logger.warning("Reopening ended bighelp chats unavailable")
        finally:
            if stores is None:
                db.close()
    if reopened:
        logger.info("Reopened %d bighelp chat(s) ended when the app's connection dropped", reopened)
    return reopened


def _profile_stores() -> list[Any]:
    try:
        from hermes_cli.profiles import get_profile_dir, list_profile_names
        from hermes_state import SessionDB
    except Exception:
        return []
    stores = []
    for name in list_profile_names():
        try:
            path = get_profile_dir(name) / "state.db"
            if path.is_file():
                stores.append(SessionDB(db_path=path))
        except Exception:
            continue
    return stores
