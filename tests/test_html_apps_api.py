"""The html-apps routes, with Hermes' auth middleware and real per-profile homes."""
from __future__ import annotations

import base64
import hashlib
import json
import unittest
import uuid

import yaml

import test_native_api as native
from loopdy_plugin import html_apps

PREFIX = native.PREFIX
CHAT = "20261107_101500_abc123"
PAGE = "<!doctype html><title>Trip</title><p>héllo</p>"


class HtmlAppRouteTests(unittest.TestCase):
    context = native.NativeAPITests.context
    headers = native.NativeAPITests.headers

    def setUp(self):
        native.NativeAPITests.setUp(self)
        self.research = self.home / "profiles" / "research"
        self.work = self.home / "research-work"
        self.work.mkdir()
        (self.research / "config.yaml").write_text(yaml.safe_dump({"terminal": {"cwd": str(self.work)}}))
        self.store = html_apps.store_for_home(self.research)
        self.page = PAGE.encode()
        self.app_id = self.new("a_" + "1" * 32)

    def new(self, app_id: str, store=None, **extra) -> str:
        (store or self.store).save(app_id, replace=False, session_id=CHAT, title="Trip planner",
                                   description="Plan a road trip.", sends_answers=True, html=self.page, **extra)
        return app_id

    def route(self, operation: str, body: dict, agent: str = "research", **options):
        return self.client.post(PREFIX + "/html-apps/" + operation, headers=self.headers(),
                                json={"agentId": agent, **body}, **options)

    def assert_error(self, response, status: int, code: str) -> None:
        self.assertEqual(status, response.status_code, response.text)
        self.assertEqual(code, response.json()["error"]["code"])

    def test_the_feature_is_advertised(self):
        self.assertIn("native-html-apps-v1", self.context().json()["features"])

    def test_get_answers_the_app_and_its_page(self):
        response = self.route("get", {"appId": self.app_id})
        self.assertEqual(200, response.status_code, response.text)
        value = response.json()
        digest = hashlib.sha256(self.page).hexdigest()
        self.assertEqual({"app", "html"}, set(value))
        self.assertEqual({"appId", "title", "description", "sendsAnswers", "live", "revision", "updatedAt",
                          "htmlSha256", "htmlBytes"}, set(value["app"]))
        app = value["app"]
        self.assertEqual((self.app_id, "Trip planner", "Plan a road trip.", True, False, 1, digest, len(self.page)),
                         (app["appId"], app["title"], app["description"], app["sendsAnswers"], app["live"],
                          app["revision"], app["htmlSha256"], app["htmlBytes"]))
        self.assertRegex(app["updatedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.page, base64.b64decode(value["html"], validate=True))
        self.assertEqual("no-store", response.headers["cache-control"])

    def test_the_page_is_left_out_when_the_phone_already_has_it(self):
        digest = hashlib.sha256(self.page).hexdigest()
        same = self.route("get", {"appId": self.app_id, "knownSha256": digest})
        self.assertEqual(200, same.status_code, same.text)
        self.assertEqual({"app"}, set(same.json()))
        self.assertEqual(digest, same.json()["app"]["htmlSha256"])
        other = self.route("get", {"appId": self.app_id, "knownSha256": "0" * 64})
        self.assertIn("html", other.json())
        # A replacement is a new sha: the phone with the old one gets the new page.
        self.store.save(self.app_id, replace=True, session_id=CHAT, title="Trip planner", description="D",
                        sends_answers=False, html=b"<p>v2</p>")
        newer = self.route("get", {"appId": self.app_id, "knownSha256": digest}).json()
        self.assertEqual((2, b"<p>v2</p>"), (newer["app"]["revision"], base64.b64decode(newer["html"])))

    def test_a_page_of_2_mib_fits_the_answer(self):
        big = self.new("a_" + "2" * 32)
        self.store.save(big, replace=True, session_id=CHAT, title="Big", description="D", sends_answers=False,
                        html="é".encode() * 1_048_576)
        response = self.route("get", {"appId": big})
        self.assertEqual(200, response.status_code, response.text[:200])
        self.assertEqual(2_097_152, response.json()["app"]["htmlBytes"])

    def test_an_unknown_app_or_another_agents_app_is_not_found(self):
        self.assert_error(self.route("get", {"appId": "a_" + "9" * 32}), 404, "app_not_found")
        self.assert_error(self.route("data", {"appId": "a_" + "9" * 32}), 404, "app_not_found")
        mine = html_apps.store_for_home(self.home)
        theirs = self.new("a_" + "3" * 32, store=mine)
        for operation in ("get", "data"):
            with self.subTest(operation=operation):
                self.assert_error(self.route(operation, {"appId": theirs}), 404, "app_not_found")
                self.assertEqual(200, self.route(operation, {"appId": theirs}, agent="default").status_code)

    def test_an_unknown_agent_is_not_found(self):
        self.assert_error(self.route("get", {"appId": self.app_id}, agent="nobody"), 404, "profile_not_found")

    def test_a_bad_body_is_a_400_invalid_request(self):
        bad = {
            "get": [{}, {"appId": "a_123"}, {"appId": "A_" + "1" * 32}, {"appId": 5},
                    {"appId": self.app_id, "extra": 1}, {"appId": self.app_id, "knownSha256": "ABC"}],
            # Only get takes knownSha256.
            "data": [{}, {"appId": "a_123"}, {"appId": "A_" + "1" * 32}, {"appId": 5},
                     {"appId": self.app_id, "extra": 1}, {"appId": self.app_id, "knownSha256": "0" * 64}],
        }
        for operation, bodies in bad.items():
            for body in bodies:
                with self.subTest(operation=operation, body=body):
                    self.assert_error(self.route(operation, body), 400, "invalid_request")
        self.assert_error(self.route("get", {"appId": self.app_id}, agent="Bad Name"), 400, "invalid_request")
        notjson = self.client.post(PREFIX + "/html-apps/get", headers={**self.headers(),
                                   "Content-Type": "application/json"}, content=b"not json")
        self.assert_error(notjson, 400, "invalid_request")

    def test_the_usual_route_checks_apply(self):
        url = PREFIX + "/html-apps/get"
        body = {"agentId": "research", "appId": self.app_id}
        headers = self.headers()
        self.assert_error(self.client.post(url, headers={k: v for k, v in headers.items() if k != "If-Match"},
                                           json=body), 428, "context_required")
        self.assert_error(self.client.post(url, headers={**headers, "If-Match": '"sha256:stale"'}, json=body),
                          412, "context_changed")
        self.assert_error(self.client.post(url, headers={**headers, "X-Loopdy-Request-ID": "nope"}, json=body),
                          422, "invalid_request")
        self.assertEqual(401, self.client.post(url, json=body).status_code)
        self.assert_error(self.route("nonsense", {"appId": self.app_id}), 404, "operation_not_found")
        sent = str(uuid.uuid4())
        echoed = self.client.post(url, headers={**headers, "X-Loopdy-Request-ID": sent}, json=body)
        self.assertEqual(sent, echoed.headers["x-loopdy-request-id"])

    def test_data_of_an_app_without_any(self):
        self.assertEqual({"appId": self.app_id, "data": None, "updatedAt": None, "dataSha256": None,
                          "problem": None}, self.route("data", {"appId": self.app_id}).json())

    def test_inline_data_keeps_the_order_the_agent_wrote(self):
        self.store.set_inline_data(self.app_id, '{"b":1,"a":[true,null,"é"]}')
        response = self.route("data", {"appId": self.app_id})
        self.assertEqual(200, response.status_code, response.text)
        self.assertIn('"data":{"b":1,"a":[true,null,"é"]}', response.text)
        value = response.json()
        self.assertEqual(hashlib.sha256('{"b":1,"a":[true,null,"é"]}'.encode()).hexdigest(), value["dataSha256"])
        self.assertRegex(value["updatedAt"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIsNone(value["problem"])

    def test_a_data_file_is_read_again_on_every_request(self):
        app_id = self.new("a_" + "4" * 32, data_path="live.json")
        path = self.work / "live.json"
        self.assertEqual("missing", self.route("data", {"appId": app_id}).json()["problem"])
        path.write_text('{"n": 1}')
        first = self.route("data", {"appId": app_id}).json()
        self.assertEqual(({"n": 1}, None), (first["data"], first["problem"]))
        path.write_text('{"n": 2}')
        second = self.route("data", {"appId": app_id}).json()
        self.assertEqual({"n": 2}, second["data"])
        self.assertNotEqual(first["dataSha256"], second["dataSha256"])
        path.write_text("{oops")
        broken = self.route("data", {"appId": app_id}).json()
        self.assertEqual((None, None, None, "invalid"), (broken["data"], broken["updatedAt"], broken["dataSha256"],
                                                          broken["problem"]))
        path.write_text("[" + "1," * 200_000 + "1]")
        self.assertEqual("too_large", self.route("data", {"appId": app_id}).json()["problem"])

    def test_data_up_to_the_limit_fits_the_answer(self):
        app_id = self.new("a_" + "5" * 32, data_path="live.json")
        (self.work / "live.json").write_text(json.dumps(["x" * (html_apps.MAX_DATA_BYTES - 4)],
                                                        separators=(",", ":")))
        response = self.route("data", {"appId": app_id})
        self.assertEqual(200, response.status_code, response.text[:200])
        self.assertIsNone(response.json()["problem"])

    def test_a_data_file_is_only_read_from_the_agents_own_folder(self):
        app_id = self.new("a_" + "6" * 32, data_path="live.json")
        outside = self.home / "elsewhere.json"
        outside.write_text("[1]")
        (self.work / "live.json").symlink_to(outside)
        self.assertEqual("missing", self.route("data", {"appId": app_id}).json()["problem"])
        other = self.new("a_" + "7" * 32, data_path=str(outside))
        response = self.route("data", {"appId": other}).json()
        self.assertEqual(("missing", None), (response["problem"], response["data"]))


if __name__ == "__main__":
    unittest.main()
