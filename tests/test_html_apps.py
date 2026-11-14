"""HTML apps: the tools, the store and how an app's data is read (docs/HTML_APPS.md)."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loopdy_plugin import html_apps, people, workspace_root
from loopdy_plugin.generative_ui import extract_rendered_envelope
from loopdy_plugin.html_apps import AppError, HtmlAppStore
from loopdy_plugin.people import PeopleStore

CHAT = "20261107_101500_abc123"
OTHER_CHAT = "20261107_111500_def456"
PAGE = "<!doctype html><title>Trip</title><p>Hello</p>"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "loopdy_card_v1" / "static-html-app.json"


class _Context:
    profile_name = "personal"

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.schemas: dict[str, dict] = {}

    def register_tool(self, *, name, handler, schema, **_kwargs) -> None:
        self.handlers[name] = handler
        self.schemas[name] = schema


class Host:
    """One agent: its Hermes home, its working folder, and the plugin's store for it."""

    def __init__(self, case: unittest.TestCase, name: str = "personal") -> None:
        folder = tempfile.TemporaryDirectory(prefix="bighelp-apps-", dir=Path(tempfile.gettempdir()).resolve())
        case.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "home"
        self.work = Path(folder.name) / "work"
        self.home.mkdir()
        self.work.mkdir()
        self.work = self.work.resolve()
        self.people = PeopleStore(self.home / "plugin-data" / "loopdy")
        self.people.set_renders(CHAT, ["contact", "html_app"])
        self.people.set_renders(OTHER_CHAT, ["contact"])
        self.store = html_apps.store_for_home(self.home)
        self.context = _Context()
        self.context.profile_name = name
        html_apps.register(self.context)

    def call(self, tool: str, args: dict, session: str | None = CHAT) -> dict:
        kwargs = {} if session is None else {"session_id": session}
        with patch.object(html_apps, "current_home", lambda: self.home), \
                patch.object(people, "current_store", lambda: self.people), \
                patch.object(workspace_root, "resolve",
                             lambda _profile: workspace_root.Workspace(self.work, "config")), \
                patch.object(workspace_root, "hermes_folders", lambda _profile: frozenset()):
            return json.loads(self.context.handlers[tool](args, **kwargs))

    def show(self, **args) -> dict:
        return self.call("bighelp_show_app", {"title": "Trip planner", "description": "Plan a road trip.",
                                              "html": PAGE, **args})

    def update(self, app_id: str, data, session: str | None = CHAT) -> dict:
        return self.call("bighelp_update_app_data", {"app_id": app_id, "data": data}, session)

    def props(self, result: dict) -> dict:
        self.assert_ok(result)
        card = extract_rendered_envelope({key: result[key] for key in
                                          ("schema", "version", "card", "display_markdown", "instruction")})
        return card["elements"]["app"]["props"]

    @staticmethod
    def assert_ok(result: dict) -> None:
        if "success" in result:
            raise AssertionError(result)


class ToolCase(unittest.TestCase):
    def setUp(self) -> None:
        self.host = Host(self)

    def assert_refused(self, result: dict, code: str) -> str:
        self.assertEqual({"success": False, "error": code, "message": result.get("message")}, result)
        return result["message"]


