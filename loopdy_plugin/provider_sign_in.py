"""Sign in to AI provider accounts from the bighelp app, with each provider's own sign-in tool.

Some accounts only sign in from a terminal on this computer: GitHub's Copilot CLI, Anthropic's
Claude Code, and the GitHub login `hermes model` runs for Copilot. From the app's Provider Keys
screen, this module runs that same command in a private terminal here and passes back only what a
person needs: the provider's sign-in link, the one-time code to enter there, and whether it worked.
When the tool asks for a code from the provider's page, the app sends it back and it's typed in.

Provider terms: the provider's own client does every sign-in (Claude Code for Claude accounts,
GitHub's Copilot CLI or Hermes' own `hermes model` login for Copilot). bighelp never contacts a
sign-in service, never builds an OAuth request and never reads another tool's credential store. A
sign-in its provider has ended is listed as retired instead of started.

Adding a provider: add a ``Recipe`` to ``RECIPES`` with the provider's Hermes id, its own CLI and
arguments, the domains its links may use, and whether its page approves by itself (``DEVICE``) or
shows a code to paste back (``PASTE``). Commands are fixed here; the phone only names a provider.

Privacy: terminal output stays in memory for one sign-in and is never logged or returned. A token a
tool prints for the person to keep (``claude setup-token``) is saved straight to this profile's
Hermes env, as the person would, and dropped.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid

from .provider_usage import find_cli, _search_path

CAPABILITY = "native-provider-sign-in-v1"
DEVICE, PASTE = "device", "paste"
STARTING, WAITING, NEEDS_CODE, FINISHING = "starting", "waiting", "needsCode", "finishing"
SIGNED_IN, FAILED, EXPIRED, CANCELLED = "signedIn", "failed", "expired", "cancelled"
_ENDED = frozenset({SIGNED_IN, FAILED, EXPIRED, CANCELLED})

SESSION_SECONDS = 15 * 60
START_WAIT_SECONDS = 12.0
_KEEP_ENDED_SECONDS = 5 * 60
_MAX_OUTPUT = 256 * 1024
_MAX_SESSIONS = 4
_MAX_CODE = 2048
_STATUS_CACHE_SECONDS = 60.0
_HELPER = Path(__file__).with_name("provider_sign_in_helper.py")

logger = logging.getLogger("hermes.plugins.bighelp")


class SignInError(Exception):
    """An expected refusal; ``code`` is a fixed string the route maps to a status."""

    def __init__(self, code: str, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Recipe:
    provider_id: str
    name: str
    client: str
    flow: str
    link_hosts: tuple[str, ...]
    command: Callable[[], list[str] | None]
    install: str = ""
    docs: str = ""
    code: re.Pattern[str] | None = None
    rejected: re.Pattern[str] | None = None
    env: Callable[[str], dict[str, str]] | None = None
    drop_env: tuple[str, ...] = ()
    keep_token: tuple[re.Pattern[str], str] | None = None
    signed_in: Callable[[], bool | None] | None = None
    retired: str = ""
    replacement_key: str = ""


def available() -> bool:
    """A private terminal needs POSIX; profile helpers scope saved tokens to the chosen profile."""
    if os.name != "posix":
        return False
    try:
        import pty
        from hermes_cli.profiles import get_profile_dir, profile_exists
    except ImportError:
        return False
    return all(map(callable, (pty.openpty, get_profile_dir, profile_exists)))


# ---- Commands ---------------------------------------------------------------------------------

def _cli(name: str, *arguments: str, override: tuple[str, ...] = ()) -> Callable[[], list[str] | None]:
    """The provider's CLI from an override setting (absolute path or bare name), else PATH."""
    def command() -> list[str] | None:
        for variable in override:
            value = os.environ.get(variable, "").strip()
            if not value:
                continue
            if os.path.isabs(value):
                if os.path.isfile(value) and os.access(value, os.X_OK):
                    return [value, *arguments]
                continue
            if re.fullmatch(r"[A-Za-z0-9._-]{1,64}", value) and (found := find_cli(value)):
                return [found, *arguments]
        found = find_cli(name)
        return [found, *arguments] if found else None
    return command


