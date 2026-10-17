"""Composio Connect for the bighelp app (Settings › Apps › Composio Apps).

People sign in to their own Composio account in the app to find and connect apps. Agents use those apps through
Composio Connect's MCP server, which Hermes must have, turned on and signed in to the same account. This module
tells the app which Composio MCP servers an agent's Hermes config has, and can add or turn on Composio Connect.

Hermes' own MCP code does the work. Config is read and written with the helpers, profile scope and config lock
that Hermes' dashboard MCP routes use, and the app signs the server in with Hermes' MCP OAuth
(`POST /api/mcp/servers/<name>/auth`), which keeps the tokens. No Composio credential passes through here: only
server names, URLs without query or user info, and whether Hermes has saved sign-in tokens reach the app.
"""
from __future__ import annotations

from contextlib import contextmanager
import logging
from types import SimpleNamespace
from typing import Any, Callable, Iterator
import unicodedata
from urllib.parse import urlsplit, urlunsplit

CAPABILITY = "native-composio-v1"
CONNECT_HOST = "connect.composio.dev"
CONNECT_URL = "https://connect.composio.dev/mcp"
# The name a new Composio Connect server gets, then the one used when a different server already has it.
NAMES = ("composio", "composio-connect")
# Composio Connect lists many tools; Hermes' default (60 seconds) can run out while it connects.
CONNECT_TIMEOUT = 120
MAX_SERVERS = 32
MAX_NAME = 128
MAX_URL = 2_048
_DOMAIN = "composio.dev"

logger = logging.getLogger("hermes.plugins.bighelp")


class ComposioError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def _hermes() -> SimpleNamespace:
    """Hermes' MCP config helpers, as its dashboard MCP routes use them (ImportError on hosts without them)."""
    from hermes_cli.config import load_config, save_config
    from hermes_cli.mcp_config import _get_mcp_servers, _save_mcp_server
    from hermes_cli.profiles import profile_exists
    from hermes_cli.web_routers._common import _profile_scope, config_write_scope
    from tools.mcp_oauth import HermesTokenStorage
    return SimpleNamespace(load_config=load_config, save_config=save_config, servers=_get_mcp_servers,
                           save_server=_save_mcp_server, profile_exists=profile_exists, read_scope=_profile_scope,
                           write_scope=config_write_scope, token_storage=HermesTokenStorage)


def available() -> bool:
    try:
        hermes = _hermes()
    except ImportError:
        return False
    return all(callable(value) for value in vars(hermes).values())


def _enabled_check() -> Callable[[dict], bool]:
    try:
        # Hermes' one reader of `enabled` (0.22 and later), so this says what its MCP client does.
        from tools.mcp_tool_common import mcp_server_enabled
        return mcp_server_enabled
    except ImportError:
        return lambda config: config.get("enabled", True) is not False


def _plugin_server_names(servers: dict) -> set[str]:
    """Names other plugins give their MCP servers. Hermes' own add refuses these names, so this does too."""
    try:
        from tui_gateway.mcp_rpc_helpers import server_configs_with_sources
    except ImportError:
        return set()
    try:
        _merged, owners = server_configs_with_sources(servers)
    except Exception as error:
        logger.warning("bighelp composio plugin servers unreadable: %s", type(error).__name__)
        return set()
    return {name for name, plugin in owners.items() if plugin is not None}


def _name(value: Any) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_NAME:
        return None
    if any(unicodedata.category(character).startswith("C") for character in value):
        return None
    return value


def _url(value: Any) -> tuple[str, str] | None:
    """(host, URL without user info, query or fragment) for a Composio URL; None for every other URL."""
    if not isinstance(value, str) or len(value) > 8 * MAX_URL:
        return None
    try:
        parts = urlsplit(value.strip())
        host = (parts.hostname or "").rstrip(".")
        port = parts.port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if host != _DOMAIN and not host.endswith("." + _DOMAIN):
        return None
    if any(unicodedata.category(character).startswith("C") for character in parts.path):
        return None
    clean = urlunsplit((parts.scheme.lower(), host if port is None else f"{host}:{port}", parts.path, "", ""))
    return (host, clean) if len(clean) <= MAX_URL else None


def _auth(config: dict) -> str:
    auth = config.get("auth")
    # Read as Hermes' MCP client reads it; OAuth wins over headers, as in Hermes' own sign-in route.
    if isinstance(auth, str) and auth.lower().strip() == "oauth":
        return "oauth"
    headers = config.get("headers")
    return "headers" if isinstance(headers, dict) and headers else "none"


