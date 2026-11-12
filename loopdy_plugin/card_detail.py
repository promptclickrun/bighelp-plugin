"""What a Live Activity row shows beyond its phase, sealed for the phone (sealed-card-v1).

The notification service and Apple read a row's phase, time and helper count. Everything
else (the step in progress, the last steps, a question, the reply's first lines) goes in
`sealed`, which only the phone opens. The phone gives this host one key per chat row over
its own connection, never through the notification service.

Format: unpadded base64url of nonce(12) + AES-256-GCM(JSON) + tag(16).
AAD: "bighelp-card-v1|" + the update's timestamp, which is the row's timestamp on the phone,
so a blob cannot be shown with another update's time.
The JSON's keys are short because five rows share one 4 KB push:
  v 1 · b turn start · s steps so far · t tool in progress · n its note
  tr last finished steps, newest first: {t tool, c how many in a row, n note}
  say the model's latest words between steps · q the question that waits
  p the reply's first lines · h helper goals · cu/cm context tokens used/window · m model
The phone words the steps itself from the tool names (it has the translations).
`fixtures/contracts/sealed-card-v1-vector.json` is shared with the app's opener.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .notification_crypto import b64url_decode, b64url_encode
from .sensitive import contains_sensitive_credential

VERSION = 1
KEY_BYTES = 32
# What the notification service takes for one row.
MAX_SEALED_CHARS = 1_200
# (840 + 28) bytes are 1,158 base64url characters.
_MAX_PLAINTEXT_BYTES = 840
_AAD_LABEL = "bighelp-card-v1|"
_TRAIL = 3


def key_from(value: Any) -> bytes | None:
    """The row key a phone sent, or None when it is not 32 bytes of base64url."""
    if not isinstance(value, str) or len(value) != 43: return None
    try:
        key = b64url_decode(value)
    except ValueError:
        return None
    return key if len(key) == KEY_BYTES else None


def new_detail(started: int) -> dict[str, Any]:
    return {"started": int(started), "steps": 0, "tool": "", "note": "", "trail": [], "say": "",
            "question": "", "preview": "", "goals": {}, "used": None, "window": None, "model": ""}


def _line(value: Any, limit: int) -> str:
    """One bounded line of text, or nothing when it could carry a credential."""
    if not isinstance(value, str): return ""
    text = " ".join(value.split())
    if not text or contains_sensitive_credential(text): return ""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _site(value: Any) -> str:
    if isinstance(value, list): value = value[0] if value else ""
    if not isinstance(value, str): return ""
    host = urlsplit(value if "//" in value else "//" + value).hostname or ""
    return _line(host.removeprefix("www."), 60)


def _note(tool: str, args: Any) -> str:
    """A few words on what this step works on. Never a whole argument list."""
    if not isinstance(args, dict): return ""
    name = tool.lower()
    if "search" in name: return _line(args.get("query") or args.get("pattern"), 80)
    if "web_extract" in name or "browser" in name or "fetch" in name:
        return _site(args.get("url") or args.get("urls"))
    if "terminal" in name: return _line((args.get("command") or "").split("\n")[0] if isinstance(args.get("command"), str) else "", 60)
    if "file" in name or name == "patch":
        path = args.get("path") or args.get("file_path")
        return _line(os.path.basename(path), 60) if isinstance(path, str) else ""
    if "delegate" in name: return _line(args.get("goal"), 80)
    if "image" in name: return _line(args.get("prompt"), 80)
    if "skill" in name: return _line(args.get("name"), 60)
    return ""


def tool_started(detail: dict[str, Any], tool: Any, args: Any) -> None:
    if not isinstance(tool, str) or not tool: return
    detail["steps"] = min(detail["steps"] + 1, 9_999)
    detail["tool"], detail["note"] = tool[:48], _note(tool, args)


def tool_finished(detail: dict[str, Any], tool: Any, args: Any) -> None:
    if not isinstance(tool, str) or not tool: return
    tool, trail = tool[:48], detail["trail"]
    note = detail["note"] if detail["tool"] == tool else _note(tool, args)
    if trail and trail[0]["t"] == tool:
        trail[0]["c"] = min(trail[0]["c"] + 1, 999)
        if note: trail[0]["n"] = note
    else:
        trail.insert(0, {"t": tool, "c": 1, **({"n": note} if note else {})})
        del trail[_TRAIL:]
    if detail["tool"] == tool: detail["tool"], detail["note"] = "", ""


def model_answered(detail: dict[str, Any], payload: dict[str, Any]) -> None:
    """One model request ended (Hermes' post_api_request): its words, the context it used, its model."""
    message = payload.get("assistant_message")
    content = getattr(message, "content", None)
    if isinstance(message, dict): content = message.get("content")
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    said = _line(content, 160)
    if said: detail["say"] = said
    usage, window = payload.get("usage"), payload.get("context_length")
    used = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    if type(used) is int and used > 0: detail["used"] = used
    if type(window) is int and window > 0: detail["window"] = window
    model = payload.get("response_model") or payload.get("model")
    if isinstance(model, str) and model: detail["model"] = _line(model.rsplit("/", 1)[-1], 48)


def plaintext(detail: dict[str, Any], phase: str) -> bytes:
    """The row's JSON, cut down until it fits one row's share of a push."""
    body: dict[str, Any] = {"v": VERSION, "b": detail["started"], "s": detail["steps"]}
    terminal = phase in ("completed", "failed")
    if detail["tool"] and not terminal:
        body["t"] = detail["tool"]
        if detail["note"]: body["n"] = detail["note"]
    if detail["trail"]: body["tr"] = [dict(step) for step in detail["trail"]]
    if detail["say"] and not terminal: body["say"] = detail["say"]
    if phase == "waiting" and detail["question"]: body["q"] = _line(detail["question"], 200)
    if terminal and detail["preview"]: body["p"] = _line(detail["preview"], 280)
    goals = [goal for goal in detail["goals"].values() if goal][:2]
    if goals and not terminal: body["h"] = goals
    if detail["used"] and detail["window"]: body["cu"], body["cm"] = detail["used"], detail["window"]
    if detail["model"]: body["m"] = detail["model"]

    def encoded() -> bytes:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    # Drop what matters least first; the question and the reply's lines go last, and shorter.
    for cut in ("h", "say", "m", "n"):
        if len(encoded()) <= _MAX_PLAINTEXT_BYTES: break
        body.pop(cut, None)
    if len(encoded()) > _MAX_PLAINTEXT_BYTES:
        for step in body.get("tr", []): step.pop("n", None)
    for key in ("p", "q"):
        while len(encoded()) > _MAX_PLAINTEXT_BYTES and len(body.get(key, "")) > 40:
            body[key] = body[key][:len(body[key]) // 2].rstrip() + "…"
    return encoded()


def digest(plain: bytes) -> str:
    """Stands for the detail in an update's signature, so a new step counts as a change."""
    return hashlib.sha256(plain).hexdigest()[:16]


def seal(key: bytes, timestamp: int, plain: bytes, *, nonce: bytes | None = None) -> str:
    nonce = nonce or os.urandom(12)
    sealed = b64url_encode(nonce + AESGCM(key).encrypt(nonce, plain, (_AAD_LABEL + str(int(timestamp))).encode()))
    if len(sealed) > MAX_SEALED_CHARS: raise ValueError("sealed card detail is too large")
    return sealed


def open_sealed(key: bytes, timestamp: int, sealed: str) -> dict[str, Any]:
    """What the phone does. Here for tests and the shared vector."""
    raw = b64url_decode(sealed)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], (_AAD_LABEL + str(int(timestamp))).encode()))
