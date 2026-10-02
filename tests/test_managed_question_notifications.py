from __future__ import annotations

import json
import sqlite3
import unittest
import uuid

from typing import Any
from loopdy_plugin.managed_notifications import ManagedNotifications
if __package__:
    from . import test_managed_notifications as fixtures
else:
    import test_managed_notifications as fixtures


class ManagedQuestionNotificationTests(unittest.TestCase):
    """A question the agent asks with the clarify tool alerts the phone.

    Chats in the bighelp app run on the dashboard, where the messaging adapter
    never presents the question, so those questions used to send no alert.
    """

    service: ManagedNotifications
    grant: dict[str, Any]
    calls: list[tuple[str, str, bytes, dict[str, str]]]
    now: int
    phone_public: str
    setUp = fixtures.ManagedNotificationTests.setUp
    tearDown = fixtures.ManagedNotificationTests.tearDown
    get_session = fixtures.ManagedNotificationTests.get_session
    transport = fixtures.ManagedNotificationTests.transport

    def enroll(self, event_types: list[str] | None = None):
        self.grant_id = str(uuid.uuid4())
        self.grant = dict(self.grant, grantId=self.grant_id, eventTypes=event_types or [
            "session.completed", "session.failed", "approval.required", "clarification.required"])
        self.service.enroll(self.grant_id, str(uuid.uuid4()))
        self.service.register_recipient(self.grant_id, self.phone_public)
        self.service.subscribe(self.grant_id, "default", "native-session", True)
        self.service.producer_loaded("default", start_worker=False, clarification_producer_loaded=True)
        self.calls.clear()

    def ask(self, args: Any, *, tool: str = "clarify", call: str = "toolu_01question", **changes: Any):
        payload = dict(profile="default", session_id="native-session", turn_id="turn-a",
                       tool_call_id=call, tool_name=tool, args=args)
        payload.update(changes)
        self.service.observe("pre_tool_call", **payload)

    def pending(self):
        with sqlite3.connect(self.service.db_path) as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT raw FROM pending WHERE state='pending' AND grant_id=? AND path='/events'",
                (self.grant_id,))]

    def detail(self, row: dict[str, Any]) -> dict[str, Any]:
        return self.service.event(self.grant_id, row["eventId"])["event"]

    def test_a_question_in_an_app_chat_alerts_once(self):
        self.enroll()
        self.ask({"question": "Which city should I book?", "choices": ["Austin", "Denver"]})
        self.ask({"question": "Which city should I book?", "choices": ["Austin", "Denver"]})
        rows = self.pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["eventType"], "clarification.required")
        detail = self.detail(rows[0])
        self.assertEqual(detail["content"], {"kind": "clarification", "text": "Which city should I book?"})
        self.assertEqual((detail["sessionId"], detail["turnId"]), ("native-session", "turn-a"))

    def test_each_question_in_a_turn_alerts(self):
        self.enroll()
        self.ask({"question": "First?"}, call="toolu_01first")
        self.ask({"question": "Second?"}, call="toolu_01second")
        self.assertEqual([self.detail(row)["content"]["text"] for row in self.pending()], ["First?", "Second?"])

    def test_a_batch_leads_with_its_first_question(self):
        self.enroll()
        self.ask({"questions": [{"question": "Which city?"}, {"question": "Which dates?"}, "Budget?"]})
        self.assertEqual(self.detail(self.pending()[0])["content"]["text"], "Which city? (+2 more)")

    def test_other_tools_and_empty_questions_send_nothing(self):
        self.enroll()
        self.ask({"command": "ls"}, tool="terminal")
        self.ask({"question": "   "})
        self.ask("not arguments")
        self.ask({"questions": []})
        self.ask({"question": "Which city?"}, turn_id="")
        self.ask({"question": "Which city?"}, call="")
        self.assertEqual(self.pending(), [])

    def test_a_grant_without_questions_sends_nothing(self):
        self.enroll(["session.completed", "session.failed"])
        self.ask({"question": "Which city should I book?"})
        self.assertEqual(self.pending(), [])

    def test_a_subagents_question_belongs_to_its_parent_and_sends_nothing_itself(self):
        self.enroll()
        self.ask({"question": "Which city?"}, parent_session_id="native-session", session_id="child-session")
        self.assertEqual(self.pending(), [])

    def test_the_messaging_adapter_does_not_alert_the_same_question_again(self):
        self.enroll()
        self.ask({"question": "Which city should I book?"})
        self.service.publish_clarification(profile="default", session_id="native-session", turn_id="turn-a",
                                           request_id="clarify-1", question="Which city should I book?")
        self.assertEqual(len(self.pending()), 1)


if __name__ == "__main__":
    unittest.main()