class ShowAppTests(ToolCase):
    def test_a_new_app_gets_its_card_and_is_kept(self):
        result = self.host.show(sends_answers=True, data={"stops": ["Reno"]})
        props = self.host.props(result)
        self.assertRegex(props["app_id"], r"^a_[0-9a-f]{32}$")
        self.assertEqual({"app_id": props["app_id"], "description": "Plan a road trip.", "sends_answers": True,
                          "live": True}, props)
        self.assertEqual(props["app_id"], result["app_id"])
        self.assertEqual(1, result["revision"])
        self.assertEqual("Trip planner", result["card"]["title"])
        self.assertEqual("An app to open in Bighelp: Trip planner.", result["card"]["spoken_summary"])
        self.assertIn("Open App", result["note"])
        self.assertIn("next message", result["note"])
        app = self.host.store.get(props["app_id"])
        self.assertEqual((CHAT, 1, hashlib.sha256(PAGE.encode()).hexdigest(), len(PAGE.encode())),
                         (app.session_id, app.revision, app.html_sha256, app.html_bytes))
        # The page is its own file, and the host's paths stay out of what the agent reads.
        self.assertEqual(PAGE.encode(), self.host.store.read_html(props["app_id"])[1])
        self.assertNotIn(str(self.host.home), json.dumps(result))

    def test_the_card_is_the_shared_fixture(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        props = fixture["elements"]["app"]["props"]
        self.assertEqual(fixture, html_apps.build_card(
            app_id=props["app_id"], title=fixture["title"], description=props["description"],
            sends_answers=props["sends_answers"], live=props["live"]))

    def test_without_answers_or_data_the_card_says_so(self):
        result = self.host.show()
        self.assertEqual((False, False), (self.host.props(result)["sends_answers"], self.host.props(result)["live"]))
        self.assertNotIn("next message", result["note"])

    def test_texts_are_collapsed_to_single_spaces(self):
        result = self.host.show(title="  Trip \n planner ", description="Plan\n\n a   road\ttrip. ")
        self.assertEqual("Plan a road trip.", self.host.props(result)["description"])
        self.assertEqual("Trip planner", result["card"]["title"])

    def test_a_page_from_a_file_in_the_working_folder(self):
        (self.host.work / "site").mkdir()
        (self.host.work / "site" / "app.HTML").write_text(PAGE, encoding="utf-8")
        for path in ("site/app.HTML", str(self.host.work / "site" / "app.HTML")):
            args = {"title": "T", "description": "D", "path": path}
            result = self.host.call("bighelp_show_app", args)
            self.host.assert_ok(result)
            self.assertEqual(PAGE.encode(), self.host.store.read_html(result["app_id"])[1])

    def test_a_path_outside_what_the_agent_may_read_is_refused(self):
        outside = self.host.work.parent / "secret.html"
        outside.write_text("<p>not yours</p>")
        (self.host.work / "link.html").symlink_to(outside)
        (self.host.work / "notes.txt").write_text("hello")
        for path in (str(outside), "../secret.html", "link.html", "missing.html", str(self.host.home / "x.html")):
            with self.subTest(path=path):
                self.assert_refused(self.host.call("bighelp_show_app", {"title": "T", "description": "D",
                                                                       "path": path}), "invalid_path")
        # Only pages: .html or .htm.
        self.assert_refused(self.host.call("bighelp_show_app", {"title": "T", "description": "D",
                                                               "path": "notes.txt"}), "invalid_arguments")
        self.assertEqual([], os.listdir(self.host.store.pages))

    def test_a_page_that_is_not_utf8_is_refused(self):
        (self.host.work / "latin.html").write_bytes(b"<p>caf\xe9</p>")
        self.assert_refused(self.host.call("bighelp_show_app", {"title": "T", "description": "D",
                                                               "path": "latin.html"}), "invalid_arguments")
        self.assert_refused(self.host.show(html="<p>\ud800</p>"), "invalid_arguments")

    def test_a_page_over_16_mib_is_refused_and_exactly_16_mib_is_kept(self):
        limit = html_apps.MAX_HTML_BYTES
        self.assertEqual(16_777_216, limit)
        self.assert_refused(self.host.show(html="x" * (limit + 1)), "html_too_large")
        self.assert_refused(self.host.show(html="é" * (limit // 2 + 1)), "html_too_large")
        (self.host.work / "big.html").write_text("x" * (limit + 1))
        self.assert_refused(self.host.call("bighelp_show_app", {"title": "T", "description": "D",
                                                               "path": "big.html"}), "html_too_large")
        result = self.host.show(html="x" * limit)
        self.host.assert_ok(result)
        self.assertEqual(limit, self.host.store.get(result["app_id"]).html_bytes)

    def test_a_chat_whose_app_cant_draw_apps_is_refused(self):
        update = "This chat's Bighelp app can't show apps. Ask the person to update Bighelp."
        self.assertEqual(update, self.assert_refused(self.host.call(
            "bighelp_show_app", {"title": "T", "description": "D", "html": PAGE}, session=OTHER_CHAT),
            "apps_unavailable"))
        self.host.people.set_renders(CHAT, [])
        self.assertEqual(update, self.assert_refused(self.host.show(), "apps_unavailable"))
        self.assertEqual([], os.listdir(self.host.store.pages))

    def test_only_a_bighelp_chat_may_show_an_app(self):
        for session in (None, "", "not a chat id"):
            with self.subTest(session=session):
                result = self.host.call("bighelp_show_app", {"title": "T", "description": "D", "html": PAGE},
                                        session=session)
                self.assert_refused(result, "apps_unavailable")

    def test_bad_arguments_are_refused_in_plain_words(self):
        good = {"title": "T", "description": "D", "html": PAGE}
        cases = {
            "both page and path": {**good, "path": "a.html"},
            "neither page nor path": {"title": "T", "description": "D"},
            "both kinds of data": {**good, "data": {"a": 1}, "data_path": "d.json"},
            "no title": {"description": "D", "html": PAGE},
            "empty title": {**good, "title": "   "},
            "title of 61 characters": {**good, "title": "t" * 61},
            "description of 241 characters": {**good, "description": "d" * 241},
            "a control character": {**good, "title": "bell\x07"},
            "sends_answers as text": {**good, "sends_answers": "yes"},
            "app_id of the wrong shape": {**good, "app_id": "a_123"},
            "an unknown argument": {**good, "color": "red"},
            "html as a number": {**good, "html": 5},
            "an empty page": {**good, "html": " \n"},
            "not an object": "html",
        }
        for name, args in cases.items():
            with self.subTest(name):
                result = self.host.call("bighelp_show_app", args)
                self.assertFalse(result["success"], result)
                self.assertIn(result["error"], ("invalid_arguments",))
                self.assertGreater(len(result["message"]), 10)
        self.host.assert_ok(self.host.show(title="t" * 60, description="d" * 240))
        self.assertEqual(1, len(os.listdir(self.host.store.pages)))

    def test_inline_data_is_limited_as_compact_json(self):
        self.assertEqual(1_048_576, html_apps.MAX_DATA_BYTES)
        # 1,048,576 bytes of compact JSON: ["xxx…"] is the text plus four bytes of structure.
        fits = ["x" * (html_apps.MAX_DATA_BYTES - 4)]
        self.assertEqual(html_apps.MAX_DATA_BYTES, len(json.dumps(fits, separators=(",", ":"))))
        self.host.assert_ok(self.host.show(data=fits))
        self.assert_refused(self.host.show(data=["x" * (html_apps.MAX_DATA_BYTES - 3)]), "data_too_large")
        self.assert_refused(self.host.show(data=float("nan")), "invalid_arguments")

    def test_data_from_a_file_makes_the_app_live_and_warns_when_the_file_isnt_ready(self):
        result = self.host.show(data_path="todo.json")
        self.assertTrue(self.host.props(result)["live"])
        self.assertIn("is missing", result["note"])
        (self.host.work / "todo.json").write_text("[1, 2]")
        self.assertNotIn("data file", self.host.show(data_path="todo.json")["note"])
        (self.host.work / "todo.json").write_text("nope")
        self.assertIn("isn't JSON", self.host.show(data_path="todo.json")["note"])
        for path in ("../todo.json", str(self.host.home / "todo.json")):
            self.assert_refused(self.host.show(data_path=path), "invalid_path")

    def test_replacing_an_app_keeps_its_id_and_bumps_the_revision(self):
        first = self.host.show(data={"v": 1}, sends_answers=True)
        app_id = first["app_id"]
        created = self.host.store.get(app_id).created
        old_page = self.host.store._page(app_id, self.host.store.get(app_id).html_sha256)
        self.assertTrue(old_page.exists())
        second = self.host.call("bighelp_show_app", {
            "app_id": app_id, "title": "Trip planner 2", "description": "Now with maps.", "html": "<p>v2</p>"},
            session=CHAT)
        props = self.host.props(second)
        self.assertEqual((app_id, 2), (second["app_id"], second["revision"]))
        self.assertEqual({"app_id": app_id, "description": "Now with maps.", "sends_answers": False,
                          "live": False}, props)
        app = self.host.store.get(app_id)
        self.assertEqual(("Trip planner 2", "Now with maps.", False, "none", None, 2),
                         (app.title, app.description, app.sends_answers, app.data_kind, app.inline_json,
                          app.revision))
        self.assertEqual(created, app.created)
        self.assertEqual(b"<p>v2</p>", self.host.store.read_html(app_id)[1])
        self.assertFalse(old_page.exists())
        self.assertEqual(1, len(os.listdir(self.host.store.pages)))

    def test_an_unknown_app_id_is_refused(self):
        result = self.host.show(app_id="a_" + "9" * 32)
        self.assertIn("isn't one of your apps", self.assert_refused(result, "app_not_found"))
        self.assertEqual([], os.listdir(self.host.store.pages))

    def test_a_spaces_page_is_changed_only_through_its_space(self):
        app_id = "a_" + "5" * 32
        self.host.store.save(app_id, replace=False, session_id=CHAT, title="Commute", description="D",
                             sends_answers=False, html=b"<p>space</p>", space_id="s_" + "5" * 32)
        result = self.host.show(app_id=app_id)
        self.assertEqual("Change a space's page with bighelp_draft_space.", self.assert_refused(result, "app_in_space"))
        self.assertEqual(b"<p>space</p>", self.host.store.read_html(app_id)[1])

    def test_another_agents_app_cant_be_replaced(self):
        other = Host(self, "research")
        theirs = other.show()["app_id"]
        self.assert_refused(self.host.show(app_id=theirs), "app_not_found")
        self.assertEqual(1, other.store.get(theirs).revision)
        self.assertIsNone(self.host.store.get(theirs))


class UpdateAppDataTests(ToolCase):
    def test_data_is_replaced_and_null_clears_it(self):
        app_id = self.host.show()["app_id"]
        result = self.host.update(app_id, {"score": 3})
        self.assertEqual({"app_id", "updated_at", "note"}, set(result))
        self.assertRegex(result["updated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual("The open app gets the new data within a few seconds. Don't post the card again.",
                         result["note"])
        answer = html_apps.data_response(self.host.store.get(app_id), "personal")
        self.assertEqual({"score": 3}, answer["data"])
        self.assertEqual(hashlib.sha256(b'{"score":3}').hexdigest(), answer["dataSha256"])
        self.host.update(app_id, [0])
        self.assertEqual([0], html_apps.data_response(self.host.store.get(app_id), "personal")["data"])
        self.host.update(app_id, None)
        self.assertEqual({"appId": app_id, "data": None, "updatedAt": None, "dataSha256": None, "problem": None},
                         html_apps.data_response(self.host.store.get(app_id), "personal"))
        # The page and revision are untouched by data.
        self.assertEqual(1, self.host.store.get(app_id).revision)

    def test_it_needs_no_bighelp_chat(self):
        app_id = self.host.show()["app_id"]
        self.host.assert_ok(self.host.update(app_id, {"later": True}, session=None))

    def test_an_app_that_reads_a_file_says_to_write_the_file(self):
        app_id = self.host.show(data_path="data/todo.json")["app_id"]
        message = self.assert_refused(self.host.update(app_id, [1]), "data_from_file")
        self.assertEqual("This app reads its data from data/todo.json. Write that file instead.", message)

    def test_unknown_foreign_and_malformed_apps_are_refused(self):
        other = Host(self, "research")
        theirs = other.show()["app_id"]
        self.assert_refused(self.host.update(theirs, [1]), "app_not_found")
        self.assert_refused(self.host.update("a_" + "9" * 32, [1]), "app_not_found")
        self.assert_refused(self.host.update("a_123", [1]), "invalid_arguments")
        self.assert_refused(self.host.call("bighelp_update_app_data", {"app_id": theirs}), "invalid_arguments")
        self.assertIsNone(other.store.get(theirs).inline_json)

    def test_data_is_limited_like_inline_data(self):
        app_id = self.host.show()["app_id"]
        self.assert_refused(self.host.update(app_id, ["x" * html_apps.MAX_DATA_BYTES]), "data_too_large")
        self.host.assert_ok(self.host.update(app_id, ["x" * (html_apps.MAX_DATA_BYTES - 4)]))


class DataFileTests(ToolCase):
    """A data_path app reads its file fresh on every request."""

    def read(self, app_id: str) -> dict:
        with patch.object(workspace_root, "resolve",
                          lambda _profile: workspace_root.Workspace(self.host.work, "config")), \
                patch.object(workspace_root, "hermes_folders", lambda _profile: frozenset()):
            return html_apps.data_response(self.host.store.get(app_id), "personal")

    def test_every_request_reads_the_file_again(self):
        app_id = self.host.show(data_path="data.json")["app_id"]
        path = self.host.work / "data.json"
        self.assertEqual({"appId": app_id, "data": None, "updatedAt": None, "dataSha256": None,
                          "problem": "missing"}, self.read(app_id))
        path.write_text('{"b": 1, "a": [1, 2]}')
        os.utime(path, (1_800_000_000, 1_800_000_000))
        first = self.read(app_id)
        self.assertEqual({"b": 1, "a": [1, 2]}, first["data"])
        self.assertEqual(["b", "a"], list(first["data"]))
        self.assertEqual("2027-01-15T08:00:00Z", first["updatedAt"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), first["dataSha256"])
        self.assertIsNone(first["problem"])
        path.write_text('{"b": 2}')
        os.utime(path, (1_800_000_100, 1_800_000_100))
        second = self.read(app_id)
        self.assertEqual({"b": 2}, second["data"])
        self.assertNotEqual(first["dataSha256"], second["dataSha256"])
        self.assertEqual("2027-01-15T08:01:40Z", second["updatedAt"])

    def test_each_problem_has_its_own_name_and_no_data(self):
        app_id = self.host.show(data_path="data.json")["app_id"]
        path = self.host.work / "data.json"
        cases = {
            "invalid": [b"not json", b"", b"[1, 2", b"\xff\xfe[1]", b'{"a": NaN}', b"[Infinity]", b"[1e999]"],
            "too_large": [b"[" + b"1," * (html_apps.MAX_DATA_BYTES // 2) + b"1]"],
        }
        for problem, contents in cases.items():
            for content in contents:
                with self.subTest(problem=problem, content=content[:20]):
                    path.write_bytes(content)
                    self.assertEqual({"appId": app_id, "data": None, "updatedAt": None, "dataSha256": None,
                                      "problem": problem}, self.read(app_id))
        path.unlink()
        self.assertEqual("missing", self.read(app_id)["problem"])

    def test_exactly_the_limit_is_read(self):
        app_id = self.host.show(data_path="data.json")["app_id"]
        (self.host.work / "data.json").write_text(json.dumps(["x" * (html_apps.MAX_DATA_BYTES - 4)],
                                                             separators=(",", ":")))
        self.assertIsNone(self.read(app_id)["problem"])

    def test_a_file_that_moves_outside_the_working_folder_is_just_missing(self):
        app_id = self.host.show(data_path="data.json")["app_id"]
        outside = self.host.work.parent / "elsewhere.json"
        outside.write_text("[1]")
        (self.host.work / "data.json").symlink_to(outside)
        self.assertEqual("missing", self.read(app_id)["problem"])

    def test_inline_and_empty_apps(self):
        inline = self.host.show(data={"k": "é"})["app_id"]
        answer = self.read(inline)
        self.assertEqual({"k": "é"}, answer["data"])
        self.assertIsNotNone(answer["updatedAt"])
        self.assertIsNone(answer["problem"])
        self.assertEqual({"data": None, "updatedAt": None, "dataSha256": None, "problem": None},
                         {key: value for key, value in self.read(self.host.show()["app_id"]).items()
                          if key != "appId"})


class StoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="bighelp-app-store-", dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(folder.cleanup)
        self.clock = 1_000.0
        self.store = HtmlAppStore(Path(folder.name), now=lambda: self.clock)

    def add(self, number: int, **extra):
        self.clock += 10
        app_id = f"a_{number:032x}"
        self.store.save(app_id, replace=False, session_id=CHAT, title=f"App {number}", description="D",
                        sends_answers=False, html=f"<p>{number}</p>".encode(), **extra)
        return app_id

    def test_the_least_recently_updated_go_first(self):
        with patch.object(html_apps, "MAX_APPS", 3):
            first, second, third = (self.add(n) for n in (1, 2, 3))
            self.clock += 10
            self.store.set_inline_data(first, "[1]")  # the oldest app is now the newest
            fourth = self.add(4)
            self.assertEqual([True, False, True, True],
                             [self.store.get(app) is not None for app in (first, second, third, fourth)])
            fifth = self.add(5)
            self.assertEqual([True, False, True, True],
                             [self.store.get(app) is not None for app in (first, third, fourth, fifth)])
            # A pruned app's page goes with it.
            self.assertEqual(sorted(f"{app}.{self.store.get(app).html_sha256}.html" for app in (first, fourth, fifth)),
                             sorted(os.listdir(self.store.pages)))

    def test_the_default_keeps_200(self):
        self.assertEqual(200, html_apps.MAX_APPS)

    def test_the_app_just_written_is_never_pruned(self):
        with patch.object(html_apps, "MAX_APPS", 1):
            self.add(1)
            kept = self.add(2)
            self.assertIsNotNone(self.store.get(kept))
            self.assertIsNone(self.store.get(f"a_{1:032x}"))

    def test_a_failed_replace_leaves_the_app_as_it_was(self):
        app_id = self.add(1)
        with self.assertRaises(AppError):
            self.store.save("a_" + "9" * 32, replace=True, session_id=CHAT, title="T", description="D",
                            sends_answers=False, html=b"<p>x</p>")
        self.assertEqual([f"{app_id}.{self.store.get(app_id).html_sha256}.html"], os.listdir(self.store.pages))

    def test_two_stores_over_one_folder_see_each_other(self):
        # The dashboard's routes and the agent's tools are two plugin copies on one folder.
        other = HtmlAppStore(self.store.path.parent)
        app_id = self.add(1)
        self.assertEqual(b"<p>1</p>", other.read_html(app_id)[1])

    def test_the_database_is_private_to_the_host_user(self):
        self.assertEqual(0o600, self.store.path.stat().st_mode & 0o777)

    def test_a_spaces_page_is_never_pruned(self):
        with patch.object(html_apps, "MAX_APPS", 2):
            page = self.add(1, space_id="s_" + "1" * 32)
            first, second = self.add(2), self.add(3)
            third = self.add(4)
            # Two apps of the agent's own are kept beside the space's page, which is the oldest.
            self.assertEqual([True, False, True, True],
                             [self.store.get(app) is not None for app in (page, first, second, third)])
            self.assertEqual("s_" + "1" * 32, self.store.get(page).space_id)

    def test_replacing_a_page_keeps_its_space(self):
        app_id = self.add(1, space_id="s_" + "1" * 32)
        self.store.save(app_id, replace=True, session_id=CHAT, title="T", description="D", sends_answers=False,
                        html=b"<p>new</p>")
        self.assertEqual(("s_" + "1" * 32, 2), (self.store.get(app_id).space_id, self.store.get(app_id).revision))

    def test_delete_takes_the_row_and_every_page(self):
        app_id = self.add(1)
        self.store.save(app_id, replace=True, session_id=CHAT, title="T", description="D", sends_answers=False,
                        html=b"<p>two</p>")
        stray = self.store.pages / f"{app_id}.{'0' * 64}.html"
        stray.write_bytes(b"left over")
        other = self.add(2)
        self.assertTrue(self.store.delete(app_id))
        self.assertIsNone(self.store.get(app_id))
        self.assertEqual([f"{other}.{self.store.get(other).html_sha256}.html"], os.listdir(self.store.pages))
        self.assertFalse(self.store.delete(app_id))

    def test_a_store_from_before_spaces_gets_the_space_column(self):
        folder = Path(self.store.path.parent) / "old"
        folder.mkdir()
        # The schema this plugin wrote before spaces, with one app in it.
        db = sqlite3.connect(folder / "html-apps.sqlite3")
        db.execute("""
            CREATE TABLE apps(
                app_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL, sends_answers INTEGER NOT NULL, data_kind TEXT NOT NULL,
                data_path TEXT, inline_json TEXT, data_updated REAL, revision INTEGER NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, html_sha256 TEXT NOT NULL,
                html_bytes INTEGER NOT NULL)""")
        old_id = "a_" + "7" * 32
        db.execute("INSERT INTO apps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (old_id, CHAT, "Old", "D", 0, "none", None, None, None, 1, 1.0, 1.0, "0" * 64, 3))
        db.commit()
        db.close()
        store = HtmlAppStore(folder)
        self.assertIsNone(store.get(old_id).space_id)
        # A second plugin copy opening it again finds the column already there.
        HtmlAppStore(folder)
        new_id = "a_" + "8" * 32
        store.save(new_id, replace=False, session_id=CHAT, title="New", description="D", sends_answers=False,
                   html=b"<p>n</p>", space_id="s_" + "8" * 32)
        self.assertEqual("s_" + "8" * 32, HtmlAppStore(folder).get(new_id).space_id)


if __name__ == "__main__":
    unittest.main()
