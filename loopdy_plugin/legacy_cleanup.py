"""Clean-up for settings that retired bighelp features left on hosts.

Hosts that paired with the retired bighelp Link have its keys in their Hermes .env.
The plugin no longer reads them. This removes exactly those keys and touches no other
setting.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping


logger = logging.getLogger("hermes.plugins.bighelp")

RETIRED_LINK_KEYS = (
    "LOOPDY_LINK_BASE_URL",
    "LOOPDY_LINK_DEVICE_ID",
    "LOOPDY_LINK_AUTHORIZATION_EPOCH",
    "LOOPDY_LINK_SIGNING_PRIVATE_KEY",
    "LOOPDY_LINK_AGREEMENT_PRIVATE_KEY",
    "LOOPDY_LINK_ACCOUNT_KEY",
)


def remove_retired_link_keys(
    environ: Mapping[str, str] | None = None,
    remover: Callable[[str], bool] | None = None,
) -> int:
    """Remove the retired Link keys from this Hermes home's .env; return how many were there.

    Hermes loads .env into the process environment, so a host without the keys never has
    its .env opened here. A failure is logged and never stops the plugin from loading.
    """
    source = os.environ if environ is None else environ
    present = [key for key in RETIRED_LINK_KEYS if key in source]
    if not present:
        return 0
    if remover is None:
        try:
            from hermes_cli.config import remove_env_value as remover
        except ImportError:
            return 0
    removed = 0
    try:
        for key in present:
            removed += int(bool(remover(key)))
    except Exception:
        logger.warning("bighelp could not remove the retired Link settings (legacy_cleanup_failed)")
        return removed
    if removed:
        logger.info("bighelp removed %d retired Link settings from .env", removed)
    return removed
