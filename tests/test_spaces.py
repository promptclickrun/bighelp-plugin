"""Spaces: the store, drafts, Add to Today, the app's changes and what it reads (docs/SPACES.md)."""
from __future__ import annotations

import telemetry_off  # noqa: F401  (no test run posts host telemetry)
import json
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from loopdy_plugin import html_apps, people, spaces, spaces_tools, workspace_root
from loopdy_plugin.generative_ui import extract_rendered_envelope
from loopdy_plugin.people import PeopleStore
from loopdy_plugin.spaces import SpaceError

CHAT = "20261112_101500_abc123"
PROFILE = "personal"
PAGE = "<!doctype html><title>Commute</title><p>Drive time</p>"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "loopdy_card_v1" / "static-space-draft.json"
COLLECTOR = "import json, sys\njson.load(sys.stdin)\nprint(json.dumps({'data': {'minutes': 31}}))\n"


def metric(value: int = 42, title: str = "Commute") -> dict:
    """A small basic card, as a bot writes it (bighelp.card)."""
    return {"schema": "bighelp.card", "version": 1, "title": title, "spoken_summary": f"{value} minutes to work.",
            "data_sources": [], "root": "m", "elements": {"m": {
                "type": "metric", "props": {"label": "Drive", "value": {"literal": value}}, "children": []}}}


def op() -> str:
    return str(uuid.uuid4())


class FakeCron:
    """Hermes' cron.jobs with create/remove/list faked the way tests/test_workflow_api.py fakes them, with
    Hermes' own signatures (autospec). Each job remembers which cron store it was made in; parse_schedule
    and use_cron_store are Hermes' own. ``failing`` names functions that raise; ``kept`` holds job ids that
    remove_job answers for but leaves in place, as a store Hermes couldn't write would."""

    def __init__(self, case: unittest.TestCase) -> None:
        import cron.jobs as jobs
        self.module = jobs
        self.jobs: dict[str, dict] = {}
        self.removed: list[str] = []
        self.triggered: list[str] = []
        self.resumed: list[str] = []
        self.failing: set[str] = set()
        self.kept: set[str] = set()
        self.made = 0
        for name in ("create_job", "remove_job", "list_jobs", "update_job", "resume_job", "trigger_job"):
            patcher = patch.object(jobs, name, autospec=True, side_effect=getattr(self, name))
            patcher.start()
            case.addCleanup(patcher.stop)

    def _store(self) -> str:
        return str(self.module._current_cron_store().jobs_file)

    def _maybe_fail(self, name: str) -> None:
        if name in self.failing:
            raise OSError(f"{name} failed")

    def create_job(self, **fields) -> dict:
        self._maybe_fail("create_job")
        self.made += 1
        job = dict(fields, id=f"job{self.made}", enabled=True, state="scheduled", next_run_at=None,
                   last_run_at=None, last_status=None, store=self._store())
        self.jobs[job["id"]] = job
        return job

    def remove_job(self, job_id: str) -> bool:
        self._maybe_fail("remove_job")
        self.removed.append(job_id)
        if job_id in self.kept:
            return False
        return self.jobs.pop(job_id, None) is not None

    def list_jobs(self, include_disabled: bool = False) -> list[dict]:
        self._maybe_fail("list_jobs")
        return [dict(job) for job in self.jobs.values() if include_disabled or job["enabled"]]

    def update_job(self, job_id: str, updates: dict) -> dict | None:
        if job_id in self.jobs:
            self.jobs[job_id].update(updates)
        return self.jobs.get(job_id)

    def resume_job(self, job_id: str) -> dict | None:
        self.resumed.append(job_id)
        return self.update_job(job_id, {"enabled": True, "state": "scheduled"})

    def trigger_job(self, job_id: str) -> dict | None:
        self._maybe_fail("trigger_job")
        self.triggered.append(job_id)
        return self.jobs.get(job_id)


class _Context:
    profile_name = PROFILE

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.schemas: dict[str, dict] = {}

    def register_tool(self, *, name, handler, schema, **_kwargs) -> None:
        self.handlers[name] = handler
        self.schemas[name] = schema


class Host:
    """One bot: its Hermes home, its working folder, its stores, a fake clock and Hermes' cron faked."""

    def __init__(self, case: unittest.TestCase, *, fake_cron: bool = True, profile: str = PROFILE) -> None:
        folder = tempfile.TemporaryDirectory(prefix="bighelp-spaces-", dir=Path(tempfile.gettempdir()).resolve())
        case.addCleanup(folder.cleanup)
        self.case = case
        self.home = (Path(folder.name) / "home").resolve()
        self.work = Path(folder.name) / "work"
        self.home.mkdir()
        self.work.mkdir()
        self.work = self.work.resolve()
        self.profile = profile
        self.clock = 1_800_000_000.0
        self.people = PeopleStore(self.home / "plugin-data" / "loopdy")
        self.people.set_renders(CHAT, ["html_app", "space_draft"])
        self.store = spaces.store_for_home(self.home)
        self.store._now = lambda: self.clock
        self.pages = html_apps.store_for_home(self.home)
        home = self.home

        def profile_home(name: str) -> Path:
            if name != profile:
                raise html_apps.AppError(404, "profile_not_found", "The selected profile no longer exists.")
            return home

        for target, name, value in (
                (html_apps, "current_home", lambda: home),
                (html_apps, "profile_home", profile_home),
                (people, "current_store", lambda: self.people),
                (workspace_root, "resolve", lambda _profile: workspace_root.Workspace(self.work, "config"))):
            patcher = patch.object(target, name, value)
            patcher.start()
            case.addCleanup(patcher.stop)
        self.cron = FakeCron(case) if fake_cron else None
        self.context = _Context()
        self.context.profile_name = profile
        spaces_tools.register(self.context)

    def write(self, name: str, text: str) -> Path:
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def call(self, tool: str, args: dict | None, session: str | None = CHAT) -> dict:
        kwargs = {} if session is None else {"session_id": session}
        return json.loads(self.context.handlers[tool](args, **kwargs))

    def draft(self, *, routine: dict | None = None, **args) -> dict:
        if routine is None:
            self.write("collect.py", COLLECTOR)
            routine = {"schedule": "every 15m", "script": "collect.py"}
        result = self.call("bighelp_draft_space", {
            "title": "Commute", "description": "Drive time to work, from live traffic.", "card": metric(),
            "html": PAGE, "routine": routine, **args})
        if "success" in result:
            raise AssertionError(result)
        return result

    def add(self, space_id: str, revision: int = 1, **options) -> dict:
        return spaces.add(self.profile, space_id, revision, op(), **options)

    def approve(self, space_id: str) -> None:
        """What the person's Add of a re-drafted space records: the collector's content as it is now."""
        space = self.store.get(space_id)
        digest = spaces.script_digest(Path(space.routine["script_path"]).resolve())
        with self.store._db(write=True) as db:
            db.execute("UPDATE spaces SET script_sha256 = ? WHERE space_id = ?", (digest, space_id))

    def live(self, **args) -> str:
        space_id = self.draft(**args)["space_id"]
        self.add(space_id)
        return space_id

    def props(self, result: dict) -> dict:
        card = extract_rendered_envelope({key: result[key] for key in
                                          ("schema", "version", "card", "display_markdown", "instruction")})
        return card["elements"]["space"]["props"]

    def refused(self, call, code: str, status: int | None = None) -> SpaceError:
        with self.case.assertRaises(SpaceError) as caught:
            call()
        self.case.assertEqual(code, caught.exception.code)
        if status is not None:
            self.case.assertEqual(status, caught.exception.status)
        return caught.exception


class SpaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.host = Host(self)


class DraftTests(SpaceCase):
    def test_a_draft_schedules_nothing(self):
        result = self.host.draft()
        props = self.host.props(result)
        self.assertEqual({"space_id": result["space_id"], "description": "Drive time to work, from live traffic.",
                          "updates": "Every 15 minutes", "uses_ai": False, "revision": 1}, props)
        self.assertEqual(1, result["revision"])
        self.assertIn("Nothing runs until then", result["note"])
        # No Hermes job and no shim before the person's Add.
        self.assertEqual({}, self.host.cron.jobs)
        self.assertEqual(0, self.host.cron.made)
        self.assertFalse((self.host.home / "scripts").exists())
        space = self.host.store.get(result["space_id"])
        self.assertEqual(("draft", None, "script", 15), (space.state, space.job_id, space.routine["kind"],
                                                          space.routine["cadence_minutes"]))
        # Its page is an HTML app owned by the space, which never sends answers.
        page = self.host.pages.get(space.app_id)
        self.assertEqual((result["space_id"], False, CHAT), (page.space_id, page.sends_answers, page.session_id))
        self.assertEqual(PAGE.encode(), self.host.pages.read_html(space.app_id)[1])
        # The bot sees no host path.
        self.assertNotIn(str(self.host.work), json.dumps(result))

    def test_the_draft_card_is_the_shared_fixture(self):
        result = self.host.draft()
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        fixture["elements"]["space"]["props"]["space_id"] = result["space_id"]
        self.assertEqual(fixture, spaces.draft_card(self.host.store.get(result["space_id"])))

    def test_an_ai_draft_says_it_uses_ai(self):
        result = self.host.draft(routine={"schedule": "0 7 * * 1-5", "prompt": "Check the garden sensors."})
        self.assertEqual(("Weekdays at 7:00", True), (self.host.props(result)["updates"],
                                                      self.host.props(result)["uses_ai"]))
        self.assertEqual("A space to add to Today: Commute. Updates weekdays at 7:00, with AI.",
                         result["card"]["spoken_summary"])

    def test_changing_a_draft_replaces_it_and_bumps_the_revision(self):
        first = self.host.draft(data={"minutes": 30})
        app_id = self.host.store.get(first["space_id"]).app_id
        second = self.host.draft(space_id=first["space_id"], title="Commute home", html="<p>Two</p>",
                                 routine={"schedule": "every 2h", "prompt": "Look it up."})
        self.assertEqual((first["space_id"], 2), (second["space_id"], second["revision"]))
        space = self.host.store.get(first["space_id"])
        self.assertEqual(("Commute home", "ai", "Every 2 hours", app_id), (space.title, space.routine["kind"],
                                                                           space.routine["updates"], space.app_id))
        self.assertEqual(b"<p>Two</p>", self.host.pages.read_html(app_id)[1])
        # A new page keeps the data it had.
        self.assertEqual('{"minutes":30}', self.host.pages.get(app_id).inline_json)
        self.assertEqual(0, self.host.cron.made)

    def test_texts_and_cards_have_limits(self):
        def refused(code: str, **args):
            result = self.host.call("bighelp_draft_space", {
                "title": "T", "description": "D", "card": metric(), "html": PAGE,
                "routine": {"schedule": "every 1h", "prompt": "x"}, **args})
            self.assertEqual(code, result.get("error"), result)

        refused("invalid_arguments", title="x" * 61)
        refused("invalid_arguments", description="x" * 241)
        self.host.call("bighelp_draft_space", {"title": "x" * 60, "description": "x" * 240, "card": metric(),
                                               "html": PAGE, "routine": {"schedule": "every 1h", "prompt": "x"}})
        def texts(groups: int, each: int) -> dict:
            card = metric()
            names = [f"g{group}" for group in range(groups)]
            card["root"] = "root"
            card["elements"] = {"root": {"type": "vstack", "props": {}, "children": names}}
            for group, name in enumerate(names):
                children = [f"t{group}x{n}" for n in range(each)]
                card["elements"][name] = {"type": "vstack", "props": {}, "children": children}
                for child in children:
                    card["elements"][child] = {"type": "text", "props": {"value": {"literal": "y" * 1_999}},
                                               "children": []}
            return card

        result = self.host.call("bighelp_draft_space", {"title": "T", "description": "D", "card": texts(3, 12),
                                                        "html": PAGE, "routine": {"schedule": "every 1h",
                                                                                  "prompt": "x"}})
        self.assertEqual("card_invalid", result["error"])  # one card is at most 64 KiB
        self.assertIn("payload_too_large", result["message"])
        # Each card under 64 KiB, all together over 128 KiB.
        part = texts(2, 14)
        refused("too_large", card=part, sizes={"small": part, "tall": part})
        result = self.host.call("bighelp_draft_space", {"title": "T", "description": "D", "card": part, "html": PAGE,
                                                        "routine": {"schedule": "every 1h", "prompt": "x"}})
        self.assertNotIn("success", result)
        refused("too_large", data="x" * html_apps.MAX_DATA_BYTES)
        refused("invalid_arguments", routine={"schedule": "every 1h", "prompt": "x" * 2_001})
        refused("invalid_arguments", size="huge")

    def test_drafts_older_than_14_days_go_at_the_next_write(self):
        old = self.host.draft()["space_id"]
        old_app = self.host.store.get(old).app_id
        self.host.clock += 14 * 86_400 + 1
        self.host.draft()
        self.assertIsNone(self.host.store.get(old))
        self.assertIsNone(self.host.pages.get(old_app))

    def test_past_24_drafts_the_oldest_goes(self):
        first = self.host.draft()["space_id"]
        for _ in range(23):
            self.host.clock += 1
            self.host.draft()
        self.assertEqual(24, len(self.host.store.all("draft")))
        self.host.clock += 1
        self.host.draft()
        self.assertEqual(24, len(self.host.store.all("draft")))
        self.assertIsNone(self.host.store.get(first))


