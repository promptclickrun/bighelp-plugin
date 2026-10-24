"""The Photo actions routes, with Hermes' auth middleware and real per-profile homes."""
from __future__ import annotations

import base64
import os
import unittest
import uuid

import test_native_api as native
from loopdy_plugin import image_drafts

PREFIX = native.PREFIX

CHAT = "20261024_101500_abc123"
OTHER_CHAT = "20261024_111500_def456"
DECISION = "0b6f2a8e-4c1d-4e9a-9f3b-2d7c5e8a1b40"
OTHER_DECISION = "7d3e9c1a-2b4f-4a8e-8c6d-1f0e9b8a7c65"
HASH = "c" * 64


class ImageDraftRouteTests(unittest.TestCase):
    context = native.NativeAPITests.context
    headers = native.NativeAPITests.headers

    def setUp(self):
        native.NativeAPITests.setUp(self)
        from hermes_state import SessionDB
        self.research = self.home / "profiles" / "research"
        self.db = SessionDB(db_path=self.research / "state.db")
        self.addCleanup(self.db.close)
        for session in (CHAT, OTHER_CHAT):
            self.db.create_session(session, "bighelp")
        self.store = image_drafts.store_for_home(self.research)
        (self.research / "images").mkdir()
        # Over two pieces, made-up bytes behind a JPEG header.
        self.bytes = b"\xff\xd8\xff\xe0" + os.urandom(image_drafts.MAX_CHUNK_BYTES * 2 + 1234)
        self.path = self.research / "images" / "upload_card.jpg"
        self.path.write_bytes(self.bytes)
        self.draft = self.new("d_" + "4" * 32)

    def new(self, draft_id: str, session: str = CHAT) -> str:
        picture = image_drafts.inspect_picture(str(self.path))
        assert picture is not None
        self.store.create(draft_id=draft_id, session_id=session, kind="contact_draft", picture=picture,
                          config_revision=1, label="Riley Moss", lineage=[session])
        return draft_id

    def route(self, path: str, body: dict, agent: str = "research"):
        return self.client.post(PREFIX + path, headers=self.headers(), json={"agentId": agent, **body})

    def drafts(self, operation: str, body: dict, agent: str = "research", session: str = CHAT):
        return self.route("/image-drafts/" + operation, {"sessionId": session, **body}, agent)

    def assert_error(self, response, status: int, code: str) -> dict:
        self.assertEqual(status, response.status_code, response.text)
        error = response.json()["error"]
        self.assertEqual(code, error["code"])
        return error["details"]

    def test_the_feature_is_advertised(self):
        self.assertIn("native-image-drafts-v1", self.context().json()["features"])

    def test_settings_get_save_conflict_and_reset(self):
        got = self.route("/image-actions/get", {})
        self.assertEqual(200, got.status_code, got.text)
        value = got.json()
        self.assertEqual(1, value["settings"]["revision"])
        self.assertIsNone(value["settings"]["presets"]["business_card"]["question"])
        self.assertEqual(image_drafts.DEFAULTS["event_poster"]["messages"]["saved"],
                         value["defaults"]["presets"]["event_poster"]["messages"]["saved"])
        settings = {"schemaVersion": 1, "presets": {"event_poster": {"offer": False}}}
        saved = self.route("/image-actions/save", {"expectedRevision": 1, "settings": settings})
        self.assertEqual(200, saved.status_code, saved.text)
        self.assertEqual((2, False), (saved.json()["settings"]["revision"],
                                      saved.json()["settings"]["presets"]["event_poster"]["offer"]))
        stale = self.route("/image-actions/save", {"expectedRevision": 1, "settings": {"schemaVersion": 1}})
        self.assertEqual({"revision": 2}, self.assert_error(stale, 409, "revision_conflict"))
        reset = self.route("/image-actions/save", {"expectedRevision": 2, "settings": {"schemaVersion": 1}})
        self.assertEqual(3, reset.json()["settings"]["revision"])
        self.assertIsNone(reset.json()["settings"]["presets"]["event_poster"]["offer"])
        bad = {"schemaVersion": 1, "presets": {"business_card": {"messages": {"review": "{{name}}"}}}}
        self.assert_error(self.route("/image-actions/save", {"expectedRevision": 3, "settings": bad}),
                          422, "invalid_settings")
        self.assert_error(self.route("/image-actions/save", {"expectedRevision": 3, "settings": {}, "x": 1}),
                          422, "invalid_request")
        self.assert_error(self.route("/image-actions/get", {}, agent="nobody"), 404, "profile_not_found")
        self.assert_error(self.route("/image-actions/delete", {}), 404, "operation_not_found")

    def test_each_agent_keeps_its_own_settings(self):  # F12
        settings = {"schemaVersion": 1, "presets": {"business_card": {"question": "A contact?"}}}
        self.assertEqual(200, self.route("/image-actions/save", {"expectedRevision": 1, "settings": settings},
                                         agent="research").status_code)
        default = self.route("/image-actions/get", {}, agent="default").json()["settings"]
        self.assertEqual(1, default["revision"])
        self.assertIsNone(default["presets"]["business_card"]["question"])
        self.assertEqual({"drafts": []}, self.drafts("states", {"draftIds": [self.draft]}, agent="default").json())

    def test_states_belong_to_the_chat_and_its_continuations(self):
        states = self.drafts("states", {"draftIds": [self.draft, "d_" + "5" * 32]})
        self.assertEqual(200, states.status_code, states.text)
        [view] = states.json()["drafts"]
        self.assertEqual(("awaiting_review", self.draft), (view["state"], view["draftId"]))
        self.assertRegex(view["updatedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual({"drafts": []}, self.drafts("states", {"draftIds": [self.draft]}, session=OTHER_CHAT).json())
        continued = "20261024_121500_cont01"
        self.db.create_session(continued, "bighelp", parent_session_id=CHAT)
        self.assertEqual(1, len(self.drafts("states", {"draftIds": [self.draft]}, session=continued).json()["drafts"]))
        self.assert_error(self.drafts("states", {"draftIds": []}), 422, "invalid_request")
        self.assert_error(self.drafts("states", {"draftIds": ["d_" + "6" * 32] * 51}), 422, "invalid_request")
        self.assert_error(self.drafts("states", {"draftIds": ["../d"]}), 422, "invalid_request")

    def test_the_source_comes_in_pieces_and_only_to_its_chat(self):
        pieces, offset = [], 0
        while True:
            response = self.drafts("source", {"draftId": self.draft, "offset": offset})
            self.assertEqual(200, response.status_code, response.text[:200])
            value = response.json()
            self.assertEqual(("image/jpeg", len(self.bytes), offset),
                             (value["mimeType"], value["totalBytes"], value["offset"]))
            self.assertNotIn(str(self.research), response.text)
            pieces.append(base64.b64decode(value["data"]))
            self.assertLessEqual(len(pieces[-1]), image_drafts.MAX_CHUNK_BYTES)
            if "nextOffset" not in value:
                break
            offset = value["nextOffset"]
        self.assertEqual(3, len(pieces))
        self.assertEqual(self.bytes, b"".join(pieces))
        self.assert_error(self.drafts("source", {"draftId": self.draft, "offset": len(self.bytes)}),
                          422, "invalid_request")
        self.assert_error(self.drafts("source", {"draftId": self.draft, "offset": 0}, session=OTHER_CHAT),
                          404, "draft_not_found")
        self.assert_error(self.drafts("source", {"draftId": self.draft, "offset": 0}, agent="default"),
                          404, "draft_not_found")
        self.path.write_bytes(self.bytes + b"changed")
        self.assert_error(self.drafts("source", {"draftId": self.draft, "offset": 0}), 404, "source_unavailable")

    def test_claim_finish_and_conflicts(self):
        claim = {"draftId": self.draft, "decisionId": DECISION, "fieldsHash": HASH, "destination": "iCloud"}
        first = self.drafts("claim", claim)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(("applying", DECISION, "iCloud"),
                         (first.json()["state"], first.json()["decisionId"], first.json()["destination"]))
        self.assertEqual(first.json(), self.drafts("claim", claim).json())
        details = self.assert_error(self.drafts("claim", {**claim, "decisionId": OTHER_DECISION}), 409, "draft_busy")
        self.assertEqual(("applying", DECISION), (details["state"], details["decisionId"]))
        self.assertEqual("applying", self.assert_error(self.drafts("discard", {"draftId": self.draft}),
                                                       409, "draft_busy")["state"])
        finish = {"draftId": self.draft, "decisionId": DECISION, "outcome": "saved", "verified": True}
        self.assert_error(self.drafts("finish", {**finish, "decisionId": OTHER_DECISION}), 409, "draft_busy")
        self.assert_error(self.drafts("finish", {**finish, "reason": "write_failed"}), 422, "invalid_request")
        self.assert_error(self.drafts("finish", {**finish, "outcome": "failed"}), 422, "invalid_request")
        done = self.drafts("finish", finish)
        self.assertEqual(200, done.status_code, done.text)
        self.assertEqual(("saved", "saved", True, "iCloud"), tuple(done.json()[key] for key in
                                                                  ("state", "outcome", "verified", "destination")))
        details = self.assert_error(self.drafts("claim", {**claim, "decisionId": OTHER_DECISION}),
                                    409, "draft_decided")
        self.assertEqual("saved", details["state"])
        self.assert_error(self.drafts("reopen", {"draftId": self.draft, "decisionId": DECISION}),
                          409, "draft_decided")
        self.assertEqual(['The person saved the contact draft for "Riley Moss" to "iCloud".'],
                         self.store.take_notes([CHAT]))

    def test_discard_reopen_and_ownership(self):
        other = self.new("d_" + "7" * 32)
        claim = {"draftId": other, "decisionId": DECISION, "fieldsHash": HASH, "destination": "Home"}
        self.assertEqual(200, self.drafts("claim", claim).status_code)
        self.assert_error(self.drafts("reopen", {"draftId": other, "decisionId": OTHER_DECISION}), 409, "draft_busy")
        reopened = self.drafts("reopen", {"draftId": other, "decisionId": DECISION})
        self.assertEqual("awaiting_review", reopened.json()["state"])
        self.assertEqual("discarded", self.drafts("discard", {"draftId": self.draft}).json()["state"])
        self.assertEqual("discarded", self.drafts("discard", {"draftId": self.draft}).json()["state"])
        for operation, body in (("discard", {"draftId": other}),
                                ("claim", claim),
                                ("reopen", {"draftId": other, "decisionId": DECISION}),
                                ("finish", {"draftId": other, "decisionId": DECISION, "outcome": "saved"})):
            with self.subTest(operation=operation):
                self.assert_error(self.drafts(operation, body, session=OTHER_CHAT), 404, "draft_not_found")
                self.assert_error(self.drafts(operation, body, agent="default"), 404, "draft_not_found")
        self.assert_error(self.drafts("claim", {**claim, "decisionId": DECISION.upper()}), 422, "invalid_request")
        self.assert_error(self.drafts("claim", {**claim, "destination": "d" * 101}), 422, "invalid_request")
        self.assert_error(self.drafts("claim", {**claim, "fieldsHash": "nothex"}), 422, "invalid_request")

    def test_requests_need_the_context_and_a_request_id(self):
        headers = self.headers()
        headers["X-Loopdy-Request-ID"] = str(uuid.uuid4()).upper()
        response = self.client.post(PREFIX + "/image-drafts/states", headers=headers,
                                    json={"agentId": "research", "sessionId": CHAT, "draftIds": [self.draft]})
        self.assert_error(response, 422, "invalid_request")
        headers = self.headers()
        del headers["If-Match"]
        response = self.client.post(PREFIX + "/image-actions/get", headers=headers, json={"agentId": "research"})
        self.assertEqual(428, response.status_code)


if __name__ == "__main__":
    unittest.main()
