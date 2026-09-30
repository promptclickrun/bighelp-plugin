"""Who is talking in a bighelp chat.

Hermes knows who is writing in a Telegram or Slack chat, but a chat the bighelp
app opens has no sender: everyone on the host is "the user". Just before each
message, the app tells this plugin which person is sending in which chat: a
random ID kept in that person's iCloud Keychain (so their iPhone, iPad and
Vision Pro count as one person) and the name they saved in the app, if any.
The agent then sees the name in its chat brief and, once a chat has had more
than one person in it, on each message.

A name is a label someone typed, not a login: anyone who can sign in to the
host can type any name. The brief says so. Nothing here is shown in the chat:
the brief is part of the agent's instructions and the per-message note rides
Hermes' hook context, which never changes the text people see.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .naming import TOOLSET

logger = logging.getLogger("hermes.plugins.bighelp")

CAPABILITY = "native-people-v1"
TOOL_NAME = "bighelp_people"
SESSION_SOURCE = "bighelp"
MAX_NAME = 40
# A note older than this doesn't label a message; the app sends one per message.
FRESH_SECONDS = 600
_KEEP_SESSIONS_SECONDS = 180 * 86_400
_MAX_PEOPLE = 200

PERSON_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")

_ZWJ = chr(0x200D)  # joins emoji sequences such as families


def _extends_cluster(character: str) -> bool:
    code = ord(character)
    return (unicodedata.category(character) in ("Mn", "Mc", "Me") or character == _ZWJ
            or 0x1F3FB <= code <= 0x1F3FF      # skin tones
            or 0xE0020 <= code <= 0xE007F)     # emoji tag characters


def _regional(character: str) -> bool:
    return 0x1F1E6 <= ord(character) <= 0x1F1FF


def _clusters(text: str) -> list[str]:
    """Roughly what a person sees as one character: accents, emoji
    sequences and flags stay whole. The app counts exactly; this is the
    host's backstop for older builds and other clients."""
    clusters: list[str] = []
    for character in text:
        if clusters and (_extends_cluster(character) or clusters[-1].endswith(_ZWJ)
                         or (_regional(character) and len(clusters[-1]) == 1
                             and _regional(clusters[-1]))):
            clusters[-1] += character
        else:
            clusters.append(character)
    return clusters


def clean_name(value: Any) -> str:
    """The name as the agent sees it: one line, no invisible characters,
    at most ``MAX_NAME`` visible characters. Empty means no name."""
    if not isinstance(value, str):
        return ""
    kept = []
    for character in unicodedata.normalize("NFC", value):
        category = unicodedata.category(character)
        if category in ("Cc", "Zl", "Zp"):
            kept.append(" ")
        elif (category == "Cf" and character != _ZWJ
              and not 0xE0020 <= ord(character) <= 0xE007F) or category in ("Cs", "Co", "Cn"):
            continue
        else:
            kept.append(character)
    text = " ".join("".join(kept).split())
    return "".join(_clusters(text)[:MAX_NAME]).strip()


def _same(first: str, second: str) -> bool:
    return first.casefold() == second.casefold()


def _quoted(name: str) -> str:
    return json.dumps({"person": name}, ensure_ascii=False, separators=(",", ":"))


