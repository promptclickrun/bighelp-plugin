from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from loopdy_plugin.agent_board import (
    ANSWER_MEMORY_SECONDS, GOAL_CATEGORIES, TOOL_PARAMETERS, ActivityRecorder, BoardError, BoardStore,
    MAX_ITEMS_PER_KIND, handle_tool, tool_category,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


class AgentBoardStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = BoardStore(self.root / "board")

    def test_posts_copy_images_and_reject_non_images(self):
        image = self.root / "porch.png"
        image.write_bytes(PNG)
        item = self.store.publish("feed", title="Porch", body="Bag delivered.", images=[str(image)])
        image.unlink()  # a cleaned cache must not break the post
        mime, data = self.store.image(item["id"], 0)
        self.assertEqual((mime, data), ("image/png", PNG))
        secret = self.root / "notes.txt"
        secret.write_text("private")
        with self.assertRaises(BoardError):
            self.store.publish("feed", title="Leak", images=[str(secret)])
        with self.assertRaises(BoardError):
            self.store.publish("feed", title="Relative", images=["porch.png"])
        with self.assertRaises(BoardError):
            self.store.publish("feed", title="", body="No title")
        with self.assertRaises(BoardError):
            self.store.publish("feed", title="Bad link", links=["javascript:alert(1)"])

    def test_goals_update_in_place_and_ideas_reuse_ids(self):
        self.store.publish("goal", title="Package watch", section="tracking", note="Ordered", item_id="pkg")
        self.store.update_goal("pkg", note="Out for delivery")
        again = self.store.publish("goal", title="Package watch", section="tracking", note="Delivered",
                                   item_id="pkg", status="done")
        goals = self.store.items(("goal",))
        self.assertEqual(len(goals), 1)
        self.assertEqual((goals[0]["note"], goals[0]["status"]), ("Delivered", "done"))
        self.assertEqual(again["createdAt"], goals[0]["createdAt"])
        with self.assertRaises(BoardError):
            self.store.publish("goal", title="Nope", section="wishlist")
        with self.assertRaises(BoardError):
            self.store.publish("idea", title="Clash", item_id="pkg")
        self.store.publish("idea", title="Audit Google access", section="Security", item_id="audit")
        self.store.publish("idea", title="Audit Google access now", section="Security", item_id="audit")
        self.assertEqual([idea["title"] for idea in self.store.items(("idea",))], ["Audit Google access now"])

    def test_goals_carry_a_category_from_the_fixed_list(self):
        self.assertEqual(GOAL_CATEGORIES, ("health", "relationships", "finance", "career", "interests",
                                           "productivity", "other"))
        goal = self.store.publish("goal", title="Run a 10k", category="Health", item_id="10k")
        self.assertEqual(goal["category"], "health")
        self.assertEqual(self.store.publish("goal", title="Inbox under 20")["category"], "")
        with self.assertRaises(BoardError):
            self.store.publish("goal", title="Nope", category="hobbies")
        with self.assertRaises(BoardError):
            self.store.publish("goal", title="Nope", category=3)
        # Republishing without one keeps it; update_goal can move it.
        self.assertEqual(self.store.publish("goal", title="Run a 10k", note="Week 2", item_id="10k")["category"],
                         "health")
        self.assertEqual(self.store.update_goal("10k", category="interests")["category"], "interests")
        self.assertEqual(self.store.update_goal("10k", note="Week 3")["category"], "interests")
        with self.assertRaises(BoardError):
            self.store.update_goal("10k", category="hobbies")
        # Only goals have one.
        self.assertEqual(self.store.publish("feed", title="News", category="health")["category"], "")
        self.assertEqual(self.store.publish("idea", title="Plan", category="health")["category"], "")

    def test_the_agent_sets_and_sees_goal_categories(self):
        self.assertEqual(TOOL_PARAMETERS["properties"]["category"]["enum"], list(GOAL_CATEGORIES))
        created = json.loads(handle_tool({"action": "goal", "title": "Save for Lisbon", "category": "finance",
                                          "id": "lisbon"}, self.store))
        self.assertEqual(created["kind"], "goal")
        listed = json.loads(handle_tool({"action": "list", "kind": "goal"}, self.store))["items"]
        self.assertEqual(listed[0]["category"], "finance")
        moved = json.loads(handle_tool({"action": "update_goal", "id": "lisbon", "category": "other"}, self.store))
        self.assertTrue(moved["ok"])
        self.assertIn("error", json.loads(handle_tool({"action": "goal", "title": "x", "category": "pets"},
                                                      self.store)))

    def test_an_idea_in_a_category_section_becomes_a_goal_in_that_category(self):
        health = self.store.publish("idea", title="Walk after lunch", section="Health")
        money = self.store.publish("idea", title="Cheaper phone plan", section="Money")
        self.assertEqual(self.store.promote_idea(health["id"])["category"], "health")
        self.assertEqual(self.store.promote_idea(money["id"])["category"], "")

    def test_dismissed_items_hide_and_republishing_restores(self):
        item = self.store.publish("goal", title="Package watch", section="tracking", item_id="pkg")
        self.store.set_flags(item["id"], dismissed=True, liked=True)
        self.assertEqual(self.store.items(("goal",)), [])
        self.assertTrue(self.store.items(("goal",), include_dismissed=True)[0]["liked"])
        self.store.publish("goal", title="Package watch", section="tracking", item_id="pkg")
        self.assertEqual(len(self.store.items(("goal",))), 1)

    def test_not_now_reaches_the_agent_and_blocks_the_same_offer(self):
        self.store.publish("idea", title="Fitness plan", section="Health", item_id="fit", now=1_000)
        self.store.set_flags("fit", dismissed=True)
        listed = json.loads(handle_tool({"action": "list", "kind": "idea"}, self.store))
        self.assertEqual(listed["items"], [], "Not now still takes it off the board")
        answered = listed["answered"]
        self.assertEqual([(row["id"], row["answer"], row["section"]) for row in answered],
                         [("fit", "not now", "Health")])
        refused = json.loads(handle_tool({"action": "idea", "id": "fit", "title": "Fitness plan"}, self.store))
        self.assertIn("not now", refused["error"])
        self.assertEqual(self.store.items(("idea",)), [], "Re-offering under the same id stays hidden")
        # Undo in the app clears the answer.
        self.store.set_flags("fit", dismissed=False)
        self.assertEqual(self.store.items(("idea",))[0]["answer"], "none")

    def test_not_now_expires_after_thirty_days(self):
        self.store.publish("idea", title="Fitness plan", item_id="fit")
        self.store.set_flags("fit", dismissed=True, now=1_000)
        later = 1_000 + ANSWER_MEMORY_SECONDS + 1
        self.assertEqual(self.store.answered_ideas(now=later), [])
        again = self.store.publish("idea", title="Fitness plan", item_id="fit", now=later)
        self.assertEqual((again["dismissed"], again["answer"]), (False, "none"))

    def test_feed_deletes_are_just_clearing_and_never_count_as_answers(self):
        post = self.store.publish("feed", title="Morning brief")
        self.store.set_flags(post["id"], dismissed=True)
        listed = json.loads(handle_tool({"action": "list"}, self.store))
        self.assertEqual((listed["items"], listed["answered"]), ([], []))

    def test_lets_do_it_marks_the_idea_yes(self):
        self.store.publish("idea", title="Draft a late-fee clause", item_id="fee")
        self.store.publish("idea", title="Plan a trip", item_id="trip")
        recorder = ActivityRecorder(lambda: self.store)
        recorder.observe("pre_llm_call", session_id="s1", turn_id="t1",
                         user_message="Yes, go ahead with this idea: \u201cDraft a late-fee clause\u201d.")
        by_id = {row["id"]: row for row in json.loads(handle_tool({"action": "list", "kind": "idea"},
                                                                   self.store))["items"]}
        self.assertEqual((by_id["fee"]["answer"], by_id["trip"]["answer"]), ("yes", "none"))
        # Clearing a finished idea afterwards keeps the yes.
        self.store.set_flags("fee", dismissed=True)
        self.assertEqual(self.store.answered_ideas()[0]["answer"], "yes")

    def test_make_it_a_goal_is_remembered_as_an_answer(self):
        self.store.publish("idea", title="Sleep by 11", item_id="sleep")
        self.store.promote_idea("sleep")
        self.assertEqual([(row["id"], row["answer"]) for row in self.store.answered_ideas()], [("sleep", "goal")])

    def test_each_kind_is_capped(self):
        for index in range(MAX_ITEMS_PER_KIND + 3):
            self.store.publish("feed", title=f"Post {index}", now=1_000 + index)
        feed = self.store.items(("feed",), limit=200)
        self.assertEqual(feed[0]["title"], f"Post {MAX_ITEMS_PER_KIND + 2}")
        with self.store._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], MAX_ITEMS_PER_KIND)

    def test_tool_reports_errors_to_the_agent_instead_of_raising(self):
        self.assertEqual(json.loads(handle_tool({"action": "post", "title": ""}, self.store)),
                         {"error": "title is required."})
        created = json.loads(handle_tool({"action": "idea", "title": "Track sleep", "icon": "🌙",
                                          "section": "Health"}, self.store))
        self.assertEqual((created["kind"], created["shownIn"]), ("idea", "Ideas"))
        listed = json.loads(handle_tool({"action": "list", "kind": "idea"}, self.store))
        self.assertEqual(listed["items"][0]["title"], "Track sleep")
        self.assertEqual(json.loads(handle_tool({"action": "remove", "id": created["id"]}, self.store)),
                         {"removed": True})
        self.assertIn("error", json.loads(handle_tool({"action": "launch"}, self.store)))


    def test_feedback_rates_items_with_a_reason_and_the_agent_sees_it(self):
        post = self.store.publish("feed", title="Evening AI news", source="Evening AI news")
        other = self.store.publish("feed", title="Stock tips")
        self.assertEqual((post["rating"], post["reason"], post["read"]), ("none", "", False))
        liked = self.store.set_flags(post["id"], rating="up")
        self.assertEqual((liked["rating"], liked["liked"]), ("up", True))
        down = self.store.set_flags(other["id"], rating="down", reason="Not relevant")
        self.assertEqual((down["rating"], down["reason"], down["liked"]), ("down", "Not relevant", False))
        # A reason belongs to a thumbs down only.
        self.assertEqual(self.store.set_flags(post["id"], reason="Too frequent")["reason"], "")
        # Older apps still send liked.
        self.assertEqual(self.store.set_flags(other["id"], liked=True)["rating"], "up")
        self.assertEqual(self.store.set_flags(other["id"], liked=False)["rating"], "none")
        with self.assertRaises(BoardError):
            self.store.set_flags(post["id"], rating="sideways")
        self.store.set_flags(other["id"], rating="down", reason="Already knew")
        listed = json.loads(handle_tool({"action": "list", "kind": "feed"}, self.store))["items"]
        by_title = {item["title"]: item for item in listed}
        self.assertEqual(by_title["Evening AI news"]["rating"], "up")
        self.assertEqual((by_title["Stock tips"]["rating"], by_title["Stock tips"]["reason"]), ("down", "Already knew"))
        self.assertEqual(by_title["Stock tips"]["read"], False)
        self.assertIn("source", by_title["Evening AI news"])

    def test_read_state_survives_and_marks_in_bulk(self):
        first = self.store.publish("feed", title="One")
        second = self.store.publish("idea", title="Two")
        self.assertEqual(self.store.mark_read([first["id"], second["id"], "gone"], read=True), 2)
        self.assertTrue(all(item["read"] for item in BoardStore(self.root / "board").items()))
        self.assertFalse(self.store.set_flags(first["id"], read=False)["read"])
        with self.assertRaises(BoardError):
            self.store.mark_read(["x"] * 201)

    def test_hide_can_be_undone_and_hidden_items_stay_out_of_the_agents_list(self):
        item = self.store.publish("idea", title="Plan a trip")
        self.store.set_flags(item["id"], dismissed=True)
        self.assertEqual(json.loads(handle_tool({"action": "list", "kind": "idea"}, self.store))["items"], [])
        self.store.set_flags(item["id"], dismissed=False)
        self.assertEqual([row["title"] for row in self.store.items(("idea",))], ["Plan a trip"])

    def test_an_idea_turns_into_a_goal(self):
        idea = self.store.publish("idea", title="Sleep by 11", body="I can nudge you at 10:30.", icon="🌙")
        goal = self.store.promote_idea(idea["id"])
        self.assertEqual((goal["kind"], goal["title"], goal["icon"], goal["status"]), ("goal", "Sleep by 11", "🌙", "active"))
        self.assertEqual(goal["section"], "goal")
        self.assertEqual(self.store.items(("idea",)), [], "The idea moved to Goals")
        self.assertEqual(self.store.publish("idea", title="Sleep by 11", item_id="later")["answer"], "none")
        with self.assertRaises(BoardError):
            self.store.promote_idea(goal["id"])

    def test_old_boards_keep_likes_and_start_read(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        import sqlite3
        db = sqlite3.connect(legacy / "board.sqlite3")
        db.executescript("""CREATE TABLE items(
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT '',
            section TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '', links TEXT NOT NULL DEFAULT '[]',
            images TEXT NOT NULL DEFAULT '[]', source TEXT NOT NULL DEFAULT '',
            liked INTEGER NOT NULL DEFAULT 0, dismissed INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, updated REAL NOT NULL);
            INSERT INTO items(id,kind,title,liked,created,updated) VALUES('a','feed','Liked',1,1,1);
            INSERT INTO items(id,kind,title,liked,created,updated) VALUES('b','feed','Plain',0,2,2);""")
        db.commit()
        db.close()
        items = {item["id"]: item for item in BoardStore(legacy).items()}
        self.assertEqual((items["a"]["rating"], items["b"]["rating"]), ("up", "none"))
        self.assertTrue(items["a"]["read"] and items["b"]["read"], "Nothing already there shows as new")
        self.assertEqual((items["a"]["category"], items["b"]["category"]), ("", ""), "Old goals have no category")
        self.assertFalse(BoardStore(legacy).publish("feed", title="Fresh")["read"])

class ActivityRecorderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = BoardStore(Path(self.temporary.name))
        self.recorder = ActivityRecorder(lambda: self.store)

    def turn(self, tools, response="Done. The bag is by the chair.", **end):
        base = {"session_id": "s1", "turn_id": "t1"}
        self.recorder.observe("pre_llm_call", user_message="Did the dog food arrive? Check the porch.", **base)
        for tool in tools:
            self.recorder.observe("post_tool_call", tool_name=tool, **base)
        self.recorder.observe("post_llm_call", assistant_response=response, **base)
        self.recorder.observe("on_session_end", completed=True, **base, **end)

    def test_turns_that_used_tools_become_activity(self):
        self.turn(["vision_analyze", "vision_analyze", "memory"])
        row = self.store.activity()[0]
        self.assertEqual(row["request"], "Did the dog food arrive?")
        self.assertEqual(row["summary"], "Done.")
        self.assertEqual((row["category"], row["tools"], row["outcome"]), ("seeing", ["vision_analyze", "memory"], "done"))

    def test_plain_chat_and_subagents_are_not_activity(self):
        self.turn([])
        self.recorder.observe("pre_llm_call", session_id="child", turn_id="c", platform="subagent", user_message="x")
        self.assertEqual(self.store.activity(), [])

    def test_failed_and_stopped_turns_keep_their_outcome(self):
        self.turn(["terminal"], interrupted=True)
        self.assertEqual(self.store.activity()[0]["outcome"], "stopped")
        self.assertEqual(self.store.activity()[0]["category"], "coding")

    def test_approval_decisions_are_logged(self):
        self.recorder.observe("post_approval_response", session_id="s1", description="Delete a folder",
                              command="rm -rf build", choice="once")
        self.assertEqual(self.store.approvals()[0]["choice"], "once")

    def test_tool_categories(self):
        self.assertEqual(tool_category("image_generate"), "images")
        self.assertEqual(tool_category("browser_navigate"), "web")
        self.assertEqual(tool_category("execute_code"), "coding")
        self.assertEqual(tool_category("cronjob"), "scheduling")
        self.assertEqual(tool_category("mcp_linear_create_issue"), "tools")
