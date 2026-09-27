"""Secure input the agent can ask for, using Hermes' own secret-capture pathway.

The bighelp app (and the Hermes TUI) answer Hermes' ``secret`` server request with a
masked field. Hermes saves the answer to this profile's ``.env`` through its credential
lifecycle; this tool then lets the agent's terminal and code tools read it as ``$NAME``.
The value never enters the conversation, the tool result or any log: the model only
learns whether it was saved.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

SCHEMA = "bighelp.secure-input"
TOOL_NAME = "bighelp_request_secure_input"
_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
# Names that steer processes rather than hold a secret for a task.
_RESERVED = {"PATH", "HOME", "SHELL", "USER", "LOGNAME", "PWD", "TMPDIR", "TERM", "LANG", "NODE_OPTIONS"}
_RESERVED_PREFIXES = ("HERMES_", "LOOPDY_", "BIGHELP_", "PYTHON", "LD_", "DYLD_", "GIT_", "SSH_", "LC_")

PARAMETERS = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Variable name to save it under, UPPER_SNAKE_CASE, e.g. GITHUB_TOKEN or BANK_PASSWORD.",
        },
        "prompt": {
            "type": "string",
            "description": "One or two plain sentences telling the person what to enter and why.",
        },
        "label": {
            "type": "string",
            "description": "Short field label shown above the input, e.g. \"GitHub token\".",
        },
        "replace": {
            "type": "boolean",
            "description": "Ask again even when a value is already saved (e.g. it stopped working).",
        },
    },
    "required": ["name", "prompt"],
    "additionalProperties": False,
}

DESCRIPTION = (
    "Ask the person to type a password, API key, token or other sensitive value into a secure "
    "pop-up on their device. Never ask for secrets in chat. What they type is saved privately on "
    "this computer and you never see it: afterwards use it only as the environment variable "
    "$NAME in terminal commands or code (for example curl -H \"Authorization: Bearer $GITHUB_TOKEN\"). "
    "Never print, echo or log it. If a value is already saved you get already_saved without a "
    "pop-up; pass replace=true only when the saved one is wrong or expired. Hermes' own AI "
    "provider keys can't be set this way; point the person to Settings for those."
)


def _result(**fields: Any) -> str:
    return json.dumps({"schema": SCHEMA, "version": 1, **fields}, separators=(",", ":"), sort_keys=True)


def _clean(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:limit]


def valid_name(name: Any) -> bool:
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name in _RESERVED:
        return False
    return not name.startswith(_RESERVED_PREFIXES)


def _is_provider_credential(name: str) -> bool:
    """Hermes-managed AI provider keys stay in Settings (fails closed)."""
    try:
        from tools.environments.local_env_policy import _is_hermes_internal_secret, _is_provider_env_blocklisted
    except Exception:
        return True
    return bool(_is_hermes_internal_secret(name) or _is_provider_env_blocklisted(name))


def _already_saved(name: str) -> bool:
    try:
        from agent.secret_scope import get_secret
        return bool(get_secret(name))
    except Exception:
        return bool(os.environ.get(name))


def _capture_callback() -> Callable[..., Any] | None:
    try:
        from tools import skills_tool
    except Exception:
        return None
    return getattr(skills_tool, "_secret_capture_callback", None)


def _has_interactive_session() -> bool:
    """Only an app or TUI session can show the pop-up; messaging chats can't."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return False
    return bool(get_session_env("HERMES_UI_SESSION_ID"))


def _allow_in_commands(name: str) -> None:
    from tools.env_passthrough import register_env_passthrough
    register_env_passthrough([name])


def request(
    args: dict,
    *,
    callback: Callable[..., Any] | None = None,
    interactive: Callable[[], bool] = _has_interactive_session,
    saved: Callable[[str], bool] = _already_saved,
    allow: Callable[[str], None] = _allow_in_commands,
    provider_credential: Callable[[str], bool] = _is_provider_credential,
) -> str:
    name = args.get("name")
    if not valid_name(name):
        return _result(success=False, error="invalid_name",
                       message="Use an UPPER_SNAKE_CASE name like GITHUB_TOKEN (not a system variable).")
    if provider_credential(name):
        return _result(success=False, error="provider_credential", stored_as=name,
                       message="AI provider keys are set in the bighelp app under Settings, not here.")
    prompt = _clean(args.get("prompt"), 400)
    if not prompt:
        return _result(success=False, error="missing_prompt", message="Say what to enter and why.")
    if saved(name) and args.get("replace") is not True:
        allow(name)
        return _result(success=True, already_saved=True, stored_as=name,
                       message=f"Already saved. Use ${name} in commands or code; never print it.")
    capture = callback if callback is not None else _capture_callback()
    if capture is None or not interactive():
        return _result(success=False, error="secure_input_unavailable", stored_as=name,
                       message=f"Secure input needs the bighelp app or the Hermes TUI. Ask the person to add {name} "
                               "to this agent's .env file on the computer instead. Never ask for it in chat.")
    metadata = {"source": "agent"}
    if label := _clean(args.get("label"), 60):
        metadata["label"] = label
    try:
        outcome = capture(name, prompt, metadata)
    except Exception:
        return _result(success=False, error="secure_input_failed", stored_as=name,
                       message="The secure pop-up couldn't be shown. Try again in a moment.")
    if not isinstance(outcome, dict) or not outcome.get("success") or outcome.get("skipped"):
        return _result(success=False, skipped=True, stored_as=name,
                       message="The person didn't enter it. Don't ask for it in chat; continue without it or ask later.")
    allow(name)
    return _result(success=True, stored_as=name,
                   message=f"Saved. Use ${name} in terminal commands or code; never print, echo or log it.")


def supported() -> bool:
    try:
        from tools.env_passthrough import register_env_passthrough  # noqa: F401
        from tools import skills_tool  # noqa: F401
    except Exception:
        return False
    return hasattr(skills_tool, "set_secret_capture_callback")


def register(ctx: Any) -> bool:
    if not supported():
        return False
    ctx.register_tool(
        name=TOOL_NAME,
        toolset="loopdy",
        schema={"name": TOOL_NAME, "description": DESCRIPTION, "parameters": PARAMETERS},
        handler=lambda args, **_: request(args or {}),
        emoji="🔐",
    )
    return True