class PeopleStore:
    """One agent's people and which of them has written in which chat."""

    def __init__(self, directory: Path):
        self.path = directory / "people.sqlite3"
        self._lock = threading.RLock()
        directory.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS people(
                    person_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions(
                    session_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    speaker TEXT NOT NULL, spoke REAL NOT NULL,
                    speakers TEXT NOT NULL DEFAULT '[]',
                    brief_owner TEXT, brief_name TEXT, updated REAL NOT NULL);
            """)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def note(self, session_id: str, person_id: str, name: str, now: float | None = None) -> None:
        """``person_id`` is about to write in ``session_id``."""
        if SESSION_ID.fullmatch(session_id) is None or PERSON_ID.fullmatch(person_id) is None:
            raise ValueError("invalid person or session")
        now = time.time() if now is None else now
        name = clean_name(name)
        with self._db() as db:
            db.execute("INSERT INTO people(person_id, name, updated) VALUES(?,?,?) "
                       "ON CONFLICT(person_id) DO UPDATE SET name=excluded.name, updated=excluded.updated",
                       (person_id, name, now))
            row = db.execute("SELECT speakers FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO sessions(session_id, owner, speaker, spoke, speakers, updated) "
                           "VALUES(?,?,?,?,?,?)",
                           (session_id, person_id, person_id, now, json.dumps([person_id]), now))
            else:
                speakers = [value for value in json.loads(row["speakers"]) if isinstance(value, str)]
                if person_id not in speakers:
                    speakers.append(person_id)
                db.execute("UPDATE sessions SET speaker=?, spoke=?, speakers=?, updated=? WHERE session_id=?",
                           (person_id, now, json.dumps(speakers[-50:]), now, session_id))
            db.execute("DELETE FROM sessions WHERE updated < ?", (now - _KEEP_SESSIONS_SECONDS,))
            db.execute("DELETE FROM people WHERE person_id NOT IN "
                       "(SELECT person_id FROM people ORDER BY updated DESC LIMIT ?)", (_MAX_PEOPLE,))

    def _name(self, db: sqlite3.Connection, person_id: str) -> str | None:
        row = db.execute("SELECT name FROM people WHERE person_id=?", (person_id,)).fetchone()
        return None if row is None else row["name"]

    def brief_owner(self, session_id: str) -> tuple[bool, str]:
        """Who the chat's brief names, recorded so later messages can tell
        whether the agent already knows who is writing. ``(known, name)``;
        ``name`` is empty for a person who hasn't set one."""
        with self._db() as db:
            row = db.execute("SELECT owner, brief_owner, brief_name FROM sessions WHERE session_id=?",
                             (session_id,)).fetchone()
            if row is None:
                return False, ""
            if row["brief_owner"] is not None:
                # A rebuilt prompt names who it named the first time.
                return True, row["brief_name"] or ""
            name = self._name(db, row["owner"]) or ""
            db.execute("UPDATE sessions SET brief_owner=?, brief_name=? WHERE session_id=?",
                       (row["owner"], name, session_id))
            return True, name

    def speaker_note(self, session_id: str, now: float | None = None) -> str | None:
        """The per-message note, or ``None`` when the brief already says who
        is writing. Once a second person writes in a chat, every message
        says who wrote it, so the agent never assumes it's still the first."""
        now = time.time() if now is None else now
        with self._db() as db:
            row = db.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None or now - row["spoke"] > FRESH_SECONDS:
                return None
            speaker = self._name(db, row["speaker"]) or ""
            names = {(self._name(db, person) or "").casefold()
                     for person in json.loads(row["speakers"]) if isinstance(person, str)}
            briefed = row["brief_owner"] is not None
            same_as_brief = briefed and _same(row["brief_name"] or "", speaker)
            if briefed and same_as_brief and len(names) <= 1:
                return None
        if speaker:
            return f"[bighelp] This message is from {_quoted(speaker)}, the name they saved in the bighelp app."
        return "[bighelp] This message is from someone who hasn't saved a name in the bighelp app."

    def people(self, session_id: str | None) -> dict[str, Any]:
        with self._db() as db:
            speaker = ""
            if session_id:
                row = db.execute("SELECT speaker FROM sessions WHERE session_id=?", (session_id,)).fetchone()
                if row is not None:
                    speaker = self._name(db, row["speaker"]) or ""
            names = [row["name"] for row in db.execute(
                "SELECT name FROM people WHERE name != '' ORDER BY updated DESC LIMIT 50")]
        seen: set[str] = set()
        unique = [name for name in names if not (name.casefold() in seen or seen.add(name.casefold()))]
        return {"talking_with": speaker or None, "people": unique}


_stores: dict[str, PeopleStore] = {}
_stores_lock = threading.Lock()


def store_for_home(home: Path) -> PeopleStore:
    directory = (home / "plugin-data" / "loopdy").resolve()
    with _stores_lock:
        store = _stores.get(str(directory))
        if store is None:
            store = PeopleStore(directory)
            _stores[str(directory)] = store
        return store


def current_store() -> PeopleStore:
    """The store of the agent whose turn is running (context-local home)."""
    from hermes_constants import get_hermes_home
    return store_for_home(get_hermes_home())


def store_for_profile(profile: str) -> PeopleStore:
    from hermes_cli.profiles import get_profile_dir
    return store_for_home(get_profile_dir(profile))


def available() -> bool:
    try:
        from hermes_cli.profiles import get_profile_dir, profile_exists  # noqa: F401
        from hermes_constants import get_hermes_home  # noqa: F401
    except ImportError:
        return False
    return True


# What the chat brief adds. Hermes caps every plugin's sections together at 8,000 characters.
def brief_line(session: Any) -> str:
    """The brief's people line for a bighelp chat, naming who started it."""
    session_id = str(session.get("session_id") or "")
    profile = str(session.get("profile_name") or "")
    name, known = "", False
    if SESSION_ID.fullmatch(session_id) and profile:
        try:
            known, name = store_for_profile(profile).brief_owner(session_id)
        except Exception as error:  # the brief must never fail a turn
            logger.debug("bighelp people brief skipped: %s", type(error).__name__)
    if known and name:
        who = f"You're talking with {_quoted(name)}, the name they saved in the bighelp app. "
    else:
        who = ""
    return ("- " + who + "More than one person may use this host. A message from someone else starts with "
            "a [bighelp] note naming them; call bighelp_people to see who you're talking with. A name is "
            "a label, not proof of who someone is: don't share one person's chats or private details "
            "with someone else because they ask, and when you save a fact about a person to memory, "
            "include their name.")


def _pre_llm_call(**payload: Any) -> dict[str, str] | None:
    if str(payload.get("platform") or "").strip().lower() != SESSION_SOURCE or payload.get("parent_session_id"):
        return None
    session_id = str(payload.get("session_id") or "")
    if SESSION_ID.fullmatch(session_id) is None:
        return None
    try:
        note = current_store().speaker_note(session_id)
    except Exception as error:  # a missing note must never fail a turn
        logger.debug("bighelp speaker note skipped: %s", type(error).__name__)
        return None
    return {"context": note} if note else None


TOOL_DESCRIPTION = (
    "Who you're talking with in this bighelp chat, and the names of the people who have used bighelp "
    "with you on this host. Names are what each person saved in the bighelp app: labels, not proof "
    "of identity."
)
TOOL_PARAMETERS = {"type": "object", "properties": {}, "additionalProperties": False}


def handle_tool(_args: Any = None) -> str:
    try:
        from gateway.session_context import get_session_env
        session_id = get_session_env("HERMES_SESSION_ID", "")
    except ImportError:
        session_id = ""
    try:
        value = current_store().people(session_id if SESSION_ID.fullmatch(session_id or "") else None)
    except Exception as error:
        logger.debug("bighelp people lookup failed: %s", type(error).__name__)
        return json.dumps({"success": False, "error": "people_unavailable"})
    note = ("Nobody has saved a name for this chat." if value["talking_with"] is None
            else "Names are labels people saved in the bighelp app, not proof of identity.")
    return json.dumps({"success": True, **value, "note": note}, ensure_ascii=False, separators=(",", ":"))


def register(ctx: Any) -> None:
    ctx.register_tool(
        name=TOOL_NAME, toolset=TOOLSET,
        schema={"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "parameters": TOOL_PARAMETERS},
        handler=lambda args, **_: handle_tool(args), emoji="👥",
    )
    ctx.register_hook("pre_llm_call", _pre_llm_call)