class AddTests(SpaceCase):
    def test_add_schedules_a_script_routine_without_an_agent(self):
        space_id = self.host.draft()["space_id"]
        space = self.host.add(space_id, size="small")
        self.assertEqual(("live", "small", 1024, "running"),
                         (space["state"], space["size"], space["position"], space["routine"]["state"]))
        [job] = self.host.cron.jobs.values()
        self.assertEqual((None, True, "local", f"bighelp-space-{space_id}.py", "every 15m", "Space: Commute"),
                         (job["prompt"], job["no_agent"], job["deliver"], job["script"], job["schedule"],
                          job["name"]))
        # In the bot's own cron store, and the shim in its own scripts folder.
        self.assertEqual(str(self.host.home / "cron" / "jobs.json"), job["store"])
        shim = (self.host.home / "scripts" / job["script"]).read_text()
        self.assertEqual(spaces.shim(PROFILE, space_id), shim)
        self.assertIn(f"run_scheduled({PROFILE!r}, {space_id!r})", shim)
        self.assertNotIn("Commute", shim)
        self.assertEqual(0o700, (self.host.home / "scripts" / job["script"]).stat().st_mode & 0o777)

    def test_an_ai_routine_is_an_agent_job_with_the_wrapped_prompt(self):
        space_id = self.host.draft(routine={"schedule": "every 1h", "prompt": "Check the garden."})["space_id"]
        self.host.add(space_id)
        [job] = self.host.cron.jobs.values()
        self.assertNotIn("no_agent", job)
        marker = job["prompt"].split("BIGHELP-SPACE-", 1)[1][:16]
        self.assertEqual(spaces.ai_prompt(space_id, "Commute", "Check the garden.", marker), job["prompt"])
        self.assertTrue(job["prompt"].startswith(f"Update the bighelp space {space_id} on the person's Today."))
        self.assertIn(f"BIGHELP-SPACE-{marker}\nTitle: Commute\nCheck the garden.\nEND-BIGHELP-SPACE-{marker}\n",
                      job["prompt"])
        self.assertTrue(job["prompt"].endswith("Reply only [SILENT]."))
        self.assertFalse((self.host.home / "scripts").exists())

    def test_the_revision_must_be_the_one_the_person_saw(self):
        space_id = self.host.draft()["space_id"]
        self.host.draft(space_id=space_id)
        error = self.host.refused(lambda: self.host.add(space_id, 1), "draft_changed", 409)
        self.assertEqual({"revision": 2}, error.details)
        self.assertEqual(0, self.host.cron.made)
        self.assertEqual("live", self.host.add(space_id, 2)["state"])

    def test_adding_again_changes_nothing(self):
        space_id = self.host.draft()["space_id"]
        self.host.add(space_id)
        self.assertEqual("live", self.host.add(space_id)["state"])
        self.assertEqual(1, self.host.cron.made)

    def test_new_spaces_go_after_the_last_and_today_holds_24(self):
        positions = []
        for _ in range(24):
            positions.append(self.host.add(self.host.draft()["space_id"])["position"])
        self.assertEqual([1024 * n for n in range(1, 25)], positions)
        extra = self.host.draft()["space_id"]
        self.host.refused(lambda: self.host.add(extra), "spaces_full", 409)
        self.assertEqual("draft", self.host.store.get(extra).state)

    def test_unknown_spaces_and_bad_options_are_refused(self):
        space_id = self.host.draft()["space_id"]
        self.host.refused(lambda: self.host.add("s_" + "0" * 32), "space_not_found", 404)
        self.host.refused(lambda: self.host.add(space_id, size="huge"), "invalid_request")
        self.host.refused(lambda: self.host.add(space_id, position=float("nan")), "invalid_request")
        self.host.refused(lambda: self.host.add(space_id, position=1e12 + 1), "invalid_request")
        self.host.refused(lambda: spaces.add(PROFILE, space_id, 1, "NOT-A-UUID"), "invalid_request")
        self.host.refused(lambda: spaces.add("someone-else", space_id, 1, op()), "profile_not_found", 404)
        self.assertEqual(1e12, self.host.add(space_id, position=1e12)["position"])

    def test_a_live_spaces_changed_routine_waits_for_add(self):
        space_id = self.host.live()
        self.host.write("other.py", COLLECTOR)
        result = self.host.draft(space_id=space_id, title="Commute home", card=metric(50),
                                 routine={"schedule": "every 30m", "script": "other.py"})
        self.assertIn("old one keeps running", result["note"])
        self.assertEqual(("Every 30 minutes", 2), (self.host.props(result)["updates"], result["revision"]))
        # The new title and card show now; the old routine keeps running.
        space = spaces.get_space(PROFILE, space_id)
        self.assertEqual(("Commute home", 50, True, "every 15m"),
                         (space["title"], space["card"]["elements"]["m"]["props"]["value"]["literal"],
                          space["routine"]["pendingChange"], self.host.cron.jobs["job1"]["schedule"]))
        self.assertEqual("Space: Commute home", self.host.cron.jobs["job1"]["name"])
        self.assertEqual(["job1"], list(self.host.cron.jobs))
        # Add starts the new job, then removes the old one.
        added = self.host.add(space_id, 2)
        self.assertEqual((False, "Every 30 minutes"), (added["routine"]["pendingChange"], added["routine"]["updates"]))
        self.assertEqual(["job2"], list(self.host.cron.jobs))
        self.assertEqual(["job1"], self.host.cron.removed)
        self.assertEqual(str(self.host.work / "other.py"), self.host.store.get(space_id).routine["script_path"])

    def test_the_same_routine_leaves_no_pending_change(self):
        space_id = self.host.live()
        self.host.draft(space_id=space_id, card=metric(7))
        self.assertIsNone(self.host.store.get(space_id).pending)

    def test_a_script_space_that_becomes_ai_loses_its_shim_at_add(self):
        space_id = self.host.live()
        shim = self.host.home / "scripts" / spaces.shim_name(space_id)
        self.host.draft(space_id=space_id, routine={"schedule": "every 1h", "prompt": "Look it up."})
        self.assertTrue(shim.exists())
        self.host.add(space_id, 2)
        self.assertFalse(shim.exists())


class ChangeTests(SpaceCase):
    def test_size_hidden_position_and_title(self):
        space_id = self.host.live()
        space = spaces.update(PROFILE, space_id, op(), {"size": "tall", "hidden": True, "position": -5.5,
                                                         "title": "  Drive  "})
        self.assertEqual(("tall", True, -5.5, "Drive"), (space["size"], space["hidden"], space["position"],
                                                          space["title"]))
        # Hidden keeps it running; the job takes the new name.
        self.assertEqual(("running", "Space: Drive"), (space["routine"]["state"], self.host.cron.jobs["job1"]["name"]))
        self.assertEqual(spaces.shim(PROFILE, space_id),
                         (self.host.home / "scripts" / spaces.shim_name(space_id)).read_text())
        for changes in ({}, {"size": "huge"}, {"hidden": 1}, {"position": True}, {"title": ""},
                        {"title": "x" * 61}, {"colour": "red"}):
            with self.subTest(changes=changes):
                self.host.refused(lambda: spaces.update(PROFILE, space_id, op(), changes), "invalid_request")

    def test_the_bot_may_set_size_and_hidden_when_asked(self):
        space_id = self.host.live()
        listed = self.host.call("bighelp_space_settings", None, session=None)["spaces"]
        self.assertEqual([space_id], [space["space_id"] for space in listed])
        result = self.host.call("bighelp_space_settings", {"space_id": space_id, "size": "small", "hidden": True},
                                session=None)
        self.assertEqual(("small", True, ["hidden", "size"]), (result["size"], result["hidden"], result["changed"]))