def _signed_in(hermes: SimpleNamespace, name: str) -> bool | None:
    try:
        # The file check behind Hermes' `_oauth_tokens_present`, which says True when it can't tell.
        return bool(hermes.token_storage(name).has_cached_tokens())
    except Exception as error:
        logger.warning("bighelp composio sign-in state unreadable: %s", type(error).__name__)
        return None


def _composio_servers(servers: dict) -> Iterator[tuple[str, dict, str, str]]:
    """(name, config, host, clean URL) for each Composio entry in `mcp_servers`, by name."""
    for raw_name in sorted((key for key in servers if isinstance(key, str))):
        name, config = _name(raw_name), servers[raw_name]
        if name is None or not isinstance(config, dict):
            continue
        url = _url(config.get("url"))
        if url is not None:
            yield name, config, url[0], url[1]


def _project(hermes: SimpleNamespace) -> dict:
    """The status body for the profile already in scope."""
    enabled = _enabled_check()
    servers: list[dict] = []
    for name, config, host, url in _composio_servers(hermes.servers()):
        if len(servers) == MAX_SERVERS:
            break
        auth = _auth(config)
        servers.append({
            "name": name, "url": url, "enabled": bool(enabled(config)), "auth": auth,
            "kind": "connect" if host == CONNECT_HOST else "other",
            "signedIn": _signed_in(hermes, name) if auth == "oauth" else None,
        })
    connect = [server for server in servers if server["kind"] == "connect"]
    chosen = next((server for server in connect if server["enabled"]), connect[0] if connect else None)
    return {"schemaVersion": 1, "connect": None if chosen is None else chosen["name"], "servers": servers}


def _helpers() -> SimpleNamespace:
    try:
        return _hermes()
    except ImportError:
        raise ComposioError(503, "composio_unavailable", "This Hermes can't set up Composio from the app.") from None


@contextmanager
def _scope(hermes: SimpleNamespace, scope: Callable, agent_id: str):
    if not hermes.profile_exists(agent_id):
        raise ComposioError(404, "profile_not_found", "The selected profile no longer exists.")
    entered = False
    try:
        with scope(agent_id):
            entered = True
            yield
    except Exception as error:
        # Hermes' profile scope answers a profile removed since the check with an HTTPException.
        if entered or getattr(error, "status_code", None) not in (400, 404):
            raise
        raise ComposioError(404, "profile_not_found", "The selected profile no longer exists.") from None


def status(agent_id: str) -> dict:
    hermes = _helpers()
    with _scope(hermes, hermes.read_scope, agent_id):
        return _project(hermes)


def install(agent_id: str) -> dict:
    """Turn on the agent's Composio Connect server, or add one. Running it again changes nothing."""
    hermes = _helpers()
    enabled = _enabled_check()
    # Profile scope and Hermes' config lock, as its dashboard add and enable routes take them, so this
    # check and write can't interleave with another config change.
    with _scope(hermes, hermes.write_scope, agent_id):
        servers = hermes.servers()
        connect = [(name, config) for name, config, host, _url in _composio_servers(servers) if host == CONNECT_HOST]
        if connect:
            if not any(enabled(config) for _name, config in connect):
                _enable(hermes, connect[0][0])
        else:
            taken = set(servers) | _plugin_server_names(servers)
            name = next((name for name in NAMES if name not in taken), None)
            if name is None:
                raise ComposioError(409, "composio_name_taken",
                                    "Other MCP servers already use the names Composio Connect needs.")
            config = {"url": CONNECT_URL, "auth": "oauth", "enabled": True, "connect_timeout": CONNECT_TIMEOUT}
            if not hermes.save_server(name, config):
                logger.warning("bighelp composio install refused: hermes_rejected_config")
                raise ComposioError(503, "composio_install_refused", "Hermes refused the Composio Connect server.")
        return _project(hermes)


def _enable(hermes: SimpleNamespace, name: str) -> None:
    # The same edit as Hermes' `PUT /api/mcp/servers/{name}/enabled`.
    config = hermes.load_config()
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict) or not isinstance(servers.get(name), dict):
        raise ComposioError(409, "composio_changed", "The MCP servers changed; try again.")
    servers[name]["enabled"] = True
    hermes.save_config(config)
