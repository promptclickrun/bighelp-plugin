"""Hosts that paired with the retired Link lose its keys, and nothing else, from .env."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loopdy_plugin.legacy_cleanup import RETIRED_LINK_KEYS, remove_retired_link_keys

# Made-up values in the shape pairing saved.
OLD_ENV = (
    "# My Hermes settings\n"
    "EXAMPLE_SETTING=keep-me\n"
    "LOOPDY_LINK_BASE_URL=https://link.loopdy.example\n"
    "LOOPDY_LINK_DEVICE_ID=host-fixture\n"
    "LOOPDY_LINK_AUTHORIZATION_EPOCH=3\n"
    "LOOPDY_LINK_SIGNING_PRIVATE_KEY=fixture-signing-key\n"
    "LOOPDY_LINK_AGREEMENT_PRIVATE_KEY=fixture-agreement-key\n"
    "LOOPDY_LINK_ACCOUNT_KEY=fixture-account-key\n"
    "LOOPDY_HOME_TARGET=all\n"
    "LOOPDY_LINK_EXTRA_NOTE=kept because the plugin never wrote it\n"
)
KEPT_ENV = (
    "# My Hermes settings\n"
    "EXAMPLE_SETTING=keep-me\n"
    "LOOPDY_HOME_TARGET=all\n"
    "LOOPDY_LINK_EXTRA_NOTE=kept because the plugin never wrote it\n"
)


class RetiredLinkKeyTests(unittest.TestCase):
    def setUp(self):
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.env = self.home / ".env"
        token = set_hermes_home_override(self.home)
        self.addCleanup(reset_hermes_home_override, token)

    def test_only_the_keys_pairing_saved_are_removed(self):
        self.env.write_text(OLD_ENV, encoding="utf-8")
        values = {key: "fixture" for key in RETIRED_LINK_KEYS}
        with patch.dict(os.environ, values):
            self.assertEqual(remove_retired_link_keys(), len(RETIRED_LINK_KEYS))
        self.assertEqual(self.env.read_text(encoding="utf-8"), KEPT_ENV)

    def test_a_host_without_the_keys_never_has_its_env_file_written(self):
        self.env.write_text(KEPT_ENV, encoding="utf-8")
        os.utime(self.env, (1_000_000, 1_000_000))
        with patch.dict(os.environ, {}, clear=False):
            for key in RETIRED_LINK_KEYS:
                os.environ.pop(key, None)
            self.assertEqual(remove_retired_link_keys(), 0)
        self.assertEqual(self.env.read_text(encoding="utf-8"), KEPT_ENV)
        self.assertEqual(self.env.stat().st_mtime, 1_000_000)

    def test_a_second_run_finds_nothing_to_do(self):
        self.env.write_text(OLD_ENV, encoding="utf-8")
        with patch.dict(os.environ, {"LOOPDY_LINK_DEVICE_ID": "host-fixture"}):
            self.assertEqual(remove_retired_link_keys(), 6 - 5)
        # Hermes loaded the rest into the environment too; the next start removes them.
        with patch.dict(os.environ, {key: "fixture" for key in RETIRED_LINK_KEYS}):
            self.assertEqual(remove_retired_link_keys(), 5)
            self.assertEqual(remove_retired_link_keys(), 0)
        self.assertEqual(self.env.read_text(encoding="utf-8"), KEPT_ENV)

    def test_a_failure_never_stops_the_plugin(self):
        def refuse(_key):
            raise PermissionError("read-only fixture")

        with patch.dict(os.environ, {"LOOPDY_LINK_DEVICE_ID": "host-fixture"}), \
                self.assertLogs("hermes.plugins.bighelp", level="WARNING") as logs:
            self.assertEqual(remove_retired_link_keys(remover=refuse), 0)
        self.assertIn("legacy_cleanup_failed", logs.output[0])
        self.assertNotIn("read-only fixture", logs.output[0])


if __name__ == "__main__":
    unittest.main()
