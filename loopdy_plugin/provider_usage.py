"""Usage and limits for the AI tools on this computer, for the bighelp app.

Finds the coding tools installed here (Claude Code, Codex, GitHub Copilot,
OpenCode, Gemini CLI) and the providers configured in Hermes (OpenRouter,
DeepSeek, Google AI Studio, Nous Portal, OpenCode Go, and any provider plugin
with a usage hook), then reads each one's current usage from that provider's
own source.

Read-only: logins borrowed from other tools are never refreshed or written
back (an expired Claude Code login asks the person to open Claude Code), CLIs
refresh their own logins, and tokens, emails and account IDs never leave this
module. Credentials are only sent to their provider, and never across a redirect.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

CAPABILITY = "native-provider-usage-v1"
CACHE_SECONDS = 300
MIN_REFRESH_SECONDS = 15
DEADLINE_SECONDS = 20.0
_CALL_SECONDS = 12.0
_MAX_BYTES = 1_048_576

OK, SIGN_IN, NOT_SHARED, ERROR = "ok", "signInNeeded", "notShared", "error"

logger = logging.getLogger("hermes.plugins.bighelp")


def available() -> bool:
    """The route scopes Hermes lookups to the requested profile with these public helpers."""
    try:
        from hermes_cli.profiles import get_profile_dir, profile_exists
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    except ImportError:
        return False
    return all(map(callable, (get_profile_dir, profile_exists, set_hermes_home_override, reset_hermes_home_override)))


# ---- Report model ---------------------------------------------------------------------------

@dataclass
class Window:
    label: str
    used_percent: float
    resets_at: datetime | None = None
    detail: str | None = None


@dataclass
class Report:
    id: str
    name: str
    status: str = OK
    message: str | None = None
    plan: str | None = None
    windows: list[Window] = field(default_factory=list)
    facts: list[tuple[str, str]] = field(default_factory=list)
    manage_url: str | None = None
    via: tuple[str, ...] = ()
    approximate: bool = False

    def payload(self, active: bool) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "status": self.status, "message": self.message,
            "plan": self.plan, "detectedVia": list(self.via), "activeInHermes": active,
            "windows": [{"label": w.label, "usedPercent": round(w.used_percent, 2),
                         "resetsAt": _iso(w.resets_at), "detail": w.detail} for w in self.windows],
            "facts": [{"label": label, "value": value} for label, value in self.facts],
            "manageUrl": self.manage_url, "approximate": self.approximate,
        }


class Unavailable(Exception):
    """A fixed, secret-free reason shown to the person instead of numbers."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class _Source:
    """A detected tool or provider. Detection is instant; ``fetch`` does the slow part."""
    id: str
    name: str
    via: tuple[str, ...]
    fetch: Callable[[], list[Report]]
    manage_url: str | None = None


# ---- Small helpers ----------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _percent(value: Any) -> float | None:
    number = _num(value)
    return None if number is None else max(0.0, min(100.0, number))


def _when(value: Any) -> datetime | None:
    if isinstance(value, str) and re.fullmatch(r"\d+(\.\d+)?", value.strip()):
        value = float(value)
    number = None if isinstance(value, str) else _num(value)
    if number is not None:
        # Epoch seconds; some APIs send milliseconds.
        return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, tz=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """Whole seconds, UTC, ``Z``: what ISO 8601 date parsers accept by default."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _money(amount: float, currency: str | None = "USD") -> str:
    code = (currency or "USD").upper()
    symbol = {"USD": "$", "CNY": "¥", "EUR": "€", "GBP": "£"}.get(code)
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {code}"


def _title(value: Any) -> str | None:
    text = str(value or "").strip()
    return text.replace("_", " ").replace("-", " ").title() if text else None


def _duration(minutes: float) -> str:
    if minutes == 300:
        return "Session (5 hours)"
    if minutes >= 1440 and minutes % 1440 == 0:
        days = int(minutes // 1440)
        return {1: "Day", 7: "Week"}.get(days, f"{days} days")
    if minutes >= 60 and minutes % 60 == 0:
        hours = int(minutes // 60)
        return "Hour" if hours == 1 else f"{hours} hours"
    return f"{int(minutes)} minutes"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a credential must never follow a redirect to another origin


_opener = build_opener(_NoRedirect())


class HTTPStatus(Exception):
    def __init__(self, code: int):
        super().__init__(f"HTTP {code}")
        self.code = code


def http_json(url: str, headers: dict[str, str], *, timeout: float = _CALL_SECONDS) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "bighelp-plugin", **headers})
    try:
        with _opener.open(request, timeout=timeout) as response:
            body = response.read(_MAX_BYTES + 1)
    except HTTPError as error:
        raise HTTPStatus(error.code) from None
    except (URLError, OSError, ValueError):
        raise Unavailable(ERROR, "Couldn't reach the provider. Check the computer's internet connection.") from None
    try:
        if len(body) > _MAX_BYTES:
            raise ValueError("too large")
        value = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise Unavailable(ERROR, "The provider sent usage bighelp couldn't read.") from None
    if not isinstance(value, dict):
        raise Unavailable(ERROR, "The provider sent usage bighelp couldn't read.")
    return value


# Services often run with a short PATH (launchd, systemd), so also look where installers put these CLIs.
_EXTRA_BIN_DIRS = ("~/.local/bin", "~/.opencode/bin", "~/.claude/local", "~/.bun/bin", "~/.npm-global/bin",
                   "~/.volta/bin", "~/.cargo/bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")


def _search_path() -> str:
    parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    for extra in _EXTRA_BIN_DIRS:
        path = os.path.expanduser(extra)
        if path not in parts:
            parts.append(path)
    return os.pathsep.join(parts)


def find_cli(name: str) -> str | None:
    return shutil.which(name, path=_search_path())


def _child_env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(PATH=_search_path(), NO_COLOR="1", TERM="dumb", **extra)
    return env


def run(argv: list[str], *, timeout: float = _CALL_SECONDS, env: dict[str, str] | None = None) -> str | None:
    """stdout of a finished command, or None. Never a shell; stdin closed."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, env=env or _child_env(), stdin=subprocess.DEVNULL,
                                cwd=str(Path.home()), check=False)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return result.stdout if result.returncode == 0 else None


