"""Submit templates to the bighelp Template Catalog on the person's behalf.

The catalog (``catalog.bighelp.app``) holds every community template for review; nothing is published
until a reviewer approves it. Agents submit through this plugin after the person signs in to GitHub once
per host. The catalog limits submissions per GitHub account (5 a day), so reinstalling the plugin or adding
hosts doesn't add any.

Sign-in uses GitHub's device flow, which needs only the public client ID of bighelp's OAuth App (no scopes,
no secret). It can start from chat (``bighelp_catalog_login``: code, link and a QR picture) or from a
terminal (``hermes bighelp catalog login``: code, link and a text QR). Both share one pending sign-in, so
starting again while a code is still good shows the same code. Whichever device approves finishes it.

After approval the GitHub token is sent to the catalog once, which reads the GitHub user ID and hands back
an install token of its own. The GitHub token is never stored here. The install token, the pending sign-in
and submission receipts live in ``plugin-data/loopdy/catalog`` with owner-only permissions.

Before anything leaves the computer, a submission passes the catalog's own rules, the same key and
password scan as My Templates, and the person's yes in Hermes' approval prompt, every time. Nothing is
submitted when no person can answer (approvals off, scheduled jobs, single queries, unattended platforms,
workflow stages). The tools are in the ``bighelp_templates`` toolset, next to the template tools.

Review results come back without an account: each submission keeps its status receipt, and a throttled
check (tool calls, the CLI, and at most every few hours after a chat turn) posts a Feed item when a
template is approved or rejected. Checks are plain HTTP and never spend AI.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .sensitive import contains_sensitive_credential
from .template_catalog import (
    DEFAULT_MAX_LENGTH,
    KEY,
    LIMIT_MAX_LENGTH,
    MAX_VARIABLES,
    PLACEHOLDER,
    RESERVED_KEYS,
    TYPES as VARIABLE_TYPES,
)
from .template_tools import APPROVAL_PREVIEW, TOOLSET, request_approval, unattended_reason
from .workflow_runner import in_workflow_stage

logger = logging.getLogger(__name__)

LOGIN_TOOL = "bighelp_catalog_login"
SUBMIT_TOOL = "bighelp_submit_catalog_template"
SCHEMA = "bighelp.catalog"

CATALOG_HOST = "catalog.bighelp.app"
DEFAULT_CATALOG_URL = f"https://{CATALOG_HOST}"
# BIGHELP_CATALOG_URL is for tests on this computer. The GitHub and catalog tokens go wherever it points.
_LOOPBACK_HOSTS = ("127.0.0.1", "::1")
# Public client ID of bighelp's GitHub OAuth App "bighelp Template Catalog" (device flow on, no scopes);
# BIGHELP_CATALOG_GITHUB_CLIENT_ID overrides it.
GITHUB_CLIENT_ID = "Ov23liuVeD1RItWCWtV2"
GITHUB_DEVICE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 64 * 1024
MAX_RECEIPTS = 100
STATUS_CHECK_SECONDS = 6 * 3600
MAX_STATUS_TOKENS = 50
# The catalog's install and status tokens: 32 random bytes, base64url without padding.
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class CatalogError(Exception):
    def __init__(self, code: str, message: str, status: int = 0, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.field = field


# MARK: - Configuration and files

def catalog_url() -> str:
    """The catalog, or a test catalog on this computer. Any other address falls back to the real catalog."""
    value = (os.environ.get("BIGHELP_CATALOG_URL") or "").strip()
    if not value:
        return DEFAULT_CATALOG_URL
    origin = _catalog_origin(value)
    if origin is None:
        if value not in _refused_overrides:  # once: the sign-in poller asks every few seconds
            _refused_overrides.add(value)
            logger.warning("bighelp catalog address override refused")
        return DEFAULT_CATALOG_URL
    return origin


_refused_overrides: set[str] = set()


def _catalog_origin(value: str) -> str | None:
    """Scheme, exact host and port only: https to the catalog's own host, or plain http to a loopback address."""
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    if parts.scheme == "https" and parts.hostname == CATALOG_HOST and port in (None, 443):
        return DEFAULT_CATALOG_URL
    if parts.scheme in ("http", "https") and parts.hostname in _LOOPBACK_HOSTS:
        return f"{parts.scheme}://{parts.netloc}"
    return None


def client_id() -> str:
    return (os.environ.get("BIGHELP_CATALOG_GITHUB_CLIENT_ID") or GITHUB_CLIENT_ID).strip()


def _home() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def state_dir(home: Path | None = None) -> Path:
    return (home or _home()) / "plugin-data" / "loopdy" / "catalog"


_lock = threading.Lock()


