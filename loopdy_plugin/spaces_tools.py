"""The bot's tools for spaces on the person's Today (spaces.py, docs/SPACES.md).

``bighelp_draft_space`` drafts or changes a space in a bighelp chat and shows its card there; the person adds
it with Add to Today, and nothing runs before that. ``bighelp_update_space`` gives a space new cards or page
data from anywhere (a chat, a scheduled run, a helper). ``bighelp_space_settings`` lists a bot's spaces and
reads (or, when the person asked, sets) their size and whether they're hidden.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from . import card_objects, html_apps, spaces, tool_session
from .generative_ui import rendered_card_delivery
from .image_drafts import _BRIDGE
from .loopdy_cards import SPACE_TYPES, BighelpCardError, render_card
from .naming import TOOLSET

logger = logging.getLogger("hermes.plugins.bighelp")

UPDATE_NOTE = "This chat's Bighelp app can't show spaces. Ask the person to update Bighelp."


class _ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _session(context: dict[str, Any]) -> str:
    session_id = tool_session.session_id(context, "HERMES_SESSION_ID")
    if html_apps.SESSION_ID.fullmatch(session_id or "") is None:
        raise _ToolError("spaces_unavailable", "Spaces are drafted only in a Bighelp chat with the person.")
    return session_id


def _declared(session_id: str) -> bool:
    try:
        return bool(card_objects.declared_for(session_id) & SPACE_TYPES)
    except Exception as error:  # a broken store refuses, never shows a card the app can't draw
        logger.debug("Bighelp space renders lookup failed: %s", type(error).__name__)
        return False


def _space_id(value: Any) -> str:
    if not isinstance(value, str) or spaces.SPACE_ID.fullmatch(value) is None:
        raise _ToolError("invalid_arguments", "space_id is the space_id of a space of yours.")
    return value


def _mine(store: spaces.SpaceStore, space_id: str) -> spaces.Space:
    space = store.get(space_id)
    if space is None or space.state == "deleting":
        raise _ToolError("space_not_found", "That space_id isn't one of your spaces.")
    return space


def _page(args: dict[str, Any], profile: str) -> bytes:
    try:
        return html_apps._page(args, profile)
    except html_apps._ToolError as error:
        raise _ToolError(error.code, error.message) from None


def _draft_note(space: spaces.Space) -> str:
    if space.state == "draft":
        return ("The person adds it with Add to Today. Nothing runs until then. Put display_markdown in your "
                "reply once.")
    note = "It's on the person's Today already: the new title, card and page show now."
    if space.pending is not None:
        note += (" The new routine starts when the person taps Add to Today on this card; the old one keeps "
                 "running until then.")
    return note + " Put display_markdown in your reply once."


def draft(args: Any, context: dict[str, Any], *, now: Callable[[], datetime], profile: str) -> dict[str, Any]:
    allowed = {"title", "description", "card", "sizes", "size", "html", "path", "data", "routine", "space_id"}
    if not isinstance(args, dict) or set(args) - allowed or not {"title", "description", "card", "routine"} <= set(args):
        raise _ToolError("invalid_arguments", "Arguments are title, description, card, routine and exactly one of "
                         "html or path; optionally sizes, size, data and space_id.")
    session_id = _session(context)
    if not _declared(session_id):
        raise _ToolError("spaces_unavailable", UPDATE_NOTE)
    space_id = _space_id(args["space_id"]) if args.get("space_id") is not None else None
    store = spaces.current_store()
    if space_id is not None:
        _mine(store, space_id)
    size = args.get("size", "wide")
    if size not in spaces.SIZES:
        raise _ToolError("invalid_arguments", "size is small, wide or tall.")
    if args.get("sizes") is not None and not isinstance(args["sizes"], dict):
        raise _ToolError("invalid_arguments", "sizes is an object with a card for small, wide or tall.")
    try:
        title = spaces.line(args["title"], "title", spaces.MAX_TITLE)
        description = spaces.line(args["description"], "description", spaces.MAX_DESCRIPTION)
    except spaces.SpaceError as error:
        raise _ToolError("invalid_arguments", error.message) from None
    try:
        routine = spaces.check_routine(args["routine"], profile, now=store.now(), home=store.home)
    except spaces.SpaceError as error:
        raise _ToolError(error.code, error.message) from None
    page = _page(args, profile)
    try:
        space = spaces.save_draft(store, profile, session_id=session_id, space_id=space_id, title=title,
                                  description=description, card=args["card"], sizes=args.get("sizes"), size=size,
                                  html=page, data=args.get("data"), routine=routine)
        card = render_card(spaces.draft_card(space), now=now(), allow_spaces=True)
    except spaces.SpaceError as error:
        raise _ToolError(error.code, error.message) from None
    except BighelpCardError as error:
        raise _ToolError(error.code, str(error)) from None
    return {**rendered_card_delivery(card), "space_id": space.space_id, "revision": space.revision,
            "note": _draft_note(space)}


def update(args: Any, context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict) or set(args) - {"space_id", "card", "sizes", "data"} or "space_id" not in args:
        raise _ToolError("invalid_arguments", "Arguments are space_id and at least one of card, sizes or data.")
    space_id = _space_id(args["space_id"])
    if all(args.get(key) is None for key in ("card", "sizes", "data")):
        raise _ToolError("invalid_arguments", "Give at least one of card, sizes or data.")
    store = spaces.current_store()
    _mine(store, space_id)
    try:
        return spaces.apply_to(store, space_id, card=args.get("card"), sizes=args.get("sizes"),
                               data=args.get("data"), source="tool")
    except spaces.SpaceError as error:
        raise _ToolError(error.code, error.message) from None


def _entry(space: spaces.Space) -> dict[str, Any]:
    """What the bot reads about one of its spaces. The problem note (what its script wrote to stderr) is
    for the bot only; the app never gets it."""
    routine = space.pending or space.routine
    return {"space_id": space.space_id, "title": space.title, "state": space.state, "size": space.size,
            "hidden": space.hidden, "updates": routine["updates"], "uses_ai": routine["kind"] == "ai",
            "updated_at": spaces._timestamp(space.card_updated), "last_problem": space.last_problem,
            "problem_note": space.problem_note}


def settings(args: Any, context: dict[str, Any]) -> dict[str, Any]:
    args = {} if args is None else args
    if not isinstance(args, dict) or set(args) - {"space_id", "size", "hidden"}:
        raise _ToolError("invalid_arguments", "Arguments are space_id and, only when the person asked, size or "
                         "hidden.")
    store = spaces.current_store()
    if args.get("space_id") is None:
        if args.get("size") is not None or args.get("hidden") is not None:
            raise _ToolError("invalid_arguments", "Give space_id with size or hidden.")
        return {"spaces": [_entry(space) for space in store.all() if space.state != "deleting"]}
    space = _mine(store, _space_id(args["space_id"]))
    changes = {key: args[key] for key in ("size", "hidden") if args.get(key) is not None}
    if changes:
        try:
            space = spaces.change(store, space.space_id, changes)
        except spaces.SpaceError as error:
            code = "invalid_arguments" if error.code == "invalid_request" else error.code
            raise _ToolError(code, error.message) from None
    return {**_entry(space), "changed": sorted(changes)}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _handler(work: Callable[..., dict[str, Any]], **options: Any) -> Callable[..., str]:
    def handle(args: Any = None, **context: Any) -> str:
        try:
            return _json(work(args, context, **options))
        except _ToolError as error:
            return _json({"success": False, "error": error.code, "message": error.message})
        except (ValueError, RecursionError) as error:  # what a model's odd input can still raise (a NUL)
            logger.warning("Bighelp space tool refused its input: %s", type(error).__name__)
            return _json({"success": False, "error": "invalid_request",
                          "message": "An argument isn't usable (a control character or too deeply nested)."})
        except (OSError, sqlite3.Error) as error:
            logger.warning("Bighelp space tool failed: %s", type(error).__name__)
            return _json({"success": False, "error": "spaces_unavailable",
                          "message": "Spaces aren't available on this computer right now."})
    return handle


DRAFT_DESCRIPTION = (
    "Draft a space for the person's Today in the Bighelp app: a live panel (a commute time, a tracker, a "
    "garden's soil moisture) that keeps itself up to date. Use it only when the person asks for something on "
    "Today. A space has three parts. card: the panel, a loopdy.card (bighelp.card accepted) with real, current "
    "data, data_sources [] and basic components only (text, metric, list, table, chart, badge, progress); keep "
    "it small, it's a panel. sizes: optional cards made for small (a square), wide (full width, same height) or "
    "tall (full width, twice as high); card is used for any size without one. size: the size you designed for "
    "(default wide). html or path: the page the panel opens, one self-contained page made like a "
    "bighelp_show_app page (window.bighelp.getData and onData read its data; it can't send answers). data: the "
    "page's first data. routine: how it stays up to date. schedule is a repeating Hermes schedule (every 15m, "
    "every 2h, 0 7 * * 1-5). Prefer script: a .py or .sh file you wrote in your working folder that prints one "
    'JSON object, {"card": …, "sizes": {…}, "data": …} (any of them) or {"skip": true} when nothing changed. It '
    'gets {"spaceId", "size", "hidden", "lastData", "lastUpdatedAt"} on stdin, runs in your working folder for '
    "at most 120 seconds and may print at most 1,310,720 bytes; updates then spend no AI, at most every 5 "
    "minutes. Don't call AI APIs from it. The person approves the script as it is when they add the space: if you "
    "change the script, draft the space again; the person adds it again, and until then it doesn't run "
    "(script_changed). Use prompt only when a script can't do it: every update then runs you "
    "with that prompt (it uses AI, at most every 60 minutes). "
    "Nothing runs until the person taps Add to Today on the card. space_id: a space of yours to change; a live "
    "space shows its new title, card and page at once, and a changed routine waits for the person's Add. Put "
    "the returned display_markdown in your reply once."
    + _BRIDGE
)
UPDATE_DESCRIPTION = (
    "Give one of your spaces on the person's Today a new card (card, sizes) or its page new data (data), from "
    "any chat or scheduled run. Cards follow the same rules as in bighelp_draft_space: real, current data, "
    "data_sources [], basic components. Call bighelp_space_settings first to make the card for the space's "
    "size. Today shows the update within a minute; don't post a card in the chat."
    + _BRIDGE
)
SETTINGS_DESCRIPTION = (
    "List your spaces on the person's Today (without space_id), or read one: its state (draft or live), size "
    "(small, wide or tall), whether it's hidden, how often it updates and whether that uses AI, when it last "
    "updated and its last problem (with problem_note: what its script printed to stderr). Set size or hidden "
    "only when the person asked for it."
    + _BRIDGE
)

_DATA_TYPES = ["object", "array", "string", "number", "boolean", "null"]
_SPACE_ID_SCHEMA = {"type": "string", "pattern": "^s_[0-9a-f]{32}$"}
_SIZE_SCHEMA = {"type": "string", "enum": list(spaces.SIZES)}


def draft_parameters() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "required": ["title", "description", "card", "routine"], "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": spaces.MAX_TITLE},
        "description": {"type": "string", "minLength": 1, "maxLength": spaces.MAX_DESCRIPTION,
                        "description": "What it shows, one line, on the draft card."},
        "card": {"type": "object", "description": "The panel: a loopdy.card (bighelp.card accepted) with real, "
                 "current data and data_sources []. Basic components only."},
        "sizes": {"type": "object", "additionalProperties": False,
                  "properties": {size: {"type": "object"} for size in spaces.SIZES},
                  "description": "Optional cards made for one size; card is used for the others."},
        "size": {**_SIZE_SCHEMA, "description": "The size you designed for; default wide."},
        "html": {"type": "string", "description": "The page opened from the panel. Give this or path."},
        "path": {"type": "string", "description": "An .html or .htm file in your working folder."},
        "data": {"type": _DATA_TYPES, "description": "The page's first data."},
        "routine": {"type": "object", "additionalProperties": False, "required": ["schedule"], "properties": {
            "schedule": {"type": "string", "maxLength": spaces.MAX_SCHEDULE,
                         "description": "A repeating Hermes schedule: every 15m, 0 7 * * 1-5."},
            "script": {"type": "string", "description": "A .py or .sh file in your working folder that prints the "
                       "update JSON. Preferred: updates spend no AI. If you change it later, draft the space "
                       "again; the person adds it again."},
            "prompt": {"type": "string", "maxLength": spaces.MAX_PROMPT, "description": "Only when a script can't "
                       "do it: what to do each update. Every update then uses AI."}}},
        "space_id": {**_SPACE_ID_SCHEMA, "description": "A space of yours to change."}}}


def update_parameters() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["space_id"], "properties": {
        "space_id": dict(_SPACE_ID_SCHEMA), "card": {"type": "object"}, "sizes": {"type": "object"},
        "data": {"type": _DATA_TYPES}}}


def settings_parameters() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "properties": {
        "space_id": dict(_SPACE_ID_SCHEMA),
        "size": {**_SIZE_SCHEMA, "description": "Only when the person asked for this size."},
        "hidden": {"type": "boolean", "description": "Only when the person asked."}}}


def register(ctx: Any) -> None:
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    profile = str(getattr(ctx, "profile_name", "default") or "default")
    tools = (
        ("bighelp_draft_space", DRAFT_DESCRIPTION, draft_parameters(), _handler(draft, now=clock, profile=profile),
         "🪟"),
        ("bighelp_update_space", UPDATE_DESCRIPTION, update_parameters(), _handler(update), "🔄"),
        ("bighelp_space_settings", SETTINGS_DESCRIPTION, settings_parameters(), _handler(settings),
         "📐"),
    )
    for name, description, parameters, handler, emoji in tools:
        ctx.register_tool(name=name, toolset=TOOLSET,
                          schema={"name": name, "description": description, "parameters": parameters},
                          handler=handler, emoji=emoji)
