"""Hermes' own GitHub Copilot login, run by ``provider_sign_in.py`` in a private terminal.

It prints the link and code `hermes model` prints, waits for GitHub, and saves the token where
`hermes model` does (``COPILOT_GITHUB_TOKEN`` in the profile's env, from ``HERMES_HOME``). The token
is never printed. Exit status 0 means signed in.

Hermes 0.22.6 moved Copilot out of core into the github-copilot provider plugin. There the login is the
provider profile's own device-code hooks, the ones Hermes Desktop's Accounts tab drives, and its save
writes the same ``COPILOT_GITHUB_TOKEN``.
"""
from __future__ import annotations

import sys
import time

_DEVICE_HOOKS = ("start_device_login", "poll_device_login", "save_device_login")


def copilot() -> int:
    try:
        from hermes_cli.copilot_auth import copilot_device_code_login
    except ImportError:
        return profile_device_login("copilot")
    try:
        from hermes_cli.config import save_env_value_secure as save
    except ImportError:
        from hermes_cli.config import save_env_value as save
    token = copilot_device_code_login()
    if not token:
        return 1
    save("COPILOT_GITHUB_TOKEN", token)
    return 0


def profile_device_login(provider_id: str) -> int:
    """A provider profile's device-code sign-in (``ProviderProfile.start_device_login`` and its poll and save).

    Hermes leaves the wait loop to the caller: one poll per interval until the provider approves, refuses or
    the code expires. This process runs in the agent's ``HERMES_HOME``, so the save lands in that profile."""
    from providers import get_provider_profile
    profile = get_provider_profile(provider_id)
    hooks = [getattr(profile, name, None) for name in _DEVICE_HOOKS]
    if not all(map(callable, hooks)):
        print("This Hermes has no sign-in for this provider. Install its provider plugin first.", flush=True)
        return 1
    start, poll, save = hooks
    login = dict(start() or {})
    url, code = login.get("verification_url"), login.get("user_code")
    if not url or not code:
        print("The provider did not return a sign-in code.", flush=True)
        return 1
    print(f"Open this URL in your browser: {url}\nEnter this code: {code}", flush=True)
    interval = max(1, int(login.get("interval") or 5))
    deadline = time.monotonic() + max(1, int(login.get("expires_in") or 900))
    while time.monotonic() < deadline:
        time.sleep(interval)
        try:
            result = dict(poll(login.get("state")) or {})
        except OSError:
            continue  # a dropped poll: GitHub keeps the code until it expires
        status = result.get("status")
        if status == "approved":
            save(result.get("credential"))
            return 0
        if status in ("denied", "expired"):
            print(result.get("message") or "The sign-in did not complete.", flush=True)
            return 1
        interval = max(1, int(result.get("interval") or interval))
    print("The sign-in code expired.", flush=True)
    return 1


FLOWS = {"copilot": copilot}


def main(arguments: list[str]) -> int:
    if len(arguments) != 1 or arguments[0] not in FLOWS:
        return 2
    return FLOWS[arguments[0]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
