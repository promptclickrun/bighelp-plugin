"""App state: the bighelp app's own layout and bot settings, kept for each person on this computer.

`StoreTests` run on a temporary folder. `RouteTests` mount the native router with a fixed native context,
so they need no Hermes sign-in.
"""
from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from loopdy_plugin import app_state, native_api
from loopdy_plugin.app_state import AppStateError, AppStateStore
from loopdy_plugin.native_context import NativeContext


PERSON = "5f0c2a9e-7d1b-4e5a-9c3f-2b8d6e4a1f07"
OTHER_PERSON = "0b6f3e2d-1c4a-4f8e-9d7b-6a5c4b3e2d1f"
PHONE = "a1d4c3b2-9e8f-4a7b-8c6d-5e4f3a2b1c0d"
IPAD = "c3b2a1d4-8e9f-4b7a-9d6c-4f5e2b3a0c1d"
LAYOUT = {"v": 1, "sections": [{"id": "sec-m3k2a-q8z1x", "name": "Work"}], "order": ["bot:a/juniper"]}


def change(value, revision: int, base: int) -> dict:
    return {"value": value, "revision": revision, "baseRevision": base}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name) / "plugin-data" / "loopdy" / "app-state"
        self.clock = [1_792_000_000.0]
        self.store = AppStateStore(self.directory, now=lambda: self.clock[0])

    def refused(self, code: str, call) -> AppStateError:
        with self.assertRaises(AppStateError) as raised:
            call()
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_a_written_key_reads_back_with_its_revision(self):
        written = self.store.write(PERSON, PHONE, {"bots.layout": change(LAYOUT, 1, 0)})
        entry = {"value": LAYOUT, "revision": 1, "updatedAt": "2026-10-14T17:46:40Z", "device": PHONE}
        self.assertEqual(written["keys"], {"bots.layout": entry})
        read = self.store.read(PERSON)
        self.assertEqual(read["keys"], {"bots.layout": entry})
        self.assertEqual(read["computerId"], written["computerId"])
        self.assertEqual(self.store.read(PERSON, ["bots.settings"])["keys"], {})

    def test_the_computer_id_is_made_once_and_kept(self):
        first = self.store.read(PERSON)["computerId"]
        uuid.UUID(first)
        self.assertEqual(first, first.lower())
        self.assertEqual(AppStateStore(self.directory).read(OTHER_PERSON)["computerId"], first)
        self.assertEqual(stat.S_IMODE((self.directory / "computer.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)

    def test_an_unreadable_computer_id_is_an_error_never_a_new_id(self):
        self.store.read(PERSON)
        (self.directory / "computer.json").write_text("{corrupt", encoding="utf-8")
        self.refused("app_state_unavailable", lambda: self.store.read(PERSON))
        self.assertEqual((self.directory / "computer.json").read_text(encoding="utf-8"), "{corrupt")

    def test_each_person_has_their_own_keys(self):
        self.store.write(PERSON, PHONE, {"bots.layout": change(LAYOUT, 1, 0)})
        self.assertEqual(self.store.read(OTHER_PERSON)["keys"], {})
        path = self.directory / "people" / f"{PERSON}.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_a_write_on_an_older_revision_is_refused_whole(self):
        self.store.write(PERSON, PHONE, {"bots.layout": change(LAYOUT, 4, 3)})
        error = self.refused("app_state_conflict", lambda: self.store.write(PERSON, IPAD, {
            "bots.layout": change({"v": 1, "sections": []}, 4, 2),
            "bots.settings": change({"v": 1}, 1, 0),
        }))
        self.assertEqual(error.status, 409)
        self.assertEqual(error.details["keys"]["bots.layout"]["revision"], 4)
        self.assertEqual(error.details["keys"]["bots.layout"]["value"], LAYOUT)
        self.assertEqual(set(self.store.read(PERSON)["keys"]), {"bots.layout"})

    def test_a_computer_that_is_behind_takes_the_newer_revision(self):
        self.store.write(PERSON, PHONE, {"bots.layout": change(LAYOUT, 3, 0)})
        newer = {**LAYOUT, "order": []}
        self.store.write(PERSON, IPAD, {"bots.layout": change(newer, 9, 8)})
        self.assertEqual(self.store.read(PERSON)["keys"]["bots.layout"]["value"], newer)

    def test_a_deleted_key_keeps_its_revision(self):
        self.store.write(PERSON, PHONE, {"bots.layout": change(LAYOUT, 5, 0)})
        self.store.write(PERSON, PHONE, {"bots.layout": change(None, 6, 5)})
        self.assertIsNone(self.store.read(PERSON)["keys"]["bots.layout"]["value"])
        self.refused("app_state_conflict", lambda: self.store.write(PERSON, IPAD, {"bots.layout": change(LAYOUT, 3, 2)}))

    def test_revisions_and_ids_are_checked(self):
        for bad in (change(LAYOUT, 3, 3), change(LAYOUT, 0, 0), change(LAYOUT, True, 0),
                    change(LAYOUT, 2**53, 1), {"revision": 1, "baseRevision": 0}):
            with self.subTest(change=bad):
                self.refused("invalid_request", lambda: self.store.write(PERSON, PHONE, {"bots.layout": bad}))
        self.refused("invalid_request", lambda: self.store.write(PERSON.upper(), PHONE, {"bots.layout": change(LAYOUT, 1, 0)}))
        self.refused("invalid_request", lambda: self.store.write(PERSON, PHONE, {"Bots Layout": change(LAYOUT, 1, 0)}))
        self.refused("invalid_request", lambda: self.store.read("../people"))

    def test_values_are_bounded_and_scanned(self):
        deep: dict = {}
        for _ in range(17):
            deep = {"a": deep}
        cases = [
            ("app_state_too_large", {"items": ["x" * 4_000] * 20}),
            ("app_state_too_large", {"text": "x" * 4_097}),
            ("invalid_request", deep),
            ("invalid_request", {"number": float("nan")}),
            ("invalid_request", {"name": "Work\u0000"}),
            ("secret_scan_blocked", {"name": "token: abcdefghij1234567890XYZ"}),
        ]
        for code, value in cases:
            with self.subTest(code=code):
                self.refused(code, lambda: self.store.write(PERSON, PHONE, {"bots.layout": change(value, 1, 0)}))
        self.store.write(PERSON, PHONE, {"bots.layout": change({"note": "Line one\nLine two\tend"}, 1, 0)})

    def test_ids_and_links_are_not_mistaken_for_keys(self):
        # The app's own values: computer/profile IDs and https links with digits. Prose rules would refuse them.
        value = {"order": ["bot:5f0c2a9e-7d1b-4e5a-9c3f-2b8d6e4a1f07/juniper"],
                 "pack": "https://avatars.example.com/v2/packs/5f0c2a9e7d1b4e5a/pack.json"}
        self.store.write(PERSON, PHONE, {"bots.settings": change(value, 1, 0)})
        for secret in ("token: abcdefghij1234567890XYZ", "password=hunter2hunter2",
                       "sk-proj-abcdefghijklmnop1234", "https://user:pass@example.com/x",
                       "-----BEGIN PRIVATE KEY-----"):
            with self.subTest(secret=secret):
                self.refused("secret_scan_blocked",
                             lambda: self.store.write(PERSON, PHONE, {"bots.layout": change({"x": secret}, 9, 8)}))

    def test_keys_and_people_are_bounded(self):
        self.store.write(PERSON, PHONE, {f"key{index}": change(index, 1, 0) for index in range(24)})
        self.refused("store_full", lambda: self.store.write(PERSON, PHONE, {"key24": change(1, 1, 0)}))
        with patch.object(app_state, "MAX_PEOPLE", 2):
            self.store.write(OTHER_PERSON, PHONE, {"bots.layout": change(LAYOUT, 1, 0)})
            third = str(uuid.uuid4())
            self.refused("store_full", lambda: self.store.write(third, PHONE, {"bots.layout": change(LAYOUT, 1, 0)}))
            self.store.write(PERSON, PHONE, {"key0": change(0, 2, 1)})

    def test_a_damaged_file_reads_empty_and_the_next_write_replaces_it(self):
        people = self.directory / "people"
        people.mkdir(parents=True)
        (people / f"{PERSON}.json").write_text("{corrupt", encoding="utf-8")
        self.assertEqual(self.store.read(PERSON)["keys"], {})
        self.store.write(PERSON, PHONE, {"bots.layout": change(LAYOUT, 1, 0)})
        self.assertEqual(self.store.read(PERSON)["keys"]["bots.layout"]["value"], LAYOUT)

    def test_a_damaged_entry_is_skipped_alone(self):
        self.store.write(PERSON, PHONE, {"bots.layout": change(LAYOUT, 2, 0), "bots.settings": change({"v": 1}, 1, 0)})
        path = self.directory / "people" / f"{PERSON}.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["keys"]["bots.settings"]["revision"] = "one"
        path.write_text(json.dumps(document), encoding="utf-8")
        with self.assertLogs("hermes.plugins.bighelp", level="WARNING") as logs:
            keys = self.store.read(PERSON)["keys"]
        self.assertEqual(set(keys), {"bots.layout"})
        self.assertNotIn(PERSON, "\n".join(logs.output))

    def test_a_link_is_never_followed(self):
        target = Path(self.folder.name) / "elsewhere.json"
        target.write_text(json.dumps({"schemaVersion": 1, "person": PERSON, "keys": {}}), encoding="utf-8")
        people = self.directory / "people"
        people.mkdir(parents=True)
        (people / f"{PERSON}.json").symlink_to(target)
        self.assertEqual(self.store.read(PERSON)["keys"], {})

    def test_the_folder_is_under_the_default_hermes_root(self):
        with patch("hermes_constants.get_default_hermes_root", return_value=Path("/private/tmp/hermes-root"),
                   create=True):
            self.assertEqual(app_state.directory(), Path("/private/tmp/hermes-root/plugin-data/loopdy/app-state"))


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name) / "app-state"
        self.features = ("native-context-v1", app_state.CAPABILITY)
        context = lambda _request: NativeContext(None, None, None, "default", self.features, "runtime")
        for target in (patch.object(native_api, "native_context", side_effect=context),
                       patch.object(app_state, "directory", return_value=self.directory)):
            target.start()
            self.addCleanup(target.stop)
        app = FastAPI()
        app.include_router(native_api.router, prefix="/api/plugins/loopdy")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    @property
    def etag(self) -> str:
        return NativeContext(None, None, None, "default", self.features, "runtime").etag

    def post(self, operation: str, body: dict, *, etag: str | None = None, request_id: str | None = None):
        headers = {"X-Loopdy-Request-ID": request_id or str(uuid.uuid4())}
        if etag is not False:
            headers["If-Match"] = etag or self.etag
        return self.client.post("/api/plugins/loopdy/native/app-state/" + operation, json=body, headers=headers)

    def test_write_and_read(self):
        request_id = str(uuid.uuid4())
        written = self.post("write", {"person": PERSON, "device": PHONE,
                                      "keys": {"bots.layout": change(LAYOUT, 1, 0)}}, request_id=request_id)
        self.assertEqual(written.status_code, 200, written.text)
        self.assertEqual(written.headers["x-loopdy-request-id"], request_id)
        self.assertEqual(written.headers["cache-control"], "no-store")
        computer = written.json()["computerId"]
        uuid.UUID(computer)
        read = self.post("read", {"person": PERSON})
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(read.json(), {"computerId": computer, "keys": written.json()["keys"]})
        self.assertEqual(self.post("read", {"person": PERSON, "keys": ["bots.settings"]}).json()["keys"], {})

    def test_a_conflict_says_what_is_stored(self):
        self.post("write", {"person": PERSON, "device": PHONE, "keys": {"bots.layout": change(LAYOUT, 2, 0)}})
        refused = self.post("write", {"person": PERSON, "device": IPAD,
                                      "keys": {"bots.layout": change({"v": 1}, 2, 1)}})
        self.assertEqual(refused.status_code, 409, refused.text)
        error = refused.json()["error"]
        self.assertEqual(error["code"], "app_state_conflict")
        self.assertEqual(error["details"]["keys"]["bots.layout"]["value"], LAYOUT)

    def test_bad_requests_are_refused(self):
        good = {"person": PERSON, "device": PHONE, "keys": {"bots.layout": change(LAYOUT, 1, 0)}}
        for body in ({**good, "extra": 1}, {**good, "person": PERSON.upper()}, {**good, "keys": {}},
                     {**good, "keys": {"Bad Key": change(LAYOUT, 1, 0)}},
                     {**good, "keys": {"bots.layout": {"value": 1, "revision": "2", "baseRevision": 0}}}):
            with self.subTest(body=body):
                self.assertEqual(self.post("write", body).status_code, 422)
        refused = self.post("write", {**good, "keys": {"bots.layout": change({"name": "token: abcdefghij1234567890XYZ"}, 1, 0)}})
        self.assertEqual(refused.json()["error"]["code"], "secret_scan_blocked")
        self.assertNotIn("abcdefghij", refused.text)
        self.assertEqual(self.post("read", {"person": PERSON, "agentId": "default"}).status_code, 422)
        self.assertEqual(self.post("erase", {"person": PERSON}).status_code, 404)

    def test_the_context_guards_every_request(self):
        self.assertEqual(self.post("read", {"person": PERSON}, etag=False).status_code, 428)
        self.assertEqual(self.post("read", {"person": PERSON}, etag='"sha256:' + "0" * 64 + '"').status_code, 412)
        self.assertEqual(self.post("read", {"person": PERSON}, request_id="NOT-A-UUID").status_code, 422)

    def test_a_host_without_the_feature_refuses(self):
        self.features = ("native-context-v1",)
        refused = self.post("read", {"person": PERSON})
        self.assertEqual(refused.status_code, 503)
        self.assertEqual(refused.json()["error"]["code"], "app_state_unavailable")


if __name__ == "__main__":
    unittest.main()
