"""Which channel an alert comes from, and whether a phone wants alerts from it.

A channel is where a person talks with an agent: bighelp itself, iMessage, Telegram, a terminal, and so on.
An ordinary turn's channel is its session's stored ``source``. A scheduled run has no person on its own
channel (Hermes runs it as ``cron``), so its channel is where the job delivers its result: ``local`` is the
bighelp app, ``imsg:+1…`` is iMessage, ``origin`` is the chat the job was made in. A run that also delivers
to every home channel (``all``) comes from every channel but bighelp.

Each phone keeps its own rules (``ChannelRules``): a choice for each channel it has seen, and one for every
other channel. bighelp itself is never filtered here; its own alert kinds have their own switches.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

BIGHELP = "bighelp"
TERMINAL = "terminal"
# Every channel but bighelp: a job that delivers to all home channels.
EVERY_OTHER = "*"
MAX_CHOICES = 64
_CHANNEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# bighelp's own sessions, and turns no person reads anywhere else (workflow stages, helpers, Kanban
# workers, Group Chat members), which bighelp shows. `loopdy` and `ios` are older bighelp builds' names.
_BIGHELP_SOURCES = frozenset({BIGHELP, "loopdy", "ios", "bot_room", "workflow", "subagent", "kanban",
                              "delegate", "worker"})
# Hermes' own terminal ways in: the CLI and TUI, one-shot runs, and `hermes chat -q` from a tool.
_TERMINAL_SOURCES = frozenset({"cli", "tui", "oneshot", "tool", "local", ""})
_SCHEDULED_SOURCES = frozenset({"cron"})
_NAMES = {
    BIGHELP: "bighelp", TERMINAL: "Terminal", "imsg": "iMessage", "bluebubbles": "BlueBubbles",
    "telegram": "Telegram", "discord": "Discord", "slack": "Slack", "whatsapp": "WhatsApp",
    "whatsapp_cloud": "WhatsApp", "signal": "Signal", "matrix": "Matrix", "mattermost": "Mattermost",
    "email": "Email", "sms": "SMS", "desktop": "Hermes Desktop", "webui": "Web UI", "api_server": "API server",
    "webhook": "Webhooks", "msgraph_webhook": "Microsoft 365", "homeassistant": "Home Assistant",
    "feishu": "Feishu", "dingtalk": "DingTalk", "wecom": "WeCom", "wecom_callback": "WeCom", "weixin": "WeChat",
    "qqbot": "QQ", "yuanbao": "Yuanbao", "relay": "Relay", "irc": "IRC", "teams": "Microsoft Teams",
    "google_chat": "Google Chat", "line": "LINE", "raft": "Raft",
}
_MAX_JOBS_BYTES = 8 * 1024 * 1024
# The last read of each agent's jobs file, by path, kept while the file is unchanged.
_JOBS_CACHE: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}


def is_channel(value: Any) -> bool:
    return isinstance(value, str) and _CHANNEL.fullmatch(value) is not None


def channel_of_source(source: Any) -> str | None:
    """The channel of a session's stored source; None for a scheduled run (its job decides) or a source
    that isn't a channel name."""
    value = source.strip().lower() if isinstance(source, str) else ""
    if value in _SCHEDULED_SOURCES:
        return None
    if value in _BIGHELP_SOURCES:
        return BIGHELP
    if value in _TERMINAL_SOURCES:
        return TERMINAL
    return value if is_channel(value) else None


def name(channel: str) -> str:
    """What the app shows for a channel. Plugin platforms Hermes has already loaded name themselves."""
    known = _NAMES.get(channel)
    if known:
        return known
    try:
        from gateway.platform_registry import platform_registry
        entry = platform_registry.get(channel)
        label = getattr(entry, "label", None)
        if isinstance(label, str) and 0 < len(label.strip()) <= 40:
            return label.strip()
    except Exception:  # Hermes without the registry, or one that can't say here.
        pass
    return " ".join(part.capitalize() for part in channel.replace("-", "_").split("_") if part) or channel


def _lane_parts(lane: Any) -> list[str]:
    if isinstance(lane, (list, tuple)):
        return [str(part).strip() for part in lane]
    return str(lane if lane not in (None, "") else "local").split(",")


def delivery_channels(job: dict[str, Any], *, failed: bool) -> frozenset[str]:
    """Where a scheduled job's run goes, as channels, by Hermes' own rules (cron/scheduler_delivery.py):
    ``local``, ``bot-chat`` and the retired ``loopdy`` inbox are bighelp; ``origin`` is the chat the job was
    made in. A failed run comes from both its job's lanes: Hermes sends its notice to ``failure_deliver``,
    and the run itself is where ``deliver`` keeps it."""
    parts = _lane_parts(job.get("deliver", "local"))
    lane = job.get("failure_deliver") if failed else None
    if lane is not None and str(lane).strip():
        parts += _lane_parts(lane)
    channels: set[str] = set()
    for raw in parts:
        part = raw.strip()
        if not part:
            continue
        platform = part.split(":", 1)[0].strip().lower()
        if platform in {"local", "loopdy", BIGHELP} or platform.startswith("bot-chat"):
            channels.add(BIGHELP)
        elif platform == "all":
            channels.add(EVERY_OTHER)
        elif platform == "origin":
            # Hermes skips an origin without a chat or on the API server and falls back to a home channel;
            # those, and a job without one, alert as bighelp rather than risk dropping one.
            origin = job.get("origin") if isinstance(job.get("origin"), dict) else {}
            found = channel_of_source(origin.get("platform")) if origin.get("chat_id") else None
            channels.add(found if found and found != "api_server" else BIGHELP)
        else:
            channels.add(channel_of_source(platform) or BIGHELP)
    return frozenset(channels or {BIGHELP})


