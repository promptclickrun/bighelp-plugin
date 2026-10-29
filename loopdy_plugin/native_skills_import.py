"""Skill import: a skill the person picked in the app (a .zip folder or one SKILL.md), added to one agent.

Stock Hermes can create a skill from SKILL.md text, but has no way to take a skill folder with its `scripts/`,
`references/`, `templates/` and `assets/`. This module checks an uploaded archive in full before it writes
anything, then writes the skill with Hermes' own writers (`tools.skill_manager_tool`), in the agent's own
profile. It never overwrites a skill, and when any step fails it deletes what it made, so nothing half-made
stays. Hermes' validators decide what a skill may hold; this module adds only the archive rules.
"""
from __future__ import annotations

import base64
import binascii
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
import io
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Any
import zipfile

from .workspace_capabilities import profile_scope

CAPABILITY = "native-skills-import-v1"
MAX_ZIP_BYTES = 3 * 1024 * 1024
# Base64 of MAX_ZIP_BYTES. The route's request limit leaves room for the JSON around it.
MAX_DATA_CHARS = 4 * ((MAX_ZIP_BYTES + 2) // 3)
MAX_REQUEST_BYTES = MAX_DATA_CHARS + 4096
MAX_ENTRIES = 200
# Folder entries and Mac junk don't count toward MAX_ENTRIES, but a table this long is not a skill.
MAX_TABLE_ENTRIES = 2000
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_PATH_CHARS = 240
logger = logging.getLogger("hermes.plugins.bighelp")

# Hermes' own functions and limits this module relies on.
_TOOL_NAMES = ("_validate_name", "_validate_category", "_validate_frontmatter", "_validate_file_path",
               "_resolve_supporting_file", "_create_skill", "_write_file", "_delete_skill", "_find_skill",
               "_resolve_skill_dir", "_security_scan_skill", "_parse_frontmatter")
_UNIX = 3
_UNSAFE_PATH = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029\\]")
_DRIVE = re.compile(r"[A-Za-z]:")
# An absolute path inside a Hermes message (two or more segments), for scrubbing before it reaches the app.
_HOST_PATH = re.compile(r"(?<![\w/.])/[^\s'\"():;,]+/[^\s'\"():;,]+")


class SkillImportError(Exception):
    """An import the route refuses with a fixed code and a plain message; `details` are safe facts."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        self.status, self.code, self.message, self.details = status, code, message, details or {}
        super().__init__(message)


def available() -> bool:
    """Only when Hermes' skill writers import (they are internals, so an odd Hermes turns this feature off)."""
    try:
        from tools import skill_manager_tool as tool
        return (all(callable(getattr(tool, name, None)) for name in _TOOL_NAMES)
                and isinstance(getattr(tool, "ALLOWED_SUBDIRS", None), (set, frozenset))
                and isinstance(getattr(tool, "MAX_SKILL_FILE_BYTES", None), int)
                and isinstance(getattr(tool, "MAX_SKILL_CONTENT_CHARS", None), int))
    except Exception:
        return False


def _invalid(message: str, code: str = "invalid_archive", **details: Any) -> SkillImportError:
    return SkillImportError(422, code, message, details)


def _too_large(message: str) -> SkillImportError:
    return SkillImportError(413, "too_large", message)


def _reason(message: Any) -> str:
    """A Hermes message for the app: no host paths, bounded."""
    text = _HOST_PATH.sub("<path>", " ".join(str(message).split()))
    return text if len(text) <= 300 else text[:297] + "..."


# MARK: - The archive


@dataclass
class _Skill:
    skill_md: str
    # Relative paths in write order, with their bytes.
    files: list[tuple[str, bytes]]


def _decode_data(data: str, limit: int) -> bytes:
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise SkillImportError(422, "invalid_request", "The upload is not valid base64.") from None
    if len(raw) > limit:
        raise _too_large("The upload is too big.")
    return raw


def _skill_text(raw: bytes, tool: Any) -> str:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _invalid("SKILL.md must be UTF-8 text.", "invalid_skill") from None
    if len(text) > tool.MAX_SKILL_CONTENT_CHARS:
        raise _too_large(f"SKILL.md is longer than {tool.MAX_SKILL_CONTENT_CHARS:,} characters.")
    return text


def _is_ignored(name: str) -> bool:
    parts = name.split("/")
    return "__MACOSX" in parts or parts[-1] == ".DS_Store"


