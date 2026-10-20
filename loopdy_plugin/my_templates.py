"""My Templates: bots people save as templates from the bighelp app, kept on this computer.

Each template is one small JSON file in the Template Catalog's agent shape (name, role, vibe, description,
instructions with `{{agent_name}}` where the name goes, optional variables), so the app shows it in the same
template picker as the catalog. Files live at the Hermes root, `plugin-data/loopdy/my-templates/<id>.json`,
so every agent and every phone on this computer sees the same list.

Reading is lenient and per file: a file that is corrupt, unreadable, too big or missing a field is skipped and
counted, never failing the list. Saving is strict: text is reduced to plain text, checked against the catalog's
limits, and refused if it looks like it holds a key or password. Nothing here leaves the computer except through
the app's own routes.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping
from contextlib import contextmanager
import uuid

from .sensitive import contains_sensitive_credential
from .template_catalog import PLACEHOLDER, plain_text, _parse_variables

try:
    import fcntl
except ImportError:  # Windows: no process-safe lock, so the feature isn't offered.
    fcntl = None


CAPABILITY = "native-my-templates-v1"
SCHEMA_VERSION = 1
SOURCE = "mine"
MAX_TEMPLATES = 40
MAX_FILE_BYTES = 131_072
MAX_INSTRUCTIONS = 20_000
# What one list reply may carry; the route refuses replies over MAX_RESPONSE_BYTES.
LIST_BUDGET_BYTES = 3_500_000
MAX_RESPONSE_BYTES = 4_194_304
# The catalog's own limits (services/catalog in the app repo), with room for a bot's longer role.
LIMITS = {"name": 40, "role": 160, "vibe": 160, "description": 400, "symbol": 60, "category": 40}
_FIELDS = {"name", "role", "vibe", "description", "instructions", "symbol", "category", "variables"}
_STORED = _FIELDS | {"id", "source", "updatedAt"}
_SCAN_LIMIT = MAX_TEMPLATES * 4
_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_SYMBOL = re.compile(r"[a-z0-9.]+\Z")
_UPDATED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_lock = threading.Lock()
logger = logging.getLogger("hermes.plugins.bighelp")


class MyTemplateError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def _invalid(message: str = "The template is missing a name or instructions, or has text that's too long.") -> MyTemplateError:
    return MyTemplateError(422, "invalid_template", message)


def directory() -> Path:
    """The Hermes root, not a profile's home: one list for the whole computer."""
    try:
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
    except ImportError:
        from hermes_constants import get_hermes_home
        root = get_hermes_home()
    return Path(root) / "plugin-data" / "loopdy" / "my-templates"


def available() -> bool:
    # Feeds /native/context, which every app reads: an odd host turns My Templates off, never the plugin.
    if fcntl is None or os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        return False
    try:
        directory()
    except Exception:
        return False
    return True


def store() -> "MyTemplatesStore":
    return MyTemplatesStore(directory())


# MARK: - Checking a template