def _hermes_helper(flow: str, module: str) -> Callable[[], list[str] | None]:
    """A Hermes login run by ``provider_sign_in_helper.py`` with this Python, when Hermes has it."""
    def command() -> list[str] | None:
        try:
            __import__(module)
        except ImportError:
            return None
        return [sys.executable, str(_HELPER), flow]
    return command


def _profile_home(agent_id: str) -> dict[str, str]:
    from hermes_cli.profiles import get_profile_dir
    # The helper imports Hermes from this process's path and saves into the chosen profile.
    return {"HERMES_HOME": str(get_profile_dir(agent_id)),
            "PYTHONPATH": os.pathsep.join(path for path in sys.path if path),
            "HERMES_DISABLE_LAZY_INSTALLS": "1"}


def _claude_env(config_variable: str = "") -> Callable[[str], dict[str, str]]:
    def env(_: str) -> dict[str, str]:
        extra = {"DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"}
        if config_variable and (config := os.environ.get(config_variable, "").strip()):
            extra["CLAUDE_CONFIG_DIR"] = config
        return extra
    return env


_status_cache: dict[tuple[str, ...], tuple[float, bool | None]] = {}
_status_lock = threading.Lock()


def _claude_signed_in(command: Callable[[], list[str] | None], config_variable: str = "") -> Callable[[], bool | None]:
    """Claude Code's own ``auth status`` (local, no network); None when it can't say."""
    def signed_in() -> bool | None:
        argv = command()
        if argv is None:
            return None
        env = {**os.environ, "PATH": _search_path(), **_claude_env(config_variable)("")}
        key = (*argv, env.get("CLAUDE_CONFIG_DIR", ""))
        now = time.monotonic()
        with _status_lock:
            cached = _status_cache.get(key)
            if cached and now - cached[0] < _STATUS_CACHE_SECONDS:
                return cached[1]
        try:
            result = subprocess.run([*argv, "auth", "status", "--json"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=8, env=env,
                                    stdin=subprocess.DEVNULL, cwd=str(Path.home()), check=False)
            value = json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}
            state = value.get("loggedIn") if isinstance(value.get("loggedIn"), bool) else None
        except (OSError, ValueError, subprocess.SubprocessError):
            state = None
        with _status_lock:
            _status_cache[key] = (now, state)
        return state
    return signed_in


