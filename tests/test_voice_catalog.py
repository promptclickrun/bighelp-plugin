"""The voices a speech plugin offers an agent. The plugin is a real Hermes plugin in one agent's own profile,
found by Hermes' own discovery and TTS registry, as Phonon is on a host.
"""
from __future__ import annotations
import telemetry_off  # noqa: F401  (no test run posts host telemetry)

import os
from pathlib import Path
import tempfile
import textwrap
import unittest
from unittest.mock import patch

from loopdy_plugin import voice_catalog
from hermes_process_state import single_profile_host
import test_native_api as native_fixtures
from test_native_api import PREFIX

PLUGIN = '''
from agent.tts_provider import TTSProvider


class Chime(TTSProvider):
    """Reads its default voice from the agent's own config, as Phonon does."""

    @property
    def name(self):
        return "chime"

    def list_voices(self):
        return [{"id": "Wren", "display": "Wren", "language": "en"}, {"id": "Otto", "language": "de"},
                {"id": "Wren", "display": "Wren again"}, {"display": "No ID"}, "not a voice",
                {"id": "Bad\\nID"}, {"id": "Long", "display": "é" * 120, "language": "x" * 200}]

    def list_models(self):
        return [{"id": "chime-1", "display": "Chime"}]

    def default_voice(self):
        from hermes_cli.config import load_config_readonly
        return ((load_config_readonly().get("tts") or {}).get("chime") or {}).get("voice") or "Wren"

    def synthesize(self, text, output_path, **kwargs):
        raise RuntimeError("not in tests")


class Many(Chime):
    @property
    def name(self):
        return "many"

    def list_voices(self):
        return [{"id": f"voice-{index}", "display": "n" * 150, "language": "l" * 150} for index in range(700)]

    def list_models(self):
        return [{"id": f"model-{index}"} for index in range(80)]


class Broken(Chime):
    @property
    def name(self):
        return "broken"

    def list_voices(self):
        raise RuntimeError("/Users/someone/.secret path in an error")


def register(ctx):
    for provider in (Chime(), Many(), Broken()):
        ctx.register_tts_provider(provider)
'''


def hermes_tts_available() -> bool:
    try:
        from hermes_cli.plugins import get_plugin_manager  # noqa: F401
    except Exception:
        return False
    return voice_catalog.available()


@unittest.skipUnless(hermes_tts_available(), "this Hermes has no plugin speech providers")
class VoiceCatalogTests(unittest.TestCase):
    def setUp(self):
        single_profile_host(self)
        folder = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        (self.root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
        self.agent("nova", "tts:\n  provider: chime\n  chime:\n    voice: Otto\n")
        # The same plugin, but a command provider of the same name wins, as in Hermes' own dispatch.
        self.agent("pip", "tts:\n  providers:\n    chime:\n      type: command\n      command: 'true'\n")
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.root), "HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def agent(self, name: str, tts: str) -> None:
        home = self.root / "profiles" / name
        plugin = home / "plugins" / "chime-tts"
        plugin.mkdir(parents=True)
        (plugin / "plugin.yaml").write_text("manifest_version: 1\nname: chime-tts\nkind: standalone\n"
                                            "version: \"1.0.0\"\n", encoding="utf-8")
        (plugin / "__init__.py").write_text(textwrap.dedent(PLUGIN), encoding="utf-8")
        (home / "config.yaml").write_text("model: {}\nplugins:\n  enabled: [chime-tts]\n" + tts, encoding="utf-8")

    def test_a_plugin_lists_its_voices_models_and_the_agents_default(self):
        self.assertEqual(voice_catalog.catalog("nova", "Chime"), {
            "provider": "chime",
            "voices": [{"id": "Wren", "name": "Wren", "language": "en"}, {"id": "Otto", "name": "Otto", "language": "de"},
                       {"id": "Long", "name": "é" * 80}],
            "models": [{"id": "chime-1", "name": "Chime"}],
            "defaultVoice": "Otto",
        }, "Duplicates, entries without an ID and bad text are left out; a long name is cut at 160 bytes")

    def test_only_the_agent_that_loads_the_plugin_has_it_and_hermes_rules_win(self):
        empty = {"voices": [], "models": []}
        self.assertEqual(voice_catalog.catalog("default", "chime"), {"provider": "chime", **empty},
                         "The default agent doesn't load this plugin")
        self.assertEqual(voice_catalog.catalog("pip", "chime"), {"provider": "chime", **empty},
                         "A command provider of the same name wins")
        for name in ("edge", "elevenlabs", "openai", "nous", "nothing-here"):
            self.assertEqual(voice_catalog.catalog("nova", name), {"provider": name, **empty}, name)
        with self.assertRaises(LookupError):
            voice_catalog.catalog("ghost", "chime")

    def test_bounds_and_a_failing_plugin(self):
        value = voice_catalog.catalog("nova", "many")
        self.assertEqual(len(value["models"]), voice_catalog.MAX_MODELS)
        self.assertLessEqual(len(value["voices"]), voice_catalog.MAX_VOICES)
        self.assertGreater(len(value["voices"]), 100)
        import json
        self.assertLess(len(json.dumps(value).encode("utf-8")), 131_072 - 1_024, "Fits the app's answer limit")
        with self.assertLogs("hermes.plugins.bighelp", level="WARNING") as logs, \
                self.assertRaises(voice_catalog.VoiceCatalogError):
            voice_catalog.catalog("nova", "broken")
        self.assertNotIn("secret", " ".join(logs.output))


class VoiceCatalogRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def call(self, body):
        return self.fixture.client.post(PREFIX + "/voice-catalog", json=body, headers=self.fixture.headers())

    def test_route_answers_and_refuses_bad_requests(self):
        if not voice_catalog.available():
            self.skipTest("this Hermes has no plugin speech providers")
        self.assertIn(voice_catalog.CAPABILITY, self.fixture.context().json()["features"])
        result = self.call({"profile": "research", "provider": "edge"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json(), {"ok": True, "provider": "edge", "voices": [], "models": []})
        self.assertEqual(result.headers["cache-control"], "no-store")
        for error, status, code in ((LookupError("ghost"), 404, "profile_not_found"),
                                    (voice_catalog.VoiceCatalogError(), 503, "voice_catalog_failed")):
            with self.subTest(code=code), patch.object(voice_catalog, "catalog", side_effect=error):
                result = self.call({"profile": "research", "provider": "phonon"})
            self.assertEqual((result.status_code, result.json()["error"]["code"]), (status, code), result.text)
        self.assertEqual(self.call({"profile": "ghost", "provider": "phonon"}).status_code, 404)
        for body in ({}, {"profile": "research"}, {"provider": "phonon"}, {"profile": "../x", "provider": "phonon"},
                     {"profile": "research", "provider": "../phonon"}, {"profile": "research", "provider": "p" * 65},
                     {"profile": "research", "provider": "phonon", "voice": "Easton"}):
            self.assertEqual(self.call(body).status_code, 422, body)


if __name__ == "__main__":
    unittest.main()
