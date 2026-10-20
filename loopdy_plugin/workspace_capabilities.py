"""Run Hermes' own profile-aware helpers inside one agent's profile."""
from __future__ import annotations

from contextlib import contextmanager


@contextmanager
def profile_scope(agent_id: str):
    """Use Hermes' context-local home API, never process environment/globals."""
    from hermes_cli.profiles import get_profile_dir, profile_exists
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    if not profile_exists(agent_id):
        raise ValueError("The selected Hermes profile no longer exists")
    token = set_hermes_home_override(get_profile_dir(agent_id))
    try:
        yield
    finally:
        reset_hermes_home_override(token)
