"""bighelp_save_ui_template: an agent keeps a card layout it will fill in again.

Saving is optional. It writes one private template to the profile's existing card
template store, the one bighelp_search_card_templates, bighelp_get_card_template
and bighelp_render_card_template already read, so there's no second template
system. It never renders, publishes, schedules or notifies.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable

from .loopdy_cards import BighelpCardError, render_card, validate_card_input
from .loopdy_cards import canonical_json
from .naming import card_input
from .sensitive import contains_sensitive_credential
from .store import CardTemplateConflict, CardTemplateSaveRejected
from .tools import (
    _TEMPLATE_SLOT,
    _strict_object,
    _substitute_template_parameters,
    _text,
    _validated_template_parameters,
)


TOOL_NAME = "bighelp_save_ui_template"
INTENT = (
    "Save a reusable UI card layout for future rendering with fresh data. This is optional, "
    "not a required step after creating a card. Use it when the user requests reuse, has "
    "repeatedly asked for similar cards, or the layout supports a likely recurring need. Skip "
    "one-off cards and unfinished experiments. Save the structure and parameter definitions, "
    "not personal data or current values. Before creating a template, check for an existing "
    "match and reuse or update it. Saving does not publish or share the template."
)
DESCRIPTION = (
    INTENT
    + " Check first with bighelp_search_card_templates. `layout` is a bighelp_render_card "
    "document (no data_sources) where every value that changes is a {{name}} placeholder in "
    "text or a literal value; `parameters` declares each placeholder once, and optional ones "
    "need a default. To change a saved template, pass its template_id and the version you "
    "read as expected_version. Returns template_id; render it later with "
    "bighelp_render_card_template. When Hermes has progressively disclosed this tool, use "
    "tool_search, tool_describe, and tool_call to invoke this exact tool."
)

MAX_ARGUMENT_BYTES = 81_920
MAX_PARAMETERS = 32
MAX_ENUM_VALUES = 50
MAX_PARAMETER_TEXT = 500
MAX_TEMPLATES = 500  # the most the app's template list reads (native_api.MAX_TEMPLATES)

_ARGUMENTS = {
    "name", "purpose", "usage_guidance", "layout", "parameters", "template_id", "expected_version",
}
_REQUIRED_ARGUMENTS = {"name", "purpose", "usage_guidance", "layout", "parameters"}
_PARAMETER_KEYS = {"name", "type", "description", "required", "default", "enum", "title"}
_PARAMETER_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_TEMPLATE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SAMPLE_VALUES = {"string": "Sample", "integer": 1, "number": 1.5, "boolean": True}


class _Rejected(Exception):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def parameters() -> dict[str, Any]:
    scalar = {"type": ["string", "integer", "number", "boolean"]}
    definition = _strict_object(
        {
            "name": {"type": "string", "pattern": _PARAMETER_NAME.pattern},
            "type": {"type": "string", "enum": ["string", "integer", "number", "boolean"]},
            "description": _text(MAX_PARAMETER_TEXT),
            "required": {"type": "boolean"},
            "default": scalar,
            "enum": {"type": "array", "minItems": 1, "maxItems": MAX_ENUM_VALUES, "items": scalar},
            "title": _text(120),
        },
        ["name", "type", "description", "required"],
    )
    return _strict_object(
        {
            "name": _text(120),
            "purpose": {**_text(600), "description": "What the card shows."},
            "usage_guidance": {**_text(600), "description": "When to pick this template."},
            "layout": {
                "type": "object",
                "description": "A bighelp_render_card document with {{name}} placeholders.",
            },
            "parameters": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_PARAMETERS,
                "items": definition,
            },
            "template_id": {
                "type": "string",
                "pattern": _TEMPLATE_ID.pattern,
                "description": "Only to update a template this agent saved.",
            },
            "expected_version": {
                "type": "integer",
                "minimum": 1,
                "description": "The version you read; required with template_id.",
            },
        },
        sorted(_REQUIRED_ARGUMENTS),
    )


def handler(*, store: Any, profile: str, now: Callable[[], datetime]):
    def handle(payload, **_kwargs):
        try:
            request = _request(payload, now=now())
            result = store.save_card_template(profile=profile, limit=MAX_TEMPLATES, **request)
        except _Rejected as rejected:
            return _rejected(rejected.code, rejected.message, rejected.details)
        except CardTemplateSaveRejected as rejected:
            return _rejected(rejected.code, str(rejected), rejected.details)
        except CardTemplateConflict:
            return _rejected(
                "version_conflict", "The template changed while saving. Get it again and retry.", {},
            )
        template = result["template"]
        return canonical_json({
            "status": result["status"],
            "template_id": template["id"],
            "version": template["version"],
            "sha256": template["sha256"],
            "template": {
                key: template[key]
                for key in (
                    "id", "version", "name", "summary", "author", "license",
                    "minimum_card_version", "sha256",
                )
            },
            "usage_guidance": result["usage_guidance"],
            "parameters_schema": template["parameters_schema"],
            "render_with": "bighelp_render_card_template",
            "note": (
                "Saved privately for this agent; nothing was shown, shared or published. "
                "Render it with bighelp_render_card_template and fresh parameters."
            ),
        })

    return handle


def _rejected(code: str, message: str, details: dict[str, Any]) -> str:
    return canonical_json({
        "status": "rejected",
        "error": {"code": code, "message": message, **details},
    })


def _request(payload: Any, *, now: datetime) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise _Rejected("invalid_arguments", "Arguments must be an object.")
    try:
        size = len(canonical_json(payload).encode("utf-8"))
    except BighelpCardError:
        raise _Rejected("invalid_arguments", "Arguments must be plain JSON with finite numbers.") from None
    if size > MAX_ARGUMENT_BYTES:
        raise _Rejected(
            "payload_too_large",
            f"Arguments are over {MAX_ARGUMENT_BYTES // 1024} KB. Save a smaller layout.",
        )
    unknown = sorted(set(payload) - _ARGUMENTS)
    if unknown:
        raise _Rejected(
            "invalid_arguments",
            "Unknown arguments. A template stores structure only, never current values.",
            arguments=[name for name in unknown if _PARAMETER_NAME.fullmatch(str(name))][:8],
        )
    missing = sorted(_REQUIRED_ARGUMENTS - set(payload))
    if missing:
        raise _Rejected("invalid_arguments", "Required arguments are missing.", arguments=missing)

    name = _plain_text(payload["name"], "name", 120)
    purpose = _plain_text(payload["purpose"], "purpose", 600)
    usage_guidance = _plain_text(payload["usage_guidance"], "usage_guidance", 600)
    template_id = payload.get("template_id")
    expected_version = payload.get("expected_version")
    if template_id is not None and (not isinstance(template_id, str) or not _TEMPLATE_ID.fullmatch(template_id)):
        raise _Rejected("invalid_arguments", "template_id is invalid.")
    if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
        raise _Rejected("invalid_arguments", "expected_version must be a positive integer.")
    if template_id is not None and expected_version is None:
        raise _Rejected(
            "missing_expected_version",
            "To update a template, pass expected_version: the version you got from "
            "bighelp_get_card_template or the last save.",
        )
    if template_id is None and expected_version is not None:
        raise _Rejected("invalid_arguments", "expected_version is only used with template_id.")

    schema = _parameters_schema(payload["parameters"])
    document = _layout(payload["layout"], now=now)
    declared = set(schema["properties"])
    used = _placeholders(document)
    if not declared and not used:
        raise _Rejected(
            "no_parameters",
            "This layout has no {{placeholders}}, so it's a snapshot, not a template. Put each "
            "value that changes behind a placeholder and declare it in parameters.",
        )
    undeclared = sorted(used - declared)
    if undeclared:
        raise _Rejected(
            "undeclared_placeholder",
            "The layout uses placeholders that parameters doesn't declare.",
            placeholders=undeclared,
        )
    unused = sorted(declared - used)
    if unused:
        raise _Rejected(
            "unused_parameter",
            "These parameters never appear in the layout as {{name}}. Use them or remove them.",
            parameters=unused,
        )
    _reject_sensitive(name, purpose, usage_guidance, schema, document)
    _render_check(document, schema, now=now)
    return {
        "name": name,
        "summary": purpose,
        "usage_guidance": usage_guidance,
        "parameters_schema": schema,
        "document": document,
        "template_id": template_id,
        "expected_version": expected_version,
    }


def _plain_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise _Rejected("invalid_arguments", f"{field} must be 1 to {maximum} characters.")
    if any(ord(character) < 32 for character in value):
        raise _Rejected("invalid_arguments", f"{field} can't contain control characters.")
    return value.strip()


def _parameters_schema(value: Any) -> dict[str, Any]:
    if not isinstance(value, list) or len(value) > MAX_PARAMETERS:
        raise _Rejected("invalid_parameter", f"parameters must be a list of at most {MAX_PARAMETERS}.")
    properties: dict[str, Any] = {}
    required: list[str] = []
    for definition in value:
        if not isinstance(definition, dict) or set(definition) - _PARAMETER_KEYS or not {
            "name", "type", "description", "required",
        } <= set(definition):
            raise _Rejected(
                "invalid_parameter",
                "Each parameter needs name, type, description and required, and may add "
                "default, enum and title.",
            )
        name = definition["name"]
        if not isinstance(name, str) or not _PARAMETER_NAME.fullmatch(name):
            raise _Rejected(
                "invalid_parameter",
                "Parameter names start with a letter and use letters, digits, - or _ (64 at most).",
            )
        if name in properties:
            raise _Rejected("invalid_parameter", "Each parameter is declared once.", parameter=name)
        kind = definition["type"]
        if kind not in _SAMPLE_VALUES:
            raise _Rejected(
                "invalid_parameter",
                "Parameter type must be string, integer, number or boolean.",
                parameter=name,
            )
        if type(definition["required"]) is not bool:
            raise _Rejected("invalid_parameter", "required must be true or false.", parameter=name)
        schema: dict[str, Any] = {
            "type": kind,
            "description": _parameter_text(definition["description"], name, "description"),
        }
        if "title" in definition:
            schema["title"] = _parameter_text(definition["title"], name, "title", 120)
        if "enum" in definition:
            choices = definition["enum"]
            if (
                not isinstance(choices, list)
                or not 1 <= len(choices) <= MAX_ENUM_VALUES
                or len({canonical_json(item) for item in choices}) != len(choices)
                or not all(_fits(item, kind) for item in choices)
            ):
                raise _Rejected(
                    "invalid_parameter",
                    f"enum must list 1 to {MAX_ENUM_VALUES} different values of the parameter's type.",
                    parameter=name,
                )
            schema["enum"] = choices
        if "default" in definition:
            default = definition["default"]
            if not _fits(default, kind) or ("enum" in schema and default not in schema["enum"]):
                raise _Rejected(
                    "invalid_parameter",
                    "default must match the parameter's type (and enum, if any).",
                    parameter=name,
                )
            schema["default"] = default
        if definition["required"]:
            required.append(name)
        elif "default" not in schema:
            # The renderer fills an omitted optional parameter from its default; without
            # one, its placeholder would have nothing to show.
            raise _Rejected(
                "invalid_parameter",
                "Optional parameters need a default. Give one, or make it required.",
                parameter=name,
            )
        properties[name] = schema
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required),
        "additionalProperties": False,
    }


def _parameter_text(value: Any, parameter: str, field: str, maximum: int = MAX_PARAMETER_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise _Rejected(
            "invalid_parameter", f"Parameter {field} must be 1 to {maximum} characters.",
            parameter=parameter,
        )
    return value.strip()


def _fits(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str) and len(value) <= MAX_PARAMETER_TEXT
    if kind == "integer":
        return type(value) is int
    if kind == "number":
        return type(value) in {int, float}
    return type(value) is bool


def _layout(value: Any, *, now: datetime) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _Rejected("invalid_arguments", "layout must be a bighelp_render_card document.")
    try:
        return validate_card_input(card_input(value), now=now)
    except BighelpCardError as error:
        message = (
            "Saved templates hold their values in the layout; data_sources must be empty."
            if error.code == "live_data_unavailable"
            else f"The layout isn't a valid bighelp card: {error}."
        )
        raise _Rejected("invalid_layout", message, card_error=error.code) from None


def _placeholders(document: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for text in _strings(document):
        names.update(_TEMPLATE_SLOT.findall(text))
        if "{{" in _TEMPLATE_SLOT.sub("", text) or "}}" in _TEMPLATE_SLOT.sub("", text):
            raise _Rejected(
                "malformed_placeholder",
                "Write placeholders as {{name}}, with no spaces inside the braces.",
            )
    return names


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)


def _reject_sensitive(*values: Any) -> None:
    if any(contains_sensitive_credential(text) for value in values for text in _strings(value)):
        raise _Rejected(
            "sensitive_content",
            "The template looks like it contains a password, key or token. Remove it; "
            "templates keep layout, not secrets.",
        )


def _render_check(document: dict[str, Any], schema: dict[str, Any], *, now: datetime) -> None:
    """Render once with stand-in values, so a saved template is known to render later."""
    samples = {
        name: definition.get("default", (definition.get("enum") or [_SAMPLE_VALUES[definition["type"]]])[0])
        for name, definition in schema["properties"].items()
    }
    values = _validated_template_parameters(schema, samples)
    try:
        filled = _substitute_template_parameters(document, values, declared_names=set(schema["properties"]))
    except ValueError as error:
        if "structural" in str(error):
            raise _Rejected(
                "misplaced_placeholder",
                "Placeholders can only fill text and literal values, not ids, types, formats, "
                "bindings or other structure.",
            ) from None
        raise _Rejected("render_check_failed", "The layout couldn't be filled in with its parameters.") from None
    try:
        render_card(filled, now=now)
    except BighelpCardError as error:
        raise _Rejected(
            "render_check_failed",
            f"Filled with sample values, the card fails: {error}. Check that each placeholder's "
            "type fits where it's used (a title needs a string parameter).",
            card_error=error.code,
        ) from None


__all__ = ["DESCRIPTION", "INTENT", "TOOL_NAME", "handler", "parameters"]
