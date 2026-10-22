"""Arguments of the iPhone tools beyond Calendar and Reminders.

One description of each operation's arguments serves two readers: the tool schemas the agent sees
(device_tools.py) and the check that runs before a request leaves the host (device_tool_contract.py).
The phone checks the same limits (docs/IPHONE_DEVICE_TOOLS.md), so a request the host accepts is
never refused for its shape after it reaches the phone.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Callable

_ID = {"type": "string", "minLength": 1, "maxLength": 512}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_BIRTHDAY = r"^(\d{4}-\d{2}-\d{2}|--\d{2}-\d{2})$"
_BIRTHDAY_OR_EMPTY = r"^(|\d{4}-\d{2}-\d{2}|--\d{2}-\d{2})$"
_TOKEN = r"^[A-Za-z][A-Za-z0-9_-]{0,23}$"
MAX_PHOTO_PATH_LENGTH = 4_096


@dataclass(frozen=True)
class Operation:
    """One `<kind>.<operation>`: its argument fields, which of them are required, and a rule across them."""

    properties: dict[str, dict[str, Any]] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    rule: Callable[[dict[str, Any]], None] | None = None


def _text(maximum: int, minimum: int = 1, **extra: Any) -> dict[str, Any]:
    return {"type": "string", "minLength": minimum, "maxLength": maximum, **extra}


def _choice(*values: str) -> dict[str, Any]:
    return {"type": "string", "minLength": min(map(len, values)), "maxLength": max(map(len, values)), "enum": list(values)}


def _whole(minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _decimal(minimum: float, maximum: float) -> dict[str, Any]:
    return {"type": "number", "minimum": minimum, "maximum": maximum}


def _list(item: dict[str, Any], minimum: int, maximum: int, *, unique: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "array", "minItems": minimum, "maxItems": maximum, "items": item}
    if unique:
        value["uniqueItems"] = True
    return value


def _object(properties: dict[str, dict[str, Any]], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_BOOLEAN = {"type": "boolean"}
_TRUE_ONLY = {"type": "boolean", "enum": [True]}

# What a place on a reminder needs; the same shape as the phone's `locationTrigger`.
LOCATION_TRIGGER = _object(
    {
        "name": _text(100),
        "latitude": _decimal(-90, 90),
        "longitude": _decimal(-180, 180),
        "radiusMeters": _whole(100, 5_000),
        "proximity": _choice("arrive", "leave"),
    },
    ("name", "latitude", "longitude", "proximity"),
)


def check_value(value: Any, schema: dict[str, Any], name: str) -> None:
    """Raise ValueError unless `value` fits the JSON-schema subset used above."""
    invalid = ValueError(f"bighelp device tool {name} is invalid")
    kind = schema["type"]
    if kind == "string":
        if not isinstance(value, str) or not schema["minLength"] <= len(value) <= schema["maxLength"]:
            raise invalid
        if "enum" in schema and value not in schema["enum"]:
            raise invalid
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise invalid
        if schema.get("format") == "date-time":
            parse_date_time(value, name)
    elif kind == "integer":
        if type(value) is not int or not schema["minimum"] <= value <= schema["maximum"]:
            raise invalid
    elif kind == "number":
        if type(value) not in (int, float) or not math.isfinite(value) \
                or not schema["minimum"] <= value <= schema["maximum"]:
            raise invalid
    elif kind == "boolean":
        if type(value) is not bool or ("enum" in schema and value not in schema["enum"]):
            raise invalid
    elif kind == "array":
        if not isinstance(value, list) or not schema["minItems"] <= len(value) <= schema["maxItems"]:
            raise invalid
        for item in value:
            check_value(item, schema["items"], name)
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise invalid
    elif kind == "object":
        if not isinstance(value, dict) or not set(value).issubset(schema["properties"]) \
                or not set(schema["required"]).issubset(value):
            raise invalid
        for key, item in value.items():
            check_value(item, schema["properties"][key], f"{name}.{key}")
    else:  # A schema in this file that the checker can't read is a bug, never an input problem.
        raise AssertionError(kind)


def parse_date_time(raw: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"bighelp device tool {name} is invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"bighelp device tool {name} is invalid")
    return parsed


def _fail(name: str) -> ValueError:
    return ValueError(f"bighelp device tool {name} is invalid")


def _changes(arguments: dict[str, Any], *identity: str) -> None:
    if not set(arguments) - set(identity):
        raise ValueError("bighelp device tool update has nothing to change")


# Contacts ---------------------------------------------------------------------------------------

def _contact_fields(*, clearable: bool) -> dict[str, dict[str, Any]]:
    """On update an empty string or list clears a field; on create it has no meaning."""
    least = 0 if clearable else 1
    labelled = lambda maximum: _object({"label": _text(40), "value": _text(maximum)}, ("value",))  # noqa: E731
    address = _object({
        "label": _text(40), "street": _text(200), "city": _text(200), "region": _text(200),
        "postalCode": _text(40), "country": _text(200),
    })
    return {
        "givenName": _text(200, least), "familyName": _text(200, least), "organization": _text(200, least),
        "nickname": _text(200, least), "jobTitle": _text(200, least),
        "birthday": _text(10, least, pattern=_BIRTHDAY_OR_EMPTY if clearable else _BIRTHDAY),
        "phones": _list(labelled(64), least, 10), "emails": _list(labelled(320), least, 10),
        "urls": _list(labelled(2_000), least, 5), "addresses": _list(address, least, 5),
    }


def _check_contact(arguments: dict[str, Any]) -> None:
    birthday = arguments.get("birthday")
    if birthday:
        # `YYYY-MM-DD`, or `--MM-DD` when the year is unknown (February 29 is a real birthday).
        parts = birthday.split("-")
        year, month, day = (2000, parts[2], parts[3]) if birthday.startswith("--") else parts
        try:
            date(int(year), int(month), int(day))
        except ValueError:
            raise _fail("birthday") from None
    for address in arguments.get("addresses", []):
        if not any(address.get(part) for part in ("street", "city", "region", "postalCode", "country")):
            raise _fail("addresses")


def _check_contact_create(arguments: dict[str, Any]) -> None:
    if not any(arguments.get(name) for name in ("givenName", "familyName", "organization")):
        raise ValueError("bighelp device tool contact needs a name or an organization")
    _check_contact(arguments)


def _check_contact_update(arguments: dict[str, Any]) -> None:
    _changes(arguments, "id", "expectedRevision")
    _check_contact(arguments)


# Photos -----------------------------------------------------------------------------------------

def _check_photo_list(arguments: dict[str, Any]) -> None:
    if ("start" in arguments) != ("end" in arguments):
        raise ValueError("bighelp device tool start and end go together")
    if "start" in arguments and parse_date_time(arguments["end"], "end") <= parse_date_time(arguments["start"], "start"):
        raise _fail("end")


def _check_photo_update(arguments: dict[str, Any]) -> None:
    _changes(arguments, "id", "expectedRevision")


# Arriving and leaving ---------------------------------------------------------------------------

def _check_geofence(arguments: dict[str, Any]) -> None:
    where = {"latitude", "longitude"} & set(arguments)
    if arguments.get("here") is True:
        if where:
            raise ValueError("bighelp device tool place is either here or coordinates")
    elif where != {"latitude", "longitude"}:
        raise ValueError("bighelp device tool place needs latitude and longitude, or here")


# Alarms and timers ------------------------------------------------------------------------------

_ALARM_FIELDS = {
    "label": _text(60),
    "time": _object({"hour": _whole(0, 23), "minute": _whole(0, 59)}, ("hour", "minute")),
    "weekdays": _list(_choice(*_WEEKDAYS), 1, 7, unique=True),
    "date": {"type": "string", "minLength": 1, "maxLength": 80, "format": "date-time"},
    "timerSeconds": _whole(5, 86_400),
}


def _check_schedule(arguments: dict[str, Any], *, exactly_one: bool) -> None:
    chosen = [name for name in ("time", "date", "timerSeconds") if name in arguments]
    if len(chosen) > 1 or (exactly_one and not chosen):
        raise ValueError("bighelp device tool alarm needs exactly one of time, date or timerSeconds")
    if "weekdays" in arguments and "time" not in arguments:
        raise ValueError("bighelp device tool weekdays need a time")


def _check_alarm_create(arguments: dict[str, Any]) -> None:
    _check_schedule(arguments, exactly_one=True)


def _check_alarm_update(arguments: dict[str, Any]) -> None:
    _changes(arguments, "id")
    _check_schedule(arguments, exactly_one=False)


# Music ------------------------------------------------------------------------------------------

_MUSIC_SOURCE = _choice("catalog", "library")
# The phone names the kinds in its results (song, album ...); the host only bounds the word.
_MUSIC_TYPE = _text(24, pattern=_TOKEN)


def _check_music_play(arguments: dict[str, Any]) -> None:
    by_result = {"id", "type", "source"} & set(arguments)
    if "query" in arguments:
        if by_result:
            raise ValueError("bighelp device tool play is either a query or a search result")
    elif by_result != {"id", "type", "source"}:
        raise ValueError("bighelp device tool play needs a query, or id, type and source from a result")


OPERATIONS: dict[str, Operation] = {
    "contacts.list": Operation({"query": _text(200), "limit": _whole(1, 50)}),
    "contacts.create": Operation(_contact_fields(clearable=False), rule=_check_contact_create),
    "contacts.update": Operation(
        {"id": _ID, "expectedRevision": _ID, **_contact_fields(clearable=True)},
        ("id", "expectedRevision"), _check_contact_update),
    "contacts.delete": Operation({"id": _ID, "expectedRevision": _ID}, ("id", "expectedRevision")),
    "photos.list": Operation(
        {
            "start": {"type": "string", "minLength": 1, "maxLength": 80, "format": "date-time"},
            "end": {"type": "string", "minLength": 1, "maxLength": 80, "format": "date-time"},
            "albumID": _ID, "mediaType": _choice("image", "video", "any"),
            "favorites": _BOOLEAN, "limit": _whole(1, 100),
        },
        rule=_check_photo_list),
    "photos.albums": Operation(),
    "photos.get": Operation({"id": _ID, "maxPixels": _whole(256, 1_024)}, ("id",)),
    "photos.save": Operation({"path": _text(MAX_PHOTO_PATH_LENGTH), "albumID": _ID}, ("path",)),
    "photos.update": Operation(
        {
            "id": _ID, "expectedRevision": _ID, "favorite": _BOOLEAN,
            "addToAlbumIDs": _list(_ID, 1, 20), "removeFromAlbumIDs": _list(_ID, 1, 20),
        },
        ("id", "expectedRevision"), _check_photo_update),
    "photos.createAlbum": Operation({"title": _text(200)}, ("title",)),
    "photos.delete": Operation({"ids": _list(_ID, 1, 50)}, ("ids",)),
    "geofences.list": Operation(),
    "geofences.create": Operation(
        {
            "name": _text(100), "latitude": _decimal(-90, 90), "longitude": _decimal(-180, 180),
            "here": _BOOLEAN, "radiusMeters": _whole(100, 5_000),
            "on": _choice("arrive", "leave", "both"),
            "note": _text(1_000, 0), "repeats": _BOOLEAN,
        },
        ("name",), _check_geofence),
    "geofences.delete": Operation({"id": _ID}, ("id",)),
    "alarms.list": Operation(),
    "alarms.create": Operation(_ALARM_FIELDS, ("label",), _check_alarm_create),
    "alarms.update": Operation({"id": _ID, **_ALARM_FIELDS}, ("id",), _check_alarm_update),
    "alarms.delete": Operation({"id": _ID}, ("id",)),
    "music.nowPlaying": Operation(),
    "music.search": Operation(
        {
            "query": _text(200),
            "types": _list(_choice("songs", "albums", "playlists", "artists"), 1, 4, unique=True),
            "source": _MUSIC_SOURCE, "limit": _whole(1, 25),
        },
        ("query",)),
    "music.library": Operation(
        {"type": _choice("playlists", "recent"), "limit": _whole(1, 50)}),
    "music.play": Operation(
        {"id": _ID, "type": _MUSIC_TYPE, "source": _MUSIC_SOURCE, "query": _text(200), "shuffle": _BOOLEAN},
        rule=_check_music_play),
    "music.control": Operation(
        {"action": _choice("pause", "resume", "next", "previous", "stop")}, ("action",)),
    "music.createPlaylist": Operation(
        {"name": _text(100), "description": _text(300, 0), "itemIDs": _list(_ID, 0, 100)}, ("name",)),
    "music.addToPlaylist": Operation({"playlistID": _ID, "itemIDs": _list(_ID, 1, 100)}, ("playlistID", "itemIDs")),
    "music.addToLibrary": Operation({"id": _ID, "type": _MUSIC_TYPE}, ("id", "type")),
    "focus.current": Operation(),
    "shortcuts.list": Operation(),
    "shortcuts.run": Operation({"name": _text(200), "input": _text(4_000, 0)}, ("name",)),
}

KIND_OPERATIONS: dict[str, tuple[str, ...]] = {}
for _name in OPERATIONS:
    _kind, _, _operation = _name.partition(".")
    KIND_OPERATIONS[_kind] = (*KIND_OPERATIONS.get(_kind, ()), _operation)


def validate_operation(operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Check `arguments` of one of OPERATIONS; return them unchanged or raise ValueError."""
    spec = OPERATIONS[operation]
    if not set(arguments).issubset(spec.properties):
        raise ValueError("bighelp device tool arguments contain an unknown key")
    if not set(spec.required).issubset(arguments):
        raise ValueError("bighelp device tool arguments are missing a required key")
    for key, value in arguments.items():
        check_value(value, spec.properties[key], key)
    if spec.rule is not None:
        spec.rule(arguments)
    return dict(arguments)


