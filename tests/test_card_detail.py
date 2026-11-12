from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from loopdy_plugin import card_detail
from loopdy_plugin.managed_notifications import ManagedNotificationError
from loopdy_plugin.notification_crypto import b64url_decode, b64url_encode
from test_managed_notifications import ManagedNotificationTests

VECTOR = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "sealed-card-v1-vector.json"
KEY = bytes(range(32))


class CardDetailFormatTests(unittest.TestCase):
    def test_shared_vector_opens_and_is_what_this_host_seals(self):
        vector = json.loads(VECTOR.read_text())
        key = b64url_decode(vector["key"])
        self.assertEqual(card_detail.open_sealed(key, vector["timestamp"], vector["sealed"]), vector["detail"])
        plain = json.dumps(vector["detail"], ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        self.assertEqual(card_detail.seal(key, vector["timestamp"], plain, nonce=b64url_decode(vector["nonce"])),
                         vector["sealed"])
        with self.assertRaises(Exception):  # another update's time
            card_detail.open_sealed(key, vector["timestamp"] + 1, vector["sealed"])

    def test_detail_stays_inside_one_rows_share_and_keeps_secrets_out(self):
        detail = card_detail.new_detail(100)
        card_detail.tool_started(detail, "terminal", {"command": "export OPENAI_API_KEY=sk-" + "a" * 48})
        self.assertEqual(detail["note"], "")
        for index in range(5):
            card_detail.tool_started(detail, "web_search", {"query": "protein powder price per lb " + "x" * 300})
            card_detail.tool_finished(detail, "web_search", {})
        detail["preview"], detail["say"] = "p" * 5_000, "s" * 5_000
        detail["goals"] = {"a": "g" * 60, "b": "h" * 60, "c": "i" * 60}
        plain = card_detail.plaintext(detail, "completed")
        self.assertLessEqual(len(card_detail.seal(KEY, 1, plain)), card_detail.MAX_SEALED_CHARS)
        body = json.loads(plain)
        self.assertEqual(body["tr"][0]["c"], 5)
        self.assertEqual(body["s"], 6)
        self.assertNotIn("t", body)  # nothing is in progress once the turn ended


class SealedCardUpdateTests(ManagedNotificationTests):
    def bind(self, key: bytes | None = KEY):
        self.service.observe("pre_llm_call", profile="default", session_id="native-session", turn_id="turn-a", platform="desktop")
        with sqlite3.connect(self.service.db_path) as db:
            db.execute("INSERT INTO activities(activity_id,grant_id,profile,session_id,session_ref,lease_expires,work_turn,state) VALUES(?,?,?,?,?,?,?,'active')",
                       ("activity-a", self.grant_id, "default", "native-session", "x" * 43, self.now + 7_200, "turn-a"))
        original = self.service.transport
        def transport(method, path, raw, headers):
            # The notification service takes an update.
            if path.endswith("/updates"): return {"version": 1, "status": "accepted", "deliveryId": "live_fixture"}
            return original(method, path, raw, headers)
        self.service.transport = transport
        if key: self.service.set_card_key(self.grant_id, "activity-a", b64url_encode(key))

    def pending(self) -> dict:
        with sqlite3.connect(self.service.db_path) as db:
            return json.loads(db.execute("SELECT raw FROM pending WHERE activity_id='activity-a' AND state='pending'").fetchone()[0])

    def detail(self, update: dict) -> dict:
        return card_detail.open_sealed(KEY, update["timestamp"], update["sealed"])

    def tool(self, hook: str, name: str, **args):
        self.service.observe(hook, profile="default", session_id="native-session", turn_id="turn-a", tool_name=name,
                             args=args, tool_call_id="call-1")

    def test_without_a_key_updates_are_what_they_were(self):
        self.bind(key=None)
        self.tool("pre_tool_call", "web_search", query="private query")
        update = self.pending()
        self.assertNotIn("sealed", update); self.assertNotIn("quiet", update)
        self.assertNotIn("private", json.dumps(update))

    def test_a_key_makes_the_next_update_carry_the_step_only_inside_the_seal(self):
        self.bind()
        first = self.pending()  # setting the key queues the card's state at once
        self.assertEqual(self.detail(first)["s"], 0)
        self.now += 10
        self.tool("pre_tool_call", "web_search", query="protein price per lb")
        update = self.pending()
        self.assertNotIn("protein", json.dumps(update))
        self.assertIsNone(update["latestTool"])
        detail = self.detail(update)
        self.assertEqual((detail["t"], detail["n"], detail["s"], detail["b"]), ("web_search", "protein price per lb", 1, self.now - 10))
        self.assertNotIn("quiet", update)  # the phase changed: high priority

    def test_a_new_step_alone_goes_quietly_and_one_update_in_thirty_seconds_is_loud(self):
        self.bind()
        self.tool("pre_tool_call", "web_search", query="one")
        self.service.drain_pending()
        self.now += 9
        self.tool("post_tool_call", "web_search")
        self.tool("pre_tool_call", "read_file", path="/Users/someone/prices.csv")
        update = self.pending()
        self.assertIs(update["quiet"], True)
        detail = self.detail(update)
        self.assertEqual(detail["n"], "prices.csv")
        self.assertEqual(detail["tr"], [{"t": "web_search", "c": 1, "n": "one"}])
        self.service.drain_pending()
        self.now += 31
        self.tool("post_tool_call", "read_file")
        self.assertNotIn("quiet", self.pending())

    def test_a_question_waits_on_the_card_and_the_reply_shows_at_the_end(self):
        self.bind()
        self.tool("pre_tool_call", "clarify", question="Send the order for $47.00?")
        update = self.pending()
        self.assertEqual(update["phase"], "waiting")
        self.assertEqual(self.detail(update)["q"], "Send the order for $47.00?")
        self.tool("post_tool_call", "clarify")
        self.assertEqual(self.pending()["phase"], "using_tool")
        self.service.observe("post_api_request", profile="default", session_id="native-session", turn_id="turn-a",
                             assistant_message={"content": "Comparing four brands now."}, model="vendor/sample-model-1",
                             usage={"prompt_tokens": 30_300}, context_length=272_000)
        self.now += 9
        self.tool("pre_tool_call", "execute_code")
        detail = self.detail(self.pending())
        self.assertEqual((detail["say"], detail["cu"], detail["cm"], detail["m"]),
                         ("Comparing four brands now.", 30_300, 272_000, "sample-model-1"))
        self.service.observe("post_llm_call", profile="default", session_id="native-session", turn_id="turn-a",
                             assistant_response="Cheapest per lb is Northfield Whey at $9.40.")
        self.service.observe("on_session_end", profile="default", session_id="native-session", turn_id="turn-a",
                             completed=True, platform="desktop")
        update = self.pending()
        self.assertEqual(update["phase"], "completed")
        self.assertEqual(self.detail(update)["p"], "Cheapest per lb is Northfield Whey at $9.40.")

    def test_a_service_that_refuses_sealed_detail_gets_plain_updates_after(self):
        self.bind()
        original = self.service.transport
        def transport(method, path, raw, headers):
            if path.endswith("/updates") and b'"sealed"' in raw: raise ManagedNotificationError("notification_activity_invalid", 400)
            return original(method, path, raw, headers)
        self.service.transport = transport
        self.service.drain_pending()
        self.now += 31
        self.tool("pre_tool_call", "web_search", query="one")
        self.assertNotIn("sealed", self.pending())

    def test_only_a_real_key_for_a_known_card_is_taken(self):
        self.bind(key=None)
        for bad in ("short", "!" * 43, None):
            with self.assertRaises(ManagedNotificationError): self.service.set_card_key(self.grant_id, "activity-a", bad)
        with self.assertRaises(ManagedNotificationError): self.service.set_card_key(self.grant_id, "activity-b", b64url_encode(KEY))
        self.assertEqual(self.service.capabilities()["sealedCardDetail"], {"version": 1})


class CardRefreshTests(SealedCardUpdateTests):
    def test_a_question_that_waits_is_sent_again_before_the_card_goes_stale(self):
        self.bind()
        self.tool("pre_tool_call", "clarify", question="Send the order for $47.00?")
        first = self.pending()
        self.service.drain_pending()
        self.now += 30
        self.service.refresh_activities()  # inside the minute: nothing new
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pending WHERE activity_id='activity-a' AND state='pending'").fetchone()[0], 0)
        self.now += 2_000  # no hook for half an hour and more: a question still waits
        self.service.refresh_activities()
        again = self.pending()
        self.assertEqual(again["phase"], "waiting")
        self.assertGreater(again["timestamp"], first["timestamp"])
        self.assertEqual(self.detail(again)["q"], "Send the order for $47.00?")
        self.assertNotIn("quiet", again)

    def test_a_long_tool_call_is_sent_again_but_not_for_ever(self):
        self.bind()
        self.tool("pre_tool_call", "terminal", command="make build")
        self.service.drain_pending()
        self.now += 90
        self.service.refresh_activities()
        self.assertEqual(self.detail(self.pending())["t"], "terminal")
        self.service.drain_pending()
        self.now += 1_900
        self.service.refresh_activities()
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pending WHERE activity_id='activity-a' AND state='pending'").fetchone()[0], 0)

    def test_a_phase_change_that_never_left_keeps_its_priority_when_a_step_takes_its_place(self):
        self.bind()
        self.service.drain_pending()
        self.now += 2
        self.tool("pre_tool_call", "web_search", query="one")   # thinking -> using_tool, waits for its turn
        self.assertNotIn("quiet", self.pending())
        self.tool("post_tool_call", "web_search")
        self.tool("pre_tool_call", "read_file", path="a.txt")    # same phase, takes its place
        self.assertNotIn("quiet", self.pending())

    def test_the_same_key_again_changes_nothing_and_odd_input_never_stops_updates(self):
        self.bind()
        self.service.drain_pending()
        self.service.set_card_key(self.grant_id, "activity-a", b64url_encode(KEY))
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pending WHERE activity_id='activity-a' AND state='pending'").fetchone()[0], 0)
        self.now += 10
        self.tool("pre_tool_call", "web_extract", url="http://[broken")
        self.assertEqual(self.pending()["phase"], "using_tool")
        detail = card_detail.new_detail(1)
        for index in range(3):
            card_detail.tool_started(detail, "工" * 48 + str(index), {})
            card_detail.tool_finished(detail, "工" * 48 + str(index), {})
        detail["tool"] = "具" * 48
        self.assertLessEqual(len(card_detail.seal(KEY, 1, card_detail.plaintext(detail, "using_tool"))), card_detail.MAX_SEALED_CHARS)