@contextlib.contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """One writer at a time, across the dashboard and gateway copies of this module too."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _lock, open(directory / ".lock", "a+") as handle:
        try:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        except ImportError:  # Windows: the in-process lock is all we have.
            pass
        yield


def _read(directory: Path, name: str) -> Any:
    try:
        return json.loads((directory / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(directory: Path, name: str, value: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = directory / f".{name}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle)
    os.replace(temporary, directory / name)


def _delete(directory: Path, name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        (directory / name).unlink()


# MARK: - HTTP

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _http(method: str, url: str, *, form: dict | None = None, body: Any = None,
          bearer: str | None = None) -> tuple[int, dict]:
    """One bounded request. Returns (status, JSON object); network trouble raises CatalogError."""
    headers = {"Accept": "application/json", "User-Agent": "bighelp-plugin"}
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = _encoded(body)
        headers["Content-Type"] = "application/json"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT_SECONDS) as response:
            status, raw = response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read(MAX_RESPONSE_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError):
        raise CatalogError("network", "Couldn't reach the server. Try again in a minute.") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CatalogError("response_too_large", "The server's answer was too large.", status)
    try:
        parsed = json.loads(raw or b"{}")
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


def _catalog_error(status: int, body: dict, fallback: str) -> CatalogError:
    message = body.get("error") if isinstance(body.get("error"), str) else fallback
    field = body["field"][:40] if isinstance(body.get("field"), str) else None
    return CatalogError(f"catalog_{status}", message[:300], status, field)


# MARK: - GitHub sign-in

def signed_in(home: Path | None = None) -> dict | None:
    install = _read(state_dir(home), "install.json")
    if isinstance(install, dict) and isinstance(install.get("token"), str) and isinstance(install.get("login"), str):
        return install
    return None


def _pending(directory: Path, now: float) -> dict | None:
    pending = _read(directory, "pending.json")
    if isinstance(pending, dict) and float(pending.get("expiresAt", 0)) > now + 5:
        return pending
    return None


def start_login(home: Path | None = None, *, now: Callable[[], float] = time.time) -> dict:
    """Start (or reuse) the GitHub sign-in. Returns the code and link the person needs."""
    directory = state_dir(home)
    install = signed_in(home)
    if install:
        return {"status": "signed_in", "login": install["login"]}
    if not client_id():
        raise CatalogError("not_configured", "Template submissions aren't set up in this plugin version yet.")
    with _locked(directory):
        pending = _pending(directory, now())
        if pending is None:
            status, body = _http("POST", GITHUB_DEVICE_URL, form={"client_id": client_id(), "scope": ""})
            user_code, device_code = body.get("user_code"), body.get("device_code")
            uri = body.get("verification_uri")
            if status != 200 or not all(isinstance(v, str) for v in (user_code, device_code, uri)):
                raise CatalogError("github_unavailable", "GitHub didn't start the sign-in. Try again in a minute.")
            if not str(uri).startswith("https://github.com/"):
                raise CatalogError("github_unavailable", "GitHub sent an unexpected sign-in link.")
            interval = max(5, min(int(body.get("interval") or 5), 60))
            pending = {
                "deviceCode": device_code, "userCode": user_code[:16], "verificationUri": uri,
                "verificationUriComplete": body.get("verification_uri_complete")
                if str(body.get("verification_uri_complete") or "").startswith("https://github.com/") else None,
                "interval": interval, "nextPollAt": now() + interval,
                "expiresAt": now() + max(60, min(int(body.get("expires_in") or 900), 1800)),
            }
            _write(directory, "pending.json", pending)
    return {
        "status": "waiting",
        "userCode": pending["userCode"],
        "verificationUri": pending["verificationUri"],
        "qrTarget": pending.get("verificationUriComplete") or pending["verificationUri"],
        "expiresInSeconds": int(pending["expiresAt"] - now()),
    }


def poll_login(home: Path | None = None, *, now: Callable[[], float] = time.time) -> dict:
    """Check the pending sign-in once, respecting GitHub's interval. Finishes it when approved."""
    directory = state_dir(home)
    if install := signed_in(home):
        return {"status": "signed_in", "login": install["login"]}
    with _locked(directory):
        pending = _pending(directory, now())
        if pending is None:
            _delete(directory, "pending.json")
            return {"status": "not_started"}
        if now() < float(pending.get("nextPollAt", 0)):
            return {"status": "waiting", "userCode": pending["userCode"]}
        status, body = _http("POST", GITHUB_TOKEN_URL, form={
            "client_id": client_id(), "device_code": pending["deviceCode"], "grant_type": DEVICE_GRANT,
        })
        problem = body.get("error")
        if problem in ("authorization_pending", "slow_down"):
            if problem == "slow_down":
                pending["interval"] = min(int(pending["interval"]) + 5, 60)
            pending["nextPollAt"] = now() + int(pending["interval"])
            _write(directory, "pending.json", pending)
            return {"status": "waiting", "userCode": pending["userCode"]}
        _delete(directory, "pending.json")
        if problem == "access_denied":
            return {"status": "denied"}
        if problem == "expired_token":
            return {"status": "expired"}
        github_token = body.get("access_token")
        if status != 200 or not isinstance(github_token, str):
            return {"status": "failed"}
        # The GitHub token goes to the catalog once and is dropped here.
        status, registered = _http("POST", f"{catalog_url()}/agent/register", body={"githubToken": github_token})
        del github_token
        if status != 201:
            raise _catalog_error(status, registered, "The catalog didn't accept the sign-in.")
        if not isinstance(registered.get("token"), str) or not _TOKEN.match(registered["token"]):
            raise CatalogError("catalog_unexpected", "The catalog sent an unexpected answer. Try again later.")
        install = {"token": registered["token"], "login": str(registered.get("login") or "")[:39],
                   "signedInAt": int(now())}
        _write(directory, "install.json", install)
        return {"status": "signed_in", "login": install["login"]}


