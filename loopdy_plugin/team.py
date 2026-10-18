"""Teamwork: every agent on this computer knows the others and asks them for help.

Turned on from the bighelp app (Settings › Teamwork). Hermes loads plugins per
agent, and most agents don't load this one, so a prompt section here would
reach only the default agent. Instead the team brief rides Hermes' own
per-agent setting, ``platform_hints.bighelp.append`` in each agent's
config.yaml, which Hermes adds to that agent's instructions in bighelp chats.

The plugin writes it for every agent, keeps the roster current as agents come
and go (each new chat reads it), and takes its own words back out when
teamwork is turned off. Anything else in that setting stays as it was.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Callable, Iterator

from .yaml_compat import profile_name, safe_load


CAPABILITY = "native-team-v1"
PLATFORM = "bighelp"
MAX_AGENTS = 16
MAX_NAME = 60
MAX_ROLE = 110
# Agents come and go rarely; a turn checks the roster at most this often.
REFRESH_SECONDS = 600.0
PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
logger = logging.getLogger("hermes.plugins.bighelp")


def _hermes() -> Any:
    """Hermes' profile and config helpers, as its dashboard config routes use them."""
    from types import SimpleNamespace
    from hermes_cli.config import load_config, save_config
    from hermes_cli.profiles import get_profile_dir, list_profile_names, profile_exists
    from hermes_cli.web_routers._common import config_write_scope
    return SimpleNamespace(load_config=load_config, save_config=save_config, profile_dir=get_profile_dir,
                           profile_names=list_profile_names, profile_exists=profile_exists,
                           write_scope=config_write_scope)


def available() -> bool:
    # Feeds /native/context, which every app reads: an odd Hermes turns Teamwork off, never the plugin.
    try:
        hermes = _hermes()
    except Exception:
        return False
    return all(callable(value) for value in vars(hermes).values())


def _state_path() -> Path:
    """The Hermes root, not a profile's home: one switch for the whole computer."""
    try:
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
    except ImportError:
        from hermes_constants import get_hermes_home
        root = get_hermes_home()
    return Path(root) / "plugin-data" / "loopdy" / "team.json"


