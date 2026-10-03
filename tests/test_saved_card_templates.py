"""bighelp_save_ui_template: agents save card layouts they will reuse.

Every value here is made up.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import yaml

from loopdy_plugin.generative_ui import extract_rendered_envelope
from loopdy_plugin.loopdy_cards import canonical_json, validate_card_result
from loopdy_plugin.registration import register as register_plugin
from loopdy_plugin.store import BighelpStore
from loopdy_plugin.tools import register as register_tools


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CARD_FIXTURE = PLUGIN_ROOT / "fixtures" / "loopdy_card_v1" / "static-metrics.json"
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
SAVE = "bighelp_save_ui_template"
INTENT = (
    "Save a reusable UI card layout for future rendering with fresh data. This is optional, "
    "not a required step after creating a card. Use it when the user requests reuse, has "
    "repeatedly asked for similar cards, or the layout supports a likely recurring need. Skip "
    "one-off cards and unfinished experiments. Save the structure and parameter definitions, "
    "not personal data or current values. Before creating a template, check for an existing "
    "match and reuse or update it. Saving does not publish or share the template."
)


def _layout() -> dict[str, Any]:
    return {
        "schema": "bighelp.card",
        "version": 1,
        "title": "{{title}}",
        "spoken_summary": "{{steps}} of {{goal}} steps so far.",
        "data_sources": [],
        "root": "card",
        "elements": {
            "card": {
                "type": "card",
                "props": {"title": "{{title}}", "subtitle": "Goal {{goal}} steps"},
                "children": ["numbers", "status"],
            },
            "numbers": {
                "type": "hstack",
                "props": {"spacing": "medium"},
                "children": ["steps", "progress"],
            },
            "steps": {
                "type": "metric",
                "props": {
                    "label": "Steps",
                    "value": {"literal": "{{steps}}"},
                    "format": {"style": "integer"},
                    "semantic": "positive",
                },
                "children": [],
            },
            "progress": {
                "type": "progress",
                "props": {
                    "label": "Goal",
                    "value": {"literal": "{{steps}}"},
                    "maximum": {"literal": "{{goal}}"},
                },
                "children": [],
            },
            "status": {
                "type": "badge",
                "props": {"value": {"literal": "{{status}}"}, "semantic": "accent"},
                "children": [],
            },
        },
    }


def _parameters() -> list[dict[str, Any]]:
    return [
        {"name": "title", "type": "string", "description": "Card heading", "required": False,
         "default": "Daily steps"},
        {"name": "steps", "type": "integer", "description": "Steps so far today", "required": True},
        {"name": "goal", "type": "integer", "description": "Daily step goal", "required": False,
         "default": 8000},
        {"name": "status", "type": "string", "description": "How the day is going", "required": True,
         "enum": ["On track", "Behind"]},
    ]


def _save_arguments(**overrides: Any) -> dict[str, Any]:
    value = {
        "name": "Daily steps",
        "purpose": "Show today's step count against a daily goal.",
        "usage_guidance": "Pick this when the user asks how their walking is going today.",
        "layout": _layout(),
        "parameters": _parameters(),
    }
    value.update(overrides)
    return value


def _installed_template(template_id: str = "build-health") -> dict[str, Any]:
    value = {
        "id": template_id,
        "version": 1,
        "name": "Build health",
        "summary": "Show bounded build health metrics.",
        "author": "Loopdy",
        "license": "MIT",
        "minimum_card_version": 1,
        "parameters_schema": {
            "type": "object", "properties": {}, "required": [], "additionalProperties": False,
        },
        "document": json.loads(CARD_FIXTURE.read_text(encoding="utf-8")),
    }
    value["sha256"] = hashlib.sha256(canonical_json(value["document"]).encode("utf-8")).hexdigest()
    return value


class _Context:
    state = None

    def __init__(self, profile: str = "personal") -> None:
        self.profile_name = profile
        self.tools: dict[str, Callable[..., str]] = {}
        self.schemas: dict[str, dict[str, Any]] = {}

    def register_tool(self, *, name, handler, schema, **_kwargs) -> None:
        self.tools[name] = handler
        self.schemas[name] = schema

    def register_platform(self, **_kwargs) -> None: pass
    def register_approval_transport(self, *_args) -> None: pass
    def register_hook(self, *_args) -> None: pass
    def register_cli_command(self, **_kwargs) -> None: pass
    def register_skill(self, *_args, **_kwargs) -> None: pass
    def on_unload(self, *_args) -> None: pass


class _Service:
    def __init__(self, store: BighelpStore) -> None:
        self.store = store

    def enqueue(self, *_args, **_kwargs) -> None:
        raise AssertionError("saving a template must not notify anyone")


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "loopdy.sqlite3"
        self.store = BighelpStore(self.path)
        self.tools = self.tools_for("personal")

    def tools_for(self, profile: str, store: BighelpStore | None = None) -> dict[str, Callable[..., str]]:
        context = _Context(profile)
        register_tools(context, store=store or self.store, profile=profile, now=lambda: NOW)
        return context.tools

    def call(self, name: str, arguments: dict[str, Any], tools: dict | None = None) -> dict[str, Any]:
        return json.loads((tools or self.tools)[name](arguments))

    def save(self, tools: dict | None = None, **overrides: Any) -> dict[str, Any]:
        return self.call(SAVE, _save_arguments(**overrides), tools)

    def render(self, template_id: str, parameters: dict[str, Any], tools: dict | None = None) -> dict:
        delivered = self.call(
            "bighelp_render_card_template",
            {"template_id": template_id, "parameters": parameters},
            tools,
        )
        self.assertEqual(delivered["schema"], "loopdy.card_delivery")
        card = extract_rendered_envelope(delivered)
        self.assertEqual(validate_card_result(card, now=NOW), card)
        return card

    def assertRejected(self, result: dict[str, Any], code: str) -> dict[str, Any]:
        self.assertEqual(result.get("status"), "rejected", result)
        self.assertEqual(result["error"]["code"], code, result)
        self.assertTrue(result["error"]["message"])
        return result["error"]


class SaveToolRegistrationTests(_Case):
    def test_tool_is_registered_with_the_issue_intent_and_a_strict_schema(self) -> None:
        context = _Context()
        register_plugin(context, service=_Service(self.store))

        self.assertIn(SAVE, context.tools)
        schema = context.schemas[SAVE]
        self.assertEqual(schema["name"], SAVE)
        self.assertIn(INTENT, schema["description"])
        parameters = schema["parameters"]
        self.assertFalse(parameters["additionalProperties"])
        self.assertEqual(
            set(parameters["required"]),
            {"name", "purpose", "usage_guidance", "layout", "parameters"},
        )
        self.assertEqual(
            set(parameters["properties"]),
            {"name", "purpose", "usage_guidance", "layout", "parameters", "template_id",
             "expected_version"},
        )

    def test_manifest_declares_the_tool(self) -> None:
        manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertIn(SAVE, manifest["provides_tools"])

    def test_tool_is_absent_without_template_storage(self) -> None:
        context = _Context()
        register_tools(context, store=None, profile="personal", now=lambda: NOW)
        self.assertNotIn(SAVE, context.tools)


class SaveRoundTripTests(_Case):
    def test_save_search_get_render_with_fresh_values(self) -> None:
        saved = self.save()

        self.assertEqual(saved["status"], "created")
        template_id = saved["template_id"]
        self.assertEqual(template_id, "saved-daily-steps")
        self.assertEqual(saved["version"], 1)
        self.assertEqual(saved["usage_guidance"], _save_arguments()["usage_guidance"])
        self.assertEqual(saved["render_with"], "bighelp_render_card_template")
        self.assertEqual(saved["template"]["id"], template_id)
        self.assertEqual(saved["template"]["summary"], _save_arguments()["purpose"])
        self.assertEqual(
            saved["parameters_schema"]["required"], ["status", "steps"],
        )

        found = self.call("bighelp_search_card_templates", {"query": "walking going"})
        self.assertEqual([item["id"] for item in found["templates"]], [template_id])
        self.assertEqual(found["templates"][0]["origin"], "saved")
        self.assertEqual(found["templates"][0]["usage_guidance"], _save_arguments()["usage_guidance"])

        fetched = self.call("bighelp_get_card_template", {"template_id": template_id})
        self.assertEqual(fetched["origin"], "saved")
        self.assertEqual(fetched["template"]["version"], 1)
        self.assertEqual(
            set(fetched["template"]["parameters_schema"]["properties"]),
            {"title", "steps", "goal", "status"},
        )

        first = self.render(template_id, {"steps": 4321, "status": "On track"})
        second = self.render(
            template_id,
            {"steps": 9876, "status": "Behind", "goal": 12000, "title": "Weekend walk"},
        )

        self.assertEqual(first["title"], "Daily steps")
        self.assertEqual(first["elements"]["steps"]["props"]["value"], {"literal": 4321})
        self.assertEqual(first["elements"]["progress"]["props"]["maximum"], {"literal": 8000})
        self.assertEqual(first["spoken_summary"], "4321 of 8000 steps so far.")
        self.assertEqual(second["title"], "Weekend walk")
        self.assertEqual(second["elements"]["card"]["props"]["subtitle"], "Goal 12000 steps")
        self.assertEqual(second["elements"]["status"]["props"]["value"], {"literal": "Behind"})
        # Same layout, nothing carried over from the first render.
        for card in (first, second):
            self.assertEqual(card["root"], "card")
            self.assertEqual(
                {key: (value["type"], value["children"]) for key, value in card["elements"].items()},
                {key: (value["type"], value["children"]) for key, value in _layout()["elements"].items()},
            )
        second_text = canonical_json(second)
        for stale in ("4321", "On track", "Daily steps", "8000"):
            self.assertNotIn(stale, second_text)
        stored = canonical_json(self.store.get_card_template(profile="personal", template_id=template_id))
        for value in ("4321", "9876", "Weekend walk", "Behind\"}"):
            self.assertNotIn(value, stored)

    def test_saved_template_survives_a_restart_with_the_same_id(self) -> None:
        saved = self.save()

        restarted = BighelpStore(self.path)
        tools = self.tools_for("personal", restarted)

        found = self.call("bighelp_search_card_templates", {"query": "steps"}, tools)
        self.assertEqual([item["id"] for item in found["templates"]], [saved["template_id"]])
        self.assertEqual(found["templates"][0]["version"], 1)
        self.assertEqual(found["templates"][0]["sha256"], saved["sha256"])
        card = self.render(saved["template_id"], {"steps": 100, "status": "Behind"}, tools)
        self.assertEqual(card["elements"]["steps"]["props"]["value"], {"literal": 100})

    def test_older_store_gains_saved_template_columns(self) -> None:
        legacy = Path(self._directory.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy) as connection:
            connection.execute(
                """
                CREATE TABLE card_templates (
                    profile TEXT NOT NULL, template_id TEXT NOT NULL, version INTEGER NOT NULL,
                    name TEXT NOT NULL, summary TEXT NOT NULL, sha256 TEXT NOT NULL,
                    template_json TEXT NOT NULL, created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL, PRIMARY KEY (profile, template_id)
                )
                """
            )
            template = _installed_template()
            connection.execute(
                "INSERT INTO card_templates VALUES (?, ?, 1, ?, ?, ?, ?, 1, 1)",
                ("personal", template["id"], template["name"], template["summary"],
                 template["sha256"], canonical_json(template)),
            )
        store = BighelpStore(legacy)
        tools = self.tools_for("personal", store)

        found = self.call("bighelp_search_card_templates", {"query": ""}, tools)
        self.assertEqual(found["templates"][0]["origin"], "installed")
        self.assertEqual(self.save(tools)["status"], "created")


class SaveParameterContractTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.template_id = self.save()["template_id"]

    def render_error(self, parameters: dict[str, Any]) -> str:
        with self.assertRaises(ValueError) as raised:
            self.tools["bighelp_render_card_template"]({
                "template_id": self.template_id, "parameters": parameters,
            })
        return str(raised.exception)

    def test_optional_parameters_fall_back_to_their_defaults(self) -> None:
        card = self.render(self.template_id, {"steps": 10, "status": "Behind"})
        self.assertEqual(card["title"], "Daily steps")
        self.assertEqual(card["elements"]["progress"]["props"]["maximum"], {"literal": 8000})

    def test_missing_required_parameters_are_named(self) -> None:
        message = self.render_error({"steps": 10})
        self.assertIn("missing", message)
        self.assertIn("status", message)

    def test_extra_parameters_are_named(self) -> None:
        message = self.render_error({"steps": 10, "status": "Behind", "city": "Springfield"})
        self.assertIn("unknown", message)
        self.assertIn("city", message)

    def test_wrong_types_and_values_are_rejected(self) -> None:
        for parameters in (
            {"steps": "ten", "status": "Behind"},
            {"steps": True, "status": "Behind"},
            {"steps": 1.5, "status": "Behind"},
            {"steps": 10, "status": "Sideways"},
            {"steps": 10, "status": "Behind", "title": 7},
        ):
            with self.subTest(parameters=parameters):
                self.assertIn("is invalid", self.render_error(parameters))


class SaveValidationTests(_Case):
    def test_placeholder_parameter_mismatches_are_named(self) -> None:
        undeclared = _layout()
        undeclared["elements"]["status"]["props"]["value"] = {"literal": "{{mood}}"}
        error = self.assertRejected(self.save(layout=undeclared), "undeclared_placeholder")
        self.assertEqual(error["placeholders"], ["mood"])

        unused = _parameters() + [
            {"name": "weather", "type": "string", "description": "Sky", "required": True},
        ]
        error = self.assertRejected(self.save(parameters=unused), "unused_parameter")
        self.assertEqual(error["parameters"], ["weather"])

    def test_parameter_definitions_are_checked(self) -> None:
        cases = {
            "optional without default": {"name": "goal", "type": "integer", "description": "Goal",
                                         "required": False},
            "default of the wrong type": {"name": "goal", "type": "integer", "description": "Goal",
                                          "required": False, "default": "8000"},
            "unsupported type": {"name": "goal", "type": "array", "description": "Goal",
                                 "required": True},
            "bad name": {"name": "goal amount", "type": "integer", "description": "Goal",
                         "required": True},
            "default outside enum": {"name": "goal", "type": "integer", "description": "Goal",
                                     "required": False, "default": 3, "enum": [1, 2]},
        }
        for label, definition in cases.items():
            with self.subTest(label):
                parameters = [item for item in _parameters() if item["name"] != "goal"] + [definition]
                self.assertRejected(self.save(parameters=parameters), "invalid_parameter")
        duplicated = _parameters() + [_parameters()[1]]
        self.assertRejected(self.save(parameters=duplicated), "invalid_parameter")

    def test_layout_without_placeholders_is_a_snapshot(self) -> None:
        snapshot = json.loads(CARD_FIXTURE.read_text(encoding="utf-8"))
        self.assertRejected(self.save(layout=snapshot, parameters=[]), "no_parameters")

    def test_invalid_and_unsupported_layouts_are_rejected(self) -> None:
        unsupported = _layout()
        unsupported["elements"]["status"]["type"] = "web_view"
        error = self.assertRejected(self.save(layout=unsupported), "invalid_layout")
        self.assertEqual(error["card_error"], "unsupported_element")

        live = _layout()
        live["data_sources"] = [{
            "id": "feed",
            "request": {"method": "GET", "url": "https://example.com/steps.json"},
            "response": {"format": "json", "root": ""},
            "refresh": {"minimum_interval_seconds": 60, "stale_after_seconds": 60,
                        "expires_at": "2026-09-03T12:00:00Z"},
        }]
        error = self.assertRejected(self.save(layout=live), "invalid_layout")
        self.assertEqual(error["card_error"], "live_data_unavailable")

        self.assertRejected(self.save(layout="not a card"), "invalid_arguments")

    def test_placeholders_only_fill_values(self) -> None:
        structural = _layout()
        structural["elements"]["steps"]["props"]["format"] = {
            "style": "currency", "currency_pointer": "/{{status}}",
        }
        self.assertRejected(self.save(layout=structural), "misplaced_placeholder")

        malformed = _layout()
        malformed["elements"]["card"]["props"]["subtitle"] = "Goal {{ goal }} steps"
        self.assertRejected(self.save(layout=malformed), "malformed_placeholder")

    def test_placeholder_types_must_fit_where_they_are_used(self) -> None:
        layout = _layout()
        layout["title"] = "{{steps}}"
        error = self.assertRejected(self.save(layout=layout), "render_check_failed")
        self.assertIn("placeholder", error["message"])

    def test_credentials_and_oversized_payloads_are_rejected(self) -> None:
        secret = _layout()
        secret["elements"]["card"]["props"]["subtitle"] = "api_key=sk-proj-abcdefghijklmnopqrstuv {{goal}}"
        self.assertRejected(self.save(layout=secret), "sensitive_content")

        huge = _save_arguments()
        huge["purpose"] = "x" * 200_000
        self.assertRejected(self.call(SAVE, huge), "payload_too_large")

        self.assertRejected(self.save(name=""), "invalid_arguments")
        self.assertRejected(self.save(name="a" * 121), "invalid_arguments")
        arguments = _save_arguments()
        arguments["values"] = {"steps": 1}
        self.assertRejected(self.call(SAVE, arguments), "invalid_arguments")

    def test_rejections_store_nothing(self) -> None:
        layout = _layout()
        layout["title"] = "{{mood}}"
        self.save(layout=layout)
        self.assertEqual(self.store.list_card_templates(profile="personal"), [])


class SaveDuplicateAndUpdateTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self.first = self.save()

    def test_identical_save_is_unchanged(self) -> None:
        again = self.save()
        self.assertEqual(again["status"], "unchanged")
        self.assertEqual((again["template_id"], again["version"]),
                         (self.first["template_id"], 1))
        self.assertEqual(len(self.store.list_card_templates(profile="personal")), 1)

    def test_same_name_with_new_content_is_not_overwritten(self) -> None:
        changed = _layout()
        changed["elements"]["status"]["props"]["semantic"] = "warning"
        error = self.assertRejected(self.save(layout=changed), "duplicate_name")
        self.assertEqual(error["existing_template_id"], self.first["template_id"])
        self.assertEqual(error["existing_version"], 1)
        # Names that differ only in case and spacing are the same name.
        self.assertRejected(self.save(name="  daily   STEPS ", layout=changed), "duplicate_name")
        stored = self.store.get_card_template(profile="personal", template_id=self.first["template_id"])
        self.assertEqual(stored["sha256"], self.first["sha256"])

    def test_same_layout_under_another_name_points_at_the_match(self) -> None:
        error = self.assertRejected(self.save(name="Walking today"), "duplicate_layout")
        self.assertEqual(error["existing_template_id"], self.first["template_id"])

    def test_a_similar_but_different_template_is_saved_separately(self) -> None:
        layout = _layout()
        layout["elements"]["status"]["props"]["semantic"] = "warning"
        other = self.save(name="Daily steps (work days)", layout=layout)
        self.assertEqual(other["status"], "created")
        self.assertNotEqual(other["template_id"], self.first["template_id"])
        self.assertEqual(
            self.store.get_card_template(profile="personal", template_id=self.first["template_id"])["sha256"],
            self.first["sha256"],
        )

    def test_explicit_update_keeps_the_id_and_bumps_the_version(self) -> None:
        layout = _layout()
        layout["elements"]["status"]["props"]["semantic"] = "warning"
        updated = self.save(
            layout=layout, template_id=self.first["template_id"], expected_version=1,
            name="Daily steps, renamed",
        )
        self.assertEqual(updated["status"], "updated")
        self.assertEqual((updated["template_id"], updated["version"]),
                         (self.first["template_id"], 2))
        self.assertNotEqual(updated["sha256"], self.first["sha256"])
        fetched = self.call("bighelp_get_card_template", {"template_id": self.first["template_id"]})
        self.assertEqual(fetched["template"]["name"], "Daily steps, renamed")

        stale = self.save(layout=_layout(), template_id=self.first["template_id"], expected_version=1)
        error = self.assertRejected(stale, "version_conflict")
        self.assertEqual(error["current_version"], 2)

        same = self.save(
            layout=layout, template_id=self.first["template_id"], expected_version=2,
            name="Daily steps, renamed",
        )
        self.assertEqual((same["status"], same["version"]), ("unchanged", 2))

    def test_update_needs_a_version_and_an_existing_saved_template(self) -> None:
        self.assertRejected(
            self.save(template_id=self.first["template_id"]), "missing_expected_version",
        )
        self.assertRejected(self.save(expected_version=1), "invalid_arguments")
        self.assertRejected(
            self.save(template_id="saved-nothing-here", expected_version=1), "template_not_found",
        )
        self.store.install_card_template(profile="personal", template=_installed_template())
        self.assertRejected(
            self.save(template_id="build-health", expected_version=1, name="Build health"),
            "not_a_saved_template",
        )
        self.assertEqual(
            self.store.get_card_template(profile="personal", template_id="build-health"),
            _installed_template(),
        )

    def test_update_cannot_take_another_templates_name(self) -> None:
        layout = _layout()
        layout["elements"]["status"]["props"]["semantic"] = "warning"
        second = self.save(name="Work steps", layout=layout)
        error = self.assertRejected(
            self.save(name="Daily steps", layout=layout, template_id=second["template_id"],
                      expected_version=1),
            "duplicate_name",
        )
        self.assertEqual(error["existing_template_id"], self.first["template_id"])

    def test_catalog_has_a_bound(self) -> None:
        with mock.patch("loopdy_plugin.saved_card_templates.MAX_TEMPLATES", 1):
            layout = _layout()
            layout["elements"]["status"]["props"]["semantic"] = "warning"
            self.assertRejected(self.save(name="Work steps", layout=layout), "catalog_full")


class SaveProfileIsolationTests(_Case):
    def test_profiles_never_see_or_change_each_others_templates(self) -> None:
        mine = self.save()
        research = self.tools_for("research")

        self.assertEqual(self.call("bighelp_search_card_templates", {"query": ""}, research),
                         {"templates": []})
        with self.assertRaisesRegex(ValueError, "not found"):
            research["bighelp_get_card_template"]({"template_id": mine["template_id"]})
        self.assertRejected(
            self.save(research, template_id=mine["template_id"], expected_version=1),
            "template_not_found",
        )
        theirs = self.save(research)
        self.assertEqual(theirs["status"], "created")
        self.assertEqual(theirs["template_id"], mine["template_id"])
        self.assertEqual(
            self.store.get_card_template(profile="personal", template_id=mine["template_id"])["sha256"],
            mine["sha256"],
        )


class SaveSideEffectTests(_Case):
    def test_saving_publishes_and_notifies_nothing(self) -> None:
        context = _Context()
        register_plugin(context, service=_Service(self.store))
        result = json.loads(context.tools[SAVE](_save_arguments()))

        self.assertEqual(result["status"], "created")
        self.assertEqual(self.store.list_events(), [])
        self.assertNotIn("display_markdown", result)

    def test_rendering_never_saves_a_template(self) -> None:
        card = copy.deepcopy(json.loads(CARD_FIXTURE.read_text(encoding="utf-8")))
        card["schema"] = "bighelp.card"
        self.tools["bighelp_render_card"](card)
        self.assertEqual(self.store.list_card_templates(profile="personal"), [])

        saved = self.save()
        self.render(saved["template_id"], {"steps": 10, "status": "Behind"})
        templates = self.store.list_card_templates(profile="personal")
        self.assertEqual([(item["id"], item["version"]) for item in templates],
                         [(saved["template_id"], 1)])
        self.assertEqual(self.store.list_events(), [])


class SaveGuidanceTests(unittest.TestCase):
    def test_skills_say_when_saving_helps_and_when_to_skip(self) -> None:
        for relative in ("skills/bighelp/SKILL.md", "skills/generative-ui/SKILL.md"):
            with self.subTest(relative):
                text = (PLUGIN_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn(SAVE, text)
                self.assertIn("optional", text)
                self.assertIn("one-off", text)
        docs = (PLUGIN_ROOT / "docs" / "CARDS.md").read_text(encoding="utf-8")
        self.assertIn(SAVE, docs)


if __name__ == "__main__":
    unittest.main()
