"""App state: the bighelp app's own choices, kept for each person on this computer.

The app saves a person's layout (the order of their bots, sections, group chat pins) and its settings for each
bot here, so another phone or a reinstall gets them back. The values belong to the app: the plugin checks only
their size and shape, and that no text in them looks like a key or password. Each key carries a revision the
app picks, so the copies on all of a person's computers end up with the newest one (docs/APP_STATE.md).

Files live at the Hermes root, `plugin-data/loopdy/app-state/`: `computer.json` holds this computer's ID, made
once and never changed, and `people/<person>.json` one person's keys.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable, Iterator, Mapping
import unicodedata
import uuid

from .sensitive import contains_credential_shape

try:
    import fcntl
except ImportError:  # Windows: no process-safe lock, so the feature isn't offered.
    fcntl = None


CAPABILITY = "native-app-state-v1"
SCHEMA_VERSION = 1
MAX_KEYS = 24
MAX_VALUE_BYTES = 65_536
MAX_STRING = 4_096
MAX_DEPTH = 16
MAX_FILE_BYTES = 524_288
MAX_PEOPLE = 64
MAX_REVISION = 2**53 - 1
MAX_RESPONSE_BYTES = 1_048_576
KEY_PATTERN = r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+){0,5}$"
UUID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
_KEY = re.compile(KEY_PATTERN)
_UUID = re.compile(UUID_PATTERN)
_UPDATED = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_TEXT_CONTROLS = {"\t", "\n", "\r"}
_lock = threading.Lock()
logger = logging.getLogger("hermes.plugins.bighelp")


class AppStateError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        self.status, self.code, self.message = status, code, message
        self.details = details or {}
        super().__init__(message)


def _invalid(message: str = "The app state request is invalid.") -> AppStateError:
    return AppStateError(422, "invalid_request", message)


def _too_large() -> AppStateError:
    return AppStateError(413, "app_state_too_large", "This is too much to keep for the app.")


def directory() -> Path:
    """The Hermes root, not a profile's home: one store for the whole computer."""
    try:
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
    except ImportError:
        from hermes_constants import get_hermes_home
        root = get_hermes_home()
    return Path(root) / "plugin-data" / "loopdy" / "app-state"


def available() -> bool:
    # Feeds /native/context, which every app reads: an odd host turns app state off, never the plugin.
    if fcntl is None or os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        return False
    try:
        directory()
    except Exception:
        return False
    return True


def store() -> "AppStateStore":
    return AppStateStore(directory())


# MARK: - Checking a value


def _check_text(text: str) -> None:
    if len(text) > MAX_STRING:
        raise _too_large()
    if any(character not in _TEXT_CONTROLS and unicodedata.category(character) == "Cc" for character in text):
        raise _invalid("Text in the app state has control characters.")
    # IDs and links are the data here, so the prose rule for `name: <long token>` would refuse them.
    if contains_credential_shape(text):
        raise AppStateError(422, "secret_scan_blocked", "The app state can't hold keys or passwords.")


def encoded_value(value: Any) -> bytes:
    """The value as compact JSON, after checking every part of it. Raises for anything the store won't keep."""
    pending: list[tuple[Any, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise _invalid("The app state nests too deeply.")
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, int):
            if abs(item) > MAX_REVISION:
                raise _invalid("A number in the app state is too large.")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise _invalid("A number in the app state isn't finite.")
        elif isinstance(item, str):
            _check_text(item)
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, dict):
            for name, child in item.items():
                if not isinstance(name, str):
                    raise _invalid()
                _check_text(name)
                pending.append((child, depth + 1))
        else:
            raise _invalid()
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                         allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_VALUE_BYTES:
        raise _too_large()
    return encoded


def _timestamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _no_constants(_value: str) -> Any:
    raise ValueError("Non-finite JSON number")


