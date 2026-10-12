"""Restart the Hermes process serving the bighelp app, in place.

After the app updates this plugin, new code only runs once the process that
serves the native API restarts. Hermes has no API for restarting that process,
so this re-executes it with the exact command line and environment it started
with. The PID stays the same, so launchd, systemd and Hermes Desktop keep
supervising it. Running chats on this process stop; the restarted process frees
their chat locks as it starts (``release_locks_left_by_restart``).
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Any

CAPABILITY = "native-host-restart-v1"
_DELAY_SECONDS = 0.75

logger = logging.getLogger("hermes.plugins.bighelp")
_lock = threading.Lock()
_scheduled = False


def available() -> bool:
    """POSIX only: Windows has no in-place exec."""
    return os.name == "posix" and bool(getattr(sys, "orig_argv", None)) and bool(sys.executable)


def restart_command() -> list[str]:
    # orig_argv keeps "-m hermes_cli.main" and every flag (--isolated, --port, ...).
    return [sys.executable, *sys.orig_argv[1:]]


def release_locks_left_by_restart() -> None:
    """Free the chat locks this process held before an in-place restart.

    Hermes keys each chat's lock (its active-session lease) by PID and process start time, and an
    in-place restart keeps both. So every chat that had a turn before the restart still looked held by
    this process, and its next message was refused as "open in another Hermes window" until Hermes'
    own orphan sweep, up to five minutes later. Run while the process starts, before it accepts a
    connection: no chat in this process holds a lock yet, so every lock in its name is left over.
    Hermes keeps locks taken in the last 30 seconds; its sweep frees those.
    """
    try:
        from hermes_cli.active_sessions import release_orphaned_leases
    except ImportError:
        return
    try:
        released = release_orphaned_leases(set())
    except Exception:
        logger.warning("bighelp: chat locks left by a restart were not released (release_failed)")
        return
    if released:
        logger.info("bighelp: released %d chat lock(s) left by the previous run of this process", released)


def schedule(*, execv=os.execv, delay: float = _DELAY_SECONDS, timer=threading.Timer) -> dict[str, Any]:
    """Re-exec shortly after the HTTP response is written. One restart at a time."""
    global _scheduled
    if not available():
        raise RuntimeError("host_restart_unavailable")
    with _lock:
        if _scheduled:
            return {"restarting": True, "alreadyScheduled": True}
        _scheduled = True
    command = restart_command()

    def restart() -> None:
        global _scheduled
        logger.warning("bighelp: restarting this Hermes process to load an updated plugin")
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        try:
            execv(command[0], command)
        except OSError:
            logger.exception("bighelp: in-place restart failed; the process keeps running")
            with _lock:
                _scheduled = False

    worker = timer(delay, restart)
    worker.daemon = True
    worker.start()
    return {"restarting": True, "alreadyScheduled": False}
