"""The voices (and models) a speech plugin offers an agent, for the app's voice picker.

Hermes lets a plugin add a text-to-speech provider (``register_tts_provider``; Phonon is one). Its voices are
known only to the plugin (``list_voices()``, ``list_models()``, ``default_voice()``), so the app can't show
them without asking here. The lookup follows Hermes' own TTS dispatch, in the agent's own profile: built-in
providers and a ``type: command`` provider of the same name never reach a plugin, and only a plugin the agent
loads answers. Anything else answers empty lists, never an error.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import logging
from typing import Any

from .workspace_capabilities import profile_scope

CAPABILITY = "native-voice-catalog-v1"
MAX_VOICES = 500
MAX_MODELS = 50
MAX_TEXT_BYTES = 160
# Well inside the app's 128 KiB answer, whatever a plugin lists.
MAX_LIST_BYTES = 120_000
logger = logging.getLogger("hermes.plugins.bighelp")


class VoiceCatalogError(Exception):
    """The plugin's own code failed while listing; the route answers a fixed code."""


def available() -> bool:
    try:
        from agent.tts_registry import get_provider  # noqa: F401
        from hermes_cli.config import load_config_readonly  # noqa: F401
        from tools.tts_tool_plugins import _lookup_plugin_provider, _plugin_route_key  # noqa: F401
    except Exception:
        return False
    return True


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip() or any(ord(character) < 32 or ord(character) == 127
                                                              for character in value):
        return None
    try:
        return value if len(value.encode("utf-8")) <= MAX_TEXT_BYTES else None
    except UnicodeEncodeError:
        return None


def _name(value: Any, fallback: str) -> str:
    # A long display name is cut, never the ID it names.
    if not isinstance(value, str):
        return fallback
    return _text(value.encode("utf-8", "ignore")[:MAX_TEXT_BYTES].decode("utf-8", "ignore")) or fallback


def _entries(rows: Any, limit: int, budget: list[int], *, language: bool) -> list[dict]:
    """At most ``limit`` well-formed entries with unique IDs, within the shared byte ``budget``."""
    result, seen = [], set()
    for row in rows if isinstance(rows, (list, tuple)) else ():
        if len(result) >= limit:
            break
        if not isinstance(row, dict):
            continue
        identifier = _text(row.get("id"))
        if identifier is None or identifier in seen:
            continue
        entry = {"id": identifier, "name": _name(row.get("display") or row.get("name"), identifier)}
        spoken = _text(row.get("language")) if language else None
        if spoken is not None:
            entry["language"] = spoken
        size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8")) + 1
        if size > budget[0]:
            break
        budget[0] -= size
        seen.add(identifier)
        result.append(entry)
    return result


def _plugin_provider(name: str):
    """The agent's plugin provider for ``name``, by Hermes' own dispatch rules, or None."""
    from hermes_cli.config import load_config_readonly
    from tools.tts_tool_plugins import _lookup_plugin_provider, _plugin_route_key
    tts = (load_config_readonly() or {}).get("tts")
    key = _plugin_route_key(name, tts if isinstance(tts, dict) else {})
    return None if key is None else _lookup_plugin_provider(key)


def catalog(profile: str, provider: str) -> dict:
    """What ``provider`` offers ``profile``. ``LookupError``: no such agent; ``VoiceCatalogError``: the plugin
    failed while listing."""
    name = provider.strip().lower()
    value = {"provider": name, "voices": [], "models": []}
    with ExitStack() as stack:
        try:
            stack.enter_context(profile_scope(profile))
        except ValueError:
            raise LookupError(profile) from None
        try:
            plugin = _plugin_provider(name)
            if plugin is None:
                return value
            voices, models, default = plugin.list_voices(), plugin.list_models(), plugin.default_voice()
        except Exception as error:  # the plugin's own code: its text can carry paths or keys
            logger.warning("bighelp voice catalog failed: %s", type(error).__name__)
            raise VoiceCatalogError() from None
    budget = [MAX_LIST_BYTES]
    value["models"] = _entries(models, MAX_MODELS, budget, language=False)
    value["voices"] = _entries(voices, MAX_VOICES, budget, language=True)
    if (default := _text(default)) is not None:
        value["defaultVoice"] = default
    return value