def wait_for_login(home: Path | None = None, *, sleep: Callable[[float], None] = time.sleep,
                   now: Callable[[], float] = time.time) -> dict:
    """Poll until the sign-in ends (approved, denied, expired). Used by the CLI and a background thread."""
    while True:
        result = poll_login(home, now=now)
        if result["status"] != "waiting":
            return result
        pending = _read(state_dir(home), "pending.json") or {}
        sleep(max(1.0, float(pending.get("nextPollAt", now() + 5)) - now()))


_pollers: set[str] = set()


def _poll_in_background(home: Path) -> None:
    """One poller per home in this process, so chat sign-ins finish without another tool call."""
    key = str(home)
    with _lock:
        if key in _pollers:
            return
        _pollers.add(key)

    def run() -> None:
        try:
            wait_for_login(home)
        except CatalogError as error:
            logger.info("bighelp catalog sign-in stopped: %s", error.code)
        except Exception:  # noqa: BLE001 - a background thread must never take the host down
            logger.warning("bighelp catalog sign-in poller failed")
        finally:
            with _lock:
                _pollers.discard(key)

    threading.Thread(target=run, name="bighelp-catalog-login", daemon=True).start()


def logout(home: Path | None = None) -> dict:
    directory = state_dir(home)
    with _locked(directory):
        install = signed_in(home)
        _delete(directory, "pending.json")
        if not install:
            return {"status": "signed_out"}
        with contextlib.suppress(CatalogError):
            _http("POST", f"{catalog_url()}/agent/revoke", bearer=install["token"])
        _delete(directory, "install.json")
    return {"status": "signed_out", "login": install["login"]}


# MARK: - QR code

def qr_matrix(text: str) -> list[list[bool]] | None:
    """The QR modules for ``text``, or None when the optional ``qrcode`` package isn't installed."""
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError:
        return None
    code = qrcode.QRCode(border=2, error_correction=ERROR_CORRECT_M)
    code.add_data(text)
    code.make(fit=True)
    return [list(map(bool, row)) for row in code.get_matrix()]


def qr_text(matrix: list[list[bool]]) -> str:
    """Two module rows per line with half blocks. Dark modules print as spaces, so it reads right on the
    usual dark terminal; phone cameras also read the inverted version a light terminal shows."""
    rows = matrix + ([[False] * len(matrix[0])] if len(matrix) % 2 else [])
    glyphs = {(True, True): " ", (True, False): "▄", (False, True): "▀", (False, False): "█"}
    return "\n".join(
        "".join(glyphs[(top, bottom)] for top, bottom in zip(rows[index], rows[index + 1]))
        for index in range(0, len(rows), 2)
    )


def qr_png(matrix: list[list[bool]], scale: int = 10) -> bytes:
    """A black-on-white PNG, written by hand so no imaging library is needed."""
    size = len(matrix) * scale
    raw = bytearray()
    for row in matrix:
        line = bytearray([0])  # filter type: none
        for dark in row:
            line.extend((b"\x00" if dark else b"\xff") * scale)
        raw.extend(bytes(line) * scale)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)  # 8-bit grayscale
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def qr_file(target: str, home: Path | None = None) -> Path | None:
    """Save the QR in Hermes' image cache, a folder Hermes always lets chat deliver from."""
    matrix = qr_matrix(target)
    if matrix is None:
        return None
    from hermes_constants import get_hermes_dir
    directory = get_hermes_dir("cache/images", "image_cache", home=home or _home())
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "bighelp-github-sign-in.png"
    path.write_bytes(qr_png(matrix))
    return path


# MARK: - Templates

# The catalog Worker's own rules (services/catalog/src/templates.ts and variables.ts in the app repo), checked here
# first so an agent fixes a mistake before it asks the person or spends one of the day's submissions. Lengths are
# counted and spaces trimmed the way the Worker's JavaScript does it, and `field` names the same field.
BOARDS = ("feed", "ideas", "goals")
BLUEPRINT_CATEGORIES = ("productivity", "marketing", "content", "personal", "research")
GOAL_CATEGORIES = ("health", "relationships", "finance", "career", "interests", "productivity", "other")
AGENT_CATEGORIES = ("productivity", "business", "coding", "science", "education", "health", "creative", "personal",
                    "leadership", "security")
LIMITS = {"blueprintText": 1_000, "name": 40, "role": 80, "vibe": 120, "description": 240, "instructions": 12_000}
VARIABLE_LIMITS = {"label": 40, "example": 120, "help": 160, "whenEmpty": 200, "option": 60, "options": (2, 12)}
# The Worker refuses request bodies over 32 KB.
MAX_BODY_BYTES = 32 * 1024
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_NEWLINES = re.compile(r"\r\n?")
# JavaScript's white space (String.prototype.trim and \s).
_JS_SPACE = "".join(map(chr, (0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680, *range(0x2000, 0x200B),
                               0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF)))
_JS_SPACES = re.compile("[" + re.escape(_JS_SPACE) + "]+")
# What JavaScript's Number() reads from trimmed text, apart from Infinity (never a finite number anyway).
_JS_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_JS_RADIX = re.compile(r"0(?:[xX][0-9a-fA-F]+|[oO][0-7]+|[bB][01]+)")


def _invalid(field: str, message: str) -> CatalogError:
    return CatalogError("invalid_template", message, field=field)


def _length(text: str) -> int:
    """Length in UTF-16 code units, as the catalog counts it: an emoji counts as two."""
    return len(text.encode("utf-16-le")) // 2


