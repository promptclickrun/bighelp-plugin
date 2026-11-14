"""The html_app card element (docs/HTML_APPS.md): only bighelp_show_app may make one."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from loopdy_plugin import card_objects
from loopdy_plugin.card_previews import reply_preview
from loopdy_plugin.generative_ui import rendered_card_delivery
from loopdy_plugin.loopdy_cards import BighelpCardError, render_card, validate_card_input, validate_card_result
from loopdy_plugin.tools import register

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = PLUGIN_ROOT / "spec" / "loopdy-card-v1.schema.json"
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
CHAT = "20261107_101500_abc123"
FIXTURE = PLUGIN_ROOT / "fixtures" / "loopdy_card_v1" / "static-html-app.json"
APP_ID = "a_0123456789abcdef0123456789abcdef"
RENDERED = ("content_hash", "card_id", "created_at", "origin")


def app_card(**props) -> dict:
    card = json.loads(FIXTURE.read_text(encoding="utf-8"))
    card["elements"]["app"]["props"].update(props)
    return card


def render(card: dict) -> dict:
    return render_card(card, now=NOW, allow_apps=True)


class _Context:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.schemas: dict[str, dict] = {}

    def register_tool(self, *, name, handler, schema, **_kwargs) -> None:
        self.handlers[name] = handler
        self.schemas[name] = schema

    def register_platform(self, **_kwargs): pass
    def register_approval_transport(self, *_args): pass
    def register_hook(self, *_args): pass
    def register_skill(self, *_args, **_kwargs): pass
    def register_cli_command(self, **_kwargs): pass
    def on_unload(self, *_args): pass


class HtmlAppCardTests(unittest.TestCase):
    def test_the_shared_fixture_fits_the_portable_schema_and_the_validator(self):
        fixture = app_card()
        validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
        self.assertEqual([], [error.message for error in validator.iter_errors(fixture)])
        self.assertEqual(fixture, validate_card_input(fixture, now=NOW, allow_apps=True))

    def test_the_tools_card_is_accepted_and_matches_the_contract(self):
        card = render(app_card())
        self.assertEqual("An app to open in Bighelp: Trip planner.", card["spoken_summary"])
        self.assertEqual([], card["data_sources"])
        self.assertEqual({"type": "html_app", "children": [],
                         "props": {"app_id": APP_ID, "description": "Plan the stops of a road trip.",
                                   "sends_answers": True, "live": False}}, card["elements"]["app"])
        for key in RENDERED:
            self.assertIn(key, card)
        self.assertEqual(card, validate_card_result(card, now=NOW))

    def test_nothing_else_may_make_one(self):
        with self.assertRaises(BighelpCardError) as caught:
            validate_card_input(app_card(), now=NOW)
        self.assertEqual("apps_tool_only", caught.exception.code)
        with self.assertRaises(BighelpCardError) as caught:
            render_card(app_card(), now=NOW, allow_objects=True, allow_drafts=True)
        self.assertEqual("apps_tool_only", caught.exception.code)

    def test_bighelp_render_card_refuses_it_even_in_a_chat_that_draws_apps(self):
        context = _Context()
        register(context, now=lambda: NOW, card_renders=lambda _session: {"contact", "html_app"})
        with self.assertRaises(BighelpCardError) as caught:
            context.handlers["bighelp_render_card"](app_card(), session_id=CHAT)
        self.assertEqual("apps_tool_only", caught.exception.code)
        # The card tool's own parameters never mention apps.
        self.assertNotIn("html_app", json.dumps(context.schemas["bighelp_render_card"]["parameters"]))

    def test_props_are_exactly_the_four_keys(self):
        for missing in ("app_id", "description", "sends_answers", "live"):
            props = app_card()["elements"]["app"]["props"]
            del props[missing]
            card = app_card()
            card["elements"]["app"]["props"] = props
            with self.subTest(missing=missing), self.assertRaises(BighelpCardError):
                render(card)
        with self.assertRaises(BighelpCardError):
            render(app_card(extra=True))

    def test_each_prop_is_checked(self):
        bad = {
            "app_id": ["a_" + "1" * 31, "a_" + "G" * 32, "d_" + "1" * 32, "a_" + "A" * 32, 5, None],
            "description": ["", "x" * 241, "two  spaces", " padded", "line\nbreak", "bell\x07", "tab\tx", 5],
            "sends_answers": ["yes", 1, None],
            "live": ["no", 0, None],
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(BighelpCardError):
                    render(app_card(**{key: value}))
        render(app_card(description="x" * 240))

    def test_an_app_is_the_only_element_in_its_card(self):
        wrapped = app_card()
        wrapped["elements"]["root"] = {"type": "vstack", "props": {}, "children": ["app"]}
        wrapped["root"] = "root"
        beside = app_card()
        beside["elements"]["other"] = {"type": "text", "props": {"value": "hi"}, "children": []}
        for card in (wrapped, beside):
            with self.assertRaises(BighelpCardError):
                render(card)

    def test_an_app_has_no_children(self):
        card = app_card()
        card["elements"]["app"]["children"] = ["app"]
        with self.assertRaises(BighelpCardError):
            render(card)

    def test_a_chat_can_say_it_draws_apps(self):
        self.assertEqual(frozenset({"html_app"}), card_objects.known(["html_app", "nonsense"]))

    def test_a_notification_says_what_the_app_does(self):
        # The delivery is checked against the real clock.
        card = rendered_card_delivery(render_card(app_card(), now=datetime.now(timezone.utc), allow_apps=True))
        reply = "Here you go.\n\n" + card["display_markdown"]
        self.assertEqual("Here you go. Trip planner: Plan the stops of a road trip.", reply_preview(reply))


SPACE_FIXTURE = PLUGIN_ROOT / "fixtures" / "loopdy_card_v1" / "static-space-draft.json"
SPACE_ID = "s_0123456789abcdef0123456789abcdef"


def space_card(**props) -> dict:
    card = json.loads(SPACE_FIXTURE.read_text(encoding="utf-8"))
    card["elements"]["space"]["props"].update(props)
    return card


def render_space(card: dict) -> dict:
    return render_card(card, now=NOW, allow_spaces=True)


class SpaceDraftCardTests(unittest.TestCase):
    """The space_draft element (docs/SPACES.md): only bighelp_draft_space may make one."""

    def test_the_shared_fixture_is_accepted_and_matches_the_contract(self):
        card = render_space(space_card())
        self.assertEqual({"type": "space_draft", "children": [], "props": {
            "space_id": SPACE_ID, "description": "Drive time to work, from live traffic.",
            "updates": "Every 15 minutes", "uses_ai": False, "revision": 1}}, card["elements"]["space"])
        self.assertEqual("A space to add to Today: Commute. Updates every 15 minutes, without AI.",
                         card["spoken_summary"])
        self.assertEqual(card, validate_card_result(card, now=NOW))

    def test_nothing_else_may_make_one(self):
        for allowed in ({}, {"allow_objects": True, "allow_drafts": True, "allow_apps": True}):
            with self.subTest(allowed=allowed), self.assertRaises(BighelpCardError) as caught:
                render_card(space_card(), now=NOW, **allowed)
            self.assertEqual("spaces_tool_only", caught.exception.code)

    def test_props_are_exactly_the_five_keys(self):
        for missing in ("space_id", "description", "updates", "uses_ai", "revision"):
            card = space_card()
            del card["elements"]["space"]["props"][missing]
            with self.subTest(missing=missing), self.assertRaises(BighelpCardError):
                render_space(card)
        with self.assertRaises(BighelpCardError):
            render_space(space_card(extra=True))

    def test_each_prop_is_checked(self):
        bad = {
            "space_id": ["s_" + "1" * 31, "s_" + "A" * 32, "a_" + "1" * 32, 5, None],
            "description": ["", "x" * 241, "two  spaces", " padded", "line\nbreak", "bell\x07", 5],
            "updates": ["", "x" * 61, "Every  hour", "Every\nhour", None],
            "uses_ai": ["no", 0, None],
            "revision": [0, -1, 1.5, True, "1", 9_007_199_254_740_992],
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(BighelpCardError):
                    render_space(space_card(**{key: value}))
        render_space(space_card(description="x" * 240, updates="x" * 60, revision=9_007_199_254_740_991))

    def test_a_space_is_the_only_element_in_its_card(self):
        wrapped = space_card()
        wrapped["elements"]["root"] = {"type": "vstack", "props": {}, "children": ["space"]}
        wrapped["root"] = "root"
        beside = space_card()
        beside["elements"]["other"] = {"type": "text", "props": {"value": {"literal": "hi"}}, "children": []}
        leaf = space_card()
        leaf["elements"]["space"]["children"] = ["space"]
        for card in (wrapped, beside, leaf):
            with self.assertRaises(BighelpCardError):
                render_space(card)

    def test_a_chat_can_say_it_draws_spaces(self):
        self.assertEqual(frozenset({"space_draft"}), card_objects.known(["space_draft", "nonsense"]))
        self.assertIn("space_draft", card_objects.DECLARABLE)

    def test_a_notification_says_what_the_space_shows(self):
        card = rendered_card_delivery(render_card(space_card(), now=datetime.now(timezone.utc), allow_spaces=True))
        reply = "Here it is.\n\n" + card["display_markdown"]
        self.assertEqual("Here it is. Commute: Drive time to work, from live traffic.", reply_preview(reply))


if __name__ == "__main__":
    unittest.main()