class RpcError(Exception):
    pass


class Rpc:
    """Minimal JSON-RPC client for a CLI's stdio server (Codex: JSON lines; Copilot: Content-Length frames)."""

    def __init__(self, argv: list[str], *, framed: bool, env: dict[str, str] | None = None,
                 timeout: float = _CALL_SECONDS, popen: Callable[..., Any] = subprocess.Popen):
        self._framed = framed
        self._deadline = time.monotonic() + timeout
        self._messages: queue.Queue[Any] = queue.Queue()
        try:
            self._process = popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  env=env or _child_env(), cwd=str(Path.home()))
        except (OSError, ValueError):
            raise RpcError("start") from None
        threading.Thread(target=self._reader, daemon=True, name="bighelp-usage-rpc").start()

    def __enter__(self) -> "Rpc":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _reader(self) -> None:
        try:
            while True:
                self._messages.put(self._read())
        except BaseException as error:  # noqa: BLE001 - handed to the waiting caller
            self._messages.put(error)

    def _read(self) -> Any:
        stream = self._process.stdout
        if not self._framed:
            line = stream.readline(_MAX_BYTES + 1)
            if not line:
                raise EOFError
            if len(line) > _MAX_BYTES:
                raise ValueError("line too long")
            return json.loads(line) if line.strip() else None
        length, header_bytes = None, 0
        while True:
            line = stream.readline(8193)
            if not line:
                raise EOFError
            header_bytes += len(line)
            if header_bytes > 8192:
                raise ValueError("headers too long")
            if line in (b"\r\n", b"\n"):
                break
            name, _, value = line.decode("ascii").partition(":")
            if name.strip().lower() == "content-length":
                length = int(value.strip())
        if length is None or not 0 <= length <= _MAX_BYTES:
            raise ValueError("bad frame")
        body = stream.read(length)
        if len(body) != length:
            raise EOFError
        return json.loads(body)

    def _send(self, message: dict[str, Any]) -> None:
        if self._framed:
            message = {"jsonrpc": "2.0", **message}
        data = json.dumps(message, separators=(",", ":")).encode("utf-8")
        data = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii") + data if self._framed else data + b"\n"
        try:
            self._process.stdin.write(data)
            self._process.stdin.flush()
        except (OSError, ValueError):
            raise RpcError("write") from None

    def notify(self, method: str) -> None:
        self._send({"method": method})

    def call(self, request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._send({"id": request_id, "method": method, "params": params or {}})
        while True:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            try:
                message = self._messages.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(method) from None
            if isinstance(message, BaseException):
                raise RpcError("closed") from None
            if not isinstance(message, dict):
                continue
            if message.get("id") == request_id and "method" not in message:
                if "error" in message or not isinstance(message.get("result"), dict):
                    raise RpcError(method)
                return message["result"]
            if "id" in message and "method" in message:  # the server asked us something; we don't handle requests
                self._send({"id": message["id"], "error": {"code": -32601, "message": "Not supported"}})

    def close(self) -> None:
        try:
            if self._process.poll() is None:
                self._process.terminate()
            self._process.wait(timeout=3)
        except Exception:  # noqa: BLE001
            try:
                self._process.kill()
                self._process.wait(timeout=3)
            except Exception:  # noqa: BLE001
                pass
        for stream in (self._process.stdin, self._process.stdout):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass


# ---- Hermes lookups (profile-scoped by the caller; all optional on older hosts) -------------

def hermes_env(*names: str) -> str | None:
    try:
        from hermes_cli.config import get_env_value
    except ImportError:
        get_env_value = os.environ.get
    for name in names:
        try:
            value = str(get_env_value(name) or "").strip()
        except Exception:  # noqa: BLE001
            value = ""
        if value:
            return value
    return None


def hermes_key(provider: str) -> tuple[str, str] | None:
    """(api key, base URL) Hermes would use for an API-key provider, or None when it has none."""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        runtime = resolve_runtime_provider(requested=provider)
    except Exception:  # noqa: BLE001 - AuthError and friends mean "not configured"
        return None
    key = str(runtime.get("api_key") or "").strip() if isinstance(runtime, dict) else ""
    return (key, str(runtime.get("base_url") or "").rstrip("/")) if key else None


def hermes_account_usage(provider: str) -> Any:
    try:
        from agent.account_usage import fetch_account_usage
    except ImportError:
        return None
    return fetch_account_usage(provider)


def active_provider() -> str | None:
    try:
        from hermes_cli.config import load_config
        model = load_config().get("model")
    except Exception:  # noqa: BLE001
        return None
    provider = str(model.get("provider") or "").strip().lower() if isinstance(model, dict) else ""
    return provider or None


_HERMES_IDS = {"anthropic": "claude", "openai-codex": "codex", "copilot": "copilot", "copilot-acp": "copilot",
               "gemini": "gemini", "deepseek": "deepseek", "openrouter": "openrouter", "opencode-zen": "opencode",
               "opencode-go": "opencode", "nous": "nous"}


def usage_id(hermes_provider: str | None) -> str | None:
    provider = (hermes_provider or "").lower()
    if provider in _HERMES_IDS:
        return _HERMES_IDS[provider]
    for needle, report_id in (("claude", "claude"), ("anthropic", "claude"), ("codex", "codex"),
                              ("copilot", "copilot"), ("gemini", "gemini"), ("opencode", "opencode")):
        if needle in provider:
            return report_id
    return f"hermes-{provider}" if provider else None


def _via(cli: bool, hermes: bool) -> tuple[str, ...]:
    return tuple(name for name, found in (("cli", cli), ("hermes", hermes)) if found)


# ---- Claude (Claude Code login or Hermes' own Claude sign-in) ------------------------------

_CLAUDE_URL = "https://api.anthropic.com/api/oauth/usage"
_CLAUDE_WINDOWS = (("five_hour", "Session (5 hours)"), ("seven_day", "Week"),
                   ("seven_day_opus", "Opus week"), ("seven_day_sonnet", "Sonnet week"))


def claude_code_logins() -> list[dict[str, Any]]:
    """Claude Code's saved logins (macOS Keychain and credentials file), unmodified. Honors Hermes'
    ``auth.adopt_external_logins: false``, which tells Hermes to leave other tools' logins alone."""
    try:
        from agent.credential_sources import adopt_external_logins_enabled
        if not adopt_external_logins_enabled():
            return []
    except Exception:  # noqa: BLE001 - older Hermes: no such setting
        pass
    logins = []
    if sys.platform == "darwin":
        raw = run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"], timeout=5)
        try:
            logins.append(json.loads(raw or "").get("claudeAiOauth"))
        except (ValueError, AttributeError):
            pass
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR", "").strip() or Path.home() / ".claude").expanduser()
    try:
        logins.append(json.loads((root / ".credentials.json").read_text("utf-8")).get("claudeAiOauth"))
    except (OSError, ValueError, AttributeError):
        pass
    return [login for login in logins if isinstance(login, dict) and isinstance(login.get("accessToken"), str)
            and login["accessToken"].strip()]


def _claude_fresh(login: dict[str, Any]) -> bool:
    expires = _num(login.get("expiresAt"))
    return expires is None or expires / 1000 > time.time() + 60


def _claude_plan(login: dict[str, Any]) -> str | None:
    tier = str(login.get("rateLimitTier") or "")
    for marker, name in (("max_20x", "Max 20x"), ("max_5x", "Max 5x")):
        if marker in tier:
            return name
    return _title(login.get("subscriptionType"))


def hermes_claude_token() -> str | None:
    """Hermes' own Claude sign-in (not one borrowed from Claude Code), read without refreshing."""
    for name in ("ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        token = hermes_env(name)
        if token and token.startswith("sk-ant-oat"):
            return token
    try:
        from agent.anthropic_credentials import _resolve_anthropic_pool_token
        return _resolve_anthropic_pool_token(skip_borrowed=True) or None
    except Exception:  # noqa: BLE001
        return None


def detect_claude(active: str | None) -> _Source | None:
    cli = find_cli("claude")
    logins = claude_code_logins()
    hermes_token = hermes_claude_token()
    api_key = hermes_env("ANTHROPIC_API_KEY")
    if not (cli or logins or hermes_token or api_key):
        return None
    source = _Source("claude", "Claude", _via(bool(cli or logins), bool(hermes_token or api_key)), lambda: [],
                     "https://claude.ai/settings/usage")

    def fetch() -> list[Report]:
        fresh = [login for login in logins if _claude_fresh(login)]
        # Keychain first: Claude Code 2.1.x refreshes it and can leave the file stale.
        login = fresh[0] if fresh else None
        token = login["accessToken"] if login else hermes_token
        if not token:
            if logins:
                raise Unavailable(SIGN_IN, "Claude Code's sign-in has expired. Open Claude Code on this computer to refresh it.")
            if api_key:
                raise Unavailable(NOT_SHARED, "Anthropic doesn't share usage for API keys. Check it in the Claude Console.")
            raise Unavailable(SIGN_IN, "Sign in to Claude Code on this computer to see your limits.")
        try:
            payload = http_json(_CLAUDE_URL, {"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
        except HTTPStatus as error:
            if error.code == 401:
                raise Unavailable(SIGN_IN, "Claude didn't accept the saved sign-in. Open Claude Code on this computer to refresh it.") from None
            if error.code == 403:
                raise Unavailable(NOT_SHARED, "This Claude sign-in can't read usage. Sign in to Claude Code to see your limits.") from None
            if error.code == 429:
                raise Unavailable(ERROR, "Claude is limiting how often usage can be checked. Try again in a minute.") from None
            raise Unavailable(ERROR, "Claude's usage service answered with an error.") from None
        report = Report(source.id, source.name, plan=_claude_plan(login) if login else None,
                        manage_url=source.manage_url, via=source.via)
        for key, label in _CLAUDE_WINDOWS:
            window = payload.get(key)
            used = _percent(window.get("utilization")) if isinstance(window, dict) else None
            if used is not None:
                report.windows.append(Window(label, used, _when(window.get("resets_at"))))
        extra = payload.get("extra_usage")
        if isinstance(extra, dict) and extra.get("is_enabled"):
            used = _percent(extra.get("utilization"))
            spent, limit = _num(extra.get("used_credits")), _num(extra.get("monthly_limit"))
            if used is None and spent is not None and limit:
                used = _percent(spent / limit * 100)
            if used is not None:
                report.windows.append(Window("Extra usage this month", used))
        if not report.windows:
            raise Unavailable(ERROR, "Claude didn't send any limits.")
        return [report]

    source.fetch = fetch
    return source


# ---- Codex (the Codex CLI's own app server, or Hermes' Codex sign-in) -----------------------

def _codex_windows(limits: dict[str, Any], prefix: str = "") -> list[Window]:
    windows = []
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        used, minutes = _percent(window.get("usedPercent")), _num(window.get("windowDurationMins"))
        if used is None:
            continue
        label = _duration(minutes) if minutes else ("Session" if key == "primary" else "Week")
        windows.append(Window(f"{prefix}{label}", used, _when(window.get("resetsAt"))))
    return windows


def _codex_report(result: dict[str, Any], source: _Source, report_id: str, name: str, via: tuple[str, ...]) -> Report:
    limits = result.get("rateLimits") if isinstance(result.get("rateLimits"), dict) else {}
    report = Report(report_id, name, plan=_title(limits.get("planType")), manage_url=source.manage_url, via=via,
                    windows=_codex_windows(limits))
    extras = result.get("rateLimitsByLimitId")
    for limit_id, extra in (extras.items() if isinstance(extras, dict) else ()):
        if limit_id != limits.get("limitId") and isinstance(extra, dict):
            label = str(extra.get("limitName") or limit_id).strip()
            report.windows.extend(_codex_windows(extra, f"{_title(label)} · "))
    credits = limits.get("credits") if isinstance(limits.get("credits"), dict) else {}
    balance = _num(credits.get("balance"))
    if credits.get("unlimited"):
        report.facts.append(("Credits", "Unlimited"))
    elif credits.get("hasCredits") and balance is not None:
        report.facts.append(("Credits", _money(balance)))
    banked = _num((result.get("rateLimitResetCredits") or {}).get("availableCount"))
    if banked:
        report.facts.append(("Banked resets", f"{int(banked)}"))
    if not report.windows and not report.facts:
        raise Unavailable(ERROR, "Codex didn't send any limits.")
    return report


def codex_app_server(cli: str) -> dict[str, Any]:
    """``account/rateLimits/read`` from the Codex CLI's documented app server; it refreshes its own login."""
    from .link_contracts import PLUGIN_VERSION
    try:
        with Rpc([cli, "app-server"], framed=False) as rpc:
            rpc.call(0, "initialize", {"clientInfo": {"name": "bighelp", "title": "bighelp", "version": PLUGIN_VERSION}})
            rpc.notify("initialized")
            account = rpc.call(1, "account/read", {}).get("account")
            if not isinstance(account, dict):
                raise Unavailable(SIGN_IN, "Sign in to Codex on this computer (run codex login) to see your limits.")
            if account.get("type") == "apiKey":
                raise Unavailable(NOT_SHARED, "Codex uses an API key here. OpenAI shows API usage on its website.")
            return rpc.call(2, "account/rateLimits/read")
    except TimeoutError:
        raise Unavailable(ERROR, "Codex took too long to answer.") from None
    except RpcError:
        raise Unavailable(ERROR, "Codex couldn't read your limits. Update the Codex CLI and try again.") from None


def _codex_from_hermes(snapshot: Any) -> dict[str, Any] | None:
    """Hermes' /usage payload for Codex, in the app server's shape."""
    raw = getattr(snapshot, "raw", None)
    if not isinstance(raw, dict):
        return None
    rate = raw.get("rate_limit") if isinstance(raw.get("rate_limit"), dict) else {}
    limits: dict[str, Any] = {"planType": raw.get("plan_type")}
    for source, target in (("primary_window", "primary"), ("secondary_window", "secondary")):
        window = rate.get(source)
        if isinstance(window, dict):
            seconds = _num(window.get("limit_window_seconds"))
            limits[target] = {"usedPercent": window.get("used_percent"), "resetsAt": window.get("reset_at"),
                              "windowDurationMins": seconds / 60 if seconds else None}
    credits = raw.get("credits") if isinstance(raw.get("credits"), dict) else {}
    limits["credits"] = {"hasCredits": credits.get("has_credits"), "unlimited": credits.get("unlimited"),
                         "balance": credits.get("balance")}
    resets = raw.get("rate_limit_reset_credits") if isinstance(raw.get("rate_limit_reset_credits"), dict) else {}
    return {"rateLimits": limits, "accountId": raw.get("account_id"),
            "rateLimitResetCredits": {"availableCount": resets.get("available_count")}}


def detect_codex(active: str | None) -> _Source | None:
    cli = find_cli("codex")
    hermes_active = active == "codex"
    hermes = hermes_active or _hermes_has_codex()
    if not (cli or hermes):
        return None
    source = _Source("codex", "Codex", _via(bool(cli), hermes), lambda: [], "https://chatgpt.com/codex/settings/usage")

    def fetch() -> list[Report]:
        from_cli = None
        cli_problem: Unavailable | None = None
        if cli:
            try:
                from_cli = codex_app_server(cli)
            except Unavailable as problem:
                cli_problem = problem
        # With a working CLI, ask Hermes too only when Hermes chats with Codex (it may be another account).
        from_hermes = None
        if from_cli is None or hermes_active:
            try:
                from_hermes = _codex_from_hermes(hermes_account_usage("openai-codex"))
            except Exception:  # noqa: BLE001
                from_hermes = None
        if from_cli is None and from_hermes is None:
            raise cli_problem or Unavailable(SIGN_IN, "Sign in to Codex on this computer (run codex login) to see your limits.")
        if from_cli is None:
            return [_codex_report(from_hermes, source, "codex", "Codex", ("hermes",))]
        same = from_hermes is None or from_hermes.get("accountId") == from_cli.get("accountId")
        reports = [_codex_report(from_cli, source, "codex", "Codex", source.via if same else ("cli",))]
        if not same:
            reports.append(_codex_report(from_hermes, source, "codex-hermes", "Codex (Hermes)", ("hermes",)))
        return reports

    source.fetch = fetch
    return source


def _hermes_has_codex() -> bool:
    """Read-only: Hermes' own Codex sign-in or credential pool rows (load_pool can write, so it isn't used)."""
    try:
        from hermes_cli.auth import get_provider_auth_state, read_credential_pool
        return bool(get_provider_auth_state("openai-codex")) or bool(read_credential_pool("openai-codex"))
    except Exception:  # noqa: BLE001
        return False


# ---- GitHub Copilot (after github.com/promptclickrun/ghc-usage-hermes) ----------------------

_COPILOT_URL = "https://api.github.com/copilot_internal/user"
_COPILOT_PLANS = {"individual": "Pro", "individual_pro": "Pro+", "business": "Business",
                  "enterprise": "Enterprise", "free": "Free"}


def copilot_token() -> str | None:
    """Hermes' Copilot credential order: env tokens, then ``gh auth token``. Classic PATs don't work."""
    for name in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        token = hermes_env(name)
        if token and not token.startswith("ghp_"):
            return token
    gh = find_cli("gh")
    if not gh:
        return None
    env = _child_env(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1")
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    token = (run([gh, "auth", "token"], timeout=5, env=env) or "").strip()
    return token if token and not token.startswith("ghp_") else None


def _copilot_window(used: float | None, limit: float | None, percent_left: Any, label: str,
                    resets: datetime | None) -> Window | None:
    if limit:
        used_percent = _percent((used or 0) / limit * 100)
        return Window(label, used_percent, resets, f"{int(used or 0):,} of {int(limit):,} used")
    left = _percent(percent_left)
    return Window(label, 100 - left, resets) if left is not None else None


def copilot_from_api(payload: dict[str, Any], source: _Source) -> Report:
    snapshots = payload.get("quota_snapshots")
    bucket = snapshots.get("premium_interactions") if isinstance(snapshots, dict) else None
    if not isinstance(bucket, dict):
        raise Unavailable(ERROR, "GitHub didn't send Copilot usage.")
    plan = str(payload.get("copilot_plan") or "")
    report = Report(source.id, source.name, plan=_COPILOT_PLANS.get(plan, _title(plan)),
                    manage_url=source.manage_url, via=source.via)
    label = "Monthly credits" if payload.get("token_based_billing") else "Premium requests"
    if bucket.get("unlimited"):
        report.facts.append((label, "Unlimited"))
        return report
    limit = _num(bucket.get("entitlement"))
    used = _num(bucket.get("credits_used"))
    if used is None and limit is not None and _num(bucket.get("remaining")) is not None:
        used = max(0.0, limit - _num(bucket.get("remaining")))
    resets = _when(payload.get("quota_reset_date_utc")) or _when(payload.get("quota_reset_date"))
    window = _copilot_window(used, limit, bucket.get("percent_remaining"), label, resets)
    if window is None:
        raise Unavailable(ERROR, "GitHub sent Copilot usage bighelp couldn't read.")
    report.windows.append(window)
    overage = _num(bucket.get("overage_count"))
    if overage:
        report.facts.append(("Over the limit", f"{int(overage):,}"))
    return report


def copilot_from_cli(cli: str, token: str | None, source: _Source) -> Report:
    """The Copilot CLI's ``account.getQuota``. Its counts are rounded, so the report says approximate."""
    argv = [cli, "--headless", "--stdio", "--no-auto-update", "--log-level", "error"]
    env = _child_env()
    if token:
        argv += ["--auth-token-env", "COPILOT_SDK_AUTH_TOKEN"]
        env["COPILOT_SDK_AUTH_TOKEN"] = token
    try:
        with Rpc(argv, framed=True, env=env) as rpc:
            if not rpc.call(1, "connect").get("ok"):
                raise RpcError("connect")
            quota = rpc.call(2, "account.getQuota")
    except TimeoutError:
        raise Unavailable(ERROR, "The Copilot CLI took too long to answer.") from None
    except RpcError:
        raise Unavailable(SIGN_IN, "Sign in to GitHub Copilot on this computer (run copilot login) to see your usage.") from None
    snapshots = quota.get("quotaSnapshots")
    bucket = snapshots.get("premium_interactions") if isinstance(snapshots, dict) else None
    if not isinstance(bucket, dict):
        raise Unavailable(ERROR, "The Copilot CLI didn't send usage.")
    report = Report(source.id, source.name, manage_url=source.manage_url, via=source.via, approximate=True)
    label = "Monthly credits" if bucket.get("tokenBasedBilling") else "Premium requests"
    if bucket.get("isUnlimitedEntitlement"):
        report.facts.append((label, "Unlimited"))
        return report
    window = _copilot_window(_num(bucket.get("usedRequests")), _num(bucket.get("entitlementRequests")),
                             bucket.get("remainingPercentage"), label, None)
    if window is None:
        raise Unavailable(ERROR, "The Copilot CLI sent usage bighelp couldn't read.")
    report.windows.append(window)
    return report


def detect_copilot(active: str | None) -> _Source | None:
    cli = find_cli("copilot")
    hermes = active == "copilot" or bool(hermes_env("COPILOT_GITHUB_TOKEN"))
    # A GitHub CLI login alone might not include Copilot; fetch() drops it when GitHub says so.
    gh = find_cli("gh")
    if not (cli or hermes or gh):
        return None
    source = _Source("copilot", "GitHub Copilot", _via(bool(cli or gh), hermes), lambda: [],
                     "https://github.com/settings/copilot")

    def fetch() -> list[Report]:
        token = copilot_token()
        problem: Unavailable | None = None
        rejected = False
        if token:
            try:
                return [copilot_from_api(http_json(_COPILOT_URL, {"Authorization": f"Bearer {token}",
                                                                  "X-GitHub-Api-Version": "2025-04-01"}), source)]
            except HTTPStatus as error:
                if error.code in (403, 404) and not (cli or hermes):
                    return []  # a GitHub login without Copilot: not a Copilot user
                rejected = error.code == 401
                problem = (Unavailable(SIGN_IN, "GitHub didn't accept the saved sign-in. Sign in to Copilot again.")
                           if rejected else Unavailable(ERROR, "GitHub's Copilot usage service answered with an error."))
            except Unavailable as unavailable:
                problem = unavailable
        if cli:  # the CLI's own login when GitHub turned the token down
            return [copilot_from_cli(cli, None if rejected else token, source)]
        if problem:
            raise problem
        if not hermes:
            return []
        raise Unavailable(SIGN_IN, "Sign in to GitHub Copilot on this computer to see your usage.")

    source.fetch = fetch
    return source


# ---- Google AI Studio / Gemini ------------------------------------------------------------------

def detect_gemini(active: str | None) -> _Source | None:
    cli = find_cli("gemini")
    creds = hermes_key("gemini")
    if not (cli or creds):
        return None
    name = "Google AI Studio" if creds else "Gemini CLI"
    source = _Source("gemini", name, _via(bool(cli), bool(creds)), lambda: [], "https://aistudio.google.com/usage")

    def fetch() -> list[Report]:
        if not creds:
            raise Unavailable(NOT_SHARED, "Gemini CLI shows its limits inside the CLI. Run /stats there.")
        try:
            http_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1", {"x-goog-api-key": creds[0]})
        except HTTPStatus as error:
            if error.code in (400, 401, 403):
                raise Unavailable(SIGN_IN, "Google didn't accept the Gemini API key saved in Hermes.") from None
            raise Unavailable(ERROR, "Google's Gemini API answered with an error.") from None
        # Google has no API for AI Studio usage; the key works, so point to where the numbers live.
        raise Unavailable(NOT_SHARED, "Your API key works. Google shows AI Studio usage only on its website.")

    source.fetch = fetch
    return source


# ---- DeepSeek ----------------------------------------------------------------------------------

def detect_deepseek(active: str | None) -> _Source | None:
    creds = hermes_key("deepseek")
    if not creds:
        return None
    source = _Source("deepseek", "DeepSeek", ("hermes",), lambda: [], "https://platform.deepseek.com/usage")

    def fetch() -> list[Report]:
        try:
            payload = http_json("https://api.deepseek.com/user/balance", {"Authorization": f"Bearer {creds[0]}"})
        except HTTPStatus as error:
            if error.code in (401, 403):
                raise Unavailable(SIGN_IN, "DeepSeek didn't accept the API key saved in Hermes.") from None
            raise Unavailable(ERROR, "DeepSeek's balance service answered with an error.") from None
        report = Report(source.id, source.name, manage_url=source.manage_url, via=source.via)
        infos = payload.get("balance_infos")
        for info in infos if isinstance(infos, list) else ():
            if not isinstance(info, dict):
                continue
            currency, total = info.get("currency"), _num(info.get("total_balance"))
            if total is None:
                continue
            report.facts.append(("Balance", _money(total, currency)))
            for key, label in (("granted_balance", "Granted"), ("topped_up_balance", "Topped up")):
                amount = _num(info.get(key))
                if amount:
                    report.facts.append((label, _money(amount, currency)))
        if payload.get("is_available") is False:
            report.message = "The balance is too low for API calls. Top up on DeepSeek."
        if not report.facts:
            raise Unavailable(ERROR, "DeepSeek didn't send a balance.")
        return [report]

    source.fetch = fetch
    return source


# ---- OpenRouter --------------------------------------------------------------------------------

def detect_openrouter(active: str | None) -> _Source | None:
    creds = hermes_key("openrouter")
    if not creds:
        return None
    base = creds[1] if creds[1].startswith("https://openrouter.ai/") else "https://openrouter.ai/api/v1"
    source = _Source("openrouter", "OpenRouter", ("hermes",), lambda: [], "https://openrouter.ai/settings/credits")

    def fetch() -> list[Report]:
        auth = {"Authorization": f"Bearer {creds[0]}"}
        try:
            key = http_json(f"{base}/key", auth).get("data")
        except HTTPStatus as error:
            if error.code in (401, 403):
                raise Unavailable(SIGN_IN, "OpenRouter didn't accept the API key saved in Hermes.") from None
            raise Unavailable(ERROR, "OpenRouter answered with an error.") from None
        key = key if isinstance(key, dict) else {}
        try:
            credits = http_json(f"{base}/credits", auth).get("data")
        except (HTTPStatus, Unavailable):
            credits = None  # some keys can't read the account balance; the key's own numbers still show
        report = Report(source.id, source.name, plan="Free tier" if key.get("is_free_tier") else None,
                        manage_url=source.manage_url, via=source.via)
        if isinstance(credits, dict):
            total, spent = _num(credits.get("total_credits")), _num(credits.get("total_usage"))
            if total is not None and spent is not None:
                report.facts.append(("Balance", _money(max(0.0, total - spent))))
        limit, left = _num(key.get("limit")), _num(key.get("limit_remaining"))
        if limit and left is not None and 0 <= left <= limit:
            reset = str(key.get("limit_reset") or "").strip()
            report.windows.append(Window(f"Key limit ({reset})" if reset else "Key limit",
                                         (limit - left) / limit * 100, detail=f"{_money(left)} of {_money(limit)} left"))
        for field_name, label in (("usage_daily", "Spent today"), ("usage_weekly", "Spent this week"),
                                  ("usage_monthly", "Spent this month")):
            amount = _num(key.get(field_name))
            if amount is not None:
                report.facts.append((label, _money(amount)))
        if not report.facts and not report.windows:
            raise Unavailable(ERROR, "OpenRouter didn't send usage.")
        return [report]

    source.fetch = fetch
    return source


# ---- OpenCode (local stats from the CLI; OpenCode Go limits through Hermes) ----------------------

_STAT_ROW = re.compile(r"^\s*│\s*([A-Za-z][A-Za-z /]*?)\s{2,}(\S.*?)\s*│\s*$")


def opencode_stats(cli: str, days: int) -> dict[str, str]:
    output = run([cli, "stats", "--pure", "--days", str(days)],
                 env=_child_env(OPENCODE_DISABLE_AUTOUPDATE="1")) or ""
    rows = {}
    for line in output.splitlines():
        match = _STAT_ROW.match(line)
        if match:
            rows.setdefault(match.group(1).strip(), match.group(2).strip())
    return rows


def _snapshot_windows(snapshot: Any) -> list[Window]:
    windows = []
    for window in getattr(snapshot, "windows", ()) or ():
        used = _percent(getattr(window, "used_percent", None))
        if used is not None:
            windows.append(Window(str(window.label), used, getattr(window, "reset_at", None),
                                  getattr(window, "detail", None)))
    return windows


def detect_opencode(active: str | None) -> _Source | None:
    cli = find_cli("opencode")
    go, zen = hermes_key("opencode-go"), hermes_key("opencode-zen")
    if not (cli or go or zen):
        return None
    source = _Source("opencode", "OpenCode", _via(bool(cli), bool(go or zen)), lambda: [], "https://opencode.ai/auth")

    def fetch() -> list[Report]:
        report = Report(source.id, source.name, plan="Go" if go else None, manage_url=source.manage_url, via=source.via)
        if go:
            try:
                report.windows = _snapshot_windows(hermes_account_usage("opencode-go"))
            except Exception:  # noqa: BLE001
                report.windows = []
        if cli:
            results: dict[int, dict[str, str]] = {}
            threads = [threading.Thread(target=lambda d=d: results.__setitem__(d, opencode_stats(cli, d)), daemon=True)
                       for d in (7, 30)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(_CALL_SECONDS + 1)
            for days in (7, 30):
                rows = results.get(days) or {}
                cost, sessions = rows.get("Total Cost"), rows.get("Sessions")
                if cost:
                    report.facts.append((f"Last {days} days", f"{cost} · {sessions} sessions" if sessions else cost))
            rows = results.get(30) or {}
            if rows.get("Input") and rows.get("Output"):
                report.facts.append(("Tokens (30 days)", f"{rows['Input']} in · {rows['Output']} out"))
        if report.windows or report.facts:
            return [report]
        if go:
            raise Unavailable(ERROR, "OpenCode Go didn't send usage.")
        if zen:
            raise Unavailable(NOT_SHARED, "OpenCode Zen shows its balance on opencode.ai.")
        raise Unavailable(ERROR, "OpenCode didn't share its usage stats.")

    source.fetch = fetch
    return source


# ---- Nous Portal -------------------------------------------------------------------------------

def detect_nous(active: str | None) -> _Source | None:
    try:
        from hermes_cli.auth import get_provider_auth_state
        token = (get_provider_auth_state("nous") or {}).get("access_token")
    except Exception:  # noqa: BLE001
        return None
    if not (isinstance(token, str) and token.strip()):
        return None
    source = _Source("nous", "Nous Portal", ("hermes",), lambda: [], "https://portal.nousresearch.com")

    def fetch() -> list[Report]:
        try:
            from hermes_cli.nous_account import get_nous_portal_account_info, nous_portal_topup_url
            account = get_nous_portal_account_info(force_fresh=True)
        except Exception:  # noqa: BLE001
            raise Unavailable(ERROR, "Couldn't reach Nous Portal.") from None
        if account is None or not getattr(account, "logged_in", False):
            raise Unavailable(SIGN_IN, "Sign in to Nous Portal again (hermes auth add nous).")
        sub, access = getattr(account, "subscription", None), getattr(account, "paid_service_access_info", None)
        try:
            manage = nous_portal_topup_url(account)
        except Exception:  # noqa: BLE001
            manage = source.manage_url
        report = Report(source.id, source.name, plan=_title(getattr(sub, "plan", None)), manage_url=manage, via=source.via)
        cap, left = _num(getattr(sub, "monthly_credits", None)), _num(getattr(sub, "credits_remaining", None))
        if cap and left is not None and 0 <= left <= cap:
            report.windows.append(Window("Monthly credits", (cap - left) / cap * 100,
                                         _when(getattr(sub, "current_period_end", None)),
                                         f"{_money(left)} of {_money(cap)} left"))
        for attr, label in (("subscription_credits_remaining", "Subscription credits"),
                            ("purchased_credits_remaining", "Top-up credits"), ("total_usable_credits", "Total usable")):
            amount = _num(getattr(access, attr, None))
            if amount is not None:
                report.facts.append((label, _money(amount)))
        if getattr(account, "paid_service_access", None) is False:
            report.message = "Out of credits. Top up to keep using Nous models."
        if not report.windows and not report.facts:
            raise Unavailable(ERROR, "Nous Portal didn't send a balance.")
        return [report]

    source.fetch = fetch
    return source


# ---- Any other Hermes provider plugin with a usage hook ------------------------------------------

_BUILT_IN = frozenset({"anthropic", "openai-codex", "copilot", "copilot-acp", "gemini", "deepseek", "openrouter",
                       "opencode-zen", "opencode-go", "nous"})


def detect_hermes_hooks(active: str | None) -> list[_Source]:
    try:
        from providers import list_providers
        from providers.base import ProviderProfile
    except ImportError:
        return []
    sources = []
    for profile in list_providers():
        name = str(getattr(profile, "name", "") or "")
        if not name or name in _BUILT_IN or type(profile).fetch_account_usage is ProviderProfile.fetch_account_usage:
            continue
        if not hermes_key(name):
            continue
        source = _Source(f"hermes-{name}", getattr(profile, "display_name", "") or _title(name) or name, ("hermes",),
                         lambda: [])

        def fetch(source: _Source = source, name: str = name) -> list[Report]:
            snapshot = hermes_account_usage(name)
            if snapshot is None:
                raise Unavailable(ERROR, "Hermes couldn't read this provider's usage.")
            report = Report(source.id, source.name, plan=getattr(snapshot, "plan", None), via=source.via,
                            windows=_snapshot_windows(snapshot))
            for line in getattr(snapshot, "details", ()) or ():
                label, _, value = str(line).partition(": ")
                report.facts.append((label, value) if value else ("", label))
            reason = getattr(snapshot, "unavailable_reason", None)
            if reason and not report.windows and not report.facts:
                raise Unavailable(NOT_SHARED, str(reason))
            return [report]

        source.fetch = fetch
        sources.append(source)
    return sources


# ---- Collection -------------------------------------------------------------------------------

def detect(active: str | None) -> list[_Source]:
    sources = []
    for detector in (detect_claude, detect_codex, detect_copilot, detect_gemini, detect_opencode,
                     detect_openrouter, detect_deepseek, detect_nous, detect_hermes_hooks):
        try:
            found = detector(active)
        except Exception as error:  # noqa: BLE001 - one broken detector never hides the others
            logger.warning("bighelp usage: detection failed (%s)", type(error).__name__)
            continue
        sources.extend(found if isinstance(found, list) else [found] if found else [])
    return sources


def _failed(source: _Source, status: str, message: str) -> Report:
    return Report(source.id, source.name, status=status, message=message, manage_url=source.manage_url, via=source.via)


def collect(*, deadline: float = DEADLINE_SECONDS) -> list[dict[str, Any]]:
    """Every detected tool's usage, fetched in parallel. Slow ones report a timeout instead of holding the rest."""
    active = usage_id(active_provider())
    sources = detect(active)
    results: dict[int, list[Report]] = {}

    def fetch(index: int, source: _Source) -> None:
        try:
            results[index] = source.fetch()
        except Unavailable as problem:
            results[index] = [_failed(source, problem.status, problem.message)]
        except Exception as error:  # noqa: BLE001 - never echo exception text; it may hold paths or secrets
            logger.warning("bighelp usage: %s failed (%s)", source.id, type(error).__name__)
            results[index] = [_failed(source, ERROR, "Couldn't read usage right now.")]

    threads = []
    for index, source in enumerate(sources):
        context = contextvars.copy_context()  # keep the request's Hermes profile in each worker
        thread = threading.Thread(target=context.run, args=(fetch, index, source), daemon=True,
                                  name=f"bighelp-usage-{source.id}")
        thread.start()
        threads.append(thread)
    end = time.monotonic() + deadline
    for thread in threads:
        thread.join(max(0.0, end - time.monotonic()))
    reports: list[Report] = []
    for index, source in enumerate(sources):
        reports.extend(results.get(index, [_failed(source, ERROR, f"{source.name} took too long to answer.")]))
    if active and any(report.id == f"{active}-hermes" for report in reports):
        active = f"{active}-hermes"  # Hermes chats with a different account than the CLI
    reports.sort(key=lambda report: report.id != active)  # stable: the provider Hermes chats with first
    return [report.payload(report.id == active) for report in reports]


_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def usage(agent_id: str, *, refresh: bool = False, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """Cached for five minutes. ``refresh`` skips the cache, but not more than once every 15 seconds."""
    with _locks_guard:
        lock = _locks.setdefault(agent_id, threading.Lock())
    with lock:  # concurrent requests share one fetch
        cached = _cache.get(agent_id)
        if cached:
            age = clock() - cached[0]
            if 0 <= age < (MIN_REFRESH_SECONDS if refresh else CACHE_SECONDS):
                return {**cached[1], "cached": True}
        now = clock()
        value = {"fetchedAt": _iso(datetime.fromtimestamp(now, tz=timezone.utc)), "providers": collect()}
        _cache[agent_id] = (now, value)
        return {**value, "cached": False}
