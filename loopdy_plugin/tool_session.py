"""The chat a tool call belongs to.

Hermes hands every tool handler the call's ``session_id`` (the plugin tool contract). The session
env (``HERMES_SESSION_KEY``, ``HERMES_SESSION_ID``) is only the fallback for Hermes builds that
don't: Hermes 0.22 blanks it for the rest of any turn that starts with a model switch, so tools that
read only the env found no chat in new app chats.
"""
from __future__ import annotations

import contextlib
from typing import Any, Iterator

_MAX_LENGTH = 256


def _clean(value: Any) -> str:
    if (isinstance(value, str) and 0 < len(value) <= _MAX_LENGTH and value.isprintable()
            and not any(character.isspace() for character in value)):
        return value
    return ""


def session_id(context: dict[str, Any] | None, *env_names: str) -> str:
    """The call's own session, else the first non-empty session env value, else ""."""
    if found := _clean((context or {}).get("session_id")):
        return found
    try:
        from gateway.session_context import get_session_env
    except ImportError:
        return ""
    for name in env_names:
        if found := _clean(get_session_env(name, "")):
            return found
    return ""


@contextlib.contextmanager
def bound(session: str) -> Iterator[None]:
    """Address session-scoped live events (``message.reaction``) to ``session`` even when the
    turn's session env was blanked; the previous value comes back on exit."""
    try:
        from gateway.session_context import scoped_current_session_id
    except ImportError:
        scoped_current_session_id = None
    if scoped_current_session_id is None or not session:
        yield
        return
    with scoped_current_session_id(session):
        yield
