"""Host telemetry: a small, anonymous usage stream from this Hermes host (``native-host-telemetry-v1``).

It is on by default and has an off switch (see ``docs/HOST_TELEMETRY.md``). It sends only numbers,
codes and values from fixed lists: how long turns and tool calls take, which built-in tool ran, how
alerts fared, and a few facts about the install. No chat, prompt, tool argument, file name, path,
profile name, user name, host name, email or address is ever read into an event. Every emitter takes
typed values and checks them against the allowlists below before anything is queued, so there is no
way for free text to reach a batch.

Events wait in a bounded in-memory queue (the oldest drop first). A daemon thread posts them every 60
seconds or 50 events. A failed post is dropped without a word and a 429 backs off. Nothing here
blocks a turn or raises into Hermes.

One process sends one stream, though a dashboard imports this package twice and Hermes loads it
again for each profile ("How the plugin loads" in AGENTS.md). All state lives in one
``sys.modules`` entry, made once with ``setdefault``, and holds only plain containers, locks and
callables. The sender thread, the queue and the turn timers are therefore shared by every copy.
"""
from __future__ import annotations

import atexit
import functools
import json
import logging
import os
import re
import sys
import threading
import time
import types
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import naming
from .version import PLUGIN_VERSION

CAPABILITY = "native-host-telemetry-v1"
ENDPOINT = "https://telemetry.bighelp.app/v1/batch"
DOC_PATH = "docs/HOST_TELEMETRY.md"
DOC_URL = "https://github.com/promptclickrun/bighelp-plugin/blob/main/" + DOC_PATH

FLUSH_SECONDS = 60
START_DELAY_SECONDS = 30
BATCH_EVENTS = 50
MAX_QUEUE = 500
TIMEOUT_SECONDS = 5
EXIT_TIMEOUT_SECONDS = 3
_POSTS_PER_FLUSH = 3
_SETTINGS_TTL = 5.0
_MAX_BACKOFF = 3_600
_DEFAULT_BACKOFF = 120
_ERROR_REPEAT_SECONDS = 60
_MAX_TURNS = 256
_TURN_LIFETIME = 3_600
_MAX_SEEN_TOOLS = 512
_MAX_CONFIG_BYTES = 1_000_000

logger = logging.getLogger("hermes.plugins.bighelp.telemetry")

# ---------------------------------------------------------------- allowlists (mirror the ingestion contract)

MAX_MS = 86_400_000
MAX_COUNT = 100_000
_CODE = re.compile(r"[a-z0-9_]{1,64}\Z")
_VERSION = re.compile(r"[0-9]{1,4}(\.[0-9]{1,4}){0,3}\Z")
_HERMES_VERSION = re.compile(r"[0-9]{1,4}(\.[0-9]{1,4}){0,3}([.+-][0-9A-Za-z]{1,16})?\Z")
_PYTHON = re.compile(r"3\.[0-9]{1,2}\Z")
_TOOL = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_PROVIDER = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
_MODEL = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,63}\Z")
_PROFILE_DIR = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_UUID4 = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")

ALERT_EVENT_TYPES = frozenset({
    "session.completed", "session.failed", "scheduled.completed", "scheduled.failed",
    "approval.required", "clarification.required", "subagent.completed", "subagent.failed",
    "wake", "liveactivity",
})
OUTCOMES = frozenset({"ok", "failed", "cancelled"})
TOOL_OUTCOMES = frozenset({"ok", "failed"})
SESSION_SOURCES = frozenset({
    "bighelp", "cli", "cron", "api", "telegram", "discord", "slack", "whatsapp", "signal",
    "email", "webhook", "other",
})
_SOURCE_ALIASES = {"loopdy": "bighelp", "tui": "cli", "desktop": "cli", "acp": "cli", "api_server": "api"}
OS_FAMILIES = frozenset({"linux", "macos", "windows", "other"})
AGENT_COUNTS = frozenset({"1", "2-5", "6-20", "21+"})
# Where an error can come from. A code is accepted from these places only.
ERROR_STAGES = frozenset({"native_route", "notification_send"})
EVENT_NAMES = frozenset({
    "host.started", "turn.completed", "tool.completed", "alert.queued", "alert.sent", "error",
})