class ReplayTests(SpaceCase):
    """The app's operationId: a retry within a day gets the stored answer and nothing happens twice."""

    def test_add(self):
        space_id = self.host.draft()["space_id"]
        key = op()
        first = spaces.add(PROFILE, space_id, 1, key)
        self.host.draft(space_id=space_id, card=metric(9))  # revision 2 now
        # The retry answers the space as it is now, and schedules nothing again.
        again = spaces.add(PROFILE, space_id, 1, key)
        self.assertEqual((first["spaceId"], "live", 2), (again["spaceId"], again["state"], again["revision"]))
        self.assertEqual(1, self.host.cron.made)

    def test_update(self):
        space_id = self.host.live()
        key = op()
        spaces.update(PROFILE, space_id, key, {"size": "tall"})
        spaces.update(PROFILE, space_id, op(), {"size": "small"})
        # The retry changes nothing again: it answers the space as it is.
        self.assertEqual("small", spaces.update(PROFILE, space_id, key, {"size": "tall"})["size"])
        self.assertEqual("small", self.host.store.get(space_id).size)

    def test_a_key_belongs_to_one_operation_and_one_request(self):
        space_id = self.host.live()
        key = op()
        spaces.update(PROFILE, space_id, key, {"size": "tall"})
        # Reused for a delete, the key is refused, and nothing is deleted.
        self.host.refused(lambda: spaces.delete(PROFILE, space_id, key), "invalid_request", 422)
        self.assertIsNotNone(self.host.store.get(space_id))
        self.host.refused(lambda: spaces.update(PROFILE, space_id, key, {"size": "small"}), "invalid_request", 422)
        self.assertEqual("tall", self.host.store.get(space_id).size)
        add_key = op()
        draft = self.host.draft()["space_id"]
        spaces.add(PROFILE, draft, 1, add_key, size="small")
        self.host.refused(lambda: spaces.add(PROFILE, draft, 1, add_key, size="tall"), "invalid_request", 422)

    def test_answers_are_kept_slim(self):
        space_id = self.host.live()
        spaces.update(PROFILE, space_id, op(), {"size": "tall"})
        with self.host.store._db() as db:
            stored = [row[0] for row in db.execute("SELECT answer_json FROM operations")]
        self.assertEqual(['{"answer":{"space":true}}'] * 2, stored)

    def test_refresh(self):
        space_id = self.host.live()
        key = op()
        first = spaces.refresh(PROFILE, space_id, key)
        self.assertEqual("now", first["ran"])
        # Again within the minute: the same answer, not refresh_too_soon, and the script didn't run again.
        checked = self.host.store.get(space_id).checked
        self.host.clock += 5
        self.assertEqual(first, spaces.refresh(PROFILE, space_id, key))
        self.assertEqual(checked, self.host.store.get(space_id).checked)
        self.host.refused(lambda: spaces.refresh(PROFILE, space_id, op()), "refresh_too_soon", 429)

    def test_delete(self):
        space_id = self.host.live()
        key = op()
        self.assertEqual({"spaceId": space_id, "deleted": True, "existed": True}, spaces.delete(PROFILE, space_id, key))
        self.assertEqual({"spaceId": space_id, "deleted": True, "existed": True}, spaces.delete(PROFILE, space_id, key))
        self.assertEqual({"spaceId": space_id, "deleted": True, "existed": False},
                         spaces.delete(PROFILE, space_id, op()))

    def test_answers_last_a_day_and_a_key_belongs_to_one_space(self):
        space_id = self.host.live()
        other = self.host.live()
        key = op()
        spaces.update(PROFILE, space_id, key, {"size": "tall"})
        self.host.refused(lambda: spaces.update(PROFILE, other, key, {"size": "tall"}), "invalid_request")
        self.host.clock += 24 * 3600 + 1
        spaces.update(PROFILE, space_id, op(), {"size": "small"})
        self.assertEqual("tall", spaces.update(PROFILE, space_id, key, {"size": "tall"})["size"])  # done again

    def test_at_most_512_answers_are_kept(self):
        space_id = self.host.live()
        for _ in range(520):
            self.host.clock += 1
            spaces.update(PROFILE, space_id, op(), {"hidden": False})
        with self.host.store._db() as db:
            self.assertEqual(512, db.execute("SELECT COUNT(*) FROM operations").fetchone()[0])


class RefreshTests(SpaceCase):
    def test_a_missing_job_is_made_again(self):
        space_id = self.host.live()
        self.host.cron.jobs.clear()
        self.assertEqual("missing", spaces.get_space(PROFILE, space_id)["routine"]["state"])
        self.assertEqual("running", spaces.refresh(PROFILE, space_id, op())["space"]["routine"]["state"])
        self.assertEqual("job2", self.host.store.get(space_id).job_id)

    def test_a_paused_script_space_runs_once_and_stays_paused(self):
        space_id = self.host.live()
        self.host.cron.jobs["job1"].update(enabled=False, state="paused")
        answer = spaces.refresh(PROFILE, space_id, op())
        self.assertEqual(("now", "paused"), (answer["ran"], answer["space"]["routine"]["state"]))
        self.assertEqual(self.host.clock, self.host.store.get(space_id).checked)
        self.assertEqual([], self.host.cron.resumed)

    def test_a_paused_ai_space_isnt_run_or_resumed(self):
        space_id = self.host.live(routine={"schedule": "every 1h", "prompt": "Look it up."})
        self.host.cron.jobs["job1"].update(enabled=False, state="paused")
        answer = spaces.refresh(PROFILE, space_id, op())
        self.assertEqual(("paused", "paused"), (answer["ran"], answer["space"]["routine"]["state"]))
        self.assertEqual(([], []), (self.host.cron.resumed, self.host.cron.triggered))
        # Nothing ran, so the next refresh isn't too soon.
        self.assertEqual("paused", spaces.refresh(PROFILE, space_id, op())["ran"])

    def test_an_ai_space_is_queued_at_most_every_ten_minutes(self):
        space_id = self.host.live(routine={"schedule": "every 1h", "prompt": "Look it up."})
        self.assertEqual("queued", spaces.refresh(PROFILE, space_id, op())["ran"])
        self.assertEqual(["job1"], self.host.cron.triggered)
        self.host.clock += 599
        error = self.host.refused(lambda: spaces.refresh(PROFILE, space_id, op()), "refresh_too_soon", 429)
        self.assertEqual({"retryAfter": 1}, error.details)
        self.host.clock += 1
        spaces.refresh(PROFILE, space_id, op())

    def test_a_failed_run_answers_routine_failed_with_the_space(self):
        space_id = self.host.live()
        self.host.write("collect.py", "import sys\nsys.exit(3)\n")
        self.host.approve(space_id)
        error = self.host.refused(lambda: spaces.refresh(PROFILE, space_id, op()), "routine_failed", 502)
        self.assertEqual(("failed", "script_failed"), (error.details["space"]["routine"]["state"],
                                                       error.details["space"]["routine"]["problem"]))

    def test_a_draft_isnt_refreshed(self):
        space_id = self.host.draft()["space_id"]
        self.host.refused(lambda: spaces.refresh(PROFILE, space_id, op()), "invalid_request")