_GITHUB_CODE = re.compile(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b")
_CLAUDE_HOSTS = ("claude.com", "claude.ai", "anthropic.com")
_CLAUDE_INSTALL = "npm install -g @anthropic-ai/claude-code"
_CLAUDE_DOCS = "https://docs.claude.com/en/docs/claude-code/setup"
# `claude setup-token` prints a long-lived token for the person to keep; this is its shape.
_SETUP_TOKEN = re.compile(r"(sk-ant-oat01-[A-Za-z0-9_-]{16,512})(?=[^A-Za-z0-9_-])")
# What Claude Code prints when the pasted code doesn't work; `setup-token` then waits for Enter.
_CLAUDE_REJECTED = re.compile(r"OAuth error|Press Enter to retry|Invalid code", re.IGNORECASE)
_claude = _cli("claude")
_directsdk = _cli("claude", override=("CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND",))
_copilot = _cli("copilot", override=("HERMES_COPILOT_ACP_COMMAND", "COPILOT_CLI_PATH"))

RECIPES: tuple[Recipe, ...] = (
    Recipe(
        provider_id="copilot-acp", name="GitHub Copilot (ACP)", client="GitHub Copilot CLI", flow=DEVICE,
        link_hosts=("github.com",), command=lambda: (argv + ["login", "--device-code"]) if (argv := _copilot()) else None,
        install="npm install -g @github/copilot",
        docs="https://docs.github.com/en/copilot/how-tos/set-up/install-copilot-cli", code=_GITHUB_CODE,
        # A token in the environment would stand in for the account the person signs in to.
        drop_env=("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")),
    Recipe(
        provider_id="copilot", name="GitHub Copilot", client="Hermes", flow=DEVICE, link_hosts=("github.com",),
        command=_hermes_helper("copilot", "hermes_cli.copilot_auth"),
        docs="https://docs.github.com/en/copilot", code=_GITHUB_CODE, env=_profile_home),
    Recipe(
        provider_id="claude-code", name="Claude Code", client="Claude Code", flow=PASTE, link_hosts=_CLAUDE_HOSTS,
        command=lambda: (argv + ["auth", "login", "--claudeai"]) if (argv := _claude()) else None,
        install=_CLAUDE_INSTALL, docs=_CLAUDE_DOCS, rejected=_CLAUDE_REJECTED, env=_claude_env(),
        signed_in=_claude_signed_in(_claude)),
    Recipe(
        provider_id="claude-subscription-directsdk-experimental", name="Claude Subscription DirectSDK",
        client="Claude Code", flow=PASTE, link_hosts=_CLAUDE_HOSTS,
        command=lambda: (argv + ["auth", "login", "--claudeai"]) if (argv := _directsdk()) else None,
        install=_CLAUDE_INSTALL, docs=_CLAUDE_DOCS, rejected=_CLAUDE_REJECTED,
        env=_claude_env("CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR"),
        signed_in=_claude_signed_in(_directsdk, "CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR")),
    # Hermes keeps its own Claude login in the terminal on purpose; Claude Code's long-lived token
    # is the route Hermes documents for Claude subscriptions, and Claude Code mints it.
    Recipe(
        provider_id="anthropic", name="Anthropic Account", client="Claude Code", flow=PASTE,
        link_hosts=_CLAUDE_HOSTS, command=lambda: (argv + ["setup-token"]) if (argv := _claude()) else None,
        install=_CLAUDE_INSTALL, docs=_CLAUDE_DOCS, rejected=_CLAUDE_REJECTED, env=_claude_env(),
        keep_token=(_SETUP_TOKEN, "CLAUDE_CODE_OAUTH_TOKEN")),
    Recipe(
        provider_id="qwen-oauth", name="Qwen OAuth", client="Qwen Code", flow=DEVICE, link_hosts=("qwen.ai",),
        command=lambda: None, docs="https://github.com/QwenLM/qwen-code",
        retired="Qwen stopped offering Qwen OAuth sign-in in April 2026. Use a Qwen Cloud API key instead.",
        replacement_key="DASHSCOPE_API_KEY"),
)
_BY_ID = {recipe.provider_id: recipe for recipe in RECIPES}


def recipe(provider_id: str) -> Recipe | None:
    return _BY_ID.get(provider_id)


def providers() -> list[dict]:
    """Every sign-in this host can run, or why it can't, for the app's Provider Keys screen."""
    rows = []
    for item in RECIPES:
        row: dict = {"providerId": item.provider_id, "name": item.name, "client": item.client, "flow": item.flow}
        if item.docs:
            row["docsURL"] = item.docs
        if item.retired:
            row.update(state="retired", message=item.retired)
            if item.replacement_key:
                row["replacementKey"] = item.replacement_key
        elif item.command() is None:
            row.update(state="notInstalled", message=f"{item.client} isn't installed on this computer.")
            if item.install:
                row["installCommand"] = item.install
        else:
            row["state"] = "ready"
            if item.signed_in is not None and (state := item.signed_in()) is not None:
                row["signedIn"] = state
        rows.append(row)
    return rows


# ---- Terminal output --------------------------------------------------------------------------

_OSC = re.compile(r"\x1b\]([^\x07\x1b]*)(?:\x07|\x1b\\)")
_CURSOR_MOVE = re.compile(r"\x1b\[\d*[CG]")
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESCAPE = re.compile(r"\x1b[()][0-9A-Za-z]|\x1b[@-_]")
_URL = re.compile(r"https://[^\s\"'<>\\\x00-\x1f\x7f]+")


def _visible(raw: str) -> tuple[str, list[str]]:
    """Terminal output as plain text, plus the targets of any hyperlinks it drew."""
    links = []
    for body in _OSC.findall(raw):
        parts = body.split(";", 2)
        if len(parts) == 3 and parts[0] == "8" and parts[2]:
            links.append(parts[2])
    text = _OSC.sub("", raw)
    text = _CURSOR_MOVE.sub(" ", text)
    text = _ESCAPE.sub("", _CSI.sub("", text))
    return text.replace("\r", ""), links


def allowed_link(url: str, hosts: tuple[str, ...]) -> bool:
    """An https link on one of the provider's own domains, nothing else."""
    if len(url) > 4096:
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and parts.username is None and parts.password is None
            and port in (None, 443) and any(host == item or host.endswith("." + item) for item in hosts))