# Providers whose model IDs are their own public catalog: the model is reported with the provider.
_PUBLIC_MODEL_PROVIDERS = frozenset({
    "anthropic", "openai", "openai-api", "openai-codex", "gemini", "google", "openrouter", "nous",
    "deepseek", "xai", "zai", "kimi-coding", "minimax", "mistral", "groq", "copilot", "perplexity",
    "cohere", "opencode-zen", "opencode-go", "ai-gateway", "stepfun",
})
# Public services where a model ID can name a person's own fine-tune, deployment or account: only
# the provider is reported, and the model is "custom".
_PUBLIC_PROVIDERS = _PUBLIC_MODEL_PROVIDERS | frozenset({
    "huggingface", "together", "fireworks", "deepinfra", "nvidia", "bedrock", "vertex", "alibaba",
    "qwen-oauth", "kilocode", "nebius",
})
_PRIVATE_MODEL_MARKERS = ("ft:", "arn:", "projects/", "endpoints/", "deployments/", "accounts/", "::")

# Hermes' built-in tools (its toolsets, past and present). Anything else is "mcp" or "custom".
_HERMES_TOOLS = frozenset("""
annotate_preview browser_back browser_cdp browser_click browser_close browser_console browser_dialog
browser_exec browser_get_images browser_navigate browser_press browser_scroll browser_snapshot
browser_type browser_vault browser_vision clarify close_terminal computer_use cronjob cronjob_manage
delegate_task desktop_preview desktop_project discord discord_admin drive_preview execute_code
feishu_doc_read feishu_drive_add_comment feishu_drive_list_comment_replies feishu_drive_list_comments
feishu_drive_reply_comment focus_pane gui_tour ha_call_service ha_get_state ha_list_entities
ha_list_services image_generate kanban_attach kanban_attach_url kanban_attachments kanban_block
kanban_comment kanban_complete kanban_create kanban_heartbeat kanban_link kanban_list
kanban_request_changes kanban_request_review kanban_show kanban_unblock manage_catalog
manage_connections memory mixture_of_agents patch process process_manage read_file read_terminal
read_window_below search_files send_message session_search set_theme setup_beat show_tip skill_manage
skill_view skills_list terminal text_to_speech todo todo_list tool_batch tool_call tool_describe
tool_search video_analyze video_generate vision_analyze web_extract web_search write_file x_search
xai_video_edit xai_video_extend yb_query_group_info yb_query_group_members yb_search_sticker yb_send_dm
yb_send_sticker
""".split())
# Tools this plugin registers (plugin.yaml provides_tools).
_BIGHELP_TOOLS = frozenset("""
iphone_health iphone_calendar iphone_reminders iphone_location iphone_appearance iphone_contacts
iphone_photos iphone_geofences iphone_alarms iphone_music iphone_focus iphone_shortcuts
bighelp_render_summary bighelp_render_metrics bighelp_render_list bighelp_render_timeline
bighelp_render_weather_forecast bighelp_render_sports_game bighelp_render_stock_quote
bighelp_render_chart bighelp_render_dashboard bighelp_render_form bighelp_await_form_response
bighelp_render_card bighelp_render_checklist bighelp_render_selection bighelp_render_automation
bighelp_search_card_templates bighelp_get_card_template bighelp_render_card_template
bighelp_save_ui_template bighelp_react_to_message bighelp_request_secure_input bighelp_board
bighelp_people bighelp_templates_search bighelp_templates_get bighelp_templates_fill
bighelp_templates_create_agent bighelp_workflows bighelp_catalog_login bighelp_submit_catalog_template
""".split())
_KNOWN_TOOLS = _HERMES_TOOLS | _BIGHELP_TOOLS | {"mcp", "custom"}