class DeleteTests(SpaceCase):
    def test_delete_removes_the_job_the_shim_the_page_and_the_row(self):
        space_id = self.host.live()
        space = self.host.store.get(space_id)
        spaces.refresh(PROFILE, space_id, op())  # makes the lock file
        lock = self.host.store.locks / f"{space_id}.lock"
        self.assertTrue(lock.exists())
        spaces.delete(PROFILE, space_id, op())
        self.assertEqual(({}, ["job1"]), (self.host.cron.jobs, self.host.cron.removed))
        self.assertFalse((self.host.home / "scripts" / spaces.shim_name(space_id)).exists())
        self.assertIsNone(self.host.pages.get(space.app_id))
        self.assertEqual([], list(self.host.pages.pages.glob(f"{space.app_id}.*")))
        self.assertIsNone(self.host.store.get(space_id))
        self.assertFalse(lock.exists())
        self.host.refused(lambda: spaces.get_space(PROFILE, space_id), "space_not_found", 404)

    def test_a_draft_can_be_deleted(self):
        space_id = self.host.draft()["space_id"]
        self.assertTrue(spaces.delete(PROFILE, space_id, op())["existed"])
        self.assertEqual([], self.host.cron.removed)


class ReadTests(SpaceCase):
    def test_the_space_object(self):
        space_id = self.host.live()
        space = spaces.get_space(PROFILE, space_id)
        stored = self.host.store.get(space_id)
        self.assertEqual({
            "spaceId", "agentId", "state", "title", "description", "size", "hidden", "position", "revision",
            "cardsSha256", "card", "sizes", "cardOmitted", "updatedAt", "checkedAt", "page", "routine", "status"},
            set(space))
        self.assertEqual({"appId": stored.app_id, "htmlSha256": self.host.pages.get(stored.app_id).html_sha256,
                          "live": True}, space["page"])
        self.assertEqual({"kind": "script", "usesAI": False, "updates": "Every 15 minutes", "cadenceMinutes": 15,
                          "state": "running", "nextRunAt": None, "lastRunAt": None, "problem": None,
                          "pendingChange": False}, space["routine"])
        self.assertEqual(("2027-01-15T08:00:00Z", None, "fresh", PROFILE),
                         (space["updatedAt"], space["checkedAt"], space["status"], space["agentId"]))
        # Never a host path.
        self.assertNotIn(str(self.host.work), json.dumps(space))
        self.assertNotIn(str(self.host.home), json.dumps(space))

    def test_drafts_are_read_but_not_listed(self):
        draft = self.host.draft()["space_id"]
        self.assertEqual(("draft", "draft", "draft"), (
            spaces.get_space(PROFILE, draft)["state"], spaces.get_space(PROFILE, draft)["status"],
            spaces.get_space(PROFILE, draft)["routine"]["state"]))
        self.assertEqual([], spaces.list_spaces(PROFILE, {}, 1_000_000))

    def test_list_leaves_out_cards_the_app_has_and_cards_past_the_budget(self):
        first, second = self.host.live(), self.host.live()
        listed = spaces.list_spaces(PROFILE, {}, 1_000_000)
        self.assertEqual([first, second], [space["spaceId"] for space in listed])
        digest = listed[0]["cardsSha256"]
        known = spaces.list_spaces(PROFILE, {first: digest}, 1_000_000)
        self.assertEqual((False, False), ("card" in known[0], known[0]["cardOmitted"]))
        self.assertTrue("card" in known[1])
        # The route's key form works too.
        self.assertNotIn("card", spaces.list_spaces(PROFILE, {f"{PROFILE}/{first}": digest}, 1_000_000)[0])
        tight = spaces.list_spaces(PROFILE, {}, len(json.dumps(listed[0], separators=(",", ":"))) + 10)
        self.assertEqual([True, False], ["card" in space for space in tight])
        self.assertEqual([False, True], [space["cardOmitted"] for space in tight])
        self.host.refused(lambda: spaces.list_spaces("someone-else", {}, 1), "profile_not_found", 404)

    def test_the_same_card_keeps_its_hash_and_its_update_time(self):
        space_id = self.host.live()
        made = self.host.clock
        before = self.host.store.get(space_id).cards_sha256
        spaces.apply_update(PROFILE, space_id, data={"minutes": 3}, source="cli")
        self.host.clock += 600
        spaces.apply_update(PROFILE, space_id, card=metric(), data={"minutes": 3}, source="cli")
        after = self.host.store.get(space_id)
        # Nothing the person sees changed: updatedAt stays, checkedAt moves.
        self.assertEqual((before, made, self.host.clock), (after.cards_sha256, after.card_updated, after.checked))
        self.host.clock += 60
        spaces.apply_update(PROFILE, space_id, data={"minutes": 4}, source="cli")
        self.assertEqual(self.host.clock, self.host.store.get(space_id).card_updated)
        spaces.apply_update(PROFILE, space_id, card=metric(43), source="cli")
        self.assertNotEqual(before, self.host.store.get(space_id).cards_sha256)