def _optional_text(value: Any, field: str, maximum: int, *, braces: bool = True,
                   shown: int | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _invalid(field, f"{field} must be text.")
    text = _CONTROL.sub("", _NEWLINES.sub("\n", value)).strip(_JS_SPACE)
    if not braces and ("{{" in text or "}}" in text):
        raise _invalid(field, f"{field} can't use {{{{ or }}}}.")
    if _length(text) > maximum:
        raise _invalid(field, f"Keep {field} under {shown or maximum} characters.")
    return text or None


def _optional_line(value: Any, field: str, maximum: int, *, braces: bool = True) -> str | None:
    # Like the catalog: at most twice the limit before runs of spaces become one, then the limit itself.
    text = _optional_text(value, field, maximum * 2, braces=braces, shown=maximum)
    text = _JS_SPACES.sub(" ", text) if text else text
    if text and _length(text) > maximum:
        raise _invalid(field, f"Keep {field} under {maximum} characters.")
    return text or None


def _text(value: Any, field: str, maximum: int, minimum: int) -> str:
    text = _optional_text(value, field, maximum)
    if not text or _length(text) < minimum:
        raise _invalid(field, f"Write at least {minimum} characters in {field}.")
    return text


def _line(value: Any, field: str, maximum: int, minimum: int) -> str:
    text = _optional_line(value, field, maximum)
    if not text or _length(text) < minimum:
        raise _invalid(field, f"{field} is required." if minimum <= 1 else f"{field} needs at least {minimum} characters.")
    return text


def _one_of(value: Any, field: str, allowed: tuple[str, ...]) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    raise _invalid(field, f"{field} must be one of: {', '.join(allowed)}.")


def _flag(value: Any, field: str) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _invalid(field, f"{field} must be true or false.")
    return value


def _finite(value: Any, field: str) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(field, f"{field} must be a number.")
    try:
        number = float(value)
    except OverflowError:
        raise _invalid(field, f"{field} must be a number.") from None
    if not math.isfinite(number):
        raise _invalid(field, f"{field} must be a number.")
    return int(value) if isinstance(value, float) and value.is_integer() else value


def _js_number(text: str) -> float:
    if _JS_RADIX.fullmatch(text):
        return float(int(text, 0))
    return float(text) if _JS_DECIMAL.fullmatch(text) else math.nan


def _whole(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _only(entry: dict, names: tuple[str, ...], allowed: bool, path: str, types: str) -> None:
    # Settings that belong to another type are refused, so a typo can't quietly do nothing.
    if allowed:
        return
    for name in names:
        if entry.get(name) is not None:
            raise _invalid(f"{path}.{name}", f"Only {types} fields use {name}.")


def _variable(entry: Any, path: str) -> dict:
    if not isinstance(entry, dict):
        raise _invalid(path, "Each variable is an object.")

    def field(name: str) -> str:
        return f"{path}.{name}"

    key = entry.get("key")
    if not isinstance(key, str) or KEY.match(key) is None:
        raise _invalid(field("key"), "A key uses lowercase letters, digits and underscores, starts with a letter "
                                     "and has at most 40 characters.")
    if key in RESERVED_KEYS:
        raise _invalid(field("key"), f"{key} is filled in automatically. Take it out of variables.")
    label = _optional_line(entry.get("label"), field("label"), VARIABLE_LIMITS["label"], braces=False)
    if not label:
        raise _invalid(field("label"), "Give it a label.")
    kind = entry.get("type")
    if not isinstance(kind, str) or kind not in VARIABLE_TYPES:
        raise _invalid(field("type"), f"type must be one of: {', '.join(VARIABLE_TYPES)}.")
    required = _flag(entry.get("required"), field("required"))
    variable: dict[str, Any] = {"key": key, "label": label, "type": kind,
                                "required": True if required is None else required}
    for name in ("example", "help"):
        text = _optional_line(entry.get(name), field(name), VARIABLE_LIMITS[name], braces=False)
        if text:
            variable[name] = text

    _only(entry, ("maxLength",), kind in ("text", "long_text"), path, "text and long_text")
    _only(entry, ("options", "allowOther"), kind == "choice", path, "choice")
    _only(entry, ("min", "max"), kind == "number", path, "number")

    if kind in ("text", "long_text"):
        ceiling = LIMIT_MAX_LENGTH[kind]
        if "maxLength" in entry:
            maximum = _whole(entry["maxLength"])
            if maximum is None or not 1 <= maximum <= ceiling:
                raise _invalid(field("maxLength"), f"Use a whole number from 1 to {ceiling}.")
            variable["maxLength"] = maximum
        limit = variable.get("maxLength", DEFAULT_MAX_LENGTH[kind])
        default = (_optional_line if kind == "text" else _optional_text)(entry.get("default"), field("default"), limit,
                                                                         braces=False)
        if default:
            variable["default"] = default
    elif kind == "choice":
        options = entry.get("options")
        fewest, most = VARIABLE_LIMITS["options"]
        if not isinstance(options, list) or not fewest <= len(options) <= most:
            raise _invalid(field("options"), f"Give {fewest} to {most} options.")
        cleaned = []
        for index, option in enumerate(options):
            name = f"{field('options')}[{index}]"
            text = _optional_line(option, name, VARIABLE_LIMITS["option"], braces=False)
            if not text:
                raise _invalid(name, "Options can't be empty.")
            cleaned.append(text)
        if len(set(cleaned)) != len(cleaned):
            raise _invalid(field("options"), "Each option must be different.")
        variable["options"] = cleaned
        allow_other = _flag(entry.get("allowOther"), field("allowOther"))
        if allow_other:
            variable["allowOther"] = True
        default = _optional_line(entry.get("default"), field("default"), VARIABLE_LIMITS["option"], braces=False)
        if default:
            if default not in cleaned and not allow_other:
                raise _invalid(field("default"), "Use one of the options.")
            variable["default"] = default
    else:
        minimum = _finite(entry.get("min"), field("min"))
        maximum = _finite(entry.get("max"), field("max"))
        if minimum is not None and maximum is not None and minimum > maximum:
            raise _invalid(field("max"), "max must be at least min.")
        if minimum is not None:
            variable["min"] = minimum
        if maximum is not None:
            variable["max"] = maximum
        raw = entry.get("default")
        if isinstance(raw, str) and raw.strip(_JS_SPACE):
            raw = _js_number(raw.strip(_JS_SPACE))
        number = _finite(raw, field("default"))
        if number is not None:
            if (minimum is not None and number < minimum) or (maximum is not None and number > maximum):
                raise _invalid(field("default"), "Keep the default between min and max.")
            variable["default"] = number

    if entry.get("whenEmpty") is not None:
        if variable["required"]:
            raise _invalid(field("whenEmpty"), "Only optional fields (required: false) use whenEmpty.")
        text = _optional_text(entry["whenEmpty"], field("whenEmpty"), VARIABLE_LIMITS["whenEmpty"], braces=False)
        if text:
            variable["whenEmpty"] = text
    return variable


def _variables(value: Any, texts: dict[str, str | None]) -> list[dict]:
    """The declared fill-in fields, checked against the text that uses them (TEMPLATE_VARIABLES.md)."""
    used: dict[str, str] = {}
    for field, text in texts.items():
        if not text:
            continue
        rest = PLACEHOLDER.sub("", text)
        if "{{" in rest or "}}" in rest:
            raise _invalid(field, "Use {{ and }} only around a fill-in field's key, like {{agent_role}}.")
        for key in PLACEHOLDER.findall(text):
            used.setdefault(key, field)
    variables: list[dict] = []
    if value is not None:
        if not isinstance(value, list):
            raise _invalid("variables", "variables must be a list.")
        if len(value) > MAX_VARIABLES:
            raise _invalid("variables", f"Use at most {MAX_VARIABLES} variables.")
        seen: set[str] = set()
        for index, entry in enumerate(value):
            variable = _variable(entry, f"variables[{index}]")
            key = variable["key"]
            if key in seen:
                raise _invalid(f"variables[{index}].key", f"{key} is already in variables.")
            seen.add(key)
            if key not in used:
                raise _invalid(f"variables[{index}].key",
                               f"The text never uses {{{{{key}}}}}. Use it or take it out of variables.")
            variables.append(variable)
    declared = {variable["key"] for variable in variables}
    for key, field in used.items():
        if key not in RESERVED_KEYS and key not in declared:
            raise _invalid(field, f"{{{{{key}}}}} isn't in variables. Add it there or take it out of the text.")
    return variables


def validate_template(args: Any) -> dict:
    """The template exactly as the catalog will take it, or CatalogError naming the field to fix."""
    if not isinstance(args, dict):
        raise _invalid("body", "Send the template as an object.")
    kind = args.get("kind")
    if kind == "blueprint":
        board = _one_of(args.get("board"), "board", BOARDS)
        template = {
            "kind": kind, "board": board,
            "category": _one_of(args.get("category"), "category", BLUEPRINT_CATEGORIES),
            "text": _text(args.get("text"), "text", LIMITS["blueprintText"], 20),
        }
        if board == "goals":
            goal = args.get("goalCategory")
            template["goalCategory"] = _one_of("other" if goal is None else goal, "goalCategory", GOAL_CATEGORIES)
        return template
    if kind != "agent":
        raise _invalid("kind", "kind must be blueprint or agent.")
    template = {
        "kind": kind,
        "name": _line(args.get("name"), "name", LIMITS["name"], 1),
        "role": _line(args.get("role"), "role", LIMITS["role"], 2),
        "vibe": _line(args.get("vibe"), "vibe", LIMITS["vibe"], 2),
        "instructions": _text(args.get("instructions"), "instructions", LIMITS["instructions"], 80),
        "category": _one_of(args.get("category"), "category", AGENT_CATEGORIES),
    }
    description = _optional_line(args.get("description"), "description", LIMITS["description"])
    if description:
        template["description"] = description
    for field in ("name", "vibe"):
        if "{{" in template[field] or "}}" in template[field]:
            raise _invalid(field, f"Fill-in fields go in the instructions, role or description, not in {field}.")
    variables = _variables(args.get("variables"), {
        "role": template["role"], "description": description, "instructions": template["instructions"],
    })
    if variables:
        template["variables"] = variables
    return template


def _encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _texts(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)


def prepare(args: Any) -> dict:
    """The template as it would leave this computer: the catalog's rules, then the My Templates secret scan."""
    template = validate_template(args)
    if any(contains_sensitive_credential(text) for text in _texts(template)):
        raise CatalogError("secret_scan_blocked", "Templates can't hold keys or passwords. Take them out and try again.")
    if len(_encoded(template)) > MAX_BODY_BYTES:
        raise CatalogError("too_large", "The template is too large for the catalog (32 KB). Make it shorter.")
    return template


def _title(template: dict) -> str:
    title = template.get("name") or template.get("text", "")
    return title if len(title) <= 80 else title[:77].rstrip() + "…"


# MARK: - The person's yes

def _unattended() -> str | None:
    """Why no person can say yes now, or None. Same checks as the template tools, plus workflow stages."""
    if in_workflow_stage():
        return "workflow_stage"
    return unattended_reason()


def _approve(description: str, rule_key: str) -> Any:
    return request_approval(description, rule_key, tool=SUBMIT_TOOL)


def _approval_text(template: dict, login: str) -> str:
    if template["kind"] == "agent":
        lines = [f"Agent template \"{template['name']}\" ({template['category']})", f"Role: {template['role']}",
                 f"Vibe: {template['vibe']}"]
        if template.get("description"):
            lines.append(f"Description: {template['description']}")
        if template.get("variables"):
            lines.append("Fill-in fields: " + ", ".join(variable["label"] for variable in template["variables"]))
        body = template["instructions"]
    else:
        lines = [f"Blueprint for the {template['board'].capitalize()} board ({template['category']})"]
        body = template["text"]
    preview = body if len(body) <= APPROVAL_PREVIEW else body[:APPROVAL_PREVIEW] + "…"
    return (f"Submit this template to the public bighelp Template Catalog as GitHub user @{login}? A reviewer "
            "reads it first. If it's approved, anyone can see it, with your GitHub name.\n\n"
            + "\n".join(lines) + "\n\n" + preview)


def _consent(template: dict, login: str) -> None:
    """Hermes' approval prompt for this one submission. Refuses without asking when no person can answer."""
    if _unattended() is not None:
        raise CatalogError("approval_unavailable", "No one can approve this right now (approvals are off, or this is "
                                                   "a scheduled or unattended run), so nothing was submitted.")
    digest = hashlib.sha256(json.dumps([login, template], sort_keys=True).encode("utf-8")).hexdigest()[:12]
    try:
        answer = _approve(_approval_text(template, login), f"bighelp-catalog-submit:{digest}")
    except Exception:
        logger.warning("bighelp catalog submission approval failed")
        raise CatalogError("not_approved", "The approval prompt failed, so nothing was submitted.") from None
    if not isinstance(answer, Mapping) or answer.get("approved") is not True:
        message = answer.get("message") if isinstance(answer, Mapping) else None
        reason = message[:300] if isinstance(message, str) and message else "The person didn't approve it."
        raise CatalogError("not_approved", f"Nothing was submitted. {reason}")


def submit(args: dict, home: Path | None = None) -> dict:
    """Check the template, scan it, ask the person, then send it. Nothing leaves before each step passes."""
    template = prepare(args)
    directory = state_dir(home)
    install = signed_in(home)
    if not install:
        raise CatalogError("not_signed_in", "Sign in to GitHub first (bighelp_catalog_login).")
    _consent(template, install["login"])
    # The person may take a while: send only with the sign-in they said yes to.
    current = signed_in(home)
    if not current or current["token"] != install["token"]:
        raise CatalogError("not_signed_in", "This computer's catalog sign-in changed before the answer came, so "
                                            "nothing was submitted. Try again.")
    status, body = _http("POST", f"{catalog_url()}/submit/agent/templates", body=template, bearer=install["token"])
    if status == 401:
        # Revoked, banned or signed out elsewhere: forget it so the next step is a fresh sign-in.
        with _locked(directory):
            _delete(directory, "install.json")
        raise CatalogError("not_signed_in", "This computer's catalog sign-in ended. Sign in to GitHub again.", 401)
    token = body.get("statusToken")
    if status != 201 or not isinstance(token, str) or not _TOKEN.match(token):
        raise _catalog_error(status, body, "The catalog didn't take the template.")
    with _locked(directory):
        receipts = _read(directory, "submissions.json") or []
        receipts.insert(0, {"id": str(body.get("id"))[:40], "title": _title(template), "kind": template["kind"],
                            "statusToken": token, "status": "pending", "submittedAt": int(time.time())})
        _write(directory, "submissions.json", receipts[:MAX_RECEIPTS])
    return {"status": "pending", "id": body.get("id"), "credit": body.get("credit"),
            "remainingToday": body.get("remainingToday")}


# MARK: - Review results

Notify = Callable[[dict], None]


def _feed_post(home: Path) -> Notify:
    def post(receipt: dict) -> None:
        from .agent_board import store_for_home
        approved = receipt["status"] == "approved"
        body = ("It's live in the Template Catalog now, with your GitHub name as the credit." if approved
                else f"The reviewer said: {receipt.get('reviewNote') or 'no reason given.'}")
        store_for_home(home).publish(
            "feed", title=f"Template {'approved' if approved else 'not accepted'}: {receipt['title']}"[:200],
            body=body, icon="🎉" if approved else "📝", source="Template Catalog",
            item_id=f"catalog-{receipt['id']}"[:64])
    return post


def check_statuses(home: Path | None = None, *, force: bool = False, notify: Notify | None = None,
                   now: Callable[[], float] = time.time) -> list[dict]:
    """Ask the catalog how pending submissions did; post each new result to Feed once. Throttled."""
    home = home or _home()
    directory = state_dir(home)
    if not (directory / "submissions.json").exists():
        return []
    with _locked(directory):
        receipts = _read(directory, "submissions.json") or []
        checked = _read(directory, "checked.json") or {}
        waiting = [r for r in receipts if r.get("status") == "pending"]
        if not waiting or (not force and now() - float(checked.get("at", 0)) < STATUS_CHECK_SECONDS):
            return []
        _write(directory, "checked.json", {"at": now()})
        tokens = [r["statusToken"] for r in waiting][:MAX_STATUS_TOKENS]
        try:
            status, body = _http("POST", f"{catalog_url()}/submit/status", body={"tokens": tokens})
        except CatalogError:
            return []
        if status != 200 or not isinstance(body.get("submissions"), list):
            return []
        results = {item.get("id"): item for item in body["submissions"] if isinstance(item, dict)}
        changed = []
        for receipt in waiting:
            result = results.get(receipt["id"])
            if result and result.get("status") in ("approved", "rejected"):
                receipt["status"] = result["status"]
                receipt["reviewNote"] = str(result.get("reviewNote") or "")[:1000] or None
                changed.append(receipt)
        if changed:
            _write(directory, "submissions.json", receipts)
    post = notify or _feed_post(home)
    for receipt in changed:
        try:
            post(receipt)
        except Exception:  # noqa: BLE001 - a Feed failure must not lose the status we just saved
            logger.warning("bighelp catalog result could not be posted to Feed")
    return changed


def recent(home: Path | None = None, limit: int = 10) -> list[dict]:
    receipts = _read(state_dir(home), "submissions.json") or []
    return [{k: r.get(k) for k in ("id", "title", "kind", "status", "reviewNote")} for r in receipts[:limit]]


# MARK: - Tools

_BRIDGE = (" Call it directly when it is in your tool list; when Hermes has hidden it, use tool_search, "
           "tool_describe and tool_call to run this exact tool.")

LOGIN_DESCRIPTION = (
    "Sign this computer in to the bighelp Template Catalog with GitHub, so you can submit templates for "
    "the user. action 'start' returns a code, a link and a QR picture: show the code, the link, and put the "
    "returned media line on its own line so the QR shows in chat (they tap the link on this device, or scan "
    "the QR with another). They approve on GitHub; sign-in finishes by itself. 'status' says whether "
    "they're signed in and how recent submissions did. 'logout' signs this computer out. Only start a "
    "sign-in when the user wants to submit templates." + _BRIDGE
)
LOGIN_PARAMETERS = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ["start", "status", "logout"]}},
    "required": ["action"],
    "additionalProperties": False,
}