def _number(value: Any, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = int(value)
    except (ValueError, OverflowError):
        return None
    return number if 0 <= number <= maximum else None


def _pattern(value: Any, pattern: re.Pattern) -> str | None:
    return value if isinstance(value, str) and pattern.match(value) else None


def classify_tool(name: Any) -> str:
    """A built-in Hermes or bighelp tool by name; "mcp" for an MCP server's; otherwise "custom"."""
    if not isinstance(name, str):
        return "custom"
    if name in _KNOWN_TOOLS:
        return name
    if name.startswith("mcp_") or name.startswith("mcp__"):
        return "mcp"
    return "custom"


def classify_source(value: Any) -> str:
    """The session source as one of the fixed names."""
    if not isinstance(value, str):
        return "other"
    key = value.strip().lower()
    key = _SOURCE_ALIASES.get(key, key)
    return key if key in SESSION_SOURCES else "other"


def classify_route(provider: Any, model: Any) -> tuple[str | None, str | None]:
    """(provider, model) safe to send: a known public provider by name, else "custom".

    A model is reported only for a provider whose model IDs are a public catalog. Nothing is
    reported when Hermes didn't say which provider answered."""
    if not isinstance(provider, str) or not provider.strip():
        return None, None
    key = provider.strip().lower()
    if key not in _PUBLIC_PROVIDERS:
        return "custom", "custom"
    if key not in _PUBLIC_MODEL_PROVIDERS or not isinstance(model, str):
        return key, "custom"
    candidate = model.strip().lower()
    if (_MODEL.match(candidate) is None or candidate.count("/") > 1
            or any(marker in candidate for marker in _PRIVATE_MODEL_MARKERS)):
        return key, "custom"
    return key, candidate


def agent_bucket(count: int) -> str:
    if count <= 1:
        return "1"
    if count <= 5:
        return "2-5"
    if count <= 20:
        return "6-20"
    return "21+"


# ---------------------------------------------------------------- shared state

_SHARED_STREAM = "_loopdy_host_telemetry_stream_v1"


def _new_stream() -> types.ModuleType:
    stream = types.ModuleType(_SHARED_STREAM)
    stream.lock = threading.RLock()
    stream.events = deque()
    stream.wake = threading.Event()
    stream.thread = None
    stream.atexit_registered = False
    stream.started = False
    stream.start_timer = None
    stream.muted = False
    stream.session = str(uuid.uuid4())
    stream.turns = OrderedDict()
    stream.seen_tools = OrderedDict()
    stream.error_last = {}
    stream.transport = None
    stream.retry_at = 0.0
    stream.backoff = 0
    stream.settings = None
    stream.settings_at = 0.0
    stream.config_cache = {}
    stream.forced_off = set()
    stream.context = None
    stream.context_at = 0.0
    stream.sent_batches = 0
    stream.stopped = False
    return stream


def _stream() -> types.ModuleType:
    """The one stream of this process, whichever copy of this module asks."""
    stream = sys.modules.get(_SHARED_STREAM)
    if stream is None:
        stream = sys.modules.setdefault(_SHARED_STREAM, _new_stream())
    return stream


def set_transport(transport: Callable[[bytes, float], tuple[int, int | None]] | None) -> None:
    """Replace how a batch is posted: ``transport(body, timeout) -> (status, retry_after_seconds)``."""
    _stream().transport = transport


# ---------------------------------------------------------------- the off switch

def _root() -> Path:
    """The Hermes root of this process, not a profile's home: one switch and one ID for the whole host.

    A profile home may differ per turn (Hermes scopes it to the running agent), but the host is one
    computer and its owner decides once. Hosts without the helper fall back to the process home."""
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except ImportError:
        pass
    try:
        from hermes_constants import get_process_hermes_home
        return Path(get_process_hermes_home())
    except ImportError:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())


def _data_dir() -> Path:
    return _root() / "plugin-data" / "loopdy"


_OFF_WORDS = frozenset({"0", "false", "off", "no"})


def _env_off() -> bool:
    value = naming.env("TELEMETRY")
    return value is not None and value.strip().lower() in _OFF_WORDS


def _do_not_track() -> bool:
    return os.environ.get("DO_NOT_TRACK", "").strip() == "1"