def read_cron_jobs(profile: str) -> list[dict[str, Any]]:
    """The agent's scheduled jobs (``<agent home>/cron/jobs.json``), read only."""
    try:
        from hermes_cli.profiles import get_profile_dir
        path = Path(get_profile_dir(profile)) / "cron" / "jobs.json"
        info = path.stat()
        if info.st_size > _MAX_JOBS_BYTES:
            return []
        stamp = (info.st_mtime_ns, info.st_size)
        cached = _JOBS_CACHE.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # No such agent, no jobs file, or one that doesn't read.
        return []
    jobs = value.get("jobs") if isinstance(value, dict) else value
    found = [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []
    if len(_JOBS_CACHE) >= 64:
        _JOBS_CACHE.clear()
    _JOBS_CACHE[str(path)] = (stamp, found)
    return found


def scheduled_channels(profile: str, job_id: str | None, *, failed: bool,
                       jobs: Callable[[str], list[dict[str, Any]]] = read_cron_jobs) -> frozenset[str]:
    """A scheduled run's channels. A run whose job can't be found stays a bighelp alert, as before."""
    if job_id:
        for job in jobs(profile):
            if job.get("id") == job_id:
                return delivery_channels(job, failed=failed)
    return frozenset({BIGHELP})


class ChannelRules:
    """One phone's rules: ``choices`` for the channels it has seen, ``other`` for every other one."""

    __slots__ = ("other", "choices")

    def __init__(self, other: bool = True, choices: dict[str, bool] | None = None):
        self.other = other
        self.choices = dict(choices or {})

    @classmethod
    def validated(cls, value: Any) -> "ChannelRules":
        if not isinstance(value, dict) or set(value) != {"other", "choices"}:
            raise ValueError("channel rules need other and choices")
        other, choices = value["other"], value["choices"]
        if type(other) is not bool or not isinstance(choices, dict) or len(choices) > MAX_CHOICES:
            raise ValueError("channel rules are invalid")
        for channel, allowed in choices.items():
            # Only names an alert can carry: `cli` is `terminal`, `loopdy` is bighelp, `cron` is its job's.
            if (not is_channel(channel) or channel == BIGHELP or channel_of_source(channel) != channel
                    or type(allowed) is not bool):
                raise ValueError("channel rules are invalid")
        return cls(other, choices)

    @classmethod
    def stored(cls, raw: Any) -> "ChannelRules":
        try:
            return cls.validated(json.loads(raw)) if isinstance(raw, str) else cls()
        except ValueError:
            return cls()

    @property
    def is_default(self) -> bool:
        return self.other and all(self.choices.values())

    def public(self) -> dict[str, Any]:
        return {"other": self.other, "choices": dict(sorted(self.choices.items()))}

    def stored_json(self) -> str:
        return json.dumps(self.public(), separators=(",", ":"), sort_keys=True)

    def allows_one(self, channel: str) -> bool:
        if channel == BIGHELP:
            return True
        if channel == EVERY_OTHER:
            return self.other or any(self.choices.values())
        return self.choices.get(channel, self.other)

    def allows(self, channels: frozenset[str] | None) -> bool:
        """An alert from any allowed channel goes out. Unknown channels (None) aren't filtered."""
        return channels is None or any(self.allows_one(channel) for channel in channels)


def profile_channels(profile: str, *, opener: Callable, jobs: Callable[[str], list[dict[str, Any]]] = read_cron_jobs
                     ) -> list[dict[str, str]]:
    """Channels this agent has talked on or delivers to, for a phone's Channels list. bighelp isn't one."""
    found: set[str] = set()

    def read(db):
        counts = getattr(db, "session_count_by_source", None)
        if counts is None:
            return {}
        return counts(include_archived=True)
    try:
        for source in opener(profile, read, read_only=True) or {}:
            channel = channel_of_source(source)
            if channel:
                found.add(channel)
    except Exception:  # An agent without a session store still has its jobs.
        pass
    for job in jobs(profile):
        for failed in (False, True):
            found.update(delivery_channels(job, failed=failed))
    found -= {BIGHELP, EVERY_OTHER}
    return [{"id": channel, "name": name(channel)} for channel in sorted(found, key=lambda c: (name(c).lower(), c))]
