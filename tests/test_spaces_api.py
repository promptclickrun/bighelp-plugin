"""The spaces routes (docs/SPACES.md), with Hermes' auth middleware and the spaces module's functions faked.

The routes only parse, check, dispatch and shape answers; what a space does is tested in test_spaces.py.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

import yaml

import test_native_api as native
from loopdy_plugin import native_api, registration, spaces
from loopdy_plugin.native_context import LATE_FEATURES

PREFIX = native.PREFIX
FEATURE = "native-spaces-v1"
SPACE = "s_" + "1" * 32
OTHER = "s_" + "2" * 32
OPERATION = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
DIGEST = "ab" * 32


def space_object(space_id: str = SPACE, agent: str = "research", **extra) -> dict:
    return {"spaceId": space_id, "agentId": agent, "state": "live", "title": "Commute",
            "description": "Drive time to work.", "size": "wide", "hidden": False, "position": 1024.0,
            "revision": 1, "cardsSha256": DIGEST, "card": {"schema": "loopdy.card"}, "cardOmitted": False,
            "updatedAt": "2026-11-12T14:02:00Z", "checkedAt": "2026-11-12T14:15:01Z",
            "page": {"appId": "a_" + "3" * 32, "htmlSha256": DIGEST, "live": True},
            "routine": {"kind": "script", "usesAI": False, "updates": "Every 15 minutes", "cadenceMinutes": 15,
                        "state": "running", "nextRunAt": None, "lastRunAt": None, "problem": None,
                        "pendingChange": False},
            "status": "fresh", **extra}


class SpacesRouteTests(unittest.TestCase):
    context = native.NativeAPITests.context
    headers = native.NativeAPITests.headers

    def setUp(self):
        native.NativeAPITests.setUp(self)
        self.fakes = {}
        for name, value in (("get_space", space_object()), ("add", space_object()),
                            ("update", space_object()), ("list_spaces", []),
                            ("refresh", {"space": space_object(), "ran": "now"}),
                            ("delete", {"spaceId": SPACE, "deleted": True, "existed": True})):
            patcher = patch.object(spaces, name, Mock(return_value=value))
            self.fakes[name] = patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(spaces, "available", Mock(return_value=True))
        self.available = patcher.start()
        self.addCleanup(patcher.stop)

    def route(self, operation: str, body: dict, agent: str = "research", **options):
        return self.client.post(PREFIX + "/spaces/" + operation, headers=self.headers(),
                                json={"agentId": agent, **body}, **options)

    def mutation(self, operation: str, body: dict | None = None, **options):
        return self.route(operation, {"spaceId": SPACE, "operationId": OPERATION, **(body or {})}, **options)

    def assert_error(self, response, status: int, code: str) -> dict:
        self.assertEqual(status, response.status_code, response.text)
        self.assertEqual(code, response.json()["error"]["code"])
        return response.json()["error"]

    # MARK: The feature

    def test_the_feature_is_advertised_last(self):
        full = self.client.get(PREFIX + "/context", headers={"Authorization": "Bearer fixture-alice",
                                                             "X-Bighelp-Features": "all"})
        self.assertIn(FEATURE, full.json()["features"])
        self.assertIn(FEATURE, LATE_FEATURES)
        self.assertEqual(FEATURE, full.json()["features"][-1])

    def test_a_host_without_scheduled_tasks_leaves_the_feature_out_and_refuses_the_routes(self):
        self.available.return_value = False
        self.assertNotIn(FEATURE, self.context().json()["features"])
        error = self.assert_error(self.route("get", {"spaceId": SPACE}), 503, "spaces_unavailable")
        self.assertTrue(error["retryable"])
        self.fakes["get_space"].assert_not_called()

    # MARK: The usual checks

    def test_the_usual_route_checks_apply(self):
        url = PREFIX + "/spaces/get"
        body = {"agentId": "research", "spaceId": SPACE}
        headers = self.headers()
        self.assert_error(self.client.post(url, headers={k: v for k, v in headers.items() if k != "If-Match"},
                                           json=body), 428, "context_required")
        self.assert_error(self.client.post(url, headers={**headers, "If-Match": '"sha256:stale"'}, json=body),
                          412, "context_changed")
        for bad in ("nope", "0A1B2C3D-4E5F-4A6B-8C7D-9E0F1A2B3C4D"):
            self.assert_error(self.client.post(url, headers={**headers, "X-Loopdy-Request-ID": bad}, json=body),
                              422, "invalid_request")
        self.assertEqual(401, self.client.post(url, json=body).status_code)
        self.assert_error(self.route("nonsense", {"spaceId": SPACE}), 404, "operation_not_found")
        sent = str(uuid.uuid4())
        echoed = self.client.post(url, headers={**headers, "X-Loopdy-Request-ID": sent}, json=body)
        self.assertEqual(200, echoed.status_code, echoed.text)
        self.assertEqual(sent, echoed.headers["x-loopdy-request-id"])
        self.assertEqual("no-store", echoed.headers["cache-control"])
        self.fakes["get_space"].assert_called_once()

    def test_a_request_that_is_not_json_is_a_400(self):
        for content, kind in ((b"not json", "application/json"), (b"{}", "text/plain")):
            with self.subTest(kind=kind):
                response = self.client.post(PREFIX + "/spaces/get", headers={**self.headers(), "Content-Type": kind},
                                            content=content)
                self.assert_error(response, 400, "invalid_request")
        duplicate = self.client.post(PREFIX + "/spaces/get", headers={**self.headers(), "Content-Type": "application/json"},
                                     content=b'{"agentId":"a","agentId":"b","spaceId":"' + SPACE.encode() + b'"}')
        self.assert_error(duplicate, 400, "invalid_request")
        self.fakes["get_space"].assert_not_called()

    # MARK: Answers

    def test_get_answers_the_space_even_for_a_draft(self):
        self.fakes["get_space"].return_value = space_object(state="draft", status="draft")
        response = self.route("get", {"spaceId": SPACE})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({"space"}, set(response.json()))
        self.assertEqual(("draft", SPACE), (response.json()["space"]["state"], response.json()["space"]["spaceId"]))
        self.fakes["get_space"].assert_called_once_with("research", SPACE)

    def test_add_passes_the_draft_revision_the_retry_key_and_the_choices(self):
        response = self.mutation("add", {"draftRevision": 3, "size": "small", "position": 4096})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({"space"}, set(response.json()))
        self.fakes["add"].assert_called_once_with("research", SPACE, 3, OPERATION, size="small", position=4096.0)
        self.fakes["add"].reset_mock()
        self.mutation("add", {"draftRevision": 1})
        self.fakes["add"].assert_called_once_with("research", SPACE, 1, OPERATION, size=None, position=None)

    def test_update_passes_only_the_changes_that_were_sent(self):
        response = self.mutation("update", {"changes": {"hidden": True, "position": -2.5}})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({"space"}, set(response.json()))
        self.fakes["update"].assert_called_once_with("research", SPACE, OPERATION, {"hidden": True, "position": -2.5})
        self.fakes["update"].reset_mock()
        self.mutation("update", {"changes": {"size": "tall", "title": "Drive time", "hidden": False}})
        self.fakes["update"].assert_called_once_with("research", SPACE, OPERATION,
                                                     {"size": "tall", "title": "Drive time", "hidden": False})

    def test_refresh_and_delete_answer_what_the_module_answers(self):
        refresh = self.mutation("refresh")
        self.assertEqual(200, refresh.status_code, refresh.text)
        self.assertEqual({"space", "ran"}, set(refresh.json()))
        self.assertEqual("now", refresh.json()["ran"])
        self.fakes["refresh"].assert_called_once_with("research", SPACE, OPERATION)
        gone = self.mutation("delete")
        self.assertEqual({"spaceId": SPACE, "deleted": True, "existed": True}, gone.json())
        self.fakes["delete"].assert_called_once_with("research", SPACE, OPERATION)

    def test_every_operation_runs_off_the_event_loop(self):
        # A refresh runs the routine's script (up to 50 seconds): nothing may block the loop meanwhile.
        seen = {}

        def probe(name, value):
            def run(*args, **kwargs):
                try:
                    asyncio.get_running_loop()
                    seen[name] = "event loop"
                except RuntimeError:
                    seen[name] = "worker thread"
                return value
            return run

        self.fakes["get_space"].side_effect = probe("get", space_object())
        self.fakes["add"].side_effect = probe("add", space_object())
        self.fakes["update"].side_effect = probe("update", space_object())
        self.fakes["refresh"].side_effect = probe("refresh", {"space": space_object(), "ran": "now"})
        self.fakes["delete"].side_effect = probe("delete", {"spaceId": SPACE, "deleted": True, "existed": False})
        self.fakes["list_spaces"].side_effect = probe("list", [])
        self.route("get", {"spaceId": SPACE})
        self.mutation("add", {"draftRevision": 1})
        self.mutation("update", {"changes": {"hidden": True}})
        self.mutation("refresh")
        self.mutation("delete")
        self.client.post(PREFIX + "/spaces/list", headers=self.headers(), json={"agentIds": ["research"]})
        self.assertEqual({name: "worker thread" for name in ("get", "add", "update", "refresh", "delete", "list")}, seen)

    def test_the_context_is_checked_again_after_the_work(self):
        def change_context(*args, **kwargs):
            self.available.return_value = False
            return {"spaceId": SPACE, "deleted": True, "existed": True}

        self.fakes["delete"].side_effect = change_context
        self.assert_error(self.mutation("delete"), 412, "context_changed")
        self.fakes["delete"].assert_called_once()

    # MARK: Errors from the module

    def test_each_space_error_keeps_its_status_code_and_details(self):
        cases = (
            (400, "invalid_request", {}), (404, "profile_not_found", {}), (404, "space_not_found", {}),
            (409, "draft_changed", {"revision": 4}), (409, "space_not_draft", {}), (409, "spaces_full", {}),
            (429, "refresh_too_soon", {"retryAfter": 41}), (503, "schedules_unavailable", {}),
            (502, "routine_failed", {"space": space_object(status="stopped")}),
        )
        for status, code, details in cases:
            with self.subTest(code=code):
                self.fakes["refresh"].side_effect = spaces.SpaceError(status, code, "Words for people.", details)
                error = self.assert_error(self.mutation("refresh"), status, code)
                self.assertEqual("Words for people.", error["message"])
                self.assertEqual(details, error["details"])
                self.assertEqual(status == 503, error["retryable"])

    def test_every_operation_turns_a_space_error_into_the_envelope(self):
        failure = spaces.SpaceError(404, "space_not_found", "That space isn't there.", {})
        for name in ("get_space", "add", "update", "refresh", "delete"):
            self.fakes[name].side_effect = failure
        self.assert_error(self.route("get", {"spaceId": SPACE}), 404, "space_not_found")
        self.assert_error(self.mutation("add", {"draftRevision": 1}), 404, "space_not_found")
        self.assert_error(self.mutation("update", {"changes": {"hidden": True}}), 404, "space_not_found")
        self.assert_error(self.mutation("refresh"), 404, "space_not_found")
        self.assert_error(self.mutation("delete"), 404, "space_not_found")

    def test_an_unexpected_failure_is_a_generic_503_without_its_text(self):
        self.fakes["get_space"].side_effect = RuntimeError("/private/path/secret")
        response = self.route("get", {"spaceId": SPACE})
        self.assert_error(response, 503, "native_service_unavailable")
        self.assertNotIn("secret", response.text)

    def test_the_answer_is_capped_at_512_kib_except_the_list(self):
        self.fakes["get_space"].return_value = space_object(card={"filler": "x" * native_api.SPACES_RESPONSE_BYTES})
        self.assert_error(self.route("get", {"spaceId": SPACE}), 413, "payload_too_large")

    # MARK: Strict bodies

    def test_a_bad_body_is_a_400_invalid_request_and_calls_nothing(self):
        good = {"spaceId": SPACE}
        mutation = {**good, "operationId": OPERATION}
        add = {**mutation, "draftRevision": 1}
        update = {**mutation, "changes": {"hidden": True}}
        bad = {
            "get": [{}, {"spaceId": "s_123"}, {"spaceId": "S_" + "1" * 32}, {"spaceId": "s_" + "A" * 32},
                    {"spaceId": 5}, {**good, "extra": 1}, {**good, "operationId": OPERATION}],
            "add": [mutation, {**add, "draftRevision": 0}, {**add, "draftRevision": "1"},
                    {**add, "draftRevision": True}, {**add, "draftRevision": 1.5}, {**add, "size": "huge"},
                    {**add, "size": None}, {**add, "position": "1"}, {**add, "position": True},
                    {**add, "position": 1e13}, {**add, "position": None}, {**add, "extra": 1},
                    {**add, "operationId": OPERATION.upper()}, {**add, "operationId": "not-a-uuid"},
                    {"draftRevision": 1, "operationId": OPERATION}],
            "update": [good, {**mutation}, {**update, "changes": {}}, {**update, "changes": []},
                       {**update, "changes": {"size": "x"}}, {**update, "changes": {"size": None}},
                       {**update, "changes": {"hidden": 1}}, {**update, "changes": {"hidden": "true"}},
                       {**update, "changes": {"title": ""}}, {**update, "changes": {"title": "x" * 61}},
                       {**update, "changes": {"title": 5}}, {**update, "changes": {"position": True}},
                       {**update, "changes": {"position": -1e13}}, {**update, "changes": {"extra": 1}},
                       {**update, "extra": 1}, {**update, "changes": {"hidden": None}}],
            "refresh": [good, {**mutation, "extra": 1}, {**mutation, "operationId": 7}],
            "delete": [good, {**mutation, "extra": 1}, {**mutation, "spaceId": "s_1"}],
        }
        for operation, bodies in bad.items():
            for body in bodies:
                with self.subTest(operation=operation, body=body):
                    self.assert_error(self.route(operation, body), 400, "invalid_request")
        for operation in ("get", "add", "update", "refresh", "delete"):
            with self.subTest(operation=operation, agentId="Bad Name"):
                self.assert_error(self.route(operation, {**mutation, "draftRevision": 1, "changes": {"hidden": True}},
                                             agent="Bad Name"), 400, "invalid_request")
        for fake in self.fakes.values():
            fake.assert_not_called()

    def test_every_mutation_needs_an_operation_id(self):
        for operation, extra in (("add", {"draftRevision": 1}), ("update", {"changes": {"hidden": True}}),
                                 ("refresh", {}), ("delete", {})):
            with self.subTest(operation=operation):
                self.assert_error(self.route(operation, {"spaceId": SPACE, **extra}), 400, "invalid_request")

    def test_positions_up_to_a_trillion_are_accepted_as_numbers(self):
        for position in (0, 3072, -1e12, 1e12, 0.5):
            with self.subTest(position=position):
                self.assertEqual(200, self.mutation("update", {"changes": {"position": position}}).status_code)

    # MARK: list

    def test_list_reads_each_agent_and_says_which_were_unknown(self):
        def listed(agent, known, budget):
            if agent == "nova":
                raise spaces.SpaceError(404, "profile_not_found", "No such agent.", {})
            if agent == "broken":
                raise spaces.SpaceError(503, "schedules_unavailable", "No scheduled tasks.", {})
            if agent == "locked":
                raise sqlite3.OperationalError("database is locked: /made/up/path")
            return [space_object(SPACE, agent), space_object(OTHER, agent, cardOmitted=True)]

        self.fakes["list_spaces"].side_effect = listed
        with self.assertLogs("loopdy_plugin.native_api", "WARNING") as logged:
            response = self.client.post(PREFIX + "/spaces/list", headers=self.headers(),
                                        json={"agentIds": ["research", "nova", "broken", "locked"]})
        self.assertEqual(200, response.status_code, response.text)
        value = response.json()
        self.assertEqual({"spaces", "agents"}, set(value))
        self.assertEqual({"research": "ok", "nova": "profile_not_found", "broken": "unavailable",
                          "locked": "unavailable"}, value["agents"])
        self.assertNotIn("/made/up/path", "\n".join(logged.output))
        self.assertIn("OperationalError", "\n".join(logged.output))
        self.assertEqual([(SPACE, "research", False), (OTHER, "research", True)],
                         [(item["spaceId"], item["agentId"], item["cardOmitted"]) for item in value["spaces"]])

    def test_list_gives_each_agent_only_the_hashes_the_app_sent_for_it(self):
        known = {"research/" + SPACE: DIGEST, "nova/" + OTHER: "cd" * 32}
        response = self.client.post(PREFIX + "/spaces/list", headers=self.headers(),
                                    json={"agentIds": ["research", "nova", "quiet"], "known": known})
        self.assertEqual(200, response.status_code, response.text)
        calls = {call.args[0]: call.args[1] for call in self.fakes["list_spaces"].call_args_list}
        self.assertEqual({"research": {SPACE: DIGEST}, "nova": {OTHER: "cd" * 32}, "quiet": {}}, calls)

    def test_list_hands_the_rest_of_a_3_5_mib_budget_to_each_next_agent(self):
        big = space_object(card={"filler": "x" * 1_000_000})
        self.fakes["list_spaces"].return_value = [big]
        response = self.client.post(PREFIX + "/spaces/list", headers=self.headers(),
                                    json={"agentIds": ["research", "nova", "quiet"]})
        self.assertEqual(200, response.status_code, response.text)
        budgets = [call.args[2] for call in self.fakes["list_spaces"].call_args_list]
        self.assertEqual(native_api.SPACES_LIST_BUDGET, budgets[0])
        self.assertEqual(3_670_016, budgets[0])
        self.assertTrue(budgets[0] > budgets[1] > budgets[2], budgets)
        self.assertGreaterEqual(budgets[0] - budgets[1], 1_000_000)

    def test_list_answers_up_to_4_mib_and_refuses_more(self):
        self.fakes["list_spaces"].return_value = [space_object(card={"filler": "x" * 3_900_000})]
        fits = self.client.post(PREFIX + "/spaces/list", headers=self.headers(), json={"agentIds": ["research"]})
        self.assertEqual(200, fits.status_code, fits.text[:200])
        self.assertGreater(len(fits.content), native_api.SPACES_RESPONSE_BYTES)
        self.fakes["list_spaces"].return_value = [space_object(card={"filler": "x" * 4_300_000})]
        self.assert_error(self.client.post(PREFIX + "/spaces/list", headers=self.headers(),
                                           json={"agentIds": ["research"]}), 413, "payload_too_large")

    def test_a_bad_list_body_is_a_400(self):
        good = {"agentIds": ["research"]}
        key = "research/" + SPACE
        too_many = {f"a{index}/s_{index:032x}": DIGEST for index in range(513)}
        bad = [{}, {"agentIds": []}, {"agentIds": ["a%d" % index for index in range(65)]},
               {"agentIds": ["research", "research"]}, {"agentIds": ["Bad Name"]}, {"agentIds": "research"},
               {"agentIds": [5]}, {**good, "extra": 1}, {**good, "agentId": "research"},
               {**good, "known": []}, {**good, "known": {"research": DIGEST}},
               {**good, "known": {SPACE: DIGEST}}, {**good, "known": {"Research/" + SPACE: DIGEST}},
               {**good, "known": {key: "ABC"}}, {**good, "known": {key: DIGEST.upper()}},
               {**good, "known": {key: 5}}, {**good, "known": {key: None}}, {**good, "known": too_many}]
        for body in bad:
            with self.subTest(body=str(body)[:80]):
                self.assert_error(self.client.post(PREFIX + "/spaces/list", headers=self.headers(), json=body),
                                  400, "invalid_request")
        self.fakes["list_spaces"].assert_not_called()
        sixty_four = {"agentIds": ["a%d" % index for index in range(64)], "known": {}}
        self.assertEqual(200, self.client.post(PREFIX + "/spaces/list", headers=self.headers(), json=sixty_four).status_code)
        five_twelve = {"agentIds": ["research"], "known": dict(list(too_many.items())[:512])}
        self.assertEqual(200, self.client.post(PREFIX + "/spaces/list", headers=self.headers(), json=five_twelve).status_code)


def metric_card(value: int) -> dict:
    return {"schema": "bighelp.card", "version": 1, "title": "Commute", "spoken_summary": f"{value} minutes.",
            "data_sources": [], "root": "m", "elements": {"m": {
                "type": "metric", "props": {"label": "Drive", "value": {"literal": value}}, "children": []}}}


# Counts its own runs in a file next to it, so a test can see how often Hermes or a refresh ran it.
COLLECTOR = ("import json, pathlib, sys\n"
             "json.load(sys.stdin)\n"
             "path = pathlib.Path('runs.txt')\n"
             "runs = int(path.read_text()) + 1 if path.exists() else 1\n"
             "path.write_text(str(runs))\n"
             "card = json.loads(" + repr(json.dumps(metric_card(0))) + ")\n"
             "card['elements']['m']['props']['value']['literal'] = 100 + runs\n"
             "print(json.dumps({'card': card, 'data': {'runs': runs}}))\n")


class SpacesEndToEndTests(unittest.TestCase):
    """The routes with the real spaces module, a real working folder and Hermes' own scheduled-task store."""
    context = native.NativeAPITests.context
    headers = native.NativeAPITests.headers

    def setUp(self):
        native.NativeAPITests.setUp(self)
        self.research = self.home / "profiles" / "research"
        self.work = self.home / "research-work"
        self.work.mkdir()
        (self.research / "config.yaml").write_text(yaml.safe_dump({"terminal": {"cwd": str(self.work)}}))
        (self.work / "collect.py").write_text(COLLECTOR)
        (self.work / "page.html").write_text("<!doctype html><title>Commute</title><p>Drive</p>")
        self.assertTrue(spaces.available(), "this Hermes has scheduled tasks")
        store = spaces.store_for_home(self.research)
        routine = spaces.check_routine({"schedule": "every 15m", "script": "collect.py"}, "research", now=store.now())
        self.space = spaces.save_draft(
            store, "research", session_id="20261112_101500_abc123", space_id=None, title="Commute",
            description="Drive time to work.", card=metric_card(1), sizes=None, size="wide",
            html=b"<!doctype html><title>Commute</title><p>Drive</p>", data=None, routine=routine)
        self.id = self.space.space_id

    def post(self, operation: str, body: dict):
        return self.client.post(PREFIX + "/spaces/" + operation, headers=self.headers(),
                                json={"agentId": "research", "spaceId": self.id, **body})

    def jobs(self) -> list[dict]:
        path = self.research / "cron" / "jobs.json"
        if not path.exists():
            return []
        value = json.loads(path.read_text())
        return value["jobs"] if isinstance(value, dict) else value

    def test_a_draft_is_added_refreshed_changed_and_deleted_through_the_routes(self):
        draft = self.post("get", {}).json()["space"]
        self.assertEqual(("draft", "draft", 1, self.id), (draft["state"], draft["status"], draft["revision"],
                                                          draft["spaceId"]))
        self.assertEqual([], self.jobs())
        listed = self.client.post(PREFIX + "/spaces/list", headers=self.headers(), json={"agentIds": ["research", "nobody"]})
        self.assertEqual(({"research": "ok", "nobody": "profile_not_found"}, []),
                         (listed.json()["agents"], listed.json()["spaces"]), "a draft is not on Today")

        stale = self.post("add", {"draftRevision": 7, "operationId": str(uuid.uuid4())})
        self.assertEqual((409, "draft_changed", {"revision": 1}),
                         (stale.status_code, stale.json()["error"]["code"], stale.json()["error"]["details"]))
        self.assertEqual([], self.jobs(), "a refused add schedules nothing")

        added_id = str(uuid.uuid4())
        added = self.post("add", {"draftRevision": 1, "operationId": added_id, "size": "small"})
        self.assertEqual(200, added.status_code, added.text)
        live = added.json()["space"]
        self.assertEqual(("live", "small", "running"), (live["state"], live["size"], live["routine"]["state"]))
        self.assertEqual(1, len(self.jobs()))
        job = self.jobs()[0]
        self.assertEqual((True, "Space: Commute"), (job.get("no_agent"), job.get("name")))
        # The same operationId answers the same space and schedules nothing twice.
        again = self.post("add", {"draftRevision": 1, "operationId": added_id, "size": "small"})
        self.assertEqual(live["spaceId"], again.json()["space"]["spaceId"])
        self.assertEqual(1, len(self.jobs()))
        # A new add of a space that is live with nothing pending answers the same space and schedules nothing.
        second = self.post("add", {"draftRevision": 1, "operationId": str(uuid.uuid4())})
        self.assertEqual((200, self.id), (second.status_code, second.json()["space"]["spaceId"]))
        self.assertEqual(1, len(self.jobs()))

        listed = self.client.post(PREFIX + "/spaces/list", headers=self.headers(), json={"agentIds": ["research"]})
        spaces_listed = listed.json()["spaces"]
        self.assertEqual([self.id], [item["spaceId"] for item in spaces_listed])
        self.assertIn("card", spaces_listed[0])
        digest = spaces_listed[0]["cardsSha256"]
        known = self.client.post(PREFIX + "/spaces/list", headers=self.headers(),
                                 json={"agentIds": ["research"], "known": {"research/" + self.id: digest}})
        left_out = known.json()["spaces"][0]
        self.assertEqual((False, False), ("card" in left_out, left_out["cardOmitted"]))

        changed = self.post("update", {"operationId": str(uuid.uuid4()),
                                       "changes": {"size": "tall", "hidden": True, "position": 2048, "title": "Drive"}})
        self.assertEqual(200, changed.status_code, changed.text)
        value = changed.json()["space"]
        self.assertEqual(("tall", True, 2048, "Drive"), (value["size"], value["hidden"], value["position"], value["title"]))

        refresh_id = str(uuid.uuid4())
        refreshed = self.post("refresh", {"operationId": refresh_id})
        self.assertEqual(200, refreshed.status_code, refreshed.text)
        self.assertEqual("now", refreshed.json()["ran"])
        self.assertEqual("1", (self.work / "runs.txt").read_text())
        card = refreshed.json()["space"]["card"]
        self.assertEqual(101, card["elements"]["m"]["props"]["value"]["literal"])
        self.assertEqual("fresh", refreshed.json()["space"]["status"])
        # A repeat of that operation is answered from the record; a new one too soon is a 429.
        self.assertEqual(200, self.post("refresh", {"operationId": refresh_id}).status_code)
        self.assertEqual("1", (self.work / "runs.txt").read_text())
        soon = self.post("refresh", {"operationId": str(uuid.uuid4())})
        self.assertEqual(429, soon.status_code, soon.text)
        self.assertEqual("refresh_too_soon", soon.json()["error"]["code"])
        self.assertGreater(soon.json()["error"]["details"]["retryAfter"], 0)

        gone_id = str(uuid.uuid4())
        gone = self.post("delete", {"operationId": gone_id})
        self.assertEqual({"spaceId": self.id, "deleted": True, "existed": True}, gone.json())
        self.assertEqual([], self.jobs())
        self.assertEqual({"spaceId": self.id, "deleted": True, "existed": True},
                         self.post("delete", {"operationId": gone_id}).json(), "a retry gets the stored answer")
        self.assertEqual({"spaceId": self.id, "deleted": True, "existed": False},
                         self.post("delete", {"operationId": str(uuid.uuid4())}).json())
        self.assertEqual(404, self.post("get", {}).status_code)
        self.assertEqual("space_not_found", self.post("get", {}).json()["error"]["code"])


