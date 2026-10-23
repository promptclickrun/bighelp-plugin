"""Pictures that cross between this host and the iPhone's Photos (iphone_photos).

photos.get: the phone sends a small JPEG as base64. It's written to a folder only this plugin uses, and the
agent gets the file's path to look at with its vision tool. The bytes go no further than that file.

photos.save: the agent names a file on this host. It's checked the way attachments/resolve checks files
agents send, then the phone downloads it through attachments/resolve and attachments/fetch. The phone names
a path to resolve, which resolve only serves when the chat's agent emitted it, so a checked path is also
granted to this chat for a few minutes.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import mimetypes
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

from .device_tool_arguments import MAX_PHOTO_PATH_LENGTH
from .device_tool_contract import MAX_PHOTO_JPEG_BYTES, MAX_PHOTO_PIXELS

# A picture or video up to this size can be saved to Photos.
MAX_SAVE_BYTES = 50 * 1024 * 1024
KEEP_PICTURES = 50
KEEP_SECONDS = 7 * 24 * 60 * 60
GRANT_SECONDS = 10 * 60
# Per chat, so one chat saving a lot can't push another chat's file out; the total only guards memory.
MAX_GRANTS_PER_SESSION = 16
MAX_GRANTS = 1_024
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".heic", ".heif", ".webp", ".tif", ".tiff", ".bmp", ".dng"})
_VIDEO_SUFFIXES = frozenset({".mov", ".mp4", ".m4v", ".avi", ".mkv", ".webm"})
_STALE_PARTIAL_SECONDS = 24 * 60 * 60

# One dashboard process imports this package twice (see AGENTS.md): the tool middleware grants a path, and the
# attachments route in the other copy must see it. Only plain containers live in the shared record.
_state = sys.modules.setdefault(
    "_bighelp_phone_photo_grants", SimpleNamespace(lock=threading.Lock(), grants={}))  # type: ignore[arg-type]
_clock = time.monotonic


class PhotoRefusal(ValueError):
    """A picture or path the host won't pass on. `code` is the fixed answer the agent gets."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def photos_directory(profile: str | None = None) -> Path:
    """The folder pictures from photos.get are kept in: inside the Hermes home of the agent that asked.

    With no profile it is the serving profile's, as before.
    """
    from hermes_constants import get_process_hermes_home, profile_name_for_home
    home = get_process_hermes_home()
    if profile and profile_name_for_home(home) != profile:
        from hermes_cli.profiles import get_profile_dir
        home = get_profile_dir(profile)
    return home / "bighelp" / "phone-photos"


# photos.save -------------------------------------------------------------------------------------

@contextlib.contextmanager
def profile_scope(profile: str):
    """Run the checks of a save in the Hermes home of the agent that asked, so the gateway's delivery rules
    are the ones that agent works under (the grant below is keyed to the same agent)."""
    try:
        from hermes_cli.profiles import get_profile_dir
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override
        home = get_profile_dir(profile)
    except (ImportError, OSError, ValueError):
        raise PhotoRefusal("files_unavailable") from None
    token = set_hermes_home_override(home)
    try:
        yield
    finally:
        reset_hermes_home_override(token)


def check_save_path(raw: Any) -> str:
    """The absolute path the phone should download, or a PhotoRefusal.

    The same refusals as attachments/resolve: the gateway's own delivery policy decides which files exist
    for an agent to send. On top of that the file must be a picture or video of at most 50 MB.
    """
    if (
        not isinstance(raw, str) or not raw or len(raw) > MAX_PHOTO_PATH_LENGTH
        or any(ord(character) < 32 or ord(character) == 127 for character in raw)
    ):
        raise PhotoRefusal("invalid_arguments")
    try:
        from gateway.platforms import base
        from hermes_cli.profiles import get_profile_dir, profile_exists  # noqa: F401  (attachments/resolve needs them)
    except ImportError:
        raise PhotoRefusal("files_unavailable") from None
    BasePlatformAdapter = base.BasePlatformAdapter
    if not _is_served_media(raw, base):
        raise PhotoRefusal("unsupported_media")
    safe = BasePlatformAdapter.validate_media_delivery_path(raw)
    # The phone asks resolve for "MEDIA:<path>", which must read back as exactly this one file.
    media, _cleaned = BasePlatformAdapter.extract_media("MEDIA:" + safe) if safe else ([], "")
    if (
        not safe or not os.path.isabs(safe) or [path for path, _voice in media] != [safe]
        or BasePlatformAdapter.filter_media_delivery_paths(media) != [(safe, False)]
    ):
        raise PhotoRefusal("invalid_arguments")
    if not _is_served_media(safe, base):  # A link to another kind of file.
        raise PhotoRefusal("unsupported_media")
    try:
        size = os.stat(safe).st_size
    except OSError:
        raise PhotoRefusal("invalid_arguments") from None
    if size > MAX_SAVE_BYTES:
        raise PhotoRefusal("file_too_large")
    if size == 0:
        raise PhotoRefusal("invalid_arguments")
    return safe