SUBMIT_DESCRIPTION = (
    "Submit a template to the public bighelp Template Catalog for review, credited to the user's GitHub name. "
    "Only when the user asked for this template to be submitted. The user approves each submission in Hermes' "
    "approval prompt; scheduled and unattended runs can't submit. Templates that hold keys or passwords are "
    "refused. A reviewer reads every submission before anyone else sees it, and the result shows up in the "
    "user's Feed. Limit: 5 a day per GitHub account. kind 'blueprint' is a prompt for the Feed, Ideas or Goals "
    "board (board, category, text of 20 to 1,000 characters; goals also take goalCategory). kind 'agent' is an "
    "agent personality (name, role, vibe, optional description, category, instructions of 80 to 12,000 "
    "characters). In its instructions, role or description, {{agent_name}} stands for the name a person gives "
    "it and {{user_name}} for the person's name; declare every other {{key}} in variables, the fields the app "
    "asks for. On invalid_template, fix the named field. If the user isn't signed in, run "
    "bighelp_catalog_login first." + _BRIDGE
)
_VARIABLE = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "maxLength": 40,
                "description": "Lowercase letters, digits and _, starting with a letter. Used as {{key}} in the text."},
        "label": {"type": "string", "maxLength": VARIABLE_LIMITS["label"]},
        "type": {"type": "string", "enum": list(VARIABLE_TYPES)},
        "required": {"type": "boolean", "description": "true if not given."},
        "default": {"anyOf": [{"type": "string"}, {"type": "number"}],
                    "description": "The value the form shows first. For choice, one of options."},
        "example": {"type": "string", "maxLength": VARIABLE_LIMITS["example"]},
        "help": {"type": "string", "maxLength": VARIABLE_LIMITS["help"]},
        "maxLength": {"type": "integer", "minimum": 1, "maximum": LIMIT_MAX_LENGTH["long_text"],
                      "description": "text and long_text only. text: 80 if not given, at most 200. "
                                     "long_text: 600 if not given, at most 4000."},
        "options": {"type": "array", "items": {"type": "string", "maxLength": VARIABLE_LIMITS["option"]},
                    "minItems": VARIABLE_LIMITS["options"][0], "maxItems": VARIABLE_LIMITS["options"][1],
                    "description": "choice only."},
        "allowOther": {"type": "boolean", "description": "choice only: adds a free-text Other."},
        "min": {"type": "number", "description": "number only."},
        "max": {"type": "number", "description": "number only."},
        "whenEmpty": {"type": "string", "maxLength": VARIABLE_LIMITS["whenEmpty"],
                      "description": "Optional fields only: the text used when the person leaves it empty."},
    },
    "required": ["key", "label", "type"],
}
SUBMIT_PARAMETERS = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["blueprint", "agent"]},
        "board": {"type": "string", "enum": list(BOARDS)},
        "category": {"type": "string", "enum": sorted(set(BLUEPRINT_CATEGORIES) | set(AGENT_CATEGORIES)),
                     "description": f"Blueprints: {', '.join(BLUEPRINT_CATEGORIES)}. "
                                    f"Agents: {', '.join(AGENT_CATEGORIES)}."},
        "goalCategory": {"type": "string", "enum": list(GOAL_CATEGORIES)},
        "text": {"type": "string", "maxLength": LIMITS["blueprintText"], "description": "Blueprint prompt text."},
        **{field: {"type": "string", "maxLength": LIMITS[field]}
           for field in ("name", "role", "vibe", "description", "instructions")},
        "variables": {"type": "array", "maxItems": MAX_VARIABLES, "items": _VARIABLE,
                      "description": "Agents only: the fill-in fields for the {{key}}s in the text, in the order "
                                     "the form asks for them."},
    },
    "required": ["kind", "category"],
    "additionalProperties": False,
}


