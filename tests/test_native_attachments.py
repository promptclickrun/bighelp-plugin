"""Native agent-attachment provenance and transport."""
from __future__ import annotations

import base64
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loopdy_plugin import native_attachments as na
from loopdy_plugin.attachments import AttachmentStore


def _db(path: Path, rows: list[tuple[str, str, str]], parents: dict[str, str | None]) -> None:
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)")
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT)")
        c.executemany("INSERT INTO sessions VALUES (?, ?)", parents.items())
        c.executemany("INSERT INTO messages (session_id, role, content) VALUES (?, ?, ?)", rows)


class NativeAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.pdf = root / "Report.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 5000)
        self.secret = root / "other.pdf"
        self.secret.write_bytes(b"%PDF-1.4 other")
        self.db = root / "state.db"
        _db(self.db, [
            ("parent", "assistant", f"Here you go.\nMEDIA://{self.pdf}"),
            ("child", "user", f"MEDIA:{self.secret}"),
        ], {"parent": None, "child": "parent"})
        na._store = AttachmentStore(root / "a.sqlite3")
        self.addCleanup(setattr, na, "_store", None)
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(na, "_state_db", return_value=self.db)
        p.start()
        self.addCleanup(p.stop)

    def test_resolves_emitted_path_through_compaction_lineage_including_double_slash(self):
        body = na._Resolve(agentId="default", storedId="child",
                           items=[na._Item(itemId="m1", text=f"Here you go.\nMEDIA://{self.pdf}")])
        [item] = na.resolve(body)["items"]
        self.assertEqual(item["text"], "Here you go.")
        [attachment] = item["attachments"]
        self.assertEqual((attachment["fileName"], attachment["mimeType"]), ("Report.pdf", "application/pdf"))
        self.assertNotIn(self.tmp.name, repr(item))

        na.MAX_CHUNK_BYTES, saved = 2048, na.MAX_CHUNK_BYTES
        self.addCleanup(setattr, na, "MAX_CHUNK_BYTES", saved)
        data, offset = b"", 0
        while offset is not None:
            chunk = na.fetch(na._Fetch(agentId="default", attachmentId=attachment["id"], offset=offset))
            data += base64.b64decode(chunk["data"])
            offset = chunk["nextOffset"]
        self.assertEqual(data, self.pdf.read_bytes())

    def test_refuses_paths_no_assistant_emitted_even_when_the_client_names_them(self):
        body = na._Resolve(agentId="default", storedId="child", items=[
            na._Item(itemId="m1", text=f"MEDIA:{self.secret}"),
            na._Item(itemId="m2", text=f"MEDIA:{self.pdf}\nMEDIA:{self.secret}"),
        ])
        first, second = na.resolve(body)["items"]
        self.assertEqual(first["attachments"], [])
        self.assertEqual([a["fileName"] for a in second["attachments"]], ["Report.pdf"])

    def test_unknown_session_and_other_profile_fail_closed(self):
        body = na._Resolve(agentId="default", storedId="nope",
                           items=[na._Item(itemId="m1", text=f"MEDIA:{self.pdf}")])
        self.assertEqual(na.resolve(body)["items"][0]["attachments"], [])
        [item] = na.resolve(na._Resolve(agentId="default", storedId="parent",
                                        items=[na._Item(itemId="m1", text=f"MEDIA:{self.pdf}")]))["items"]
        with self.assertRaises(na.NativeAPIError):
            na.fetch(na._Fetch(agentId="other", attachmentId=item["attachments"][0]["id"], offset=0))


