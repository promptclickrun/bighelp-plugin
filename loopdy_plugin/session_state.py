"""Read-only access to one Hermes profile's own session store."""

from __future__ import annotations

from contextlib import closing
from typing import Any


class SessionStateNotFound(ValueError):
    """The profile has no Hermes session store."""


def open_profile_store(profile: str, callback: Any, *, read_only: bool = True) -> Any:
    """Use public profile resolution and a separately owned read-only store."""
    from hermes_cli.profiles import get_profile_dir, profile_exists, validate_profile_name
    from hermes_state import SessionDB
    validate_profile_name(profile)
    if not read_only or not profile_exists(profile):
        raise LookupError("Profile unavailable")
    path = get_profile_dir(profile) / "state.db"
    if not path.is_file():
        raise SessionStateNotFound("Profile session store is unavailable")
    with closing(SessionDB(db_path=path, read_only=True)) as db:
        return callback(db)