def _config_value_off(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return value == 0
    return isinstance(value, str) and value.strip().lower() in _OFF_WORDS


def _config_off(stream: types.ModuleType) -> bool:
    """`bighelp: {telemetry: false}` in the Hermes root's or this process home's config.yaml.
    A file is parsed again only when it changed."""
    paths = {_root() / "config.yaml"}
    try:
        from hermes_constants import get_process_hermes_home
        paths.add(Path(get_process_hermes_home()) / "config.yaml")
    except ImportError:
        pass
    off = False
    for path in sorted(paths):
        try:
            info = path.stat()
        except OSError:
            continue
        if info.st_size > _MAX_CONFIG_BYTES:
            continue
        key = (info.st_mtime_ns, info.st_size)
        cached = stream.config_cache.get(path)
        if cached is None or cached[0] != key:
            try:
                from .yaml_compat import safe_load
                config = safe_load(path.read_text(encoding="utf-8"))
            except Exception:
                config = None
            block = config.get("bighelp") if isinstance(config, dict) else None
            cached = (key, isinstance(block, dict) and _config_value_off(block.get("telemetry")))
            stream.config_cache[path] = cached
        off = off or cached[1]
    return off


def _flags_path() -> Path:
    return _data_dir() / "telemetry-settings.json"


def _read_flags() -> dict[str, bool]:
    """The CLI and app switches. A missing file, key or a bad value means "on"."""
    flags = {"cli": True, "app": True}
    try:
        path = _flags_path()
        if path.stat().st_size > 4_096:
            return flags
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return flags
    if isinstance(value, dict):
        for key in flags:
            if value.get(key) is False:
                flags[key] = False
    return flags


def _write_flags(flags: dict[str, bool]) -> None:
    directory = _data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = _flags_path()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"cli": flags["cli"], "app": flags["app"]}, handle)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _file_settings(stream: types.ModuleType, *, fresh: bool = False) -> dict[str, bool]:
    now = time.monotonic()
    if fresh or stream.settings is None or now - stream.settings_at > _SETTINGS_TTL:
        flags = _read_flags()
        stream.settings = {**flags, "config": not _config_off(stream)}
        stream.settings_at = now
    return stream.settings


def sources(*, fresh: bool = False) -> dict[str, bool]:
    """Each switch, True when it lets telemetry run."""
    stream = _stream()
    files = _file_settings(stream, fresh=fresh)
    return {
        "doNotTrack": not _do_not_track(),
        "env": not _env_off(),
        "config": files["config"],
        "cli": files["cli"] and "cli" not in stream.forced_off,
        "app": files["app"] and "app" not in stream.forced_off,
    }


def enabled() -> bool:
    """Whether telemetry may run now. When it may not, anything waiting is dropped."""
    try:
        stream = _stream()
        if not stream.muted and all(sources().values()):
            return True
        if stream.events:
            clear_queue()
        return False
    except Exception:
        return False


def status() -> dict[str, Any]:
    """The route's answer: the effective state, the app's own switch, and whether env or config forces it off."""
    switches = sources(fresh=True)
    return {
        "version": 1,
        "enabled": all(switches.values()) and not _stream().muted,
        "appEnabled": switches["app"],
        "lockedOff": not (switches["doNotTrack"] and switches["env"] and switches["config"]),
    }


def available() -> bool:
    return True


def _forget_identity() -> None:
    try:
        _id_path().unlink()
    except OSError:
        pass


def clear_queue() -> None:
    stream = _stream()
    with stream.lock:
        stream.events.clear()
        stream.turns.clear()


def _set_flag(name: str, value: bool) -> None:
    stream = _stream()
    with stream.lock:
        if value:
            stream.forced_off.discard(name)
        flags = _read_flags()
        flags[name] = value
        try:
            _write_flags(flags)
        except OSError:
            # A switch that can't be saved still holds until this process ends.
            if not value:
                stream.forced_off.add(name)
                clear_queue()
            raise
        _file_settings(stream, fresh=True)
        if not value:
            clear_queue()
            _forget_identity()


def set_app_enabled(value: bool) -> dict[str, Any]:
    """The app's switch (PUT /native/telemetry). Off drops what is waiting and forgets the ID."""
    _set_flag("app", bool(value))
    return status()


def set_cli_enabled(value: bool) -> dict[str, Any]:
    """``hermes bighelp telemetry off``. ``on`` also clears the app's switch."""
    if value:
        flags = _read_flags()
        flags["cli"] = flags["app"] = True
        stream = _stream()
        with stream.lock:
            _write_flags(flags)
            _file_settings(stream, fresh=True)
    else:
        _set_flag("cli", False)
    return status()


def mute_process() -> None:
    """Send nothing more from this process (a short `hermes bighelp telemetry ...` command)."""
    stream = _stream()
    stream.muted = True
    clear_queue()


