"""The hand-off contract between a workflow stage and the coordinator, and the check-stage rules.

An agent's final reply carries exactly one fenced ``bighelp-handoff`` block of JSON. Nothing in the reply is
trusted: sizes, types and file paths are all checked here before anything is stored.
"""
from __future__ import annotations

import json
import math
import mimetypes
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


FENCE = "bighelp-handoff"
MAX_REPLY_BYTES = 2 * 1024 * 1024
MAX_MARKDOWN_BYTES = 512 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILE_NAME = 120
MAX_TEXT_BYTES = 8 * 1024
MAX_NOTES = 20
MAX_NOTE_CHARS = 1000
_BLOCK = re.compile(r"^[ \t]*(`{3,}|~{3,})[ \t]*" + re.escape(FENCE) + r"[ \t]*\r?\n(.*?)^[ \t]*\1[ \t]*$",
                    re.MULTILINE | re.DOTALL)
_OPENER = re.compile(r"^[ \t]*(?:`{3,}|~{3,})[ \t]*" + re.escape(FENCE) + r"[ \t]*$", re.MULTILINE)
_HEADING = re.compile(r"#{1,6}[ \t]+\S")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")
# Pictures by their first bytes, never by the name the agent gave them.
_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png", ".png"), (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
                (b"GIF87a", "image/gif", ".gif"), (b"GIF89a", "image/gif", ".gif"))


class ContractError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class Output:
    name: str
    type: str
    data: bytes
    word_count: int | None = None
    number_value: float | None = None
    value: Any = None


def word_count(text: str) -> int:
    return len(text.split())


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non-finite number")


def _depth(value: Any, limit: int = 16) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > limit:
            raise ValueError("too deep")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)


def extract_block(reply: str) -> dict:
    if len(reply.encode("utf-8", "replace")) > MAX_REPLY_BYTES:
        raise ContractError("contract_too_large", "The hand-off is too large.")
    blocks = _BLOCK.findall(reply)
    openers = _OPENER.findall(reply)
    if not blocks:
        raise ContractError("contract_no_block", "The agent didn't hand off its work.")
    if len(blocks) > 1 or len(openers) > 1:
        raise ContractError("contract_many_blocks", "The agent handed off more than once.")
    try:
        value = json.loads(blocks[0][1], object_pairs_hook=_pairs, parse_constant=_reject_constant)
        _depth(value)
    except (ValueError, RecursionError):
        raise ContractError("contract_bad_json", "The hand-off isn't valid JSON.") from None
    if type(value) is not dict or type(value.get("outputs")) is not dict:
        raise ContractError("contract_bad_json", "The hand-off has no outputs.")
    return value["outputs"]


def _read_out_file(attempt_dir: Path, relative: Any, title: str, limit: int = MAX_MARKDOWN_BYTES) -> bytes:
    """Read out/<name> without following links or leaving the attempt's out folder."""
    if type(relative) is not str or not relative.startswith("out/") or "\x00" in relative:
        raise ContractError("contract_bad_path", f"{title} named a file outside its out folder.")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts) or len(parts) > 8:
        raise ContractError("contract_bad_path", f"{title} named a file outside its out folder.")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        descriptor = os.open(attempt_dir, flags | os.O_DIRECTORY)
    except OSError:
        raise ContractError("contract_bad_path", f"{title} named a file that isn't there.") from None
    try:
        for part in parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        handle = os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=descriptor)
    except OSError:
        os.close(descriptor)
        raise ContractError("contract_bad_path", f"{title} named a file that isn't there.") from None
    os.close(descriptor)
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ContractError("contract_bad_path", f"{title} named something that isn't a plain file.")
        too_large = ContractError("contract_too_large", f"{title} handed off a file over {_size(limit)}.")
        if info.st_size > limit:
            raise too_large
        chunks, total = [], 0
        while True:
            chunk = os.read(handle, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise too_large
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(handle)


def _size(limit: int) -> str:
    return f"{limit // (1024 * 1024)} MB" if limit >= 1024 * 1024 else f"{limit // 1024} KB"


def file_name(relative: str) -> str:
    """A name that is safe to show and to save: the last part of the path, plain characters only."""
    name = _UNSAFE_NAME.sub("_", relative.rsplit("/", 1)[-1]).strip(" .")
    return (name or "file")[-MAX_FILE_NAME:]


def image_type(data: bytes) -> tuple[str, str] | None:
    """(MIME type, extension) of a PNG, JPEG, GIF, WebP or HEIC picture, else None."""
    for magic, mime, extension in _IMAGE_MAGIC:
        if data.startswith(magic):
            return mime, extension
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"mif1", b"msf1"):
        return "image/heic", ".heic"
    return None


