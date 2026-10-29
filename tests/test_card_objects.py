from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from loopdy_plugin import agent_guide, card_objects, people
from loopdy_plugin.generative_ui import extract_rendered_envelope
from loopdy_plugin.loopdy_cards import (
    BighelpCardError,
    canonical_json,
    render_card,
    validate_card_input,
    validate_card_result,
)
from loopdy_plugin.people import PeopleStore
from loopdy_plugin.tools import register


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PLUGIN_ROOT / "fixtures" / "loopdy_card_v1"
SCHEMA = PLUGIN_ROOT / "spec" / "loopdy-card-v1.schema.json"
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
CHAT = "20261022_101500_abc123"
OTHER_CHAT = "20261022_111500_def456"


def objects_card() -> dict:
    return json.loads((FIXTURES / "static-objects.json").read_text(encoding="utf-8"))


def metrics_card() -> dict:
    return json.loads((FIXTURES / "static-metrics.json").read_text(encoding="utf-8"))


def one(element_type: str, props: dict) -> dict:
    """A card with a single object under the root."""
    return {
        "schema": "loopdy.card", "version": 1, "title": "Sample", "spoken_summary": "Sample.",
        "data_sources": [], "root": "card",
        "elements": {
            "card": {"type": "card", "props": {"title": "Sample"}, "children": ["thing"]},
            "thing": {"type": element_type, "props": props, "children": []},
        },
    }


def validate(card: dict) -> dict:
    return validate_card_input(card, now=NOW, allow_objects=True)


class _Context:
    profile_name = "personal"

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.schemas: dict[str, dict] = {}

    def register_tool(self, *, name, handler, schema, **_kwargs) -> None:
        self.handlers[name] = handler
        self.schemas[name] = schema


