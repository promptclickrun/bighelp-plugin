"""Agents share the default agent's plugins instead of carrying copies of them.

Hermes copies the source agent's ``plugins/`` into every agent cloned from it (``_clone_plugins``), and the new
agent turns the same plugins on. bighelp makes agents that way: New agent and Simple Bot's Create clone the
default agent. Hermes puts every agent's plugins into one shared Python environment, one member per real plugin
folder. A plugin that ships its own buildable package is then there twice under one name, and uv refuses every
later plugin install on the host, the bighelp plugin's own updates included: "Two workspace members are both
named …". A link resolves to the default agent's folder, so Hermes sees one package, and an agent cloned from
this one keeps the link.

An agent's plugin folder becomes a link to the default agent's only when both install records name the same
source and revision and the files are the same. An agent's own plugins, other versions and local edits are left
alone and reported.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

logger = logging.getLogger(__name__)

CAPABILITY = "native-agent-plugin-links-v1"
MAXIMUM_FILES = 20_000
MAXIMUM_BYTES = 512 * 1024 * 1024
_METADATA = ".install-metadata.json"
_METADATA_BYTES = 1024 * 1024
_LOCK_SECONDS = 10.0
# What differs between two copies of one install without being the plugin: Git's own bookkeeping (fetches since
# the clone) and Python's caches.
_SKIPPED_FOLDERS = frozenset({".git", "__pycache__"})
_SKIPPED_FILES = frozenset({".DS_Store"})
_LISTED = 100

# One dashboard process imports this module twice (see AGENTS.md); one record of the last pass is enough.
_state = sys.modules.setdefault(
    "_loopdy_plugin_links", SimpleNamespace(lock=threading.Lock(), seen=None, running=False))  # type: ignore[arg-type]


class _TooLarge(Exception):
    pass


def available() -> bool:
    if os.name == "nt":  # Hermes copies junctions there; plugin folders stay as they are
        return False
    try:
        import fcntl  # noqa: F401
        from hermes_cli.profiles import get_profile_dir, list_profile_names  # noqa: F401
    except Exception:
        return False
    return True


def link_agent(profile_id: str) -> dict:
    """Links an agent's exact copies of the default agent's plugins. The default agent has nothing to link."""
    from hermes_cli.profiles import get_profile_dir, normalize_profile_name
    if normalize_profile_name(profile_id) == "default":
        return {"linked": [], "kept": []}
    return link_plugin_copies(get_profile_dir("default") / "plugins", get_profile_dir(profile_id) / "plugins")


def link_every_agent() -> dict:
    from hermes_cli.profiles import list_profile_names
    linked = kept = failed = 0
    for name in list_profile_names():
        try:
            result = link_agent(name)
        except Exception:
            failed += 1
            continue
        linked += len(result["linked"])
        kept += len(result["kept"])
    return {"linked": linked, "kept": kept, "failed": failed}


def refresh_in_background() -> None:
    """From each app connection: a pass over every agent when the set of agents changed since the last one in
    this process (first: every agent this host already has, then each one Hermes Desktop or the CLI adds)."""
    if not available():
        return
    try:
        from hermes_cli.profiles import list_profile_names
        names = tuple(sorted(list_profile_names()))
    except Exception:
        return
    with _state.lock:
        if _state.running or _state.seen == names:
            return
        _state.running, _state.seen = True, names
    threading.Thread(target=_pass, name="loopdy-plugin-links", daemon=True).start()


def _pass() -> None:
    try:
        result = link_every_agent()
        if result["linked"] or result["failed"]:
            logger.info("bighelp plugin links: %d copies linked, %d kept, %d agents unreadable",
                        result["linked"], result["kept"], result["failed"])
    except Exception:
        logger.warning("bighelp plugin links unavailable")
    finally:
        with _state.lock:
            _state.running = False


def link_plugin_copies(source_root: Path, agent_root: Path) -> dict:
    """``{"linked": [names], "kept": [{"name", "reason"}]}`` for one agent's ``plugins/``. Reasons:
    ``not_installed`` (no install record to match), ``different_install``, ``changed``, ``too_large``, ``error``."""
    linked: list[str] = []
    kept: list[dict[str, str]] = []
    if not agent_root.is_dir() or not source_root.is_dir():
        return {"linked": linked, "kept": kept}
    candidates = [entry for entry in sorted(agent_root.iterdir(), key=lambda path: path.name)
                  if not entry.name.startswith(".") and not entry.is_symlink() and entry.is_dir()
                  and (source_root / entry.name).is_dir() and entry.resolve() != (source_root / entry.name).resolve()]
    if not candidates:
        return {"linked": linked, "kept": kept}
    with _install_lock(agent_root):
        source_records, agent_records = _records(source_root), _records(agent_root)
        for copy in candidates:
            reason = _reason_to_keep(copy, source_root / copy.name, source_records, agent_records)
            if reason is None:
                try:
                    _swap(copy, source_root / copy.name)
                except OSError:
                    reason = "error"
                    logger.warning("bighelp plugin link not made: swap")
            if reason is None:
                linked.append(copy.name)
            else:
                kept.append({"name": copy.name, "reason": reason})
    return {"linked": linked[:_LISTED], "kept": kept[:_LISTED]}


def _reason_to_keep(copy: Path, source: Path, source_records: dict, agent_records: dict) -> str | None:
    mine, theirs = agent_records.get(copy.name), source_records.get(copy.name)
    if not _is_record(mine) or not _is_record(theirs):
        return "not_installed"
    if (mine["source"], mine["revision"]) != (theirs["source"], theirs["revision"]):
        return "different_install"
    try:
        return None if _fingerprint(copy) == _fingerprint(source) else "changed"
    except _TooLarge:
        return "too_large"
    except OSError:
        return "error"


def _is_record(record: Any) -> bool:
    return (isinstance(record, dict) and isinstance(record.get("source"), str) and record["source"]
            and isinstance(record.get("revision"), str) and record["revision"])


def _records(plugins: Path) -> dict:
    path = plugins / _METADATA
    try:
        if path.stat().st_size > _METADATA_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _fingerprint(root: Path) -> dict[str, tuple]:
    """Each file's size and SHA-256 and each link's target, by relative path."""
    entries: dict[str, tuple] = {}
    total = 0
    for directory, folders, files in os.walk(root, followlinks=False):
        here = Path(directory)
        folders[:] = sorted(name for name in folders if name not in _SKIPPED_FOLDERS)
        for name in folders:
            if (here / name).is_symlink():
                entries[str((here / name).relative_to(root))] = ("link", os.readlink(here / name))
        for name in sorted(files):
            if name in _SKIPPED_FILES or name.endswith(".pyc"):
                continue
            path = here / name
            relative = str(path.relative_to(root))
            if len(entries) >= MAXIMUM_FILES:
                raise _TooLarge
            if path.is_symlink():
                entries[relative] = ("link", os.readlink(path))
                continue
            if not path.is_file():
                entries[relative] = ("special",)
                continue
            size = path.stat().st_size
            total += size
            if total > MAXIMUM_BYTES:
                raise _TooLarge
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            entries[relative] = ("file", size, digest.hexdigest())
    return entries


def _swap(copy: Path, source: Path) -> None:
    """The copy's place goes to a link; on any failure the copy is put back."""
    token = secrets.token_hex(4)
    link = copy.with_name(f".bighelp-link-{copy.name}-{token}")
    old = copy.with_name(f".bighelp-old-{copy.name}-{token}")
    os.symlink(str(source), link, target_is_directory=True)
    try:
        os.rename(copy, old)
    except OSError:
        link.unlink(missing_ok=True)
        raise
    try:
        os.rename(link, copy)
    except OSError:
        os.rename(old, copy)
        link.unlink(missing_ok=True)
        raise
    shutil.rmtree(old, ignore_errors=True)


@contextlib.contextmanager
def _install_lock(plugins: Path) -> Iterator[None]:
    """Hermes' plugin installer takes an flock on ``.install-metadata.json.lock`` (hermes_cli.plugins_cmd_git);
    holding it keeps an install in this agent from running mid-swap."""
    import fcntl
    with (plugins / f"{_METADATA}.lock").open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + _LOCK_SECONDS
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("plugin install lock busy") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
