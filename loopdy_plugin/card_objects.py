"""Contact, place and music card components (and photo drafts), and which chats may use them.

Older bighelp apps reject an element type they don't know ("Invalid or
unsupported card"). So a card with an object component is accepted only in a
chat whose app said it draws them: before each turn the app's ``people/speaking``
note carries ``renders``, and ``people.PeopleStore`` keeps it per chat. A card
tool call in any other chat (an older app, a scheduled job, another agent's
chat) is refused with a note that tells the agent to use the basic components.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from .loopdy_cards import DRAFT_TYPES, OBJECT_TYPES, BighelpCardError

logger = logging.getLogger("hermes.plugins.bighelp")

CAPABILITY = "native-card-objects-v1"
# The most names one note may list, and the chats a host remembers per agent.
MAX_RENDERS = 8
MAX_SESSIONS = 2_048

REFUSAL_CODE = "card_objects_unavailable"
REFUSAL = (
    "This chat's app can't show {names} components. Rebuild the card with basic components "
    "(text, list, table, metric, badge) and call bighelp_render_card again."
)

# What an agent is told, in the card tool's description.
TOOL_NOTE = (
    " Components contact, place and music show a person, a place or a song with action buttons; "
    "use one after finding it, instead of a text list. A chat whose app can't draw them "
    "refuses the call: then use basic components."
)

Renders = Callable[[str], Iterable[str]]


# What a chat's app may say it draws: the object components and the photo drafts (image_drafts.py).
DECLARABLE = OBJECT_TYPES | DRAFT_TYPES


def known(names: Iterable[Any]) -> frozenset[str]:
    """The object and draft components among ``names``; anything else is ignored."""
    return frozenset(name for name in names if isinstance(name, str) and name in DECLARABLE)


def used_in(payload: Any) -> frozenset[str]:
    """Object component types an agent's card document asks for.

    Reads the raw document without validating it, so the gate can answer
    before validation and never lets a malformed card slip an object through.
    """
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, dict):
        return frozenset()
    return frozenset(
        element["type"] for element in elements.values()
        if isinstance(element, dict) and element.get("type") in OBJECT_TYPES
    )


def require(payload: Any, session_id: str, renders: Renders) -> bool:
    """Refuse a card whose objects this chat's app hasn't said it draws.

    Returns whether the card holds objects. Raises ``BighelpCardError``
    (``card_objects_unavailable``) otherwise.
    """
    wanted = used_in(payload)
    if not wanted:
        return False
    try:
        declared = known(renders(session_id)) if session_id else frozenset()
    except Exception as error:  # a broken store must refuse, never show an unsupported card
        logger.debug("bighelp card objects lookup failed: %s", type(error).__name__)
        declared = frozenset()
    missing = sorted(wanted - declared)
    if missing:
        raise BighelpCardError(REFUSAL_CODE, REFUSAL.format(names=", ".join(missing)))
    return True


def declared_for(session_id: str) -> frozenset[str]:
    """What the app of this chat (of the agent whose turn is running) draws."""
    from .people import SESSION_ID, current_store
    if SESSION_ID.fullmatch(session_id or "") is None:
        return frozenset()
    return current_store().renders(session_id)
