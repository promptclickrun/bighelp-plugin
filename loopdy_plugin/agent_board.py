"""An agent's board in the bighelp app: Feed posts, Ideas and Goals, plus the
Activity and Approvals history shown on its profile.

Nothing here calls a model or schedules work. Agents write to the board with the
``bighelp_board`` tool only when a user has asked for that kind of update (the
bundled ``bighelp-feed-and-ideas`` skill explains how). Activity and approval
rows are recorded from lifecycle hooks the agent already fires.

Each profile keeps its own database under its Hermes home, so a turn that runs
in the gateway and a read served by the dashboard see the same rows.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .naming import TOOLSET

logger = logging.getLogger(__name__)

CAPABILITY = "native-agent-board-v1"
# Thumbs up/down with a reason, read state, bulk read and idea → goal.
FEEDBACK_CAPABILITY = "native-agent-board-feedback-v1"
# Goals carry one of GOAL_CATEGORIES; the app groups them and starts new ones by category.
GOAL_CATEGORIES_CAPABILITY = "native-agent-board-goal-categories-v1"
# Feed posts carry host files by reference; the app fetches them through the
# attachment routes (``attachments/board``), never by path.
FILES_CAPABILITY = "native-agent-board-files-v1"
TOOL_NAME = "bighelp_board"
KINDS = ("feed", "idea", "goal")
GOAL_SECTIONS = ("tracking", "goal")
GOAL_STATUSES = ("active", "done")
# The app's fixed list, in its order. "other" is the app's "Something else".
GOAL_CATEGORIES = ("health", "relationships", "finance", "career", "interests", "productivity", "other")
MAX_TITLE = 200
MAX_BODY = 4_000
MAX_NOTE = 600
MAX_ICON = 16
MAX_SECTION = 60
MAX_IMAGES = 6
MAX_LINKS = 8
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_FILES = 10
# The same cap as a file an agent sends in chat (``MEDIA:``).
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_ITEMS_PER_KIND = 500
MAX_ACTIVITY = 1_000
MAX_REASON = 120
MAX_READ_BATCH = 200
RATINGS = {"down": -1, "none": 0, "up": 1}
MAX_APPROVALS = 1_000
# How long the agent remembers your answer to an idea. A "not now" blocks the
# same offer for this long, then the idea may come back.
ANSWER_MEMORY_SECONDS = 30 * 24 * 3600
MAX_ANSWERED = 30
# What the app sends when someone taps Let's do it on an idea.
_LETS_DO_IT = re.compile(r"Yes, go ahead with this idea: \u201c(.{1,200})\u201d\.\s*\Z", re.S)
_ITEM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_IMAGE_TYPES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)


class BoardError(ValueError):
    """A request the board refuses; the message is safe to show the agent."""


def _clean(value: Any, limit: int, *, field: str, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise BoardError(f"{field} must be text.")
    text = value.strip()
    if required and not text:
        raise BoardError(f"{field} is required.")
    if len(text) > limit:
        raise BoardError(f"{field} is longer than {limit} characters.")
    return text


def _category(value: Any) -> str:
    """A goal's category, or "" for none. Anything outside the fixed list is refused."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BoardError("category must be text.")
    category = value.strip().lower()
    if category and category not in GOAL_CATEGORIES:
        raise BoardError("category must be one of " + ", ".join(GOAL_CATEGORIES) + ".")
    return category


def _image_type(head: bytes) -> tuple[str, str] | None:
    for magic, mime, extension in _IMAGE_TYPES:
        if head.startswith(magic):
            return mime, extension
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1"):
        return "image/heic", "heic"
    return None


class BoardStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.media = directory / "board-media"
        self.path = directory / "board.sqlite3"
        self._lock = threading.RLock()
        directory.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS items(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT '',
                    section TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '', links TEXT NOT NULL DEFAULT '[]',
                    images TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL DEFAULT '',
                    liked INTEGER NOT NULL DEFAULT 0, dismissed INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS items_kind ON items(kind, created DESC);
                CREATE TABLE IF NOT EXISTS activity(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL DEFAULT '', request TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '',
                    tools TEXT NOT NULL DEFAULT '[]', outcome TEXT NOT NULL DEFAULT '',
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS approvals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '', command TEXT NOT NULL DEFAULT '',
                    choice TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(items)")}
            if "rating" not in columns:
                # Likes become thumbs up. Everything already on the board counts
                # as read, so an update doesn't turn it all into "new".
                db.executescript("""
                    ALTER TABLE items ADD COLUMN rating INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE items ADD COLUMN reason TEXT NOT NULL DEFAULT '';
                    ALTER TABLE items ADD COLUMN read INTEGER NOT NULL DEFAULT 0;
                    UPDATE items SET rating=1 WHERE liked=1;
                    UPDATE items SET read=1;
                """)
            if "answer" not in columns:
                # Your answer to an idea: yes (Let's do it), goal (Make it a goal) or
                # not now. Ideas hidden before this have no answer; we can't know which.
                db.executescript("""
                    ALTER TABLE items ADD COLUMN answer TEXT NOT NULL DEFAULT '';
                    ALTER TABLE items ADD COLUMN answered REAL NOT NULL DEFAULT 0;
                """)
            if "category" not in columns:
                # Goals from before categories have none; the app shows them under Other.
                db.execute("ALTER TABLE items ADD COLUMN category TEXT NOT NULL DEFAULT ''")
            if "files" not in columns:
                # Host paths the post refers to, with the name, type and size shown in the app.
                db.execute("ALTER TABLE items ADD COLUMN files TEXT NOT NULL DEFAULT '[]'")

    @contextmanager
    def _db(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                db.execute("PRAGMA journal_mode=WAL")
                yield db
                db.commit()
            finally:
                db.close()

    # MARK: Items

    def publish(self, kind: str, *, title: Any, body: Any = "", icon: Any = "", section: Any = "",
                links: Any = None, images: Any = None, source: Any = "", item_id: Any = None,
                note: Any = "", status: Any = None, category: Any = None, files: Any = None,
                now: float | None = None) -> dict:
        if kind not in KINDS:
            raise BoardError("kind must be feed, idea or goal.")
        now = time.time() if now is None else now
        title = _clean(title, MAX_TITLE, field="title", required=True)
        body = _clean(body, MAX_BODY, field="body")
        icon = _clean(icon, MAX_ICON, field="icon")
        section = _clean(section, MAX_SECTION, field="section")
        note = _clean(note, MAX_NOTE, field="note")
        source = _clean(source, 200, field="source")
        if kind == "goal":
            section = section.lower() or "goal"
            if section not in GOAL_SECTIONS:
                raise BoardError("A goal's section must be tracking or goal.")
            status = (status or "active").lower()
            if status not in GOAL_STATUSES:
                raise BoardError("A goal's status must be active or done.")
            category = _category(category)
        else:
            status = ""
            category = ""
        link_rows = self._links(links)
        if files is not None and kind != "feed":
            raise BoardError("Only Feed posts carry files.")
        # None keeps an updated post's files; a list (even empty) replaces them.
        file_rows = None if files is None else [{**row, "added": now} for row in self._files(files)]
        if item_id is not None:
            item_id = _clean(item_id, 64, field="id", required=True)
            if not _ITEM_ID.fullmatch(item_id):
                raise BoardError("id may use letters, digits, dot, dash and underscore.")
        else:
            item_id = uuid.uuid4().hex
        image_rows = self._store_images(item_id, images)
        with self._db() as db:
            existing = db.execute("SELECT kind, created, answer, answered, files FROM items WHERE id=?",
                                  (item_id,)).fetchone()
            if existing and existing["kind"] != kind:
                raise BoardError("That id belongs to a different kind of item.")
            if (existing and existing["answer"] == "not now"
                    and now - existing["answered"] < ANSWER_MEMORY_SECONDS):
                raise BoardError("The user said not now to this idea recently. Don't offer it again yet; "
                                 "offer something different.")
            created = existing["created"] if existing else now
            if file_rows is None:
                file_rows = json.loads(existing["files"]) if existing else []
            db.execute("""INSERT INTO items(id,kind,title,body,icon,section,status,note,links,images,source,category,
                                            files,created,updated)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(id) DO UPDATE SET title=excluded.title, body=excluded.body,
                            icon=excluded.icon, section=excluded.section, status=excluded.status,
                            note=excluded.note, links=excluded.links,
                            images=CASE WHEN excluded.images='[]' THEN items.images ELSE excluded.images END,
                            source=excluded.source,
                            category=CASE WHEN excluded.category='' THEN items.category ELSE excluded.category END,
                            files=excluded.files,
                            answer=CASE WHEN items.dismissed=1 THEN '' ELSE items.answer END,
                            answered=CASE WHEN items.dismissed=1 THEN 0 ELSE items.answered END,
                            dismissed=0, updated=excluded.updated""",
                       (item_id, kind, title, body, icon, section, status, note,
                        json.dumps(link_rows), json.dumps(image_rows), source, category,
                        json.dumps(file_rows), created, now))
            self._prune(db, kind)
            return self._item(db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())

    def update_goal(self, item_id: Any, *, note: Any = None, status: Any = None, category: Any = None,
                    now: float | None = None) -> dict:
        item_id = _clean(item_id, 64, field="id", required=True)
        with self._db() as db:
            row = db.execute("SELECT * FROM items WHERE id=? AND kind='goal'", (item_id,)).fetchone()
            if row is None:
                raise BoardError("No goal has that id. List goals to find it.")
            next_note = row["note"] if note is None else _clean(note, MAX_NOTE, field="note")
            next_status = row["status"] if status is None else str(status).lower()
            if next_status not in GOAL_STATUSES:
                raise BoardError("A goal's status must be active or done.")
            next_category = row["category"] if category is None else _category(category)
            db.execute("UPDATE items SET note=?, status=?, category=?, updated=? WHERE id=?",
                       (next_note, next_status, next_category, time.time() if now is None else now, item_id))
            return self._item(db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())

    def set_flags(self, item_id: str, *, liked: bool | None = None, dismissed: bool | None = None,
                  status: str | None = None, rating: str | None = None, reason: str | None = None,
                  read: bool | None = None, now: float | None = None) -> dict:
        """What the person did with an item in the app. ``liked`` is the older
        app's heart; it maps onto the thumbs rating. Hiding an idea is its "not
        now" (unless they already said yes); hiding a Feed post or goal is just
        clearing it."""
        if rating is not None and rating not in RATINGS:
            raise BoardError("rating must be up, down or none.")
        with self._db() as db:
            row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise BoardError("That item no longer exists.")
            if status is not None and (row["kind"] != "goal" or status not in GOAL_STATUSES):
                raise BoardError("Only goals have a status.")
            score = row["rating"]
            if rating is not None:
                score = RATINGS[rating]
            elif liked is not None:
                score = 1 if liked else (0 if score == 1 else score)
            next_reason = row["reason"] if reason is None else _clean(reason, MAX_REASON, field="reason")
            if rating is not None and reason is None:
                next_reason = ""
            if score != -1:
                next_reason = ""
            now = time.time() if now is None else now
            answer, answered = row["answer"], row["answered"]
            if row["kind"] == "idea" and dismissed is True and not row["dismissed"] and not answer:
                answer, answered = "not now", now
            elif dismissed is False and answer == "not now":
                answer, answered = "", 0
            db.execute("""UPDATE items SET liked=?, rating=?, reason=?, read=?, dismissed=?, status=?, answer=?,
                          answered=?, updated=? WHERE id=?""", (
                int(score == 1), score, next_reason,
                int(row["read"] if read is None else read),
                int(row["dismissed"] if dismissed is None else dismissed),
                row["status"] if status is None else status, answer, answered, now, item_id))
            return self._item(db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())

    def mark_read(self, item_ids: list[str], *, read: bool = True) -> int:
        """Marks items read (or unread) together, as the app shows them."""
        if len(item_ids) > MAX_READ_BATCH:
            raise BoardError(f"Mark at most {MAX_READ_BATCH} items at once.")
        ids = [item_id for item_id in dict.fromkeys(item_ids) if _ITEM_ID.fullmatch(item_id)]
        if not ids:
            return 0
        with self._db() as db:
            return db.execute(f"UPDATE items SET read=? WHERE id IN ({','.join('?' for _ in ids)})",
                              (int(read), *ids)).rowcount

    def promote_idea(self, item_id: Any, *, now: float | None = None) -> dict:
        """An idea the person wants to pursue becomes one of their goals."""
        item_id = _clean(item_id, 64, field="id", required=True)
        with self._db() as db:
            row = db.execute("SELECT * FROM items WHERE id=? AND kind='idea'", (item_id,)).fetchone()
        if row is None:
            raise BoardError("No idea has that id.")
        # An idea filed under a category's name ("Health") keeps it as a goal.
        section = row["section"].strip().lower()
        goal = self.publish("goal", title=row["title"], body=row["body"], icon=row["icon"], section="goal",
                            source=row["source"] or "From an idea",
                            category=section if section in GOAL_CATEGORIES else None, now=now)
        self._answer(item_id, "goal", now)
        self.set_flags(item_id, dismissed=True)
        return goal

    def accept_idea(self, title: str, *, now: float | None = None) -> bool:
        """Records a Let's do it: the newest idea on the board with that title."""
        with self._db() as db:
            row = db.execute("""SELECT id FROM items WHERE kind='idea' AND dismissed=0 AND title=?
                                ORDER BY created DESC LIMIT 1""", (title.strip(),)).fetchone()
        if row is None:
            return False
        self._answer(row["id"], "yes", now)
        return True

    def _answer(self, item_id: str, answer: str, now: float | None) -> None:
        with self._db() as db:
            db.execute("UPDATE items SET answer=?, answered=? WHERE id=? AND kind='idea'",
                       (answer, time.time() if now is None else now, item_id))

    def answered_ideas(self, *, now: float | None = None, limit: int = MAX_ANSWERED) -> list[dict]:
        """Ideas the person answered that are off the board now, newest first,
        from the last ``ANSWER_MEMORY_SECONDS``. Feed and goals never appear."""
        since = (time.time() if now is None else now) - ANSWER_MEMORY_SECONDS
        with self._db() as db:
            rows = db.execute("""SELECT * FROM items WHERE kind='idea' AND dismissed=1 AND answer!=''
                                 AND answered>=? ORDER BY answered DESC LIMIT ?""", (since, limit)).fetchall()
        return [self._item(row) for row in rows]

    def remove(self, item_id: Any) -> bool:
        item_id = _clean(item_id, 64, field="id", required=True)
        with self._db() as db:
            removed = db.execute("DELETE FROM items WHERE id=?", (item_id,)).rowcount > 0
        for file in self.media.glob(f"{item_id}-*"):
            file.unlink(missing_ok=True)
        return removed

    def items(self, kinds: tuple[str, ...] = KINDS, *, limit: int = 100,
              include_dismissed: bool = False) -> list[dict]:
        kinds = tuple(kind for kind in kinds if kind in KINDS) or KINDS
        limit = max(1, min(int(limit), 200))
        placeholders = ",".join("?" for _ in kinds)
        where = f"kind IN ({placeholders})" + ("" if include_dismissed else " AND dismissed=0")
        with self._db() as db:
            rows = db.execute(f"SELECT * FROM items WHERE {where} ORDER BY created DESC LIMIT ?",
                              (*kinds, limit)).fetchall()
            return [self._item(row) for row in rows]

    def image(self, item_id: str, index: int) -> tuple[str, bytes]:
        with self._db() as db:
            row = db.execute("SELECT images FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise BoardError("That item no longer exists.")
        images = json.loads(row["images"])
        if not 0 <= index < len(images) or images[index].get("file") is None:
            raise BoardError("That image is not stored on this computer.")
        path = self.media / images[index]["file"]
        data = path.read_bytes()
        kind = _image_type(data[:16])
        if kind is None:
            raise BoardError("That image is not stored on this computer.")
        return kind[0], data

    def file_reference(self, item_id: str, index: int) -> tuple[str, int]:
        """The host path a Feed post refers to and when the agent attached it, for
        the attachment routes only. Attaching the same path again may mean a
        rewritten file; a thumbs up or read state doesn't."""
        with self._db() as db:
            row = db.execute("SELECT files FROM items WHERE id=? AND kind='feed'", (item_id,)).fetchone()
        if row is None:
            raise BoardError("That post no longer exists.")
        files = json.loads(row["files"])
        if not isinstance(index, int) or not 0 <= index < len(files):
            raise BoardError("That post has no such file.")
        return files[index]["path"], int(files[index].get("added", 0))

    @staticmethod
    def _files(files: Any) -> list[dict]:
        """Host files a post refers to. Each goes through Hermes' own delivery policy,
        the one ``MEDIA:`` files in chat get (no credentials, system folders or
        Hermes' own secrets; strict hosts only allow their media folders), and is
        checked again when the app fetches it."""
        if not isinstance(files, list) or len(files) > MAX_FILES:
            raise BoardError(f"files must be a list of at most {MAX_FILES} file paths.")
        try:
            from gateway.platforms.base import BasePlatformAdapter
        except ImportError:
            raise BoardError("This Hermes host can't attach files to posts.") from None
        from .attachments import _safe_filename
        rows: list[dict] = []
        for index, raw in enumerate(files):
            if not isinstance(raw, str) or not raw.strip() or len(raw) > 4_096:
                raise BoardError("Each file is an absolute file path on this computer.")
            raw = raw.strip()
            # The app gets what a MEDIA: line would deliver, so a path that line can't carry is refused here.
            parsed = BasePlatformAdapter.extract_media("MEDIA:" + raw)[0]
            safe = (BasePlatformAdapter.validate_media_delivery_path(raw)
                    if Path(raw).expanduser().is_absolute() and len(parsed) == 1 else None)
            if safe is None:
                raise BoardError(f"File {index + 1} can't be shared: it isn't a file on this computer, "
                                 "or it's in a private folder.")
            if any(row["path"] == safe for row in rows):
                continue
            size = Path(safe).stat().st_size
            if size == 0:
                raise BoardError(f"File {index + 1} is empty.")
            if size > MAX_FILE_BYTES:
                raise BoardError(f"File {index + 1} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
            name = _safe_filename(Path(safe).name)
            rows.append({"path": safe, "name": name,
                         "mimeType": mimetypes.guess_type(name)[0] or "application/octet-stream", "size": size})
        return rows

    @staticmethod
    def _links(links: Any) -> list[dict]:
        if links in (None, ""):
            return []
        if not isinstance(links, list) or len(links) > MAX_LINKS:
            raise BoardError(f"links must be a list of at most {MAX_LINKS}.")
        rows = []
        for link in links:
            if isinstance(link, str):
                link = {"url": link}
            if not isinstance(link, dict):
                raise BoardError("Each link needs a url.")
            url = _clean(link.get("url"), 2_000, field="link url", required=True)
            if not url.startswith(("https://", "http://")):
                raise BoardError("Links must be http or https URLs.")
            rows.append({"url": url, "title": _clean(link.get("title"), MAX_TITLE, field="link title")})
        return rows

    def _store_images(self, item_id: str, images: Any) -> list[dict]:
        if images in (None, ""):
            return []
        if not isinstance(images, list) or len(images) > MAX_IMAGES:
            raise BoardError(f"images must be a list of at most {MAX_IMAGES}.")
        rows: list[dict] = []
        for index, image in enumerate(images):
            if not isinstance(image, str) or not image.strip():
                raise BoardError("Each image is a file path or an https URL.")
            image = image.strip()
            if image.startswith("https://"):
                if len(image) > 2_000:
                    raise BoardError("An image URL is too long.")
                rows.append({"url": image})
                continue
            # A copy keeps the post intact after caches are cleaned, and the app
            # can only ever read these copies, never an arbitrary path.
            source = Path(image).expanduser()
            if not source.is_absolute() or not source.is_file():
                raise BoardError(f"Image {index + 1} is not a file on this computer.")
            if source.stat().st_size > MAX_IMAGE_BYTES:
                raise BoardError(f"Image {index + 1} is larger than 8 MB.")
            data = source.read_bytes()
            kind = _image_type(data[:16])
            if kind is None:
                raise BoardError(f"Image {index + 1} is not a PNG, JPEG, GIF, WebP or HEIC image.")
            self.media.mkdir(parents=True, exist_ok=True)
            name = f"{item_id}-{index}.{kind[1]}"
            (self.media / name).write_bytes(data)
            rows.append({"file": name, "mimeType": kind[0]})
        return rows

    def _prune(self, db, kind: str) -> None:
        stale = db.execute("SELECT id FROM items WHERE kind=? ORDER BY created DESC LIMIT -1 OFFSET ?",
                           (kind, MAX_ITEMS_PER_KIND)).fetchall()
        for row in stale:
            db.execute("DELETE FROM items WHERE id=?", (row["id"],))
            for file in self.media.glob(f"{row['id']}-*"):
                file.unlink(missing_ok=True)

    @staticmethod
    def _item(row) -> dict:
        images = []
        for index, image in enumerate(json.loads(row["images"])):
            images.append({"url": image["url"]} if "url" in image
                          else {"index": index, "mimeType": image.get("mimeType", "")})
        return {
            "id": row["id"], "kind": row["kind"], "title": row["title"], "body": row["body"],
            "icon": row["icon"], "section": row["section"], "status": row["status"], "note": row["note"],
            "links": json.loads(row["links"]), "images": images, "source": row["source"],
            # Names, types and sizes only: the path stays on the host.
            "files": [{"index": index, "fileName": file["name"], "mimeType": file["mimeType"],
                       "byteCount": file["size"], "addedAt": int(file.get("added", 0))}
                      for index, file in enumerate(json.loads(row["files"]))],
            "liked": row["rating"] == 1, "dismissed": bool(row["dismissed"]),
            "rating": {-1: "down", 1: "up"}.get(row["rating"], "none"), "reason": row["reason"],
            "read": bool(row["read"]), "answer": row["answer"] or "none", "category": row["category"],
            "createdAt": int(row["created"]), "updatedAt": int(row["updated"]),
        }

    # MARK: Activity and approvals

    def record_activity(self, *, session_id: str, turn_id: str, request: str, summary: str,
                        category: str, tools: list[str], outcome: str, now: float | None = None) -> None:
        with self._db() as db:
            db.execute("""INSERT INTO activity(session_id,turn_id,request,summary,category,tools,outcome,created)
                          VALUES(?,?,?,?,?,?,?,?)""",
                       (session_id, turn_id, request[:400], summary[:400], category,
                        json.dumps(tools[:40]), outcome, time.time() if now is None else now))
            db.execute("DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT ?)",
                       (MAX_ACTIVITY,))

    def activity(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self._db() as db:
            rows = db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": row["id"], "sessionId": row["session_id"], "request": row["request"],
                 "summary": row["summary"], "category": row["category"], "tools": json.loads(row["tools"]),
                 "outcome": row["outcome"], "createdAt": int(row["created"])} for row in rows]

    def record_approval(self, *, session_id: str, description: str, command: str, choice: str,
                        now: float | None = None) -> None:
        with self._db() as db:
            db.execute("INSERT INTO approvals(session_id,description,command,choice,created) VALUES(?,?,?,?,?)",
                       (session_id, description[:300], command[:300], choice[:40],
                        time.time() if now is None else now))
            db.execute("DELETE FROM approvals WHERE id NOT IN (SELECT id FROM approvals ORDER BY id DESC LIMIT ?)",
                       (MAX_APPROVALS,))

    def approvals(self, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        with self._db() as db:
            rows = db.execute("SELECT * FROM approvals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"id": row["id"], "sessionId": row["session_id"], "description": row["description"],
                 "command": row["command"], "choice": row["choice"], "createdAt": int(row["created"])}
                for row in rows]


_stores: dict[str, BoardStore] = {}
_stores_lock = threading.Lock()


def store_for_home(home: Path) -> BoardStore:
    directory = (home / "plugin-data" / "loopdy").resolve()
    with _stores_lock:
        store = _stores.get(str(directory))
        if store is None:
            store = BoardStore(directory)
            _stores[str(directory)] = store
        return store


def current_store() -> BoardStore:
    """The store of the profile whose turn is running (context-local home)."""
    from hermes_constants import get_hermes_home
    return store_for_home(get_hermes_home())


def store_for_profile(profile: str) -> BoardStore:
    from hermes_cli.profiles import get_profile_dir
    return store_for_home(get_profile_dir(profile))


def available() -> bool:
    try:
        from hermes_cli.profiles import get_profile_dir  # noqa: F401
        from hermes_constants import get_hermes_home  # noqa: F401
    except ImportError:
        return False
    return True


MAX_IDENTITY_TEXT = 64 * 1024


def identity(profile: str) -> dict:
    """SOUL and built-in memory for the app's Identity cards. Read-only, capped."""
    from hermes_cli.profiles import get_profile_dir
    home = get_profile_dir(profile)

    def read(path: Path) -> dict:
        try:
            info = path.stat()
        except OSError:
            return {"text": "", "updatedAt": 0, "truncated": False}
        if not path.is_file():
            return {"text": "", "updatedAt": 0, "truncated": False}
        with path.open("rb") as handle:
            data = handle.read(MAX_IDENTITY_TEXT + 1)
        text = data[:MAX_IDENTITY_TEXT].decode("utf-8", errors="replace")
        return {"text": text, "updatedAt": int(info.st_mtime), "truncated": len(data) > MAX_IDENTITY_TEXT}

    return {"soul": read(home / "SOUL.md"), "memory": read(home / "memories" / "MEMORY.md"),
            "user": read(home / "memories" / "USER.md")}


def session_titles(profile: str, session_ids: list[str]) -> dict[str, str]:
    """Hermes' own session titles, read-only, for activity rows."""
    if not session_ids:
        return {}
    from hermes_cli.profiles import get_profile_dir
    path = get_profile_dir(profile) / "state.db"
    if not path.is_file():
        return {}
    ids = list(dict.fromkeys(session_ids))[:200]
    try:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            rows = db.execute(f"SELECT id, title FROM sessions WHERE id IN ({','.join('?' for _ in ids)})",
                              ids).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return {}
    return {row[0]: row[1] for row in rows if isinstance(row[1], str) and row[1].strip()}


# MARK: Tool

TOOL_DESCRIPTION = (
    "Publish to the user's bighelp app. Actions: 'post' adds a Feed post (a briefing, news item or "
    "update with optional images, links and files the user can open, save and share), or updates the "
    "post with that id; 'idea' proposes something you could do for the user; "
    "'goal' adds or updates a Goal (section 'tracking' for things you watch, 'goal' for the user's "
    "own goals) with a short status note and a category; 'update_goal' changes a goal's note or category, "
    "or marks it done; "
    "'list' shows recent items with the user's rating (up/down), reason and read state, so you can "
    "update rather than duplicate and post more of what they rate up, plus 'answered': ideas the user "
    "said yes, goal or not now to in the last 30 days; 'remove' deletes one. "
    "Only publish what the user asked you to surface. Never create schedules or posts on your own "
    "initiative; see the bighelp-feed-and-ideas skill."
)

TOOL_PARAMETERS = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["post", "idea", "goal", "update_goal", "list", "remove"]},
        "id": {"type": "string", "maxLength": 64,
               "description": "Stable id to update an existing post, goal or idea instead of adding a new one."},
        "title": {"type": "string", "maxLength": MAX_TITLE},
        "body": {"type": "string", "maxLength": MAX_BODY,
                 "description": "Markdown. Keep Feed posts to a short paragraph; ideas explain the offer."},
        "icon": {"type": "string", "maxLength": MAX_ICON, "description": "One emoji that fits the item."},
        "section": {"type": "string", "maxLength": MAX_SECTION,
                    "description": "Ideas: a short category like Health or Shopping. Goals: tracking or goal."},
        "note": {"type": "string", "maxLength": MAX_NOTE, "description": "A goal's latest status in one line."},
        "status": {"type": "string", "enum": list(GOAL_STATUSES)},
        "category": {"type": "string", "enum": list(GOAL_CATEGORIES),
                     "description": "Goals: the one that fits best. The app groups goals by it; 'other' "
                                    "when none fits."},
        "images": {"type": "array", "maxItems": MAX_IMAGES, "items": {"type": "string"},
                   "description": "Absolute image file paths on this computer or https URLs."},
        "files": {"type": "array", "maxItems": MAX_FILES, "items": {"type": "string"},
                  "description": "Posts: absolute paths of files on this computer to attach (PDFs, pictures, "
                                 "documents; 25 MB each). Updating a post without files keeps its files; "
                                 "an empty list removes them."},
        "links": {"type": "array", "maxItems": MAX_LINKS, "items": {"type": "object", "properties": {
            "url": {"type": "string"}, "title": {"type": "string"}}, "required": ["url"]}},
        "kind": {"type": "string", "enum": list(KINDS), "description": "For list: which items to show."},
        "source": {"type": "string", "maxLength": 200,
                   "description": "Which automation or request produced this, e.g. 'Evening AI news'."},
    },
    "required": ["action"],
}


def handle_tool(args: dict, store: BoardStore | None = None) -> str:
    store = store or current_store()
    action = args.get("action")
    try:
        if action == "post":
            item = store.publish("feed", title=args.get("title"), body=args.get("body"), icon=args.get("icon"),
                                 links=args.get("links"), images=args.get("images"), source=args.get("source"),
                                 files=args.get("files"), item_id=args.get("id"))
        elif action == "idea":
            item = store.publish("idea", title=args.get("title"), body=args.get("body"), icon=args.get("icon"),
                                 section=args.get("section"), links=args.get("links"),
                                 source=args.get("source"), item_id=args.get("id"))
        elif action == "goal":
            item = store.publish("goal", title=args.get("title"), body=args.get("body"), icon=args.get("icon"),
                                 section=args.get("section"), note=args.get("note"), status=args.get("status"),
                                 category=args.get("category"), source=args.get("source"), item_id=args.get("id"))
        elif action == "update_goal":
            item = store.update_goal(args.get("id"), note=args.get("note"), status=args.get("status"),
                                     category=args.get("category"))
        elif action == "list":
            kind = args.get("kind")
            items = store.items((kind,) if kind in KINDS else KINDS, limit=30, include_dismissed=False)
            answered = store.answered_ideas() if kind in (None, "idea") else []
            # The person's thumbs, reasons, read state and answers to ideas tell the
            # agent what's worth offering.
            return json.dumps({
                "items": [{key: item[key] for key in
                           ("id", "kind", "title", "section", "status", "note", "category", "source",
                            "createdAt", "rating", "reason", "read", "answer")}
                          | ({"files": [file["fileName"] for file in item["files"]]} if item["files"] else {})
                          for item in items],
                "answered": [{"id": item["id"], "title": item["title"], "section": item["section"],
                              "answer": item["answer"], "rating": item["rating"], "reason": item["reason"]}
                             for item in answered],
            })
        elif action == "remove":
            return json.dumps({"removed": store.remove(args.get("id"))})
        else:
            raise BoardError("action must be post, idea, goal, update_goal, list or remove.")
    except BoardError as error:
        return json.dumps({"error": str(error)})
    result = {"ok": True, "id": item["id"], "kind": item["kind"],
              "shownIn": {"feed": "Feed", "idea": "Ideas", "goal": "Goals"}[item["kind"]]}
    if item["files"]:
        result["files"] = len(item["files"])
    return json.dumps(result)


# MARK: Hooks

_TOOL_CATEGORIES = (
    ("images", ("image_generate", "video", "image", "mixture_of")),
    ("coding", ("terminal", "execute_code", "process", "patch", "code")),
    ("web", ("web_search", "web_extract", "browser", "search_web", "fetch")),
    ("seeing", ("vision",)),
    ("memory", ("memory", "session_search", "skill")),
    ("scheduling", ("cronjob", "schedule", "cron")),
    ("delegating", ("delegate", "subagent")),
    ("files", ("read_file", "write_file", "search_files", "file")),
    ("messaging", ("send_message", "text_to_speech", "tts")),
    ("publishing", (TOOL_NAME,)),
)


def tool_category(name: str) -> str:
    lowered = name.lower()
    for category, markers in _TOOL_CATEGORIES:
        if any(marker in lowered for marker in markers):
            return category
    return "tools"


def _first_sentence(text: Any, limit: int = 180) -> str:
    if not isinstance(text, str):
        return ""
    text = re.sub(r"`{3}.*?`{3}", " ", text, flags=re.S)
    text = " ".join(re.sub(r"[#*_>`\[\]]", " ", text).split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    sentence = match.group(1) if match else text
    return sentence if len(sentence) <= limit else sentence[: limit - 1].rstrip() + "…"


class ActivityRecorder:
    """Per-process turn buffers; a turn starts and ends in the same process."""

    def __init__(self, store_getter=current_store):
        self._turns: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self._lock = threading.Lock()
        self._store_getter = store_getter

    def observe(self, hook: str, **payload: Any) -> None:
        try:
            self._observe(hook, **payload)
        except Exception as error:  # observability must never break a turn
            logger.debug("bighelp board observer skipped %s: %s", hook, type(error).__name__)

    def _observe(self, hook: str, **payload: Any) -> None:
        if payload.get("parent_session_id") or payload.get("platform") == "subagent":
            return
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return
        if hook == "post_approval_response":
            choice = payload.get("choice")
            if isinstance(choice, str) and choice:
                self._store_getter().record_approval(
                    session_id=session_id, description=str(payload.get("description") or ""),
                    command=str(payload.get("command") or ""), choice=choice)
            return
        turn_id = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else ""
        key = (session_id, turn_id)
        with self._lock:
            if hook == "pre_llm_call":
                message = payload.get("user_message")
                accepted = _LETS_DO_IT.match(message) if isinstance(message, str) else None
                if accepted and key not in self._turns:
                    self._store_getter().accept_idea(accepted.group(1))
                if key not in self._turns:
                    self._turns[key] = {"request": _first_sentence(message, 200) if isinstance(message, str)
                                        else "", "tools": [], "response": ""}
                    while len(self._turns) > 64:
                        self._turns.popitem(last=False)
                return
            turn = self._turns.get(key) or self._turns.get((session_id, ""))
            if turn is None:
                return
            if hook == "post_tool_call" and isinstance(payload.get("tool_name"), str):
                if len(turn["tools"]) < 200:
                    turn["tools"].append(payload["tool_name"])
                return
            if hook == "post_llm_call" and isinstance(payload.get("assistant_response"), str):
                turn["response"] = payload["assistant_response"]
                return
            if hook != "on_session_end":
                return
            self._turns.pop(key, None)
        # Only turns that did something become activity; plain chat stays in the chat.
        tools = [name for name in turn["tools"] if name != TOOL_NAME] or turn["tools"]
        if not tools:
            return
        counts: dict[str, int] = {}
        for name in tools:
            category = tool_category(name)
            counts[category] = counts.get(category, 0) + 1
        category = max(counts, key=lambda value: (counts[value], value != "tools"))
        outcome = ("stopped" if payload.get("interrupted") is True
                   else "failed" if payload.get("failed") is True else "done")
        self._store_getter().record_activity(
            session_id=session_id, turn_id=turn_id, request=turn["request"],
            summary=_first_sentence(turn["response"]), category=category,
            tools=list(dict.fromkeys(tools)), outcome=outcome)


def register(ctx: Any) -> None:
    """Register the board tool and its activity/approval observers."""
    ctx.register_tool(
        name=TOOL_NAME, toolset=TOOLSET,
        schema={"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "parameters": TOOL_PARAMETERS},
        handler=lambda args, **_: handle_tool(args), emoji="📌",
    )
    recorder = ActivityRecorder()
    for hook in ("pre_llm_call", "post_tool_call", "post_llm_call", "on_session_end", "post_approval_response"):
        ctx.register_hook(hook, lambda _hook=hook, **payload: recorder.observe(_hook, **payload))
    skill = Path(__file__).resolve().parents[1] / "skills" / "bighelp-feed-and-ideas" / "SKILL.md"
    description = ("Use when the user wants regular updates, briefings, news, ideas or goal tracking "
                   "surfaced in the bighelp app's Feed, Ideas or Goals.")
    ctx.register_skill("bighelp-feed-and-ideas", skill, description=description,
                       frontmatter={"name": "bighelp-feed-and-ideas", "description": description})


def media_payload(mime: str, data: bytes) -> dict:
    return {"mimeType": mime, "data": base64.b64encode(data).decode("ascii"), "byteCount": len(data)}
