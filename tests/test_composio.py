"""Composio Connect for the bighelp app: what the status shows and what install changes.

`StatusTests` and `InstallTests` run on a fake of Hermes' MCP config helpers (one in-memory config per profile,
with Hermes' config lock). `RouteTests` run the HTTP routes on Hermes' real helpers, auth middleware and a
throwaway HERMES_HOME.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, contextmanager
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from loopdy_plugin import composio

CONNECT = {"url": "https://connect.composio.dev/mcp", "auth": "oauth", "enabled": True, "connect_timeout": 120}
# Composio Connect's tools as its MCP server lists them; the search tool's description names the account's
# connected apps (the app reads its own account's list from the same words).
SEARCH_DESCRIPTION = ("Tool Server Info: discover the tools of 500+ apps for a task, with a plan to run them.\n"
                      "- User has manually connected the apps: gmail, github, google_calendar. Prefer these apps when "
                      "intent is unclear.")
CONNECT_TOOLS = (("COMPOSIO_SEARCH_TOOLS", SEARCH_DESCRIPTION),
                 ("COMPOSIO_MULTI_EXECUTE_TOOL", "Run up to 50 tools of the user's connected apps in parallel."),
                 ("COMPOSIO_MANAGE_CONNECTIONS", "List, add, rename or remove the user's connections to apps."),
                 ("COMPOSIO_GET_TOOL_SCHEMAS", "Input schemas of tools that the search returned."))
SECRETS = ("fixture-header-secret", "fixture-env-secret", "fixture-query-secret", "fixture-user",
           "fixture-password", "fixture-fragment", "fixture-token-secret")


def _module(name: str, **attributes) -> ModuleType:
    value = ModuleType(name)
    value.__dict__.update(attributes)
    return value


def _enabled(config: dict) -> bool:
    # Like Hermes' `mcp_server_enabled`: absent or unreadable is on; false-like words are off.
    return str(config.get("enabled", True)).strip().lower() not in {"false", "0", "no", "off"}


class FakeHermes:
    """Hermes' MCP config helpers over one config per profile, with its profile scope and config lock."""

    def __init__(self, servers: dict | None = None, *, tokens=(), plugin_servers: dict | None = None):
        self.configs = {profile: {"model": {"default": "fixture-model"}, "mcp_servers": copy.deepcopy(servers or {})}
                        for profile in ("default", "research")}
        self.tokens = set(tokens)
        self.plugin_servers = plugin_servers or {}
        self.active: str | None = None
        self.locked = False
        self.writes: list[tuple[str | None, bool, str]] = []
        self.refuse = False
        self.token_error = False
        # Hermes' MCP connection, as Composio Connect answers it.
        self.tools = CONNECT_TOOLS
        self.connect_error: BaseException | None = None
        self.hang = False
        self.connects: list[tuple[str | None, str, dict, bool]] = []
        self.shutdowns = 0
        self.stamps: dict[tuple[str, str], tuple] = {}
        self.scopes: list[tuple[str, str]] = []
        self.prompts_off = False

    @contextmanager
    def profile_scope(self, profile):
        previous, self.active = self.active, profile
        try:
            yield
        finally:
            self.active = previous

    @contextmanager
    def config_profile_scope(self, profile):
        # Hermes' probe-class scope: home and secrets, without the skills lock.
        self.scopes.append(("config", profile))
        with self.profile_scope(profile):
            yield

    @contextmanager
    def no_prompt(self):
        previous, self.prompts_off = self.prompts_off, True
        try:
            yield
        finally:
            self.prompts_off = previous

    async def connect_server(self, name, config):
        self.connects.append((self.active, name, dict(config), self.prompts_off))
        if self.hang:
            await asyncio.sleep(3600)
        if self.connect_error is not None:
            raise self.connect_error
        hermes = self

        class Server:
            _tools = [SimpleNamespace(name=tool, description=text) for tool, text in hermes.tools]

            async def shutdown(self):
                hermes.shutdowns += 1
        return Server()

    def run_on_loop(self, factory, timeout=30):
        return asyncio.run(factory())

    @contextmanager
    def config_write_scope(self, profile):
        with self.profile_scope(profile):
            self.locked = True
            try:
                yield
            finally:
                self.locked = False

    def load_config(self):
        return copy.deepcopy(self.configs[self.active])

    def save_config(self, config):
        self.writes.append((self.active, self.locked, "save_config"))
        self.configs[self.active] = copy.deepcopy(config)

    def get_mcp_servers(self, config=None):
        servers = (self.load_config() if config is None else config).get("mcp_servers")
        return servers if servers and isinstance(servers, dict) else {}

    def save_mcp_server(self, name, server_config):
        if self.refuse:
            return False
        self.writes.append((self.active, self.locked, "save_server"))
        self.configs[self.active].setdefault("mcp_servers", {})[name] = copy.deepcopy(server_config)
        return True

    def token_storage(self, name):
        hermes = self

        class Storage:
            def has_cached_tokens(self):
                if hermes.token_error:
                    raise PermissionError("/private/fixture-home/mcp-tokens")
                return (hermes.active, name) in hermes.tokens

            def credential_stamp(self):
                return hermes.stamps.get((hermes.active, name), (1, None))
        return Storage()

    def server_configs_with_sources(self, servers):
        merged, owners = dict(servers), {name: None for name in servers}
        for name, plugin in self.plugin_servers.items():
            if name not in merged:
                merged[name], owners[name] = {"url": "https://plugin.example/mcp"}, plugin
        return merged, owners

    def modules(self, *, enabled_reader=True, plugin_sources=True):
        return {
            "hermes_cli.config": _module("hermes_cli.config", load_config=self.load_config,
                                         save_config=self.save_config),
            "hermes_cli.mcp_config": _module("hermes_cli.mcp_config", _get_mcp_servers=self.get_mcp_servers,
                                             _save_mcp_server=self.save_mcp_server,
                                             _resolve_mcp_server_config=lambda config: dict(config)),
            "hermes_cli.mcp_security": _module("hermes_cli.mcp_security",
                                               validate_mcp_server_entry=lambda name, entry: []),
            "hermes_cli.profiles": _module("hermes_cli.profiles", profile_exists=lambda name: name in self.configs),
            "hermes_cli.web_routers._common": _module("hermes_cli.web_routers._common",
                                                      _profile_scope=self.profile_scope,
                                                      config_write_scope=self.config_write_scope),
            "hermes_cli.web_server_profiles": _module("hermes_cli.web_server_profiles",
                                                      _config_profile_scope=self.config_profile_scope),
            "tools.mcp_oauth": _module("tools.mcp_oauth", HermesTokenStorage=self.token_storage,
                                       suppress_interactive_oauth=self.no_prompt),
            "tools.mcp_tool_discovery": _module("tools.mcp_tool_discovery", _connect_server=self.connect_server),
            "tools.mcp_tool_lifecycle": _module("tools.mcp_tool_lifecycle", _stop_mcp_loop_if_idle=lambda: True),
            "tools.mcp_tool_loop": _module("tools.mcp_tool_loop", _ensure_mcp_loop=lambda: None,
                                           _run_on_mcp_loop=self.run_on_loop),
            "tools.mcp_tool_common": _module("tools.mcp_tool_common", mcp_server_enabled=_enabled)
            if enabled_reader else None,
            "tui_gateway.mcp_rpc_helpers": _module("tui_gateway.mcp_rpc_helpers",
                                                   server_configs_with_sources=self.server_configs_with_sources)
            if plugin_sources else None,
        }