def describe() -> dict[str, Any]:
    """`hermes bighelp telemetry status`."""
    switches = sources(fresh=True)
    return {
        "enabled": all(switches.values()),
        "sources": {
            "DO_NOT_TRACK": "off (=1)" if not switches["doNotTrack"] else "not set",
            "BIGHELP_TELEMETRY": "off" if not switches["env"] else "not off",
            "config bighelp.telemetry": "off (false)" if not switches["config"] else "not off",
            "hermes bighelp telemetry": "off" if not switches["cli"] else "not off",
            "bighelp app": "off" if not switches["app"] else "not off",
        },
        "fields": DOC_PATH,
        "fieldsUrl": DOC_URL,
    }


# ---------------------------------------------------------------- the host's random ID

def _id_path() -> Path:
    return _data_dir() / "telemetry-id"


def telemetry_id() -> str:
    """A random UUID v4 made on first use. Nothing else derives it, so it can't be joined to
    anything, and it is forgotten when telemetry goes off."""
    path = _id_path()
    for _ in range(3):
        try:
            value = path.read_text(encoding="ascii").strip()
            if _UUID4.match(value):
                return value
        except (OSError, ValueError):
            pass
        value = str(uuid.uuid4())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Another process made it, or a bad one is there: replace only a bad one.
            try:
                if not _UUID4.match(path.read_text(encoding="ascii").strip()):
                    path.unlink()
            except (OSError, ValueError):
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(value)
        return value
    raise OSError("telemetry ID unavailable")


# ---------------------------------------------------------------- context

def _hermes_version() -> str | None:
    version = None
    try:
        import hermes_cli
        version = getattr(hermes_cli, "__version__", None)
    except Exception:
        version = None
    if not isinstance(version, str):
        try:
            from importlib import metadata
            version = metadata.version("hermes-agent")
        except Exception:
            version = None
    return version if isinstance(version, str) and _HERMES_VERSION.match(version) else None


def _agent_count() -> int:
    try:
        entries = list((_root() / "profiles").iterdir())
    except OSError:
        return 1
    return 1 + sum(1 for entry in entries if _PROFILE_DIR.match(entry.name) and entry.is_dir())


def _os_family() -> str:
    platform = sys.platform
    if platform.startswith("linux"):
        return "linux"
    if platform == "darwin":
        return "macos"
    if platform in ("win32", "cygwin"):
        return "windows"
    return "other"


def build_context() -> dict[str, str]:
    context: dict[str, str] = {}
    if _VERSION.match(PLUGIN_VERSION):
        context["pluginVersion"] = PLUGIN_VERSION
    hermes = _hermes_version()
    if hermes:
        context["hermesVersion"] = hermes
    context["osFamily"] = _os_family()
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if _PYTHON.match(python):
        context["python"] = python
    context["agentCount"] = agent_bucket(_agent_count())
    return context


def _context(stream: types.ModuleType) -> dict[str, str]:
    now = time.monotonic()
    if stream.context is None or now - stream.context_at > 600:
        stream.context = build_context()
        stream.context_at = now
    return stream.context


# ---------------------------------------------------------------- queue and emitters

def _enqueue(name: str, props: dict[str, Any]) -> None:
    if name not in EVENT_NAMES or not enabled():
        return
    stream = _stream()
    event = {"name": name, "t": int(time.time() * 1000), "p": props}
    with stream.lock:
        stream.events.append(event)
        while len(stream.events) > MAX_QUEUE:
            stream.events.popleft()
        count = len(stream.events)
    _ensure_sender(stream)
    if count >= BATCH_EVENTS:
        stream.wake.set()


def _emit(name: str, props: dict[str, Any]) -> None:
    try:
        _enqueue(name, {key: value for key, value in props.items() if value is not None})
    except Exception:
        logger.debug("bighelp telemetry unavailable")


def host_started() -> None:
    """The host came up. Once per process."""
    try:
        stream = _stream()
        if stream.started or stream.muted or not enabled():
            return
        stream.started = True
        _emit("host.started", {})
    except Exception:
        logger.debug("bighelp telemetry unavailable")


def announce_host_started() -> None:
    """Counts a start once this process has been up for a while, so a short `hermes` command
    (which also loads the plugin) is not a host start."""
    stream = _stream()
    if stream.start_timer is not None:
        return
    timer = threading.Timer(START_DELAY_SECONDS, host_started)
    timer.daemon = True
    stream.start_timer = timer
    timer.start()