def _result(**fields: Any) -> str:
    return json.dumps({"schema": SCHEMA, "version": 1, **fields}, ensure_ascii=False, separators=(",", ":"))


def _failure(error: CatalogError) -> str:
    extra = {"field": error.field} if error.field else {}
    return _result(ok=False, error=error.code, message=str(error), **extra)


def handle_login(args: dict) -> str:
    action = (args or {}).get("action")
    home = _home()
    try:
        if action == "start":
            result = start_login(home)
            if result["status"] == "waiting":
                qr = qr_file(result.pop("qrTarget"), home)
                if qr is not None:
                    result["media"] = f"MEDIA:{qr}"
                _poll_in_background(home)
            return _result(ok=True, **result)
        if action == "status":
            result = poll_login(home)
            check_statuses(home)
            return _result(ok=True, **result, submissions=recent(home))
        if action == "logout":
            return _result(ok=True, **logout(home))
        return _result(ok=False, error="invalid_action", message="Use start, status or logout.")
    except CatalogError as error:
        return _failure(error)


def handle_submit(args: dict) -> str:
    home = _home()
    try:
        prepare(args or {})  # the catalog's rules and the secret scan come before any request
        poll_login(home)  # finishes a sign-in approved since the last call
        return _result(ok=True, **submit(args or {}, home))
    except CatalogError as error:
        return _failure(error)


