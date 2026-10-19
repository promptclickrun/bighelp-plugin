"""My Templates: bots saved as templates on this computer, in the Template Catalog's shape.

`StoreTests` run on a temporary folder. `RouteTests` mount the native router with a fixed native context,
so they need no Hermes sign-in.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from loopdy_plugin import my_templates, native_api
from loopdy_plugin.my_templates import MyTemplateError, MyTemplatesStore
from loopdy_plugin.native_context import NativeContext


def sample(**changes) -> dict:
    template = {
        "name": "Trip Planner",
        "role": "Travel helper",
        "vibe": "Organized, upbeat",
        "description": "Saved from Juniper",
        "instructions": "# {{agent_name}}\n\nYou are {{agent_name}}. Plan trips step by step.",
    }
    template.update(changes)
    return template


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name) / "plugin-data" / "loopdy" / "my-templates"
        self.clock = [1_792_000_000.0]
        self.store = MyTemplatesStore(self.directory, now=lambda: self.clock[0])

    def write(self, name: str, content: bytes | str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / name
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        return path

    def test_a_saved_template_lists_in_the_catalogs_shape(self):
        saved = self.store.save(sample())
        uuid.UUID(saved["id"])
        self.assertEqual(saved["id"], saved["id"].lower())
        self.assertEqual(saved["source"], "mine")
        self.assertEqual(saved["name"], "Trip Planner")
        self.assertEqual(saved["role"], "Travel helper")
        self.assertEqual(saved["vibe"], "Organized, upbeat")
        self.assertEqual(saved["description"], "Saved from Juniper")
        self.assertTrue(saved["instructions"].startswith("# {{agent_name}}\n"))
        self.assertEqual(saved["updatedAt"], "2026-10-14T17:46:40Z")
        listed = self.store.list()
        self.assertEqual(listed, {"templates": [saved], "skipped": 0})

    def test_each_template_is_one_private_file(self):
        saved = self.store.save(sample())
        path = self.directory / f"{saved['id']}.json"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["schemaVersion"], 1)
        self.assertEqual(document["template"], saved)
        self.assertEqual([entry for entry in os.listdir(self.directory) if not entry.startswith(".")],
                         [f"{saved['id']}.json"], "No temporary files are left behind")

    def test_newest_first(self):
        first = self.store.save(sample(name="First"))
        self.clock[0] += 60
        second = self.store.save(sample(name="Second"))
        self.assertEqual([row["id"] for row in self.store.list()["templates"]], [second["id"], first["id"]])

    def test_a_missing_folder_is_an_empty_list(self):
        self.assertEqual(self.store.list(), {"templates": [], "skipped": 0})
        self.assertFalse(self.directory.exists(), "Listing never creates the folder")

    def test_a_corrupt_unreadable_or_incomplete_template_is_skipped_alone(self):
        good = self.store.save(sample())
        def name() -> str:
            return f"{uuid.uuid4()}.json"
        self.write(name(), "{not json")
        self.write(name(), "[]")
        self.write(name(), json.dumps({"schemaVersion": 1}))
        self.write(name(), json.dumps({"schemaVersion": 2, "template": sample()}))
        missing = sample()
        del missing["instructions"]
        self.write(name(), json.dumps({"schemaVersion": 1, "template": missing}))
        self.write(name(), json.dumps({"schemaVersion": 1, "template": sample(name=7)}))
        self.write(name(), json.dumps({"schemaVersion": 1, "template": sample(instructions="   ")}))
        self.write(name(), b"\xff\xfe\x00 not utf-8")
        self.write(name(), b"")
        self.write(name(), json.dumps({"schemaVersion": 1, "template": sample()}) + " " * my_templates.MAX_FILE_BYTES)
        other = str(uuid.uuid4())
        self.write(name(), json.dumps({"schemaVersion": 1, "template": {**sample(), "id": other}}))
        self.write("not-a-template-id.json", json.dumps({"schemaVersion": 1, "template": sample()}))
        outside = Path(self.folder.name) / "outside.json"
        outside.write_text(json.dumps({"schemaVersion": 1, "template": sample()}), encoding="utf-8")
        os.symlink(outside, self.directory / name())
        (self.directory / name()).mkdir()
        self.write("notes.txt", "ignored")
        listed = self.store.list()
        self.assertEqual(listed["templates"], [good])
        self.assertEqual(listed["skipped"], 14, "notes.txt is not a template")

    def test_a_template_with_a_key_on_disk_never_reaches_the_app(self):
        self.write(f"{uuid.uuid4()}.json", json.dumps({"schemaVersion": 1, "template": sample(
            instructions="Use api_key=abcdefghijklmnop1234 for everything.")}))
        self.assertEqual(self.store.list(), {"templates": [], "skipped": 1})

    def test_text_is_cleaned_and_variables_follow_the_catalog_rules(self):
        saved = self.store.save(sample(
            name="Trip\u0000 Planner‮", vibe=" Calm\n and kind ",
            instructions="Line one\r\nLine two\u0007",
            variables=[
                {"key": "home_city", "label": "Home city", "type": "text", "example": "Lisbon"},
                {"key": "agent_name", "label": "Reserved", "type": "text"},
                {"key": "Bad Key", "label": "Nope", "type": "text"},
                "not a variable",
            ]))
        self.assertEqual(saved["name"], "Trip Planner")
        self.assertEqual(saved["vibe"], "Calm and kind")
        self.assertEqual(saved["instructions"], "Line one\nLine two")
        self.assertEqual([variable["key"] for variable in saved["variables"]], ["home_city"])

    def test_optional_text_can_be_left_out(self):
        saved = self.store.save({"name": "Plain", "instructions": "Be helpful."})
        self.assertEqual(saved["role"], "")
        self.assertNotIn("vibe", saved)
        self.assertNotIn("description", saved)
        self.assertNotIn("variables", saved)
        self.assertEqual(self.store.list()["templates"], [saved])

    def test_saving_refuses_what_the_catalog_would_refuse(self):
        cases = {
            "empty name": (sample(name="  "), "invalid_template"),
            "long name": (sample(name="x" * (my_templates.LIMITS["name"] + 1)), "invalid_template"),
            "name with a field": (sample(name="{{agent_name}}'s twin"), "invalid_template"),
            "no instructions": (sample(instructions=""), "invalid_template"),
            "long instructions": (sample(instructions="x" * (my_templates.MAX_INSTRUCTIONS + 1)), "template_too_large"),
            "long role": (sample(role="x" * (my_templates.LIMITS["role"] + 1)), "invalid_template"),
            "bad symbol": (sample(symbol="Not A Symbol"), "invalid_template"),
            "not text": (sample(role=["a"]), "invalid_template"),
            "unknown field": (sample(avatar="data:image/png;base64,AAAA"), "invalid_template"),
            "a key": (sample(instructions="My password: hunter2hunter2hunter2"), "secret_scan_blocked"),
            "a token in the vibe": (sample(vibe="ghp_abcdefghijklmnopqrstuvwxyz0123"), "secret_scan_blocked"),
        }
        for label, (template, code) in cases.items():
            with self.subTest(label), self.assertRaises(MyTemplateError) as raised:
                self.store.save(template)
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(raised.exception.status, 413 if code == "template_too_large" else 422)
        self.assertEqual(self.store.list(), {"templates": [], "skipped": 0})

    def test_the_store_is_bounded(self):
        with patch.object(my_templates, "MAX_TEMPLATES", 2):
            self.store.save(sample(name="One"))
            self.store.save(sample(name="Two"))
            with self.assertRaises(MyTemplateError) as raised:
                self.store.save(sample(name="Three"))
        self.assertEqual((raised.exception.status, raised.exception.code), (409, "store_full"))
        self.assertEqual(len(self.store.list()["templates"]), 2)

    def test_a_long_list_stays_under_the_response_limit(self):
        for index in range(4):
            self.clock[0] += 1
            self.store.save(sample(name=f"Bot {index}", instructions="y" * 900))
        with patch.object(my_templates, "LIST_BUDGET_BYTES", 2_500):
            listed = self.store.list()
        self.assertEqual([row["name"] for row in listed["templates"]], ["Bot 3", "Bot 2"])
        self.assertEqual(listed["skipped"], 2)

    def test_delete(self):
        saved = self.store.save(sample())
        self.assertTrue(self.store.delete(saved["id"]))
        self.assertFalse(self.store.delete(saved["id"]))
        self.assertEqual(self.store.list(), {"templates": [], "skipped": 0})
        with self.assertRaises(MyTemplateError) as raised:
            self.store.delete("../team")
        self.assertEqual(raised.exception.code, "invalid_request")

    def test_a_corrupt_template_can_still_be_deleted(self):
        identifier = str(uuid.uuid4())
        self.write(f"{identifier}.json", "{broken")
        self.assertTrue(self.store.delete(identifier))
        self.assertFalse((self.directory / f"{identifier}.json").exists())

    def test_the_folder_is_under_the_default_hermes_root(self):
        with patch("hermes_constants.get_default_hermes_root", return_value=Path("/private/tmp/hermes-root"),
                   create=True):
            self.assertEqual(my_templates.directory(),
                             Path("/private/tmp/hermes-root/plugin-data/loopdy/my-templates"))


ETAG = '"sha256:' + "b" * 64 + '"'


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(self.folder.cleanup)
        self.directory = Path(self.folder.name) / "my-templates"
        self.features = ("native-context-v1", my_templates.CAPABILITY)
        context = lambda _request: NativeContext(None, None, None, "default", self.features, "runtime")
        for target in (patch.object(native_api, "native_context", side_effect=context),
                       patch.object(my_templates, "directory", return_value=self.directory)):
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
        return self.client.post("/api/plugins/loopdy/native/my-templates/" + operation, json=body, headers=headers)

    def test_save_list_and_delete(self):
        request_id = str(uuid.uuid4())
        saved = self.post("save", {"template": sample()}, request_id=request_id)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.headers["x-loopdy-request-id"], request_id)
        self.assertEqual(saved.headers["etag"], self.etag)
        self.assertEqual(saved.headers["cache-control"], "no-store")
        template = saved.json()["template"]
        self.assertEqual(template["source"], "mine")
        listed = self.post("list", {})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), {"templates": [template], "skipped": 0})
        deleted = self.post("delete", {"id": template["id"]})
        self.assertEqual(deleted.json(), {"id": template["id"], "deleted": True})
        self.assertEqual(self.post("list", {}).json()["templates"], [])

    def test_a_corrupt_template_never_breaks_the_list(self):
        saved = self.post("save", {"template": sample()}).json()["template"]
        (self.directory / f"{uuid.uuid4()}.json").write_text("{corrupt", encoding="utf-8")
        listed = self.post("list", {})
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), {"templates": [saved], "skipped": 1})

    def test_refusals_carry_a_fixed_code(self):
        refused = self.post("save", {"template": sample(instructions="token: abcdefghij1234567890XYZ")})
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(refused.json()["error"]["code"], "secret_scan_blocked")
        self.assertNotIn("abcdefghij", refused.text)
        self.assertEqual(self.post("save", {"template": sample(), "extra": 1}).status_code, 422)
        self.assertEqual(self.post("list", {"agentId": "default"}).status_code, 422)
        self.assertEqual(self.post("delete", {"id": "Not-A-UUID"}).status_code, 422)

    def test_the_context_guards_every_request(self):
        self.assertEqual(self.post("list", {}, etag=False).status_code, 428)
        self.assertEqual(self.post("list", {}, etag='"sha256:' + "0" * 64 + '"').status_code, 412)
        self.assertEqual(self.post("list", {}, request_id="NOT-A-UUID").status_code, 422)

    def test_a_host_without_the_feature_refuses(self):
        self.features = ("native-context-v1",)
        refused = self.post("list", {})
        self.assertEqual(refused.status_code, 503)
        self.assertEqual(refused.json()["error"]["code"], "my_templates_unavailable")


if __name__ == "__main__":
    unittest.main()