def _quiet(function: Callable[..., None]) -> Callable[..., None]:
    """An emitter that never raises into Hermes, whatever it is given."""
    @functools.wraps(function)
    def emit(*args: Any, **kwargs: Any) -> None:
        try:
            function(*args, **kwargs)
        except Exception:
            logger.debug("bighelp telemetry unavailable")
    return emit


@_quiet
def turn_completed(*, duration_ms: Any = None, first_token_ms: Any = None, tool_calls: Any = None,
                   outcome: Any = None, provider: Any = None, model: Any = None,
                   session_source: Any = None) -> None:
    provider_name, model_name = classify_route(provider, model)
    _emit("turn.completed", {
        "durationMs": _number(duration_ms, MAX_MS),
        "firstTokenMs": _number(first_token_ms, MAX_MS),
        "toolCalls": _number(tool_calls, MAX_COUNT),
        "outcome": outcome if outcome in OUTCOMES else None,
        "provider": _pattern(provider_name, _PROVIDER),
        "model": _pattern(model_name, _MODEL),
        "sessionSource": classify_source(session_source),
    })


@_quiet
def tool_completed(*, tool: Any = None, duration_ms: Any = None, outcome: Any = None) -> None:
    name = classify_tool(tool)
    _emit("tool.completed", {
        "tool": _pattern(name, _TOOL),
        "durationMs": _number(duration_ms, MAX_MS),
        "outcome": outcome if outcome in TOOL_OUTCOMES else None,
    })


@_quiet
def alert_queued(*, event_type: Any = None, queue_ms: Any = None) -> None:
    if event_type not in ALERT_EVENT_TYPES:
        return
    _emit("alert.queued", {"eventType": event_type, "queueMs": _number(queue_ms, MAX_MS)})


@_quiet
def alert_sent(*, event_type: Any = None, relay_ms: Any = None, result: Any = None,
               attempt: Any = None) -> None:
    if event_type not in ALERT_EVENT_TYPES:
        return
    _emit("alert.sent", {
        "eventType": event_type,
        "relayMs": _number(relay_ms, MAX_MS),
        "result": _pattern(result, _CODE) or "other",
        "attempt": _number(attempt, MAX_COUNT),
    })


def error(*, code: Any = None, stage: Any = None) -> None:
    """A fixed error code the plugin already logs, and the fixed stage it came from. The same pair is
    reported once a minute at most."""
    try:
        if _pattern(code, _CODE) is None or stage not in ERROR_STAGES:
            return
        stream = _stream()
        now = time.monotonic()
        with stream.lock:
            last = stream.error_last.get((code, stage))
            if last is not None and now - last < _ERROR_REPEAT_SECONDS:
                return
            if len(stream.error_last) > 256:
                stream.error_last.clear()
            stream.error_last[(code, stage)] = now
        _emit("error", {"code": code, "stage": stage})
    except Exception:
        logger.debug("bighelp telemetry unavailable")


# ---------------------------------------------------------------- sending

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _under_test_runner() -> bool:
    """True when this process is a test runner: tests never reach the network."""
    main = sys.modules.get("__main__")
    spec = getattr(main, "__spec__", None)
    name = getattr(spec, "name", "") or ""
    if name in ("unittest.__main__", "pytest.__main__"):
        return True
    return os.path.basename(sys.argv[0] if sys.argv else "") in ("pytest", "py.test")


