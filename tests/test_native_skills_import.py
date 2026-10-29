"""Skill import: a .zip skill folder or one SKILL.md added to an agent, with Hermes' own skill writers.

Runs on a scratch Hermes home. Every refusal must leave the agent's skills exactly as they were.
"""
from __future__ import annotations
import telemetry_off  # noqa: F401  (no test run posts host telemetry)

import base64
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from loopdy_plugin import native_skills_import as skills_import
from loopdy_plugin.native_skills_import import SkillImportError, import_skill
from hermes_process_state import single_profile_host
import test_native_api as native_fixtures
from test_native_api import PREFIX

SKILL_MD = "---\nname: trip-notes\ndescription: Plan a trip. Use when asked about travel.\n---\n\n# Trip notes\n\nAsk where.\n"
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(range(256))


def encode(raw: bytes | str) -> str:
    return base64.b64encode(raw.encode() if isinstance(raw, str) else raw).decode()


def make_zip(entries: dict[str, bytes | str], *, symlinks: tuple[str, ...] = (), compress=zipfile.ZIP_DEFLATED) -> bytes:
    """Entries are written as given, so a test can build a path Python's own tools would refuse."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compress) as archive:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = compress
            info.create_system = 3
            info.external_attr = ((stat.S_IFLNK | 0o777) if name in symlinks else (stat.S_IFREG | 0o644)) << 16
            if name.endswith("/"):
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def skill_md(name: str = "trip-notes") -> str:
    return SKILL_MD.replace("trip-notes", name)


class SkillImportTests(unittest.TestCase):
    def setUp(self):
        single_profile_host(self)
        folder = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        (self.root / "config.yaml").write_text("model: {}\n", encoding="utf-8")
        home = self.root / "profiles" / "nova"
        home.mkdir(parents=True)
        (home / "config.yaml").write_text("model: {}\n", encoding="utf-8")
        self.skills = home / "skills"
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.root), "HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        cache = patch.object(skills_import, "_clear_prompt_cache")
        self.cleared = cache.start()
        self.addCleanup(cache.stop)

    def upload(self, kind, raw, category=None, agent="nova"):
        return import_skill(agent, kind, encode(raw), category)

    def refused(self, kind, raw, code, category=None, status=None):
        with self.assertRaises(SkillImportError) as caught:
            self.upload(kind, raw, category)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        if status:
            self.assertEqual(caught.exception.status, status)
        self.assertEqual(self.on_disk(), [], "A refusal leaves nothing behind")
        self.cleared.assert_not_called()
        return caught.exception

    def on_disk(self):
        return sorted(str(path.relative_to(self.skills)) for path in self.skills.rglob("*")
                      if ".locks" not in path.parts) if self.skills.exists() else []

    # MARK: - Accepting

    def test_a_skill_md_takes_its_name_from_the_frontmatter_and_the_category(self):
        result = self.upload("skillMd", SKILL_MD, " travel ")
        self.assertEqual(result, {"name": "trip-notes", "category": "travel", "files": ["SKILL.md"]})
        self.assertEqual((self.skills / "travel" / "trip-notes" / "SKILL.md").read_text(encoding="utf-8"), SKILL_MD)
        self.cleared.assert_called_once()

    def test_a_byte_order_mark_is_dropped(self):
        self.upload("skillMd", b"\xef\xbb\xbf" + SKILL_MD.encode())
        self.assertEqual((self.skills / "trip-notes" / "SKILL.md").read_bytes(), SKILL_MD.encode())

    def test_a_zip_with_scripts_references_and_a_binary_asset(self):
        archive = make_zip({"SKILL.md": SKILL_MD, "scripts/x.py": "print('hi')\n", "references/a.md": "# A\n",
                            "templates/t.txt": "hello\n", "assets/logo.png": PNG, "assets/notes.json": '{"a": 1}'})
        result = self.upload("zip", archive)
        self.assertEqual(result, {"name": "trip-notes", "category": None, "files": [
            "SKILL.md", "assets/logo.png", "assets/notes.json", "references/a.md", "scripts/x.py", "templates/t.txt"]})
        folder = self.skills / "trip-notes"
        self.assertEqual((folder / "assets/logo.png").read_bytes(), PNG)
        self.assertEqual((folder / "scripts/x.py").read_text(encoding="utf-8"), "print('hi')\n")
        self.assertEqual((folder / "assets/notes.json").read_text(encoding="utf-8"), '{"a": 1}')
        self.cleared.assert_called_once()

    def test_a_zip_with_one_top_folder_drops_the_folder_and_the_mac_junk(self):
        archive = make_zip({"trip-notes/": b"", "trip-notes/SKILL.md": SKILL_MD, "trip-notes/references/": b"",
                            "trip-notes/references/a.md": "# A\n", "trip-notes/.DS_Store": b"junk",
                            "__MACOSX/trip-notes/._SKILL.md": b"junk", "__MACOSX/": b""})
        result = self.upload("zip", archive, "travel")
        self.assertEqual(result["files"], ["SKILL.md", "references/a.md"])
        self.assertEqual(self.on_disk(), ["travel", "travel/trip-notes", "travel/trip-notes/SKILL.md",
                                          "travel/trip-notes/references", "travel/trip-notes/references/a.md"])

    # MARK: - Refusing (nothing is written)

    def test_a_skill_md_that_hermes_would_not_take(self):
        for text, code in (("no frontmatter", "invalid_skill"),
                           ("---\ndescription: x\n---\nbody\n", "invalid_skill"),
                           ("---\nname: Bad Name\ndescription: x\n---\nbody\n", "invalid_skill"),
                           ("---\nname: 7\ndescription: x\n---\nbody\n", "invalid_skill"),
                           ("---\nname: ok\ndescription: x\n---\n", "invalid_skill"),
                           (SKILL_MD + "x" * 100_001, "too_large"), (b"\xff\xfe\x00", "invalid_skill")):
            with self.subTest(text=text[:30]):
                self.refused("skillMd", text, code)
        error = self.refused("skillMd", SKILL_MD, "invalid_skill", category="Bad/Category")
        self.assertIn("Invalid category", error.details["reason"])
        with self.assertRaises(SkillImportError) as caught:
            import_skill("nova", "skillMd", "not base64!", None)
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_an_archive_that_is_not_safe(self):
        good = {"SKILL.md": SKILL_MD}
        cases = {
            "traversal": ({**good, "references/../../evil.md": "x"}, "invalid_archive"),
            "traversal above": ({**good, "../evil.md": "x"}, "invalid_archive"),
            "absolute": ({**good, "/etc/evil.md": "x"}, "invalid_archive"),
            "backslash": ({**good, "scripts\\..\\evil.py": "x"}, "invalid_archive"),
            "drive": ({**good, "C:/evil.md": "x"}, "invalid_archive"),
            "double slash": ({**good, "references//a.md": "x"}, "invalid_archive"),
            "duplicate": ({**good, "references/a.md": "x", "references/A.md": "y"}, "invalid_archive"),
        }
        for label, (entries, code) in cases.items():
            with self.subTest(label):
                self.refused("zip", make_zip(entries), code)
        with self.subTest("symlink"):
            self.refused("zip", make_zip({**good, "references/link": "/etc/passwd"}, symlinks=("references/link",)),
                         "invalid_archive")
        with self.subTest("not a zip"):
            self.refused("zip", b"PK\x03\x04 nope", "invalid_archive")
        with self.subTest("duplicate path"):
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive, self.assertWarns(UserWarning):
                for _ in range(2):
                    archive.writestr("SKILL.md", SKILL_MD)
            self.refused("zip", buffer.getvalue(), "invalid_archive")

    def test_an_archive_with_the_wrong_layout(self):
        cases = {
            "missing SKILL.md": ({"references/a.md": "x"}, "missing_skill_md"),
            "only junk": ({"__MACOSX/x": "x", ".DS_Store": "x"}, "missing_skill_md"),
            "SKILL.md two folders deep": ({"a/b/SKILL.md": SKILL_MD}, "unsupported_layout"),
            "two top folders": ({"a/SKILL.md": SKILL_MD, "b/references/x.md": "x"}, "unsupported_layout"),
            "file outside the allowed folders": ({"SKILL.md": SKILL_MD, "README.md": "x"}, "unsupported_layout"),
            "unknown folder": ({"SKILL.md": SKILL_MD, "docs/a.md": "x"}, "unsupported_layout"),
            "a second SKILL.md": ({"SKILL.md": SKILL_MD, "references/SKILL.md": SKILL_MD}, "unsupported_layout"),
            "a folder name, not a file": ({"SKILL.md": SKILL_MD, "scripts": "x"}, "unsupported_layout"),
            "binary outside assets": ({"SKILL.md": SKILL_MD, "scripts/tool.bin": b"\xff\x00\xfe"}, "binary_not_allowed"),
            "SKILL.md not text": ({"SKILL.md": b"\xff\xfe\x00"}, "invalid_skill"),
        }
        for label, (entries, code) in cases.items():
            with self.subTest(label):
                self.refused("zip", make_zip(entries), code)

    def test_size_limits_and_zip_bombs(self):
        megabyte = b"\0" * (1024 * 1024)
        cases = {
            "one file over 1 MiB": {"SKILL.md": SKILL_MD, "assets/big.bin": megabyte + b"\0"},
            "more than 16 MiB together": {"SKILL.md": SKILL_MD, **{f"assets/{n}.bin": megabyte for n in range(17)}},
            "more than 200 files": {"SKILL.md": SKILL_MD, **{f"references/{n}.md": "x" for n in range(200)}},
            "SKILL.md too long": {"SKILL.md": SKILL_MD + "x" * 100_000},
            "text file too long": {"SKILL.md": SKILL_MD, "references/long.md": "x" * 100_001},
        }
        for label, entries in cases.items():
            archive = make_zip(entries)
            with self.subTest(label):
                self.assertLess(len(archive), 100_000, "A bomb is small until it is read")
                self.refused("zip", archive, "too_large", status=413)
        with self.subTest("upload over 3 MiB"):
            self.refused("zip", os.urandom(3 * 1024 * 1024 + 1), "too_large", status=413)
        with self.subTest("a lie about the size does not make a bigger read"):
            archive = bytearray(make_zip({"SKILL.md": SKILL_MD, "references/a.md": "x" * 5000}))
            for marker in (b"PK\x03\x04", b"PK\x01\x02"):
                position = archive.rfind(marker)
                offset = 22 if marker == b"PK\x03\x04" else 24  # the uncompressed size, which now says 10
                archive[position + offset:position + offset + 4] = (10).to_bytes(4, "little")
            self.refused("zip", bytes(archive), "invalid_archive")

    def test_a_skill_that_is_already_there_is_never_overwritten(self):
        self.upload("skillMd", SKILL_MD)
        before = (self.skills / "trip-notes" / "SKILL.md").read_bytes()
        self.cleared.reset_mock()
        for kind, raw in (("skillMd", skill_md().replace("Ask where.", "Changed.")),
                          ("zip", make_zip({"SKILL.md": skill_md().replace("Ask where.", "Changed."),
                                            "references/a.md": "x"}))):
            with self.subTest(kind), self.assertRaises(SkillImportError) as caught:
                self.upload(kind, raw, "travel")
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "skill_exists"))
        self.assertEqual((self.skills / "trip-notes" / "SKILL.md").read_bytes(), before)
        self.assertEqual(self.on_disk(), ["trip-notes", "trip-notes/SKILL.md"])
        self.cleared.assert_not_called()

    def test_a_blocked_scan_deletes_the_new_skill(self):
        archive = make_zip({"SKILL.md": SKILL_MD, "references/a.md": "# A\n", "scripts/x.py": "print('hi')\n",
                            "assets/logo.png": PNG})

        def scan_when(path):
            def scan(skill_dir):
                return "Security scan blocked this skill (1 finding):\nreport for " + str(skill_dir) \
                    if (Path(skill_dir) / path).exists() else None
            return scan

        for label, trigger in (("a text file", "scripts/x.py"), ("the binary asset", "assets/logo.png")):
            with self.subTest(label), patch("tools.skill_manager_tool._security_scan_skill", scan_when(trigger)), \
                    self.assertRaises(SkillImportError) as caught:
                self.upload("zip", archive, "travel")
            self.assertEqual((caught.exception.status, caught.exception.code), (422, "blocked"))
            self.assertNotIn(str(self.root), repr(caught.exception.details), "No host path reaches the app")
            self.assertEqual(self.on_disk(), [], "The skill and its empty category folder are gone")
            self.cleared.assert_called()
            self.cleared.reset_mock()

    def test_a_failing_write_deletes_the_new_skill(self):
        archive = make_zip({"SKILL.md": SKILL_MD, "references/a.md": "# A\n", "assets/logo.png": PNG})
        with patch.object(skills_import, "_write_binary", side_effect=OSError("/private/secret path")), \
                self.assertRaises(OSError):
            self.upload("zip", archive)
        self.assertEqual(self.on_disk(), [])

    def test_an_agent_that_is_gone(self):
        with self.assertRaises(SkillImportError) as caught:
            self.upload("skillMd", SKILL_MD, agent="ghost")
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "profile_not_found"))


class SkillImportRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        (self.fixture.home / "profiles/research/skills").mkdir()
        self.skills = self.fixture.home / "profiles/research/skills"

    def call(self, body, headers=None):
        return self.fixture.client.post(PREFIX + "/skills/import", json=body,
                                        headers=self.fixture.headers() if headers is None else headers)

    def body(self, kind="zip", raw=b"", **extra):
        return {"agentId": "research", "fileName": "trip-notes.zip", "kind": kind, "data": encode(raw), **extra}

    def test_the_feature_is_advertised_only_when_it_is_available(self):
        for available in (True, False):
            with self.subTest(available=available), patch.object(skills_import, "available", return_value=available):
                features = self.fixture.context().json()["features"]
                self.assertEqual(skills_import.CAPABILITY in features, available)
        with patch.object(skills_import, "available", return_value=False):
            result = self.call(self.body("skillMd", SKILL_MD), self.fixture.headers())
        self.assertEqual((result.status_code, result.json()["error"]["code"]), (503, "skill_import_unavailable"))
        self.assertTrue(skills_import.available())
        with patch("tools.skill_manager_tool._delete_skill", None):
            self.assertFalse(skills_import.available(), "Hermes without one of its writers turns the feature off")

    def test_a_big_archive_imports_and_a_refusal_is_a_plain_error(self):
        if not skills_import.available():
            self.skipTest("this Hermes has no skill writers")
        blob = os.urandom(900_000)  # a body far over the 192 KiB limit of other routes
        archive = make_zip({"SKILL.md": SKILL_MD, "references/a.md": "# A\n", "assets/logo.png": PNG,
                            "assets/blob.bin": blob}, compress=zipfile.ZIP_STORED)
        self.assertGreater(len(encode(archive)), 1_000_000)
        with patch.object(skills_import, "_clear_prompt_cache"):
            result = self.call(self.body(raw=archive, category="travel"))
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json(), {"name": "trip-notes", "category": "travel",
                                             "files": ["SKILL.md", "assets/blob.bin", "assets/logo.png", "references/a.md"]})
            self.assertEqual(result.headers["cache-control"], "no-store")
            self.assertEqual((self.skills / "travel/trip-notes/assets/blob.bin").read_bytes(), blob)
            again = self.call(self.body("skillMd", SKILL_MD))
        self.assertEqual((again.status_code, again.json()["error"]["code"]), (409, "skill_exists"))
        self.assertNotIn(str(self.fixture.home), again.text)

    def test_bad_requests(self):
        headers = self.fixture.headers()
        for label, body in (("unknown field", {**self.body("skillMd", SKILL_MD), "extra": 1}),
                            ("unknown kind", self.body("tar", b"x")),
                            ("bad agent", {**self.body("skillMd", SKILL_MD), "agentId": "../x"}),
                            ("empty name", {**self.body("skillMd", SKILL_MD), "fileName": ""}),
                            ("not base64", {**self.body("skillMd", SKILL_MD), "data": "***"}),
                            ("data too long", {**self.body("skillMd", SKILL_MD), "data": "A" * 4_194_308})):
            with self.subTest(label):
                result = self.call(body, headers)
                self.assertIn(result.status_code, (413, 422), result.text)
        with self.subTest("missing context"):
            self.assertEqual(self.call(self.body("skillMd", SKILL_MD), {"Authorization": headers["Authorization"]}).status_code, 428)
        with self.subTest("agent that is gone"):
            result = self.call({**self.body("skillMd", SKILL_MD), "agentId": "ghost"})
            self.assertEqual((result.status_code, result.json()["error"]["code"]), (404, "profile_not_found"))
