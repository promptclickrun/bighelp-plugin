"""Keeps host telemetry off the network in a test run.

Import this from every test module that registers the plugin or runs ManagedNotifications:

    import telemetry_off  # noqa: F401

It does three things, so that no test can post a batch even if one of them is undone:
- sets ``BIGHELP_TELEMETRY=0`` unless the run already chose a value (the documented test command and the CI
  workflows set it too),
- replaces the default transport with one that sends nothing,
- gives the shared stream the same transport.

Tests of the telemetry module itself clear the variable and bring their own transport. ``REAL_POST`` is the
real transport, for the tests that check what it would send with the network call itself replaced.
"""
from __future__ import annotations

import os

from loopdy_plugin import host_telemetry

os.environ.setdefault("BIGHELP_TELEMETRY", "0")
REAL_POST = host_telemetry._post


def discard(body, timeout):
    return 0, None


host_telemetry._post = discard
host_telemetry.set_transport(discard)