def _clean(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    # Control characters and the prompt's own markup never reach another agent's instructions.
    text = "".join(character for character in text if character.isprintable()).replace("`", "'")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _yaml(path: Path) -> dict:
    """profile.yaml as a dict."""
    try:
        if not path.is_file() or path.stat().st_size > 65_536:
            return {}
        value = safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def roster(hermes: Any | None = None) -> list[dict[str, str]]:
    """Every agent here, the default first: its ID, name and role, as the app shows them."""
    hermes = hermes or _hermes()
    agents: list[dict[str, str]] = []
    for profile in hermes.profile_names():
        if not PROFILE_ID.fullmatch(str(profile)) or not hermes.profile_exists(profile):
            continue
        meta = _yaml(Path(hermes.profile_dir(profile)) / "profile.yaml")
        ui_meta = meta.get("ui_meta") if isinstance(meta.get("ui_meta"), dict) else {}
        bots = ui_meta.get("hermes-bots") if isinstance(ui_meta.get("hermes-bots"), dict) else {}
        name = (_clean(profile_name(meta), MAX_NAME)
                or " ".join(part.capitalize() for part in profile.replace("-", "_").split("_") if part))
        role = _clean(meta.get("description"), MAX_ROLE)
        about = _clean(bots.get("description"), MAX_ROLE)
        agents.append({"id": profile, "name": name, "role": role, "about": about})
    agents.sort(key=lambda agent: (agent["id"] != "default", agent["name"].lower()))
    return agents[:MAX_AGENTS]


def brief(agent_id: str, agents: list[dict[str, str]]) -> str:
    """What one agent is told about its team and how to work with it."""
    me = next((agent for agent in agents if agent["id"] == agent_id), None)
    teammates = [agent for agent in agents if agent["id"] != agent_id]
    if me is None or not teammates:
        return ""
    lines = []
    for agent in teammates:
        what = ". ".join(part for part in (agent["role"], agent["about"]) if part).rstrip(".")
        lines.append(f"- {agent['name']} (@{agent['id']})" + (f": {what}." if what else ""))
    signature = f"Message from {me['name']} (@{me['id']}):"
    return "\n".join([
        "## Your team",
        f"You're {me['name']} (@{me['id']}), one of several agents on this computer. Each has its own role, "
        "tools, accounts and memory. Your teammates:",
        *lines,
        "",
        "Work together like coworkers:",
        "- When part of a request fits a teammate's role, accounts or tools better than yours, ask them instead "
        "of guessing or saying you can't. When the person asks you to check with a teammate, do it.",
        f'- To ask, run in the terminal (allow a few minutes): hermes -p <id> chat -Q -q "{signature} <message>". '
        "Escape double quotes in the message. Their answer is the command's output. If you have the "
        "message_agent tool, you can use it instead.",
        "- Say exactly what you need back, and share only what they need; keep the person's private details "
        "private.",
        "- Ask several teammates at once when the questions don't depend on each other. Then answer the person "
        "once, with what you learned and who helped.",
        "- Never send a task back to the teammate who asked you, or pass it around. If nobody fits better, do "
        "it yourself.",
        "- The person sees these conversations in bighelp.",
    ])


class TeamStore:
    """The switch and what the plugin last wrote to each agent, in one small file."""

    def __init__(self, path: Callable[[], Path] = _state_path):
        self._path = path
        self._lock = threading.RLock()

    def read(self) -> dict:
        with self._lock:
            try:
                value = json.loads(self._path().read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {"enabled": False, "written": {}, "signature": ""}
            if not isinstance(value, dict):
                return {"enabled": False, "written": {}, "signature": ""}
            written = value.get("written") if isinstance(value.get("written"), dict) else {}
            return {"enabled": value.get("enabled") is True,
                    "written": {key: text for key, text in written.items()
                                if isinstance(key, str) and isinstance(text, str)},
                    "signature": value.get("signature") if isinstance(value.get("signature"), str) else ""}

    def write(self, state: dict) -> None:
        with self._lock:
            path = self._path()
            path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".team-", suffix=".json")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    json.dump(state, file, ensure_ascii=False, sort_keys=True)
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise


@contextmanager
def _profile(hermes: Any, profile: str) -> Iterator[None]:
    with hermes.write_scope(profile):
        yield


def _write_hint(hermes: Any, profile: str, previous: str, new: str) -> None:
    """Replaces this plugin's words in the agent's bighelp hint, leaving the rest as it was."""
    with _profile(hermes, profile):
        config = hermes.load_config()
        hints = config.get("platform_hints")
        hints = dict(hints) if isinstance(hints, dict) else {}
        spec = hints.get(PLATFORM)
        spec = {"append": spec} if isinstance(spec, str) else dict(spec) if isinstance(spec, dict) else {}
        current = spec.get("append") if isinstance(spec.get("append"), str) else ""
        base = current.replace(previous, "") if previous else current
        text = "\n\n".join(part for part in (base.strip(), new) if part)
        if text == current.strip():
            return
        if text:
            spec["append"] = text
        else:
            spec.pop("append", None)
        if spec:
            hints[PLATFORM] = spec
        else:
            hints.pop(PLATFORM, None)
        config["platform_hints"] = hints
        hermes.save_config(config)


def _signature(agents: list[dict[str, str]]) -> str:
    return hashlib.sha256(json.dumps(agents, sort_keys=True).encode("utf-8")).hexdigest()


class Team:
    def __init__(self, store: TeamStore | None = None, hermes: Callable[[], Any] = _hermes,
                 monotonic: Callable[[], float] = time.monotonic):
        self.store = store or TeamStore()
        self._hermes = hermes
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._checked_at: float | None = None

    def status(self) -> dict:
        """Where teamwork stands, bringing every agent's brief up to date first."""
        return self.set_enabled(None)

    def set_enabled(self, enabled: bool | None) -> dict:
        hermes = self._hermes()
        with self._lock:
            state = self.store.read()
            if enabled is not None:
                state["enabled"] = enabled
            agents = roster(hermes)
            signature = _signature(agents)
            failed = self._apply(hermes, state, agents)
            state["signature"] = signature if not failed else ""
            self.store.write(state)
            self._checked_at = self._monotonic()
        return {
            "enabled": state["enabled"],
            "agents": [{"id": agent["id"], "name": agent["name"], "role": agent["role"]} for agent in agents],
            "reached": sorted(state["written"]) if state["enabled"] else [],
            "failed": failed,
        }

    def refresh_if_due(self) -> None:
        """From a turn: at most every ten minutes, and only when the roster changed."""
        now = self._monotonic()
        if self._checked_at is not None and now - self._checked_at < REFRESH_SECONDS:
            return
        if not self._lock.acquire(blocking=False):
            return
        try:
            self._checked_at = now
            state = self.store.read()
            if not state["enabled"]:
                return
            hermes = self._hermes()
            agents = roster(hermes)
            signature = _signature(agents)
            if signature == state["signature"] and set(state["written"]) == {agent["id"] for agent in agents}:
                return
            failed = self._apply(hermes, state, agents)
            state["signature"] = signature if not failed else ""
            self.store.write(state)
        finally:
            self._lock.release()

    def _apply(self, hermes: Any, state: dict, agents: list[dict[str, str]]) -> list[str]:
        written: dict[str, str] = state["written"]
        failed: list[str] = []
        current = {agent["id"] for agent in agents}
        # Agents removed since: their words go with them (a deleted profile has no config to clean).
        for profile in list(written):
            if profile not in current:
                written.pop(profile)
        for agent in agents:
            profile = agent["id"]
            new = brief(profile, agents) if state["enabled"] else ""
            previous = written.get(profile, "")
            if new == previous:
                continue
            try:
                _write_hint(hermes, profile, previous, new)
            except Exception as error:
                # Never log config text or paths; the kind of failure is enough.
                logger.warning("bighelp teamwork brief not saved: %s", type(error).__name__)
                failed.append(profile)
                continue
            if new:
                written[profile] = new
            else:
                written.pop(profile, None)
        return failed


def _shared_team() -> Team:
    # One dashboard process imports this module twice (see AGENTS.md); one Team is enough.
    import sys
    return sys.modules.setdefault("_loopdy_team", Team())  # type: ignore[return-value]


def team() -> Team:
    return _shared_team()


def _pre_llm_call(**payload: Any) -> None:
    if str(payload.get("platform") or "").strip().lower() != PLATFORM:
        return
    try:
        team().refresh_if_due()
    except Exception as error:  # a stale roster must never fail a turn
        logger.debug("bighelp teamwork refresh skipped: %s", type(error).__name__)


def register(ctx: Any) -> None:
    # Every Hermes process loads the plugin, so this imports nothing from the dashboard. Until Teamwork is on,
    # the hook only reads one small file every ten minutes.
    ctx.register_hook("pre_llm_call", _pre_llm_call)
