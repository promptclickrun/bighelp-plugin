"""Read bighelp's public Template Catalog and fill its agent templates, for the template tools.

The catalog is public, read-only content from catalog.bighelp.app. This module fetches only that one address, keeps
a copy in the plugin data folder, checks for a newer one at most every six hours (with the ETag), and keeps the last
good copy when the catalog can't be reached. Community text is untrusted, so it is reduced to plain text here.

Filling follows the template variables contract (services/catalog/docs/TEMPLATE_VARIABLES.md in the app repo), the
same rules the app's form uses.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

logger = logging.getLogger("hermes.plugins.bighelp")

CATALOG_HOST = "catalog.bighelp.app"
CATALOG_URL = f"https://{CATALOG_HOST}/v1/catalog.json"
MAX_CATALOG_BYTES = 1_048_576
REFRESH_SECONDS = 6 * 3600
# After a failed check, wait this long before the next one so tool calls don't hammer the catalog.
RETRY_SECONDS = 15 * 60
TIMEOUT_SECONDS = 10.0
_COPY_FILE = "catalog.json"

RESERVED_KEYS = ("agent_name", "user_name")
MAX_VARIABLES = 12
KEY = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
PLACEHOLDER = re.compile(r"\{\{([a-z][a-z0-9_]{0,39})\}\}")
TEMPLATE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}\Z")
TYPES = ("text", "long_text", "choice", "number")
DEFAULT_MAX_LENGTH = {"text": 80, "long_text": 600}
LIMIT_MAX_LENGTH = {"text": 200, "long_text": 4000}
AGENT_NAME_MAX = 80
USER_NAME_MAX = 40
NUMBER_MAX_LENGTH = 40
OTHER_MAX_LENGTH = 200
MISSING_REASON = "Required. Ask the person for it."
# Text the template places a value into. The SOUL text is first, so the form asks in reading order.
FILLED_FIELDS = ("instructions", "role", "description")

# Lenient caps: the catalog's own limits are smaller, so these only stop runaway content.
_AGENT_TEXT = {"name": 80, "role": 160, "vibe": 240, "description": 480, "category": 40, "symbol": 60,
               "credit": 60, "updatedAt": 40}
_BLUEPRINT_TEXT = {"board": 20, "category": 40, "goalCategory": 40, "credit": 60, "updatedAt": 40}
MAX_INSTRUCTIONS = 20_000
MAX_BLUEPRINT_TEXT = 2_000
MAX_ENTRIES = 2_000
# Zero-width joiners hold emoji sequences together, so they stay.
_JOINERS = ("\u200c", "\u200d")


class CatalogUnavailable(Exception):
    """No copy of the catalog on this computer, and the catalog can't be reached."""


class FetchError(Exception):
    """A fetch failed. The message is a fixed code, never server or exception text."""


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


@dataclass(frozen=True)
class Catalog:
    revision: str = ""
    agents: list[dict[str, Any]] = field(default_factory=list)
    blueprints: list[dict[str, Any]] = field(default_factory=list)


# MARK: - Fetching