def _after_turn(**_payload: Any) -> None:
    """Cheap, throttled review check after chat turns; real work only every few hours, off-thread."""
    try:
        home = _home()
        directory = state_dir(home)
        checked = _read(directory, "checked.json") or {}
        if not (directory / "submissions.json").exists() or time.time() - float(checked.get("at", 0)) < STATUS_CHECK_SECONDS:
            return
        threading.Thread(target=check_statuses, args=(home,), name="bighelp-catalog-status", daemon=True).start()
    except Exception:  # noqa: BLE001 - hooks must never break a turn
        logger.debug("bighelp catalog status check skipped")


def register(ctx: Any) -> None:
    ctx.register_tool(
        name=LOGIN_TOOL, toolset=TOOLSET,
        schema={"name": LOGIN_TOOL, "description": LOGIN_DESCRIPTION, "parameters": LOGIN_PARAMETERS},
        handler=lambda args, **_: handle_login(args), emoji="🔑",
    )
    ctx.register_tool(
        name=SUBMIT_TOOL, toolset=TOOLSET,
        schema={"name": SUBMIT_TOOL, "description": SUBMIT_DESCRIPTION, "parameters": SUBMIT_PARAMETERS},
        handler=lambda args, **_: handle_submit(args), emoji="📮",
    )
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("post_llm_call", _after_turn)


