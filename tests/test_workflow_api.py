"""Workflow routes through real Hermes auth, checked against the shared test vectors."""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

from loopdy_plugin import workflow_api, workflow_coordinator
import test_native_api as native_fixtures
from workflow_fixtures import AGENTS, BINDINGS, DRAFT, Engine, INPUTS


VECTORS = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "workflows-v1"
PREFIX = native_fixtures.PREFIX + "/workflows/"
# Keys a response leaves out when they don't apply (docs/WORKFLOWS.md marks them with ?).
OPTIONAL = {"attention", "failure", "waiting", "signoff", "previous", "wordCount", "value", "reason", "stageKey"}
# Fields v2 added to v1 responses: the v1 vectors stay as they were, and apps ignore fields they don't know.
ADDED_IN_V2 = frozenset({"pinned", "source", "updatedAt", "mode"})


V2_VECTORS = {"draft-save-new.json", "draft-save-v2.json", "validate-v2.json", "pin.json", "unarchive.json",
              "list-v2.json", "get-v2.json", "templates-save.json", "templates-list-v2.json",
              "templates-use-yours.json", "templates-delete.json", "status-v2.json", "runs-get-text-runner.json"}


def vector(name: str) -> dict:
    return json.loads((VECTORS / name).read_text())


def kind(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


def assert_shape(test, expected, actual, path="response", added=frozenset()):
    """Same keys (apart from optional ones) and the same JSON types, all the way down."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        if path.endswith(".definition") or path.endswith(".inputs") or path.endswith(".value"):
            return
        missing = set(expected) - set(actual) - OPTIONAL
        extra = set(actual) - set(expected) - OPTIONAL - added
        test.assertEqual((missing, extra), (set(), set()), path)
        for key in set(expected) & set(actual):
            assert_shape(test, expected[key], actual[key], f"{path}.{key}", added)
    elif isinstance(expected, list) and isinstance(actual, list):
        if expected:
            for index, item in enumerate(actual):
                assert_shape(test, expected[0], item, f"{path}[{index}]", added)
    elif kind(expected) is not None and kind(actual) is not None:
        test.assertEqual(kind(expected), kind(actual), path)


class WorkflowRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.client = self.fixture.client
        self.client.app.include_router(workflow_api.router, prefix="/api/plugins/loopdy")
        self.home = self.fixture.home
        for agent in AGENTS:
            (self.home / "profiles" / agent).mkdir(parents=True, exist_ok=True)
            (self.home / "profiles" / agent / "config.yaml").write_text("model: fixture\n")
        launch = patch.object(workflow_coordinator, "launch_service", return_value=True)
        self.launches = launch.start()
        self.addCleanup(launch.stop)
        workflow_api._probe_cache.clear()

    def call(self, path, body=None, *, headers=None, status=200):
        response = self.client.post(PREFIX + path, json={} if body is None else body,
                                    headers=self.fixture.headers() if headers is None else headers)
        self.assertEqual(response.status_code, status, response.text)
        return response

    def ok(self, path, body=None, vector_name=None):
        response = self.call(path, body)
        self.assertEqual(response.headers["cache-control"], "no-store")
        value = response.json()
        if vector_name is not None:
            assert_shape(self, vector(vector_name)["response"], value,
                         added=frozenset() if vector_name in V2_VECTORS else ADDED_IN_V2)
        return value

    def error(self, path, body, status, code):
        value = self.call(path, body, status=status).json()
        self.assertEqual(value["error"]["code"], code, value)
        return value

    def test_context_advertises_workflows(self):
        features = self.fixture.context().json()["features"]
        self.assertIn("native-workflows-v1", features)
        with patch.object(workflow_api, "probe", return_value="hermes_update_needed"):
            self.assertNotIn("native-workflows-v1", self.fixture.context().json()["features"])
            self.error("status", {}, 503, "workflows_unavailable")

    def test_every_route_matches_the_vectors(self):
        status = self.ok("status", {}, "status.json")
        self.assertEqual((status["coordinator"]["state"], status["slots"]["total"], status["runner"]),
                         ("offline", 2, {"available": True, "mode": "stream"}))
        self.assertEqual(self.ok("templates/list", {}, "templates-list.json")["templates"][0]["id"],
                         "research-draft-review")
        used = self.ok("templates/use", {"templateId": "research-draft-review", "name": "Weekly newsletter"},
                       "templates-use.json")
        workflow_id = used["workflowId"]
        self.assertFalse(used["validation"]["host"])
        for role, agent in BINDINGS:
            bound = self.ok("bind", {"workflowId": workflow_id, "role": role, "agentId": agent}, "bind.json")
        self.assertEqual([item["agentId"] for item in bound["bindings"]], ["research", "writer", "editor"])
        got = self.ok("get", {"workflowId": workflow_id}, "get.json")
        self.assertEqual(got["validation"], {"valid": True, "host": True, "issues": []})
        definition = got["workflow"]["definition"]
        definition["stages"][0]["tools"] = ["web", "terminal"]
        saved = self.ok("draft/save", {"workflowId": workflow_id, "baseDraftVersion": 1, "definition": definition},
                        "draft-save.json")
        self.assertEqual(saved["draftVersion"], 2)
        validation = self.ok("validate", {"workflowId": workflow_id}, "validate.json")["validation"]
        self.assertEqual([issue["code"] for issue in validation["issues"]], ["tool_terminal"])
        self.assertEqual(self.ok("publish", {"workflowId": workflow_id, "draftVersion": 2}, "publish.json"),
                         {"workflowId": workflow_id, "revision": 1})
        listed = self.ok("list", {}, "list.json")
        self.assertEqual(listed["workflows"][0]["needsSetupRoles"], [])

        token = str(uuid.uuid4())
        started = self.ok("runs/start", {"workflowId": workflow_id, "revision": 1, "inputs": INPUTS,
                                         "clientRunToken": token, "sample": False}, "runs-start.json")["run"]
        self.assertEqual(started["state"], "planned")
        self.assertEqual(self.launches.call_count, 1)
        again = self.ok("runs/start", {"workflowId": workflow_id, "revision": 1, "inputs": INPUTS,
                                       "clientRunToken": token, "sample": False})["run"]
        self.assertEqual(again["id"], started["id"])

        engine = Engine(self, home=self.home)
        engine.start()
        engine.tick()
        running = self.ok("runs/get", {"runId": started["id"]}, "runs-get-running.json")["run"]
        self.assertEqual(running["state"], "running")
        engine.through_review(started["id"])
        engine.coordinator.tick()
        listed = self.ok("list", {}, "list.json")
        self.assertEqual([run["id"] for run in listed["waiting"]], [started["id"]])
        waiting = self.ok("runs/get", {"runId": started["id"]}, "runs-get.json")["run"]
        self.assertEqual(waiting["state"], "waiting_for_you")
        artifact = waiting["signoff"]["artifact"]
        read = self.ok("artifacts/read", {"runId": started["id"], "sha256": artifact["sha256"], "offset": 0,
                                          "length": 38}, "artifacts-read.json")
        self.assertEqual(base64.b64decode(read["data"]), DRAFT.encode()[:38])
        self.assertFalse(read["done"])
        events = self.ok("runs/events", {"runId": started["id"], "after": 0, "limit": 5}, "runs-events.json")
        self.assertTrue(events["hasMore"])
        more = self.ok("runs/events", {"runId": started["id"], "after": events["cursor"]})
        self.assertEqual(more["events"][0]["seq"], events["cursor"] + 1)
        self.ok("runs/list", {"filter": "for_you"}, "runs-list.json")
        signed = self.ok("runs/signoff", {"runId": started["id"], "stageKey": "signoff", "decision": "approve",
                                          "artifactSha256": artifact["sha256"]}, "runs-signoff.json")["run"]
        self.assertEqual(signed["state"], "succeeded")

        other = self.ok("runs/start", {"workflowId": workflow_id, "revision": 1, "inputs": INPUTS,
                                       "clientRunToken": str(uuid.uuid4())})["run"]
        cancelled = self.ok("runs/control", {"runId": other["id"], "action": "cancel",
                                             "expectedVersion": other["version"]}, "runs-control.json")["run"]
        self.assertEqual(cancelled["state"], "cancelled")
        every = self.ok("runs/list", {"filter": "all", "limit": 50}, "run-states.json")
        self.assertEqual({run["state"] for run in every["runs"]}, {"succeeded", "cancelled"})
        self.assertEqual(self.ok("archive", {"workflowId": workflow_id}, "archive.json")["archived"], True)

    def test_errors_use_the_documented_codes(self):
        documented = {item["code"]: item["status"] for item in vector("errors.json")["errors"]}
        used = self.ok("templates/use", {"templateId": "research-draft-review"})
        workflow_id = used["workflowId"]
        cases = [
            ("get", {"workflowId": "wf_0000000000000000"}, "workflow_not_found"),
            ("templates/use", {"templateId": "nothing"}, "template_not_found"),
            ("bind", {"workflowId": workflow_id, "role": "writer", "agentId": "nobody"}, "profile_not_found"),
            ("bind", {"workflowId": workflow_id, "role": "pilot", "agentId": "writer"}, "role_not_found"),
            ("draft/save", {"workflowId": workflow_id, "baseDraftVersion": 9,
                            "definition": {"schemaVersion": 1, "name": "x", "stages": []}}, "draft_conflict"),
            ("draft/save", {"baseDraftVersion": 0, "definition": {"schemaVersion": 1}}, "invalid_definition"),
            ("publish", {"workflowId": workflow_id, "draftVersion": 4}, "draft_conflict"),
            ("runs/start", {"workflowId": workflow_id, "revision": 1, "inputs": {},
                            "clientRunToken": str(uuid.uuid4())}, "revision_not_found"),
            ("runs/get", {"runId": "run_0000000000000000"}, "run_not_found"),
        ]
        for path, body, code in cases:
            with self.subTest(path=path, code=code):
                self.error(path, body, documented[code], code)
        self.ok("publish", {"workflowId": workflow_id, "draftVersion": 1})
        self.error("runs/start", {"workflowId": workflow_id, "revision": 1, "inputs": INPUTS,
                                  "clientRunToken": str(uuid.uuid4())}, 409, "not_valid")
        self.error("runs/start", {"workflowId": workflow_id, "revision": 1, "inputs": INPUTS, "sample": True,
                                  "clientRunToken": str(uuid.uuid4())}, 422, "sample_unsupported")
        for body in ({"workflowId": workflow_id, "extra": 1}, {"workflowId": "WF_1"}, {"workflowId": 4}):
            self.error("validate", body, 422, "invalid_request")

    def test_mutations_replay_by_request_id_and_echo_it(self):
        headers = self.fixture.headers()
        first = self.call("templates/use", {"templateId": "research-draft-review"}, headers=headers)
        self.assertEqual(first.headers["x-loopdy-request-id"], headers["X-Loopdy-Request-ID"])
        self.assertEqual(first.headers["etag"], headers["If-Match"])
        second = self.call("templates/use", {"templateId": "research-draft-review"}, headers=headers)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.ok("list", {})["workflows"]), 1)
        reused = self.call("archive", {"workflowId": first.json()["workflowId"]}, headers=headers, status=409)
        self.assertEqual(reused.json()["error"]["code"], "request_reused")
        stale = dict(self.fixture.headers(), **{"If-Match": '"sha256:' + "0" * 64 + '"'})
        self.assertEqual(self.call("list", {}, headers=stale, status=412).json()["error"]["code"], "context_changed")

    def test_runner_unavailable_blocks_new_runs_only(self):
        used = self.ok("templates/use", {"templateId": "research-draft-review"})
        with patch.object(workflow_api, "probe", side_effect=lambda: None):
            for role, agent in BINDINGS:
                self.ok("bind", {"workflowId": used["workflowId"], "role": role, "agentId": agent})
            self.ok("publish", {"workflowId": used["workflowId"], "draftVersion": 1})
        def probe():
            # The feature stays listed; only starting a run asks again and finds no service manager.
            return "service_manager_missing" if sys._getframe(1).f_code.co_name == "_start" else None

        with patch.object(workflow_api, "probe", new=probe):
            self.error("runs/start", {"workflowId": used["workflowId"], "revision": 1, "inputs": INPUTS,
                                      "clientRunToken": str(uuid.uuid4())}, 503, "runner_unavailable")


if __name__ == "__main__":
    unittest.main()
