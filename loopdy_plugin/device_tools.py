"""bighelp's iPhone Health, Calendar, Reminders, Location, Appearance, Contacts, Photos, Geofence, Alarm,
Music, Focus and Shortcuts tools.

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
from .device_tool_arguments import KIND_OPERATIONS, LOCATION_TRIGGER, tool_properties
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
        properties["locationTrigger"] = LOCATION_TRIGGER
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


def _kind_schema(kind: str) -> dict[str, Any]:
    """The schema of one tool whose operations are described in device_tool_arguments.py."""
    properties: dict[str, Any] = {"operation": {"type": "string", "enum": list(KIND_OPERATIONS[kind])}}
    properties.update(tool_properties(kind))
    return _strict_object(properties, ["operation"])


_CONTACTS = (
    "Find, add, change or remove people in the user's iPhone Contacts. operation list takes an optional query "
    "(a name, an email or a phone number) and returns each person with id and revision. To change or delete, use "
    "that exact id and expectedRevision; stale_revision means they changed it: list again. In update, a list "
    "you give replaces the whole list, and an empty value clears a field. Contact notes aren't available. "
    "Works only after the user turns on Contacts in bighelp > Settings > Device access, while the app is open. "
    "With limited access you only see the people they shared. If a write is uncertain, list before you retry; "
    "never repeat it blindly."
)
_PHOTOS = (
    "Look through, save to, and tidy the user's iPhone Photos library. list (newest first, filter by dates, "
    "album, mediaType or favorites) and albums find items. get returns one item's details and a file path "
    "to a small copy of the picture on this host: look at it with the vision tool. save puts a picture or video from this host (an absolute path, up to 50 MB) into Photos, "
    "optionally in an album. update changes favorite and albums with the exact id and expectedRevision. "
    "createAlbum makes an album. delete removes items by id: iOS asks the user to confirm, and Don't Allow "
    "comes back as cancelled, so only delete what they asked you to. Works only after the user turns on Photos "
    "in bighelp > Settings > Device access, while the app is open. If a write is uncertain, read before you retry."
)
_GEOFENCES = (
    "Watch places on the user's iPhone and get a message in this chat when they arrive at or leave one. create "
    "takes a name and either latitude and longitude or here: true (where the phone is now), a radiusMeters "
    "(100 to 5000), on (arrive, leave or both) and a note for what you want to do then; repeats false (the "
    "default) removes the place after its first event. list shows this host's places; delete removes one by id. "
    "Create a place only when the user asks for one. Setting it up needs the user to turn on Arriving and leaving "
    "in bighelp > Settings > Device access, while the app is open; after that, events reach this chat even with "
    "bighelp closed if they allowed Always location. To remind them at a place, use iphone_reminders with "
    "locationTrigger instead."
)
_ALARMS = (
    "Set, change, list or remove alarms and timers on the user's iPhone or iPad (iOS 26 and later). Only the "
    "alarms and timers bighelp made are visible; the Clock app's own never are. create takes a label and exactly "
    "one of time {hour, minute} (add weekdays to repeat weekly; without them it rings once at the next such time), "
    "date (one time) or timerSeconds (5 to 86400). update takes the id and the new schedule replaces the old one. "
    "Works only after the user turns on Alarms and timers in bighelp > Settings > Device access, while the app "
    "is open. If a write is uncertain, list before you retry."
)
_MUSIC = (
    "See and control music on the user's iPhone. nowPlaying shows what plays. search (query, optional types, "
    "source and limit) and library (playlists or recent) return results with id, type and source. play takes "
    "those three exactly as a result gave them, or a query for the best song match; control is pause, resume, "
    "next, previous or stop. createPlaylist, addToPlaylist and addToLibrary change the user's library: do it only "
    "when they ask. Playing the Apple Music catalog needs their subscription; subscription_required means they "
    "have none. Works only after the user turns on Music in bighelp > Settings > Device access, while the app "
    "is open."
)
_FOCUS = (
    "Read the user's iPhone Focus, only to respect it (for example, keep messages short when they're busy). "
    "current returns active, and while active the Focus name and the user's note for agents. You can't turn a "
    "Focus on or off. A Focus without bighelp's filter reads as active false, so that doesn't mean they're free. "
    "Works only after the user turns on Focus in bighelp > Settings > Device access, while the app is open."
)
_SHORTCUTS = (
    "Run an Apple Shortcut on the user's iPhone. list returns the only shortcut names you may run (the user "
    "chose them in bighelp > Settings > Device access > Shortcuts); run takes exactly one of those names and an "
    "optional text input, opens Shortcuts for up to 60 seconds, and returns status (completed, cancelled or "
    "failed) and the shortcut's output. A shortcut can do anything on the phone, so run one only when the user "
    "asked for it, and never run it again to see if it worked. Works only after the user turns on Shortcuts, "
    "while the app is open."
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
            "List reminders on an authenticated iPhone with Reminders permission enabled, then use an exact returned id and expectedRevision for changes. To remind the user at a place, give locationTrigger (name, latitude, longitude, proximity arrive or leave, optional radiusMeters) on create or update; update with clearLocationTrigger true removes it. If a write is uncertain, reconcile by reading before retrying and never blindly repeat it.",
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
        ("iphone_contacts", _CONTACTS, _kind_schema("contacts"), _unreachable_handler),
        ("iphone_photos", _PHOTOS, _kind_schema("photos"), _unreachable_handler),
        ("iphone_geofences", _GEOFENCES, _kind_schema("geofences"), _unreachable_handler),
        ("iphone_alarms", _ALARMS, _kind_schema("alarms"), _unreachable_handler),
        ("iphone_music", _MUSIC, _kind_schema("music"), _unreachable_handler),
        ("iphone_focus", _FOCUS, _kind_schema("focus"), _unreachable_handler),
        ("iphone_shortcuts", _SHORTCUTS, _kind_schema("shortcuts"), _unreachable_handler),
    )
    for name, description, schema, handler in definitions:
        ctx.register_tool(name=name, toolset=TOOLSET, schema={"name": name, "description": description, "parameters": schema}, handler=handler, is_async=True)


__all__ = ["register"]
