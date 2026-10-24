"""The contact_draft and event_draft card objects (docs/IMAGE_DRAFTS.md)."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from loopdy_plugin import card_objects
from loopdy_plugin.loopdy_cards import (
    BighelpCardError,
    render_card,
    validate_card_input,
    validate_card_result,
)
from loopdy_plugin.people import PeopleStore
from loopdy_plugin.tools import register


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PLUGIN_ROOT / "fixtures" / "loopdy_card_v1"
SCHEMA = PLUGIN_ROOT / "spec" / "loopdy-card-v1.schema.json"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
CHAT = "20261024_101500_abc123"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def contact() -> dict:
    return fixture("static-contact-draft.json")


def event() -> dict:
    return fixture("static-event-draft.json")


def validate(card: dict) -> dict:
    return validate_card_input(card, now=NOW, allow_objects=True, allow_drafts=True)


def with_fields(card: dict, fields: dict, marks: list | None = None) -> dict:
    card = copy.deepcopy(card)
    props = card["elements"]["draft"]["props"]
    props["fields"] = fields
    if marks is None:
        props.pop("marks", None)
    else:
        props["marks"] = marks
    return card


def with_props(card: dict, **changes) -> dict:
    card = copy.deepcopy(card)
    card["elements"]["draft"]["props"].update(changes)
    return card


class _Context:
    profile_name = "personal"

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.schemas: dict[str, dict] = {}

    def register_tool(self, *, name, handler, schema, **_kwargs) -> None:
        self.handlers[name] = handler
        self.schemas[name] = schema


class DraftCardTests(unittest.TestCase):
    def assert_refused(self, code: str, card: dict) -> str:
        with self.assertRaises(BighelpCardError) as caught:
            validate(card)
        self.assertEqual(code, caught.exception.code, str(caught.exception))
        return str(caught.exception)

    def test_the_fixtures_fit_the_portable_schema_and_the_validator(self) -> None:
        validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
        for card in (contact(), event()):
            with self.subTest(card=card["title"]):
                self.assertEqual([], [error.message for error in validator.iter_errors(card)])
                self.assertEqual(card, validate(card))
        mixed = fixture("invalid-event-draft-mixed.json")
        self.assertTrue(list(validator.iter_errors(mixed)))
        self.assert_refused("variant_mismatch", mixed)

    def test_only_the_draft_tools_may_make_drafts(self) -> None:
        for card in (contact(), event()):
            with self.assertRaises(BighelpCardError) as caught:
                validate_card_input(card, now=NOW, allow_objects=True)
            self.assertEqual("drafts_tool_only", caught.exception.code)
        context = _Context()
        register(context, now=lambda: NOW,
                 card_renders=lambda _session: {"contact", "place", "music", "contact_draft", "event_draft"})
        with self.assertRaises(BighelpCardError) as caught:
            context.handlers["bighelp_render_card"](contact(), session_id=CHAT)
        self.assertEqual("drafts_tool_only", caught.exception.code)
        # The card tool's own parameters never mention drafts.
        parameters = json.dumps(context.schemas["bighelp_render_card"]["parameters"])
        self.assertNotIn("draft", parameters)

    def test_a_rendered_draft_card_reads_back(self) -> None:
        rendered = render_card(contact(), now=NOW, allow_objects=True, allow_drafts=True)
        self.assertEqual(rendered, validate_card_result(rendered, now=NOW))

    def test_a_draft_is_the_only_element_in_its_card(self) -> None:
        card = contact()
        card["root"] = "card"
        card["elements"]["card"] = {"type": "card", "props": {"title": "Riley"}, "children": ["draft"]}
        self.assert_refused("invalid_children", card)
        two = event()
        two["root"] = "stack"
        two["elements"]["second"] = copy.deepcopy(contact()["elements"]["draft"])
        two["elements"]["stack"] = {"type": "vstack", "props": {}, "children": ["draft", "second"]}
        self.assert_refused("invalid_children", two)

    def test_plugin_props_are_checked(self) -> None:
        bad = {
            "invalid_value": [
                with_props(contact(), preset="event_poster"),
                with_props(event(), preset="business_card"),
                with_props(contact(), draft_id="d_ABC"),
                with_props(contact(), draft_id="0f1e2d3c4b5a69788796a5b4c3d2e1f0"),
                with_props(contact(), config_revision=0),
                with_props(contact(), source={"image_id": "x" * 32, "mime_type": "image/jpeg"}),
                with_props(contact(), source={"image_id": "a" * 32, "mime_type": "application/pdf"}),
                with_props(contact(), source={"image_id": "a" * 32, "mime_type": "image/png", "width": 0}),
            ],
            "unknown_field": [
                with_props(contact(), source={"image_id": "a" * 32, "mime_type": "image/png", "path": "/x"}),
                with_props(contact(), extra=True),
            ],
            "missing_field": [
                with_props(contact(), messages={"review": "Check it."}),
            ],
        }
        for code, cards in bad.items():
            for card in cards:
                with self.subTest(code=code, props=card["elements"]["draft"]["props"].keys()):
                    self.assert_refused(code, card)

    def test_messages_hold_only_their_own_placeholders(self) -> None:
        def message(key: str, text: str) -> dict:
            card = contact()
            card["elements"]["draft"]["props"]["messages"][key] = text
            return card

        for key, text in (("review", "Saving to {{destination}}."), ("saved", "{{name}} is in {{destination}}."),
                          ("missing_fields", "Check {{fields}}"), ("failed", "No. {{reason}}"),
                          ("outcome_unknown", "Check Contacts first."), ("review", "x" * 500),
                          ("review", "Two lines\nare fine.")):
            with self.subTest(key=key, text=text[:20]):
                validate(message(key, text))
        for key, text, code in (
            ("review", "Saved {{name}}", "invalid_placeholder"),
            ("outcome_unknown", "Not in {{destination}}", "invalid_placeholder"),
            ("failed", "No {{reason}", "invalid_placeholder"),
            ("failed", "No {{reason}} }}", "invalid_placeholder"),
            ("saved", "{{ name }}", "invalid_placeholder"),
            ("review", "x{{{destination}}}", "invalid_placeholder"),
            ("saved", "{{name}}}", "invalid_placeholder"),
            ("review", "   ", "invalid_value"),
            ("review", "", "limit_exceeded"),
            ("review", "x" * 501, "limit_exceeded"),
        ):
            with self.subTest(key=key, text=text[:20]):
                self.assert_refused(code, message(key, text))

    def test_contact_fields(self) -> None:
        phone = {"label": "work", "value": "+1 555 0142"}
        good = {"given_name": "n" * 200, "family_name": "Moss", "organization": "Sample Studio",
                "job_title": "Designer", "phones": [phone] * 5,
                "emails": [{"value": "riley@example.com"}] * 5, "postal_address": {"city": "c" * 100}}
        validate(with_fields(contact(), good))
        validate(with_fields(contact(), {"phones": [phone]}))
        bad = {
            "limit_exceeded": [{"given_name": "n" * 201}, {"phones": [phone] * 6}, {"phones": []},
                               {"phones": [{"label": "l" * 41, "value": "555"}]},
                               {"postal_address": {"city": "c" * 101}}, {"given_name": ""}],
            "invalid_value": [{"given_name": "Riley\nMoss"}, {"given_name": "   "}, {"organization": " \t "},
                              {"postal_address": {"city": "  "}}, {"job_title": "\u00a0"}],
            "invalid_phone": [{"phones": [{"value": "call me"}]}, {"phones": [{"value": "555 0142 ext 9"}]}],
            "invalid_email": [{"emails": [{"value": "riley at example"}]}],
            "unknown_field": [{"notes": "Met at a fair"}, {"name": "Riley Moss"},
                              {"postal_address": {"county": "Sample"}}],
            "missing_field": [{}, {"postal_address": {}}],
        }
        for code, cases in bad.items():
            for fields in cases:
                with self.subTest(code=code, fields=fields):
                    self.assert_refused(code, with_fields(contact(), fields))

    def test_event_variants_times_and_zones(self) -> None:
        timed = {"title": "Night Market", "start": "2030-06-15T18:00:00-07:00",
                 "end": "2030-06-15T19:00:00-07:00", "time_zone": "America/Los_Angeles"}
        validate(with_fields(event(), timed))
        # Without a time zone any offset will do; with one, each offset must be the zone's (B15).
        validate(with_fields(event(), {**{k: v for k, v in timed.items() if k != "time_zone"},
                                       "end": "2030-06-16T02:00:00Z"}))
        with self.assertRaises(BighelpCardError) as caught:
            validate(with_fields(event(), {**timed, "end": "2030-06-16T02:00:00Z"}))
        self.assertEqual("invalid_time_zone", caught.exception.code)
        validate(with_fields(event(), {"title": "Night Market", "start": "2030-06-15T18:00:00-07:00"}))
        validate(with_fields(event(), {"title": "Night Market", "all_day": True, "start_date": "2030-06-15"}))
        validate(with_fields(event(), {"title": "Night Market", "all_day": True,
                                       "start_date": "2030-06-15", "end_date": "2030-06-17"}))
        validate(with_fields(event(), {"location": "Harbor Hall\n1 Pier Road", "notes": "n" * 2_000}))
        bad = {
            "variant_mismatch": [
                {"all_day": True, "start": "2030-06-15T18:00:00-07:00"},
                {"all_day": True, "time_zone": "America/Los_Angeles"},
                {"all_day": False, "start_date": "2030-06-15"},
                {"start_date": "2030-06-15"},
            ],
            "invalid_time_range": [
                {**timed, "end": "2030-06-15T17:00:00-07:00"},
                {**timed, "end": timed["start"]},
                {"end": "2030-06-15T19:00:00-07:00"},
                {"all_day": True, "start_date": "2030-06-15", "end_date": "2030-06-15"},
                {"all_day": True, "end_date": "2030-06-16"},
            ],
            "invalid_value": [
                {"start": "2030-06-15T18:00:00"},
                {"start": "2030-06-15 18:00"},
                {"start": "June 15"},
                {"all_day": True, "start_date": "2030-02-30"},
                {"all_day": "yes"},
                {"title": "Night\nMarket"},
                {"location": "   "},
            ],
            "invalid_time_zone": [
                {"time_zone": "Mars/Olympus_Mons"}, {"time_zone": "../etc/passwd"},
                {"time_zone": "/etc/localtime"}, {"time_zone": "x" * 65},
            ],
            "limit_exceeded": [{"location": "l" * 501}, {"notes": "n" * 2_001}, {"title": "t" * 201}],
            "unknown_field": [{"recurrence": "weekly"}, {"attendees": []}, {"alarms": []}],
        }
        for code, cases in bad.items():
            for fields in cases:
                with self.subTest(code=code, fields=fields):
                    self.assert_refused(code, with_fields(event(), fields))

    def test_marks_name_real_fields(self) -> None:
        fields = contact()["elements"]["draft"]["props"]["fields"]
        good = [{"field": "given_name", "reason": "unclear"}, {"field": "phones.0", "reason": "guessed"},
                {"field": "emails.0", "reason": "conflicting"}, {"field": "postal_address.city", "reason": "unclear"},
                {"field": "postal_address.region", "reason": "not_in_image"}]
        validate(with_fields(contact(), fields, good))
        validate(with_fields(contact(), {"given_name": "Riley"}, [{"field": "phones", "reason": "not_in_image"}]))
        validate(with_fields(event(), {"title": "Night Market"}, [{"field": "end", "reason": "not_in_image"},
                                                                   {"field": "time_zone", "reason": "not_in_image"}]))
        bad = [
            (contact(), fields, [{"field": "name", "reason": "unclear"}]),
            (contact(), fields, [{"field": "phones.5", "reason": "unclear"}]),
            (contact(), fields, [{"field": "phones.01", "reason": "unclear"}]),
            (contact(), fields, [{"field": "postal_address.county", "reason": "unclear"}]),
            (contact(), fields, [{"field": "given_name", "reason": "unclear"},
                                 {"field": "given_name", "reason": "guessed"}]),
            (contact(), fields, [{"field": "given_name", "reason": "not_in_image"}]),
            (contact(), fields, [{"field": "phones.0", "reason": "not_in_image"}]),
            (event(), {"title": "Night Market"}, [{"field": "start_date", "reason": "not_in_image"}]),
            (event(), {"all_day": True}, [{"field": "start", "reason": "not_in_image"}]),
            (event(), {"title": "Night Market"}, [{"field": "given_name", "reason": "unclear"}]),
        ]
        for card, draft_fields, marks in bad:
            with self.subTest(marks=marks):
                self.assert_refused("invalid_mark", with_fields(card, draft_fields, marks))
        self.assert_refused("invalid_value", with_fields(contact(), fields, [{"field": "given_name", "reason": "wrong"}]))
        self.assert_refused("limit_exceeded", with_fields(
            contact(), fields, [{"field": "given_name", "reason": "unclear"}] * 21))


class DraftRendersTests(unittest.TestCase):
    def test_an_app_can_declare_the_draft_objects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PeopleStore(Path(directory))
            kept = store.set_renders(CHAT, ["contact", "contact_draft", "event_draft", "unknown_thing"])
            self.assertEqual(["contact", "contact_draft", "event_draft"], kept)
            self.assertEqual(frozenset({"contact", "contact_draft", "event_draft"}), store.renders(CHAT))
        self.assertEqual(frozenset({"event_draft"}), card_objects.known(["event_draft", 3, None]))


if __name__ == "__main__":
    unittest.main()
