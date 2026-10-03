"""Card replies reach the phone as readable text, through the real reply-alert path.

Each test runs the actual renderer tools, puts their calls and results in the turn's history the
way Hermes does, finishes the turn through the post_llm_call hook, sends it and opens the sealed
alert as the phone would.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from loopdy_plugin.managed_notifications import ManagedNotifications, session_reference
from loopdy_plugin.relay_crypto import b64url_decode
from loopdy_plugin.store import BighelpStore
from loopdy_plugin.tools import register as register_tools
from test_managed_notifications import ManagedNotificationTests, open_sealed

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
V2 = PLUGIN_ROOT / "fixtures" / "generative_ui_v2"
CARD = PLUGIN_ROOT / "fixtures" / "loopdy_card_v1" / "static-metrics.json"
RENDERED_AT = datetime(2026, 8, 22, 0, 0, tzinfo=timezone.utc)


def fixture(name: str) -> dict:
    path = CARD if name == "card" else V2 / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class _Tools:
    profile_name = "default"

    def __init__(self) -> None:
        self.handlers: dict = {}
        self.schemas: dict = {}

    def register_tool(self, *, name, handler, schema, **_kwargs) -> None:
        self.handlers[name] = handler
        self.schemas[name] = schema


def _template(document: dict) -> dict:
    import hashlib

    from loopdy_plugin.loopdy_cards import canonical_json

    value = {
        "id": "build-status", "version": 1, "name": "Build status", "summary": "A build status card.",
        "author": "Fixture", "license": "MIT", "minimum_card_version": 1,
        "parameters_schema": {
            "type": "object",
            "properties": {"Status": {"type": "string", "enum": ["Ready", "Blocked"]}},
            "required": ["Status"], "additionalProperties": False,
        },
        "document": {**document, "title": "Build {{Status}}", "spoken_summary": "The build is {{Status}}."},
    }
    value["sha256"] = hashlib.sha256(canonical_json(value["document"]).encode("utf-8")).hexdigest()
    return value


class CardNotificationPreviewTests(ManagedNotificationTests):
    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = BighelpStore(Path(self.folder.name) / "loopdy.sqlite3")
        self.store.install_card_template(profile="default", template=_template(fixture("card")))
        self.tools = _Tools()
        register_tools(self.tools, store=self.store, profile="default", now=lambda: RENDERED_AT)
        self.histories: dict[str, list] = {}

    # The turn, as Hermes records it.

    def render(self, tool: str, arguments: dict, *, session: str = "native-session",
               bridge: bool = False) -> str:
        """Run a renderer as the agent would and keep the call and result in the session's history."""
        history = self.histories.setdefault(session, [{"role": "user", "content": "Show me."}])
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        result = self.tools.handlers[tool](json.loads(json.dumps(arguments)), session_id=session)
        name, sent = ("tool_call", {"name": tool, "arguments": arguments}) if bridge else (tool, arguments)
        history.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(sent)}}]})
        history.append({"role": "tool", "tool_call_id": call_id, "content": result})
        return json.loads(result)["display_markdown"]

    def finish(self, reply: str, *, session: str = "native-session", turn: str = "turn-a",
               platform: str = "desktop") -> None:
        history = self.histories.setdefault(session, [{"role": "user", "content": "Show me."}])
        history.append({"role": "assistant", "content": reply})
        self.service.observe("post_llm_call", profile="default", session_id=session, turn_id=turn,
                             assistant_response=reply, conversation_history=list(history), platform=platform)
        self.service.observe("on_session_end", profile="default", session_id=session, turn_id=turn,
                             completed=True, platform=platform)
        self.service.drain_pending()  # in turn order

    def alerts(self) -> list[str]:
        self.service.drain_pending()
        return [event["content"]["text"] for event in self.events_sent()]

    def assertReadable(self, text: str) -> None:
        for raw in ("```", "{", "}", "\"schema\"", "loopdy", "card_id", "content_hash", "request_id",
                    "native-session", "<", "data_sources"):
            self.assertNotIn(raw, text)
        self.assertTrue(text.strip())

    # Every kind of card reads as words.

    def test_a_generic_card_reads_as_its_title_and_summary(self):
        self.finish(self.render("bighelp_render_card", fixture("card")))
        [text] = self.alerts()
        self.assertEqual(text, "Build health: All twelve checks passed and the build is ready for review.")

    def test_typed_cards_read_as_their_values(self):
        cases = {
            "bighelp_render_weather_forecast": ("valid-weather", "Kansas City forecast: Clear, 79°F in Kansas City, MO."),
            "bighelp_render_stock_quote": ("valid-stock", "Fixture Corp: FIX 125.50 USD, up 1.01%."),
            "bighelp_render_sports_game": ("valid-sports-live", "Fixture live game: Away Team 71, Home Team 74, Q3 04:12."),
            "bighelp_render_chart": ("valid-chart", "Fixture trend: Fixture values increase across two days."),
            "bighelp_render_dashboard": ("valid-dashboard", "Fixture dashboard: One fixture metric and one bounded chart."),
        }
        for index, (tool, (name, expected)) in enumerate(cases.items()):
            with self.subTest(tool=tool):
                session = f"typed-{index}"
                self.finish(self.render(tool, fixture(name), session=session), session=session)
        self.assertEqual(self.alerts(), [expected for _, expected in cases.values()])

    def test_older_summary_metrics_and_list_cards_read_as_their_contents(self):
        cards = [
            ("bighelp_render_summary", {"schema": "bighelp.generative_ui", "version": 1, "component": "summary",
                                        "title": "Deploy", "body": "Version 4 is live."}),
            ("bighelp_render_metrics", {"schema": "bighelp.generative_ui", "version": 1, "component": "metrics",
                                        "title": "Today", "metrics": {"Steps": 8200, "Sleep": "7h"}}),
            ("bighelp_render_list", {"schema": "bighelp.generative_ui", "version": 1, "component": "list",
                                     "title": "Groceries", "items": ["Milk", "Eggs", "Bread", "Tea"]}),
        ]
        for index, (tool, card) in enumerate(cards):
            self.finish(self.render(tool, card, session=f"v1-{index}"), session=f"v1-{index}")
        self.assertEqual(self.alerts(), [
            "Deploy: Version 4 is live.",
            "Today: Sleep 7h, Steps 8200.",
            "Groceries: Milk, Eggs, Bread and 1 more.",
        ])

    def test_interactive_cards_read_without_their_request_identity(self):
        self.finish(self.render("bighelp_render_form", fixture("valid-form")))
        checklist = {"schema": "bighelp.generative_ui", "version": 2, "component": "checklist", "title": "Packing",
                     "data": {"items": [{"id": "passport", "label": "Passport", "completed": True},
                                        {"id": "charger", "label": "Charger", "completed": False}]},
                     "provenance": {"source_name": "You", "source_timestamp": "2026-08-21T23:00:00Z",
                                    "retrieved_at": "2026-08-21T23:00:00Z", "cache_status": "live"}}
        self.finish(self.render("bighelp_render_checklist", checklist, session="pack"), session="pack")
        form, packing = self.alerts()
        self.assertEqual(form, "Trip preference: Choose the preferred departure day. Do not enter credentials.")
        self.assertEqual(packing, "Packing: 1 of 2 done.")
        for text in (form, packing):
            self.assertReadable(text)

    def test_a_template_card_reads_as_its_filled_in_text(self):
        self.finish(self.render("bighelp_render_card_template",
                                {"template_id": "build-status", "parameters": {"Status": "Ready"}}))
        self.assertEqual(self.alerts(), ["Build Ready: The build is Ready."])

    # Authored previews.

    def test_an_authored_preview_is_used_for_its_own_card(self):
        weather = {**fixture("valid-weather"), "notification_text": "Clear and 79 tonight, no umbrella needed."}
        self.finish(self.render("bighelp_render_weather_forecast", weather))
        template = {"template_id": "build-status", "parameters": {"Status": "Blocked"},
                    "notification_text": "The build is blocked on one failing test."}
        self.finish(self.render("bighelp_render_card_template", template, session="build"), session="build")
        bridged = {**fixture("card"), "notification_text": "All checks passed."}
        self.finish(self.render("bighelp_render_card", bridged, session="bridge", bridge=True), session="bridge")
        self.assertEqual(self.alerts(), ["Clear and 79 tonight, no umbrella needed.",
                                         "The build is blocked on one failing test.",
                                         "All checks passed."])

    def test_the_preview_never_changes_the_card(self):
        plain = json.loads(self.tools.handlers["bighelp_render_stock_quote"](fixture("valid-stock")))
        authored = json.loads(self.tools.handlers["bighelp_render_stock_quote"](
            {**fixture("valid-stock"), "notification_text": "Fixture Corp is up a little."}))
        self.assertEqual(plain, authored)
        self.assertNotIn("notification_text", authored["display_markdown"])

    def test_renderers_refuse_a_preview_that_is_not_short_plain_text(self):
        for bad in ("x" * 301, {"text": "hi"}, 7, "```loopdy-card\n{}\n```", "<b>Done</b>", '{"schema": 1}'):
            with self.subTest(value=str(bad)[:20]), self.assertRaises(ValueError):
                self.tools.handlers["bighelp_render_card"]({**fixture("card"), "notification_text": bad})
        for empty in ("", "   ", None):
            with self.subTest(value=repr(empty)):
                self.tools.handlers["bighelp_render_card"]({**fixture("card"), "notification_text": empty})

    def test_every_renderer_documents_the_optional_preview(self):
        for name, schema in self.tools.schemas.items():
            if not name.startswith("bighelp_render_"):
                continue
            with self.subTest(tool=name):
                self.assertEqual(schema["parameters"]["properties"]["notification_text"],
                                 {"type": "string", "maxLength": 300})
                self.assertNotIn("notification_text", schema["parameters"].get("required", []))
                self.assertIn("notification_text", schema["description"])
                self.assertIn("never notifies", schema["description"])

    def test_bad_previews_in_history_fall_back_to_the_card(self):
        display = self.render("bighelp_render_card", fixture("card"))
        call = self.histories["native-session"][-2]["tool_calls"][0]["function"]
        for bad in (None, "", {"text": "x"}, "```loopdy-card\n{}\n```", "<i>Ready</i>"):
            with self.subTest(value=repr(bad)[:20]):
                call["arguments"] = json.dumps({**fixture("card"), "notification_text": bad})
                self.histories["native-session"] = self.histories["native-session"][:3]
                self.calls.clear()
                self.finish(display, turn=f"turn-{uuid.uuid4().hex[:6]}")
                self.assertEqual(self.alerts(), [
                    "Build health: All twelve checks passed and the build is ready for review."])
        call["arguments"] = json.dumps({**fixture("card"), "notification_text": "Word " * 200})
        self.histories["native-session"] = self.histories["native-session"][:3]
        self.calls.clear()
        self.finish(display, turn="turn-long")
        [text] = self.alerts()
        self.assertLessEqual(len(text), 300)
        self.assertTrue(text.startswith("Word Word"))

    # One reply, one alert.

    def test_prose_leads_and_each_card_reads_where_it_sits(self):
        display = self.render("bighelp_render_weather_forecast", fixture("valid-weather"))
        self.finish(f"Here's tonight in Kansas City.\n\n{display}\n\nEnjoy the evening!")
        self.assertEqual(self.alerts(), [
            "Here's tonight in Kansas City. Kansas City forecast: Clear, 79°F in Kansas City, MO. Enjoy the evening!"])

    def test_several_cards_make_one_bounded_alert(self):
        first = self.render("bighelp_render_stock_quote", fixture("valid-stock"))
        second = self.render("bighelp_render_card", {**fixture("card"), "notification_text": "Build is green."})
        self.finish(f"{first}\n{second}")
        self.assertEqual(self.alerts(), ["Fixture Corp: FIX 125.50 USD, up 1.01%. Build is green."])
        many = "\n".join(self.render("bighelp_render_summary", {
            "schema": "bighelp.generative_ui", "version": 1, "component": "summary",
            "title": f"Item {index}", "body": "x" * 400}, session="many") for index in range(12))
        self.finish(many, session="many")
        texts = self.alerts()
        self.assertEqual(len(texts), 2, "one alert per reply, never one per card")
        text = texts[-1]
        self.assertLessEqual(len(text), 1_600)
        self.assertTrue(text.startswith("Item 0: "))
        self.assertReadable(text)

    # Cards that aren't what they claim.

    def test_legacy_unknown_and_broken_cards_never_show_their_payload(self):
        weather = json.loads(self.tools.handlers["bighelp_render_weather_forecast"](fixture("valid-weather")))
        cases = {
            # The retired scheduler Inbox path: the whole reply is the card's JSON.
            json.dumps(weather["card"]): "Kansas City forecast: Clear, 79°F in Kansas City, MO.",
            json.dumps(weather): "Kansas City forecast: Clear, 79°F in Kansas City, MO.",
            "```loopdy-card\n{\"schema\":\"loopdy.future\",\"version\":9,\"title\":\"x\"}\n```": "Sent a card.",
            "```loopdy-card\n{not json\n```": "Sent a card.",
            "Here it is:\n```loopdy-card\n{\"schema\":\"loopdy.card\",\"version\":1,\"title\":\"Half": "Here it is: Sent a card.",
            # A card whose values were edited after rendering fails the app's check too.
            weather["display_markdown"].replace("Kansas City forecast", "Edited forecast"): "Sent a card.",
            "```loopdy-card\n[1, 2, 3]\n```": "Sent a card.",
        }
        for index, reply in enumerate(cases):
            self.finish(reply, session=f"odd-{index}")
        texts = self.alerts()
        self.assertEqual(texts, list(cases.values()))

    def test_ordinary_replies_alert_exactly_as_before(self):
        replies = [
            "Your flight to Denver is booked for Friday.",
            "Here's the fix:\n\n```python\nprint({'a': 1})\n```\nRun it again.",
            # A card shown as an example inside an ordinary code block is not a card (the app agrees).
            "The format looks like this:\n````markdown\n```loopdy-card\n{\"schema\":\"loopdy.card\"}\n```\n````",
            '{"answer": 42}',
        ]
        for index, reply in enumerate(replies):
            self.finish(reply, session=f"plain-{index}")
        self.assertEqual(self.alerts(), [" ".join(reply.split()) for reply in replies])

    # Exact association.

    def test_a_preview_comes_only_from_the_card_in_this_reply(self):
        # A card rendered but never sent lends nothing, even to a later card in the same chat.
        self.render("bighelp_render_card", {**fixture("card"), "notification_text": "Unused card preview."})
        self.finish("I'll leave the card for now.")
        stock = self.render("bighelp_render_stock_quote", fixture("valid-stock"))
        self.finish(stock, turn="turn-b")
        self.assertEqual(self.alerts(), ["I'll leave the card for now.", "Fixture Corp: FIX 125.50 USD, up 1.01%."])

    def test_concurrent_chats_keep_their_own_previews(self):
        first = self.render("bighelp_render_card", {**fixture("card"), "notification_text": "Chat one's build."},
                            session="chat-one")
        second = self.render("bighelp_render_card", fixture("card"), session="chat-two")
        self.assertEqual(first, second, "the same card, rendered in two chats")
        self.finish(second, session="chat-two")
        self.finish(first, session="chat-one")
        self.service.drain_pending()
        sent = {event["sessionReference"]: event["content"]["text"] for event in self.events_sent()}
        self.assertEqual(sent, {
            session_reference("default", "chat-two"):
                "Build health: All twelve checks passed and the build is ready for review.",
            session_reference("default", "chat-one"): "Chat one's build.",
        })

    def test_retries_and_a_restart_send_the_same_preview(self):
        self.fail_send = True
        self.finish(self.render("bighelp_render_card", {**fixture("card"), "notification_text": "Build is green."}))
        self.service.drain_pending()
        failed = self.calls[-1][2]
        self.fail_send = False
        self.service.close()
        # A fresh process reads the queued alert from disk.
        self.service = ManagedNotifications(Path(self.temp.name) / "managed", transport=self.transport,
                                            clock=lambda: self.now,
                                            session_opener=lambda profile, read, read_only: read(self))
        self.now += 30
        self.service.drain_pending()
        self.assertEqual(self.calls[-1][2], failed)
        self.assertEqual([event["content"]["text"] for event in self.events_sent()][-1], "Build is green.")

    def test_a_scheduled_card_reply_alerts_with_its_preview(self):
        self.grant["eventTypes"] = ["scheduled.completed", "scheduled.failed", "session.completed", "session.failed"]
        self.grant_id = self.grant["grantId"] = str(uuid.uuid4())
        self.enroll_with_key(self.grant_id)
        cron = "cron_d2b364c4a34d_20260923_093038"
        self.finish(self.render("bighelp_render_weather_forecast", fixture("valid-weather"), session=cron),
                    session=cron, turn="turn-c", platform="cron")
        self.finish("[SILENT]", session=cron, turn="turn-d", platform="cron")
        self.service.drain_pending()
        [event] = self.events_sent()
        self.assertEqual(event["eventType"], "scheduled.completed")
        self.assertEqual(event["content"]["text"], "Kansas City forecast: Clear, 79°F in Kansas City, MO.")

    def test_routing_and_tap_target_are_unchanged(self):
        self.finish(self.render("bighelp_render_card", fixture("card")), turn="turn-z")
        self.service.drain_pending()
        [event] = self.events_sent()
        self.assertEqual((event["eventType"], event["sessionReference"], event["turnId"]),
                         ("session.completed", session_reference("default", "native-session"), "turn-z"))
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
        opened = open_sealed(json.loads(self.calls[-1][2])["sealed"], self.phone,
                             b64url_decode(self.service.public_key))
        self.assertEqual(opened["title"], "Fixture Agent")

    def test_rendering_alone_never_alerts(self):
        self.service.observe("pre_llm_call", profile="default", session_id="native-session", turn_id="turn-a",
                             platform="desktop")
        for tool, card in (("bighelp_render_card", fixture("card")), ("bighelp_render_form", fixture("valid-form"))):
            self.render(tool, {**card, "notification_text": "Should not alert."})
            call, result = self.histories["native-session"][-2:]
            for hook in ("pre_tool_call", "post_tool_call"):
                self.service.observe(hook, profile="default", session_id="native-session", turn_id="turn-a",
                                     tool_name=tool, tool_call_id=call["tool_calls"][0]["id"],
                                     result=result["content"], status="ok")
        self.service.drain_pending()
        self.assertEqual(self.events_sent(), [])


def load_tests(loader, _tests, _pattern):
    # The base class's own tests already run in test_managed_notifications.
    import unittest

    return unittest.TestSuite(CardNotificationPreviewTests(name)
                              for name in loader.getTestCaseNames(CardNotificationPreviewTests)
                              if name in CardNotificationPreviewTests.__dict__)
