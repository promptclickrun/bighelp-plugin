from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from loopdy_plugin import naming


class NamingTests(unittest.TestCase):
    def test_saved_settings_that_name_the_loopdy_toolset_reach_the_bighelp_tools(self) -> None:
        # Scheduled jobs and saved tool lists from before 3.0.0 say "loopdy".
        try:
            from tools.registry import registry
            import toolsets
        except ImportError:
            self.skipTest("Hermes is not importable")
        registry.register(name="bighelp_naming_probe", toolset=naming.TOOLSET,
                          schema={"name": "bighelp_naming_probe", "description": "probe",
                                  "parameters": {"type": "object", "properties": {}}},
                          handler=lambda _args, **_kw: "{}")
        try:
            self.assertTrue(naming.register_legacy_toolset_alias())
            self.assertTrue(toolsets.validate_toolset("loopdy"))
            self.assertIn("bighelp_naming_probe", toolsets.resolve_toolset("loopdy"))
        finally:
            registry.deregister("bighelp_naming_probe")

    def test_bighelp_settings_win_and_loopdy_settings_still_count(self) -> None:
        with patch.dict(os.environ, {"LOOPDY_WORKSPACE_GIT_CONFIG": "old"}, clear=False):
            os.environ.pop("BIGHELP_WORKSPACE_GIT_CONFIG", None)
            self.assertEqual(naming.env("WORKSPACE_GIT_CONFIG"), "old")
            with patch.dict(os.environ, {"BIGHELP_WORKSPACE_GIT_CONFIG": "new"}):
                self.assertEqual(naming.env("WORKSPACE_GIT_CONFIG"), "new")
        self.assertEqual(naming.env("NOT_SET_ANYWHERE", "fallback"), "fallback")

    def test_agents_write_bighelp_card_names(self) -> None:
        self.assertEqual(naming.card_input({"schema": "bighelp.card"})["schema"], "loopdy.card")
        self.assertEqual(naming.card_input({"schema": "bighelp.generative_ui"})["schema"],
                         "loopdy.generative_ui")
        self.assertEqual(naming.card_input({"schema": "loopdy.card"})["schema"], "loopdy.card")
        self.assertEqual(naming.card_input("not an object"), "not an object")


if __name__ == "__main__":
    unittest.main()
