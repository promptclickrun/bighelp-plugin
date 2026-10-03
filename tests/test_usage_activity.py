"""When an agent works, for the app's Usage page: hours, messages and models per day."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import uuid

from loopdy_plugin import usage_activity as ua

NOW = datetime(2026, 10, 3, 21, 30).timestamp()


def _started(day: int, hour: int) -> float:
    return datetime(2026, 10, day, hour, 15).timestamp()


class _Home:
    """A HERMES_HOME whose default agent has Hermes' real state.db."""

    def __init__(self, case: unittest.TestCase):
        temporary = tempfile.TemporaryDirectory(prefix="bighelp-usage-activity-")
        case.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve()
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.path), "HOME": str(self.path)})
        environment.start()
        case.addCleanup(environment.stop)

    def sessions(self, rows):
        from hermes_state import SessionDB
        SessionDB(db_path=self.path / "state.db").close()  # Hermes' own schema
        with sqlite3.connect(self.path / "state.db") as db:
            for index, (started, model, input_tokens, output_tokens, cost, messages) in enumerate(rows):
                db.execute(
                    "INSERT INTO sessions (id, source, model, started_at, input_tokens, output_tokens, "
                    "estimated_cost_usd, message_count) VALUES (?, 'bighelp', ?, ?, ?, ?, ?, ?)",
                    (f"s{index}", model, started, input_tokens, output_tokens, cost, messages))


class ActivityTests(unittest.TestCase):
    def setUp(self):
        if not ua.available():
            self.skipTest("Hermes profile helpers unavailable")
        self.home = _Home(self)

    def test_counts_hours_messages_and_models_per_day(self):
        self.home.sessions([
            (_started(1, 20), "claude-opus-5-5", 1_000, 200, 0.5, 12),
            (_started(1, 20), "claude-opus-5-5", 3_000, 300, 1.25, 8),
            (_started(2, 9), "gpt-6-astra", 5_000, 100, None, 4),
            (_started(3, 9), None, 10, 1, 0.0, 2),
            (datetime(2026, 8, 1, 9).timestamp(), "claude-opus-5-5", 9_999, 9, 9.0, 99),  # before the period
        ])
        value = ua.activity("default", 7, now=NOW)
        self.assertEqual(value["hours"][20], 2)
        self.assertEqual(value["hours"][9], 2)
        self.assertEqual(sum(value["hours"]), 4, "Only sessions started in the period")
        self.assertEqual(value["messages"], 26)
        self.assertEqual(value["modelDays"], [
            {"day": "2026-10-01", "model": "claude-opus-5-5", "tokens": 4_500, "cost": 1.75},
            {"day": "2026-10-02", "model": "gpt-6-astra", "tokens": 5_100, "cost": 0.0},
        ])
        self.assertFalse(value["truncated"])

    def test_an_agent_without_sessions_has_an_empty_day(self):
        value = ua.activity("default", 30, now=NOW)
        self.assertEqual((value["hours"], value["messages"], value["modelDays"]), ([0] * 24, 0, []))

    def test_unknown_agents_and_ranges_are_bounded(self):
        with self.assertRaises(LookupError):
            ua.activity("ghost", 7)
        self.assertEqual(ua.activity("default", 10_000)["days"], ua.MAX_DAYS)

    def test_a_long_list_of_models_says_it_was_cut(self):
        self.home.sessions([(_started(1, 10), f"model-{index}", 10, 1, 0.0, 1) for index in range(5)])
        with patch.object(ua, "MAX_MODEL_DAYS", 3):
            value = ua.activity("default", 7, now=NOW)
        self.assertEqual(len(value["modelDays"]), 3)
        self.assertTrue(value["truncated"], "The app won't chart a model from a partial list")


class RouteTests(unittest.TestCase):
    """POST /native/usage/activity through Hermes' real auth middleware."""

    def setUp(self):
        if not ua.available():
            self.skipTest("Hermes profile helpers unavailable")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
        from hermes_cli.dashboard_auth.registry import register_provider, unregister_global_provider
        from loopdy_plugin import native_api
        from test_native_api import FixtureProvider

        self.home = _Home(self)
        self.provider = FixtureProvider()
        register_provider(self.provider)
        self.addCleanup(unregister_global_provider, self.provider.name, self.provider)
        app = FastAPI()
        app.state.auth_required = True
        app.middleware("http")(gated_auth_middleware)
        app.include_router(native_api.router, prefix="/api/plugins/loopdy")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def headers(self):
        context = self.client.get("/api/plugins/loopdy/native/context", headers={"Authorization": "Bearer fixture-alice"})
        self.assertIn(ua.CAPABILITY, context.json()["features"])
        return {"Authorization": "Bearer fixture-alice", "If-Match": context.headers["etag"],
                "X-Loopdy-Request-ID": str(uuid.uuid4())}

    def test_reads_the_agents_own_sessions(self):
        self.home.sessions([(datetime.now().timestamp() - 3_600, "claude-opus-5-5", 100, 20, 0.01, 3)])
        response = self.client.post("/api/plugins/loopdy/native/usage/activity", headers=self.headers(),
                                    json={"agentId": "default", "days": 7})
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual((value["agentId"], value["days"], sum(value["hours"]), value["messages"]),
                         ("default", 7, 1, 3))
        self.assertEqual(value["modelDays"][0]["tokens"], 120)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn(str(self.home.path), response.text, "No host paths reach the app")

    def test_rejects_unknown_agents_bad_ranges_and_missing_auth(self):
        path = "/api/plugins/loopdy/native/usage/activity"
        self.assertEqual(self.client.post(path, headers=self.headers(), json={"agentId": "ghost", "days": 7}).status_code, 404)
        for body in ({"agentId": "default", "days": 0}, {"agentId": "default", "days": 366},
                     {"agentId": "default", "days": "7"}, {"agentId": "default"},
                     {"agentId": "default", "days": 7, "extra": True}):
            with self.subTest(body=body):
                self.assertEqual(self.client.post(path, headers=self.headers(), json=body).status_code, 422)
        self.assertEqual(self.client.post(path, json={"agentId": "default", "days": 7}).status_code, 401)
        headers = self.headers()
        del headers["If-Match"]
        self.assertEqual(self.client.post(path, headers=headers, json={"agentId": "default", "days": 7}).status_code, 428)


if __name__ == "__main__":
    unittest.main()
