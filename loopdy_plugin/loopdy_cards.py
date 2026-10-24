from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, NoReturn
from urllib.parse import urlsplit


MAX_DOCUMENT_BYTES = 65_536
MAX_DATA_SOURCES = 4
MAX_ELEMENTS = 80
MAX_TREE_DEPTH = 12
MAX_CHILDREN = 20
MAX_TEXT_SCALARS = 2_000
MAX_EXPRESSION_DEPTH = 8
MAX_TABLE_COLUMNS = 8
MAX_TABLE_ROWS = 50
MAX_CHART_SERIES = 6
MAX_CHART_POINTS = 120
MAX_LIST_ITEMS = 50
MAX_OBJECT_ENTRIES = 5

_LOG = logging.getLogger(__name__)

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_HASH_64 = re.compile(r"^[0-9a-f]{64}$")
_HASH_32 = re.compile(r"^[0-9a-f]{32}$")
_DNS_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_RFC3339 = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

_ELEMENT_TYPES = {
    "card",
    "vstack",
    "hstack",
    "grid",
    "text",
    "metric",
    "badge",
    "progress",
    "chart",
    "table",
    "list",
    "divider",
    "spacer",
    "image",
}
# Object components show one real person, place or song. They are leaves, and
# a chat's app must have said it draws them (card_objects.py) before an agent
# can use them, because older apps reject an unknown element type.
OBJECT_TYPES = frozenset({"contact", "place", "music"})
# Drafts the person reviews before anything is saved (docs/IMAGE_DRAFTS.md). Only the
# draft tools make them, each alone in its card, and a chat's app must draw them too.
DRAFT_TYPES = frozenset({"contact_draft", "event_draft"})
_CONTAINERS = {"card", "vstack", "hstack", "grid"}
_LEAVES = (_ELEMENT_TYPES | OBJECT_TYPES | DRAFT_TYPES) - _CONTAINERS - {"list"}
_SEMANTICS = {
    "primary",
    "secondary",
    "positive",
    "warning",
    "negative",
    "accent",
    "neutral",
}
_SPACING = {"none", "xsmall", "small", "medium", "large", "xlarge"}
_TYPOGRAPHY = {"caption", "body", "callout", "headline", "title", "large_title"}
_ALIGNMENTS = {"leading", "center", "trailing"}
_FORMAT_STYLES = {
    "text",
    "integer",
    "number",
    "currency",
    "percent",
    "date",
    "time",
    "relative_date",
}
_OPERATIONS = {
    "coalesce",
    "add",
    "subtract",
    "multiply",
    "divide",
    "percent_change",
    "equal",
    "not_equal",
    "greater_than",
    "greater_than_or_equal",
    "less_than",
    "less_than_or_equal",
    "and",
    "or",
    "not",
}
_LOCAL_SUFFIXES = {
    "localhost",
    "local",
    "internal",
    "home",
    "lan",
    "arpa",
}
# A card's optional weather background. Unknown names are allowed so a card
# written for a newer app still renders on an older plugin; the app draws a
# plain gradient for names it doesn't know.
_BACKGROUND_FIELDS = {
    "scene": {
        "none",
        "clear",
        "partly_cloudy",
        "overcast",
        "rain",
        "thunderstorm",
        "snow",
        "fog",
        "wind",
    },
    "intensity": {"light", "moderate", "heavy"},
    "time_of_day": {"day", "dusk", "night"},
}
_BACKGROUND_TOKEN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_IMAGE_NAMES = {
    "bitcoinsign.circle.fill",
    "bolt.fill",
    "calendar",
    "chart.line.uptrend.xyaxis",
    "checkmark.circle.fill",
    "clock",
    "cloud.sun.fill",
    "drop.fill",
    "exclamationmark.triangle.fill",
    "info.circle.fill",
    "location.fill",
    "LoopdyMarkColor",
    "star.fill",
    "thermometer.medium",
    "wave.3.right.circle",
    "wind",
}


