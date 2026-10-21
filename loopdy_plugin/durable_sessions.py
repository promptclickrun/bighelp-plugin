"""bighelp's chats outlive a dropped connection, as Hermes Desktop's do.

When an app's connection has been gone a while, Hermes reclaims the session's runtime (``ws_orphan_reap``,
``ws_disconnect``, ``idle_timeout``, …). For a ``desktop`` session it keeps the saved conversation open and
resumable (tui_gateway ``_finalize_session``, Hermes #105588); for any other app's it ends the row. A phone
drops its connection each time someone leaves the app, so bighelp's chats were ended over and over. For
those automatic reasons only, a ``bighelp`` session is finalized the way a Desktop one is. Closing a chat,
archiving or deleting it, and everything else, are unchanged.

Agents also keep working with the app closed. ``message_agent`` runs a teammate's turn in a background process
and ends the asking turn, so the asking chat sits idle while it waits. Hermes reclaimed an idle chat 20 seconds
(``ws_orphan_reap_grace_s``) after the app's connection went: closing its agent killed the teammate mid-turn,
and the reply had no poller left to wake it. The reaper already waits for a chat's ``delegate_task``
subagents; a ``bighelp`` chat now also waits for its background jobs that report back on completion
(``notify_on_complete``), then is reclaimed as before.
"""
from __future__ import annotations

import contextlib
import functools
import logging
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)

BIGHELP_SOURCE = "bighelp"
_DESKTOP_SOURCE = "desktop"
_MARK = "_loopdy_keeps_bighelp_sessions"
_WAIT_MARK = "_loopdy_bighelp_chats_wait_for_their_work"
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
        _wait_for_background_work(server)
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


def _running_processes() -> list[dict]:
    from tools.process_registry import process_registry
    return process_registry.list_sessions()


def _waits_for_background_work(session: Any) -> bool:
    """A bighelp chat whose agent started a background job that reports back when it ends (a teammate's turn
    from ``message_agent``, or ``terminal(background=true, notify_on_complete=true)``), still running."""
    if not isinstance(session, dict) or str(session.get("source") or "").strip().lower() != BIGHELP_SOURCE:
        return False
    owners = getattr(session.get("agent"), "_process_owner_task_ids", None)
    if not owners:
        return False
    try:
        processes = _running_processes()
    except Exception:  # an unreadable registry never holds a chat
        logger.warning("bighelp chat background work unreadable")
        return False
    return any(isinstance(process, dict) and process.get("owner_task_id") in owners
               and process.get("status") == "running" and process.get("notify_on_complete")
               for process in processes)


def _wait_for_background_work(server: Any) -> None:
    """Called under ``_lock``. The reaper defers a detached chat while ``_session_has_active_delegations`` says it owns live work. Hermes
    binds the lifecycle's bodies onto ``tui_gateway.server``'s globals, so the reaper calls the name there."""
    original = getattr(server, "_session_has_active_delegations", None)
    if not callable(original) or getattr(original, _WAIT_MARK, False):
        return

    @functools.wraps(original)
    def has_live_work(sid: str, session: Any = None, *args: Any, **kwargs: Any) -> bool:
        if original(sid, session, *args, **kwargs):
            return True
        if session is None:
            with contextlib.suppress(Exception), server._sessions_lock:
                session = server._sessions.get(sid)
        return _waits_for_background_work(session)

    setattr(has_live_work, _WAIT_MARK, True)
    server._session_has_active_delegations = has_live_work
    logger.info("bighelp chats keep running while their agents wait on background work")


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
