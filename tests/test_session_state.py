"""Notifications read an agent's own Hermes session store, read-only."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_state import SessionDB


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name).resolve()
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_the_default_profile_store_opens_read_only(self):
        from loopdy_plugin.session_state import open_profile_store

        db = SessionDB(db_path=self.home / "state.db")
        db.create_session("session-one", source="bighelp")
        db.close()
        self.assertEqual(open_profile_store("default", lambda store: store.get_session("session-one")["id"]),
                         "session-one")

    def test_a_missing_store_profile_or_write_request_is_refused(self):
        from loopdy_plugin.session_state import SessionStateNotFound, open_profile_store

        with self.assertRaises(SessionStateNotFound):
            open_profile_store("default", lambda store: store)
        with self.assertRaises(LookupError):
            open_profile_store("missing", lambda store: store)
        with self.assertRaises(LookupError):
            open_profile_store("default", lambda store: store, read_only=False)


if __name__ == "__main__":
    unittest.main()