def _post(body: bytes, timeout: float) -> tuple[int, int | None]:
    """Posts one batch. Returns (HTTP status, Retry-After seconds); status 0 when it never got there."""
    if _under_test_runner():
        return 0, None
    request = Request(ENDPOINT, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": "bighelp-plugin",
    })
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            response.read(4_096)
            return response.status, None
    except HTTPError as failure:
        retry = None
        try:
            retry = int(failure.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            pass
        return failure.code, retry
    except Exception:
        return 0, None


def _take(stream: types.ModuleType) -> list[dict[str, Any]]:
    with stream.lock:
        count = min(BATCH_EVENTS, len(stream.events))
        return [stream.events.popleft() for _ in range(count)]


def _put_back(stream: types.ModuleType, batch: list[dict[str, Any]]) -> None:
    with stream.lock:
        combined = batch + list(stream.events)
        stream.events.clear()
        stream.events.extend(combined[-MAX_QUEUE:])


def flush(*, timeout: float = TIMEOUT_SECONDS) -> int:
    """Posts what is waiting, in batches of 50, at most three batches. Returns how many events went.
    Never raises."""
    sent = 0
    try:
        stream = _stream()
        for _ in range(_POSTS_PER_FLUSH):
            if not enabled():
                clear_queue()
                return sent
            if time.monotonic() < stream.retry_at:
                return sent
            batch = _take(stream)
            if not batch:
                return sent
            body = json.dumps({
                "version": 1, "source": "host", "id": telemetry_id(), "session": stream.session,
                "context": _context(stream), "events": batch,
            }, separators=(",", ":")).encode("utf-8")
            transport = stream.transport or _post
            try:
                status, retry_after = transport(body, timeout)
            except Exception:
                status, retry_after = 0, None
            if status == 429:
                stream.backoff = min(_MAX_BACKOFF, max(stream.backoff * 2, _DEFAULT_BACKOFF))
                wait = retry_after if isinstance(retry_after, int) and retry_after > 0 else stream.backoff
                stream.retry_at = time.monotonic() + min(wait, _MAX_BACKOFF)
                _put_back(stream, batch)
                return sent
            stream.backoff = 0
            if 200 <= status < 300:
                sent += len(batch)
                stream.sent_batches += 1
            # Any other answer: the batch is dropped, silently.
    except Exception:
        logger.debug("bighelp telemetry unavailable")
    return sent


def _run(stream: types.ModuleType) -> None:
    last = time.monotonic()
    while True:
        stream.wake.wait(max(0.0, FLUSH_SECONDS - (time.monotonic() - last)))
        stream.wake.clear()
        if stream.stopped or sys.modules.get(_SHARED_STREAM) is not stream:
            return
        if not enabled():
            last = time.monotonic()
            continue
        with stream.lock:
            waiting = len(stream.events)
        due = time.monotonic() - last >= FLUSH_SECONDS
        if waiting >= BATCH_EVENTS or (due and waiting):
            flush()
            last = time.monotonic()
        elif due:
            last = time.monotonic()


def _flush_at_exit() -> None:
    try:
        stream = _stream()
        if stream.events and enabled():
            flush(timeout=EXIT_TIMEOUT_SECONDS)
    except Exception:
        pass


def _ensure_sender(stream: types.ModuleType) -> None:
    with stream.lock:
        thread = stream.thread
        if thread is not None and thread.is_alive():
            return
        stream.thread = threading.Thread(target=_run, args=(stream,), name="bighelp-host-telemetry", daemon=True)
        stream.thread.start()
        if not stream.atexit_registered:
            stream.atexit_registered = True
            atexit.register(_flush_at_exit)


# ---------------------------------------------------------------- Hermes hooks

def _session_key(payload: dict[str, Any]) -> str:
    for name in ("session_id", "task_id", "turn_id"):
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def _turn_state(stream: types.ModuleType, key: str, *, create: bool = False) -> dict[str, Any] | None:
    with stream.lock:
        state = stream.turns.get(key)
        if state is None and create:
            now = time.monotonic()
            while stream.turns and (len(stream.turns) >= _MAX_TURNS
                                    or now - next(iter(stream.turns.values()))["start"] > _TURN_LIFETIME):
                stream.turns.popitem(last=False)
            state = {"start": now, "first_token": None, "tools": 0, "provider": None, "model": None,
                     "source": None}
            stream.turns[key] = state
        return state


def _finish_turn(key: str, outcome: str, payload: dict[str, Any]) -> None:
    stream = _stream()
    with stream.lock:
        state = stream.turns.pop(key, None)
    if state is None:
        return
    turn_completed(
        duration_ms=(time.monotonic() - state["start"]) * 1000,
        first_token_ms=state["first_token"],
        tool_calls=state["tools"],
        outcome=outcome,
        provider=state["provider"],
        model=state["model"],
        session_source=state["source"] or payload.get("platform"),
    )


def on_pre_llm_call(**payload: Any) -> None:
    if not enabled():
        return
    key = _session_key(payload)
    if not key:
        return
    stream = _stream()
    turn = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else ""
    with stream.lock:
        existing = stream.turns.get(key)
        # A turn Hermes announces again keeps its clock; a new one (or an older Hermes that names no
        # turn) starts afresh, so a turn that never ended can't lend its start to the next.
        if existing is not None and turn and existing.get("turn") == turn:
            return
        stream.turns.pop(key, None)
    state = _turn_state(stream, key, create=True)
    state["turn"] = turn
    state["source"] = payload.get("platform")
    state["model"] = payload.get("model")


def on_pre_api_request(**payload: Any) -> None:
    if not enabled():
        return
    state = _turn_state(_stream(), _session_key(payload))
    if state is not None:
        if payload.get("provider"):
            state["provider"] = payload.get("provider")
        if payload.get("model"):
            state["model"] = payload.get("model")


def on_stream_start(**payload: Any) -> None:
    if not enabled():
        return
    state = _turn_state(_stream(), _session_key(payload))
    if state is not None and state["first_token"] is None:
        state["first_token"] = (time.monotonic() - state["start"]) * 1000


def on_post_tool_call(**payload: Any) -> None:
    if not enabled():
        return
    stream = _stream()
    call_id = payload.get("tool_call_id")
    if isinstance(call_id, str) and call_id:
        # Hermes loads the plugin again for each profile: the same call can reach two copies.
        with stream.lock:
            if call_id in stream.seen_tools:
                return
            stream.seen_tools[call_id] = True
            while len(stream.seen_tools) > _MAX_SEEN_TOOLS:
                stream.seen_tools.popitem(last=False)
    state = _turn_state(stream, _session_key(payload))
    if state is not None:
        state["tools"] += 1
    tool_completed(tool=payload.get("tool_name"), duration_ms=payload.get("duration_ms"),
                   outcome=_tool_outcome(payload))


def _tool_outcome(payload: dict[str, Any]) -> str:
    status = payload.get("status")
    if isinstance(status, str):
        return "ok" if status.lower() in ("ok", "success", "completed") else "failed"
    if payload.get("error_type") or payload.get("error_message"):
        return "failed"
    result = payload.get("result")
    # Older Hermes gives only the result text; a failure has an "error" key. Read here, never sent.
    if isinstance(result, str) and len(result) < 65_536 and result.startswith("{"):
        try:
            value = json.loads(result)
        except ValueError:
            return "ok"
        if isinstance(value, dict) and value.get("error"):
            return "failed"
    return "ok"


def on_post_llm_call(**payload: Any) -> None:
    if enabled():
        _finish_turn(_session_key(payload), "ok", payload)


def on_session_end(**payload: Any) -> None:
    if not enabled() or not any(name in payload for name in ("completed", "failed", "interrupted")):
        return
    if payload.get("interrupted"):
        outcome = "cancelled"
    elif payload.get("failed"):
        outcome = "failed"
    else:
        outcome = "ok"
    _finish_turn(_session_key(payload), outcome, payload)


def _guarded(callback: Callable[..., None]) -> Callable[..., None]:
    def observe(**payload: Any) -> None:
        try:
            callback(**payload)
        except Exception:
            logger.debug("bighelp telemetry unavailable")
        return None
    observe.__name__ = getattr(callback, "__name__", "observe")
    return observe


def register(ctx: Any) -> None:
    """Observes turns and tool calls with Hermes' own hooks. Newer hooks only where Hermes has them."""
    try:
        hooks: dict[str, Callable[..., None]] = {
            "pre_llm_call": on_pre_llm_call,
            "post_llm_call": on_post_llm_call,
            "post_tool_call": on_post_tool_call,
            "on_session_end": on_session_end,
        }
        try:
            from hermes_cli.plugins import VALID_HOOKS
        except ImportError:
            VALID_HOOKS = frozenset()
        # Which provider answered, and when the answer began to stream, need these newer hooks.
        for name, callback in (("pre_api_request", on_pre_api_request), ("on_stream_start", on_stream_start)):
            if name in VALID_HOOKS:
                hooks[name] = callback
        for name, callback in hooks.items():
            ctx.register_hook(name, _guarded(callback))
        announce_host_started()
    except Exception:
        logger.warning("bighelp telemetry unavailable")
