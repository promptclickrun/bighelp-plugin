"""bighelp's iPhone Health, Calendar, Reminders, Location, Appearance, Contacts, Photos, Geofence, Alarm,
Music, Focus, Shortcuts and Navigation tools.

The phone runs each call over the native channel: native_device_tools.py takes it in
Hermes' execution middleware, so the handlers here never run on a working host.
"""

from __future__ import annotations

from typing import Any

from .device_tool_contract import (
    APPEARANCE_COLOR_PATTERN,
    APPEARANCE_MODES,
    HEALTH_TYPES,
    MAX_APPEARANCE_COLOR_LENGTH,
)
from .device_tool_arguments import KIND_OPERATIONS, tool_properties
from .naming import TOOLSET


def _strict_object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def _list_properties(ids_name: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "operation": {"type": "string", "enum": ["list", "create", "update", "delete"]},
        "start": {"type": "string", "format": "date-time"},
        "end": {"type": "string", "format": "date-time"},
        "timeZone": {"type": "string", "minLength": 1, "maxLength": 128},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        "completed": {"type": "boolean"},
        "includeUndated": {"type": "boolean"},
        ids_name: {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "string", "minLength": 1, "maxLength": 512}},
    }
    properties.update(_mutation_properties())
    if ids_name == "calendarIDs":
        for field in ("dueDate", "startDate", "listID", "priority", "completed", "includeUndated"):
            properties.pop(field, None)
    else:
        for field in ("calendarID", "location", "url", "span", "occurrenceStart"):
            properties.pop(field, None)
        # A place for iOS Reminders to watch: create or update; update can also take it away.
        properties["locationTrigger"] = {
            "type": "object", "description": "{name, latitude, longitude, proximity: arrive|leave, radiusMeters: 100-5000}"}
        properties["clearLocationTrigger"] = {"type": "boolean", "enum": [True]}
    return properties


def _mutation_properties() -> dict[str, Any]:
    return {
        "title": {"type": "string", "minLength": 1, "maxLength": 4_000},
        "id": {"type": "string", "minLength": 1, "maxLength": 512},
        "expectedRevision": {"type": "string", "minLength": 1, "maxLength": 512},
        "start": {"type": "string", "format": "date-time"},
        "end": {"type": "string", "format": "date-time"},
        "dueDate": {"type": "string", "format": "date-time"},
        "startDate": {"type": "string", "format": "date-time"},
        "calendarID": {"type": "string", "minLength": 1, "maxLength": 512},
        "listID": {"type": "string", "minLength": 1, "maxLength": 512},
        "location": {"type": "string", "maxLength": 4_000},
        "notes": {"type": "string", "maxLength": 4_000},
        "url": {"type": "string", "maxLength": 4_000},
        "priority": {"type": "integer", "minimum": 0, "maximum": 9},
        "completed": {"type": "boolean"},
        "span": {"type": "string", "enum": ["thisEvent"]},
        "occurrenceStart": {"type": "string", "format": "date-time"},
    }