def allowed_url(url: str) -> bool:
    """Only https://catalog.bighelp.app on the default port, with no user info."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    return (parts.scheme == "https" and parts.hostname == CATALOG_HOST and port in (None, 443)
            and parts.username is None and parts.password is None and "@" not in parts.netloc)


class _CatalogRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only while they stay on the catalog host."""

    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not allowed_url(newurl):
            raise FetchError("catalog_redirect_refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def https_fetch(url: str, headers: Mapping[str, str], *, timeout: float, max_bytes: int) -> Response:
    """GET ``url`` with a timeout, reading at most ``max_bytes + 1`` bytes."""
    if not allowed_url(url):
        raise FetchError("catalog_url_refused")
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener = urllib.request.build_opener(_CatalogRedirects())
    try:
        with opener.open(request, timeout=timeout) as answer:
            body = answer.read(max_bytes + 1)
            return Response(status=answer.status, headers={k.lower(): v for k, v in answer.headers.items()},
                            body=body, url=answer.geturl())
    except urllib.error.HTTPError as error:
        # 304 arrives as an HTTPError. Its body is never read.
        try:
            return Response(status=error.code, headers={k.lower(): v for k, v in (error.headers or {}).items()},
                            body=b"", url=error.geturl() or url)
        finally:
            error.close()
    except FetchError:
        raise
    except (urllib.error.URLError, OSError, ValueError):
        raise FetchError("catalog_unreachable") from None


class CatalogClient:
    """The catalog with a saved copy, an ETag and a six-hour refresh."""

    def __init__(self, *, directory: Path, fetch: Callable[..., Response] = https_fetch,
                 clock: Callable[[], float] = time.time):
        self.directory = Path(directory)
        self._fetch = fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._copy: dict[str, Any] | None = None
        self._parsed: Catalog | None = None
        self._failed_at: float | None = None

    def catalog(self) -> Catalog:
        with self._lock:
            now = self._clock()
            if self._copy is None:
                self._copy = self._read_copy()
                self._parsed = None
            copy = self._copy
            due = copy is None or now - float(copy.get("checkedAt", 0)) >= REFRESH_SECONDS
            backing_off = self._failed_at is not None and now - self._failed_at < RETRY_SECONDS
            if due and not backing_off:
                try:
                    self._refresh(now)
                    self._failed_at = None
                except FetchError as error:
                    self._failed_at = now
                    logger.info("bighelp template catalog check failed: %s", error.args[0] if error.args else "")
            if self._copy is None:
                raise CatalogUnavailable("catalog_unavailable")
            if self._parsed is None:
                self._parsed = parse_catalog(self._copy["document"])
            return self._parsed

    def _refresh(self, now: float) -> None:
        headers = {"Accept": "application/json", "User-Agent": "bighelp-plugin"}
        if self._copy is not None and self._copy.get("etag"):
            headers["If-None-Match"] = self._copy["etag"]
        response = self._fetch(CATALOG_URL, headers, timeout=TIMEOUT_SECONDS, max_bytes=MAX_CATALOG_BYTES)
        if not allowed_url(response.url):
            raise FetchError("catalog_url_refused")
        if response.status == 304 and self._copy is not None:
            self._copy = dict(self._copy, checkedAt=now)
            self._write_copy(self._copy)
            return
        if response.status != 200:
            raise FetchError("catalog_status")
        length = response.headers.get("content-length")
        if length is not None and (not str(length).strip().isdigit() or int(length) > MAX_CATALOG_BYTES):
            raise FetchError("catalog_too_large")
        if len(response.body) > MAX_CATALOG_BYTES:
            raise FetchError("catalog_too_large")
        try:
            document = json.loads(response.body.decode("utf-8"))
            parsed = parse_catalog(document)
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise FetchError("catalog_invalid") from None
        etag = str(response.headers.get("etag") or "")[:200]
        self._copy = {"etag": etag, "checkedAt": now, "document": document}
        self._parsed = parsed
        self._write_copy(self._copy)

    def _read_copy(self) -> dict[str, Any] | None:
        path = self.directory / _COPY_FILE
        try:
            if path.stat().st_size > MAX_CATALOG_BYTES * 2:
                return None
            saved = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("document"), dict):
                return None
            parse_catalog(saved["document"])
            checked = saved.get("checkedAt")
            if not isinstance(checked, (int, float)) or isinstance(checked, bool):
                saved["checkedAt"] = 0
            return saved
        except (OSError, ValueError, RecursionError):
            return None

    def _write_copy(self, copy: dict[str, Any]) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=self.directory, prefix=".catalog-", suffix=".json")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(copy, stream, ensure_ascii=False)
                os.replace(temporary, self.directory / _COPY_FILE)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise
        except OSError:
            # The copy in memory still serves this process.
            logger.warning("bighelp template catalog copy not saved")


# MARK: - Parsing