class StatusTests(SpaceCase):
    """The routine's state and the space's status, by a fake clock and Hermes' job fields."""

    def state(self, space_id: str) -> tuple[str, str, str | None]:
        space = spaces.get_space(PROFILE, space_id)
        return space["routine"]["state"], space["status"], space["routine"]["problem"]

    def at(self, seconds: float) -> str:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()

    def test_late_stale_missing_paused_and_failed(self):
        space_id = self.host.live()  # every 15 minutes
        job = self.host.cron.jobs["job1"]
        job["next_run_at"] = self.at(self.host.clock + 300)
        self.assertEqual(("running", "fresh", None), self.state(space_id))
        # Stale after three runs' worth without an update while the routine runs.
        self.host.clock += 45 * 60 + 1
        job["next_run_at"] = self.at(self.host.clock + 60)
        self.assertEqual(("running", "stale", None), self.state(space_id))
        # Late when the next run is overdue by more than two runs (at least 10 minutes): no ticker.
        job["next_run_at"] = self.at(self.host.clock - 30 * 60)
        self.assertEqual(("running", "stale", None), self.state(space_id))
        job["next_run_at"] = self.at(self.host.clock - 30 * 60 - 1)
        self.assertEqual(("late", "stopped", None), self.state(space_id))
        job.update(enabled=False, state="paused")
        self.assertEqual(("paused", "stopped", None), self.state(space_id))
        job.update(enabled=True, state="scheduled", next_run_at=self.at(self.host.clock + 60), last_status="error")
        self.assertEqual(("failed", "stopped", "routine_error"), self.state(space_id))
        del self.host.cron.jobs["job1"]
        self.assertEqual(("missing", "stopped", None), self.state(space_id))

    def test_the_late_floor_is_ten_minutes(self):
        self.host.write("collect.py", COLLECTOR)
        space_id = self.host.live(routine={"schedule": "every 5m", "script": "collect.py"})
        job = self.host.cron.jobs["job1"]
        job["next_run_at"] = self.at(self.host.clock - 600)
        self.assertEqual("running", self.state(space_id)[0])
        job["next_run_at"] = self.at(self.host.clock - 601)
        self.assertEqual("late", self.state(space_id)[0])

    def test_an_ai_run_without_an_update(self):
        space_id = self.host.live(routine={"schedule": "every 1h", "prompt": "Look it up."})
        job = self.host.cron.jobs["job1"]
        job["last_run_at"] = self.at(self.host.clock + 60)  # a run that updated it as it went
        self.assertEqual(("running", "fresh", None), self.state(space_id))
        job["last_run_at"] = self.at(self.host.clock + 1_801)
        self.host.clock += 1_801
        self.assertEqual(("failed", "stopped", "no_update"), self.state(space_id))
        spaces.apply_update(PROFILE, space_id, data={"n": 1}, source="tool")
        self.assertEqual(("running", "fresh", None), self.state(space_id))


class ScheduleTests(SpaceCase):
    def parsed(self, schedule: str) -> dict:
        from cron.jobs import parse_schedule
        return parse_schedule(schedule)

    def test_the_words_for_how_often(self):
        for schedule, words in (("every 15m", "Every 15 minutes"), ("every 2h", "Every 2 hours"),
                                ("every 60m", "Every hour"), ("every 90m", "Every 90 minutes"),
                                ("every 1d", "Every day"), ("every 2d", "Every 2 days"),
                                ("0 7 * * *", "Every day at 7:00"), ("30 18 * * *", "Every day at 18:30"),
                                ("5 7 * * 1-5", "Weekdays at 7:05"), ("0 * * * *", "Every hour"),
                                ("*/15 * * * *", "On a schedule"), ("0 7 * * 0,6", "On a schedule"),
                                ("0 7 1 * *", "On a schedule")):
            with self.subTest(schedule=schedule):
                self.assertEqual(words, spaces.updates_text(self.parsed(schedule)))

    def test_cadence_of_intervals_and_cron_expressions(self):
        now = self.host.clock
        for schedule, minutes in (("every 15m", 15), ("every 2h", 120), ("*/5 * * * *", 5),
                                  ("0,1 * * * *", 1), ("0 7 * * 1-5", 1_440), ("0 * * * *", 60),
                                  ("*/5 9 * * *", 5)):
            with self.subTest(schedule=schedule):
                self.assertEqual(minutes, spaces.cadence_minutes(self.parsed(schedule), now=now))

    def test_floors_and_schedules_that_dont_repeat(self):
        now = self.host.clock
        self.assertEqual(5, spaces.check_schedule("every 5m", "script", now=now)[1])
        self.assertEqual(5, spaces.check_schedule("*/5 * * * *", "script", now=now)[1])
        for schedule in ("every 4m", "*/4 * * * *", "0,3 * * * *"):
            with self.subTest(schedule=schedule):
                self.host.refused(lambda: spaces.check_schedule(schedule, "script", now=now), "schedule_too_often")
        self.assertEqual(60, spaces.check_schedule("every 1h", "ai", now=now)[1])
        self.assertEqual(60, spaces.check_schedule("0 * * * *", "ai", now=now)[1])
        for schedule in ("every 59m", "*/30 * * * *"):
            with self.subTest(schedule=schedule):
                self.host.refused(lambda: spaces.check_schedule(schedule, "ai", now=now), "schedule_too_often")
        # A one-shot ("2030-01-01T07:00"; Hermes 0.21 also reads a bare "30m" as once) doesn't repeat.
        for schedule in ("2030-01-01T07:00", "nonsense", "", "x" * 201):
            with self.subTest(schedule=schedule):
                self.host.refused(lambda: spaces.check_schedule(schedule, "script", now=now), "schedule_invalid")

    def test_without_croniter_only_intervals(self):
        with patch.dict("sys.modules", {"croniter": None}):
            self.host.refused(lambda: spaces.cadence_minutes({"kind": "cron", "expr": "0 7 * * *"}, now=0),
                              "schedule_invalid")
            self.assertEqual(15, spaces.cadence_minutes({"kind": "interval", "minutes": 15}, now=0))


class StoreTests(SpaceCase):
    def test_the_database_is_private_and_two_stores_share_it(self):
        self.assertEqual(0o600, self.host.store.path.stat().st_mode & 0o777)
        space_id = self.host.draft()["space_id"]
        other = spaces.SpaceStore(self.host.store.directory)
        self.assertEqual("Commute", other.get(space_id).title)

    def test_spaces_need_cron_and_locks(self):
        self.assertTrue(spaces.available())
        with patch.dict("sys.modules", {"cron.jobs": None}):
            self.assertFalse(spaces.available())

    def test_spaces_need_a_hermes_that_runs_scripts_without_an_agent(self):
        import cron.jobs as jobs

        def create_job(prompt, schedule, name=None, script=None, deliver=None):  # an older Hermes
            raise AssertionError("not called")

        with patch.object(jobs, "create_job", create_job):
            self.assertFalse(spaces.available())

    def test_ensure_shims_writes_only_what_differs(self):
        space_id = self.host.live()
        shim = self.host.home / "scripts" / spaces.shim_name(space_id)
        self.assertEqual(0, spaces.ensure_shims(self.host.home, PROFILE))
        shim.write_text("# moved\n")
        self.assertEqual(1, spaces.ensure_shims(self.host.home, PROFILE))
        self.assertEqual(spaces.shim(PROFILE, space_id), shim.read_text())


