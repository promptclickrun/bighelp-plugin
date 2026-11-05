"""An agent's core files for the app: SOUL.md, USER.md and MEMORY.md in its home, and AGENTS.md in its active
project only where that file exists. Saves are whole, refused when the file changed since the app read it, and
the memory files are written under Hermes' own memory lock. Files are real; the agent's home and project are
temporary folders.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import telemetry_off  # noqa: F401
from loopdy_plugin import core_files, workspace_capabilities
import test_native_api as native_fixtures
from test_native_api import PREFIX


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CoreFilesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name) / "home"
        self.project = Path(folder.name) / "project"
        (self.home / "memories").mkdir(parents=True)
        self.project.mkdir()
        (self.home / "SOUL.md").write_text("You are Lantern, a calm helper.\n")
        (self.home / "memories" / "USER.md").write_text("Likes short answers.")
        self.active = [(self.project, "Garden")]
        for item in (patch.object(core_files, "_home", side_effect=lambda profile: self.home),
                     patch.object(core_files, "_active_project", side_effect=lambda profile: self.active[0]),
                     patch.object(workspace_capabilities, "profile_scope", side_effect=lambda profile: nullcontext())):
            item.start()
            self.addCleanup(item.stop)

    def test_lists_the_files_and_agents_md_only_where_the_project_has_one(self):
        rows = core_files.files("nova")["files"]
        self.assertEqual([(row["kind"], row["exists"]) for row in rows],
                         [("soul", True), ("user", True), ("memory", False)])
        (self.project / "AGENTS.md").write_text("# Rules\n")
        rows = core_files.files("nova")["files"]
        self.assertEqual(rows[-1]["kind"], "agents")
        self.assertEqual(rows[-1]["project"], "Garden")
        self.active[0] = None
        self.assertNotIn("agents", [row["kind"] for row in core_files.files("nova")["files"]])

    def test_reads_whole_files_and_an_empty_one_that_doesnt_exist(self):
        soul = core_files.read("nova", "soul")
        self.assertEqual(soul["text"], "You are Lantern, a calm helper.\n")
        self.assertEqual(soul["sha256"], sha(soul["text"]))
        self.assertFalse(soul["truncated"])
        memory = core_files.read("nova", "memory")
        self.assertEqual((memory["exists"], memory["text"], memory["sha256"]), (False, "", None))
        with self.assertRaises(core_files.CoreFileError) as missing:
            core_files.read("nova", "agents")
        self.assertEqual(missing.exception.reason, "not_found")

    def test_a_save_needs_the_hash_it_read_and_never_overwrites_a_newer_file(self):
        read = core_files.read("nova", "soul")
        saved = core_files.write("nova", "soul", "You are Lantern.\n", read["sha256"])
        self.assertEqual(saved["text"], "You are Lantern.\n")
        self.assertEqual((self.home / "SOUL.md").read_text(), "You are Lantern.\n")
        for stale in (read["sha256"], None):
            with self.assertRaises(core_files.CoreFileError) as changed:
                core_files.write("nova", "soul", "Old draft", stale)
            self.assertEqual(changed.exception.reason, "changed")
        self.assertEqual((self.home / "SOUL.md").read_text(), "You are Lantern.\n")
        self.assertEqual([path.name for path in self.home.iterdir() if path.name.startswith(".")], [],
                         "No temporary file is left")

    def test_a_memory_file_is_made_when_missing_and_saved_under_hermes_memory_lock(self):
        try:
            from tools.memory_tool_store import MemoryStore
        except Exception:
            self.skipTest("this Hermes has no memory store")
        path = self.home / "memories" / "MEMORY.md"
        entered = threading.Event()
        release = threading.Event()

        def hold():
            with MemoryStore._file_lock(path):
                entered.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(entered.wait(5))
        result: list[dict] = []
        saver = threading.Thread(target=lambda: result.append(core_files.write("nova", "memory", "Waters on Sundays.")))
        saver.start()
        saver.join(0.3)
        self.assertTrue(saver.is_alive(), "The save waits for Hermes' memory lock")
        self.assertFalse(path.exists())
        release.set()
        saver.join(5)
        holder.join(5)
        self.assertEqual(path.read_text(), "Waters on Sundays.")
        self.assertEqual(result[0]["sha256"], sha("Waters on Sundays."))

    def test_agents_md_is_saved_only_where_it_exists_and_links_are_refused(self):
        agents = self.project / "AGENTS.md"
        agents.write_text("# Rules\n")
        core_files.write("nova", "agents", "# Rules\n\n- Keep it simple.\n", sha("# Rules\n"))
        self.assertIn("Keep it simple", agents.read_text())

        outside = self.home.parent / "outside.md"
        outside.write_text("secret")
        (self.home / "SOUL.md").unlink()
        os.symlink(outside, self.home / "SOUL.md")
        with self.assertRaises(core_files.CoreFileError) as linked:
            core_files.read("nova", "soul")
        self.assertEqual(linked.exception.reason, "invalid")
        self.assertNotIn("soul", [row["kind"] for row in core_files.files("nova")["files"]])

    def test_agents_md_is_the_chats_not_the_active_projects(self):
        """A chat moved to another project showed the active project's AGENTS.md: Hermes loads the one in the
        chat's own folder, so Core Files shows that one."""
        (self.project / "AGENTS.md").write_text("# Rules of A\n")
        moved = self.home.parent / "project-b"
        moved.mkdir()
        (moved / "AGENTS.md").write_text("# Rules of B\n")
        with patch.object(core_files, "_chat_folder", return_value=moved) as folder:
            rows = core_files.files("nova", "local-chat")["files"]
            self.assertEqual(rows[-1]["project"], "project-b")
            read = core_files.read("nova", "agents", "local-chat")
            self.assertEqual(read["text"], "# Rules of B\n")
            core_files.write("nova", "agents", "# Rules of B, edited\n", read["sha256"], "local-chat")
        folder.assert_called_with("nova", "local-chat")
        self.assertEqual((moved / "AGENTS.md").read_text(), "# Rules of B, edited\n")
        self.assertEqual((self.project / "AGENTS.md").read_text(), "# Rules of A\n")
        self.assertEqual(core_files.read("nova", "agents")["text"], "# Rules of A\n", "Without a chat: the active project")
        with patch.object(core_files, "_chat_folder", return_value=None):
            self.assertNotIn("agents", [row["kind"] for row in core_files.files("nova", "gone")["files"]])

    def test_the_deepest_agents_md_on_the_chats_git_chain_wins(self):
        repo = self.home.parent / "repo"
        nested = repo / "apps" / "garden"
        nested.mkdir(parents=True)
        (repo / ".git").mkdir()
        (repo / "AGENTS.md").write_text("# Repo rules\n")
        with patch.object(core_files, "_chat_folder", return_value=nested):
            self.assertEqual(core_files.read("nova", "agents", "local-chat")["text"], "# Repo rules\n")
            (nested / "AGENTS.md").write_text("# Garden rules\n")
            self.assertEqual(core_files.read("nova", "agents", "local-chat")["text"], "# Garden rules\n")

    def test_a_chats_folder_is_read_from_the_agents_state_and_its_live_part(self):
        try:
            from hermes_state import SessionDB
        except Exception:
            self.skipTest("this Hermes has no session store")
        first, moved = self.home.parent / "first", self.home.parent / "moved"
        first.mkdir()
        moved.mkdir()
        db = SessionDB(db_path=self.home / "state.db")
        try:
            db.create_session("chat-1", "bighelp", cwd=str(first), profile_name="nova")
            db.create_session("other", "bighelp", cwd=str(first), profile_name="sage")
            self.assertEqual(core_files._chat_folder("nova", "chat-1"), first)
            db.end_session("chat-1", "compression")
            db.create_session("chat-2", "bighelp", parent_session_id="chat-1", cwd=str(moved), profile_name="nova")
        finally:
            db.close()
        self.assertEqual(core_files._chat_folder("nova", "chat-1"), moved, "A compressed chat goes on in its live part")
        self.assertIsNone(core_files._chat_folder("nova", "other"), "Another agent's chat")
        self.assertIsNone(core_files._chat_folder("nova", "missing"))

    def test_too_long_and_unknown_kinds_are_refused(self):
        with self.assertRaises(core_files.CoreFileError) as long:
            core_files.write("nova", "user", "x" * (core_files.MAX_BYTES + 1), sha("Likes short answers."))
        self.assertEqual(long.exception.reason, "too_large")
        with self.assertRaises(core_files.CoreFileError) as unknown:
            core_files.read("nova", "config")
        self.assertEqual(unknown.exception.reason, "invalid")


class CoreFilesRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def call(self, route, body):
        return self.fixture.client.post(PREFIX + "/core-files/" + route, json=body, headers=self.fixture.headers())

    def test_routes_answer_and_refuse(self):
        if not core_files.available():
            self.skipTest("this Hermes has no profiles")
        self.assertIn(core_files.CAPABILITY, self.fixture.context().json()["features"])
        with patch.object(core_files, "files", return_value={"files": [{"kind": "soul", "name": "SOUL.md",
                                                                        "exists": True, "size": 4}]}) as listed:
            result = self.call("list", {"profile": "nova"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()["files"][0]["name"], "SOUL.md")
        listed.assert_called_once_with("nova", None)
        self.assertIn(core_files.CHAT_CAPABILITY, self.fixture.context().json()["features"])
        with patch.object(core_files, "files", return_value={"files": []}) as listed:
            self.assertEqual(self.call("list", {"profile": "nova", "sessionId": "local-1a2b"}).status_code, 200)
        listed.assert_called_once_with("nova", "local-1a2b")
        with patch.object(core_files, "write", side_effect=core_files.CoreFileError("changed")):
            changed = self.call("write", {"profile": "nova", "kind": "soul", "text": "x", "expectedSha256": "a" * 64})
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json()["error"]["code"], "file_changed")
        for route, body in (("read", {"profile": "nova", "kind": "config"}), ("read", {"profile": "../x", "kind": "soul"}),
                            ("write", {"profile": "nova", "kind": "soul", "text": "x", "expectedSha256": "nothex"}),
                            ("list", {"profile": "nova", "path": "/etc/passwd"}),
                            ("list", {"profile": "nova", "sessionId": "../state.db"})):
            self.assertEqual(self.call(route, body).status_code, 422, body)


if __name__ == "__main__":
    unittest.main()