# MARK: - CLI (`hermes bighelp catalog …`)

def setup_cli(actions: Any) -> None:
    catalog = actions.add_parser("catalog", help="Sign in to submit templates to the bighelp Template Catalog")
    catalog_actions = catalog.add_subparsers(dest="bighelp_catalog_action", required=True)
    catalog_actions.add_parser("login", help="Sign in with GitHub (shows a code, a link and a QR)")
    catalog_actions.add_parser("status", help="Show sign-in and recent submissions")
    catalog_actions.add_parser("logout", help="Sign this computer out")


def handle_cli(args: Any, *, out: Callable[[str], None] = print) -> None:
    action = getattr(args, "bighelp_catalog_action", "")
    home = _home()
    try:
        if action == "login":
            result = start_login(home)
            if result["status"] == "signed_in":
                out(f"Already signed in as @{result['login']}.")
                return
            matrix = qr_matrix(result["qrTarget"])
            if matrix is not None:
                out(qr_text(matrix))
            out(f"Open {result['verificationUri']} and enter {result['userCode']}")
            out("Waiting for you to approve on GitHub. Press Ctrl-C to stop; the code stays good for "
                f"{result['expiresInSeconds'] // 60} minutes and works from chat too.")
            result = wait_for_login(home)
            messages = {"signed_in": f"Signed in as @{result.get('login')}.",
                        "denied": "GitHub sign-in was declined.", "expired": "The code expired. Run login again."}
            out(messages.get(result["status"], "Sign-in didn't finish. Run login again."))
        elif action == "status":
            result = poll_login(home)
            check_statuses(home, force=True)
            out(f"Signed in as @{result['login']}." if result["status"] == "signed_in" else "Not signed in.")
            for receipt in recent(home):
                out(f"  {receipt['status']:<9} {receipt['title']}")
        elif action == "logout":
            result = logout(home)
            out(f"Signed out @{result['login']}." if result.get("login") else "Not signed in.")
    except CatalogError as error:
        out(str(error))