def valid_code(code: str) -> bool:
    """A code copied from a provider's page: one line of visible ASCII."""
    return 0 < len(code) <= _MAX_CODE and all(0x21 <= ord(character) <= 0x7e for character in code)


# ---- Sessions ---------------------------------------------------------------------------------

_BROWSER_NAMES = ("open", "xdg-open", "sensible-browser", "x-www-browser", "www-browser", "wslview",
                  "gnome-open", "kde-open")


class _Session:
    """One sign-in running in a private terminal."""

    def __init__(self, item: Recipe, agent_id: str, argv: list[str]):
        self.id = str(uuid.uuid4())
        self.recipe = item
        self.agent_id = agent_id
        self.status = STARTING
        self.link: str | None = None
        self.code: str | None = None
        self.message = ""
        self.started = time.monotonic()
        self.ended: float | None = None
        self._argv = argv
        # How much output there was when the code went in; only later output can reject it.
        self._seen = 0
        self._submitted_at: int | None = None
        self._condition = threading.Condition()
        self._process: subprocess.Popen | None = None
        self._terminal: int | None = None
        self._shims: tempfile.TemporaryDirectory | None = None

    # The person opens links on the phone, so the tool's attempt to open a browser here does nothing.
    def _environment(self) -> dict[str, str]:
        self._shims = tempfile.TemporaryDirectory(prefix="bighelp-sign-in-")
        for name in _BROWSER_NAMES:
            path = Path(self._shims.name, name)
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o700)
        env = {key: value for key, value in os.environ.items() if key not in self.recipe.drop_env}
        env.update(PATH=os.pathsep.join((self._shims.name, _search_path())),
                   BROWSER=str(Path(self._shims.name, "xdg-open")), TERM="xterm-256color",
                   COLUMNS="1000", LINES="50")
        for key in ("CI", "NO_COLOR"):
            env.pop(key, None)
        if self.recipe.env is not None:
            env.update(self.recipe.env(self.agent_id))
        return env

    def start(self) -> None:
        import fcntl
        import pty
        import termios
        terminal, child = pty.openpty()
        # A wide terminal keeps long links and tokens on one line.
        fcntl.ioctl(child, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 1000, 0, 0))
        try:
            self._process = subprocess.Popen(
                self._argv, stdin=child, stdout=child, stderr=child, env=self._environment(),
                cwd=str(Path.home()), start_new_session=True, close_fds=True)
        except (OSError, ValueError):
            os.close(terminal)
            self._cleanup()
            raise SignInError("sign_in_start_failed", f"{self.recipe.client} couldn't start on this computer.")
        finally:
            os.close(child)
        self._terminal = terminal
        threading.Thread(target=self._read, daemon=True, name="bighelp-sign-in").start()

    def _read(self) -> None:
        raw = ""
        terminal = self._terminal
        while True:
            if time.monotonic() - self.started > SESSION_SECONDS:
                self._end(EXPIRED, "The sign-in took too long. Try again.")
                break
            try:
                ready, _, _ = select.select([terminal], [], [], 0.5)
                chunk = os.read(terminal, 65536) if ready else None
            except OSError:
                chunk = b""
            if chunk is None:
                if self._process is not None and self._process.poll() is not None and not ready:
                    chunk = b""
                else:
                    continue
            if not chunk:
                break
            raw += chunk.decode("utf-8", "replace")
            if len(raw) > _MAX_OUTPUT:
                self._end(FAILED, f"{self.recipe.client} printed more than expected. Try again.")
                break
            self._scan(raw)
            with self._condition:
                if self.status in _ENDED:
                    break
        self._finish()

    def _scan(self, raw: str) -> None:
        text, hyperlinks = _visible(raw)
        with self._condition:
            self._seen = len(text)
            if self.status in _ENDED:
                return
            if self.link is None:
                self.link = next((url for url in (*hyperlinks, *_URL.findall(text))
                                  if allowed_link(url, self.recipe.link_hosts)), None)
            if self.recipe.code is not None and self.code is None and (match := self.recipe.code.search(text)):
                self.code = match.group(1)
            ready = self.link is not None and (self.recipe.code is None or self.code is not None)
            if ready and self.status == STARTING:
                self.status = NEEDS_CODE if self.recipe.flow == PASTE else WAITING
                self._condition.notify_all()
            keep = self.recipe.keep_token
            token = keep[0].search(text) if keep is not None and self.status == FINISHING else None
            after = text[self._submitted_at:] if self._submitted_at is not None else ""
            rejected = (token is None and self.status == FINISHING and self.recipe.rejected is not None
                        and self.recipe.rejected.search(after) is not None)
        if token is not None:
            self._keep(keep[1], token.group(1))
        elif rejected:
            self._end(FAILED, f"{self.recipe.client} didn't accept that code. Try again.")
            self._stop()

    def _keep(self, variable: str, token: str) -> None:
        """Save a printed token where the person would put it, then stop the tool."""
        from .workspace_capabilities import profile_scope
        try:
            with profile_scope(self.agent_id):
                try:
                    from hermes_cli.config import save_env_value_secure as save
                except ImportError:
                    from hermes_cli.config import save_env_value as save
                save(variable, token)
        except Exception as error:  # noqa: BLE001 - the class name is safe to log, the text isn't
            logger.warning("bighelp sign-in: saving %s failed (%s)", self.recipe.provider_id, type(error).__name__)
            self._end(FAILED, "Hermes couldn't save the sign-in. Try again.")
        else:
            self._end(SIGNED_IN, "")
        self._stop()

    def _finish(self) -> None:
        process = self._process
        code = None
        if process is not None:
            try:
                code = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._stop()
        with self._condition:
            done = self.status in _ENDED
        if not done:
            if code == 0 and self.recipe.keep_token is None:
                with _status_lock:
                    _status_cache.clear()
                verified = self.recipe.signed_in() if self.recipe.signed_in is not None else None
                if verified is False:
                    self._end(FAILED, f"{self.recipe.client} didn't finish signing in. Try again.")
                else:
                    self._end(SIGNED_IN, "")
            elif self.status == FINISHING and self.recipe.flow == PASTE:
                self._end(FAILED, f"{self.recipe.client} didn't accept that code. Try again.")
            else:
                self._end(FAILED, f"{self.recipe.client} didn't finish signing in. Try again.")
        self._cleanup()

    def _end(self, status: str, message: str) -> None:
        with self._condition:
            if self.status in _ENDED:
                return
            self.status = status
            self.message = message
            self.ended = time.monotonic()
            self._condition.notify_all()
        if status == SIGNED_IN:
            with _status_lock:
                _status_cache.clear()
        logger.info("bighelp sign-in: %s %s", self.recipe.provider_id, status)

    def _stop(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        for sent in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sent)
            except (OSError, ProcessLookupError):
                return
            try:
                process.wait(timeout=3)
                return
            except subprocess.TimeoutExpired:
                continue

    def _cleanup(self) -> None:
        if self._terminal is not None:
            try:
                os.close(self._terminal)
            except OSError:
                pass
            self._terminal = None
        if self._shims is not None:
            self._shims.cleanup()
            self._shims = None

    def wait_for_start(self, seconds: float) -> None:
        with self._condition:
            timed_out = not self._condition.wait_for(lambda: self.status != STARTING, timeout=seconds)
        if timed_out:
            self._end(FAILED, f"{self.recipe.client} didn't show a sign-in link. Try again.")
            self._stop()

    def submit(self, code: str) -> None:
        with self._condition:
            if self.status != NEEDS_CODE or self._terminal is None:
                raise SignInError("sign_in_not_waiting", "This sign-in isn't waiting for a code.")
            self.status = FINISHING
            self._submitted_at = self._seen
            terminal = self._terminal
        try:
            os.write(terminal, code.encode("ascii"))
            # A pause lets full-screen tools take the typed code before Return submits it.
            time.sleep(0.3)
            os.write(terminal, b"\r")
        except OSError:
            self._end(FAILED, f"{self.recipe.client} stopped before it got the code. Try again.")

    def cancel(self) -> None:
        self._end(CANCELLED, "")
        self._stop()

    def snapshot(self) -> dict:
        with self._condition:
            value = {"sessionId": self.id, "providerId": self.recipe.provider_id, "flow": self.recipe.flow,
                     "status": self.status,
                     "expiresInSeconds": max(0, int(SESSION_SECONDS - (time.monotonic() - self.started)))}
            if self.status not in _ENDED:
                if self.link:
                    value["link"] = self.link
                if self.code:
                    value["code"] = self.code
            if self.message:
                value["message"] = self.message
        return value


_sessions: dict[str, _Session] = {}
_sessions_lock = threading.Lock()


def _sweep() -> None:
    now = time.monotonic()
    with _sessions_lock:
        for key, session in list(_sessions.items()):
            if session.ended is not None and now - session.ended > _KEEP_ENDED_SECONDS:
                del _sessions[key]


def start(agent_id: str, provider_id: str, *, wait: float = START_WAIT_SECONDS) -> dict:
    """Start one provider's sign-in and wait briefly for its link."""
    _sweep()
    item = recipe(provider_id)
    if item is None:
        raise SignInError("sign_in_unavailable", "This provider can't sign in from the app.")
    if item.retired:
        raise SignInError("sign_in_retired", item.retired)
    argv = item.command()
    if argv is None:
        raise SignInError("sign_in_tool_missing", f"{item.client} isn't installed on this computer.")
    with _sessions_lock:
        # A new sign-in for the same account replaces an unfinished one.
        replaced = [session for session in _sessions.values()
                    if session.agent_id == agent_id and session.recipe.provider_id == provider_id
                    and session.status not in _ENDED]
        running = sum(1 for session in _sessions.values() if session.status not in _ENDED) - len(replaced)
        if running >= _MAX_SESSIONS:
            raise SignInError("sign_in_busy", "Too many sign-ins are running. Finish or cancel one first.")
        session = _Session(item, agent_id, argv)
        _sessions[session.id] = session
    for old in replaced:
        old.cancel()
    try:
        session.start()
    except SignInError:
        with _sessions_lock:
            _sessions.pop(session.id, None)
        raise
    session.wait_for_start(wait)
    return session.snapshot()


def _session(agent_id: str, session_id: str) -> _Session:
    with _sessions_lock:
        session = _sessions.get(session_id)
    if session is None or session.agent_id != agent_id:
        raise SignInError("sign_in_not_found", "That sign-in is no longer running. Start it again.")
    return session


def status(agent_id: str, session_id: str) -> dict:
    _sweep()
    return _session(agent_id, session_id).snapshot()


def submit(agent_id: str, session_id: str, code: str) -> dict:
    code = code.strip()
    if not valid_code(code):
        raise SignInError("sign_in_code_invalid", "That doesn't look like a sign-in code. Copy it again.")
    session = _session(agent_id, session_id)
    session.submit(code)
    return session.snapshot()


def cancel(agent_id: str, session_id: str) -> dict:
    session = _session(agent_id, session_id)
    session.cancel()
    return session.snapshot()


def cancel_all() -> None:
    with _sessions_lock:
        running = list(_sessions.values())
    for session in running:
        session.cancel()