class _FakeCase(unittest.TestCase):
    def use(self, hermes: FakeHermes, **options) -> FakeHermes:
        modules = patch.dict(sys.modules, hermes.modules(**options))
        modules.start()
        self.addCleanup(modules.stop)
        return hermes

    def assert_private(self, value: dict):
        text = json.dumps(value)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        for server in value["servers"]:
            self.assertEqual(set(server), {"name", "url", "enabled", "auth", "kind", "signedIn"})


class StatusTests(_FakeCase):
    def test_no_composio_servers(self):
        self.use(FakeHermes({"github": {"url": "https://api.example.com/mcp", "auth": "oauth"}}))
        self.assertEqual(composio.status("default"), {"schemaVersion": 1, "connect": None, "servers": []})
        self.use(FakeHermes())
        self.assertEqual(composio.status("research"), {"schemaVersion": 1, "connect": None, "servers": []})

    def test_connect_signed_in_and_signed_out(self):
        hermes = self.use(FakeHermes({"composio": CONNECT}, tokens={("default", "composio")}))
        expected = {"name": "composio", "url": "https://connect.composio.dev/mcp", "enabled": True,
                    "auth": "oauth", "kind": "connect", "signedIn": True}
        self.assertEqual(composio.status("default"), {"schemaVersion": 1, "connect": "composio", "servers": [expected]})
        hermes.tokens.clear()
        self.assertFalse(composio.status("default")["servers"][0]["signedIn"])
        hermes.token_error = True
        value = composio.status("default")
        self.assertIsNone(value["servers"][0]["signedIn"], "Unknown, not signed in")
        self.assertNotIn("fixture-home", json.dumps(value))

    def test_tokens_are_read_in_the_agents_own_profile(self):
        self.use(FakeHermes({"composio": CONNECT}, tokens={("research", "composio")}))
        self.assertFalse(composio.status("default")["servers"][0]["signedIn"])
        self.assertTrue(composio.status("research")["servers"][0]["signedIn"])

    def test_disabled_connect_is_still_named(self):
        self.use(FakeHermes({"composio": {**CONNECT, "enabled": False}}))
        value = composio.status("default")
        self.assertEqual((value["connect"], value["servers"][0]["enabled"]), ("composio", False))

    def test_connect_names_the_first_enabled_then_the_first(self):
        hermes = self.use(FakeHermes({"a-connect": {**CONNECT, "enabled": "false"}, "b-connect": CONNECT}))
        self.assertEqual(composio.status("default")["connect"], "b-connect")
        hermes.configs["default"]["mcp_servers"]["b-connect"]["enabled"] = "off"
        self.assertEqual(composio.status("default")["connect"], "a-connect")

    def test_other_composio_servers_are_listed_but_are_not_connect(self):
        self.use(FakeHermes({
            "composio-gmail": {"url": "https://backend.composio.dev/v3/mcp/fixture-server?user_id=fixture-query-secret",
                               "headers": {"x-api-key": "fixture-header-secret"}},
            "composio-tool": {"url": "https://mcp.composio.dev/partner", "auth": "OAuth "},
            "composio-local": {"command": "npx", "args": ["composio"], "env": {"COMPOSIO_API_KEY": "fixture-env-secret"}},
        }))
        value = composio.status("default")
        self.assertIsNone(value["connect"])
        self.assertEqual(value["servers"], [
            {"name": "composio-gmail", "url": "https://backend.composio.dev/v3/mcp/fixture-server", "enabled": True,
             "auth": "headers", "kind": "other", "signedIn": None},
            {"name": "composio-tool", "url": "https://mcp.composio.dev/partner", "enabled": True,
             "auth": "oauth", "kind": "other", "signedIn": False},
        ])
        self.assert_private(value)

    def test_urls_lose_user_info_query_and_fragment(self):
        self.use(FakeHermes({"composio": {
            "url": "HTTPS://fixture-user:fixture-password@Connect.Composio.dev.:443/mcp?key=fixture-query-secret"
                   "#fixture-fragment", "auth": "oauth", "env": {"TOKEN": "fixture-env-secret"},
            "headers": {"Authorization": "Bearer fixture-header-secret"}}}))
        value = composio.status("default")
        self.assertEqual(value["servers"][0]["url"], "https://connect.composio.dev:443/mcp")
        self.assertEqual((value["servers"][0]["kind"], value["servers"][0]["auth"]), ("connect", "oauth"))
        self.assert_private(value)

    def test_only_composio_dev_and_its_subdomains_count(self):
        self.use(FakeHermes({
            "lookalike": {"url": "https://connect.composio.dev.example.com/mcp"},
            "suffix": {"url": "https://notcomposio.dev/mcp"},
            "user-info": {"url": "https://connect.composio.dev@example.com/mcp"},
            "query": {"url": "https://example.com/mcp?next=https://connect.composio.dev"},
            "scheme": {"url": "ftp://connect.composio.dev/mcp"},
            "bad-port": {"url": "https://connect.composio.dev:99999/mcp"},
            "not-text": {"url": ["https://connect.composio.dev/mcp"]},
            "control\nname": CONNECT,
            "x" * 129: CONNECT,
            "not-a-server": "https://connect.composio.dev/mcp",
            "apex": {"url": "https://composio.dev/mcp"},
        }))
        value = composio.status("default")
        self.assertEqual([server["name"] for server in value["servers"]], ["apex"])
        self.assertEqual(value["servers"][0]["kind"], "other")

    def test_older_hermes_reads_enabled_its_own_way(self):
        self.use(FakeHermes({"composio": {**CONNECT, "enabled": False}, "connect-two": {**CONNECT, "enabled": "no"}}),
                 enabled_reader=False, plugin_sources=False)
        value = composio.status("default")
        self.assertEqual([server["enabled"] for server in value["servers"]], [False, True])
        self.assertEqual(value["connect"], "connect-two")

    def test_server_list_is_bounded(self):
        self.use(FakeHermes({f"composio-{index:02}": {"url": f"https://s{index}.composio.dev/mcp"}
                             for index in range(composio.MAX_SERVERS + 5)}))
        self.assertEqual(len(composio.status("default")["servers"]), composio.MAX_SERVERS)

    def test_unknown_profiles_and_missing_helpers(self):
        self.use(FakeHermes())
        with self.assertRaises(composio.ComposioError) as error:
            composio.status("ghost")
        self.assertEqual((error.exception.status, error.exception.code), (404, "profile_not_found"))
        with patch.dict(sys.modules, {"hermes_cli.web_routers._common": None}):
            self.assertFalse(composio.available())
            with self.assertRaises(composio.ComposioError) as error:
                composio.install("default")
            self.assertEqual((error.exception.status, error.exception.code), (503, "composio_unavailable"))
        self.assertTrue(composio.available())


