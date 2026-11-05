"""An agent's core files for the app's Core Files page, read and saved whole.

- ``soul``: ``SOUL.md`` in the agent's Hermes home, who the agent is.
- ``user``: ``memories/USER.md``, what it remembers about the person.
- ``memory``: ``memories/MEMORY.md``, its own notes.
- ``agents``: the ``AGENTS.md`` Hermes loads for one chat: the deepest one on the chain from the git root down to
  the chat's working folder (``agent.prompt_builder``), only when that file exists. Moving a chat to another
  project moves its folder, so the file follows the chat. Without a chat (apps before ``native-core-files-chat-v1``)
  it's the one in the main folder of the agent's active project.

Nothing else is reachable: the route takes a kind, never a path. Links are refused, a save replaces the file in
one step, and a save that names the hash it was based on is refused when the file changed since. Hermes' memory
tool writes the two memory files under a lock beside them (``MemoryStore._file_lock``); saves take the same lock,
so a save and a memory write never interleave. Hermes reads these files when a chat starts, so a change reaches
the agent's next chat.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

CAPABILITY = "native-core-files-v1"
# The routes take the chat whose AGENTS.md to show (``sessionId``).
CHAT_CAPABILITY = "native-core-files-chat-v1"
MAX_BYTES = 512 * 1024
KINDS = ("soul", "user", "memory", "agents")
NAMES = {"soul": "SOUL.md", "user": "USER.md", "memory": "MEMORY.md", "agents": "AGENTS.md"}


class CoreFileError(Exception):
    """A refusal the route answers with a fixed code: ``not_found``, ``changed``, ``too_large``, ``invalid``."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def available() -> bool:
    try:
        from hermes_cli.profiles import get_profile_dir, profile_exists  # noqa: F401
    except Exception:
        return False
    return True


def _home(profile: str) -> Path:
    from hermes_cli.profiles import get_profile_dir, profile_exists
    if not profile_exists(profile):
        raise CoreFileError("not_found")
    return Path(get_profile_dir(profile))


def _active_project(profile: str) -> tuple[Path, str] | None:
    """The active project's main folder and name, read in the agent's own profile; None without one."""
    try:
        from hermes_cli import projects_db
        from .workspace_capabilities import profile_scope
        with profile_scope(profile):
            if not projects_db.projects_db_path().is_file():
                return None
            with projects_db.connect_closing() as conn:
                active = projects_db.get_active_id(conn)
                project = projects_db.get_project(conn, active) if active else None
    except Exception:
        return None
    if project is None or project.archived:
        return None
    root = project.primary_path or next((folder.path for folder in project.folders if folder.is_primary), None)
    return (Path(root), project.name) if root else None


def _chat_folder(profile: str, session: str) -> Path | None:
    """The chat's working folder as Hermes runs it now (the live part of a compressed chat), read from the
    agent's own state; None when the chat isn't this agent's or has no folder."""
    from hermes_state import SessionDB
    path = _home(profile) / "state.db"
    if path.is_symlink() or not path.is_file():
        return None
    try:
        db = SessionDB(db_path=path, read_only=True)
        try:
            row = db.get_session(session)
            if not row or row.get("profile_name") not in (None, profile):
                return None
            tip = db.get_compression_tip(session)
            live = db.get_session(tip) if isinstance(tip, str) and tip != session else None
            cwd = (live or {}).get("cwd") or row.get("cwd")
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        return None
    return Path(cwd) if isinstance(cwd, str) and os.path.isabs(cwd) else None


def _chat_agents(profile: str, folder: Path) -> tuple[Path, str] | None:
    """The AGENTS.md Hermes loads in ``folder`` (the deepest on its chain) and the project it belongs to."""
    try:
        from agent.prompt_builder import _agents_md_directory_chain
        chain = _agents_md_directory_chain(folder)
    except Exception:
        chain = [folder]
    agents = next((directory / "AGENTS.md" for directory in reversed(chain)
                   if (directory / "AGENTS.md").is_file() and not (directory / "AGENTS.md").is_symlink()), None)
    if agents is None:
        return None
    name = folder.name
    try:
        from hermes_cli import projects_db
        from .workspace_capabilities import profile_scope
        with profile_scope(profile):
            if projects_db.projects_db_path().is_file():
                with projects_db.connect_closing() as conn:
                    project = projects_db.project_for_path(conn, str(folder))
                    name = project.name if project is not None else name
    except Exception:
        pass
    return agents, name