def _utf8(data: bytes, title: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ContractError("contract_wrong_type", f"{title} isn't UTF-8 text.") from None


def parse_outputs(reply: str, outputs: list[dict], *, attempt_dir: Path, stage_title: str) -> list[Output]:
    """Every declared output, typed and bounded, or ContractError. Extra outputs are ignored."""
    given = extract_block(reply)
    result = []
    for spec in outputs:
        name, kind = spec["name"], spec["type"]
        label = f"{stage_title} ({name})"
        if name not in given:
            raise ContractError("contract_missing_output", f"{stage_title} did not hand off {name}.")
        value = given[name]
        wrong = ContractError("contract_wrong_type", f"{label} has the wrong type.")
        if kind == "markdown_file":
            if type(value) is not dict or len(value) != 1 or not ({"content"} == set(value) or {"path"} == set(value)):
                raise wrong
            if "content" in value:
                if type(value["content"]) is not str:
                    raise wrong
                data = value["content"].encode("utf-8")
                if len(data) > MAX_MARKDOWN_BYTES:
                    raise ContractError("contract_too_large", f"{label} is over 512 KB.")
            else:
                data = _read_out_file(attempt_dir, value["path"], label)
            text = _utf8(data, label)
            if "\x00" in text:
                raise wrong
            result.append(Output(name, kind, data, word_count=word_count(text)))
        elif kind == "text":
            if type(value) is not str or "\x00" in value:
                raise wrong
            data = value.encode("utf-8")
            if len(data) > MAX_TEXT_BYTES:
                raise ContractError("contract_too_large", f"{label} is over 8 KB.")
            result.append(Output(name, kind, data, word_count=word_count(value), value=value))
        elif kind == "number":
            if type(value) not in (int, float) or not math.isfinite(value):
                raise wrong
            data = json.dumps(value).encode("ascii")
            result.append(Output(name, kind, data, number_value=float(value), value=value))
        elif kind == "decision":
            if type(value) is not str or value not in spec.get("values", []):
                raise ContractError("contract_wrong_type", f"{label} must be one of: {', '.join(spec.get('values', []))}.")
            result.append(Output(name, kind, value.encode("utf-8"), value=value))
        elif kind == "notes":
            if type(value) is not list or len(value) > MAX_NOTES:
                raise wrong
            notes = []
            for note in value:
                if (type(note) is not dict or set(note) != {"severity", "text"}
                        or note["severity"] not in ("minor", "major") or type(note["text"]) is not str
                        or not note["text"].strip() or len(note["text"]) > MAX_NOTE_CHARS or "\x00" in note["text"]):
                    raise wrong
                notes.append({"severity": note["severity"], "text": note["text"].strip()})
            data = json.dumps(notes, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            result.append(Output(name, kind, data, value=notes))
        elif kind in ("file", "image"):
            if type(value) is not dict or set(value) != {"path"}:
                raise wrong
            data = _read_out_file(attempt_dir, value["path"], label, MAX_FILE_BYTES)
            if not data:
                raise ContractError("contract_wrong_type", f"{label} is an empty file.")
            shown = file_name(value["path"])
            if kind == "image":
                found = image_type(data)
                if found is None:
                    raise ContractError("contract_wrong_type", f"{label} isn't a PNG, JPEG, GIF, WebP or HEIC picture.")
                mime, extension = found
                if not shown.lower().endswith(extension) and not (extension == ".jpg"
                                                                   and shown.lower().endswith(".jpeg")):
                    shown = f"{shown.rsplit('.', 1)[0] if '.' in shown else shown}{extension}"
            else:
                mime = mimetypes.guess_type(shown, strict=False)[0] or "application/octet-stream"
            result.append(Output(name, kind, data, value={"fileName": shown, "mimeType": mime}))
        else:
            raise wrong
    return result


def has_title(text: str) -> bool:
    for line in text.splitlines():
        if line.strip():
            return _HEADING.match(line.strip()) is not None
    return False


def check_rule(rule: dict, artifact_type: str, data: bytes) -> str | None:
    """None when the rule passes, else a short reason."""
    kind = rule["type"]
    if kind == "number_range":
        try:
            number = float(json.loads(data.decode("ascii")))
        except (ValueError, UnicodeError):
            return f"{rule['of']} isn't a number."
        if not rule["min"] <= number <= rule["max"]:
            return f"{rule['of']} is {number:g}, not between {rule['min']:g} and {rule['max']:g}."
        return None
    text = data.decode("utf-8", "replace")
    if kind == "not_empty":
        if artifact_type == "notes":
            try:
                empty = not json.loads(text)
            except ValueError:
                empty = True
        else:
            empty = not text.strip()
        return f"{rule['of']} is empty." if empty else None
    if kind == "has_title":
        return None if has_title(text) else f"{rule['of']} has no title line."
    if kind == "word_range":
        count = word_count(text)
        if not rule["min"] <= count <= rule["max"]:
            return f"{rule['of']} has {count} words, not between {rule['min']:g} and {rule['max']:g}."
        return None
    return "Unknown rule."