class RecentMediaTests(unittest.TestCase):
    """The Media page: newest pictures and videos the agent sent or generated."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.old = root / "old.png"
        self.new = root / "new.png"
        self.clip = root / "clip.mp4"
        self.doc = root / "notes.pdf"
        self.generated = root / "generated.png"
        for path in (self.old, self.new, self.clip, self.doc, self.generated):
            path.write_bytes(b"fixture-" + path.name.encode())
        self.db = root / "state.db"
        with sqlite3.connect(self.db) as c:
            c.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)")
            c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                      "content TEXT, tool_name TEXT, timestamp REAL)")
            c.executemany("INSERT INTO messages (session_id, role, content, tool_name, timestamp) VALUES (?, ?, ?, ?, ?)", [
                ("s1", "assistant", f"Old one\nMEDIA:{self.old}", None, 100.0),
                ("s1", "assistant", f"A doc\nMEDIA:{self.doc}", None, 200.0),
                ("s2", "assistant", f"Two things\nMEDIA:{self.new}\nMEDIA:{self.clip}", None, 300.0),
                ("s2", "tool", '{"success": true, "image": "%s"}' % self.generated, "image_generate", 400.0),
                ("s2", "tool", '{"success": false, "image": "%s"}' % self.old, "image_generate", 500.0),
                ("s3", "user", f"MEDIA:{self.old}", None, 600.0),
            ])
        na._store = AttachmentStore(root / "a.sqlite3")
        self.addCleanup(setattr, na, "_store", None)
        p = patch.object(na, "_state_db", return_value=self.db)
        p.start()
        self.addCleanup(p.stop)

    def test_lists_newest_delivered_and_generated_media_only(self):
        items = na.recent(na._Recent(agentId="default", limit=10))["items"]
        self.assertEqual([item["fileName"] for item in items], ["generated.png", "new.png", "clip.mp4", "old.png"])
        self.assertEqual([item["storedId"] for item in items], ["s2", "s2", "s2", "s1"])
        self.assertTrue(all(item["mimeType"].startswith(("image/", "video/")) for item in items))
        self.assertNotIn(self.tmp.name, repr(items))
        chunk = na.fetch(na._Fetch(agentId="default", attachmentId=items[1]["id"], offset=0))
        self.assertEqual(base64.b64decode(chunk["data"]), self.new.read_bytes())

    def test_limit_and_missing_database(self):
        self.assertEqual(len(na.recent(na._Recent(agentId="default", limit=2))["items"]), 2)
        self.db.unlink()
        self.assertEqual(na.recent(na._Recent(agentId="default", limit=5)), {"items": []})


class BoardFileTests(unittest.TestCase):
    """Files a Feed post refers to reach the app through the same store and chunks as chat files."""

    def setUp(self):
        from loopdy_plugin.agent_board import BoardStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.pdf = root / "Trip plan.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\n" + b"y" * 5000)
        self.board = BoardStore(root / "board")
        self.post = self.board.publish("feed", title="Lisbon", files=[str(self.pdf)], item_id="lisbon")
        na._store = AttachmentStore(root / "a.sqlite3")
        self.addCleanup(setattr, na, "_store", None)
        for target, value in (("_board_store", self.board), ("_profile_exists", True)):
            p = patch.object(na, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def resolve(self, item_id="lisbon", index=0):
        return na.board(na._Board(agentId="default", itemId=item_id, index=index))

    def test_a_posts_file_resolves_to_an_opaque_id_and_downloads_in_chunks(self):
        attachment = self.resolve()["attachment"]
        self.assertEqual((attachment["fileName"], attachment["mimeType"], attachment["byteCount"]),
                         ("Trip plan.pdf", "application/pdf", 5009))
        self.assertNotIn(self.tmp.name, repr(attachment))
        na.MAX_CHUNK_BYTES, saved = 2048, na.MAX_CHUNK_BYTES
        self.addCleanup(setattr, na, "MAX_CHUNK_BYTES", saved)
        data, offset = b"", 0
        while offset is not None:
            chunk = na.fetch(na._Fetch(agentId="default", attachmentId=attachment["id"], offset=offset))
            data += base64.b64decode(chunk["data"])
            offset = chunk["nextOffset"]
        self.assertEqual(data, self.pdf.read_bytes())
        with self.assertRaises(na.NativeAPIError):
            na.fetch(na._Fetch(agentId="other", attachmentId=attachment["id"], offset=0))

    def test_only_files_a_post_refers_to_and_the_policy_allows(self):
        for item_id, index in (("lisbon", 1), ("missing", 0)):
            with self.subTest(item_id=item_id, index=index), self.assertRaises(na.NativeAPIError) as raised:
                self.resolve(item_id, index)
            self.assertEqual(raised.exception.code, "attachment_unavailable")
        idea = self.board.publish("idea", title="Not a post", item_id="idea")
        with self.assertRaises(na.NativeAPIError):
            self.resolve(idea["id"], 0)
        # Checked again on every fetch: a file the policy refuses now isn't served.
        from gateway.platforms.base import BasePlatformAdapter
        with patch.object(BasePlatformAdapter, "validate_media_delivery_path", return_value=None), \
                self.assertRaises(na.NativeAPIError):
            self.resolve()

    def test_reattaching_serves_the_new_file_and_a_missing_one_is_unavailable(self):
        first = self.resolve()["attachment"]
        # Rating or reading a post doesn't copy its files again.
        self.board.set_flags("lisbon", rating="up", read=True)
        self.assertEqual(self.resolve()["attachment"]["id"], first["id"])
        self.pdf.write_bytes(b"%PDF-1.4\n" + b"z" * 100)
        self.board.publish("feed", title="Lisbon, updated", files=[str(self.pdf)], item_id="lisbon",
                           now=self.post["updatedAt"] + 60)
        second = self.resolve()["attachment"]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["byteCount"], 109)
        gone = self.pdf.with_name("gone.pdf")
        gone.write_bytes(b"%PDF-1.4\n")
        self.board.publish("feed", title="Moved", files=[str(gone)], item_id="moved")
        gone.unlink()
        with self.assertRaises(na.NativeAPIError):
            self.resolve("moved", 0)

    def test_requests_are_strict(self):
        for bad in ({"itemId": "../x", "index": 0}, {"itemId": "lisbon", "index": 10},
                    {"itemId": "lisbon", "index": "0"}, {"itemId": "lisbon", "index": 0, "path": "/etc"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                na._Board(agentId="default", **bad)


if __name__ == "__main__":
    unittest.main()
