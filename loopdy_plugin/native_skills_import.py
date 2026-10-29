"""Skill import: a skill the person picked in the app (a .zip folder or one SKILL.md), added to one agent.

Stock Hermes can create a skill from SKILL.md text, but has no way to take a skill folder with its `scripts/`,
`references/`, `templates/` and `assets/`. This module checks an uploaded archive in full before it writes
anything, then writes the skill with Hermes' own writers (`tools.skill_manager_tool`), in the agent's own
profile. It never overwrites a skill, and when any step fails it deletes what it made, so nothing half-made
stays. Hermes' validators decide what a skill may hold; this module adds only the archive rules.

An upload is outside content, like a hub install. So after the last file is written, Hermes' skill scanner
(`tools.skills_guard`) runs once on the whole skill with the install policy of a community source, whether or
not the host turned on `skills.guard_agent_created`. A skill the policy blocks is deleted and refused.
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
import unicodedata
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
# Bytes, not characters: with 200 files this keeps the answer far under the 64 KiB the app reads back.
MAX_PATH_BYTES = 240
# The source name Hermes' scanner sees. Anything but its own trusted ids is a community source.
SCAN_SOURCE = "bighelp-upload"
logger = logging.getLogger("hermes.plugins.bighelp")

# Hermes' own functions and limits this module relies on.
_TOOL_NAMES = ("_validate_name", "_validate_category", "_validate_frontmatter", "_validate_file_path",
               "_resolve_supporting_file", "_create_skill", "_write_file", "_delete_skill", "_find_skill",
               "_resolve_skill_dir", "_security_scan_skill", "_parse_frontmatter")
_UNIX = 3
_SCAN_NAMES = ("scan_skill", "should_allow_install")
# A double quote is left out too, so a path never grows when it is written as JSON.
_UNSAFE_PATH = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029\\\"]")
_DRIVE = re.compile(r"[A-Za-z]:")
# An absolute path inside a Hermes message (two or more segments), for scrubbing before it reaches the app.
# Folders in the middle may hold spaces and apostrophes ("/Volumes/Gordie's Codebox/..."); the last name ends
# at the first space, since the text after a path is a sentence again.
_MIDDLE = r"[^/\"():;<>]*"
_LAST = r"[^\s'\"():;,<>]*"
_HOST_PATH = re.compile(r"(?<![\w/.])/(?:" + _MIDDLE + r"/)+" + _LAST)


class SkillImportError(Exception):
    """An import the route refuses with a fixed code and a plain message; `details` are safe facts."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        self.status, self.code, self.message, self.details = status, code, message, details or {}
        super().__init__(message)


def available() -> bool:
    """Only when Hermes' skill writers and scanner import (they are internals, so an odd Hermes turns this off)."""
    try:
        from tools import skill_manager_tool as tool
        from tools import skills_guard as guard
        return (all(callable(getattr(tool, name, None)) for name in _TOOL_NAMES)
                and all(callable(getattr(guard, name, None)) for name in _SCAN_NAMES)
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


def _refusal(problem: Any, what: str = "Hermes can't take this skill") -> SkillImportError:
    """`invalid_skill` with Hermes' reason in the message too, because the app shows only the message."""
    reason = _reason(problem)
    return _invalid(f"{what}: {reason}", "invalid_skill", reason=reason)


def _folders(path: str) -> list[str]:
    parts = path.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def _same_file(path: str) -> str:
    """One key for names a disk treats as one file: Unicode-equivalent (NFC and NFD) and case-folded."""
    return unicodedata.normalize("NFC", path).casefold()


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
    if (_UNSAFE_PATH.search(name) or name.startswith("/") or _DRIVE.match(name) or len(name.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES
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
            # Folded, so one name can't be written twice on a disk that treats case or Unicode forms as equal.
            key = _same_file(info.filename)
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
    # A path that is a file and also a folder of another file can't be written.
    folders = {folder for path, _ in files for folder in _folders(_same_file(path))}
    for path, _ in files:
        if _same_file(path) in folders:
            raise _invalid("A file has the same name as a folder.", "unsupported_layout", path=path)
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
            raise _refusal(problem)
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
        raise _refusal(problem, "The skill's name isn't allowed")
    # Hermes skips these folder names in every skill lookup, so a skill in one could never be read back.
    hidden = _hidden_names()
    for label, value in (("name", name), ("category", category)):
        if value in hidden:
            raise _invalid(f"The skill {label} '{value}' can't be used: Hermes hides folders with that name.",
                           "invalid_skill", reason=f"{label} is a hidden folder name")
    return name, category


def _hidden_names() -> frozenset:
    try:
        from agent.skill_utils import EXCLUDED_SKILL_DIRS
        return frozenset(EXCLUDED_SKILL_DIRS)
    except Exception:
        return frozenset()


def _blocked(reason: str, findings: list[dict] | None = None) -> SkillImportError:
    reason = _reason(reason)
    details: dict[str, Any] = {"reason": reason}
    if findings:
        details["findings"] = findings
    return SkillImportError(422, "blocked", f"Hermes' security scan blocked this skill: {reason}", details)


def _scan(skill_dir: Path) -> dict:
    """Hermes' scanner on the finished skill, with the install policy of a community source (as a hub
    install). Runs whether or not the host turned on `skills.guard_agent_created`. Raises `blocked` when the
    policy says block; a scan that itself fails also fails the import, so nothing unchecked stays."""
    from tools.skills_guard import scan_skill, should_allow_install
    result = scan_skill(skill_dir, source=SCAN_SOURCE)
    allowed, _ = should_allow_install(result, force=False)
    if allowed is False:
        findings = [{"severity": str(f.severity)[:20], "file": _reason(f.file)[:120],
                     "description": _reason(f.description)[:160]} for f in result.findings[:5]]
        raise _blocked(result.summary, findings)
    # `allowed` is None when the policy says "ask"; an upload goes ahead and the app can show the verdict.
    return {"verdict": result.verdict, "policy": "allow" if allowed else "ask"}


def _refused(result: Any, *, writing: bool) -> SkillImportError:
    """Hermes turned a write down: a blocked scan, a skill that appeared meanwhile, or another rule."""
    message = str(result.get("error", "")) if isinstance(result, dict) else ""
    if message.startswith("Security scan blocked"):
        return _blocked(message.split("\n", 1)[0].removeprefix("Security scan blocked this skill").strip(" :") or
                        "it has content that isn't allowed")
    if "already exists" in message and not writing:
        return SkillImportError(409, "skill_exists", "This agent already has a skill with that name.")
    return _refusal(message or "no reason given", "Hermes refused this skill")


def _write_binary(tool: Any, skill_dir: Path, path: str, data: bytes) -> None:
    """A file that isn't text, with the same path checks and size cap as Hermes' text writer."""
    if len(data) > tool.MAX_SKILL_FILE_BYTES:
        raise _too_large("A file is too big.")
    target, refusal = tool._resolve_supporting_file(skill_dir, path)
    if refusal:
        reason = _reason(refusal.get("error", ""))
        raise _invalid(f"Hermes refused a file path: {reason}", "unsupported_layout", path=path, reason=reason)
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
             files: list[tuple[str, bytes, str | None]]) -> tuple[list[str], dict]:
    """Create the skill and write its files, then scan it. Returns the relative paths written, SKILL.md
    first, and the scan's verdict. Any failure deletes what was made, then raises."""
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
        scan = _scan(skill_dir)
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
    return ["SKILL.md", *(path for path, _, _ in files)], scan


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
            written, scan = _install(tool, skill.skill_md, name, category, files)
    return {"name": name, "category": category, "files": written, "scan": scan}