class InstallTests(_FakeCase):
    def test_adds_composio_connect_under_hermes_config_lock(self):
        hermes = self.use(FakeHermes({"github": {"url": "https://api.example.com/mcp"}}))
        value = composio.install("research")
        self.assertEqual(hermes.configs["research"]["mcp_servers"]["composio"], CONNECT)
        self.assertNotIn("composio", hermes.configs["default"]["mcp_servers"])
        self.assertEqual(hermes.writes, [("research", True, "save_server")])
        self.assertEqual(value, {"schemaVersion": 1, "connect": "composio", "servers": [
            {"name": "composio", "url": "https://connect.composio.dev/mcp", "enabled": True, "auth": "oauth",
             "kind": "connect", "signedIn": False}]})
        self.assertEqual(hermes.configs["research"]["model"], {"default": "fixture-model"})

    def test_running_it_again_changes_nothing(self):
        hermes = self.use(FakeHermes())
        first = composio.install("default")
        second = composio.install("default")
        self.assertEqual(first, second)
        self.assertEqual(len(hermes.writes), 1)
        self.assertEqual(composio.status("default"), second)

    def test_turns_on_a_disabled_connect_server_and_keeps_its_settings(self):
        existing = {"url": "https://connect.composio.dev/mcp", "auth": "oauth", "enabled": False,
                    "connect_timeout": 90.0, "tools": {"include": ["search"]}}
        hermes = self.use(FakeHermes({"my-composio": existing}, tokens={("default", "my-composio")}))
        value = composio.install("default")
        self.assertEqual(hermes.configs["default"]["mcp_servers"]["my-composio"], {**existing, "enabled": True})
        self.assertEqual(hermes.writes, [("default", True, "save_config")])
        self.assertEqual((value["connect"], value["servers"][0]["enabled"], value["servers"][0]["signedIn"]),
                         ("my-composio", True, True))
        self.assertNotIn("composio", hermes.configs["default"]["mcp_servers"])

    def test_leaves_an_enabled_connect_server_alone(self):
        hermes = self.use(FakeHermes({"a-connect": {**CONNECT, "enabled": False}, "b-connect": CONNECT}))
        self.assertEqual(composio.install("default")["connect"], "b-connect")
        self.assertEqual(hermes.writes, [])

    def test_other_composio_server_named_composio_gets_a_second_name(self):
        other = {"url": "https://backend.composio.dev/v3/mcp/fixture-server", "headers": {"x-api-key": "fixture-header-secret"}}
        hermes = self.use(FakeHermes({"composio": other}))
        value = composio.install("default")
        self.assertEqual(hermes.configs["default"]["mcp_servers"], {"composio": other, "composio-connect": CONNECT})
        self.assertEqual(value["connect"], "composio-connect")
        self.assertEqual([server["kind"] for server in value["servers"]], ["other", "connect"])
        self.assert_private(value)

    def test_names_another_plugin_uses_are_taken(self):
        hermes = self.use(FakeHermes(plugin_servers={"composio": "fixture-plugin"}))
        self.assertEqual(composio.install("default")["connect"], "composio-connect")
        self.assertIn("composio-connect", hermes.configs["default"]["mcp_servers"])

    def test_both_names_taken(self):
        hermes = self.use(FakeHermes({"composio": {"url": "https://api.example.com/a"},
                                      "composio-connect": {"command": "fixture"}}))
        with self.assertRaises(composio.ComposioError) as error:
            composio.install("default")
        self.assertEqual((error.exception.status, error.exception.code), (409, "composio_name_taken"))
        self.assertEqual(hermes.writes, [])

    def test_hermes_refusal_and_unknown_profiles(self):
        hermes = self.use(FakeHermes())
        hermes.refuse = True
        with self.assertRaises(composio.ComposioError) as error:
            composio.install("default")
        self.assertEqual((error.exception.status, error.exception.code), (503, "composio_install_refused"))
        with self.assertRaises(composio.ComposioError) as error:
            composio.install("ghost")
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(hermes.writes, [])