class BighelpCardError(ValueError):
    """Stable validation error raised at every bighelp Card trust boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except ValueError as error:
        if "Out of range float" in str(error):
            raise BighelpCardError(
                "invalid_number", "Non-finite numbers are not allowed"
            ) from error
        raise BighelpCardError(
            "invalid_payload", "Card is not canonical JSON"
        ) from error
    except (TypeError, OverflowError) as error:
        raise BighelpCardError(
            "invalid_payload", "Card is not canonical JSON"
        ) from error


def validate_card_input(
    payload: object,
    *,
    now: datetime,
    allow_live_data_for_testing: bool = False,
    warn: bool = True,
    allow_objects: bool = False,
    allow_drafts: bool = False,
) -> dict[str, object]:
    reference = _utc(now, code="invalid_clock")
    encoded = canonical_json(payload)
    if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        _raise("payload_too_large", "Card exceeds the 64 KiB document limit")
    if not isinstance(payload, dict):
        _raise("invalid_payload", "Card must be an object")

    value = json.loads(encoded)
    allowed = {
        "schema",
        "version",
        "title",
        "spoken_summary",
        "importance",
        "valid_until",
        "data_sources",
        "root",
        "elements",
    }
    required = allowed - {"importance", "valid_until"}
    _object(value, allowed, required)
    if value["schema"] != "loopdy.card":
        _raise("invalid_schema", "Unsupported bighelp Card schema")
    if isinstance(value["version"], bool) or value["version"] != 1:
        _raise("unsupported_version", "Unsupported bighelp Card version")
    _text(value["title"])
    _text(value["spoken_summary"])
    if "importance" in value and value["importance"] not in {"normal", "important", "urgent"}:
        _raise("invalid_importance", "Card importance is invalid")
    if "valid_until" in value:
        try:
            valid_until = _timestamp(value["valid_until"])
        except BighelpCardError as error:
            raise BighelpCardError(
                "invalid_valid_until", "Card validity timestamp is invalid"
            ) from error
        if not reference < valid_until <= reference + timedelta(days=30):
            _raise("invalid_valid_until", "Card validity is outside thirty days")

    sources = _data_sources(value["data_sources"], reference)
    if sources and not allow_live_data_for_testing:
        _raise(
            "live_data_unavailable",
            "Live bighelp Card data sources are not available in this release",
        )
    elements = value["elements"]
    if not isinstance(elements, dict) or not 1 <= len(elements) <= MAX_ELEMENTS:
        _raise("limit_exceeded", "Element count is outside the allowed range")
    for element_id in elements:
        _identifier(element_id)
    source_ids = {source["id"] for source in sources}
    for element_id, element in elements.items():
        _element(element_id, element, source_ids, warn=warn, allow_objects=allow_objects,
                 allow_drafts=allow_drafts)

    root = _identifier(value["root"])
    _validate_tree(root, elements)
    drafts = [element_id for element_id, element in elements.items() if element["type"] in DRAFT_TYPES]
    if drafts and (len(elements) != 1 or drafts != [root]):
        _raise("invalid_children", "A draft is the only element in its card")
    return value


def render_card(
    payload: object, *, now: datetime, allow_objects: bool = False, allow_drafts: bool = False
) -> dict[str, object]:
    created = _utc(now, code="invalid_clock")
    validated = validate_card_input(payload, now=created, allow_objects=allow_objects,
                                    allow_drafts=allow_drafts)
    content_hash = hashlib.sha256(
        canonical_json(validated).encode("utf-8")
    ).hexdigest()
    result: dict[str, object] = {
        **validated,
        "content_hash": content_hash,
        "card_id": content_hash[:32],
        "created_at": _timestamp_text(created),
        "origin": "live",
    }
    if len(canonical_json(result).encode("utf-8")) > MAX_DOCUMENT_BYTES:
        _raise("payload_too_large", "Rendered card exceeds the 64 KiB document limit")
    return result


def validate_card_result(payload: object, *, now: datetime) -> dict[str, object]:
    observed = _utc(now, code="invalid_clock")
    encoded = canonical_json(payload)
    if len(encoded.encode("utf-8")) > MAX_DOCUMENT_BYTES:
        _raise("payload_too_large", "Rendered card exceeds the 64 KiB document limit")
    if not isinstance(payload, dict):
        _raise("invalid_payload", "Rendered card must be an object")
    value = json.loads(encoded)
    input_keys = {
        "schema",
        "version",
        "title",
        "spoken_summary",
        "importance",
        "valid_until",
        "data_sources",
        "root",
        "elements",
    }
    renderer_keys = {"content_hash", "card_id", "created_at", "origin"}
    required_input_keys = input_keys - {"importance", "valid_until"}
    _object(value, input_keys | renderer_keys, required_input_keys | renderer_keys)

    try:
        created = _timestamp(value["created_at"])
    except BighelpCardError as error:
        raise BighelpCardError(
            "invalid_created_at", "Renderer creation time is invalid"
        ) from error
    if created > observed + timedelta(minutes=5):
        _raise("invalid_created_at", "Renderer creation time is in the future")
    if value["origin"] != "live":
        _raise("invalid_origin", "Renderer origin is invalid")

    card_input = {key: value[key] for key in input_keys if key in value}
    # render_card already warned about this document's unknown values.
    # A rendered result was already allowed its objects (and drafts) when it was made.
    validated = validate_card_input(card_input, now=created, warn=False, allow_objects=True,
                                    allow_drafts=True)
    expected_hash = hashlib.sha256(
        canonical_json(validated).encode("utf-8")
    ).hexdigest()
    content_hash = value["content_hash"]
    if not isinstance(content_hash, str) or not _HASH_64.fullmatch(content_hash):
        _raise("invalid_content_hash", "Card content hash is invalid")
    if content_hash != expected_hash:
        _raise("invalid_content_hash", "Card content hash does not match its document")
    card_id = value["card_id"]
    if (
        not isinstance(card_id, str)
        or not _HASH_32.fullmatch(card_id)
        or card_id != expected_hash[:32]
    ):
        _raise("invalid_card_id", "Card identity does not match its content")
    return {
        **validated,
        "content_hash": expected_hash,
        "card_id": expected_hash[:32],
        "created_at": _timestamp_text(created),
        "origin": "live",
    }


def _data_sources(value: object, reference: datetime) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > MAX_DATA_SOURCES:
        _raise("limit_exceeded", "Data source count exceeds the limit")
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for source in value:
        _object(source, {"id", "request", "response", "refresh"}, {"id", "request", "response", "refresh"})
        source_id = _identifier(source["id"])
        if source_id in seen:
            _raise("duplicate_source", "Data source IDs must be unique")
        seen.add(source_id)

        request = source["request"]
        _object(request, {"method", "url"}, {"method", "url"})
        if request["method"] != "GET":
            _raise("invalid_url", "Card data sources are GET-only")
        _public_https_url(request["url"])

        response = source["response"]
        _object(response, {"format", "root"}, {"format", "root"})
        if response["format"] != "json":
            _raise("invalid_value", "Card responses must be JSON")
        _pointer(response["root"])

        refresh = source["refresh"]
        _object(
            refresh,
            {"minimum_interval_seconds", "stale_after_seconds", "expires_at"},
            {"minimum_interval_seconds", "stale_after_seconds", "expires_at"},
        )
        interval = _integer(refresh["minimum_interval_seconds"])
        stale = _integer(refresh["stale_after_seconds"])
        if not 60 <= interval <= 3_600 or not interval <= stale <= 86_400:
            _raise("invalid_refresh", "Refresh and stale intervals are invalid")
        try:
            expires = _timestamp(refresh["expires_at"])
        except BighelpCardError as error:
            raise BighelpCardError(
                "invalid_expiration", "Data source expiration is invalid"
            ) from error
        if not reference < expires <= reference + timedelta(days=7):
            _raise("invalid_expiration", "Data source expiration is outside seven days")
        result.append(source)
    return result


def _element(
    element_id: str,
    value: object,
    source_ids: set[str],
    *,
    warn: bool = True,
    allow_objects: bool = False,
    allow_drafts: bool = False,
) -> None:
    _object(
        value,
        {"type", "props", "children", "background"},
        {"type", "props", "children"},
    )
    element_type = value["type"]
    if element_type in OBJECT_TYPES and not allow_objects:
        _raise(
            "objects_unavailable",
            f"Element {element_id}: contact, place and music components aren't available "
            "here. Use basic components (text, list, table, metric).",
        )
    if element_type in DRAFT_TYPES and not allow_drafts:
        _raise(
            "drafts_tool_only",
            f"Element {element_id}: contact and event drafts come only from bighelp_draft_contact "
            "and bighelp_draft_event.",
        )
    if element_type not in _ELEMENT_TYPES and element_type not in OBJECT_TYPES | DRAFT_TYPES:
        _raise("unsupported_element", f"Element {element_id} has an unsupported type")
    props = value["props"]
    if not isinstance(props, dict):
        _raise("invalid_value", f"Element {element_id} props must be an object")
    if "background" in props:
        _raise(
            "invalid_background",
            f"Element {element_id}: put background beside props on the card element, not inside props",
        )
    if "background" in value:
        if element_type != "card":
            _raise(
                "invalid_background",
                f"Element {element_id}: background is allowed only on card elements",
            )
        _background(value["background"], warn=warn)
    children = value["children"]
    if not isinstance(children, list):
        _raise("invalid_children", f"Element {element_id} children must be an array")
    if len(children) > MAX_CHILDREN:
        _raise("limit_exceeded", "Children per container exceed the limit")
    for child in children:
        _identifier(child)
    if element_type in _LEAVES and children:
        _raise("invalid_children", f"Element {element_id} cannot have children")
    if element_type == "list" and len(children) != 1:
        _raise("invalid_children", "A list requires exactly one template child")

    if element_type == "card":
        _props(props, {"title", "subtitle"}, {"title"})
        _text(props["title"])
        if "subtitle" in props:
            _text(props["subtitle"])
    elif element_type in {"vstack", "hstack"}:
        _props(props, {"spacing", "alignment"}, set())
        _optional_enum(props, "spacing", _SPACING)
        _optional_enum(props, "alignment", _ALIGNMENTS)
    elif element_type == "grid":
        _props(props, {"columns", "spacing", "alignment"}, {"columns"})
        if _integer(props["columns"]) not in {2, 3}:
            _raise("invalid_value", "Grid columns must be two or three")
        _optional_enum(props, "spacing", _SPACING)
        _optional_enum(props, "alignment", _ALIGNMENTS)
    elif element_type == "text":
        _props(
            props,
            {"value", "format", "typography", "color", "alignment", "line_limit"},
            {"value"},
        )
        _binding(props["value"], source_ids)
        _optional_format(props, source_ids)
        _optional_enum(props, "typography", _TYPOGRAPHY)
        _optional_enum(props, "color", _SEMANTICS)
        _optional_enum(props, "alignment", _ALIGNMENTS)
        if "line_limit" in props and not 1 <= _integer(props["line_limit"]) <= 20:
            _raise("limit_exceeded", "Text line limit is outside the allowed range")
    elif element_type == "metric":
        _props(
            props,
            {"label", "value", "format", "trend", "trend_format", "semantic"},
            {"label", "value"},
        )
        _text(props["label"])
        _binding(props["value"], source_ids)
        _optional_format(props, source_ids)
        if "trend" in props:
            _binding(props["trend"], source_ids)
        if "trend_format" in props:
            _format(props["trend_format"], source_ids)
        _optional_enum(props, "semantic", _SEMANTICS)
    elif element_type == "badge":
        _props(props, {"value", "semantic"}, {"value", "semantic"})
        _binding(props["value"], source_ids)
        _enum(props["semantic"], _SEMANTICS)
    elif element_type == "progress":
        _props(
            props,
            {"label", "value", "maximum", "format", "semantic"},
            {"label", "value", "maximum"},
        )
        _text(props["label"])
        _binding(props["value"], source_ids)
        _binding(props["maximum"], source_ids)
        _optional_format(props, source_ids)
        _optional_enum(props, "semantic", _SEMANTICS)
    elif element_type == "chart":
        _chart(props, source_ids)
    elif element_type == "table":
        _table(props, source_ids)
    elif element_type == "list":
        _props(props, {"items", "empty_text", "shows_dividers"}, {"items"})
        _source_binding(props["items"], source_ids)
        if "empty_text" in props:
            _text(props["empty_text"])
        if "shows_dividers" in props and not isinstance(props["shows_dividers"], bool):
            _raise("invalid_value", "List divider visibility must be boolean")
    elif element_type == "divider":
        _props(props, {"semantic"}, set())
        _optional_enum(props, "semantic", _SEMANTICS)
    elif element_type == "spacer":
        _props(props, {"size"}, {"size"})
        _enum(props["size"], _SPACING)
    elif element_type == "image":
        _props(
            props,
            {"name", "accessibility_label", "semantic", "scale"},
            {"name", "accessibility_label"},
        )
        _enum(props["name"], _IMAGE_NAMES)
        _text(props["accessibility_label"])
        _optional_enum(props, "semantic", _SEMANTICS)
        _optional_enum(props, "scale", {"small", "medium", "large"})
    elif element_type == "contact":
        _contact(props)
    elif element_type == "place":
        _place(props)
    elif element_type == "music":
        _music(props)
    elif element_type in DRAFT_TYPES:
        draft_props(element_type, props)


# Object component props are plain values, not bindings: they name one real
# thing, and the app picks the buttons from them.
_PHONE_CHARACTERS = re.compile(r"^[0-9 +()\-.]+$")
_EMAIL = re.compile(
    r"^[^\s@<>()\[\]\\,;:\"?&#%/]+@[\w-]+(?:\.[\w-]+)+$"
)
_ADDRESS_FIELDS = {"street", "city", "region", "postal_code", "country"}
_MUSIC_KINDS = {"song", "album", "playlist", "artist"}
_MUSIC_SOURCES = {"catalog", "library"}
_MUSIC_HOST = "music.apple.com"


def _string(value: object, maximum: int, what: str, *, allows_empty: bool = False) -> str:
    # Object text is short and one line: names, labels and ids, not paragraphs.
    text = _text(value, allows_empty=allows_empty)
    if len(text) > maximum:
        _raise("limit_exceeded", f"{what} is over {maximum} characters")
    if any(ord(scalar) < 32 for scalar in text):
        _raise("invalid_value", f"{what} must be one line")
    return text


def _contact(props: dict[str, object]) -> None:
    _props(
        props,
        {"name", "subtitle", "phones", "emails", "address", "contact_id"},
        {"name"},
    )
    _string(props["name"], 200, "Contact name")
    if "subtitle" in props:
        _string(props["subtitle"], 200, "Contact subtitle")
    for key, check in (("phones", _phone), ("emails", _email)):
        if key not in props:
            continue
        entries = props[key]
        if not isinstance(entries, list) or len(entries) > MAX_OBJECT_ENTRIES:
            _raise("limit_exceeded", f"Contact {key} are limited to {MAX_OBJECT_ENTRIES}")
        for entry in entries:
            # A label may be left out (or empty); the app then shows none.
            _object(entry, {"label", "value"}, {"value"})
            if "label" in entry:
                _string(entry["label"], 40, "Contact label", allows_empty=True)
            check(_string(entry["value"], 200, f"Contact {key[:-1]}"))
    if "address" in props:
        address = props["address"]
        # Every part empty reads as no address, so an empty address is not an error.
        _object(address, _ADDRESS_FIELDS, set())
        for key, field in address.items():
            _string(field, 100, f"Address {key}", allows_empty=True)
    if "contact_id" in props:
        _string(props["contact_id"], 512, "Contact id")


def _phone(value: str) -> None:
    # Letters would be dropped silently ("ext 5" would turn into a digit), so
    # refuse them and let the agent write the number again.
    if not _PHONE_CHARACTERS.fullmatch(value) or not any(c.isdigit() for c in value):
        _raise(
            "invalid_phone",
            "A phone number may hold only digits, spaces and + ( ) - . characters",
        )


def _email(value: str) -> None:
    if _EMAIL.fullmatch(value) is None or value.rsplit(".", 1)[-1].isdigit():
        _raise("invalid_email", "An email address must look like name@example.com")


def _place(props: dict[str, object]) -> None:
    _props(
        props,
        {"name", "subtitle", "address", "latitude", "longitude", "span_meters"},
        {"name", "latitude", "longitude"},
    )
    _string(props["name"], 200, "Place name")
    if "subtitle" in props:
        _string(props["subtitle"], 200, "Place subtitle")
    if "address" in props:
        _string(props["address"], 300, "Place address")
    for key, low, high in (("latitude", -90, 90), ("longitude", -180, 180)):
        value = props[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _raise("invalid_value", f"Place {key} must be a number")
        if not math.isfinite(value) or not low <= value <= high:
            _raise("invalid_value", f"Place {key} must be from {low} to {high}")
    if "span_meters" in props:
        span = props["span_meters"]
        if isinstance(span, bool) or not isinstance(span, (int, float)):
            _raise("invalid_value", "Place span_meters must be a number")
        if not math.isfinite(span) or not 200 <= span <= 50_000:
            _raise("limit_exceeded", "Place span_meters must be from 200 to 50000")


def _music(props: dict[str, object]) -> None:
    _props(
        props,
        {"kind", "title", "artist", "music_id", "source", "url"},
        {"kind", "title"},
    )
    _enum(props["kind"], _MUSIC_KINDS)
    _string(props["title"], 200, "Music title")
    if "artist" in props:
        _string(props["artist"], 200, "Music artist")
    if "music_id" in props:
        _string(props["music_id"], 512, "Music id")
    if "source" in props:
        _enum(props["source"], _MUSIC_SOURCES)
    if "url" in props:
        _apple_music_url(_string(props["url"], 500, "Music url"))


def _apple_music_url(value: str) -> None:
    problem = "Music url must be an https://music.apple.com/ address"
    if not value.isascii() or any(character.isspace() for character in value):
        _raise("invalid_url", problem)
    try:
        parts = urlsplit(value)
    except ValueError as error:
        raise BighelpCardError("invalid_url", problem) from error
    # The whole netloc must be the host: no user info, no port, no look-alike.
    if (
        parts.scheme != "https"
        or parts.netloc.lower() != _MUSIC_HOST
        or "\\" in value
        or len(parts.path) < 2
    ):
        _raise("invalid_url", problem)


# Drafts: what an agent read from the person's picture, for the person to check and save
# themselves (docs/IMAGE_DRAFTS.md). The plugin sets the ids, the picture and the messages;
# the agent's arguments become `fields` and `marks`.
DRAFT_PRESETS = {"contact_draft": "business_card", "event_draft": "event_poster"}
DRAFT_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/heic", "image/heif", "image/webp", "image/gif"})
DRAFT_MESSAGE_KEYS = ("review", "missing_fields", "saved", "failed", "outcome_unknown")
# The placeholders each message may hold; the app fills each in once, never recursively.
DRAFT_PLACEHOLDERS = {
    "review": frozenset({"destination"}),
    "missing_fields": frozenset({"fields"}),
    "saved": frozenset({"name", "destination"}),
    "failed": frozenset({"reason"}),
    "outcome_unknown": frozenset(),
}
DRAFT_MARK_REASONS = frozenset({"unclear", "guessed", "not_in_image", "conflicting"})
MAX_DRAFT_MARKS = 20
MAX_DRAFT_MESSAGE = 500
_DRAFT_ID = re.compile(r"^d_[0-9a-f]{32}$")
_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")
_CONTACT_TEXT_FIELDS = ("given_name", "family_name", "organization", "job_title")
_CONTACT_FIELDS = {*_CONTACT_TEXT_FIELDS, "phones", "emails", "postal_address"}
_EVENT_COMMON_FIELDS = {"title", "location", "notes", "all_day"}
_EVENT_TIMED_FIELDS = {"start", "end", "time_zone"}
_EVENT_DAY_FIELDS = {"start_date", "end_date"}
_LIST_PATH = re.compile(r"^(phones|emails)\.(0|[1-9][0-9]*)$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_ZONE = re.compile(r"^[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*$")
_MAX_REVISION = 9_007_199_254_740_991


def draft_message(key: str, value: object) -> str:
    """One review message: plain text, 1-500 characters, only its own placeholders."""
    text = _text(value)
    if len(text) > MAX_DRAFT_MESSAGE:
        _raise("limit_exceeded", f"Message {key} is over {MAX_DRAFT_MESSAGE} characters")
    if not text.strip():
        _raise("invalid_value", f"Message {key} is only spaces")
    rest = _PLACEHOLDER.sub("", text)
    if "{{" in rest or "}}" in rest:
        _raise("invalid_placeholder", f"Message {key} has an unbalanced placeholder")
    unknown = sorted(set(_PLACEHOLDER.findall(text)) - DRAFT_PLACEHOLDERS[key])
    if unknown:
        allowed = ", ".join("{{" + name + "}}" for name in sorted(DRAFT_PLACEHOLDERS[key])) or "none"
        _raise("invalid_placeholder", f"Message {key} may use only these placeholders: {allowed}")
    return text


def draft_props(element_type: str, props: dict[str, object]) -> None:
    _props(
        props,
        {"draft_id", "preset", "source", "config_revision", "messages", "fields", "marks"},
        {"draft_id", "preset", "source", "config_revision", "messages", "fields"},
    )
    if not isinstance(props["draft_id"], str) or not _DRAFT_ID.fullmatch(props["draft_id"]):
        _raise("invalid_value", "draft_id must be d_ and 32 lowercase hex characters")
    if props["preset"] != DRAFT_PRESETS[element_type]:
        _raise("invalid_value", f"A {element_type} has preset {DRAFT_PRESETS[element_type]}")
    source = props["source"]
    _object(source, {"image_id", "mime_type", "width", "height"}, {"image_id", "mime_type"})
    if not isinstance(source["image_id"], str) or not _HASH_32.fullmatch(source["image_id"]):
        _raise("invalid_value", "The picture's image_id must be 32 lowercase hex characters")
    _enum(source["mime_type"], set(DRAFT_IMAGE_TYPES))
    for key in ("width", "height"):
        if key in source and not 1 <= _integer(source[key]) <= 65_535:
            _raise("invalid_value", f"The picture's {key} is out of range")
    if not 1 <= _integer(props["config_revision"]) <= _MAX_REVISION:
        _raise("invalid_value", "config_revision must be a positive integer")
    messages = props["messages"]
    _object(messages, set(DRAFT_MESSAGE_KEYS), set(DRAFT_MESSAGE_KEYS))
    for key in DRAFT_MESSAGE_KEYS:
        draft_message(key, messages[key])
    fields = props["fields"]
    if element_type == "contact_draft":
        _contact_draft_fields(fields)
    else:
        _event_draft_fields(fields)
    if "marks" in props:
        _draft_marks(element_type, fields, props["marks"])


def _contact_draft_fields(fields: object) -> None:
    _object(fields, _CONTACT_FIELDS, set())
    if not fields:
        _raise("missing_field", "A contact draft needs at least one field")
    for key in _CONTACT_TEXT_FIELDS:
        if key in fields:
            _string(fields[key], 200, key)
    for key, check in (("phones", _phone), ("emails", _email)):
        if key not in fields:
            continue
        entries = fields[key]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_OBJECT_ENTRIES:
            _raise("limit_exceeded", f"{key} holds 1 to {MAX_OBJECT_ENTRIES} entries")
        for entry in entries:
            _object(entry, {"label", "value"}, {"value"})
            if "label" in entry:
                _string(entry["label"], 40, f"A {key[:-1]} label", allows_empty=True)
            check(_string(entry["value"], 200, f"A {key[:-1]}"))
    if "postal_address" in fields:
        address = fields["postal_address"]
        _object(address, _ADDRESS_FIELDS, set())
        if not address:
            _raise("missing_field", "Leave postal_address out when there's no address")
        for key, part in address.items():
            _string(part, 100, f"Address {key}")


def _event_draft_fields(fields: object) -> None:
    _object(fields, _EVENT_COMMON_FIELDS | _EVENT_TIMED_FIELDS | _EVENT_DAY_FIELDS, set())
    if not fields:
        _raise("missing_field", "An event draft needs at least one field")
    if "title" in fields:
        _string(fields["title"], 200, "title")
    for key, maximum in (("location", 500), ("notes", 2_000)):
        if key in fields:
            text = _text(fields[key])
            if len(text) > maximum:
                _raise("limit_exceeded", f"{key} is over {maximum} characters")
            if not text.strip():
                _raise("invalid_value", f"{key} is only spaces")
    all_day = fields.get("all_day", False)
    if not isinstance(all_day, bool):
        _raise("invalid_value", "all_day must be true or false")
    other = _EVENT_TIMED_FIELDS if all_day else _EVENT_DAY_FIELDS
    if other & set(fields):
        _raise("variant_mismatch", "An all-day event takes start_date and end_date; a timed one takes "
                                   "start, end and time_zone. Don't mix them.")
    if all_day:
        start = _draft_date(fields["start_date"]) if "start_date" in fields else None
        end = _draft_date(fields["end_date"]) if "end_date" in fields else None
        if end is not None and (start is None or end <= start):
            _raise("invalid_time_range", "end_date is the day after the last day, after start_date")
        return
    begins = _draft_time(fields["start"], "start") if "start" in fields else None
    ends = _draft_time(fields["end"], "end") if "end" in fields else None
    if ends is not None and (begins is None or ends <= begins):
        _raise("invalid_time_range", "end must come after start")
    if "time_zone" in fields:
        _draft_time_zone(fields["time_zone"])


def _draft_time(value: object, key: str) -> datetime:
    try:
        return _timestamp(value)
    except BighelpCardError as error:
        raise BighelpCardError(
            "invalid_value", f"{key} must be RFC 3339 with an offset, like 2030-06-15T18:00:00-07:00"
        ) from error


def _draft_date(value: object) -> date:
    if isinstance(value, str) and _DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    _raise("invalid_value", "A date must be YYYY-MM-DD")


_ZONE_DATA: bool | None = None


def _draft_time_zone(value: object) -> None:
    global _ZONE_DATA
    if not isinstance(value, str) or len(value) > 64 or not _TIME_ZONE.fullmatch(value):
        _raise("invalid_time_zone", "time_zone must be an IANA name like America/Los_Angeles")
    import zoneinfo
    try:
        zoneinfo.ZoneInfo(value)
        return
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        pass
    if _ZONE_DATA is None:
        # A host without a time zone database (Windows without tzdata) can't check names; it keeps the shape check.
        _ZONE_DATA = bool(zoneinfo.available_timezones())
    if _ZONE_DATA:
        _raise("invalid_time_zone", "time_zone must be an IANA name like America/Los_Angeles")


def _draft_marks(element_type: str, fields: dict[str, object], marks: object) -> None:
    if not isinstance(marks, list) or len(marks) > MAX_DRAFT_MARKS:
        _raise("limit_exceeded", f"A draft takes at most {MAX_DRAFT_MARKS} marks")
    seen: set[str] = set()
    for mark in marks:
        _object(mark, {"field", "reason"}, {"field", "reason"})
        path = mark["field"]
        reason = _enum(mark["reason"], set(DRAFT_MARK_REASONS))
        if not isinstance(path, str) or path in seen:
            _raise("invalid_mark", "Mark each field once")
        seen.add(path)
        present = _draft_path(element_type, fields, path)
        if reason == "not_in_image" and present:
            _raise("invalid_mark", f"{path} has a value, so it can't be not_in_image")


def _draft_path(element_type: str, fields: dict[str, object], path: str) -> bool:
    """Whether ``path`` names a real field of this draft, and whether it holds a value."""
    if element_type == "contact_draft":
        if path in _CONTACT_FIELDS:
            return path in fields
        match = _LIST_PATH.fullmatch(path)
        if match and int(match[2]) < MAX_OBJECT_ENTRIES:
            entries = fields.get(match[1])
            return isinstance(entries, list) and int(match[2]) < len(entries)
        prefix, _, part = path.partition(".")
        if prefix == "postal_address" and part in _ADDRESS_FIELDS:
            address = fields.get("postal_address")
            return isinstance(address, dict) and part in address
    else:
        variant = _EVENT_DAY_FIELDS if fields.get("all_day") is True else _EVENT_TIMED_FIELDS
        if path in _EVENT_COMMON_FIELDS | variant:
            return path in fields
    _raise("invalid_mark", f"Mark field {path!r} isn't a field of this draft")


def _background(value: object, *, warn: bool) -> None:
    # Sits beside props rather than inside them: app builds from before
    # backgrounds ignore unknown element keys but reject unknown props, so
    # they keep showing the card, just without the weather.
    if not isinstance(value, dict):
        _raise("invalid_background", "Card background must be an object")
    if set(value) - set(_BACKGROUND_FIELDS):
        _raise(
            "invalid_background",
            "Card background takes only scene, intensity and time_of_day",
        )
    if "scene" not in value:
        _raise("invalid_background", "Card background needs a scene")
    for field, known in _BACKGROUND_FIELDS.items():
        if field not in value:
            continue
        token = value[field]
        if not isinstance(token, str) or not _BACKGROUND_TOKEN.fullmatch(token):
            _raise("invalid_background", f"Card background {field} must be a lowercase name")
        if warn and token not in known:
            # Kept, not rejected: a newer app may know it, and this one falls
            # back to a plain gradient. The value itself isn't logged.
            _LOG.warning("bighelp card background unknown value field=%s", field)


def _chart(props: dict[str, object], source_ids: set[str]) -> None:
    _props(props, {"kind", "description", "series"}, {"kind", "description", "series"})
    _enum(props["kind"], {"line", "area", "bar"})
    _text(props["description"])
    series_values = props["series"]
    if not isinstance(series_values, list) or not 1 <= len(series_values) <= MAX_CHART_SERIES:
        _raise("limit_exceeded", "Chart series count is outside the allowed range")
    seen: set[str] = set()
    for series in series_values:
        _object(series, {"id", "label", "semantic", "points"}, {"id", "label", "semantic", "points"})
        series_id = _identifier(series["id"])
        if series_id in seen:
            _raise("duplicate_element", "Chart series IDs must be unique")
        seen.add(series_id)
        _text(series["label"])
        _enum(series["semantic"], _SEMANTICS)
        points = series["points"]
        if not isinstance(points, list) or not 1 <= len(points) <= MAX_CHART_POINTS:
            _raise("limit_exceeded", "Chart point count is outside the allowed range")
        for point in points:
            _object(point, {"x", "y", "label"}, {"x", "y"})
            _binding(point["x"], source_ids)
            _binding(point["y"], source_ids)
            if "label" in point:
                _text(point["label"])


def _table(props: dict[str, object], source_ids: set[str]) -> None:
    _props(props, {"columns", "rows"}, {"columns", "rows"})
    columns = props["columns"]
    rows = props["rows"]
    if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_TABLE_COLUMNS:
        _raise("limit_exceeded", "Table column count is outside the allowed range")
    if not isinstance(rows, list) or len(rows) > MAX_TABLE_ROWS:
        _raise("limit_exceeded", "Table row count exceeds the limit")
    for column in columns:
        _object(column, {"label", "alignment", "format"}, {"label"})
        _text(column["label"])
        _optional_enum(column, "alignment", _ALIGNMENTS)
        if "format" in column:
            _format(column["format"], source_ids)
    for row in rows:
        _object(row, {"cells"}, {"cells"})
        cells = row["cells"]
        if not isinstance(cells, list) or len(cells) != len(columns):
            _raise("invalid_value", "Each table row must match its columns")
        for cell in cells:
            _binding(cell, source_ids)


def _binding(value: object, source_ids: set[str], depth: int = 0) -> None:
    if not isinstance(value, dict):
        _raise("invalid_binding", "A card value must be a binding object")
    keys = set(value)
    if keys == {"literal"}:
        literal = value["literal"]
        if isinstance(literal, str):
            _text(literal, allows_empty=True)
        elif isinstance(literal, bool) or literal is None:
            pass
        elif isinstance(literal, (int, float)):
            _finite(literal)
        else:
            _raise("invalid_binding", "Literal bindings must contain a scalar JSON value")
        return
    if keys == {"source", "pointer"}:
        _source_binding(value, source_ids)
        return
    if keys != {"expression"}:
        _raise("invalid_binding", "Binding contains unknown or missing fields")
    if depth >= MAX_EXPRESSION_DEPTH:
        _raise("limit_exceeded", "Expression nesting exceeds eight levels")
    expression = value["expression"]
    _object(expression, {"op", "arguments"}, {"op", "arguments"})
    operation = _enum(expression["op"], _OPERATIONS)
    arguments = expression["arguments"]
    if not isinstance(arguments, list) or not 1 <= len(arguments) <= 8:
        _raise("invalid_expression", "Expression argument count is invalid")
    if operation == "not" and len(arguments) != 1:
        _raise("invalid_expression", "not requires one argument")
    if operation in {"and", "or"} and len(arguments) < 2:
        _raise("invalid_expression", "Boolean aggregations require two arguments")
    if operation not in {"coalesce", "and", "or", "not"} and len(arguments) != 2:
        _raise("invalid_expression", "Binary operation requires two arguments")
    for argument in arguments:
        _binding(argument, source_ids, depth + 1)


def _source_binding(value: object, source_ids: set[str]) -> None:
    _object(value, {"source", "pointer"}, {"source", "pointer"})
    source = _identifier(value["source"])
    if source not in source_ids:
        _raise("unknown_source", "Binding references an undeclared data source")
    _pointer(value["pointer"])


def _optional_format(props: dict[str, object], source_ids: set[str]) -> None:
    if "format" in props:
        _format(props["format"], source_ids)


def _format(value: object, _source_ids: set[str]) -> None:
    _object(
        value,
        {
            "style",
            "currency",
            "currency_pointer",
            "minimum_fraction_digits",
            "maximum_fraction_digits",
        },
        {"style"},
    )
    style = _enum(value["style"], _FORMAT_STYLES)
    currency_fields = {key for key in ("currency", "currency_pointer") if key in value}
    if style == "currency":
        if len(currency_fields) != 1:
            _raise("invalid_format", "Currency format requires one currency source")
    elif currency_fields:
        _raise("invalid_format", "Currency metadata is only valid for currency format")
    if "currency" in value:
        currency = value["currency"]
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            _raise("invalid_format", "Currency must be an ISO-style code")
    if "currency_pointer" in value:
        _pointer(value["currency_pointer"])
    minimum = value.get("minimum_fraction_digits")
    maximum = value.get("maximum_fraction_digits")
    if minimum is not None and not 0 <= _integer(minimum) <= 6:
        _raise("invalid_format", "Minimum fraction digits are invalid")
    if maximum is not None and not 0 <= _integer(maximum) <= 6:
        _raise("invalid_format", "Maximum fraction digits are invalid")
    if minimum is not None and maximum is not None and minimum > maximum:
        _raise("invalid_format", "Minimum fraction digits exceed maximum")
    if (minimum is not None or maximum is not None) and style not in {
        "integer",
        "number",
        "currency",
        "percent",
    }:
        _raise("invalid_format", "Fraction digits are invalid for this format")


def _validate_tree(root: str, elements: dict[str, object]) -> None:
    if root not in elements:
        _raise("missing_root", "Root element is not defined")
    visited: set[str] = set()
    active: set[str] = set()
    stack: list[tuple[str, int, bool]] = [(root, 1, False)]
    while stack:
        element_id, depth, exiting = stack.pop()
        if exiting:
            active.remove(element_id)
            visited.add(element_id)
            continue
        if element_id in active:
            _raise("cycle", "Element graph contains a cycle")
        if element_id in visited:
            _raise("duplicate_element", "Element is referenced more than once")
        if element_id not in elements:
            _raise("invalid_children", "Element references an unknown child")
        if depth > MAX_TREE_DEPTH:
            _raise("limit_exceeded", "Element tree exceeds twelve levels")
        active.add(element_id)
        stack.append((element_id, depth, True))
        children = elements[element_id]["children"]
        for child in reversed(children):
            stack.append((child, depth + 1, False))
    unreachable = set(elements) - visited
    if unreachable:
        _raise("unreachable_element", "Card contains unreachable elements")


def _public_https_url(value: object) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2_048:
        _raise("invalid_url", "Data source URL is invalid or too long")
    if any(character.isspace() or ord(character) < 32 for character in value):
        _raise("invalid_url", "Data source URL contains whitespace or controls")
    try:
        components = urlsplit(value)
        port = components.port
    except ValueError as error:
        raise BighelpCardError("invalid_url", "Data source URL is malformed") from error
    if (
        components.scheme.lower() != "https"
        or not components.netloc
        or components.username is not None
        or components.password is not None
        or components.fragment
        or port not in {None, 443}
        or "%" in components.netloc
    ):
        _raise("invalid_url", "Data source URL violates the public GET policy")
    raw_host = components.hostname
    if raw_host is None:
        _raise("invalid_url", "Data source URL requires a public DNS host")
    host = raw_host.lower().rstrip(".")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as error:
        raise BighelpCardError("invalid_url", "Data source host must be ASCII DNS") from error
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        _raise("invalid_url", "Literal IP hosts are forbidden")
    labels = host.split(".")
    if (
        len(labels) < 2
        or any(not _DNS_LABEL.fullmatch(label) for label in labels)
        or not re.fullmatch(r"[a-z]{2,63}", labels[-1])
        or any(host == suffix or host.endswith(f".{suffix}") for suffix in _LOCAL_SUFFIXES)
    ):
        _raise("invalid_url", "Data source host is not a public DNS name")
    return value


def _pointer(value: object) -> str:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 2_048:
        _raise("invalid_pointer", "JSON Pointer is invalid or too long")
    if value == "":
        return value
    if not value.startswith("/"):
        _raise("invalid_pointer", "JSON Pointer must be empty or begin with a slash")
    for token in value.split("/")[1:]:
        index = 0
        while index < len(token):
            if token[index] == "~":
                if index + 1 >= len(token) or token[index + 1] not in {"0", "1"}:
                    _raise("invalid_pointer", "JSON Pointer escape is invalid")
                index += 2
            else:
                index += 1
    return value


def _object(value: object, allowed: set[str], required: set[str]) -> None:
    if not isinstance(value, dict):
        _raise("invalid_value", "Expected an object")
    unknown = set(value) - allowed
    if unknown:
        _raise("unknown_field", "Object contains an unknown field")
    if required - set(value):
        _raise("missing_field", "Object is missing a required field")


def _props(value: dict[str, object], allowed: set[str], required: set[str]) -> None:
    _object(value, allowed, required)


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _raise("invalid_identifier", "Identifier is invalid")
    return value


def _text(value: object, *, allows_empty: bool = False) -> str:
    if not isinstance(value, str):
        _raise("invalid_value", "Expected text")
    if (not allows_empty and not value) or len(value) > MAX_TEXT_SCALARS:
        _raise("limit_exceeded", "Text is outside the 2,000-scalar limit")
    for scalar in value:
        codepoint = ord(scalar)
        if 0xD800 <= codepoint <= 0xDFFF:
            _raise("invalid_value", "Text contains an invalid Unicode scalar")
        if codepoint < 32 and scalar not in {"\n", "\t"}:
            _raise("invalid_value", "Text contains a forbidden control character")
    return value


def _finite(value: int | float) -> int | float:
    if isinstance(value, bool) or not math.isfinite(value):
        _raise("invalid_number", "Expected a finite number")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _raise("invalid_value", "Expected an integer")
    return value


def _enum(value: object, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        _raise("invalid_value", "Value is not in the allowlist")
    return value


def _optional_enum(
    value: dict[str, object], key: str, choices: set[str]
) -> None:
    if key in value:
        _enum(value[key], choices)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not _RFC3339.fullmatch(value):
        _raise("invalid_value", "Expected an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _raise("invalid_value", "Expected an RFC 3339 timestamp")
    if parsed.tzinfo is None:
        _raise("invalid_value", "Timestamp requires a timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime, *, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _raise(code, "Clock must include a timezone")
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _raise(code: str, message: str) -> NoReturn:
    raise BighelpCardError(code, message)
