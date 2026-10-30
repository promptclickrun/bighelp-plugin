"""Gemini Live for the app: a single-use token with the whole session locked in.

The host makes the token with its Gemini key; the phone connects to Gemini with the
token alone, so audio goes straight between the phone and Gemini and the key never
leaves this process. The token fixes the model, the voice, the brief and the tools:
a client that sends only the model name gets exactly this session.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

from .live_voice_provider import LiveProviderError

MODEL = "models/gemini-3.8-live"
TOKEN_URL = "https://generativelanguage.googleapis.com/v1alpha/auth_tokens"
SOCKET_URL = ("wss://generativelanguage.googleapis.com/ws/"
              "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained")
KEY_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
# Gemini answers an unknown name with its default voice instead of an error, so a
# name off this list never reaches it.
VOICES = frozenset({
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus", "Aoede", "Callirrhoe", "Autonoe",
    "Enceladus", "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
    "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
})
START_SECONDS = 60
SESSION_MINUTES = 30
_TOKEN_NAME = re.compile(r"auth_tokens/[A-Za-z0-9_.\-]{1,512}")

# The app answers these; their names are part of the app contract.
TOOLS = [
    {"googleSearch": {}},
    {"functionDeclarations": [
        {"name": "start_work",
         "description": "Start real work on the person's computer or accounts in the background, with all of "
                        "your tools. Returns at once; the result arrives later as an UPDATE.",
         "parameters": {"type": "OBJECT", "properties": {
             "request": {"type": "STRING", "description": "The complete request, in the person's words."}},
             "required": ["request"]}},
        {"name": "check_work",
         "description": "Read what your background work is doing right now."},
    ]},
]


def setup(instructions: str, voice: str) -> dict[str, Any]:
    """The BidiGenerateContent setup the token locks."""
    if voice not in VOICES:
        raise LiveProviderError("unsupported_voice")
    return {
        "model": MODEL,
        "generationConfig": {"responseModalities": ["AUDIO"],
                             "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}},
        "systemInstruction": {"parts": [{"text": instructions}]},
        "tools": TOOLS,
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
        # Without it an audio session ends at about 15 minutes.
        "contextWindowCompression": {"slidingWindow": {}},
    }


def api_key() -> str:
    """The Gemini key of the agent whose profile scope is active, else of this process."""
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        import os
        get_secret = os.environ.get
    for name in KEY_NAMES:
        value = get_secret(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


async def mint(instructions: str, voice: str, *, key: str, http: Any = None,
               now: datetime | None = None, timeout: float = 15) -> dict[str, Any]:
    """A single-use token for one call, and where the app connects with it."""
    if not key:
        raise LiveProviderError("credentials_unavailable", stage="create")
    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(minutes=SESSION_MINUTES)
    body = {"uses": 1, "expireTime": _rfc3339(expires),
            "newSessionExpireTime": _rfc3339(now + timedelta(seconds=START_SECONDS)),
            # No fieldMask: every setting is locked, not only the ones named here.
            "bidiGenerateContentSetup": setup(instructions, voice)}
    import httpx
    owned = http is None
    client = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=False)
    try:
        response = await client.post(TOKEN_URL, json=body, headers={"x-goog-api-key": key},
                                     follow_redirects=False, timeout=timeout)
    except httpx.TimeoutException:
        raise LiveProviderError("setup_timeout", stage="create") from None
    except httpx.HTTPError:
        raise LiveProviderError("create_failed", stage="create") from None
    finally:
        if owned:
            await client.aclose()
    status = response.status_code
    if not 200 <= status < 300:
        code = {400: "create_failed", 401: "authentication_failed", 403: "access_denied",
                429: "rate_limited"}.get(status, "create_failed")
        raise LiveProviderError(code, stage="create", allocation_state="rejected", http_status=status)
    try:
        name = response.json().get("name") if len(response.content) <= 4096 else None
    except ValueError:
        name = None
    if not isinstance(name, str) or not _TOKEN_NAME.fullmatch(name):
        raise LiveProviderError("invalid_token", stage="create")
    return {"transport": "websocket", "url": SOCKET_URL, "token": name, "model": MODEL,
            "expiresAt": int(expires.timestamp())}


def _rfc3339(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
