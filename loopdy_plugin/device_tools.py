"""bighelp's iPhone Health, Calendar, Reminders, Location and Appearance tools.

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
            "types": {"type": "array", "minItems": 1, "maxItems": len(HEALTH_TYPES), "items": {"type": "string", "enum": list(HEALTH_TYPES)}},
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
            "Read bounded raw HealthKit samples from an authenticated iPhone with Health permissions enabled and the phone available in the foreground. Inspect coverage and truncation; an empty or denied result does not prove zero values or full-period totals.",
            health_schema,
            _unreachable_handler,
        ),
        (
            "iphone_calendar",
            "List calendar items on an authenticated iPhone with Calendar permission enabled, then use an exact returned id and expectedRevision for changes. For one recurring event, preserve occurrenceStart and span=thisEvent. If a write is uncertain, reconcile by reading before retrying and never blindly repeat it.",
            calendar_schema,
            _unreachable_handler,
        ),
        (
            "iphone_reminders",
            "List reminders on an authenticated iPhone with Reminders permission enabled, then use an exact returned id and expectedRevision for changes. If a write is uncertain, reconcile by reading before retrying and never blindly repeat it.",
            reminder_schema,
            _unreachable_handler,
        ),
        (
            "iphone_location",
            "Get where the user is right now from their iPhone: latitude, longitude, horizontal accuracy in meters, the time of the fix and the nearby address. Works only after the user turns on Location for this host in bighelp, while the app is open in the foreground. Call it only when the request needs where they are, like places nearby. If precise is false the user kept approximate location: treat it as a rough area of about horizontalAccuracyMeters and don't name a street.",
            location_schema,
            _unreachable_handler,
        ),
        (
            "iphone_appearance",
            "See or change how the user's bighelp app looks: light or dark mode and the chat color (their message "
            "bubbles and buttons). Use it only when the user asks, like \"make it violet, dark mode\". "
            "operation \"get\" returns the current mode and color and the color names the app knows. "
            "operation \"set\" takes mode (system, light or dark) and/or color (one of those names, or #rrggbb), "
            "applies them and returns what the app shows now. An unknown_color result means the name isn't the "
            "app's: call get and pick one, or use #rrggbb. It works only while the user has this chat open in "
            "bighelp, and is on unless they turned it off in Settings > Device access. They can change the look "
            "back in Appearance.",
            appearance_schema,
            _unreachable_handler,
        ),
    )
    for name, description, schema, handler in definitions:
        ctx.register_tool(name=name, toolset=TOOLSET, schema={"name": name, "description": description, "parameters": schema}, handler=handler, is_async=True)


__all__ = ["register"]
