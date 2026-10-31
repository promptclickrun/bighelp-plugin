"""Big files from the phone, in pieces that survive a dropped connection.

Hermes' ``file.attach`` takes a file's bytes in one WebSocket frame, so the app
keeps it for files up to 8 MiB. A video from an iPhone is often 60 to 80 MB. The
app sends those here instead, in fixed pieces (``chunkBytes``), in any order and
again after a drop: the host keeps which pieces it has, and ``status`` says so.

``finish`` checks the whole file against the SHA-256 the app sent at ``start``
and answers with the staged file's path. The app hands that path to stock
``file.attach``, which copies the file into the agent's attachments the way
Hermes always does (its sandbox mounts and ``attachments.storage`` included)
and gives the agent an ``@file:`` reference. Hermes doesn't put a binary file's
bytes in the model's context; it tells the agent where the file is, so the
agent works with it through its own tools. ``discard`` then removes the staged
copy. Anything not finished or discarded goes after a day.

Every upload belongs to the signed-in person who started it. The phone never
names a path: upload IDs are random, and the staged file lives only under this
plugin's own data folder.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from .native_context import NativeAPIError, NativeContext, PROFILE_ID, native_context

try:
    import fcntl
except ImportError:  # Windows: no process-safe lock, so the feature isn't offered.
    fcntl = None

logger = logging.getLogger(__name__)

CAPABILITY = "native-uploads-v1"
MAX_UPLOAD_BYTES = 256 * 1024 * 1024
CHUNK_BYTES = 4 * 1024 * 1024
MAX_CHUNKS = -(-MAX_UPLOAD_BYTES // CHUNK_BYTES)
# Uploads one host holds at once, one person's share of them, and their bytes together.
MAX_ACTIVE_UPLOADS = 8
MAX_UPLOADS_PER_PERSON = 3
MAX_STAGED_BYTES = 1024 * 1024 * 1024
# Left free after the staged copy and Hermes' own copy into the agent's attachments.
FREE_SPACE_RESERVE_BYTES = 512 * 1024 * 1024
EXPIRY_SECONDS = 24 * 60 * 60
# Idle uploads are swept at most this often, from whichever call comes first.
PRUNE_INTERVAL_SECONDS = 60
# A piece holds one of the few memory slots only this long while its body arrives.
CHUNK_READ_SECONDS = 120
# Pieces read into memory at once in this process; each is at most CHUNK_BYTES.
_CONCURRENT_CHUNKS = 4
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MIME = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}\Z")
_DATA = "data.part"
_META = "upload.json"
# The finished file sits in its own folder, so no name a person picks meets the host's own files.
_FINISHED = "file"
_MAX_FILENAME_BYTES = 180

_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}
_chunk_slots: asyncio.Semaphore | None = None


class _Start(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agentId: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    fileName: str = Field(min_length=1, max_length=_MAX_FILENAME_BYTES)
    mimeType: str = Field(min_length=3, max_length=129)
    byteCount: StrictInt = Field(ge=1, le=MAX_UPLOAD_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _Upload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    uploadId: str = Field(pattern=r"^[0-9a-f]{32}$")


def available() -> bool:
    try:
        from hermes_constants import get_process_hermes_home  # noqa: F401
    except ImportError:
        return False
    return fcntl is not None and hasattr(os, "pwrite") and hasattr(os, "O_NOFOLLOW")


def uploads_root() -> Path:
    from hermes_constants import get_process_hermes_home
    return get_process_hermes_home() / "plugin-data" / "loopdy" / "uploads"


def safe_file_name(value: str) -> str:
    """The name the agent sees: one path part, no control characters, no leading dot."""
    name = "".join(
        "_" if ord(character) < 32 or ord(character) == 127 or character in "/\\" else character
        for character in value
    ).strip(" .")
    if not name or name in {".", ".."}:
        name = "attachment"
    if len(name.encode("utf-8")) <= _MAX_FILENAME_BYTES:
        return name
    suffix = Path(name).suffix.encode("utf-8")[:32].decode("utf-8", "ignore")
    budget = _MAX_FILENAME_BYTES - len(suffix.encode("utf-8"))
    stem = Path(name).stem.encode("utf-8")[:budget].decode("utf-8", "ignore")
    return f"{stem}{suffix}" or "attachment"


def chunk_count(byte_count: int) -> int:
    return -(-byte_count // CHUNK_BYTES)


def chunk_length(byte_count: int, index: int) -> int:
    """How many bytes piece ``index`` holds: CHUNK_BYTES, except for the last piece."""
    if not 0 <= index < chunk_count(byte_count):
        raise ValueError("chunk out of range")
    return min(CHUNK_BYTES, byte_count - index * CHUNK_BYTES)


def _owner(context: NativeContext) -> str:
    return f"{context.provider or ''}\x1f{context.user_id or ''}"


def _lock(upload_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(upload_id, threading.Lock())


@contextmanager
def _disk() -> Iterator[None]:
    """A full disk is a refusal the app can explain, not a host failure."""
    try:
        yield
    except OSError as error:
        if error.errno in (errno.ENOSPC, errno.EDQUOT):
            raise NativeAPIError(409, "upload_no_space", "This computer doesn't have room for this file.") from None
        raise


class UploadStore:
    """Staged uploads under one folder. Every method is safe to call from several threads."""

    def __init__(self, root: Path, *, now=time.time, disk_usage=None) -> None:
        self.root = root
        self._now = now
        self._disk_usage = disk_usage or (lambda path: shutil.disk_usage(path))
        self._pruned_at = float("-inf")

    # Folder layout

    def _ensure_root(self) -> Path:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = os.lstat(self.root)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NativeAPIError(503, "uploads_unavailable", "Uploads are unavailable on this host.")
        os.chmod(self.root, 0o700)
        return self.root

    def _folder(self, upload_id: str) -> Path:
        if _UPLOAD_ID.fullmatch(upload_id) is None:
            raise NativeAPIError(422, "invalid_request", "The upload is invalid.")
        folder = self._ensure_root() / upload_id
        try:
            metadata = os.lstat(folder)
        except FileNotFoundError:
            raise NativeAPIError(404, "upload_not_found", "The upload is gone. Send the file again.") from None
        if not stat.S_ISDIR(metadata.st_mode):
            raise NativeAPIError(404, "upload_not_found", "The upload is gone. Send the file again.")
        return folder

    @contextmanager
    def _locked(self, upload_id: str) -> Iterator[Path]:
        """One writer per upload, across threads and the plugin's other module copies."""
        # Only an upload that exists gets a lock, so made-up IDs leave nothing behind.
        self._folder(upload_id)
        with _lock(upload_id):
            try:
                folder = self._folder(upload_id)
            except NativeAPIError:
                with _locks_guard:
                    _locks.pop(upload_id, None)
                raise
            descriptor = os.open(folder, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_DIRECTORY", 0))
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield folder
            finally:
                os.close(descriptor)

    def _read_meta(self, folder: Path) -> dict[str, Any]:
        try:
            descriptor = os.open(folder / _META, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                value = json.loads(handle.read(65_536).decode("utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise NativeAPIError(404, "upload_not_found", "The upload is gone. Send the file again.") from None
        if not isinstance(value, dict) or value.get("version") != 1:
            raise NativeAPIError(404, "upload_not_found", "The upload is gone. Send the file again.")
        return value

    def _write_meta(self, folder: Path, value: dict[str, Any]) -> None:
        temporary = folder / f".{_META}.{secrets.token_hex(4)}"
        with _disk():
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, folder / _META)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise

    def _owned(self, folder: Path, owner: str) -> dict[str, Any]:
        meta = self._read_meta(folder)
        if meta.get("owner") != owner:
            # Someone else's upload looks the same as one that never existed.
            raise NativeAPIError(404, "upload_not_found", "The upload is gone. Send the file again.")
        if self._now() - float(meta.get("updatedAt", 0)) > EXPIRY_SECONDS:
            self._remove(folder)
            raise NativeAPIError(404, "upload_not_found", "The upload is gone. Send the file again.")
        return meta

    # Housekeeping

    def _sweep(self) -> None:
        """Every call sweeps now and then, so an upload abandoned on a quiet host still goes."""
        if self._now() - self._pruned_at >= PRUNE_INTERVAL_SECONDS:
            self.prune()

    def prune(self) -> None:
        """Remove uploads idle for a day, and folders without a readable record."""
        self._pruned_at = self._now()
        try:
            entries = list(self._ensure_root().iterdir())
        except OSError:
            return
        cutoff = self._now() - EXPIRY_SECONDS
        for entry in entries:
            try:
                metadata = os.lstat(entry)
            except OSError:
                continue
            if not stat.S_ISDIR(metadata.st_mode) or _UPLOAD_ID.fullmatch(entry.name) is None:
                if stat.S_ISLNK(metadata.st_mode) or stat.S_ISREG(metadata.st_mode):
                    entry.unlink(missing_ok=True)
                continue
            try:
                updated = float(self._read_meta(entry).get("updatedAt", 0))
            except NativeAPIError:
                updated = metadata.st_mtime
            if updated < cutoff:
                self._remove(entry)

    def _remove(self, folder: Path) -> None:
        shutil.rmtree(folder, ignore_errors=True)
        with _locks_guard:
            _locks.pop(folder.name, None)

    def _active(self) -> list[dict[str, Any]]:
        values = []
        for entry in self._ensure_root().iterdir():
            if _UPLOAD_ID.fullmatch(entry.name) is None or not entry.is_dir() or entry.is_symlink():
                continue
            try:
                values.append(self._read_meta(entry))
            except NativeAPIError:
                continue
        return values

    # Operations

    def start(self, body: _Start, owner: str) -> dict[str, Any]:
        mime = body.mimeType.lower()
        if _MIME.fullmatch(mime) is None:
            raise NativeAPIError(422, "invalid_request", "The file type is invalid.")
        try:
            body.fileName.encode("utf-8")
        except UnicodeEncodeError:
            raise NativeAPIError(422, "invalid_request", "The file name is invalid.") from None
        file_name = safe_file_name(body.fileName)
        with _lock("start"):
            self.prune()
            active = self._active()
            for meta in active:
                # The same file again (the app lost its upload, or started over after a relaunch):
                # keep the pieces the host already has.
                if (meta.get("owner"), meta.get("agentId"), meta.get("sha256"), meta.get("byteCount"),
                        meta.get("fileName"), meta.get("mimeType")) \
                        == (owner, body.agentId, body.sha256, body.byteCount, file_name, mime) \
                        and not meta.get("finished"):
                    return self._answer(meta)
            if len(active) >= MAX_ACTIVE_UPLOADS \
                    or sum(meta.get("owner") == owner for meta in active) >= MAX_UPLOADS_PER_PERSON:
                raise NativeAPIError(429, "uploads_busy", "This computer is taking other files. Try again soon.")
            staged = sum(int(meta.get("byteCount", 0)) for meta in active)
            if staged + body.byteCount > MAX_STAGED_BYTES:
                raise NativeAPIError(429, "uploads_busy", "This computer is taking other files. Try again soon.")
            try:
                free = self._disk_usage(self.root).free
            except OSError:
                raise NativeAPIError(503, "uploads_unavailable", "Uploads are unavailable on this host.") from None
            # Each file needs its staged copy and Hermes' copy into the agent's attachments. Staged
            # files are sparse, so the room uploads already started still need counts as taken.
            owed = sum(self._still_needs(meta) for meta in active)
            if free < owed + 2 * body.byteCount + FREE_SPACE_RESERVE_BYTES:
                raise NativeAPIError(409, "upload_no_space", "This computer doesn't have room for this file.")
            upload_id = secrets.token_hex(16)
            folder = self.root / upload_id
            folder.mkdir(mode=0o700)
            try:
                with _disk():
                    descriptor = os.open(folder / _DATA, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o600)
                    try:
                        os.ftruncate(descriptor, body.byteCount)
                    finally:
                        os.close(descriptor)
                now = self._now()
                meta = {
                    "version": 1, "uploadId": upload_id, "owner": owner, "agentId": body.agentId,
                    "fileName": file_name, "mimeType": mime, "byteCount": body.byteCount,
                    "sha256": body.sha256, "chunkBytes": CHUNK_BYTES, "received": [],
                    "finished": False, "createdAt": now, "updatedAt": now,
                }
                self._write_meta(folder, meta)
            except BaseException:
                self._remove(folder)
                raise
            return self._answer(meta)

    @staticmethod
    def _still_needs(meta: dict[str, Any]) -> int:
        """Disk an unfinished upload will still take: its missing pieces, then Hermes' copy."""
        byte_count = int(meta.get("byteCount", 0))
        if meta.get("finished"):
            return byte_count
        received = sum(chunk_length(byte_count, index) for index in meta.get("received", [])
                       if 0 <= index < chunk_count(byte_count))
        return 2 * byte_count - received

    def status(self, upload_id: str, owner: str) -> dict[str, Any]:
        self._sweep()
        with self._locked(upload_id) as folder:
            return self._answer(self._owned(folder, owner))

    def begin_chunk(self, upload_id: str, owner: str, offset: int, length: int) -> int:
        """The piece's index, once its offset and length are exactly one piece of this upload."""
        self._sweep()
        with self._locked(upload_id) as folder:
            meta = self._owned(folder, owner)
            if meta["finished"]:
                raise NativeAPIError(409, "upload_finished", "The upload is already complete.")
            byte_count = int(meta["byteCount"])
            if offset % CHUNK_BYTES or not 0 <= offset < byte_count:
                raise NativeAPIError(422, "upload_offset_invalid", "The piece doesn't fit this upload.")
            index = offset // CHUNK_BYTES
            if length != chunk_length(byte_count, index):
                raise NativeAPIError(422, "upload_length_invalid", "The piece doesn't fit this upload.")
            return index

    def write_chunk(self, upload_id: str, owner: str, index: int, data: bytes,
                    expected_sha256: str) -> dict[str, Any]:
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise NativeAPIError(422, "upload_chunk_corrupt", "A piece arrived damaged. Send it again.")
        with self._locked(upload_id) as folder:
            meta = self._owned(folder, owner)
            if meta["finished"]:
                raise NativeAPIError(409, "upload_finished", "The upload is already complete.")
            if len(data) != chunk_length(int(meta["byteCount"]), index):
                raise NativeAPIError(422, "upload_length_invalid", "The piece doesn't fit this upload.")
            descriptor = os.open(folder / _DATA, os.O_WRONLY | os.O_NOFOLLOW)
            try:
                with _disk():
                    written = 0
                    view = memoryview(data)
                    while written < len(data):
                        written += os.pwrite(descriptor, view[written:], index * CHUNK_BYTES + written)
                    os.fsync(descriptor)
            finally:
                os.close(descriptor)
            received = sorted(set(meta["received"]) | {index})
            meta.update(received=received, updatedAt=self._now())
            self._write_meta(folder, meta)
            return self._answer(meta)

    def finish(self, upload_id: str, owner: str) -> dict[str, Any]:
        self._sweep()
        with self._locked(upload_id) as folder:
            meta = self._owned(folder, owner)
            target = folder / _FINISHED / meta["fileName"]
            if not meta["finished"]:
                byte_count = int(meta["byteCount"])
                if len(meta["received"]) != chunk_count(byte_count):
                    raise NativeAPIError(409, "upload_incomplete", "Some of the file hasn't arrived yet.")
                digest = hashlib.sha256()
                descriptor = os.open(folder / _DATA, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as handle:
                    if os.fstat(handle.fileno()).st_size != byte_count:
                        raise NativeAPIError(409, "upload_incomplete", "Some of the file hasn't arrived yet.")
                    while block := handle.read(1024 * 1024):
                        digest.update(block)
                if digest.hexdigest() != meta["sha256"]:
                    # Pieces that don't add up to the file the phone has: start over.
                    meta.update(received=[], updatedAt=self._now())
                    self._write_meta(folder, meta)
                    raise NativeAPIError(422, "upload_corrupt", "The file arrived damaged. Send it again.")
                if os.path.lexists(target.parent):
                    raise NativeAPIError(409, "upload_conflict", "The upload can't be finished.")
                target.parent.mkdir(mode=0o700)
                os.rename(folder / _DATA, target)
                meta.update(finished=True, updatedAt=self._now())
                self._write_meta(folder, meta)
            return {**self._answer(meta), "path": str(target)}

    def discard(self, upload_id: str, owner: str) -> dict[str, Any]:
        self._sweep()
        try:
            with self._locked(upload_id) as folder:
                self._owned(folder, owner)
                self._remove(folder)
        except NativeAPIError as error:
            if error.code != "upload_not_found":
                raise
        return {"uploadId": upload_id, "discarded": True}

    @staticmethod
    def _answer(meta: dict[str, Any]) -> dict[str, Any]:
        byte_count = int(meta["byteCount"])
        received = list(meta["received"])
        return {
            "uploadId": meta["uploadId"],
            "fileName": meta["fileName"],
            "mimeType": meta["mimeType"],
            "byteCount": byte_count,
            "chunkBytes": int(meta["chunkBytes"]),
            "receivedChunks": received,
            "receivedBytes": sum(chunk_length(byte_count, index) for index in received),
            "finished": bool(meta["finished"]),
            "expiresAt": float(meta["updatedAt"]) + EXPIRY_SECONDS,
        }


_store: UploadStore | None = None


def upload_store() -> UploadStore:
    global _store
    if _store is None:
        _store = UploadStore(uploads_root())
    return _store


def _slots() -> asyncio.Semaphore:
    global _chunk_slots
    if _chunk_slots is None:
        _chunk_slots = asyncio.Semaphore(_CONCURRENT_CHUNKS)
    return _chunk_slots


def _single_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if not values:
        return None
    if len(values) != 1:
        raise NativeAPIError(422, "invalid_request", "The upload request is invalid.")
    return values[0]


async def _chunk(http_request: Request, owner: NativeContext) -> dict[str, Any]:
    """One piece, as the raw request body. Its upload and offset ride in headers."""
    if http_request.query_params:
        raise NativeAPIError(422, "invalid_request", "Query fields are not supported.")
    content_type = (http_request.headers.get("content-type") or "").split(";")[0].strip().lower()
    upload_id = _single_header(http_request, "x-loopdy-upload-id") or ""
    raw_offset = _single_header(http_request, "x-loopdy-upload-offset") or ""
    raw_length = _single_header(http_request, "content-length") or ""
    # Required: one flipped bit would otherwise surface only at finish, which then forgets every piece.
    checksum = _single_header(http_request, "x-loopdy-chunk-sha256") or ""
    if (content_type != "application/octet-stream" or _UPLOAD_ID.fullmatch(upload_id) is None
            or not raw_offset.isascii() or not raw_offset.isdecimal() or len(raw_offset) > 10
            or not raw_length.isascii() or not raw_length.isdecimal() or len(raw_length) > 10
            or _SHA256.fullmatch(checksum) is None):
        raise NativeAPIError(422, "invalid_request", "The upload request is invalid.")
    offset, length = int(raw_offset), int(raw_length)
    if not 1 <= length <= CHUNK_BYTES:
        raise NativeAPIError(413, "payload_too_large", "The request exceeds the byte limit.")
    store = upload_store()
    principal = _owner(owner)
    async with _slots():
        index = await run_in_threadpool(store.begin_chunk, upload_id, principal, offset, length)
        content = await _read_piece(http_request.stream(), length)
        return await run_in_threadpool(store.write_chunk, upload_id, principal, index, content, checksum)


async def _read_piece(stream, length: int) -> bytes:
    """Exactly ``length`` bytes of body, within CHUNK_READ_SECONDS, so a stalled phone frees its slot."""
    async def read() -> bytes:
        content = bytearray()
        async for part in stream:
            if len(content) + len(part) > length:
                raise NativeAPIError(413, "payload_too_large", "The request exceeds the byte limit.")
            content.extend(part)
        if len(content) != length:
            raise NativeAPIError(400, "request_disconnected", "The request was disconnected.")
        return bytes(content)
    try:
        return await asyncio.wait_for(read(), CHUNK_READ_SECONDS)
    except asyncio.TimeoutError:
        raise NativeAPIError(408, "upload_chunk_timeout", "A piece took too long to arrive. Send it again.") from None


async def request(operation: str, http_request: Request, *, auth_module) -> Response:
    owner: NativeContext = native_context(http_request)
    request_id = auth_module._precondition(http_request, owner)
    if CAPABILITY not in owner.features:
        raise NativeAPIError(503, "uploads_unavailable", "Uploads are unavailable on this host.")
    store = upload_store()
    principal = _owner(owner)
    if operation == "chunk":
        result = await _chunk(http_request, owner)
    elif operation == "start":
        body = await auth_module._body(http_request, _Start)
        if PROFILE_ID.fullmatch(body.agentId) is None:
            raise NativeAPIError(422, "invalid_request", "The profile is invalid.")
        from hermes_cli.profiles import profile_exists
        if not profile_exists(body.agentId):
            raise NativeAPIError(404, "profile_not_found", "The selected profile no longer exists.")
        result = await run_in_threadpool(store.start, body, principal)
    elif operation in {"status", "finish", "discard"}:
        body = await auth_module._body(http_request, _Upload)
        worker = {"status": store.status, "finish": store.finish, "discard": store.discard}[operation]
        result = await run_in_threadpool(worker, body.uploadId, principal)
    else:
        raise NativeAPIError(404, "unknown_operation", "The upload operation is unknown.")
    if native_context(http_request) != owner:
        raise NativeAPIError(412, "context_changed", "The native context changed; refresh before retrying.")
    return auth_module._response(result, owner, request_id)


__all__ = ["CAPABILITY", "CHUNK_BYTES", "MAX_UPLOAD_BYTES", "UploadStore", "available", "request"]
