"""What Hermes' marketplace knows about each catalog plugin that its dashboard catalog leaves out.

Hermes gives apps its plugin catalog as ``plugin-catalog.json`` (``/api/dashboard/plugins/catalog``): the
entries, without the signals its marketplace page and Hermes Desktop rank them by. Those read
``plugins.json`` from the same docs site, which adds each entry's GitHub stars, its curated featured rank, and
when it was added and last changed. This route reads that feed for the app (at most once in 6 hours, keeping
the last good copy on disk) and adds this computer's own install check: an entry Hermes would refuse here (a
platform it doesn't list, a ``requires_hermes`` this Hermes fails) carries Hermes' own refusal, the words an
install by name answers. The app then turns Install off instead of comparing versions itself.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

CAPABILITY = "native-marketplace-v1"
FEED_URL = "https://nousresearch.github.io/hermes-agent/docs/api/plugins.json"
FEED_TTL_SECONDS = 6 * 60 * 60
FEED_RETRY_SECONDS = 60.0
FEED_TIMEOUT_SECONDS = 8.0
# The feed is about 2 MB for 1,000 plugins.
MAX_FEED_BYTES = 16 * 1024 * 1024
MAX_PLUGINS = 5_000
MAX_REFUSAL_BYTES = 512
# Refusals change only when Hermes or its catalog does; a few minutes keeps the catalog parse off each read.
REFUSALS_TTL_SECONDS = 10 * 60
_NAME = re.compile(r"[a-z0-9_-]{1,64}\Z")
logger = logging.getLogger("hermes.plugins.bighelp")

_lock = threading.Lock()
_feed: tuple[float, dict[str, dict]] | None = None
_feed_failed_at = 0.0
_refusals: tuple[float, dict[str, str]] | None = None


class _HTTPSRedirectsOnly(HTTPRedirectHandler):
    """The docs site moves its files with redirects; never to a plain-http address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = build_opener(_HTTPSRedirectsOnly())


def available() -> bool:
    try:
        from hermes_cli.plugin_catalog import load_catalog_live  # noqa: F401
    except Exception:
        return False
    return True


def forget() -> None:
    """Drops what this process remembers (tests)."""
    global _feed, _feed_failed_at, _refusals
    with _lock:
        _feed, _feed_failed_at, _refusals = None, 0.0, None


def plugins() -> dict:
    """The app's answer: one row per plugin the feed or the install check knows, by catalog name."""
    fetched_at, facts = _feed_facts()
    refusals = _install_refusals()
    rows = []
    for name in sorted(set(facts) | set(refusals))[:MAX_PLUGINS]:
        row = {"name": name, **facts.get(name, {})}
        if name in refusals:
            row["installRefusal"] = refusals[name]
        rows.append(row)
    return {"fetchedAt": _iso(fetched_at) if fetched_at else None, "plugins": rows}


def _feed_facts() -> tuple[float | None, dict[str, dict]]:
    global _feed, _feed_failed_at
    with _lock:
        now = time.time()
        if _feed is None:
            _feed = _read_saved()
        if _feed is not None and now - _feed[0] < FEED_TTL_SECONDS:
            return _feed
        if now - _feed_failed_at < FEED_RETRY_SECONDS:
            return _feed or (None, {})
        try:
            facts = _fetch()
        except Exception as error:
            # Offline or a changed feed: the last good copy stays, whatever its age.
            logger.info("bighelp marketplace feed unavailable: %s", type(error).__name__)
            _feed_failed_at = now
            return _feed or (None, {})
        _feed = (now, facts)
        _save(_feed)
        return _feed


def _fetch() -> dict[str, dict]:
    request = Request(FEED_URL, headers={"Accept": "application/json", "User-Agent": "bighelp-plugin"})
    with _opener.open(request, timeout=FEED_TIMEOUT_SECONDS) as response:
        body = response.read(MAX_FEED_BYTES + 1)
    if len(body) > MAX_FEED_BYTES:
        raise ValueError("feed too large")
    return facts_from_feed(json.loads(body.decode("utf-8")))


def facts_from_feed(value: Any) -> dict[str, dict]:
    """Each plugin's stars, featured rank and dates from ``plugins.json``. A bad field leaves out only
    itself, a bad row only that row."""
    if not isinstance(value, list):
        raise ValueError("feed is not a list")
    facts: dict[str, dict] = {}
    for raw in value[:MAX_PLUGINS]:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME.match(name) or name in facts:
            continue
        row: dict[str, Any] = {}
        stars = _whole(raw.get("stars"), 0, 1_000_000_000)
        if stars is not None:
            row["stars"] = stars
        featured = _whole(raw.get("featured"), 1, 1_000)
        if featured is not None:
            row["featured"] = featured
        for key in ("addedAt", "updatedAt"):
            date = _date(raw.get(key))
            if date:
                row[key] = date
        facts[name] = row
    return facts


def _install_refusals() -> dict[str, str]:
    """Hermes' own words for each catalog entry an install here would refuse."""
    global _refusals
    with _lock:
        if _refusals is not None and time.time() - _refusals[0] < REFUSALS_TTL_SECONDS:
            return _refusals[1]
    try:
        from hermes_cli.plugin_catalog import load_catalog_live
        from hermes_cli.plugins_cmd import PluginOperationError
        from hermes_cli.plugins_cmd_catalog import refuse_incompatible_catalog_entry
        entries = load_catalog_live()
    except Exception:
        # A Hermes without this install check installs every entry, so it refuses none.
        return {}
    refusals: dict[str, str] = {}
    for entry in entries[:MAX_PLUGINS]:
        try:
            refuse_incompatible_catalog_entry(entry)
        except PluginOperationError as error:
            text = _text(str(error))
            if text and _NAME.match(entry.name):
                refusals[entry.name] = text
        except Exception:
            continue
    with _lock:
        _refusals = (time.time(), refusals)
    return refusals


def _whole(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        return None
    return value


def _date(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return _iso(parsed.timestamp())


def _iso(timestamp: float) -> str:
    return datetime.datetime.fromtimestamp(int(timestamp), datetime.UTC).isoformat().replace("+00:00", "Z")


def _text(value: str) -> str | None:
    words = " ".join("".join(" " if ord(character) < 32 or ord(character) == 127 else character
                             for character in value).split())
    if not words:
        return None
    encoded = words.encode("utf-8")
    if len(encoded) <= MAX_REFUSAL_BYTES:
        return words
    return encoded[:MAX_REFUSAL_BYTES - 3].decode("utf-8", "ignore") + "…"


def _saved_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "plugin-data" / "loopdy" / "marketplace-plugins.json"


def _read_saved() -> tuple[float, dict[str, dict]] | None:
    try:
        saved = json.loads(_saved_path().read_text(encoding="utf-8"))
        fetched_at = float(saved["fetchedAt"])
        # The copy holds rows this module wrote; they pass the same checks again.
        facts = facts_from_feed([{"name": name, **row} for name, row in saved["plugins"].items()])
    except Exception:
        return None
    return fetched_at, facts


def _save(feed: tuple[float, dict[str, dict]]) -> None:
    temporary: Path | None = None
    try:
        path = _saved_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False,
                                         prefix=".marketplace-", suffix=".json") as file:
            temporary = Path(file.name)
            json.dump({"fetchedAt": feed[0], "plugins": feed[1]}, file, separators=(",", ":"))
        os.replace(temporary, path)
    except Exception:
        logger.info("bighelp marketplace feed couldn't be saved")
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink(missing_ok=True)
