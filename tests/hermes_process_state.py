"""Hermes keeps process-wide switches that a test can flip and never flip back.

The first request for a named profile makes the process a multi-profile host for good: Hermes pins the
launch home, freezes the launch environment and stops publishing saved settings to ``os.environ``.
Hermes' own tests reset these before every test (its ``tests/conftest.py``); these tests do the same.
"""
from __future__ import annotations

import sys

_DEFAULTS = (
    ("hermes_constants", "_PINNED_PROCESS_HERMES_HOME", lambda: None),
    ("agent.secret_scope", "_MULTIPLEX_ACTIVE", lambda: False),
    ("agent.secret_scope", "_AUTO_PINNED_HOME", lambda: None),
    ("tui_gateway.launch_profile_policy", "_snapshot", lambda: None),
    ("tui_gateway.server", "_served_profile_homes", set),
)


def reset_multi_profile_hosting() -> None:
    for module_name, name, default in _DEFAULTS:
        module = sys.modules.get(module_name)
        if module is not None and hasattr(module, name):
            setattr(module, name, default())


def single_profile_host(test) -> None:
    """Start the test as a single-profile host and leave it as one."""
    reset_multi_profile_hosting()
    test.addCleanup(reset_multi_profile_hosting)
