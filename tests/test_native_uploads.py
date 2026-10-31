"""Big files from the phone: pieces, resume, bounds, cleanup and the native route."""
from __future__ import annotations

import telemetry_off  # noqa: F401  (no test run posts host telemetry)

import asyncio
import errno
import hashlib
import importlib
import json
import sys
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loopdy_plugin import native_uploads as nu
from loopdy_plugin.native_context import NativeAPIError
import test_native_api
from test_native_api import PREFIX

OWNER = "fixture\x1falice"
GB = 1024 * 1024 * 1024


def _start(store, data: bytes, *, name="Clip.mov", owner=OWNER, agent="default", mime="video/quicktime"):
    return store.start(nu._Start(agentId=agent, fileName=name, mimeType=mime, byteCount=len(data),
                                 sha256=hashlib.sha256(data).hexdigest()), owner)


def _send(store, upload, data: bytes, index: int, owner=OWNER):
    size = nu.CHUNK_BYTES
    piece = data[index * size:(index + 1) * size]
    store.begin_chunk(upload["uploadId"], owner, index * size, len(piece))
    return store.write_chunk(upload["uploadId"], owner, index, piece, hashlib.sha256(piece).hexdigest())


class UploadStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "uploads"
        self.clock = [1_000_000.0]
        self.free = [100 * GB]
        self.workspace = Path(temporary.name) / "workspace"
        self.workspace.mkdir()
        self.workspaces = {"default": self.workspace}
        self.store = nu.UploadStore(self.root, now=lambda: self.clock[0],
                                    disk_usage=lambda _path: SimpleNamespace(free=self.free[0]),
                                    workspace=lambda agent: self.workspaces.get(agent))
        # Small pieces keep the tests fast; the math is the same.
        chunk = patch.object(nu, "CHUNK_BYTES", 1024)
        chunk.start()
        self.addCleanup(chunk.stop)
        self.data = os.urandom(1024 * 3 + 100)

    def test_piece_math_has_a_short_last_piece(self):
        self.assertEqual(nu.chunk_count(1024 * 3 + 100), 4)
        self.assertEqual([nu.chunk_length(1024 * 3 + 100, index) for index in range(4)], [1024, 1024, 1024, 100])
        self.assertEqual(nu.chunk_count(2048), 2)
        self.assertEqual(nu.chunk_length(2048, 1), 1024)
        with self.assertRaises(ValueError):
            nu.chunk_length(2048, 2)

    def test_pieces_in_any_order_make_the_file_and_finish_checks_it(self):
        upload = _start(self.store, self.data)
        self.assertEqual((upload["receivedChunks"], upload["receivedBytes"], upload["chunkBytes"]), ([], 0, 1024))
        for index in (3, 1, 0, 2):
            answer = _send(self.store, upload, self.data, index)
        self.assertEqual((answer["receivedChunks"], answer["receivedBytes"]), ([0, 1, 2, 3], len(self.data)))
        finished = self.store.finish(upload["uploadId"], OWNER)
        path = Path(finished["path"])
        self.assertEqual(path.name, "Clip.mov")
        self.assertEqual(path.parent.parent, self.workspace / "uploads")
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        # Finishing twice gives the same answer; the phone may not have heard the first.
        self.assertEqual(self.store.finish(upload["uploadId"], OWNER)["path"], finished["path"])

    def test_resume_after_a_drop_keeps_only_whole_pieces(self):
        upload = _start(self.store, self.data)
        _send(self.store, upload, self.data, 0)
        _send(self.store, upload, self.data, 1)
        # Piece 2 started and the connection dropped: nothing is recorded for it.
        self.store.begin_chunk(upload["uploadId"], OWNER, 2048, 1024)
        status = self.store.status(upload["uploadId"], OWNER)
        self.assertEqual((status["receivedChunks"], status["receivedBytes"]), ([0, 1], 2048))
        with self.assertRaises(NativeAPIError) as early:
            self.store.finish(upload["uploadId"], OWNER)
        self.assertEqual((early.exception.status, early.exception.code), (409, "upload_incomplete"))
        # The phone starts the same file over after a relaunch: it gets the same upload back.
        again = _start(self.store, self.data)
        self.assertEqual((again["uploadId"], again["receivedChunks"]), (upload["uploadId"], [0, 1]))
        _send(self.store, upload, self.data, 2)
        _send(self.store, upload, self.data, 3)
        self.assertEqual(Path(self.store.finish(upload["uploadId"], OWNER)["path"]).read_bytes(), self.data)

    def test_pieces_must_be_whole_pieces_of_this_file(self):
        upload = _start(self.store, self.data)
        for offset, length, code in ((1, 1024, "upload_offset_invalid"), (4096, 100, "upload_offset_invalid"),
                                     (0, 1000, "upload_length_invalid"), (3072, 1024, "upload_length_invalid")):
            with self.subTest(offset=offset, length=length), self.assertRaises(NativeAPIError) as caught:
                self.store.begin_chunk(upload["uploadId"], OWNER, offset, length)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(NativeAPIError) as damaged:
            self.store.write_chunk(upload["uploadId"], OWNER, 0, self.data[:1024], "0" * 64)
        self.assertEqual(damaged.exception.code, "upload_chunk_corrupt")
        self.assertEqual(self.store.status(upload["uploadId"], OWNER)["receivedChunks"], [])

    def test_a_file_that_does_not_match_its_checksum_starts_over(self):
        wrong = bytearray(self.data)
        wrong[5] ^= 0xFF
        upload = _start(self.store, self.data)
        for index in range(4):
            piece = bytes(wrong[index * 1024:(index + 1) * 1024])
            self.store.write_chunk(upload["uploadId"], OWNER, index, piece, hashlib.sha256(piece).hexdigest())
        with self.assertRaises(NativeAPIError) as caught:
            self.store.finish(upload["uploadId"], OWNER)
        self.assertEqual((caught.exception.status, caught.exception.code), (422, "upload_corrupt"))
        self.assertEqual(self.store.status(upload["uploadId"], OWNER)["receivedChunks"], [])

    def test_free_space_is_checked_before_anything_is_written(self):
        self.free[0] = 2 * len(self.data) + nu.FREE_SPACE_RESERVE_BYTES - 1
        with self.assertRaises(NativeAPIError) as caught:
            _start(self.store, self.data)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "upload_no_space"))
        self.assertEqual(list(self.root.iterdir()), [])
        self.free[0] += 1
        _start(self.store, self.data)

    def test_size_and_count_are_bounded(self):
        with self.assertRaises(Exception):
            nu._Start(agentId="default", fileName="big.mov", mimeType="video/quicktime",
                      byteCount=nu.MAX_UPLOAD_BYTES + 1, sha256="0" * 64)
        self.assertGreaterEqual(nu.MAX_UPLOAD_BYTES, 100 * 1000 * 1000)
        for number in range(nu.MAX_ACTIVE_UPLOADS):
            _start(self.store, self.data, name=f"clip-{number}.mov", owner=f"fixture\x1fperson-{number}")
        with self.assertRaises(NativeAPIError) as caught:
            _start(self.store, self.data, name="one-more.mov", owner="fixture\x1fsomeone-else")
        self.assertEqual((caught.exception.status, caught.exception.code), (429, "uploads_busy"))

    def test_staged_bytes_together_are_bounded(self):
        with patch.object(nu, "MAX_STAGED_BYTES", len(self.data) * 2):
            _start(self.store, self.data, name="a.mov")
            _start(self.store, self.data, name="b.mov", owner="fixture\x1fbob")
            with self.assertRaises(NativeAPIError) as caught:
                _start(self.store, self.data, name="c.mov", owner="fixture\x1fcarol")
        self.assertEqual(caught.exception.code, "uploads_busy")

    def test_idle_and_discarded_uploads_are_removed(self):
        stale = _start(self.store, self.data, name="old.mov")
        _send(self.store, stale, self.data, 0)
        (self.root / "not-an-upload").write_bytes(b"x")
        self.clock[0] += nu.EXPIRY_SECONDS + 1
        with self.assertRaises(NativeAPIError) as caught:
            self.store.status(stale["uploadId"], OWNER)
        self.assertEqual(caught.exception.code, "upload_not_found")
        fresh = _start(self.store, self.data, name="new.mov")
        self.assertEqual(sorted(entry.name for entry in self.root.iterdir()), [fresh["uploadId"]])
        self.assertEqual(self.store.discard(fresh["uploadId"], OWNER)["discarded"], True)
        self.assertEqual(list(self.root.iterdir()), [])
        # Discarding again is fine: the phone may not have heard the first answer.
        self.assertEqual(self.store.discard(fresh["uploadId"], OWNER)["discarded"], True)

    def test_someone_elses_upload_looks_like_none(self):
        upload = _start(self.store, self.data)
        for action in (lambda: self.store.status(upload["uploadId"], "fixture\x1fbob"),
                       lambda: self.store.begin_chunk(upload["uploadId"], "fixture\x1fbob", 0, 1024),
                       lambda: self.store.finish(upload["uploadId"], "fixture\x1fbob")):
            with self.assertRaises(NativeAPIError) as caught:
                action()
            self.assertEqual(caught.exception.code, "upload_not_found")
        self.store.discard(upload["uploadId"], "fixture\x1fbob")
        self.assertTrue((self.root / upload["uploadId"]).is_dir())
        # Bob's identical file is his own upload.
        self.assertNotEqual(_start(self.store, self.data, owner="fixture\x1fbob")["uploadId"], upload["uploadId"])

    def test_paths_are_refused(self):
        for upload_id in ("../" + "0" * 29, "0" * 31, "A" * 32, "0" * 32 + "/x"):
            with self.subTest(upload_id=upload_id), self.assertRaises(NativeAPIError):
                self.store.status(upload_id, OWNER)
        outside = self.root.parent / "outside"
        outside.mkdir()
        self.root.mkdir(exist_ok=True)
        (self.root / ("a" * 32)).symlink_to(outside, target_is_directory=True)
        with self.assertRaises(NativeAPIError) as caught:
            self.store.status("a" * 32, OWNER)
        self.assertEqual(caught.exception.code, "upload_not_found")
        self.store.prune()
        self.assertTrue(outside.is_dir())
        self.assertFalse(os.path.lexists(self.root / ("a" * 32)))

    def test_file_names_are_one_safe_path_part(self):
        self.assertEqual(nu.safe_file_name("../../etc/passwd"), "_.._etc_passwd")
        self.assertEqual(nu.safe_file_name(".hidden.mov"), "hidden.mov")
        self.assertEqual(nu.safe_file_name("a\nb.mov"), "a_b.mov")
        self.assertEqual(nu.safe_file_name(".."), "attachment")
        long = nu.safe_file_name("é" * 200 + ".mov")
        self.assertTrue(long.endswith(".mov"))
        self.assertLessEqual(len(long.encode("utf-8")), 180)
        upload = _start(self.store, self.data, name="../../Clip.mov", agent="boxed")
        for index in range(4):
            _send(self.store, upload, self.data, index)
        path = Path(self.store.finish(upload["uploadId"], OWNER)["path"])
        self.assertEqual(path.parent, self.root / upload["uploadId"] / "file")

    # Review of #250

    def test_parallel_uploads_count_what_they_still_need_against_free_space(self):
        # Sparse files: free space doesn't drop until pieces arrive, so the host counts what's owed.
        self.free[0] = 2 * len(self.data) * 2 + nu.FREE_SPACE_RESERVE_BYTES - 1
        _start(self.store, self.data, name="a.mov")
        with self.assertRaises(NativeAPIError) as caught:
            _start(self.store, self.data, name="b.mov")
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "upload_no_space"))

    def test_a_disk_that_fills_while_writing_says_no_space(self):
        upload = _start(self.store, self.data)
        with patch.object(nu.os, "pwrite", side_effect=OSError(errno.ENOSPC, "full")), \
                self.assertRaises(NativeAPIError) as caught:
            _send(self.store, upload, self.data, 0)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "upload_no_space"))
        for index in range(4):
            _send(self.store, upload, self.data, index)
        with patch.object(nu.os, "replace", side_effect=OSError(errno.ENOSPC, "full")), \
                self.assertRaises(NativeAPIError) as finishing:
            self.store.finish(upload["uploadId"], OWNER)
        self.assertEqual((finishing.exception.status, finishing.exception.code), (409, "upload_no_space"))

    def test_an_expired_upload_found_by_any_call_is_deleted_with_the_other_idle_ones(self):
        first = _start(self.store, self.data, name="one.mov")
        second = _start(self.store, self.data, name="two.mov")
        self.clock[0] += nu.EXPIRY_SECONDS + nu.PRUNE_INTERVAL_SECONDS + 1
        with self.assertRaises(NativeAPIError):
            self.store.status(first["uploadId"], OWNER)
        self.assertFalse((self.root / first["uploadId"]).exists())
        self.assertFalse((self.root / second["uploadId"]).exists())

    def test_unknown_upload_ids_leave_no_lock_behind(self):
        before = len(nu._locks)
        for _ in range(50):
            with self.assertRaises(NativeAPIError):
                self.store.status(os.urandom(16).hex(), OWNER)
        self.assertEqual(len(nu._locks), before)

    def test_one_person_holds_only_a_few_uploads_and_others_can_still_send(self):
        for number in range(nu.MAX_UPLOADS_PER_PERSON):
            _start(self.store, self.data, name=f"mine-{number}.mov")
        with self.assertRaises(NativeAPIError) as caught:
            _start(self.store, self.data, name="one-more.mov")
        self.assertEqual(caught.exception.code, "uploads_busy")
        _start(self.store, self.data, name="theirs.mov", owner="fixture\x1fbob")

    def test_names_the_host_uses_itself_never_collide(self):
        for name in ("upload.json", "data.part", "file"):
            with self.subTest(name=name):
                upload = _start(self.store, self.data, name=name)
                for index in range(4):
                    _send(self.store, upload, self.data, index)
                path = Path(self.store.finish(upload["uploadId"], OWNER)["path"])
                self.assertEqual((path.name, path.read_bytes()), (name, self.data))
                self.store.discard(upload["uploadId"], OWNER)

    def test_a_name_that_is_not_text_is_refused_as_invalid(self):
        body = nu._Start.model_construct(agentId="default", fileName="clip\ud800.mov", mimeType="video/quicktime",
                                         byteCount=len(self.data), sha256=hashlib.sha256(self.data).hexdigest())
        with self.assertRaises(NativeAPIError) as caught:
            self.store.start(body, OWNER)
        self.assertEqual((caught.exception.status, caught.exception.code), (422, "invalid_request"))

    def test_the_same_file_with_another_type_is_its_own_upload(self):
        first = _start(self.store, self.data, mime="video/quicktime")
        second = _start(self.store, self.data, mime="video/mp4")
        self.assertNotEqual(first["uploadId"], second["uploadId"])
        self.assertEqual(second["mimeType"], "video/mp4")

    def test_a_piece_that_never_finishes_arriving_times_out(self):
        async def slow():
            yield b"x" * 10
            await asyncio.sleep(5)
            yield b"y"
        with patch.object(nu, "CHUNK_READ_SECONDS", 0.05), self.assertRaises(NativeAPIError) as caught:
            asyncio.run(nu._read_piece(slow(), 11))
        self.assertEqual((caught.exception.status, caught.exception.code), (408, "upload_chunk_timeout"))
        async def whole():
            yield b"ab"
            yield b"c"
        self.assertEqual(asyncio.run(nu._read_piece(whole(), 3)), b"abc")

    def test_a_host_without_fcntl_still_lists_its_other_features(self):
        from loopdy_plugin import native_context
        saved = sys.modules.pop("loopdy_plugin.native_uploads")
        try:
            with patch.dict(sys.modules, {"fcntl": None}):
                fresh = importlib.import_module("loopdy_plugin.native_uploads")
                self.assertFalse(fresh.available())
                skipped = []
                _profile, features = native_context._native_features(skipped=skipped)
                self.assertIn("native-context-v1", features)
                self.assertNotIn(nu.CAPABILITY, features)
                self.assertIn(nu.CAPABILITY, [feature for feature, _ in skipped])
        finally:
            sys.modules["loopdy_plugin.native_uploads"] = saved

    # Colt, 2026-10-31: a file in the chat opens in the agent's Files, so it lives in its working folder.

    def test_a_finished_file_moves_into_the_agents_working_folder_under_a_unique_name(self):
        day = time.strftime("%Y-%m-%d", time.localtime(self.clock[0]))
        paths = []
        for _ in range(2):
            upload = _start(self.store, self.data)
            for index in range(4):
                _send(self.store, upload, self.data, index)
            finished = self.store.finish(upload["uploadId"], OWNER)
            self.assertTrue(finished["inWorkspace"])
            paths.append(Path(finished["path"]))
            # Finishing again names the same file; the phone may not have heard the first answer.
            self.assertEqual(self.store.finish(upload["uploadId"], OWNER)["path"], finished["path"])
            self.store.discard(upload["uploadId"], OWNER)
        self.assertEqual(paths, [self.workspace / "uploads" / day / "Clip.mov",
                                 self.workspace / "uploads" / day / "Clip-2.mov"])
        self.assertTrue(all(path.read_bytes() == self.data for path in paths))
        # The sweep and discard never touch a file the chat links to.
        self.clock[0] += nu.EXPIRY_SECONDS * 3
        self.store.prune()
        self.assertTrue(all(path.exists() for path in paths))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_an_agent_without_a_folder_on_this_computer_keeps_the_staged_copy(self):
        upload = _start(self.store, self.data, agent="boxed")
        for index in range(4):
            _send(self.store, upload, self.data, index)
        finished = self.store.finish(upload["uploadId"], OWNER)
        self.assertFalse(finished["inWorkspace"])
        self.assertEqual(Path(finished["path"]).parent, self.root / upload["uploadId"] / "file")

    def test_a_planted_link_in_the_working_folder_is_never_followed(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        (self.workspace / "uploads").symlink_to(outside, target_is_directory=True)
        upload = _start(self.store, self.data)
        for index in range(4):
            _send(self.store, upload, self.data, index)
        finished = self.store.finish(upload["uploadId"], OWNER)
        self.assertFalse(finished["inWorkspace"])
        self.assertEqual(list(outside.iterdir()), [])


class NativeUploadRouteTests(unittest.TestCase):
    """The route as the app calls it, through Hermes' own auth middleware."""

    def setUp(self):
        test_native_api.NativeAPITests.setUp(self)
        self.agent_folder = self.home / "agent-folder"
        self.agent_folder.mkdir()
        folder = patch.object(nu, "_agent_workspace", lambda agent: self.agent_folder if agent == "default" else None)
        folder.start()
        self.addCleanup(folder.stop)

    context = test_native_api.NativeAPITests.context
    headers = test_native_api.NativeAPITests.headers

    def piece_headers(self, upload_id: str, offset: int, piece: bytes, token="fixture-alice"):
        return {**self.headers(token), "Content-Type": "application/octet-stream",
                "X-Loopdy-Upload-ID": upload_id, "X-Loopdy-Upload-Offset": str(offset),
                "X-Loopdy-Chunk-SHA256": hashlib.sha256(piece).hexdigest()}

    def test_the_app_sends_a_file_in_pieces_and_gets_a_path_for_file_attach(self):
        self.assertIn(nu.CAPABILITY, self.context().json()["features"])
        data = os.urandom(nu.CHUNK_BYTES + 4096)
        start = self.client.post(PREFIX + "/uploads/start", headers=self.headers(), json={
            "agentId": "default", "fileName": "Trip.mov", "mimeType": "video/quicktime",
            "byteCount": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        self.assertEqual(start.status_code, 200, start.text)
        upload = start.json()
        self.assertEqual((upload["chunkBytes"], upload["receivedChunks"]), (nu.CHUNK_BYTES, []))
        self.assertNotIn(str(self.home), start.text)
        for offset in (nu.CHUNK_BYTES, 0):
            piece = data[offset:offset + nu.CHUNK_BYTES]
            answer = self.client.post(PREFIX + "/uploads/chunk", content=piece,
                                      headers=self.piece_headers(upload["uploadId"], offset, piece))
            self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["receivedBytes"], len(data))
        # Another person on this host can't see or finish it.
        other = self.client.post(PREFIX + "/uploads/finish", headers=self.headers("fixture-bob"),
                                 json={"uploadId": upload["uploadId"]})
        self.assertEqual((other.status_code, other.json()["error"]["code"]), (404, "upload_not_found"))
        finished = self.client.post(PREFIX + "/uploads/finish", headers=self.headers(),
                                    json={"uploadId": upload["uploadId"]})
        self.assertEqual(finished.status_code, 200, finished.text)
        path = Path(finished.json()["path"])
        self.assertEqual(path.read_bytes(), data)
        self.assertTrue(finished.json()["inWorkspace"])
        self.assertEqual((path.parent.parent, path.name), (self.agent_folder / "uploads", "Trip.mov"))
        discarded = self.client.post(PREFIX + "/uploads/discard", headers=self.headers(),
                                     json={"uploadId": upload["uploadId"]})
        self.assertEqual(discarded.status_code, 200, discarded.text)
        # The record goes; the file the chat links to stays in the agent's folder.
        self.assertTrue(path.exists())
        self.assertFalse((self.home / "plugin-data" / "loopdy" / "uploads" / upload["uploadId"]).exists())

    def test_a_piece_needs_exact_headers_and_size(self):
        data = os.urandom(2048)
        upload = self.client.post(PREFIX + "/uploads/start", headers=self.headers(), json={
            "agentId": "default", "fileName": "a.bin", "mimeType": "application/octet-stream",
            "byteCount": len(data), "sha256": hashlib.sha256(data).hexdigest()}).json()
        good = self.piece_headers(upload["uploadId"], 0, data)
        cases = {
            "json body": ({**good, "Content-Type": "application/json"}, data, 422),
            "no offset": ({k: v for k, v in good.items() if k != "X-Loopdy-Upload-Offset"}, data, 422),
            "bad id": ({**good, "X-Loopdy-Upload-ID": "../" + "0" * 29}, data, 422),
            "short": (good, data[:100], 422),
            "damaged": ({**good, "X-Loopdy-Chunk-SHA256": "0" * 64}, data, 422),
            "stale context": ({**good, "If-Match": '"stale"'}, data, 412),
            "no sign-in": ({k: v for k, v in good.items() if k != "Authorization"}, data, 401),
        }
        for name, (headers, body, status) in cases.items():
            with self.subTest(name):
                answer = self.client.post(PREFIX + "/uploads/chunk", content=body, headers=headers)
                self.assertEqual(answer.status_code, status, answer.text)
        too_big = self.client.post(PREFIX + "/uploads/chunk", content=b"x" * (nu.CHUNK_BYTES + 1),
                                   headers=self.piece_headers(upload["uploadId"], 0, b""))
        self.assertEqual(too_big.status_code, 413, too_big.text)
        status = self.client.post(PREFIX + "/uploads/status", headers=self.headers(),
                                  json={"uploadId": upload["uploadId"]})
        self.assertEqual(status.json()["receivedChunks"], [])

    def test_an_unknown_agent_or_a_full_disk_is_refused(self):
        body = {"agentId": "nobody", "fileName": "a.mov", "mimeType": "video/quicktime",
                "byteCount": 10, "sha256": "0" * 64}
        answer = self.client.post(PREFIX + "/uploads/start", headers=self.headers(), json=body)
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (404, "profile_not_found"))
        with patch.object(nu.shutil, "disk_usage", return_value=SimpleNamespace(free=0)), \
                patch.object(nu, "_store", None):
            answer = self.client.post(PREFIX + "/uploads/start", headers=self.headers(),
                                      json={**body, "agentId": "default"})
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (409, "upload_no_space"))
        self.assertNotIn(str(self.home), json.dumps(answer.json()))


    def test_a_piece_without_its_checksum_or_a_name_that_is_not_text_is_refused(self):
        data = os.urandom(2048)
        upload = self.client.post(PREFIX + "/uploads/start", headers=self.headers(), json={
            "agentId": "default", "fileName": "a.bin", "mimeType": "application/octet-stream",
            "byteCount": len(data), "sha256": hashlib.sha256(data).hexdigest()}).json()
        headers = {k: v for k, v in self.piece_headers(upload["uploadId"], 0, data).items()
                   if k != "X-Loopdy-Chunk-SHA256"}
        answer = self.client.post(PREFIX + "/uploads/chunk", content=data, headers=headers)
        self.assertEqual(answer.status_code, 422, answer.text)
        body = ('{"agentId":"default","fileName":"a\\ud800.bin","mimeType":"application/octet-stream",'
                f'"byteCount":10,"sha256":"{"0" * 64}"}}')
        answer = self.client.post(PREFIX + "/uploads/start", content=body,
                                  headers={**self.headers(), "Content-Type": "application/json"})
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (422, "invalid_request"))

    def tearDown(self):
        nu._store = None


if __name__ == "__main__":
    unittest.main()
