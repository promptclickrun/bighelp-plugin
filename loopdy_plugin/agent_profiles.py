"""Make a new Hermes agent profile the way the bighelp app does, through Hermes' public profile helpers."""
from __future__ import annotations

import re

PROFILE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


def validate_profile_id(profile_id: str) -> None:
    """Raise ``ValueError`` unless Hermes would take this as a new named profile's id."""
    if not isinstance(profile_id, str) or PROFILE_ID.match(profile_id) is None:
        raise ValueError("invalid profile id")
    from hermes_cli.profiles import validate_profile_name

    validate_profile_name(profile_id)
    if profile_id == "default":
        raise ValueError("default is the built-in profile")


def create_agent_profile(*, agent_id: str, display_name: str, description: str, instructions: str) -> None:
    """Create ``agent_id`` with its name, description and SOUL.md.

    Hermes' ``create_profile`` builds the profile in a hidden folder and publishes it with one rename, and
    raises ``FileExistsError`` when the profile is already there, so an existing agent is never overwritten.
    """
    from hermes_cli import profiles
    from utils import atomic_write_text

    path = profiles.create_profile(name=agent_id, no_skills=False, description=description)
    profiles.seed_profile_skills(path, quiet=True)
    if not profiles.check_alias_collision(agent_id):
        profiles.create_wrapper_script(agent_id)
    profiles.set_profile_display_name(agent_id, display_name)
    atomic_write_text(path / "SOUL.md", instructions, preserve_mode=True, create_mode=0o644)


class HermesProfiles:
    """The profile operations the template tools use; tests swap in a fake."""

    def exists(self, profile_id: str) -> bool:
        from hermes_cli.profiles import get_profile_dir, profile_exists

        # A folder without a profile identity still blocks the name, so count it too.
        return profile_exists(profile_id) or get_profile_dir(profile_id).exists()

    def validate(self, profile_id: str) -> None:
        validate_profile_id(profile_id)

    def create(self, *, agent_id: str, display_name: str, description: str, instructions: str) -> None:
        create_agent_profile(agent_id=agent_id, display_name=display_name, description=description,
                             instructions=instructions)