def _is_served_media(path: str, base: Any) -> bool:
    """A picture or video, of a kind the gateway delivers. It lists the extensions it serves and has no
    .heic, for one: that's unsupported media here, not a bad path."""
    suffix = Path(path).suffix.lower()
    kind = mimetypes.guess_type(path)[0] or ""
    return (kind.startswith(("image/", "video/")) or suffix in _IMAGE_SUFFIXES | _VIDEO_SUFFIXES) \
        and suffix in getattr(base, "MEDIA_DELIVERY_EXTS", (suffix,))


def grant_save_path(profile: str, session_ids: Iterable[str], path: str) -> None:
    """Let attachments/resolve serve `path` to these chats for a few minutes."""
    now = _clock()
    with _state.lock:
        grants = _state.grants
        for key in [key for key, expires in grants.items() if expires <= now]:
            del grants[key]
        for session_id in session_ids:
            grants[(profile, session_id, path)] = now + GRANT_SECONDS
            chat = [key for key in grants if key[:2] == (profile, session_id)]
            for key in sorted(chat, key=grants.__getitem__)[:max(0, len(chat) - MAX_GRANTS_PER_SESSION)]:
                del grants[key]
        while len(grants) > MAX_GRANTS:
            del grants[min(grants, key=grants.__getitem__)]


def granted_paths(profile: str, session_id: str, paths: Iterable[str]) -> set[str]:
    now = _clock()
    with _state.lock:
        return {path for path in paths if _state.grants.get((profile, session_id, path), 0) > now}


def file_stamp(path: str) -> str:
    """Size and modification time of a granted file: the same path saved again after a rewrite is a new file."""
    try:
        status = os.stat(path)
    except OSError:
        return "missing"
    return f"{status.st_size}:{status.st_mtime_ns}"


# photos.get --------------------------------------------------------------------------------------

def store_picture(image: Any, directory: Path, *, now: float | None = None) -> dict[str, Any]:
    """Write the phone's picture to `directory`; return its `format`, `width`, `height` and `path`.

    Raises PhotoRefusal when it isn't a JPEG within the limits. Nothing is logged or kept but the file.
    """
    data = image.get("data") if isinstance(image, dict) else None
    if not isinstance(data, str):
        raise PhotoRefusal("photo_unreadable")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise PhotoRefusal("photo_unreadable") from None
    if not 0 < len(raw) <= MAX_PHOTO_JPEG_BYTES:
        raise PhotoRefusal("photo_unreadable")
    width, height = _jpeg_size(raw)
    if not (0 < width <= MAX_PHOTO_PIXELS and 0 < height <= MAX_PHOTO_PIXELS):
        raise PhotoRefusal("photo_unreadable")
    if directory.is_symlink():
        raise PhotoRefusal("photo_unavailable")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / (hashlib.sha256(raw).hexdigest()[:40] + ".jpg")
    stamp = time.time() if now is None else now
    if target.is_file() and not target.is_symlink():
        os.utime(target, (stamp, stamp))  # The same picture again: it's the newest.
    else:
        partial = directory / f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
            os.utime(partial, (stamp, stamp))
            os.replace(partial, target)
        finally:
            try:
                os.unlink(partial)
            except FileNotFoundError:
                pass
    prune_pictures(directory, now=stamp)
    return {"format": "jpeg", "width": width, "height": height, "path": str(target)}


def prune_pictures(directory: Path, *, now: float | None = None) -> None:
    """Keep the newest 50 pictures from the last 7 days; drop old leftovers of a write that was cut off."""
    stamp = time.time() if now is None else now
    pictures: list[tuple[float, str]] = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            modified = entry.stat(follow_symlinks=False).st_mtime
            if entry.name.endswith(".jpg") and not entry.name.startswith("."):
                pictures.append((modified, entry.path))
            elif entry.name.endswith(".tmp") and stamp - modified > _STALE_PARTIAL_SECONDS:
                os.unlink(entry.path)
        except OSError:
            continue
    pictures.sort(reverse=True)
    for position, (modified, path) in enumerate(pictures):
        if position >= KEEP_PICTURES or stamp - modified > KEEP_SECONDS:
            try:
                os.unlink(path)
            except OSError:
                continue


def _jpeg_size(raw: bytes) -> tuple[int, int]:
    """Width and height from the JPEG's frame header; PhotoRefusal when `raw` isn't a whole JPEG."""
    if raw[:3] != b"\xff\xd8\xff" or raw[-2:] != b"\xff\xd9":
        raise PhotoRefusal("photo_unreadable")
    position = 2
    while position + 4 <= len(raw):
        if raw[position] != 0xFF:
            break
        marker = raw[position + 1]
        if marker == 0xFF:  # Padding before a marker.
            position += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # Markers that carry no length.
            position += 2
            continue
        length = int.from_bytes(raw[position + 2:position + 4], "big")
        if marker in range(0xC0, 0xD0) and marker not in (0xC4, 0xC8, 0xCC):
            if length < 8 or position + 9 > len(raw):
                break
            return (int.from_bytes(raw[position + 7:position + 9], "big"),
                    int.from_bytes(raw[position + 5:position + 7], "big"))
        if marker == 0xDA or length < 2:  # The picture data starts and no frame header came.
            break
        position += 2 + length
    raise PhotoRefusal("photo_unreadable")