class AppsTests(_FakeCase):
    """The apps the agent's own Composio sign-in has connected, read from Composio Connect's tool list."""

    def setUp(self):
        composio._apps_cache.clear()
        self.addCleanup(composio._apps_cache.clear)

    def signed_in(self, profile="default", **options) -> FakeHermes:
        return self.use(FakeHermes({"composio": CONNECT}, tokens={(profile, "composio")}, **options))

    def test_lists_the_apps_the_agents_own_sign_in_connected(self):
        hermes = self.signed_in()
        self.assertEqual(composio.apps("default"), {"schemaVersion": 1, "connect": "composio", "state": "listed",
                                                    "reason": None, "apps": ["gmail", "github", "google_calendar"]})
        self.assertEqual(len(hermes.connects), 1)
        profile, name, config, prompts_off = hermes.connects[0]
        self.assertEqual((profile, name, config["url"]), ("default", "composio", "https://connect.composio.dev/mcp"))
        self.assertLessEqual(config["connect_timeout"], composio.APPS_TIMEOUT, "Bounded, not Hermes' 120 seconds")
        self.assertTrue(prompts_off, "A sign-in that no longer works never opens a browser on the host")
        self.assertEqual(hermes.shutdowns, 1, "Connected only to read the list")
        self.assertEqual(hermes.scopes, [("config", "default")], "Hermes' probe-class scope, not the skills lock")
        self.assertEqual(hermes.writes, [], "Changes nothing")

    def test_an_account_without_connected_apps_lists_none(self):
        hermes = self.signed_in()
        hermes.tools = (("COMPOSIO_SEARCH_TOOLS", "Tool Server Info: discover the tools of 500+ apps for a task."),)
        self.assertEqual(composio.apps("default")["apps"], [])
        self.assertEqual(composio.apps("default")["state"], "listed")

    def test_each_agent_uses_its_own_sign_in(self):
        hermes = self.signed_in("research")
        self.assertEqual(composio.apps("default")["reason"], "not_signed_in")
        self.assertEqual(composio.apps("research")["state"], "listed")
        self.assertEqual([connect[0] for connect in hermes.connects], ["research"])

    def test_without_a_working_setup_nothing_connects(self):
        hermes = self.use(FakeHermes())
        self.assertEqual(composio.apps("default"), {"schemaVersion": 1, "connect": None, "state": "unknown",
                                                    "reason": "not_installed", "apps": []})
        hermes = self.use(FakeHermes({"composio": {**CONNECT, "enabled": False}}, tokens={("default", "composio")}))
        self.assertEqual(composio.apps("default")["reason"], "turned_off")
        hermes = self.use(FakeHermes({"composio": CONNECT}))
        self.assertEqual((composio.apps("default")["connect"], composio.apps("default")["reason"]),
                         ("composio", "not_signed_in"))
        self.assertEqual(hermes.connects, [])

    def test_a_refused_sign_in_is_unknown_and_keeps_its_words_out(self):
        hermes = self.signed_in()
        hermes.connect_error = RuntimeError("401 Unauthorized: https://connect.composio.dev/mcp?token=fixture-token-secret "
                                            "/private/fixture-home/mcp-tokens/composio.json")
        with self.assertLogs("hermes.plugins.bighelp", level="WARNING") as logs:
            value = composio.apps("default")
        self.assertEqual((value["state"], value["reason"], value["apps"]), ("unknown", "unreachable", []))
        for text in (json.dumps(value), " ".join(logs.output)):
            self.assertNotIn("fixture-token-secret", text)
            self.assertNotIn("fixture-home", text)

    def test_a_server_that_does_not_answer_is_unknown_in_time(self):
        hermes = self.signed_in()
        hermes.hang = True
        with patch.object(composio, "APPS_TIMEOUT", 0.2), self.assertLogs("hermes.plugins.bighelp", level="WARNING"):
            value = composio.apps("default")
        self.assertEqual((value["state"], value["reason"]), ("unknown", "unreachable"))

    def test_no_search_tool_is_unknown_not_none(self):
        hermes = self.signed_in()
        hermes.tools = CONNECT_TOOLS[1:]
        self.assertEqual(composio.apps("default")["reason"], "no_app_list")

    def test_the_list_is_bounded_and_checked(self):
        hermes = self.signed_in()
        slugs = ", ".join([f"app_{index}" for index in range(composio.MAX_APPS + 50)])
        hermes.tools = (("COMPOSIO_SEARCH_TOOLS", "User has manually connected the apps: Gmail, bad/slug, -x, "
                         + "x" * 65 + ", slack, slack, " + slugs + ". Prefer these apps."),)
        found = composio.apps("default")["apps"]
        self.assertEqual(found[0], "slack")
        self.assertEqual(len(found), composio.MAX_APPS)
        self.assertTrue(all(composio._SLUG.fullmatch(slug) for slug in found))
        self.assertEqual(composio.connected_apps("x" * composio.MAX_DESCRIPTION + " connected the apps: gmail."), [],
                         "Read no further than the cap")
        self.assertEqual(composio.connected_apps("connected the apps: gmail,\n github"), ["gmail", "github"])

    def test_a_repeat_reuses_the_answer_until_the_sign_in_changes(self):
        hermes = self.signed_in()
        composio.apps("default")
        composio.apps("default")
        self.assertEqual(len(hermes.connects), 1)
        hermes.stamps[("default", "composio")] = (2, None)  # Hermes signed in again or refreshed its token
        composio.apps("default")
        self.assertEqual(len(hermes.connects), 2)
        with patch.object(composio.time, "monotonic", return_value=time.monotonic() + composio.APPS_CACHE_SECONDS + 1):
            composio.apps("default")
        self.assertEqual(len(hermes.connects), 3)

    def test_unknown_profiles_and_missing_helpers(self):
        self.signed_in()
        with self.assertRaises(composio.ComposioError) as error:
            composio.apps("ghost")
        self.assertEqual((error.exception.status, error.exception.code), (404, "profile_not_found"))
        self.assertTrue(composio.apps_available())
        with patch.dict(sys.modules, {"tools.mcp_tool_discovery": None}):
            self.assertTrue(composio.available(), "Status and install don't need the connection code")
            self.assertFalse(composio.apps_available())
            with self.assertRaises(composio.ComposioError) as error:
                composio.apps("default")
            self.assertEqual((error.exception.status, error.exception.code), (503, "composio_unavailable"))