class _FailingCommit:
    """Makes the commit of the transaction that puts a job on a space fail, as a locked database would."""

    def __init__(self, case: unittest.TestCase) -> None:
        original = spaces.SpaceStore._db

        @contextmanager
        def db(store, *, write: bool = False):
            if not write:
                with original(store) as connection:
                    yield connection
                return
            with original(store) as connection:
                executed: list[str] = []

                class Recording:
                    def execute(self, sql, *args):
                        executed.append(sql)
                        return connection.execute(sql, *args)

                    def __getattr__(self, name):
                        return getattr(connection, name)

                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield Recording()
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
                if any(sql.startswith("UPDATE spaces SET state = 'live'") for sql in executed):
                    connection.execute("ROLLBACK")
                    raise sqlite3.OperationalError("database is locked")
                connection.execute("COMMIT")

        patcher = patch.object(spaces.SpaceStore, "_db", db)
        patcher.start()
        case.addCleanup(patcher.stop)


class JobCleanupTests(SpaceCase):
    """A job a space no longer holds is removed until Hermes confirms it; nothing keeps running unseen."""

    def recorded(self) -> dict[str, int]:
        return {job_id: row["retired"] for job_id, row in self.host.store.recorded_jobs().items()}

    def test_every_job_is_recorded_until_its_gone(self):
        space_id = self.host.live()
        self.assertEqual({"job1": 0}, self.recorded())
        spaces.delete(PROFILE, space_id, op())
        self.assertEqual({}, self.recorded())

    def test_a_delete_hermes_cant_finish_keeps_the_space_off_today_until_a_retry(self):
        space_id = self.host.live()
        app_id = self.host.store.get(space_id).app_id
        key = op()
        for failure in ("kept", "raises"):
            with self.subTest(failure=failure):
                if failure == "kept":
                    self.host.cron.kept.add("job1")
                else:
                    self.host.cron.failing.add("remove_job")
                self.host.refused(lambda: spaces.delete(PROFILE, space_id, key), "schedules_unavailable", 503)
                self.assertIn("job1", self.host.cron.jobs)
                self.assertEqual("deleting", self.host.store.get(space_id).state)
                # Off Today, gone for the person and the bot, and never run.
                self.assertEqual([], spaces.list_spaces(PROFILE, {}, 1_000_000))
                self.host.refused(lambda: spaces.get_space(PROFILE, space_id), "space_not_found", 404)
                self.assertEqual("space_not_found", self.host.call(
                    "bighelp_update_space", {"space_id": space_id, "data": {}}, session=None)["error"])
                with patch.object(spaces, "run_routine") as run:
                    spaces.run_scheduled(PROFILE, space_id)
                run.assert_not_called()
                self.host.cron.kept.clear()
                self.host.cron.failing.clear()
        # The retry (the same operationId: the failure wasn't kept) finishes it.
        self.assertEqual({"spaceId": space_id, "deleted": True, "existed": True}, spaces.delete(PROFILE, space_id, key))
        self.assertEqual(({}, None, {}), (self.host.cron.jobs, self.host.store.get(space_id), self.recorded()))
        self.assertIsNone(self.host.pages.get(app_id))
        self.assertFalse((self.host.home / "scripts" / spaces.shim_name(space_id)).exists())

    def test_the_sweep_finishes_a_delete_nobody_retried(self):
        space_id = self.host.live()
        self.host.cron.kept.add("job1")
        self.host.refused(lambda: spaces.delete(PROFILE, space_id, op()), "schedules_unavailable", 503)
        self.host.cron.kept.clear()
        spaces.sweep_orphans(PROFILE)
        self.assertEqual(({}, None), (self.host.cron.jobs, self.host.store.get(space_id)))

    def pending_add_that_leaves_job1(self) -> str:
        space_id = self.host.live()
        self.host.write("other.py", COLLECTOR)
        self.host.draft(space_id=space_id, routine={"schedule": "every 30m", "script": "other.py"})
        self.host.cron.kept.add("job1")
        self.host.add(space_id, 2)
        self.assertEqual("job2", self.host.store.get(space_id).job_id)
        self.assertEqual({"job1": 1, "job2": 0}, self.recorded())
        self.assertEqual({"job1", "job2"}, set(self.host.cron.jobs))
        self.host.cron.kept.clear()
        return space_id

    def test_a_replaced_job_hermes_kept_goes_at_the_next_refresh(self):
        space_id = self.pending_add_that_leaves_job1()
        spaces.refresh(PROFILE, space_id, op())
        self.assertEqual(({"job2"}, {"job2": 0}), (set(self.host.cron.jobs), self.recorded()))

    def test_a_replaced_job_hermes_kept_goes_at_the_next_routine_run(self):
        space_id = self.pending_add_that_leaves_job1()
        spaces.run_scheduled(PROFILE, space_id)
        self.assertEqual(({"job2"}, {"job2": 0}), (set(self.host.cron.jobs), self.recorded()))

    def test_a_replaced_job_hermes_kept_goes_at_the_next_add(self):
        self.pending_add_that_leaves_job1()
        self.host.live()
        self.assertEqual({"job2", "job3"}, set(self.host.cron.jobs))

    def test_a_replaced_job_hermes_kept_goes_with_a_delete(self):
        space_id = self.pending_add_that_leaves_job1()
        spaces.delete(PROFILE, space_id, op())
        self.assertEqual(({}, {}), (self.host.cron.jobs, self.recorded()))

    def test_a_commit_that_fails_removes_the_new_job(self):
        space_id = self.host.draft()["space_id"]
        _FailingCommit(self)
        with self.assertRaises(sqlite3.OperationalError):
            self.host.add(space_id)
        self.assertEqual(({}, {}, "draft"), (self.host.cron.jobs, self.recorded(), self.host.store.get(space_id).state))
        self.assertFalse((self.host.home / "scripts" / spaces.shim_name(space_id)).exists())

    def test_a_commit_that_fails_while_hermes_keeps_the_job_leaves_it_to_the_sweep(self):
        space_id = self.host.draft(routine={"schedule": "every 1h", "prompt": "Look it up."})["space_id"]
        self.host.cron.kept.add("job1")
        _FailingCommit(self)
        with self.assertRaises(sqlite3.OperationalError):
            self.host.add(space_id)
        self.assertEqual(({"job1"}, {"job1": 1}), (set(self.host.cron.jobs), self.recorded()))
        self.host.cron.kept.clear()
        self.assertEqual(1, spaces.sweep_orphans(PROFILE))
        self.assertEqual(({}, {}), (self.host.cron.jobs, self.recorded()))


