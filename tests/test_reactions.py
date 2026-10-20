from __future__ import annotations

import contextvars
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from loopdy_plugin import people, reactions

CHAT = "20261020_101500_abc123"


def _registered_handler(module) -> object:
    handlers: dict = {}

    class Context:
        def register_tool(self, **kwargs):
            handlers[kwargs["name"]] = kwargs["handler"]

        def register_hook(self, *_args, **_kwargs):
            pass

    module.register(Context())
    return next(iter(handlers.values()))


def _blanked_turn(call):
    """Run ``call`` the way Hermes 0.22 runs a tool in a turn that began with a model switch:
    the turn bound its session, then a nested bind/clear blanked every session var."""
    from gateway.session_context import clear_session_vars, set_session_vars

    def run():
        set_session_vars(session_key=CHAT, session_id=CHAT, source="bighelp", ui_session_id="runtime-1")
        clear_session_vars(set_session_vars(session_key=CHAT, session_id=CHAT))
        return call()
    return contextvars.Context().run(run)


class ReactionSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        environment = patch.dict(os.environ, {"HERMES_HOME": str(home), "HOME": str(home)})
        environment.start()
        self.addCleanup(environment.stop)
        if not reactions.supported():
            self.skipTest("Hermes session reactions unavailable")
        from hermes_state_registry import acquire, release_or_close
        database = acquire(home / "state.db")
        try:
            database.create_session(CHAT, source="bighelp")
            self.row = database.append_message(CHAT, "user", "Good news: it shipped!")
            database.append_message(CHAT, "assistant", "Nice.")
        finally:
            release_or_close(database)
        self.home = home

    def _read_reactions(self):
        from hermes_state_registry import acquire, release_or_close
        database = acquire(self.home / "state.db")
        try:
            return database.get_message_reactions(CHAT, self.row)
        finally:
            release_or_close(database)

    def test_reacts_when_the_turn_lost_its_session_env(self):
        handler = _registered_handler(reactions)
        events = []

        def emit(event, payload):
            from gateway.session_context import get_session_env
            events.append((event, get_session_env("HERMES_SESSION_ID", ""), payload["row_id"]))
            return True

        with patch("tools.desktop_ui.emit", side_effect=emit):
            value = json.loads(_blanked_turn(lambda: handler({"emoji": "❤️"}, task_id=CHAT, session_id=CHAT)))
        self.assertTrue(value["success"], value)
        self.assertEqual(value["rowId"], self.row)
        self.assertEqual([(r["emoji"], r["author"]) for r in self._read_reactions()], [("❤️", "agent")])
        # The live event goes to the chat the reaction was saved in, so the app can paint it.
        self.assertEqual(events, [("message.reaction", CHAT, self.row)])

    def test_still_reads_the_session_env_when_hermes_passes_no_session(self):
        from gateway.session_context import set_session_vars

        def run():
            set_session_vars(session_key=CHAT, session_id=CHAT, source="bighelp")
            return reactions.react("👍")
        with patch("tools.desktop_ui.emit", return_value=False):
            value = json.loads(contextvars.Context().run(run))
        self.assertTrue(value["success"], value)
        self.assertEqual([r["emoji"] for r in self._read_reactions()], ["👍"])

    def test_no_session_anywhere_is_still_a_clear_error(self):
        value = json.loads(_blanked_turn(lambda: reactions.react("👍")))
        self.assertEqual(value["error"], "reaction_session_unavailable")

    def test_a_malformed_session_is_never_used(self):
        handler = _registered_handler(reactions)
        for bad in ("", "a b", "x\n", "x" * 300, 42):
            value = json.loads(_blanked_turn(lambda: handler({"emoji": "👍"}, session_id=bad)))
            self.assertEqual(value["error"], "reaction_session_unavailable", bad)


class PeopleSessionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        environment = patch.dict(os.environ, {"HERMES_HOME": str(home), "HOME": str(home)})
        environment.start()
        self.addCleanup(environment.stop)
        people._stores.clear()
        self.addCleanup(people._stores.clear)
        if not people.available() or not reactions.supported():
            self.skipTest("Hermes profile helpers unavailable")

    def test_knows_who_is_talking_when_the_turn_lost_its_session_env(self):
        people.store_for_profile("default").note(CHAT, "11111111-1111-4111-8111-111111111111", "Colt")
        handler = _registered_handler(people)
        value = json.loads(_blanked_turn(lambda: handler({}, task_id=CHAT, session_id=CHAT)))
        self.assertEqual((value["success"], value["talking_with"]), (True, "Colt"))


if __name__ == "__main__":
    unittest.main()