class RouteTests(unittest.TestCase):
    """POST /native/composio/status and /install on Hermes' own MCP helpers and auth middleware."""

    def setUp(self):
        if not composio.available():
            self.skipTest("Hermes MCP config helpers unavailable")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
        from hermes_cli.dashboard_auth.registry import register_provider, unregister_global_provider
        from loopdy_plugin import native_api
        from test_native_api import FixtureProvider

        temporary = tempfile.TemporaryDirectory(prefix="bighelp-composio-", dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        (self.home / "profiles/research").mkdir(parents=True)
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.provider = FixtureProvider()
        register_provider(self.provider)
        self.addCleanup(unregister_global_provider, self.provider.name, self.provider)
        app = FastAPI()
        app.state.auth_required = True
        app.middleware("http")(gated_auth_middleware)
        app.include_router(native_api.router, prefix="/api/plugins/loopdy")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def headers(self):
        context = self.client.get("/api/plugins/loopdy/native/context", headers={"Authorization": "Bearer fixture-alice"})
        self.assertIn(composio.CAPABILITY, context.json()["features"])
        return {"Authorization": "Bearer fixture-alice", "If-Match": context.headers["etag"],
                "X-Loopdy-Request-ID": str(uuid.uuid4())}

    def post(self, operation, body, headers=None):
        return self.client.post("/api/plugins/loopdy/native/composio/" + operation,
                                headers=self.headers() if headers is None else headers, json=body)

    def config(self, home: Path | None = None) -> dict:
        import yaml
        path = (home or self.home) / "config.yaml"
        return yaml.safe_load(path.read_text()) if path.exists() else {}

    def write_servers(self, servers: dict, home: Path | None = None):
        import yaml
        path = (home or self.home) / "config.yaml"
        path.write_text(yaml.safe_dump({"mcp_servers": servers}))

    def test_install_then_sign_in_then_again(self):
        empty = self.post("status", {"agentId": "default"})
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json(), {"agentId": "default", "schemaVersion": 1, "connect": None, "servers": []})
        self.assertEqual(empty.headers["cache-control"], "no-store")

        headers = self.headers()
        installed = self.post("install", {"agentId": "default"}, headers)
        self.assertEqual(installed.status_code, 200, installed.text)
        self.assertEqual(installed.headers["x-loopdy-request-id"], headers["X-Loopdy-Request-ID"])
        server = {"name": "composio", "url": "https://connect.composio.dev/mcp", "enabled": True, "auth": "oauth",
                  "kind": "connect", "signedIn": False}
        self.assertEqual(installed.json(), {"agentId": "default", "schemaVersion": 1, "connect": "composio",
                                            "servers": [server]})
        saved = self.config()["mcp_servers"]["composio"]
        self.assertEqual((saved["url"], saved["auth"], saved["enabled"], saved["connect_timeout"]),
                         ("https://connect.composio.dev/mcp", "oauth", True, 120))

        # Where Hermes' MCP OAuth keeps the tokens after the app signs in.
        tokens = self.home / "mcp-tokens"
        tokens.mkdir(exist_ok=True)
        (tokens / "composio.json").write_text(json.dumps({"access_token": "fixture-token-secret",
                                                          "token_type": "Bearer"}))
        signed_in = self.post("status", {"agentId": "default"})
        self.assertTrue(signed_in.json()["servers"][0]["signedIn"])
        before = (self.home / "config.yaml").read_text()
        again = self.post("install", {"agentId": "default"})
        self.assertEqual(again.json(), signed_in.json())
        self.assertEqual((self.home / "config.yaml").read_text(), before, "Nothing to change")
        for response in (installed, signed_in, again):
            self.assertNotIn("fixture-token-secret", response.text)
            self.assertNotIn(str(self.home), response.text)

    def test_turns_on_a_disabled_server_and_keeps_secrets_out(self):
        self.write_servers({
            "composio": {"url": "https://backend.composio.dev/v3/mcp/fixture-server?user_id=fixture-query-secret",
                         "headers": {"x-api-key": "fixture-header-secret"}},
            "work-composio": {"url": "https://connect.composio.dev/mcp", "auth": "oauth", "enabled": False},
        })
        response = self.post("install", {"agentId": "default"})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value["connect"], "work-composio")
        self.assertEqual([(server["name"], server["kind"], server["auth"], server["enabled"]) for server in value["servers"]],
                         [("composio", "other", "headers", True), ("work-composio", "connect", "oauth", True)])
        self.assertIs(self.config()["mcp_servers"]["work-composio"]["enabled"], True)
        self.assertNotIn("composio-connect", self.config()["mcp_servers"])
        for secret in ("fixture-query-secret", "fixture-header-secret", "x-api-key"):
            self.assertNotIn(secret, response.text)

    def test_other_composio_server_named_composio(self):
        self.write_servers({"composio": {"url": "https://backend.composio.dev/v3/mcp/fixture-server",
                                         "headers": {"x-api-key": "fixture-header-secret"}}})
        response = self.post("install", {"agentId": "default"})
        self.assertEqual(response.json()["connect"], "composio-connect")
        servers = self.config()["mcp_servers"]
        self.assertEqual(servers["composio"]["headers"], {"x-api-key": "fixture-header-secret"})
        self.assertEqual(servers["composio-connect"]["url"], "https://connect.composio.dev/mcp")

    def test_each_agent_has_its_own_config(self):
        research = self.home / "profiles/research"
        self.write_servers({}, research)  # a named profile is live once it has a config
        with ExitStack() as stack:
            try:
                from tui_gateway import launch_profile_policy
            except ImportError:  # Hermes before 0.22 has no multi-profile hosting
                pass
            else:
                # Hermes' scope for another profile switches the whole process to multi-profile hosting;
                # keep that out of the other tests in this process.
                stack.enter_context(patch.object(launch_profile_policy, "activate_multi_profile_hosting"))
            response = self.post("install", {"agentId": "research"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.post("status", {"agentId": "research"}).json()["connect"], "composio")
        self.assertIn("composio", self.config(research)["mcp_servers"])
        self.assertNotIn("composio", self.config().get("mcp_servers") or {})
        self.assertIsNone(self.post("status", {"agentId": "default"}).json()["connect"])

    def test_apps_reads_the_agents_own_list(self):
        if not composio.apps_available():
            self.skipTest("Hermes MCP connection helpers unavailable")
        composio._apps_cache.clear()
        self.addCleanup(composio._apps_cache.clear)
        connected = []

        def list_tools(probe, name, config):
            # Hermes' own connection code, without the network: Composio Connect's answer.
            connected.append((name, config["url"], callable(probe.connect), callable(probe.no_prompt)))
            return list(CONNECT_TOOLS)

        with patch.object(composio, "_list_tools", side_effect=list_tools):
            self.assertEqual(self.post("apps", {"agentId": "default"}).json()["reason"], "not_installed")
            self.post("install", {"agentId": "default"})
            unsigned = self.post("apps", {"agentId": "default"})
            self.assertEqual(unsigned.json(), {"agentId": "default", "schemaVersion": 1, "connect": "composio",
                                               "state": "unknown", "reason": "not_signed_in", "apps": []})
            self.assertEqual(connected, [], "No sign-in, no connection")
            tokens = self.home / "mcp-tokens"
            tokens.mkdir(exist_ok=True)
            (tokens / "composio.json").write_text(json.dumps({"access_token": "fixture-token-secret",
                                                              "token_type": "Bearer"}))
            headers = self.headers()
            listed = self.post("apps", {"agentId": "default"}, headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), {"agentId": "default", "schemaVersion": 1, "connect": "composio",
                                         "state": "listed", "reason": None,
                                         "apps": ["gmail", "github", "google_calendar"]})
        self.assertEqual(listed.headers["cache-control"], "no-store")
        self.assertEqual(listed.headers["x-loopdy-request-id"], headers["X-Loopdy-Request-ID"])
        self.assertEqual(connected, [("composio", "https://connect.composio.dev/mcp", True, True)])
        self.assertNotIn("fixture-token-secret", listed.text)
        self.assertNotIn(str(self.home), listed.text)

    def test_requests_are_checked(self):
        for operation in ("status", "install", "apps"):
            with self.subTest(operation=operation):
                self.assertEqual(self.post(operation, {"agentId": "ghost"}).status_code, 404)
                for body in ({}, {"profile": "default"}, {"agentId": "Default"}, {"agentId": "default", "extra": 1},
                             {"agentId": "../default"}):
                    self.assertEqual(self.post(operation, body).status_code, 422, body)
                self.assertEqual(self.client.post("/api/plugins/loopdy/native/composio/" + operation,
                                                  json={"agentId": "default"}).status_code, 401)
                headers = self.headers()
                del headers["If-Match"]
                self.assertEqual(self.post(operation, {"agentId": "default"}, headers).status_code, 428)
                headers = self.headers()
                headers["If-Match"] = '"stale"'
                self.assertEqual(self.post(operation, {"agentId": "default"}, headers).status_code, 412)
                headers = self.headers()
                headers["X-Loopdy-Request-ID"] = "NOT-A-UUID"
                self.assertEqual(self.post(operation, {"agentId": "default"}, headers).status_code, 422)
        self.assertNotIn("composio", self.config().get("mcp_servers") or {})

    def test_hosts_without_the_helpers_say_unavailable(self):
        stale = self.headers()
        with patch.object(composio, "available", return_value=False):
            self.assertEqual(self.post("status", {"agentId": "default"}, stale).status_code, 412,
                             "The feature list is part of the context")
            authorization = {"Authorization": "Bearer fixture-alice"}
            context = self.client.get("/api/plugins/loopdy/native/context", headers=authorization)
            self.assertNotIn(composio.CAPABILITY, context.json()["features"])
            response = self.post("install", {"agentId": "default"}, {
                **authorization, "If-Match": context.headers["etag"], "X-Loopdy-Request-ID": str(uuid.uuid4())})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "composio_unavailable")
        self.assertFalse((self.home / "config.yaml").exists())

    def test_hosts_without_the_connection_helpers_keep_status_and_install(self):
        with patch.object(composio, "apps_available", return_value=False):
            headers = self.headers()
            context = self.client.get("/api/plugins/loopdy/native/context",
                                      headers={"Authorization": "Bearer fixture-alice"})
            self.assertNotIn(composio.APPS_CAPABILITY, context.json()["features"])
            response = self.post("apps", {"agentId": "default"}, headers)
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "composio_unavailable"))
            self.assertEqual(self.post("status", {"agentId": "default"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
