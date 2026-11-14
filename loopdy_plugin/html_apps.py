"""HTML apps: interactive pages an agent shows in a bighelp chat (docs/HTML_APPS.md).

An agent writes one self-contained HTML page and calls ``bighelp_show_app``. The plugin keeps the page
here, per agent, and returns a card (title, description, Open App). The app on the phone fetches the page
through the ``html-apps`` routes and shows it full screen; it fetches the app's data the same way, again
and again while the app is open. The plugin stores and serves the page. It never reads, rewrites or
polices what the page loads.

An app can also send the person's answer back, but only the phone does that, after asking the person; the
answer is an ordinary chat message. Nothing here sends one.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import card_objects, tool_session
from .image_drafts import _BRIDGE
from .loopdy_cards import APP_ID, APP_TYPES, MAX_APP_DESCRIPTION, BighelpCardError, render_card
from .naming import TOOLSET

logger = logging.getLogger("hermes.plugins.bighelp")

CAPABILITY = "native-html-apps-v1"
SESSION_SOURCE = "bighelp"
MAX_APPS = 200
MAX_TITLE = 60
MAX_HTML_BYTES = 16 * 1024 * 1024
MAX_DATA_BYTES = 1024 * 1024
# A page goes to the app in pieces, so any page fits the app's 4 MiB cap on one answer: 2 MiB is
# about 2.7 MiB as base64.
MAX_PIECE_BYTES = 2 * 1024 * 1024
MAX_GET_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_DATA_RESPONSE_BYTES = 2 * 1024 * 1024 + 65_536
HTML_SUFFIXES = (".html", ".htm")

SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")

UPDATE_NOTE = "This chat's Bighelp app can't show apps. Ask the person to update Bighelp."
PROBLEMS = ("missing", "invalid", "too_large")


class AppError(Exception):
    """An expected refusal, with the status and code the routes answer."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# MARK: The store


