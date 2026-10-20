"""YAML that reads on every Hermes: older ones ship PyYAML, 0.22 and later only ruamel.yaml."""
from __future__ import annotations

from typing import Any


def safe_load(text: str) -> Any:
    """Plain data (dicts, lists, strings, numbers) from YAML text, as PyYAML's ``safe_load`` gives it."""
    try:
        import yaml
    except ImportError:
        from ruamel.yaml import YAML
        return YAML(typ="safe", pure=True).load(text)
    return yaml.safe_load(text)


def profile_name(meta: Any) -> str:
    """The name a person gave an agent, in the app's order: the bot title, then Hermes' display names."""
    if not isinstance(meta, dict):
        return ""
    ui_meta = meta.get("ui_meta") if isinstance(meta.get("ui_meta"), dict) else {}
    bots = ui_meta.get("hermes-bots") if isinstance(ui_meta.get("hermes-bots"), dict) else {}
    for value in (bots.get("title"), meta.get("display_name"), ui_meta.get("displayName")):
        if isinstance(value, str) and value.strip():
            return value
    return ""