def plain_text(value: Any, limit: int, *, multiline: bool = False) -> str:
    """Untrusted text as plain text: no control or direction characters, at most ``limit`` characters."""
    if not isinstance(value, str):
        return ""
    if multiline:
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    kept = []
    for character in value:
        if character == "\n" and multiline or character == "\t":
            kept.append(character)
            continue
        category = unicodedata.category(character)
        if category == "Cc" or (category == "Cf" and character not in _JOINERS) or category in ("Zl", "Zp"):
            continue
        kept.append(character)
    return "".join(kept).strip()[:limit]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _parse_variable(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    key = raw.get("key")
    if not isinstance(key, str) or KEY.match(key) is None or key in RESERVED_KEYS:
        return None
    kind = raw.get("type") if raw.get("type") in TYPES else "text"
    options: list[str] = []
    if kind == "choice":
        raw_options = raw.get("options") if isinstance(raw.get("options"), list) else []
        for option in raw_options[:MAX_VARIABLES]:
            text = plain_text(option, 60)
            if text and text not in options:
                options.append(text)
        if len(options) < 2:
            kind = "text"
    required = raw.get("required") is not False
    variable: dict[str, Any] = {
        "key": key,
        "label": plain_text(raw.get("label"), 40) or label_for(key),
        "type": kind,
        "required": required,
    }
    if kind in DEFAULT_MAX_LENGTH:
        length = raw.get("maxLength")
        if isinstance(length, bool) or not isinstance(length, int) or length < 1:
            length = DEFAULT_MAX_LENGTH[kind]
        variable["maxLength"] = min(length, LIMIT_MAX_LENGTH[kind])
    if kind == "choice":
        variable["options"] = options
        if raw.get("allowOther") is True:
            variable["allowOther"] = True
    if kind == "number":
        for bound in ("min", "max"):
            number = _number(raw.get(bound))
            if number is not None:
                variable[bound] = int(number) if number.is_integer() else number
    default = raw.get("default")
    if default is not None:
        if kind == "choice":
            text = plain_text(default, 60)
            if text in options:
                variable["default"] = text
        elif kind == "number":
            if _number(default) is not None or isinstance(default, str):
                variable["default"] = plain_text(str(default), NUMBER_MAX_LENGTH)
        else:
            text = plain_text(default, variable["maxLength"], multiline=kind == "long_text")
            if text:
                variable["default"] = text
    for name, limit in (("example", 120), ("help", 160)):
        text = plain_text(raw.get(name), limit)
        if text:
            variable[name] = text
    if not required:
        text = plain_text(raw.get("whenEmpty"), 200)
        if text:
            variable["whenEmpty"] = text
    return variable


def _parse_variables(raw: Any) -> list[dict[str, Any]]:
    variables: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw if isinstance(raw, list) else []:
        variable = _parse_variable(entry)
        if variable is None or variable["key"] in seen:
            continue
        seen.add(variable["key"])
        variables.append(variable)
        if len(variables) == MAX_VARIABLES:
            break
    return variables


def _source(value: Any) -> str:
    # Anything not marked as bighelp's own is treated as community text.
    return "bighelp" if value == "bighelp" else "community"


def _parse_agent(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    template_id = raw.get("id")
    instructions = raw.get("instructions")
    if not isinstance(template_id, str) or TEMPLATE_ID.match(template_id) is None:
        return None
    if not isinstance(instructions, str) or len(instructions) > MAX_INSTRUCTIONS:
        return None
    agent: dict[str, Any] = {"id": template_id}
    for name, limit in _AGENT_TEXT.items():
        text = plain_text(raw.get(name), limit)
        if text:
            agent[name] = text
    agent["instructions"] = plain_text(instructions, MAX_INSTRUCTIONS, multiline=True)
    if not agent.get("name") or not agent["instructions"]:
        return None
    agent["source"] = _source(raw.get("source"))
    agent["variables"] = _parse_variables(raw.get("variables"))
    return agent


def _parse_blueprint(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    template_id = raw.get("id")
    text = raw.get("text")
    if not isinstance(template_id, str) or TEMPLATE_ID.match(template_id) is None:
        return None
    if not isinstance(text, str) or len(text) > MAX_BLUEPRINT_TEXT:
        return None
    blueprint: dict[str, Any] = {"id": template_id}
    for name, limit in _BLUEPRINT_TEXT.items():
        value = plain_text(raw.get(name), limit)
        if value:
            blueprint[name] = value
    blueprint["text"] = plain_text(text, MAX_BLUEPRINT_TEXT, multiline=True)
    if not blueprint["text"]:
        return None
    blueprint["source"] = _source(raw.get("source"))
    return blueprint


def _parse_list(raw: Any, parse: Callable[[Any], dict[str, Any] | None]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw[:MAX_ENTRIES] if isinstance(raw, list) else []:
        entry = parse(item)
        if entry is not None and entry["id"] not in seen:
            seen.add(entry["id"])
            entries.append(entry)
    return entries


def parse_catalog(document: Any) -> Catalog:
    """A catalog document, leniently: unknown keys are ignored and broken entries skipped."""
    if not isinstance(document, dict):
        raise ValueError("catalog_not_an_object")
    return Catalog(
        revision=plain_text(document.get("revision"), 100),
        agents=_parse_list(document.get("agents"), _parse_agent),
        blueprints=_parse_list(document.get("blueprints"), _parse_blueprint),
    )


# MARK: - Form and filling


def label_for(key: str) -> str:
    """"operating_context" → "Operating context"."""
    words = key.replace("_", " ").strip()
    return (words[:1].upper() + words[1:])[:40]


def placeholders(template: Mapping[str, Any]) -> list[str]:
    """Keys the template's text uses, in order of first use."""
    keys: list[str] = []
    for name in FILLED_FIELDS:
        text = template.get(name)
        for key in PLACEHOLDER.findall(text if isinstance(text, str) else ""):
            if key not in keys:
                keys.append(key)
    return keys


def _reserved_field(key: str) -> dict[str, Any]:
    if key == "agent_name":
        return {"key": key, "label": "Name", "type": "text", "required": True, "maxLength": AGENT_NAME_MAX,
                "source": "reserved"}
    return {"key": key, "label": "Your name", "type": "text", "required": True, "maxLength": USER_NAME_MAX,
            "source": "reserved"}


def form_fields(template: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the form asks for: the name, the person's name if used, declared variables, then undeclared ones."""
    used = placeholders(template)
    fields = [_reserved_field("agent_name")]
    if "user_name" in used:
        fields.append(_reserved_field("user_name"))
    declared = {variable["key"]: variable for variable in template.get("variables") or []}
    for variable in template.get("variables") or []:
        if variable["key"] in used:
            fields.append(dict(variable, source="declared"))
    for key in used:
        if key not in declared and key not in RESERVED_KEYS:
            # An older or hand-made template: ask for it as a required line of text.
            fields.append({"key": key, "label": label_for(key), "type": "text", "required": True,
                           "maxLength": DEFAULT_MAX_LENGTH["text"], "source": "undeclared"})
    return fields


def _without_braces(text: str) -> str:
    # Removing one pair can join two halves into a new pair, so repeat until none are left.
    while "{{" in text or "}}" in text:
        text = text.replace("{{", "").replace("}}", "")
    return text


def _clean_value(value: str, *, multiline: bool) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if not multiline:
        value = re.sub(r"[\n  ]", " ", value)
    value = "".join(c for c in value if c in "\n\t" or unicodedata.category(c) != "Cc")
    return _without_braces(value).strip()


def _format_number(number: float) -> str:
    return str(int(number)) if number.is_integer() and abs(number) < 1e15 else repr(number)


def _invalid(field: Mapping[str, Any], reason: str) -> dict[str, str]:
    return {"key": field["key"], "label": field["label"], "reason": reason}


def _resolve(field: Mapping[str, Any], raw: Any) -> tuple[str | None, str | None]:
    """``(value, None)``, ``(None, reason)`` when invalid, or ``(None, None)`` when empty."""
    kind = field["type"]
    if raw is None:
        text = ""
    elif isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None, "Must be text or a number."
    elif isinstance(raw, str):
        text = _clean_value(raw, multiline=kind == "long_text")
    else:
        number = _number(raw)
        if number is None:
            return None, "Must be a finite number."
        text = _format_number(number)
    if not text and "default" in field:
        text = field["default"]
    if not text:
        return None, None
    if kind == "choice":
        for option in field["options"]:
            if option.casefold() == text.casefold():
                return option, None
        if field.get("allowOther") and len(text) <= OTHER_MAX_LENGTH:
            return text, None
        return None, f"Pick one of: {', '.join(field['options'])}."
    if kind == "number":
        try:
            number = float(text) if len(text) <= NUMBER_MAX_LENGTH else math.nan
        except ValueError:
            number = math.nan
        if not math.isfinite(number):
            return None, "Must be a number."
        if "min" in field and number < field["min"]:
            return None, f"Must be at least {field['min']}."
        if "max" in field and number > field["max"]:
            return None, f"Must be at most {field['max']}."
        return (text if isinstance(raw, str) and re.fullmatch(r"-?\d+(\.\d+)?", text) else _format_number(number)), None
    limit = field.get("maxLength", DEFAULT_MAX_LENGTH["text"])
    if len(text) > limit:
        return None, f"At most {limit} characters."
    return text, None


def fill(template: Mapping[str, Any], values: Mapping[str, Any], *, agent_name: Any) -> dict[str, Any]:
    """Fill an agent template with no side effects, and say what is missing or wrong."""
    fields = form_fields(template)
    supplied = dict(values)
    supplied.pop("agent_name", None)
    keys = {f["key"] for f in fields}
    ignored = sorted(key for key in values if key == "agent_name" or key not in keys)
    filled: dict[str, str] = {}
    missing: list[dict[str, str]] = []
    invalid: list[dict[str, str]] = []
    for field_spec in fields:
        key = field_spec["key"]
        raw = agent_name if key == "agent_name" else supplied.get(key)
        value, reason = _resolve(field_spec, raw)
        if reason is not None:
            invalid.append(_invalid(field_spec, reason))
        elif value is not None:
            filled[key] = value
        elif field_spec["required"]:
            missing.append({"key": key, "label": field_spec["label"], "reason": MISSING_REASON})
        else:
            filled[key] = field_spec.get("whenEmpty", "")

    def replace(match: re.Match[str]) -> str:
        return filled.get(match.group(1), match.group(0))

    result: dict[str, Any] = {}
    for name in FILLED_FIELDS:
        text = template.get(name) if isinstance(template.get(name), str) else ""
        if "{{" in PLACEHOLDER.sub("", text) or "}}" in PLACEHOLDER.sub("", text):
            invalid.append({"key": name, "label": label_for(name),
                            "reason": "The template has a broken placeholder, so it can't be filled."})
        # One pass: a value is never read again as a placeholder.
        result[name] = PLACEHOLDER.sub(replace, text)
    return {
        "complete": not missing and not invalid,
        "missing": missing,
        "invalid": invalid,
        "ignored": ignored,
        **result,
    }
