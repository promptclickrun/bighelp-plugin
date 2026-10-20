"""Hermes' own GitHub Copilot login, run by ``provider_sign_in.py`` in a private terminal.

It prints the link and code `hermes model` prints, waits for GitHub, and saves the token where
`hermes model` does (``COPILOT_GITHUB_TOKEN`` in the profile's env, from ``HERMES_HOME``). The token
is never printed. Exit status 0 means signed in.
"""
from __future__ import annotations

import sys


def copilot() -> int:
    from hermes_cli.copilot_auth import copilot_device_code_login
    try:
        from hermes_cli.config import save_env_value_secure as save
    except ImportError:
        from hermes_cli.config import save_env_value as save
    token = copilot_device_code_login()
    if not token:
        return 1
    save("COPILOT_GITHUB_TOKEN", token)
    return 0


FLOWS = {"copilot": copilot}


def main(arguments: list[str]) -> int:
    if len(arguments) != 1 or arguments[0] not in FLOWS:
        return 2
    return FLOWS[arguments[0]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