def _loosen(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """One schema that accepts whatever either does. The same field often differs a little by operation:
    an update may clear what create can't, and `limit` allows 25 results in one search and 50 in another."""
    if first == second:
        return first
    merged = dict(first)
    for lower, upper in (("minLength", "maxLength"), ("minimum", "maximum"), ("minItems", "maxItems")):
        if lower in first and lower in second:
            merged[lower] = min(first[lower], second[lower])
            merged[upper] = max(first[upper], second[upper])
    if "items" in first and "items" in second:
        merged["items"] = _loosen(first["items"], second["items"])
    if first.get("enum") != second.get("enum"):
        if "enum" in first and "enum" in second:
            merged["enum"] = [*first["enum"], *(value for value in second["enum"] if value not in first["enum"])]
        else:
            merged.pop("enum", None)
    if first.get("pattern") != second.get("pattern"):
        loosest = min((first, second), key=lambda schema: schema.get("minLength", 0))
        if first.get("minLength") != second.get("minLength") and "pattern" in loosest:
            merged["pattern"] = loosest["pattern"]
        else:
            merged.pop("pattern", None)
    return merged


def tool_properties(kind: str) -> dict[str, dict[str, Any]]:
    """The JSON-schema `properties` of one tool: every operation's fields, each as loose as its loosest use."""
    merged: dict[str, dict[str, Any]] = {}
    for operation in KIND_OPERATIONS[kind]:
        for key, schema in OPERATIONS[f"{kind}.{operation}"].properties.items():
            merged[key] = _loosen(merged[key], schema) if key in merged else schema
    return merged
