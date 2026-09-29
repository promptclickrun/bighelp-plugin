"""The bighelp names agents and people see, and the older names that keep working.

Hermes still knows this plugin as ``loopdy`` (its folder, routes, data and the
``loopdy:`` skill prefix): renaming those would cut off app builds that are
already installed. Everything an agent or a person reads says bighelp.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("hermes.plugins.bighelp")

TOOLSET = "bighelp"
# Saved tool settings and scheduled jobs from before 3.0.0 name the toolset "loopdy".
LEGACY_TOOLSET = "loopdy"

CLI_COMMAND = "bighelp"
LEGACY_CLI_COMMAND = "loopdy"


def register_legacy_toolset_alias() -> bool:
    """Let "loopdy" in saved settings and scheduled jobs resolve to the bighelp tools."""
    try:
        from tools.registry import registry
    except ImportError:
        return False
    register = getattr(registry, "register_toolset_alias", None)
    if not callable(register):
        return False
    try:
        register(LEGACY_TOOLSET, TOOLSET)
    except Exception:
        logger.warning("bighelp toolset alias unavailable")
        return False
    return True


def env(name: str, default: str | None = None) -> str | None:
    """``BIGHELP_<name>``, else the ``LOOPDY_<name>`` a host set before 3.0.0."""
    value = os.environ.get(f"BIGHELP_{name}")
    if value is None:
        value = os.environ.get(f"LOOPDY_{name}")
    return default if value is None else value


# Cards keep their original schema names on the wire, because app builds already
# on phones read them. Agents write the bighelp names; both are accepted.
_CARD_SCHEMAS = {"bighelp.generative_ui": "loopdy.generative_ui", "bighelp.card": "loopdy.card"}
GENERATIVE_UI_SCHEMA = "bighelp.generative_ui"
CARD_SCHEMA = "bighelp.card"


def card_input(payload):
    """An agent's card arguments, with the schema name the app reads."""
    if isinstance(payload, dict) and payload.get("schema") in _CARD_SCHEMAS:
        return {**payload, "schema": _CARD_SCHEMAS[payload["schema"]]}
    return payload