class SpaceCommandTests(unittest.TestCase):
    """`hermes bighelp space update <space_id>`."""

    def parse(self, *arguments: str):
        parser = argparse.ArgumentParser()
        registration.setup_cli(parser)
        return parser.parse_args(arguments)

    def run_command(self, stdin: bytes, *, result=None, error=None):
        args = self.parse("space", "update", SPACE)
        apply = Mock(return_value=result or {"space_id": SPACE, "updated_at": "2026-11-12T14:02:00Z"},
                     side_effect=error)
        out = io.StringIO()
        with patch.object(spaces, "apply_update", apply), patch.object(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(stdin))), \
                patch.object(sys, "stdout", out), patch("loopdy_plugin.host_telemetry.mute_process"):
            try:
                registration.handle_cli(args, service=None, profile="research")
                code = 0
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue().strip(), apply

    def test_the_command_is_declared(self):
        args = self.parse("space", "update", SPACE)
        self.assertEqual(("space", "update", SPACE), (args.bighelp_action, args.bighelp_space_action, args.space_id))
        with self.assertRaises(SystemExit), patch.object(sys, "stderr", io.StringIO()):
            self.parse("space")

    def test_it_applies_the_json_on_stdin_to_the_agents_space(self):
        update = {"card": {"schema": "loopdy.card"}, "data": {"n": 3}}
        code, printed, apply = self.run_command(json.dumps(update).encode())
        self.assertEqual((0, "updated 2026-11-12T14:02:00Z"), (code, printed))
        apply.assert_called_once_with("research", SPACE, card={"schema": "loopdy.card"}, sizes=None, data={"n": 3},
                                      source="cli")

    def test_a_refusal_prints_one_fixed_code_and_exits_1(self):
        failure = spaces.SpaceError(404, "space_not_found", "That space isn't there.", {})
        code, printed, _ = self.run_command(b'{"data": 1}', error=failure)
        self.assertEqual((1, "space_not_found"), (code, printed))

    def test_input_that_is_not_one_update_object_is_refused_before_anything_runs(self):
        for stdin in (b"", b"not json", b"[]", b"{}", b'{"extra": 1}', b'{"card": {}, "extra": 1}', b'"text"',
                      b"\xff\xfe", b'{"data":"' + b"x" * 1_310_720 + b'"}'):
            with self.subTest(stdin=stdin[:20]):
                code, printed, apply = self.run_command(stdin)
                self.assertEqual((1, "invalid_request"), (code, printed))
                apply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