def _timestamp(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


@dataclass(frozen=True)
class HtmlApp:
    app_id: str
    session_id: str
    title: str
    description: str
    sends_answers: bool
    # "none", "inline" (``inline_json``, set by the agent) or "path" (a JSON file in the agent's folder)
    data_kind: str
    data_path: str | None
    inline_json: str | None
    data_updated: float | None
    revision: int
    created: float
    updated: float
    html_sha256: str
    html_bytes: int
    # The space (spaces.py) whose page this is: kept past MAX_APPS, and changed only through the space.
    space_id: str | None = None

    @property
    def live(self) -> bool:
        return self.data_kind != "none"


class HtmlAppStore:
    """One agent's apps: a row each, and the page in a file of its own named by its SHA-256."""

    def __init__(self, directory: Path, *, now: Callable[[], float] = time.time):
        self.path = directory / "html-apps.sqlite3"
        self.pages = directory / "html-apps"
        self._now = now
        self._lock = threading.RLock()
        self.pages.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            # Pages can hold what the agent knows about the person: only the host's user reads them.
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(descriptor)
        with self._db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS apps(
                    app_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, title TEXT NOT NULL,
                    description TEXT NOT NULL, sends_answers INTEGER NOT NULL, data_kind TEXT NOT NULL,
                    data_path TEXT, inline_json TEXT, data_updated REAL, revision INTEGER NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, html_sha256 TEXT NOT NULL,
                    html_bytes INTEGER NOT NULL, space_id TEXT)""")
        self._add_space_column()

    def _add_space_column(self) -> None:
        """A store made before spaces gets their column. Another plugin copy may add it at the same time."""
        with self._db() as db:
            if "space_id" in {row["name"] for row in db.execute("PRAGMA table_info(apps)")}:
                return
        try:
            with self._db(write=True) as db:
                if "space_id" not in {row["name"] for row in db.execute("PRAGMA table_info(apps)")}:
                    db.execute("ALTER TABLE apps ADD COLUMN space_id TEXT")
        except sqlite3.OperationalError:
            with self._db() as db:
                if "space_id" not in {row["name"] for row in db.execute("PRAGMA table_info(apps)")}:
                    raise

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            db.row_factory = sqlite3.Row
            try:
                if write:
                    # The dashboard's routes and the agent's tools are two plugin copies sharing this file.
                    db.execute("BEGIN IMMEDIATE")
                    try:
                        yield db
                    except BaseException:
                        db.execute("ROLLBACK")
                        raise
                    db.execute("COMMIT")
                else:
                    yield db
            finally:
                db.close()

    @staticmethod
    def _app(row: sqlite3.Row) -> HtmlApp:
        return HtmlApp(
            app_id=row["app_id"], session_id=row["session_id"], title=row["title"],
            description=row["description"], sends_answers=bool(row["sends_answers"]), data_kind=row["data_kind"],
            data_path=row["data_path"], inline_json=row["inline_json"], data_updated=row["data_updated"],
            revision=row["revision"], created=row["created"], updated=row["updated"],
            html_sha256=row["html_sha256"], html_bytes=row["html_bytes"], space_id=row["space_id"])

    def _page(self, app_id: str, sha256: str) -> Path:
        return self.pages / f"{app_id}.{sha256}.html"

    def _write_page(self, app_id: str, sha256: str, html: bytes) -> None:
        target = self._page(app_id, sha256)
        if target.exists():
            return
        scratch = self.pages / f".{secrets.token_hex(8)}.tmp"
        descriptor = os.open(scratch, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(html)
                file.flush()
                os.fsync(file.fileno())
            os.replace(scratch, target)
        finally:
            scratch.unlink(missing_ok=True)

    def _drop_pages(self, app_id: str, *, keep: str | None = None) -> None:
        for page in self.pages.glob(f"{app_id}.*.html"):
            if keep is None or page.name != f"{app_id}.{keep}.html":
                page.unlink(missing_ok=True)

    def get(self, app_id: str) -> HtmlApp | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
        return None if row is None else self._app(row)

    def save(self, app_id: str, *, replace: bool, session_id: str, title: str, description: str,
             sends_answers: bool, html: bytes, data_json: str | None = None,
             data_path: str | None = None, space_id: str | None = None) -> HtmlApp:
        """Keep a new app, or with ``replace`` swap an existing one's page, texts and data source and bump
        its revision. Its id, the session it was made in and the space it belongs to stay."""
        sha256 = hashlib.sha256(html).hexdigest()
        kind = "path" if data_path is not None else "inline" if data_json is not None else "none"
        new_id = app_id
        self._write_page(new_id, sha256, html)  # before the row, so a row never names a missing page
        now = self._now()
        try:
            with self._db(write=True) as db:
                old = db.execute("SELECT * FROM apps WHERE app_id = ?", (new_id,)).fetchone()
                if replace and old is None:
                    raise AppError(404, "app_not_found", "That app isn't one of yours.")
                values = (session_id if old is None else old["session_id"], title, description, int(sends_answers),
                          kind, data_path, data_json, now if kind == "inline" else None,
                          1 if old is None else old["revision"] + 1,
                          now if old is None else old["created"], now, sha256, len(html),
                          space_id if space_id is not None or old is None else old["space_id"])
                db.execute(
                    "INSERT OR REPLACE INTO apps(session_id, title, description, sends_answers, data_kind, data_path, "
                    "inline_json, data_updated, revision, created, updated, html_sha256, html_bytes, space_id, "
                    "app_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*values, new_id))
                evicted = self._prune(db, keep=new_id)
        except BaseException:
            # Drop the page just written, keeping the one the app still holds.
            current = self.get(new_id)
            self._drop_pages(new_id, keep=current.html_sha256 if current is not None else None)
            raise
        self._drop_pages(new_id, keep=sha256)
        for gone in evicted:
            self._drop_pages(gone)
        saved = self.get(new_id)
        assert saved is not None
        return saved

    @staticmethod
    def _prune(db: sqlite3.Connection, *, keep: str) -> list[str]:
        """Past MAX_APPS, the least recently updated go. A space's page stays until its space goes
        (spaces.py keeps their number small)."""
        extra = db.execute("SELECT COUNT(*) FROM apps WHERE space_id IS NULL").fetchone()[0] - MAX_APPS
        if extra <= 0:
            return []
        gone = [row["app_id"] for row in db.execute(
            "SELECT app_id FROM apps WHERE app_id != ? AND space_id IS NULL ORDER BY updated, app_id LIMIT ?",
            (keep, extra))]
        db.executemany("DELETE FROM apps WHERE app_id = ?", [(app_id,) for app_id in gone])
        return gone

    def delete(self, app_id: str) -> bool:
        """Remove an app and its pages. Whether it was there."""
        with self._db(write=True) as db:
            existed = db.execute("DELETE FROM apps WHERE app_id = ?", (app_id,)).rowcount > 0
        self._drop_pages(app_id)
        return existed

    def set_inline_data(self, app_id: str, data_json: str | None) -> tuple[HtmlApp, float]:
        """Replace an app's inline data (``None`` clears it). An app that reads a file refuses."""
        now = self._now()
        with self._db(write=True) as db:
            row = db.execute("SELECT * FROM apps WHERE app_id = ?", (app_id,)).fetchone()
            if row is None:
                raise AppError(404, "app_not_found", "That app isn't one of yours.")
            if row["data_kind"] == "path":
                raise AppError(409, "data_from_file", f"This app reads its data from {row['data_path']}. "
                               "Write that file instead.")
            db.execute("UPDATE apps SET data_kind = ?, inline_json = ?, data_updated = ?, updated = ? "
                       "WHERE app_id = ?",
                       ("none" if data_json is None else "inline", data_json,
                        None if data_json is None else now, now, app_id))
        app = self.get(app_id)
        assert app is not None
        return app, now

    def read_html(self, app_id: str) -> tuple[HtmlApp, bytes]:
        """The app and its page, as one revision even when a replacement lands between the two reads."""
        for _attempt in range(3):
            app = self.get(app_id)
            if app is None:
                break
            try:
                html = self._page(app_id, app.html_sha256).read_bytes()
            except FileNotFoundError:
                continue  # replaced just now: the row has moved on
            if hashlib.sha256(html).hexdigest() == app.html_sha256:
                return app, html
            break
        raise AppError(404, "app_not_found", "That app isn't available.")


_stores: dict[str, HtmlAppStore] = {}
_stores_lock = threading.Lock()


def store_for_home(home: Path) -> HtmlAppStore:
    directory = (home / "plugin-data" / "loopdy").resolve()
    with _stores_lock:
        store = _stores.get(str(directory))
        if store is None:
            store = HtmlAppStore(directory)
            _stores[str(directory)] = store
        return store


def current_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def current_store() -> HtmlAppStore:
    """The store of the agent whose turn is running (context-local home)."""
    return store_for_home(current_home())


def profile_home(profile: str) -> Path:
    from .image_drafts import DraftError, profile_home as home_of
    try:
        return home_of(profile)
    except DraftError as error:
        raise AppError(error.status, error.code, error.message) from None


def available() -> bool:
    from .image_drafts import available as drafts_available
    return drafts_available()


# MARK: Files and data


class _Unreadable(Exception):
    """A file the agent named that can't be used. ``problem`` is one of PROBLEMS; ``code`` says why."""

    def __init__(self, problem: str, code: str, message: str):
        super().__init__(message)
        self.problem, self.code, self.message = problem, code, message


def read_workspace_file(profile: str, raw_path: str, limit: int) -> tuple[bytes, float]:
    """A file in the agent's own working folder (nothing else), its bytes and modified time."""
    try:
        from .native_context import NativeAPIError
        from .workspace_artifacts import read_confined
    except ImportError:
        raise _Unreadable("missing", "files_unavailable",
                          "Files in your working folder aren't available on this host.") from None
    try:
        return read_confined(profile, raw_path, limit)
    except NativeAPIError as error:
        raise _Unreadable("too_large" if error.code == "file_too_large" else "missing", error.code,
                          error.message) from None
    except OSError:
        raise _Unreadable("missing", "workspace_unreadable", "That file couldn't be read.") from None


def _parse_data(raw: bytes) -> Any:
    """The file's JSON, or ``_Unreadable("invalid")``: UTF-8, no NaN or Infinity, nothing too deep to send."""
    def refuse(_value: str) -> Any:
        raise ValueError("not plain JSON")

    def number(text: str) -> float:
        value = float(text)
        if not math.isfinite(value):
            raise ValueError("not plain JSON")
        return value

    try:
        value = json.loads(raw.decode("utf-8"), parse_constant=refuse, parse_float=number)
        _compact(value)
    except (ValueError, RecursionError):
        raise _Unreadable("invalid", "invalid_json", "That file isn't JSON.") from None
    return value


def data_response(app: HtmlApp, profile: str) -> dict[str, Any]:
    """What the app's data route answers: the data (or why there is none) and what changed it last."""
    answer: dict[str, Any] = {"appId": app.app_id, "data": None, "updatedAt": None, "dataSha256": None,
                              "problem": None}
    if app.data_kind == "inline" and app.inline_json is not None:
        answer.update(data=json.loads(app.inline_json),
                      updatedAt=_timestamp(app.data_updated) if app.data_updated is not None else None,
                      dataSha256=hashlib.sha256(app.inline_json.encode("utf-8")).hexdigest())
    elif app.data_kind == "path" and app.data_path is not None:
        try:
            raw, modified = read_workspace_file(profile, app.data_path, MAX_DATA_BYTES)
            answer.update(data=_parse_data(raw), updatedAt=_timestamp(modified),
                          dataSha256=hashlib.sha256(raw).hexdigest())
        except _Unreadable as error:
            answer["problem"] = error.problem
    return answer


def get_response(app: HtmlApp, html: bytes, known_sha256: str | None, *, offset: int = 0,
                 revision: str | None = None) -> dict[str, Any]:
    """The app and one piece of its page, from ``offset``; ``nextOffset`` is where the next piece starts, or None.

    The page is left out when the phone already has this revision (``known_sha256``). A later piece names the
    revision it continues (``revision``): when the page changed meanwhile, the phone starts again."""
    if revision is not None and revision != app.html_sha256:
        raise AppError(409, "app_changed", "The app changed while it was loading. Load it again.")
    answer: dict[str, Any] = {"app": {
        "appId": app.app_id, "title": app.title, "description": app.description,
        "sendsAnswers": app.sends_answers, "live": app.live, "revision": app.revision,
        "updatedAt": _timestamp(app.updated), "htmlSha256": app.html_sha256, "htmlBytes": app.html_bytes}}
    if offset == 0 and known_sha256 == app.html_sha256:
        return answer
    if offset >= len(html):
        raise AppError(400, "invalid_request", "offset is past the end of the page.")
    piece = html[offset:offset + MAX_PIECE_BYTES]
    end = offset + len(piece)
    answer.update(offset=offset, html=base64.b64encode(piece).decode("ascii"),
                  nextOffset=end if end < len(html) else None)
    return answer


# MARK: Tools


class _ToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _session(context: dict[str, Any]) -> str:
    session_id = tool_session.session_id(context, "HERMES_SESSION_ID")
    if SESSION_ID.fullmatch(session_id or "") is None:
        raise _ToolError("apps_unavailable", "Apps work only in a Bighelp chat with the person.")
    return session_id


def _declared(session_id: str) -> frozenset[str]:
    try:
        return card_objects.declared_for(session_id) & APP_TYPES
    except Exception as error:  # a broken store refuses, never shows a card the app can't draw
        logger.debug("Bighelp app renders lookup failed: %s", type(error).__name__)
        return frozenset()


def _line(value: Any, key: str, maximum: int) -> str:
    """A title or description: one line, whitespace collapsed, 1 to ``maximum`` characters."""
    if not isinstance(value, str):
        raise _ToolError("invalid_arguments", f"{key} is a string.")
    text = " ".join(value.split())
    if not text or len(text) > maximum:
        raise _ToolError("invalid_arguments", f"{key} is 1 to {maximum} characters.")
    if any(unicodedata.category(scalar) in ("Cc", "Cs") for scalar in text):
        raise _ToolError("invalid_arguments", f"{key} must be plain text.")
    return text


def _utf8(value: str, what: str) -> bytes:
    try:
        return value.encode("utf-8")
    except UnicodeError:
        raise _ToolError("invalid_arguments", f"{what} must be valid text.") from None


def _page(args: dict[str, Any], profile: str) -> bytes:
    html, path = args.get("html"), args.get("path")
    if (html is None) == (path is None):
        raise _ToolError("invalid_arguments", "Give exactly one of html (the page itself) or path (an .html "
                         "file you wrote in your working folder).")
    if html is not None:
        if not isinstance(html, str):
            raise _ToolError("invalid_arguments", "html is a string.")
        page = _utf8(html, "html")
    else:
        if not isinstance(path, str) or not path.lower().endswith(HTML_SUFFIXES):
            raise _ToolError("invalid_arguments", "path is an .html or .htm file in your working folder.")
        try:
            page, _modified = read_workspace_file(profile, path, MAX_HTML_BYTES)
        except _Unreadable as error:
            if error.problem == "too_large":
                raise _ToolError("html_too_large", f"The page is over {MAX_HTML_BYTES:,} bytes.") from None
            raise _ToolError("invalid_path", error.message) from None
        try:
            page.decode("utf-8")
        except UnicodeError:
            raise _ToolError("invalid_arguments", "The page must be UTF-8 text.") from None
    if len(page) > MAX_HTML_BYTES:
        raise _ToolError("html_too_large", f"The page is over {MAX_HTML_BYTES:,} bytes. Make it smaller; "
                         "load big libraries and pictures from the web instead of inlining them.")
    if not page.strip():
        raise _ToolError("invalid_arguments", "The page is empty.")
    return page


def _inline_data(value: Any) -> str:
    try:
        encoded = _compact(value)
    except (TypeError, ValueError, RecursionError):
        raise _ToolError("invalid_arguments", "data must be plain JSON (no NaN or Infinity, not too deeply "
                         "nested).") from None
    if len(encoded.encode("utf-8")) > MAX_DATA_BYTES:
        raise _ToolError("data_too_large", f"data is over {MAX_DATA_BYTES:,} bytes as compact JSON. Keep "
                         "inline data small, or write a file and pass data_path.")
    return encoded


def _data_path(path: Any, profile: str) -> tuple[str, str | None]:
    """The path to keep, and why its file can't be used right now (``None`` when it can)."""
    if not isinstance(path, str) or not path.strip():
        raise _ToolError("invalid_arguments", "data_path is a JSON file in your working folder.")
    path = path.strip()
    try:
        raw, _modified = read_workspace_file(profile, path, MAX_DATA_BYTES)
    except _Unreadable as error:
        # Not there yet is fine (a script may write it later); outside the working folder is not.
        if error.code not in ("file_not_found", "file_too_large"):
            raise _ToolError("invalid_path", error.message) from None
        return path, "is missing" if error.code == "file_not_found" else "is too big"
    try:
        _parse_data(raw)
    except _Unreadable:
        return path, "isn't JSON"
    return path, None


def build_card(*, app_id: str, title: str, description: str, sends_answers: bool, live: bool) -> dict[str, Any]:
    return {
        "schema": "loopdy.card",
        "version": 1,
        "title": title,
        "spoken_summary": f"An app to open in Bighelp: {title}.",
        "data_sources": [],
        "root": "app",
        "elements": {"app": {"type": "html_app", "children": [], "props": {
            "app_id": app_id, "description": description, "sends_answers": sends_answers, "live": live}}},
    }


def _show_note(sends_answers: bool, data_problem: str | None) -> str:
    note = ("The person opens the app from the card (Open App). Put the returned display_markdown in your "
            "reply once; don't say you opened it for them.")
    if sends_answers:
        note += " When the person sends an answer from the app, it arrives as their next message in this chat."
    if data_problem:
        note += f" Its data file {data_problem} right now, so the app shows no data until that's fixed."
    return note


def show(args: Any, context: dict[str, Any], *, now: Callable[[], datetime], profile: str) -> dict[str, Any]:
    allowed = {"title", "description", "html", "path", "data", "data_path", "sends_answers", "app_id"}
    if not isinstance(args, dict) or set(args) - allowed or "title" not in args or "description" not in args:
        raise _ToolError("invalid_arguments", "Arguments are title and description, exactly one of html or "
                         "path, and optionally data or data_path, sends_answers and app_id.")
    session_id = _session(context)
    if not _declared(session_id):
        raise _ToolError("apps_unavailable", UPDATE_NOTE)
    app_id = args.get("app_id")
    if app_id is not None and (not isinstance(app_id, str) or APP_ID.fullmatch(app_id) is None):
        raise _ToolError("invalid_arguments", "app_id is the app_id of an earlier app of yours.")
    title = _line(args["title"], "title", MAX_TITLE)
    description = _line(args["description"], "description", MAX_APP_DESCRIPTION)
    sends_answers = args.get("sends_answers")
    if sends_answers is None:
        sends_answers = False
    if not isinstance(sends_answers, bool):
        raise _ToolError("invalid_arguments", "sends_answers is true or false.")
    data = args.get("data")
    data_path = args.get("data_path")
    if data is not None and data_path is not None:
        raise _ToolError("invalid_arguments", "Give at most one of data or data_path.")
    store = current_store()
    if app_id is not None:
        existing = store.get(app_id)
        if existing is None:
            raise _ToolError("app_not_found", "That app_id isn't one of your apps. Leave app_id out to make a new one.")
        if existing.space_id is not None:
            raise _ToolError("app_in_space", "Change a space's page with bighelp_draft_space.")
    page = _page(args, profile)
    data_json = _inline_data(data) if data is not None else None
    data_problem = None
    if data_path is not None:
        data_path, data_problem = _data_path(data_path, profile)
    new_id = app_id or "a_" + secrets.token_hex(16)
    document = build_card(app_id=new_id, title=title, description=description, sends_answers=sends_answers,
                          live=data_json is not None or data_path is not None)
    from .generative_ui import rendered_card_delivery
    try:
        card = render_card(document, now=now(), allow_apps=True)
    except BighelpCardError as error:
        raise _ToolError(error.code, str(error)) from None
    try:
        saved = store.save(new_id, replace=app_id is not None, session_id=session_id, title=title,
                           description=description, sends_answers=sends_answers, html=page, data_json=data_json,
                           data_path=data_path)
    except AppError as error:
        raise _ToolError(error.code, error.message) from None
    return {**rendered_card_delivery(card), "app_id": saved.app_id, "revision": saved.revision,
            "note": _show_note(sends_answers, data_problem)}


def update_data(args: Any, context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict) or set(args) != {"app_id", "data"}:
        raise _ToolError("invalid_arguments", "Arguments are app_id and data (null clears it).")
    app_id = args["app_id"]
    if not isinstance(app_id, str) or APP_ID.fullmatch(app_id) is None:
        raise _ToolError("invalid_arguments", "app_id is the app_id of an app of yours.")
    data_json = None if args["data"] is None else _inline_data(args["data"])
    try:
        _app, updated = current_store().set_inline_data(app_id, data_json)
    except AppError as error:
        message = ("That app_id isn't one of your apps." if error.code == "app_not_found" else error.message)
        raise _ToolError(error.code, message) from None
    return {"app_id": app_id, "updated_at": _timestamp(updated),
            "note": "The open app gets the new data within a few seconds. Don't post the card again."}


SHOW_DESCRIPTION = (
    "Show an interactive HTML app in this Bighelp chat, for something people use rather than read: a small "
    "tool, a calculator or planner, a picker, a live dashboard. The person gets a card with an Open App button "
    "and uses the app full screen on their phone. Use it only when interactive fits; a plain card "
    "(bighelp_render_card) or a form is better for a simple answer. "
    "html is one self-contained page (or path: an .html file you wrote in your working folder); it may load any "
    "scripts, styles, fonts or APIs it needs. Follow prefers-color-scheme (light and dark) and fit a phone "
    "screen (a viewport meta tag, touch-size controls). Bighelp adds this page API: "
    "`await window.bighelp.getData()` returns the latest data or null; "
    "`window.bighelp.onData(fn)` calls fn(data) once at start when there is data and again whenever it changes "
    "while the app is open, and returns an unsubscribe function; "
    "`await window.bighelp.sendAnswer(answer)` (only when sends_answers is true) takes a string or a flat "
    "object of name to text, number, boolean or list of text, shows the person the exact message and asks "
    "before sending, and resolves true when sent or false when they cancel; "
    "`window.bighelp.close()` closes the app. "
    "data (any JSON, at most 1,048,576 bytes) or data_path (a JSON file in your working folder, read fresh every "
    "time, so a script or scheduled task can update it) gives the app live data; give at most one. Change "
    "inline data later with bighelp_update_app_data. "
    "sends_answers (default false) lets the app send the person's answer back. It arrives as their next "
    "message: for an object, `My answers to “<title>”:` then one `- <name>: <value>` line each (lists joined "
    "with \", \", booleans as Yes/No); for a string, `My answer from “<title>”:` then the text. Treat it as "
    "what the person said. "
    "app_id: an app of yours to replace. Its html, title, description, sends_answers and data source are "
    "replaced (give data or data_path again to keep data) and it keeps its id. "
    "The page is at most 16,777,216 bytes (16 MiB). Put the returned display_markdown in your reply once."
    + _BRIDGE
)
UPDATE_DATA_DESCRIPTION = (
    "Change the data of an app you made with bighelp_show_app. An open app gets the new data within a few "
    "seconds, so don't post its card again. data is any JSON (at most 1,048,576 bytes); null clears it. An app "
    "that reads data_path takes its data from that file instead: write the file."
    + _BRIDGE
)

_DATA_TYPES = ["object", "array", "string", "number", "boolean", "null"]


def show_parameters() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["title", "description"], "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": MAX_TITLE},
        "description": {"type": "string", "minLength": 1, "maxLength": MAX_APP_DESCRIPTION,
                        "description": "What the app does, in one line, shown on the card."},
        "html": {"type": "string", "description": "The whole page. Give this or path."},
        "path": {"type": "string", "description": "An .html or .htm file in your working folder. Give this or html."},
        "data": {"type": _DATA_TYPES, "description": "Inline data for the app. Give this or data_path."},
        "data_path": {"type": "string", "description": "A JSON file in your working folder, read fresh each time."},
        "sends_answers": {"type": "boolean", "description": "Let the app send the person's answer back."},
        "app_id": {"type": "string", "pattern": "^a_[0-9a-f]{32}$",
                   "description": "An app of yours to replace."}}}