def _line(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _invalid()
    text = " ".join(plain_text(value.replace("\r", " ").replace("\n", " "), len(value) + 1).split())
    if len(text) > LIMITS[field]:
        raise _invalid()
    return text


def _clean(template: Mapping[str, Any], *, stored: bool) -> dict[str, Any]:
    """The template as it is kept and listed. Raises for anything the catalog wouldn't take."""
    if not isinstance(template, Mapping) or set(template) - (_STORED if stored else _FIELDS):
        raise _invalid()
    name = _line(template.get("name"), "name")
    instructions = template.get("instructions")
    if not name or PLACEHOLDER.search(name) or "{{" in name or "}}" in name:
        raise _invalid()
    if not isinstance(instructions, str):
        raise _invalid()
    if len(instructions) > MAX_INSTRUCTIONS:
        raise MyTemplateError(413, "template_too_large", "The instructions are too long for a template.")
    instructions = plain_text(instructions, MAX_INSTRUCTIONS, multiline=True)
    if not instructions:
        raise _invalid()
    row: dict[str, Any] = {"name": name, "role": _line(template.get("role"), "role"), "instructions": instructions}
    for field in ("vibe", "description", "category"):
        text = _line(template.get(field), field)
        if text:
            row[field] = text
    symbol = _line(template.get("symbol"), "symbol")
    if symbol:
        if _SYMBOL.fullmatch(symbol) is None:
            raise _invalid()
        row["symbol"] = symbol
    raw_variables = template.get("variables")
    if raw_variables is not None and not isinstance(raw_variables, list):
        raise _invalid()
    variables = _parse_variables(raw_variables or [])
    if variables:
        row["variables"] = variables
    if any(contains_sensitive_credential(text) for text in _texts(row)):
        raise MyTemplateError(422, "secret_scan_blocked", "Templates can't hold keys or passwords.")
    return row


def _texts(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


def _timestamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _no_constants(_value: str) -> Any:
    raise ValueError("Non-finite JSON number")


# MARK: - The store


class MyTemplatesStore:
    def __init__(self, directory: Path, now: Callable[[], float] = time.time):
        self.directory = Path(directory)
        self._now = now

    def list(self) -> dict[str, Any]:
        """Every readable template, newest first, and how many were left out."""
        rows: list[dict[str, Any]] = []
        skipped = 0
        for entry in self._entries():
            row = self._read(entry)
            if row is None:
                skipped += 1
            else:
                rows.append(row)
        rows.sort(key=lambda row: (row["updatedAt"], row["id"]), reverse=True)
        kept: list[dict[str, Any]] = []
        used = 0
        for row in rows:
            size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if used + size > LIST_BUDGET_BYTES:
                skipped += 1
                continue
            used += size
            kept.append(row)
        return {"templates": kept, "skipped": skipped}

    def save(self, template: Mapping[str, Any]) -> dict[str, Any]:
        row = _clean(template, stored=False)
        identifier = str(uuid.uuid4())
        row = {"id": identifier, **row, "source": SOURCE, "updatedAt": _timestamp(self._now())}
        encoded = json.dumps({"schemaVersion": SCHEMA_VERSION, "template": row}, ensure_ascii=False,
                             separators=(",", ":"), sort_keys=True, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise MyTemplateError(413, "template_too_large", "The template is too large to save.")
        with self._locked(create=True):
            if sum(1 for _ in self._entries()) >= MAX_TEMPLATES:
                raise MyTemplateError(409, "store_full",
                                      f"This computer keeps up to {MAX_TEMPLATES} templates. Delete one first.")
            self._write(f"{identifier}.json", encoded)
        return row

    def delete(self, template_id: Any) -> bool:
        if not isinstance(template_id, str) or _ID.fullmatch(template_id) is None:
            raise MyTemplateError(422, "invalid_request", "The template ID is invalid.")
        if not self.directory.is_dir():
            return False
        with self._locked(create=False):
            path = self.directory / f"{template_id}.json"
            try:
                if not path.is_symlink() and path.is_dir():
                    return False
                path.unlink()
            except FileNotFoundError:
                return False
        return True

    # MARK: Files

    def _entries(self) -> list[os.DirEntry]:
        try:
            with os.scandir(self.directory) as scan:
                entries = []
                for entry in scan:
                    if entry.name.endswith(".json") and not entry.name.startswith("."):
                        entries.append(entry)
                        if len(entries) >= _SCAN_LIMIT:
                            break
                return entries
        except FileNotFoundError:
            return []
        except OSError:
            raise MyTemplateError(503, "my_templates_unavailable", "Saved templates can't be read on this host.") from None

    def _read(self, entry: os.DirEntry) -> dict[str, Any] | None:
        identifier = entry.name[: -len(".json")]
        if _ID.fullmatch(identifier) is None:
            return self._skip("bad_name")
        try:
            descriptor = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
        except OSError:
            return self._skip("unreadable")
        try:
            info = os.fstat(descriptor)
            if not (info.st_mode & 0o170000 == 0o100000) or info.st_size > MAX_FILE_BYTES:
                return self._skip("not_a_small_file")
            with os.fdopen(descriptor, "rb", closefd=False) as file:
                content = file.read(MAX_FILE_BYTES + 1)
        except OSError:
            return self._skip("unreadable")
        finally:
            os.close(descriptor)
        if len(content) > MAX_FILE_BYTES:
            return self._skip("not_a_small_file")
        try:
            document = json.loads(content.decode("utf-8"), parse_constant=_no_constants)
        except (ValueError, UnicodeError, RecursionError):
            return self._skip("corrupt")
        if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
            return self._skip("unknown_schema")
        stored = document.get("template")
        if not isinstance(stored, dict) or stored.get("id") != identifier:
            return self._skip("incomplete")
        try:
            row = _clean(stored, stored=True)
        except MyTemplateError as error:
            return self._skip(error.code)
        updated = stored.get("updatedAt")
        if not isinstance(updated, str) or _UPDATED.fullmatch(updated) is None:
            return self._skip("incomplete")
        return {"id": identifier, **row, "source": SOURCE, "updatedAt": updated}

    @staticmethod
    def _skip(reason: str) -> None:
        # A fixed code only: file names and contents stay on the computer.
        logger.warning("bighelp saved template skipped: %s", reason)
        return None

    def _write(self, name: str, encoded: bytes) -> None:
        handle, temporary = tempfile.mkstemp(prefix=".saving-", dir=self.directory)
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.directory / name)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @contextmanager
    def _locked(self, *, create: bool) -> Iterator[None]:
        """One writer at a time, across the plugin's two module copies and every Hermes process."""
        if create:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
        if fcntl is None:
            raise MyTemplateError(503, "my_templates_unavailable", "Saved templates need a POSIX host.")
        with _lock:
            descriptor = os.open(self.directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)