class ObjectValidationTests(unittest.TestCase):
    def assert_refused(self, code: str, card: dict) -> None:
        with self.assertRaises(BighelpCardError) as caught:
            validate(card)
        self.assertEqual(code, caught.exception.code)

    def test_the_example_card_fits_the_portable_schema_and_the_validator(self) -> None:
        card = objects_card()
        validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
        self.assertEqual([], [error.message for error in validator.iter_errors(card)])
        self.assertEqual(card, validate(card))

    def test_good_objects_with_only_the_required_props(self) -> None:
        for kind, props in (
            ("contact", {"name": "Sam Rivera"}),
            ("place", {"name": "Sample Cafe", "latitude": 0, "longitude": 0}),
            ("music", {"kind": "artist", "title": "Sample Artist"}),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(props, validate(one(kind, props))["elements"]["thing"]["props"])

    def test_contact_limits_and_rules(self) -> None:
        phone = {"label": "mobile", "value": "+1 (555) 010-0100"}
        good = {"name": "n" * 200, "subtitle": "s" * 200, "contact_id": "i" * 512,
                "phones": [phone] * 5, "emails": [{"label": "l" * 40, "value": "a@b.example"}] * 5,
                "address": {"city": "c" * 100}}
        validate(one("contact", good))
        bad = {
            "limit_exceeded": [
                {"name": "n" * 201}, {"name": "x", "subtitle": "s" * 201}, {"name": "x", "contact_id": "i" * 513},
                {"name": "x", "phones": [phone] * 6},
                {"name": "x", "emails": [{"label": "e", "value": "a@b.example"}] * 6},
                {"name": "x", "phones": [{"label": "l" * 41, "value": "555"}]},
                {"name": "x", "phones": [{"label": "m", "value": "5" * 201}]},
                {"name": "x", "address": {"city": "c" * 101}},
                {"name": ""}, {"name": "x", "phones": "555"},
            ],
            "invalid_phone": [
                {"name": "x", "phones": [{"label": "m", "value": value}]}
                for value in ("call me", "555-0100 ext 5", "tel:5550100", "555;0100", "()", "+")
            ],
            "invalid_email": [
                {"name": "x", "emails": [{"label": "w", "value": value}]}
                for value in ("not an email", "a@b", "@b.example", "a@b..example", "a b@c.example",
                              "a@b.example?bcc=z@y.example", "a@b.example/x", "a@b.example,c@d.example",
                              "<a@b.example>", "a@b.123", "a@@b.example")
            ],
            "unknown_field": [
                {"name": "x", "url": "https://example.com"}, {"name": "x", "phone": "555"},
                {"name": "x", "address": {"street": "1 Main", "county": "Sample"}},
                {"name": "x", "phones": [{"label": "m", "value": "555", "primary": True}]},
            ],
            "missing_field": [
                {"subtitle": "no name"}, {"name": "x", "phones": [{"label": "m"}]},
                {"name": "x", "emails": [{"label": "w"}]},
            ],
            "invalid_value": [
                {"name": 5}, {"name": "x", "address": "1 Main Street"},
                {"name": "x\ny"}, {"name": "x", "phones": [{"label": "m", "value": "555\n0100"}]}, {"name": {"literal": "x"}},
            ],
        }
        for code, cases in bad.items():
            for props in cases:
                with self.subTest(code=code, props=props):
                    self.assert_refused(code, one("contact", props))

    def test_a_label_may_be_left_out_and_an_empty_address_reads_as_none(self) -> None:
        for props in (
            {"name": "Sam", "phones": [{"value": "555 0100"}], "emails": [{"value": "sam@example.com"}]},
            {"name": "Sam", "phones": [{"label": "", "value": "555 0100"}]},
            {"name": "Sam", "address": {}},
            {"name": "Sam", "address": {"street": "", "city": "", "region": "", "postal_code": "", "country": ""}},
        ):
            with self.subTest(props=props):
                self.assertEqual(props, validate(one("contact", props))["elements"]["thing"]["props"])
        # The schema agents see allows the same.
        validator = Draft202012Validator(json.loads(SCHEMA.read_text(encoding="utf-8")))
        self.assertEqual([], [e.message for e in validator.iter_errors(one("contact", {
            "name": "Sam", "phones": [{"value": "555 0100"}], "address": {}}))])

    def test_place_ranges_and_limits(self) -> None:
        base = {"name": "Sample Cafe", "latitude": 10.5, "longitude": -20.5}
        for extra in ({"latitude": 90, "longitude": 180}, {"latitude": -90, "longitude": -180},
                      {"span_meters": 200}, {"span_meters": 50_000}, {"span_meters": 1_000.5},
                      {"subtitle": "s" * 200, "address": "a" * 300}):
            with self.subTest(extra=extra):
                validate(one("place", {**base, **extra}))
        for extra, code in (
            ({"latitude": 90.0001}, "invalid_value"), ({"latitude": -91}, "invalid_value"),
            ({"longitude": 180.5}, "invalid_value"), ({"longitude": -181}, "invalid_value"),
            ({"latitude": "10.5"}, "invalid_value"), ({"latitude": True}, "invalid_value"),
            ({"latitude": float("nan")}, "invalid_number"), ({"longitude": float("inf")}, "invalid_number"),
            ({"span_meters": 199}, "limit_exceeded"), ({"span_meters": 50_001}, "limit_exceeded"),
            ({"span_meters": "1000"}, "invalid_value"), ({"span_meters": True}, "invalid_value"),
            ({"address": "a" * 301}, "limit_exceeded"), ({"subtitle": "s" * 201}, "limit_exceeded"),
            ({"name": ""}, "limit_exceeded"), ({"zoom": 3}, "unknown_field"),
            ({"address": {"street": "1 Main"}}, "invalid_value"),
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(BighelpCardError) as caught:
                    validate_card_input(one("place", {**base, **extra}), now=NOW, allow_objects=True)
                self.assertEqual(code, caught.exception.code)
        for missing in ("name", "latitude", "longitude"):
            props = {key: value for key, value in base.items() if key != missing}
            with self.subTest(missing=missing):
                self.assert_refused("missing_field", one("place", props))

    def test_music_kinds_sources_and_urls(self) -> None:
        for kind in ("song", "album", "playlist", "artist"):
            validate(one("music", {"kind": kind, "title": "Sample"}))
        for source in ("catalog", "library"):
            validate(one("music", {"kind": "song", "title": "Sample", "music_id": "i" * 512, "source": source}))
        for url in ("https://music.apple.com/us/album/sample/1?i=2", "HTTPS://Music.Apple.Com/us/song/1",
                    "https://music.apple.com/" + "a" * 470):
            with self.subTest(url=url):
                validate(one("music", {"kind": "song", "title": "Sample", "url": url}))
        for url in ("http://music.apple.com/us/song/1", "https://music.apple.com", "https://music.apple.com/",
                    "https://music.apple.com.evil.example/x", "https://evil.example/music.apple.com/x",
                    "https://user@music.apple.com/x", "https://music.apple.com:8443/x",
                    "https://music.apple.com\\@evil.example/", "https://itunes.apple.com/us/album/1",
                    "music://music.apple.com/x", "javascript:alert(1)", "https://music.apple.com/a b",
                    "https://music.apple.com/é", "//music.apple.com/x", "music.apple.com/x"):
            with self.subTest(url=url):
                self.assert_refused("invalid_url", one("music", {"kind": "song", "title": "Sample", "url": url}))
        self.assert_refused("limit_exceeded", one("music", {
            "kind": "song", "title": "Sample", "url": "https://music.apple.com/" + "a" * 480}))
        for props, code in (
            ({"kind": "podcast", "title": "x"}, "invalid_value"), ({"kind": "song", "title": "x", "source": "cloud"}, "invalid_value"),
            ({"kind": "song", "title": ""}, "limit_exceeded"), ({"kind": "song", "title": "x" * 201}, "limit_exceeded"),
            ({"kind": "song", "title": "x", "artist": "a" * 201}, "limit_exceeded"),
            ({"kind": "song", "title": "x", "music_id": "i" * 513}, "limit_exceeded"),
            ({"kind": "song", "title": "x", "uri": "x"}, "unknown_field"),
            ({"title": "x"}, "missing_field"), ({"kind": "song"}, "missing_field"),
        ):
            with self.subTest(props=props):
                self.assert_refused(code, one("music", props))

    def test_objects_are_leaves_and_a_card_may_hold_several(self) -> None:
        card = one("place", {"name": "Sample Cafe", "latitude": 1, "longitude": 2})
        card["elements"]["thing"]["children"] = ["card"]
        self.assert_refused("invalid_children", card)
        many = {"schema": "loopdy.card", "version": 1, "title": "Places", "spoken_summary": "Places.",
                "data_sources": [], "root": "card", "elements": {
                    "card": {"type": "card", "props": {"title": "Places"},
                             "children": [f"p{number}" for number in range(3)]},
                    **{f"p{number}": {"type": "place", "props": {"name": f"Spot {number}", "latitude": number,
                                                                   "longitude": number}, "children": []}
                       for number in range(3)}}}
        self.assertEqual(3, len(validate(many)["elements"]) - 1)

    def test_objects_stay_out_of_anything_that_does_not_allow_them(self) -> None:
        # Templates and other callers get basic components unless they ask for objects.
        with self.assertRaises(BighelpCardError) as caught:
            validate_card_input(objects_card(), now=NOW)
        self.assertEqual("objects_unavailable", caught.exception.code)
        with self.assertRaises(BighelpCardError) as caught:
            render_card(objects_card(), now=NOW)
        self.assertEqual("objects_unavailable", caught.exception.code)
        self.assertIn("basic components", str(caught.exception))
        from loopdy_plugin import saved_card_templates
        with self.assertRaises(saved_card_templates._Rejected) as rejected:
            saved_card_templates._layout(objects_card(), now=NOW)
        self.assertEqual("invalid_layout", rejected.exception.code)

    def test_canonical_hash_is_stable_and_ignores_key_order(self) -> None:
        card = objects_card()
        first = render_card(card, now=NOW, allow_objects=True)
        reordered = json.loads(json.dumps(card, sort_keys=True))
        reordered["elements"] = dict(reversed(list(reordered["elements"].items())))
        second = render_card(reordered, now=NOW, allow_objects=True)
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(first["card_id"], first["content_hash"][:32])
        # Pinned: the app computes the same hash over the same document.
        self.assertEqual("9942f9d151ea3245a908a9e62968da0d2a1ee5d14220ad59c848cc9ba9a15ebb", first["content_hash"])
        changed = copy.deepcopy(card)
        changed["elements"]["spot"]["props"]["latitude"] = 37.6
        self.assertNotEqual(first["content_hash"], render_card(changed, now=NOW, allow_objects=True)["content_hash"])

    def test_a_rendered_card_validates_as_a_result_and_in_a_delivery(self) -> None:
        rendered = render_card(objects_card(), now=NOW, allow_objects=True)
        self.assertEqual(rendered, validate_card_result(rendered, now=NOW))
        tampered = copy.deepcopy(rendered)
        tampered["elements"]["spot"]["props"]["latitude"] = 38
        with self.assertRaises(BighelpCardError):
            validate_card_result(tampered, now=NOW)

    def test_basic_cards_hash_exactly_as_before(self) -> None:
        with_flag = render_card(metrics_card(), now=NOW, allow_objects=True)
        without = render_card(metrics_card(), now=NOW)
        self.assertEqual(without, with_flag)
        self.assertEqual("bf3cc2d664a5e7d1067c5e50173d5e643642e41e8a0f6f9c2703e962ad43563a",
                         render_card(metrics_card(), now=datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc))["content_hash"])


class RendersStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = PeopleStore(self.directory / "default")

    def test_a_chat_that_never_said_anything_draws_no_objects(self) -> None:
        self.assertEqual(frozenset(), self.store.renders(CHAT))

    def test_the_latest_note_replaces_the_list(self) -> None:
        self.assertEqual(["contact", "music", "place"],
                         self.store.set_renders(CHAT, ["place", "contact", "music", "place"], now=100))
        self.assertEqual(frozenset({"contact", "place", "music"}), self.store.renders(CHAT))
        self.store.set_renders(CHAT, ["place"], now=200)
        self.assertEqual(frozenset({"place"}), self.store.renders(CHAT))
        # An older app (or an empty list) means basic components only.
        self.store.set_renders(CHAT, [], now=300)
        self.assertEqual(frozenset(), self.store.renders(CHAT))

    def test_unknown_names_are_ignored(self) -> None:
        self.assertEqual(["place"], self.store.set_renders(CHAT, ["event", "place", "image", "card"], now=1))
        self.assertEqual(frozenset({"place"}), self.store.renders(CHAT))

    def test_chats_are_separate_and_survive_a_restart(self) -> None:
        self.store.set_renders(CHAT, ["contact"], now=1)
        self.assertEqual(frozenset(), self.store.renders(OTHER_CHAT))
        again = PeopleStore(self.directory / "default")
        self.assertEqual(frozenset({"contact"}), again.renders(CHAT))

    def test_agents_do_not_share_chats(self) -> None:
        other = PeopleStore(self.directory / "other-agent")
        self.store.set_renders(CHAT, ["contact"], now=1)
        self.assertEqual(frozenset(), other.renders(CHAT))
        other.set_renders(CHAT, ["music"], now=2)
        self.assertEqual(frozenset({"contact"}), self.store.renders(CHAT))
        self.assertEqual(frozenset({"music"}), other.renders(CHAT))

    def test_the_least_recently_noted_chats_go_first(self) -> None:
        self.assertEqual(2_048, people.MAX_RENDER_SESSIONS)
        with patch.object(people, "MAX_RENDER_SESSIONS", 3):
            for number in range(5):
                self.store.set_renders(f"chat-{number}", ["place"], now=100 + number)
            self.store.set_renders("chat-2", ["music"], now=200)  # noted again: now the newest
            self.store.set_renders("chat-5", ["contact"], now=201)
        kept = {number for number in range(6) if self.store.renders(f"chat-{number}")}
        self.assertEqual({2, 5, 4}, kept)

    def test_a_note_older_than_the_session_window_is_dropped(self) -> None:
        self.store.set_renders(CHAT, ["place"], now=1)
        self.store.set_renders(OTHER_CHAT, ["place"], now=1 + 181 * 86_400)
        self.assertEqual(frozenset(), self.store.renders(CHAT))
        self.assertEqual(frozenset({"place"}), self.store.renders(OTHER_CHAT))

    def test_a_bad_session_id_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.store.set_renders("../x", ["place"])


class CardGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.declared: dict[str, set[str]] = {}
        self.context = _Context()
        register(self.context, now=lambda: NOW, card_renders=lambda session: self.declared.get(session, set()))
        self.render = self.context.handlers["bighelp_render_card"]

    def call(self, card: dict, session: str | None = CHAT) -> dict:
        kwargs = {} if session is None else {"session_id": session}
        return extract_rendered_envelope(json.loads(self.render(card, **kwargs)))

    def refused(self, card: dict, session: str | None = CHAT) -> str:
        with self.assertRaises(BighelpCardError) as caught:
            self.call(card, session)
        self.assertEqual("card_objects_unavailable", caught.exception.code)
        return str(caught.exception)

    def test_a_chat_whose_app_declared_all_three_gets_every_object(self) -> None:
        self.declared[CHAT] = {"contact", "place", "music"}
        card = self.call(objects_card())
        self.assertEqual({"person", "spot", "song"}, set(card["elements"]) - {"card"})
        self.assertEqual("loopdy.card", card["schema"])

    def test_an_undeclared_chat_is_refused_with_a_note_to_use_basic_components(self) -> None:
        note = self.refused(objects_card())
        self.assertIn("basic components", note)
        self.assertIn("contact, music, place", note)
        self.assertIn("text", note)

    def test_declaring_one_object_does_not_unlock_the_others(self) -> None:
        self.declared[CHAT] = {"place"}
        note = self.refused(objects_card())
        self.assertIn("contact, music", note)
        self.assertNotIn("place", note.split("can't show ")[1].split(" components")[0])
        place_only = one("place", {"name": "Sample Cafe", "latitude": 1, "longitude": 2})
        self.assertEqual("place", self.call(place_only)["elements"]["thing"]["type"])

    def test_another_chat_and_a_call_with_no_chat_are_refused(self) -> None:
        self.declared[CHAT] = {"contact", "place", "music"}
        self.refused(objects_card(), OTHER_CHAT)
        self.refused(objects_card(), None)
        self.refused(objects_card(), "")

    def test_a_broken_lookup_refuses_instead_of_showing_an_unsupported_card(self) -> None:
        context = _Context()

        def broken(_session: str):
            raise RuntimeError("store is gone")

        register(context, now=lambda: NOW, card_renders=broken)
        with self.assertRaises(BighelpCardError) as caught:
            context.handlers["bighelp_render_card"](objects_card(), session_id=CHAT)
        self.assertEqual("card_objects_unavailable", caught.exception.code)

    def test_cards_without_objects_never_ask_the_lookup(self) -> None:
        def never(_session: str):
            raise AssertionError("looked up a chat for a basic card")

        context = _Context()
        register(context, now=lambda: NOW, card_renders=never)
        result = extract_rendered_envelope(json.loads(context.handlers["bighelp_render_card"](metrics_card())))
        self.assertEqual("Build health", result["title"])

    def test_a_malformed_object_card_cannot_skip_the_gate(self) -> None:
        card = objects_card()
        card["elements"]["person"]["props"] = {"name": "Sam", "oops": 1}
        self.refused(card)
        self.declared[CHAT] = {"contact", "place", "music"}
        with self.assertRaises(BighelpCardError) as caught:
            self.call(card)
        self.assertEqual("unknown_field", caught.exception.code)

    def test_agents_write_bighelp_names(self) -> None:
        self.declared[CHAT] = {"contact", "place", "music"}
        card = {**objects_card(), "schema": "bighelp.card"}
        self.assertEqual("loopdy.card", self.call(card)["schema"])

    def test_the_tool_tells_agents_about_the_objects(self) -> None:
        description = str(self.context.schemas["bighelp_render_card"]["description"])
        for word in ("contact", "place", "music", "basic components"):
            self.assertIn(word, description)
        self.assertLessEqual(len(card_objects.TOOL_NOTE), 330)
        elements = self.context.schemas["bighelp_render_card"]["parameters"]["$defs"]["element"]["oneOf"]
        self.assertEqual({"contact", "place", "music"},
                         {option["properties"]["type"]["const"] for option in elements} & {"contact", "place", "music"})


class DeclaredFromDiskTests(unittest.TestCase):
    """The card tool reads what the route wrote, through the store on disk."""

    def test_the_tool_sees_what_people_speaking_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = PeopleStore(Path(directory))
            context = _Context()
            register(context, now=lambda: NOW)
            handler = context.handlers["bighelp_render_card"]
            with patch.object(people, "current_store", return_value=store):
                with self.assertRaises(BighelpCardError):
                    handler(objects_card(), session_id=CHAT)
                store.set_renders(CHAT, ["contact", "place", "music"], now=1)
                self.assertTrue(handler(objects_card(), session_id=CHAT))
                with self.assertRaises(BighelpCardError):
                    handler(objects_card(), session_id=OTHER_CHAT)
                with self.assertRaises(BighelpCardError):
                    handler(objects_card(), session_id="../x")
                store.set_renders(CHAT, [], now=2)
                with self.assertRaises(BighelpCardError):
                    handler(objects_card(), session_id=CHAT)


class ChatBriefTests(unittest.TestCase):
    def test_the_brief_points_agents_at_the_objects_within_budget(self) -> None:
        section = agent_guide.prompt_section({"platform": "bighelp"})
        for word in ("contact", "place", "music", "iphone_contacts", "iphone_music"):
            self.assertIn(word, section)
        self.assertLessEqual(len(section), 2_800)

    def test_the_skill_teaches_the_objects(self) -> None:
        skill = (PLUGIN_ROOT / "skills" / "bighelp" / "SKILL.md").read_text(encoding="utf-8")
        reference = (PLUGIN_ROOT / "skills" / "generative-ui" / "references" / "bighelp-cards.md").read_text(encoding="utf-8")
        for text in (skill, reference):
            for word in ("`contact`", "`place`", "`music`"):
                self.assertIn(word, text)
        self.assertIn("iphone_contacts", skill)


if __name__ == "__main__":
    unittest.main()
