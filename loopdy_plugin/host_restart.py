"""Restart the Hermes process serving the bighelp app, in place.

After the app updates this plugin, new code only runs once the process that
serves the native API restarts. Hermes has no API for restarting that process,
so this re-executes it with the exact command line and environment it started
with. The PID stays the same, so launchd, systemd and Hermes Desktop keep
supervising it. Running chats on this process stop.
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