def _compact(schema: Any, *, nested: bool = False) -> Any:
    """Drop what the model doesn't need to read: these schemas go out with every request to the serving agent.
    The host checks every bound itself (device_tool_arguments.py) and says which field was wrong, so formats,
    patterns, long text limits and obvious minimums only cost bytes here."""
    if isinstance(schema, list):
        return [_compact(item, nested=nested) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = dict(schema)
    for key in ("properties",):
        if key in result:
            result[key] = {name: _compact(value, nested=True) for name, value in result[key].items()}
    if "items" in result:
        result["items"] = _compact(result["items"], nested=True)
    for key in ("minLength", "pattern", "format", "uniqueItems"):
        result.pop(key, None)
    if "enum" in result or result.get("maxLength", 0) > 64:
        result.pop("maxLength", None)
    if result.get("minItems", 2) <= 1:
        del result["minItems"]
    if result.get("type") == "integer" and result.get("minimum") == 1:
        del result["minimum"]  # A count or limit; nobody asks for fewer than one.
    if nested and result.get("type") == "object":
        result.pop("additionalProperties", None)
        if not result.get("required"):
            result.pop("required", None)
    return result


def _list_of(description: str, maximum: int) -> dict[str, Any]:
    return {"type": "array", "maxItems": maximum, "items": {"type": "object"}, "description": description}


def _kind_schema(kind: str) -> dict[str, Any]:
    """The schema of one tool whose operations are described in device_tool_arguments.py."""
    properties: dict[str, Any] = {"operation": {"type": "string", "enum": list(KIND_OPERATIONS[kind])}}
    properties.update(tool_properties(kind))
    if kind == "contacts":
        # Nested shapes are spelled out in words; the host still checks each one.
        properties.update(
            birthday={"type": "string", "description": "YYYY-MM-DD or --MM-DD"},
            phones=_list_of("[{label, value}]", 10), emails=_list_of("[{label, value}]", 10),
            urls=_list_of("[{label, value}]", 5),
            addresses=_list_of("[{label, street, city, region, postalCode, country}]", 5),
        )
    return _strict_object(properties, ["operation"])


# Short on purpose: the schema is sent on every request. skills/bighelp/SKILL.md has the detail.
_ACCESS = " Needs {} on in Device access, app open."
_CONTACTS = (
    "Find, add, change or delete iPhone contacts. Change by the exact id and expectedRevision from list; in update "
    "a list replaces the old one and an empty value clears a field. If a write is uncertain, list first."
    + _ACCESS.format("Contacts")
)
_PHOTOS = (
    "Browse and tidy iPhone Photos. get returns a file path to a small copy: view it with the vision tool. save "
    "adds a picture or video from this host (absolute path, up to 50 MB). update and delete use exact ids; delete "
    "asks the user (cancelled means no). Dates are ISO-8601. After outcome_unknown, read before retrying."
    + _ACCESS.format("Photos")
)
_GEOFENCES = (
    "Get a message in this chat when the user arrives at or leaves a place. create: name, latitude and longitude "
    "or here: true, radiusMeters, on, note (what to do then), repeats (default: gone after the first event). "
    "Only when asked. For a reminder at a place use iphone_reminders."
    + _ACCESS.format("Arriving and leaving")
)
_ALARMS = (
    "bighelp's own alarms and timers (iOS 26+; never the Clock app's). create takes a label and "
    "exactly one of time, date (ISO-8601) or timerSeconds; update replaces the schedule." + _ACCESS.format("Alarms and timers")
)
_MUSIC = (
    "See and control music. Pass id, type and source from search or library to play unchanged. Library changes "
    "(createPlaylist, addToPlaylist, addToLibrary) only when asked. subscription_required means no Apple Music "
    "subscription." + _ACCESS.format("Music")
)
_FOCUS = (
    "Read the user's current Focus (active, name, note for agents) to respect it; it can't be changed. active "
    "false may mean no bighelp filter." + _ACCESS.format("Focus")
)
_SHORTCUTS = (
    "Run an Apple Shortcut the user allowed: list gives the only names you may run; run takes one and optional "
    "input and waits up to a minute. Only when asked. outcome_unknown: it may have run; don't rerun, check first."
    + _ACCESS.format("Shortcuts")
)
_NAVIGATION = (
    "Show the user where a setting or page is in bighelp, when they ask where something is or how to find it. "
    "Call places with their words (query), then open the best match's id with a short note on what they are "
    "looking at (bot: an agent id, for a page inside one bot). It only moves between pages and points at "
    "controls; it never changes a setting or taps Save, Send or Delete. Tell the user what you opened."
)


async def _unreachable_handler(payload: Any, **kwargs: Any) -> str:
    # Reached only when the native middleware did not take the call.
    raise ValueError("verified iPhone tool context is unavailable")


def register(ctx: Any) -> None:
    health_schema = _strict_object(
        {
            "start": {"type": "string", "format": "date-time"},
            "end": {"type": "string", "format": "date-time"},
            "timeZone": {"type": "string", "minLength": 1, "maxLength": 128},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "types": {"type": "array", "minItems": 1, "maxItems": len(HEALTH_TYPES), "items": {"type": "string"},
                      "description": "HealthKit names such as step_count, heart_rate, sleep_analysis"},
        },
        ["start", "end", "timeZone"],
    )
    calendar_schema = _strict_object(_list_properties("calendarIDs"), ["operation"])
    reminder_schema = _strict_object(_list_properties("listIDs"), ["operation"])
    location_schema = _strict_object({"operation": {"type": "string", "enum": ["current"]}}, ["operation"])
    appearance_schema = _strict_object(
        {
            "operation": {"type": "string", "enum": ["get", "set"]},
            "mode": {"type": "string", "enum": list(APPEARANCE_MODES)},
            "color": {"type": "string", "minLength": 1, "maxLength": MAX_APPEARANCE_COLOR_LENGTH,
                      "pattern": APPEARANCE_COLOR_PATTERN},
        },
        ["operation"],
    )
    definitions = (
        (
            "iphone_health",
            "Read raw HealthKit samples from the iPhone (read-only, app in the foreground). Empty or denied does not prove zero.",
            health_schema,
        ),
        (
            "iphone_calendar",
            "List iPhone calendar events, then change them by the exact id and expectedRevision from list. For one recurring event keep occurrenceStart and span=thisEvent. ISO-8601 dates with an offset. If a write is uncertain, read before retrying.",
            calendar_schema,
        ),
        (
            "iphone_reminders",
            "List iPhone reminders, then change them by the exact id and expectedRevision from list. For a place give locationTrigger on create or update; clearLocationTrigger true removes it. Dates are ISO-8601 with an offset. If a write is uncertain, read before retrying.",
            reminder_schema,
        ),
        (
            "iphone_location",
            "Where the user is now, from their iPhone: latitude, longitude, accuracy in meters, time and nearby address. Needs Location on in bighelp, app open in the foreground; call it only when the request needs where they are. precise false means approximate: a rough area of about horizontalAccuracyMeters, no street.",
            location_schema,
        ),
        (
            "iphone_appearance",
            "Change how bighelp looks, only when the user asks: get shows the mode, color and the app's color names; "
            "set takes mode (system, light or dark) and/or color (a name, or #rrggbb). unknown_color: call get and "
            "pick a name. Works while this chat is open, unless turned off in Settings > Device access.",
            appearance_schema,
        ),
        ("iphone_contacts", _CONTACTS, _kind_schema("contacts")),
        ("iphone_photos", _PHOTOS, _kind_schema("photos")),
        ("iphone_geofences", _GEOFENCES, _kind_schema("geofences")),
        ("iphone_alarms", _ALARMS, _kind_schema("alarms")),
        ("iphone_music", _MUSIC, _kind_schema("music")),
        ("iphone_focus", _FOCUS, _kind_schema("focus")),
        ("iphone_shortcuts", _SHORTCUTS, _kind_schema("shortcuts")),
        ("iphone_navigation", _NAVIGATION, _kind_schema("navigation")),
    )
    for name, description, schema in definitions:
        ctx.register_tool(name=name, toolset=TOOLSET, schema={"name": name, "description": description, "parameters": _compact(schema)}, handler=_unreachable_handler, is_async=True)


__all__ = ["register"]