class SweepTests(SpaceCase):
    def job(self, job_id: str, name: str, **fields) -> None:
        self.host.cron.jobs[job_id] = {"id": job_id, "name": name, "enabled": True, "state": "scheduled",
                                       "prompt": "", "script": None, "no_agent": False, **fields}

    def test_only_jobs_that_are_provably_a_spaces_go(self):
        live = self.host.live()
        shim = spaces.shim_name("s_" + "e" * 32)
        self.job("orphan", "Space: Old", script=shim, no_agent=True)
        self.job("retired", "Space: Garden", prompt="Update the bighelp space")
        self.host.store.record_job("retired", "s_" + "f" * 32)
        self.host.store.retire_job("retired")
        self.job("mine", "Space: mine", prompt="My own job")
        self.job("backup", "Backup", script=shim, no_agent=True)
        self.job("renamed", "My job", prompt="x")
        self.host.store.record_job("renamed", "s_" + "f" * 32)
        self.host.store.retire_job("renamed")
        at = datetime.fromtimestamp(self.host.clock, timezone.utc).isoformat()
        self.job("young", "Space: Young", script=shim, no_agent=True, created_at=at)
        self.host.store.record_job("starting", "s_" + "f" * 32)
        self.job("starting", "Space: Starting", prompt="x")
        self.host.store.record_job("vanished", "s_" + "f" * 32)
        self.assertEqual(2, spaces.sweep_orphans(PROFILE))
        self.assertEqual({"job1", "mine", "backup", "renamed", "young", "starting"}, set(self.host.cron.jobs))
        self.assertNotIn("vanished", self.host.store.recorded_jobs())
        self.assertEqual("job1", self.host.store.get(live).job_id)
        # Past the grace, a job no Add took and a fresh orphan go too.
        self.host.clock += spaces.SWEEP_GRACE + 1
        self.assertEqual(2, spaces.sweep_orphans(PROFILE))
        self.assertEqual({"job1", "mine", "backup", "renamed"}, set(self.host.cron.jobs))

    def test_nothing_happens_for_a_bot_without_spaces(self):
        home = Path(tempfile.mkdtemp(dir=Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(shutil.rmtree, home, True)
        self.host.cron.failing.add("list_jobs")
        with patch.object(html_apps, "profile_home", lambda _name: home):
            self.assertEqual(0, spaces.sweep_orphans("fresh"))
            spaces.on_start("fresh")
            self.assertEqual([], spaces.list_spaces("fresh", {}, 1_000_000))
            self.host.refused(lambda: spaces.get_space("fresh", "s_" + "0" * 32), "space_not_found", 404)
            self.assertFalse(spaces.delete("fresh", "s_" + "0" * 32, op())["existed"])
        self.assertEqual([], list(home.iterdir()), "a read made nothing in the bot's home")


class StartTests(SpaceCase):
    def test_a_plugin_update_rewrites_shims_and_sweeps_at_start(self):
        space_id = self.host.live()
        path = self.host.home / "scripts" / spaces.shim_name(space_id)
        root = str(Path(spaces.__file__).resolve().parents[1])
        path.write_text(path.read_text().replace(repr(root), repr("/old/plugins/loopdy")))
        self.host.cron.jobs["orphan"] = {"id": "orphan", "name": "Space: Old", "enabled": True, "no_agent": True,
                                         "script": spaces.shim_name("s_" + "e" * 32)}
        spaces.on_start(PROFILE)
        self.assertEqual(spaces.shim(PROFILE, space_id), path.read_text())
        self.assertEqual({"job1"}, set(self.host.cron.jobs))

    def test_a_start_up_failure_never_raises(self):
        with patch.object(spaces, "ensure_shims", side_effect=sqlite3.OperationalError("locked")):
            spaces.on_start(PROFILE)

    def test_registration_runs_it_for_its_profile_and_loads_anyway(self):
        from loopdy_plugin import registration
        from test_registration import _Context, _Service
        with patch.object(spaces, "on_start", side_effect=RuntimeError("boom")) as start:
            context = _Context()
            registration.register(context, service=_Service())
        start.assert_called_once_with("personal")
        self.assertIn("bighelp_draft_space", context.tools)


class RefreshClaimTests(SpaceCase):
    """Refresh's once-a-minute turn is spent only by a refresh that started something."""

    def test_a_refresh_that_starts_nothing_leaves_the_next_one_free(self):
        space_id = self.host.live()
        self.host.cron.failing.add("list_jobs")
        self.host.refused(lambda: spaces.refresh(PROFILE, space_id, op()), "schedules_unavailable", 503)
        self.host.cron.failing.clear()
        self.host.cron.jobs.clear()
        self.host.cron.failing.add("create_job")
        self.host.refused(lambda: spaces.refresh(PROFILE, space_id, op()), "schedules_unavailable", 503)
        self.host.cron.failing.clear()
        self.assertEqual("now", spaces.refresh(PROFILE, space_id, op())["ran"])
        self.host.refused(lambda: spaces.refresh(PROFILE, space_id, op()), "refresh_too_soon", 429)

    def test_an_ai_trigger_that_fails_leaves_the_next_one_free(self):
        space_id = self.host.live(routine={"schedule": "every 1h", "prompt": "Look it up."})
        self.host.cron.failing.add("trigger_job")
        self.host.refused(lambda: spaces.refresh(PROFILE, space_id, op()), "schedules_unavailable", 503)
        self.host.cron.failing.clear()
        self.assertEqual("queued", spaces.refresh(PROFILE, space_id, op())["ran"])

    def test_a_script_that_never_started_doesnt_use_the_turn(self):
        space_id = self.host.live()
        self.host.write("collect.py", COLLECTOR + "# changed\n")
        for _ in range(2):
            error = self.host.refused(lambda: spaces.refresh(PROFILE, space_id, op()), "routine_failed", 502)
            self.assertEqual("script_changed", error.details["space"]["routine"]["problem"])

    def test_a_retry_while_the_first_still_runs_is_queued(self):
        started, release = self.host.work / "started", self.host.work / "go"
        self.host.write("collect.py", (
            "import json, pathlib, sys, time\njson.load(sys.stdin)\npathlib.Path('started').write_text('1')\n"
            "for _ in range(400):\n    if pathlib.Path('go').exists():\n        break\n    time.sleep(0.05)\n"
            "print(json.dumps({'data': {'n': 1}}))\n"))
        space_id = self.host.live(routine={"schedule": "every 15m", "script": "collect.py"})
        key = op()
        first: dict = {}
        worker = threading.Thread(target=lambda: first.update(spaces.refresh(PROFILE, space_id, key)))
        worker.start()
        try:
            for _ in range(400):
                if started.exists():
                    break
                time.sleep(0.05)
            self.assertTrue(started.exists())
            again = spaces.refresh(PROFILE, space_id, key)
            self.assertEqual(("queued", space_id), (again["ran"], again["space"]["spaceId"]))
            self.host.refused(lambda: spaces.refresh(PROFILE, space_id, op()), "refresh_too_soon", 429)
        finally:
            release.write_text("1")
            worker.join(timeout=30)
        self.assertEqual("now", first["ran"])
        self.assertEqual("now", spaces.refresh(PROFILE, space_id, key)["ran"], "then the stored answer")


if __name__ == "__main__":
    unittest.main()