def _locations(profile: str, session: str | None = None) -> dict[str, tuple[Path, str | None]]:
    home = _home(profile)
    locations: dict[str, tuple[Path, str | None]] = {
        "soul": (home / "SOUL.md", None),
        "user": (home / "memories" / "USER.md", None),
        "memory": (home / "memories" / "MEMORY.md", None),
    }
    if session:
        folder = _chat_folder(profile, session)
        found = _chat_agents(profile, folder) if folder is not None else None
        if found is not None:
            locations["agents"] = found
        return locations
    project = _active_project(profile)
    if project is not None:
        root, name = project
        agents = root / "AGENTS.md"
        if agents.is_file() and not agents.is_symlink():
            locations["agents"] = (agents, name)
    return locations


def _location(profile: str, kind: str, session: str | None = None) -> tuple[Path, str | None]:
    if kind not in KINDS:
        raise CoreFileError("invalid")
    found = _locations(profile, session).get(kind)
    if found is None:
        raise CoreFileError("not_found")
    return found


def _describe(kind: str, path: Path, project: str | None) -> dict[str, Any]:
    if path.is_symlink():
        raise CoreFileError("invalid")
    row: dict[str, Any] = {"kind": kind, "name": NAMES[kind]}
    if project:
        row["project"] = project
    try:
        info = path.stat()
    except FileNotFoundError:
        return {**row, "exists": False, "size": 0}
    if not path.is_file():
        raise CoreFileError("invalid")
    return {**row, "exists": True, "size": info.st_size, "updatedAt": int(info.st_mtime)}


def files(profile: str, session: str | None = None) -> dict:
    """The agent's core files, in a fixed order, with whether each one exists; AGENTS.md is the chat's."""
    rows = []
    for kind, (path, project) in _locations(profile, session).items():
        try:
            rows.append(_describe(kind, path, project))
        except CoreFileError:
            continue
    return {"files": rows}


def read(profile: str, kind: str, session: str | None = None) -> dict:
    """The whole file (the first ``MAX_BYTES`` of a longer one, marked truncated); empty when it doesn't exist."""
    path, project = _location(profile, kind, session)
    row = _describe(kind, path, project)
    data = b""
    if row["exists"]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    text = data[:MAX_BYTES].decode("utf-8", errors="replace")
    return {**row, "text": text, "truncated": len(data) > MAX_BYTES,
            "sha256": hashlib.sha256(data).hexdigest() if row["exists"] else None}


def write(profile: str, kind: str, text: str, expected_sha256: str | None = None, session: str | None = None) -> dict:
    """Replaces the file with ``text``. ``expected_sha256`` is the hash the app read (None: it read no file);
    a file that changed since is left as it is. AGENTS.md is saved only where it already exists."""
    from .workspace_capabilities import profile_scope
    path, project = _location(profile, kind, session)
    data = text.encode("utf-8")
    if len(data) > MAX_BYTES:
        raise CoreFileError("too_large")
    if "\x00" in text:
        raise CoreFileError("invalid")
    # In the agent's own home, so the memory lock lands beside its memory files.
    with profile_scope(profile), (_memory_lock(path) if kind in ("user", "memory") else nullcontext()):
        current = _describe(kind, path, project)
        if kind == "agents" and not current["exists"]:
            raise CoreFileError("not_found")
        if expected_sha256 is not None or current["exists"]:
            on_disk = hashlib.sha256(path.read_bytes()).hexdigest() if current["exists"] else None
            if on_disk != expected_sha256:
                raise CoreFileError("changed")
        path.parent.mkdir(parents=True, exist_ok=True)
        _replace(path, data)
    return read(profile, kind, session)


def _memory_lock(path: Path):
    try:
        from tools.memory_tool_store import MemoryStore
        return MemoryStore._file_lock(path)
    except Exception:
        return nullcontext()


def _replace(path: Path, data: bytes) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    handle = tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise
