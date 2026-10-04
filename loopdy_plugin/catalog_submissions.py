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

Review results come back without an account: each submission keeps its status receipt, and a throttled
check (tool calls, the CLI, and at most every few hours after a chat turn) posts a Feed item when a
template is approved or rejected. Checks are plain HTTP and never spend AI.
"""
from __future__ import annotations

import contextlib
import json
import logging
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
from typing import Any, Callable, Iterator

from .naming import TOOLSET

logger = logging.getLogger(__name__)

LOGIN_TOOL = "bighelp_catalog_login"
SUBMIT_TOOL = "bighelp_submit_catalog_template"
SCHEMA = "bighelp.catalog"

DEFAULT_CATALOG_URL = "https://catalog.bighelp.app"
# Public client ID of bighelp's GitHub OAuth App (device flow on, no scopes). Empty until it's registered;
# BIGHELP_CATALOG_GITHUB_CLIENT_ID overrides it.
GITHUB_CLIENT_ID = ""
GITHUB_DEVICE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"

TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 64 * 1024
MAX_RECEIPTS = 100
STATUS_CHECK_SECONDS = 6 * 3600
MAX_STATUS_TOKENS = 50

# The catalog's own rules (services/catalog/src/templates.ts), checked here first so an agent fixes a
# mistake before it spends one of the day's submissions.
BOARDS = ("feed", "ideas", "goals")
BLUEPRINT_CATEGORIES = ("productivity", "marketing", "content", "personal", "research")
GOAL_CATEGORIES = ("health", "relationships", "finance", "career", "interests", "productivity", "other")
AGENT_CATEGORIES = ("work", "personal", "learning", "creative", "research", "support", "fun")
# field: (maximum, minimum, multi-line)
_AGENT_FIELDS = {
    "name": (40, 1, False),
    "role": (80, 2, False),
    "vibe": (120, 2, False),
    "description": (240, 0, False),
    "instructions": (12_000, 80, True),
}
_BLUEPRINT_TEXT = (1_000, 20)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class CatalogError(Exception):
    def __init__(self, code: str, message: str, status: int = 0, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.field = field


# MARK: - Configuration and files

def catalog_url() -> str:
    value = (os.environ.get("BIGHELP_CATALOG_URL") or DEFAULT_CATALOG_URL).strip().rstrip("/")
    if not value.startswith("https://") and not value.startswith("http://127.0.0.1"):
        return DEFAULT_CATALOG_URL
    return value


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
        data = json.dumps(body).encode()
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
        if status != 201 or not isinstance(registered.get("token"), str):
            raise _catalog_error(status, registered, "The catalog didn't accept the sign-in.")
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

def _clean(value: Any, field: str, maximum: int, minimum: int, multiline: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CatalogError("invalid_template", f"{field} must be text.")
    text = _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not multiline:
        text = " ".join(text.split())
    if len(text) > maximum:
        raise CatalogError("invalid_template", f"Keep {field} under {maximum} characters.")
    if len(text) < minimum:
        raise CatalogError("invalid_template", f"{field} needs at least {minimum} characters." if minimum > 1
                           else f"{field} is required.")
    return text


def _choice(value: Any, field: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise CatalogError("invalid_template", f"{field} must be one of: {', '.join(allowed)}.")
    return value


def validate_template(args: dict) -> dict:
    """The template exactly as the catalog will see it, or CatalogError with what to fix."""
    kind = _choice(args.get("kind"), "kind", ("blueprint", "agent"))
    if kind == "blueprint":
        board = _choice(args.get("board"), "board", BOARDS)
        template = {
            "kind": kind, "board": board,
            "category": _choice(args.get("category"), "category", BLUEPRINT_CATEGORIES),
            "text": _clean(args.get("text"), "text", *_BLUEPRINT_TEXT, True),
        }
        if board == "goals":
            template["goalCategory"] = _choice(args.get("goalCategory") or "other", "goalCategory", GOAL_CATEGORIES)
        return template
    template = {"kind": kind, "category": _choice(args.get("category"), "category", AGENT_CATEGORIES)}
    for field, (maximum, minimum, multiline) in _AGENT_FIELDS.items():
        value = _clean(args.get(field), field, maximum, minimum, multiline)
        if value:
            template[field] = value
    return template


def _title(template: dict) -> str:
    title = template.get("name") or template.get("text", "")
    return title if len(title) <= 80 else title[:77].rstrip() + "…"


def submit(args: dict, home: Path | None = None) -> dict:
    template = validate_template(args)
    directory = state_dir(home)
    install = signed_in(home)
    if not install:
        raise CatalogError("not_signed_in", "Sign in to GitHub first (bighelp_catalog_login).")
    status, body = _http("POST", f"{catalog_url()}/submit/agent/templates", body=template, bearer=install["token"])
    if status == 401:
        # Revoked, banned or signed out elsewhere: forget it so the next step is a fresh sign-in.
        with _locked(directory):
            _delete(directory, "install.json")
        raise CatalogError("not_signed_in", "This computer's catalog sign-in ended. Sign in to GitHub again.", 401)
    if status != 201 or not isinstance(body.get("statusToken"), str):
        raise _catalog_error(status, body, "The catalog didn't take the template.")
    with _locked(directory):
        receipts = _read(directory, "submissions.json") or []
        receipts.insert(0, {"id": str(body.get("id"))[:40], "title": _title(template), "kind": template["kind"],
                            "statusToken": body["statusToken"], "status": "pending", "submittedAt": int(time.time())})
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

LOGIN_DESCRIPTION = (
    "Sign this computer in to the bighelp Template Catalog with GitHub, so you can submit templates for "
    "the user. action 'start' returns a code, a link and a QR picture: show the code, the link, and put the "
    "returned media line on its own line so the QR shows in chat (they tap the link on this device, or scan "
    "the QR with another). They approve on GitHub; sign-in finishes by itself. 'status' says whether "
    "they're signed in and how recent submissions did. 'logout' signs this computer out. Only start a "
    "sign-in when the user wants to submit templates."
)
LOGIN_PARAMETERS = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ["start", "status", "logout"]}},
    "required": ["action"],
    "additionalProperties": False,
}

SUBMIT_DESCRIPTION = (
    "Submit a template to the bighelp Template Catalog for review, credited to the user's GitHub name. "
    "Only when the user asked for this template to be submitted. A reviewer reads every submission before "
    "anyone else sees it, and the result shows up in the user's Feed. Limit: 5 a day per GitHub account. "
    "kind 'blueprint' is a prompt for the Feed, Ideas or Goals board (board, category, text; goals also "
    "take goalCategory). kind 'agent' is an agent personality (name, role, vibe, optional description, "
    "category, instructions of at least 80 characters, where {{agent_name}} stands for the name a person "
    "gives it). If the user isn't signed in, run bighelp_catalog_login first."
)
SUBMIT_PARAMETERS = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["blueprint", "agent"]},
        "board": {"type": "string", "enum": list(BOARDS)},
        "category": {"type": "string", "enum": sorted(set(BLUEPRINT_CATEGORIES) | set(AGENT_CATEGORIES)),
                     "description": f"Blueprints: {', '.join(BLUEPRINT_CATEGORIES)}. "
                                    f"Agents: {', '.join(AGENT_CATEGORIES)}."},
        "goalCategory": {"type": "string", "enum": list(GOAL_CATEGORIES)},
        "text": {"type": "string", "maxLength": _BLUEPRINT_TEXT[0], "description": "Blueprint prompt text."},
        **{field: {"type": "string", "maxLength": maximum} for field, (maximum, _, _) in _AGENT_FIELDS.items()},
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