def _check_entry(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if (_UNSAFE_PATH.search(name) or name.startswith("/") or _DRIVE.match(name) or len(name) > MAX_PATH_CHARS
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise _invalid("The archive has a file path that isn't allowed.")
    if info.create_system == _UNIX:
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            raise _invalid("The archive has a link or special file. Links aren't allowed.")
    if info.flag_bits & 0x1:
        raise _invalid("The archive is password protected.")
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise _invalid("The archive uses a compression type that isn't supported.")


def _strip_prefix(names: list[str]) -> str:
    """The folder to strip: nothing when SKILL.md is at the root, or the one top folder that holds it."""
    if "SKILL.md" in names:
        return ""
    tops = {name.split("/", 1)[0] for name in names}
    if len(tops) == 1 and all("/" in name for name in names) and f"{next(iter(tops))}/SKILL.md" in names:
        return next(iter(tops)) + "/"
    if any(name.rsplit("/", 1)[-1] == "SKILL.md" for name in names):
        raise _invalid("SKILL.md must be at the top of the archive, or inside one folder.", "unsupported_layout")
    raise _invalid("The archive has no SKILL.md.", "missing_skill_md")


def _read_archive(raw: bytes, tool: Any) -> _Skill:
    """The whole archive, checked and read into memory. Nothing is written until all of it passes."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except (zipfile.BadZipFile, EOFError, OSError, ValueError, NotImplementedError):
        raise _invalid("This is not a .zip file, or it is damaged.") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_TABLE_ENTRIES:
            raise _too_large(f"The archive has more than {MAX_ENTRIES} files.")
        kept: dict[str, zipfile.ZipInfo] = {}
        seen: set[str] = set()
        total = 0
        for info in infos:
            if info.filename.endswith("/") or _is_ignored(info.filename):
                continue
            _check_entry(info)
            # Folded, so one name can't be written twice on a host with a case-insensitive disk.
            key = info.filename.casefold()
            if key in seen:
                raise _invalid("The archive has the same file twice.")
            seen.add(key)
            if len(kept) >= MAX_ENTRIES:
                raise _too_large(f"The archive has more than {MAX_ENTRIES} files.")
            if info.file_size > tool.MAX_SKILL_FILE_BYTES:
                raise _too_large(f"A file is bigger than {tool.MAX_SKILL_FILE_BYTES // 1024 // 1024} MB.")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise _too_large(f"The files are bigger than {MAX_TOTAL_BYTES // 1024 // 1024} MB together.")
            kept[info.filename] = info
        prefix = _strip_prefix(list(kept))
        contents: dict[str, bytes] = {}
        read = 0
        for name, info in kept.items():
            try:
                with archive.open(info) as stream:
                    data = stream.read(info.file_size + 1)
            except Exception as error:
                # BadZipFile, zlib.error and friends: a damaged entry. The class name is fixed and safe to log.
                logger.warning("bighelp skill import: unreadable archive entry (%s)", type(error).__name__)
                raise _invalid("The archive is damaged.") from None
            read += len(data)
            if len(data) != info.file_size or read > MAX_TOTAL_BYTES:
                raise _invalid("The archive is damaged.")
            contents[name] = data
    skill_md = _skill_text(contents[prefix + "SKILL.md"], tool)
    files = [(name[len(prefix):], data) for name, data in contents.items() if name != prefix + "SKILL.md"
             and name.startswith(prefix)]
    files.sort()
    for path, _ in files:
        parts = path.split("/")
        if parts[0] not in tool.ALLOWED_SUBDIRS or len(parts) < 2 or parts[-1].casefold() == "skill.md":
            raise _invalid(
                "Every file must be in scripts, references, templates or assets, and SKILL.md can't be repeated.",
                "unsupported_layout", path=path)
        if (problem := tool._validate_file_path(path)):
            raise _invalid(_reason(problem), "unsupported_layout", path=path)
    return _Skill(skill_md, files)


def _plan_files(files: list[tuple[str, bytes]], tool: Any) -> list[tuple[str, bytes, str | None]]:
    """Each supporting file with its text, or None for a binary one. Binary is for assets/ only."""
    plan = []
    for path, data in files:
        try:
            text: str | None = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        if text is not None and "\x00" in text:
            text = None
        in_assets = path.startswith("assets/")
        if text is None and not in_assets:
            raise _invalid("Only assets can hold files that aren't text.", "binary_not_allowed", path=path)
        if text is not None and len(text) > tool.MAX_SKILL_CONTENT_CHARS:
            if not in_assets:
                raise _too_large(f"A text file is longer than {tool.MAX_SKILL_CONTENT_CHARS:,} characters.")
            text = None
        plan.append((path, data, text))
    return plan


# MARK: - Checking and writing with Hermes


def _check_skill(content: str, category: str | None, tool: Any) -> tuple[str, str | None]:
    """The skill's name (from its frontmatter) and category, by Hermes' own rules."""
    try:
        if isinstance(category, str):
            category = category.strip() or None
        if problem := (tool._validate_category(category) or tool._validate_frontmatter(content, new_skill=True)):
            raise _invalid("Hermes can't take this skill.", "invalid_skill", reason=_reason(problem))
        frontmatter, _ = tool._parse_frontmatter(content)
    except SkillImportError:
        raise
    except Exception:
        raise _invalid("Hermes can't read this skill's header.", "invalid_skill") from None
    name = frontmatter.get("name") if isinstance(frontmatter, dict) else None
    if not isinstance(name, str):
        raise _invalid("The SKILL.md header needs a name.", "invalid_skill")
    name = name.strip()
    if problem := tool._validate_name(name):
        raise _invalid("The skill's name isn't allowed.", "invalid_skill", reason=_reason(problem))
    return name, category


def _refused(result: Any, *, writing: bool) -> SkillImportError:
    """Hermes turned a write down: a blocked scan, a skill that appeared meanwhile, or another rule."""
    message = str(result.get("error", "")) if isinstance(result, dict) else ""
    if message.startswith("Security scan blocked"):
        return SkillImportError(422, "blocked", "Hermes' security scan blocked this skill.",
                                {"reason": _reason(message.split("\n", 1)[0])})
    if "already exists" in message and not writing:
        return SkillImportError(409, "skill_exists", "This agent already has a skill with that name.")
    return _invalid("Hermes refused this skill.", "invalid_skill", reason=_reason(message))


def _write_binary(tool: Any, skill_dir: Path, path: str, data: bytes) -> None:
    """A file that isn't text, with the same path checks and size cap as Hermes' text writer."""
    if len(data) > tool.MAX_SKILL_FILE_BYTES:
        raise _too_large("A file is too big.")
    target, refusal = tool._resolve_supporting_file(skill_dir, path)
    if refusal:
        raise _invalid("Hermes refused a file path.", "unsupported_layout", path=path,
                       reason=_reason(refusal.get("error", "")))
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".tmp_")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _rollback(tool: Any, name: str, skill_dir: Path) -> None:
    """Delete the skill this import made. Falls back to removing its folder when Hermes' delete refuses."""
    try:
        if tool._delete_skill(name).get("success"):
            return
    except Exception as error:
        logger.warning("bighelp skill import: rollback by Hermes failed (%s)", type(error).__name__)
    shutil.rmtree(skill_dir, ignore_errors=True)
    try:
        parent = skill_dir.parent
        if parent != tool._skills_dir() and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    except Exception:
        pass


def _clear_prompt_cache() -> None:
    """Best effort: the next session lists the new skill, as after a skill made in the dashboard."""
    try:
        from hermes_cli.web_routers.skills import _clear_skills_prompt_cache
        _clear_skills_prompt_cache()
        return
    except Exception:
        pass
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass


def _install(tool: Any, skill_md: str, name: str, category: str | None,
             files: list[tuple[str, bytes, str | None]]) -> list[str]:
    """Create the skill and write its files. Returns the relative paths written, SKILL.md first.
    Any failure deletes what was made, then raises."""
    skill_dir = Path(tool._resolve_skill_dir(name, category))
    created = False
    try:
        result = tool._create_skill(name, skill_md, category)
        if not result.get("success"):
            raise _refused(result, writing=False)
        created = True
        skill_dir = Path(result.get("skill_md") or skill_dir / "SKILL.md").parent
        for path, _, text in files:
            if text is not None:
                outcome = tool._write_file(name, path, text)
                if not outcome.get("success"):
                    raise _refused(outcome, writing=True)
        binaries = [(path, data) for path, data, text in files if text is None]
        for path, data in binaries:
            _write_binary(tool, skill_dir, path, data)
        if binaries and (blocked := tool._security_scan_skill(skill_dir)):
            raise _refused({"error": blocked}, writing=True)
    except BaseException:
        if created:
            _rollback(tool, name, skill_dir)
        else:
            # A create that raised may have left its empty folder; anything with content isn't ours.
            try:
                skill_dir.rmdir()
            except OSError:
                pass
        raise
    finally:
        if created:
            _clear_prompt_cache()
    return ["SKILL.md", *(path for path, _, _ in files)]


def import_skill(agent_id: str, kind: str, data: str, category: str | None = None) -> dict:
    """Add the skill to the agent's profile. Raises `SkillImportError` for every refusal the app should show."""
    from tools import skill_manager_tool as tool
    with ExitStack() as stack:
        try:
            stack.enter_context(profile_scope(agent_id))
        except ValueError:
            raise SkillImportError(404, "profile_not_found", "The selected agent no longer exists.") from None
        if kind == "zip":
            skill = _read_archive(_decode_data(data, MAX_ZIP_BYTES), tool)
        else:
            skill = _Skill(_skill_text(_decode_data(data, tool.MAX_SKILL_CONTENT_CHARS * 4), tool), [])
        name, category = _check_skill(skill.skill_md, category, tool)
        files = _plan_files(skill.files, tool)
        lock = getattr(tool, "_skill_mutation_lock", None)
        with lock(name) if lock else nullcontext():
            if tool._find_skill(name):
                raise SkillImportError(409, "skill_exists", "This agent already has a skill with that name.",
                                       {"name": name})
            written = _install(tool, skill.skill_md, name, category, files)
    return {"name": name, "category": category, "files": written}