def update_data_parameters() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["app_id", "data"], "properties": {
        "app_id": {"type": "string", "pattern": "^a_[0-9a-f]{32}$"},
        "data": {"type": _DATA_TYPES, "description": "Any JSON; null clears it."}}}


def _handler(work: Callable[..., dict[str, Any]], **options: Any) -> Callable[..., str]:
    def handle(args: Any = None, **context: Any) -> str:
        try:
            return _json(work(args, context, **options))
        except _ToolError as error:
            return _json({"success": False, "error": error.code, "message": error.message})
        except (OSError, sqlite3.Error) as error:
            logger.warning("Bighelp html app failed: %s", type(error).__name__)
            return _json({"success": False, "error": "apps_unavailable",
                          "message": "Apps aren't available on this computer right now."})
    return handle


def register(ctx: Any) -> None:
    clock = lambda: datetime.now(timezone.utc)  # noqa: E731
    profile = str(getattr(ctx, "profile_name", "default") or "default")
    tools = (
        ("bighelp_show_app", SHOW_DESCRIPTION, show_parameters(),
         _handler(show, now=clock, profile=profile), "🧩"),
        ("bighelp_update_app_data", UPDATE_DATA_DESCRIPTION, update_data_parameters(),
         _handler(update_data), "🔄"),
    )
    for name, description, parameters, handler, emoji in tools:
        ctx.register_tool(name=name, toolset=TOOLSET,
                          schema={"name": name, "description": description, "parameters": parameters},
                          handler=handler, emoji=emoji)
