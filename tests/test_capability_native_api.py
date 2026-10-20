"""Hermes helpers run inside one agent's profile, never the whole process."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ProfileScopeTests(unittest.TestCase):
    def test_profile_scope_selects_the_agent_home_and_restores_it(self):
        from hermes_constants import get_hermes_home
        from loopdy_plugin.workspace_capabilities import profile_scope

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            (home / "profiles" / "research").mkdir(parents=True)
            (home / "profiles" / "research" / "config.yaml").write_text("model: {}\n", encoding="utf-8")
            with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
                with profile_scope("research"):
                    self.assertEqual(get_hermes_home().resolve(), home / "profiles" / "research")
                self.assertEqual(get_hermes_home().resolve(), home)
                with self.assertRaisesRegex(ValueError, "no longer exists"):
                    with profile_scope("missing"):
                        pass


if __name__ == "__main__":
    unittest.main()
