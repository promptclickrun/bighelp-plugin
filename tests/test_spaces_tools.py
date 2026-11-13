"""The bot's space tools: where they work, what they refuse and what they answer (docs/SPACES.md)."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from loopdy_plugin import spaces, workspace_root
from loopdy_plugin.loopdy_cards import BighelpCardError
from loopdy_plugin.native_context import NativeAPIError
from loopdy_plugin.tools import register as register_card_tools
from test_spaces import CHAT, COLLECTOR, PAGE, PROFILE, Host, _Context, metric, op

OTHER_CHAT = "20261112_111500_def456"


class ToolCase(unittest.TestCase):
    def setUp(self) -> None:
        self.host = Host(self)
        self.host.write("collect.py", COLLECTOR)
        self.base = {"title": "Commute", "description": "Drive time.", "card": metric(), "html": PAGE,
                     "routine": {"schedule": "every 15m", "script": "collect.py"}}

    def draft(self, session: str | None = CHAT, **changes) -> dict:
        args = {**self.base, **changes}
        for key in [key for key, value in changes.items() if value is None]:
            del args[key]
        return self.host.call("bighelp_draft_space", args, session)

    def refused(self, result: dict, code: str) -> str:
        self.assertEqual({"success": False, "error": code, "message": result.get("message")}, result)
        return result["message"]


class DraftGateTests(ToolCase):
    def test_only_in_a_bighelp_chat_whose_app_draws_spaces(self):
        self.refused(self.draft(session=None), "spaces_unavailable")
        self.host.people.set_renders(OTHER_CHAT, ["html_app"])
        message = self.refused(self.draft(session=OTHER_CHAT), "spaces_unavailable")
        self.assertEqual("This chat's bighelp app can't show spaces. Ask the person to update bighelp.", message)
        self.assertEqual([], self.host.store.all())

    def test_the_card_must_be_basic_and_static(self):
        live = metric()
        live["data_sources"] = [{"id": "feed", "request": {"method": "GET", "url": "https://example.com/x"},
                                 "response": {"format": "json", "root": ""},
                                 "refresh": {"minimum_interval_seconds": 60, "stale_after_seconds": 60,
                                             "expires_at": "2027-01-16T08:00:00Z"}}]
        self.assertIn("live_data_unavailable", self.refused(self.draft(card=live), "card_invalid"))
        place = metric()
        place["elements"]["m"] = {"type": "place", "children": [], "props": {
            "name": "Office", "latitude": 1.0, "longitude": 2.0}}
        self.assertIn("objects_unavailable", self.refused(self.draft(card=place), "card_invalid"))
        self.refused(self.draft(card={"schema": "loopdy.card"}), "card_invalid")
        self.refused(self.draft(card="a card"), "card_invalid")
        self.refused(self.draft(sizes={"huge": metric()}), "card_invalid")
        self.refused(self.draft(sizes={"small": {"nope": 1}}), "card_invalid")
        self.assertEqual(0, self.host.cron.made)
        self.assertEqual([], self.host.store.all())

    def test_exactly_one_page(self):
        self.refused(self.draft(html=None), "invalid_arguments")
        self.refused(self.draft(path="page.html"), "invalid_arguments")
        self.host.write("page.html", PAGE)
        result = self.draft(html=None, path="page.html")
        self.assertNotIn("success", result)
        self.refused(self.draft(html=None, path="page.txt"), "invalid_arguments")

    def test_exactly_one_of_script_and_prompt(self):
        self.refused(self.draft(routine={"schedule": "every 1h"}), "invalid_arguments")
        self.refused(self.draft(routine={"schedule": "every 1h", "script": "collect.py", "prompt": "x"}),
                     "invalid_arguments")
        self.refused(self.draft(routine={"script": "collect.py"}), "invalid_arguments")
        self.refused(self.draft(routine={"schedule": "every 1h", "script": "collect.py", "model": "x"}),
                     "invalid_arguments")

    def test_cadence_floors_and_schedules_that_dont_repeat(self):
        self.refused(self.draft(routine={"schedule": "every 4m", "script": "collect.py"}), "schedule_too_often")
        self.assertNotIn("success", self.draft(routine={"schedule": "every 5m", "script": "collect.py"}))
        message = self.refused(self.draft(routine={"schedule": "every 30m", "prompt": "Look it up."}),
                               "schedule_too_often")
        self.assertIn("uses AI", message)
        self.assertNotIn("success", self.draft(routine={"schedule": "0 * * * *", "prompt": "Look it up."}))
        self.refused(self.draft(routine={"schedule": "2030-01-01T07:00", "script": "collect.py"}), "schedule_invalid")
        self.refused(self.draft(routine={"schedule": "whenever", "script": "collect.py"}), "schedule_invalid")

    def test_the_script_is_a_readable_py_or_sh_file_in_the_working_folder(self):
        outside = self.host.work.parent / "outside.py"
        outside.write_text(COLLECTOR)
        (self.host.work / "escape.py").symlink_to(outside)
        (self.host.work / "inside.py").symlink_to(self.host.work / "collect.py")
        self.host.write("notes.txt", "x")
        (self.host.work / "named.py").symlink_to(self.host.work / "notes.txt")
        (self.host.work / "folder.py").mkdir()
        for script in ("escape.py", str(outside), "../outside.py", "missing.py", "notes.txt", "named.py",
                       "folder.py", ""):
            with self.subTest(script=script):
                self.refused(self.draft(routine={"schedule": "every 15m", "script": script}), "invalid_request")
        # A link that stays in the folder is kept by where it leads; an absolute path in the folder works.
        result = self.draft(routine={"schedule": "every 15m", "script": "inside.py"})
        self.assertEqual(str(self.host.work / "collect.py"),
                         self.host.store.get(result["space_id"]).routine["script_path"])
        self.host.write("tools/collect.sh", "printf '{\"skip\": true}'\n")
        self.assertNotIn("success", self.draft(routine={"schedule": "every 15m",
                                                        "script": str(self.host.work / "tools/collect.sh")}))

    def test_a_script_needs_the_working_folder_on_this_computer(self):
        def container(_profile):
            raise NativeAPIError(409, "workspace_in_container", "This agent works inside a container.")

        with patch.object(workspace_root, "resolve", container):
            message = self.refused(self.draft(), "script_unavailable")
            self.assertIn("prompt", message)
            # A prompt routine still works there.
            self.assertNotIn("success", self.draft(routine={"schedule": "every 1h", "prompt": "Look it up."}))

    def test_a_space_id_must_be_one_of_this_bots(self):
        self.refused(self.draft(space_id="s_" + "0" * 32), "space_not_found")
        self.refused(self.draft(space_id="a_" + "0" * 32), "invalid_arguments")

    def test_its_page_cant_be_changed_with_the_app_tool(self):
        from loopdy_plugin import html_apps
        space_id = self.draft()["space_id"]
        app_id = self.host.store.get(space_id).app_id
        context = _Context()
        html_apps.register(context)
        result = json.loads(context.handlers["bighelp_show_app"](
            {"title": "T", "description": "D", "html": PAGE, "app_id": app_id}, session_id=CHAT))
        self.assertEqual({"success": False, "error": "app_in_space",
                          "message": "Change a space's page with bighelp_draft_space."}, result)


class UpdateToolTests(ToolCase):
    def test_it_works_without_a_chat_and_clears_a_problem(self):
        space_id = self.draft()["space_id"]
        with self.host.store._db(write=True) as db:
            db.execute("UPDATE spaces SET last_problem = 'script_failed' WHERE space_id = ?", (space_id,))
        self.host.clock += 60
        result = self.host.call("bighelp_update_space", {"space_id": space_id, "card": metric(5),
                                                         "data": {"n": 5}}, session=None)
        self.assertEqual({"space_id": space_id, "updated_at": "2027-01-15T08:01:00Z", "size": "wide",
                          "hidden": False, "note": result["note"]}, result)
        space = self.host.store.get(space_id)
        self.assertEqual((5, None, '{"n":5}'), (space.card["elements"]["m"]["props"]["value"]["literal"],
                                                space.last_problem, self.host.pages.get(space.app_id).inline_json))

    def test_refusals(self):
        space_id = self.draft()["space_id"]
        self.refused(self.host.call("bighelp_update_space", {"space_id": space_id}), "invalid_arguments")
        self.refused(self.host.call("bighelp_update_space", {"space_id": "s_" + "1" * 32, "data": 1}),
                     "space_not_found")
        self.refused(self.host.call("bighelp_update_space", {"space_id": space_id, "data": 1, "x": 2}),
                     "invalid_arguments")
        bad = metric()
        bad["elements"]["m"]["type"] = "html_app"
        self.refused(self.host.call("bighelp_update_space", {"space_id": space_id, "card": bad}), "card_invalid")
        self.refused(self.host.call("bighelp_update_space", {"space_id": space_id, "data": "x" * (1 << 20)}),
                     "too_large")

    def test_apply_update_for_the_cli(self):
        space_id = self.draft()["space_id"]
        self.assertEqual("2027-01-15T08:00:00Z",
                         spaces.apply_update(PROFILE, space_id, data=[1, 2], source="cli")["updated_at"])
        with self.assertRaises(spaces.SpaceError) as caught:
            spaces.apply_update(PROFILE, space_id, source="cli")
        self.assertEqual("invalid_request", caught.exception.code)
        with self.assertRaises(spaces.SpaceError) as caught:
            spaces.apply_update(PROFILE, "s_" + "1" * 32, data=1, source="cli")
        self.assertEqual("space_not_found", caught.exception.code)


class SettingsToolTests(ToolCase):
    def test_lists_and_reads(self):
        draft = self.draft()["space_id"]
        live = self.draft(title="Garden", routine={"schedule": "every 2h", "prompt": "Check it."})["space_id"]
        spaces.add(PROFILE, live, 1, op())
        listed = self.host.call("bighelp_space_settings", {}, session=None)["spaces"]
        self.assertEqual([live, draft], [space["space_id"] for space in listed])
        self.assertEqual({"space_id": live, "title": "Garden", "state": "live", "size": "wide", "hidden": False,
                          "updates": "Every 2 hours", "uses_ai": True, "updated_at": "2027-01-15T08:00:00Z",
                          "last_problem": None, "problem_note": None}, listed[0])
        one = self.host.call("bighelp_space_settings", {"space_id": draft}, session=None)
        self.assertEqual((draft, []), (one["space_id"], one["changed"]))

    def test_refusals(self):
        self.refused(self.host.call("bighelp_space_settings", {"size": "small"}), "invalid_arguments")
        self.refused(self.host.call("bighelp_space_settings", {"space_id": "s_" + "1" * 32}), "space_not_found")
        space_id = self.draft()["space_id"]
        self.refused(self.host.call("bighelp_space_settings", {"space_id": space_id, "size": "huge"}),
                     "invalid_arguments")


class RegistrationTests(ToolCase):
    def test_three_tools_with_the_contracts_schemas(self):
        names = ["bighelp_draft_space", "bighelp_update_space", "bighelp_space_settings"]
        self.assertEqual(set(names), set(self.host.context.schemas) & set(names))
        draft = self.host.context.schemas["bighelp_draft_space"]["parameters"]
        self.assertEqual(["title", "description", "card", "routine"], draft["required"])
        self.assertEqual({"title", "description", "card", "sizes", "size", "html", "path", "data", "routine",
                          "space_id"}, set(draft["properties"]))
        self.assertEqual(["schedule"], draft["properties"]["routine"]["required"])
        self.assertEqual("^s_[0-9a-f]{32}$", draft["properties"]["space_id"]["pattern"])
        self.assertEqual(["space_id"], self.host.context.schemas["bighelp_update_space"]["parameters"]["required"])
        self.assertNotIn("required", self.host.context.schemas["bighelp_space_settings"]["parameters"])
        for name in names:
            self.assertNotIn("sudo", self.host.context.schemas[name]["description"])

    def test_the_card_tool_never_makes_a_space(self):
        context = _Context()
        now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
        register_card_tools(context, now=lambda: now, card_renders=lambda _session: {"space_draft"})
        card = json.loads((__import__("test_spaces").FIXTURE).read_text(encoding="utf-8"))
        with self.assertRaises(BighelpCardError) as caught:
            context.handlers["bighelp_render_card"](card, session_id=CHAT)
        self.assertEqual("spaces_tool_only", caught.exception.code)
        self.assertNotIn("space_draft", json.dumps(context.schemas["bighelp_render_card"]["parameters"]))


if __name__ == "__main__":
    unittest.main()