def _is_revision(value: Any, *, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= MAX_REVISION


# MARK: - The store


class AppStateStore:
    def __init__(self, directory: Path, now: Callable[[], float] = time.time):
        self.directory = Path(directory)
        self.people = self.directory / "people"
        self._now = now

    def read(self, person: str, keys: list[str] | None = None) -> dict[str, Any]:
        """The person's keys (only `keys` when given), and this computer's ID."""
        _check_ids(person)
        computer = self._computer_id()
        entries = self._load(person)
        if keys is not None:
            wanted = set(keys)
            entries = {key: entry for key, entry in entries.items() if key in wanted}
        return {"computerId": computer, "keys": entries}

    def write(self, person: str, device: str, changes: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """Saves every change or none. Raises `app_state_conflict` when another device wrote a newer revision."""
        _check_ids(person, device)
        if not changes or len(changes) > MAX_KEYS:
            raise _invalid()
        encoded: dict[str, bytes] = {}
        for key, change in changes.items():
            if not isinstance(key, str) or _KEY.fullmatch(key) is None or len(key) > 64:
                raise _invalid("A key name is invalid.")
            revision, base = change.get("revision"), change.get("baseRevision")
            if not _is_revision(revision, minimum=1) or not _is_revision(base, minimum=0) or revision <= base:
                raise _invalid("A revision is invalid.")
            if "value" not in change:
                raise _invalid()
            encoded[key] = encoded_value(change["value"])
        with self._locked():
            computer = self._computer_id(locked=True)
            path = self.people / f"{person}.json"
            known = path.exists()
            if not known and self._people_count() >= MAX_PEOPLE:
                raise AppStateError(409, "store_full",
                                    f"This computer keeps app state for up to {MAX_PEOPLE} people.")
            entries = self._load(person)
            conflicts = {key: entries[key] for key, change in changes.items()
                         if key in entries and entries[key]["revision"] > change["baseRevision"]}
            if conflicts:
                raise AppStateError(409, "app_state_conflict",
                                    "Another device changed this first. Get it again and retry.", {"keys": conflicts})
            if len(set(entries) | set(changes)) > MAX_KEYS:
                raise AppStateError(409, "store_full", f"The app keeps up to {MAX_KEYS} keys for each person.")
            updated = _timestamp(self._now())
            for key, change in changes.items():
                entries[key] = {"value": change["value"], "revision": change["revision"],
                                "updatedAt": updated, "device": device}
            document = json.dumps({"schemaVersion": SCHEMA_VERSION, "person": person, "keys": entries},
                                  ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                                  allow_nan=False).encode("utf-8")
            if len(document) > MAX_FILE_BYTES:
                raise _too_large()
            self._write(path, document)
        return {"computerId": computer, "keys": {key: entries[key] for key in changes}}

    # MARK: This computer's ID

    def _computer_id(self, *, locked: bool = False) -> str:
        """Made the first time, then the same forever. A file that can't be read is an error, never a new ID:
        a new ID would make every phone think these are another computer's bots."""
        path = self.directory / "computer.json"
        found = self._read_computer(path)
        if found is not None:
            return found
        if not locked:
            with self._locked():
                return self._computer_id(locked=True)
        if path.exists() or path.is_symlink():
            logger.warning("bighelp app state unavailable: computer_id_unreadable")
            raise AppStateError(503, "app_state_unavailable", "The app state on this computer can't be read.")
        computer = str(uuid.uuid4())
        self._write(path, json.dumps({"schemaVersion": SCHEMA_VERSION, "computerId": computer},
                                     separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return computer

    @staticmethod
    def _read_computer(path: Path) -> str | None:
        content = _small_file(path, 4_096)
        if content is None:
            return None
        try:
            document = json.loads(content.decode("utf-8"), parse_constant=_no_constants)
        except (ValueError, UnicodeError, RecursionError):
            return None
        if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION:
            return None
        computer = document.get("computerId")
        return computer if isinstance(computer, str) and _UUID.fullmatch(computer) else None

    # MARK: Files

    def _load(self, person: str) -> dict[str, dict[str, Any]]:
        """The person's readable entries. Anything wrong with the file or an entry reads as missing."""
        path = self.people / f"{person}.json"
        if not path.exists() and not path.is_symlink():
            return {}
        content = _small_file(path, MAX_FILE_BYTES)
        if content is None:
            return self._skip("not_a_small_file")
        try:
            document = json.loads(content.decode("utf-8"), parse_constant=_no_constants)
        except (ValueError, UnicodeError, RecursionError):
            return self._skip("corrupt")
        if not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION \
                or document.get("person") != person or not isinstance(document.get("keys"), dict):
            return self._skip("unknown_schema")
        entries: dict[str, dict[str, Any]] = {}
        for key, entry in list(document["keys"].items())[:MAX_KEYS]:
            if not isinstance(key, str) or _KEY.fullmatch(key) is None or not isinstance(entry, dict) \
                    or set(entry) != {"value", "revision", "updatedAt", "device"} \
                    or not _is_revision(entry["revision"], minimum=1) \
                    or not isinstance(entry["updatedAt"], str) or _UPDATED.fullmatch(entry["updatedAt"]) is None \
                    or not isinstance(entry["device"], str) or _UUID.fullmatch(entry["device"]) is None:
                logger.warning("bighelp app state entry skipped: incomplete")
                continue
            try:
                encoded_value(entry["value"])
            except AppStateError as error:
                logger.warning("bighelp app state entry skipped: %s", error.code)
                continue
            entries[key] = entry
        return entries

    @staticmethod
    def _skip(reason: str) -> dict:
        # A fixed code only: person IDs and values stay on the computer.
        logger.warning("bighelp app state unreadable: %s", reason)
        return {}

    def _people_count(self) -> int:
        try:
            with os.scandir(self.people) as scan:
                count = 0
                for entry in scan:
                    if entry.name.endswith(".json") and not entry.name.startswith("."):
                        count += 1
                        if count >= MAX_PEOPLE:
                            break
                return count
        except FileNotFoundError:
            return 0
        except OSError:
            raise AppStateError(503, "app_state_unavailable", "The app state on this computer can't be read.") from None

    @staticmethod
    def _write(path: Path, encoded: bytes) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".saving-", dir=path.parent)
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """One writer at a time, across the plugin's two module copies and every Hermes process."""
        if fcntl is None:
            raise AppStateError(503, "app_state_unavailable", "App state needs a POSIX host.")
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.directory, 0o700)
            self.people.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            raise AppStateError(503, "app_state_unavailable", "The app state on this computer can't be saved.") from None
        with _lock:
            descriptor = os.open(self.directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)


def _check_ids(*values: Any) -> None:
    for value in values:
        if not isinstance(value, str) or _UUID.fullmatch(value) is None:
            raise _invalid("A person or device ID is invalid.")


def _small_file(path: Path, maximum: int) -> bytes | None:
    """A regular file's bytes, never through a link; None when it isn't one or is over `maximum`."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if info.st_mode & 0o170000 != 0o100000 or info.st_size > maximum:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as file:
            content = file.read(maximum + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return content if len(content) <= maximum else None
