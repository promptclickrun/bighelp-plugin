"""Where an agent's files are on this computer, decided the way Hermes decides.

An agent's chats run in its terminal working folder. When the profile's
``terminal.cwd`` names one, that is it: ``~`` is the user's home and a relative
path is relative to the folder Hermes was started in, as in Hermes. When it is
unset or a placeholder (``.``, ``auto``, ``cwd``), Hermes picks the folder a new
chat starts in. This module asks Hermes' own chat gateway for that answer (the
``config.get`` request with key ``project``, which Hermes Desktop uses too) and,
on hosts without it, applies the same rule: ``TERMINAL_CWD``, else the folder
Hermes was started in (``tui_gateway`` ``_completion_cwd``).

The plugin only serves files it can open safely on this computer, so each case
it can't serve says why with its own code:

- container backends keep the agent's files inside the container, unless Hermes
  bind-mounts a host folder for it (``docker_mount_cwd_to_workspace`` or a
  ``docker_volumes`` entry), whose host side is then the workspace;
- an SSH backend keeps them on the other computer;
- Windows lacks the descriptor-relative opens the confined traversal relies on.

A default that lands on the disk root, Hermes' own folders or its install is
not a working folder of the agent's own, and Hermes' own folders are never a
workspace: they hold its settings and keys. Hosted Hermes (the Docker image the
Nous Portal runs) starts in its home, which is also the user's home, so its
chats start there. Hermes makes a ``workspace`` folder in each home for the
agent's files (the image's stage2 hook, ``hermes profile create``); when the
agent works in Hermes' home, that folder is its workspace. Secret-bearing names
inside a workspace in Hermes' folder are never shown, as in Hermes' own Files
tab.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .native_context import NativeAPIError, PROFILE_ID


_IS_WINDOWS = os.name == "nt"
# Hermes' "not configured" spellings (gateway/cwd_placeholder.py, tools/file_tools_paths.py).
CWD_PLACEHOLDERS = frozenset({"", ".", "./", "auto", "cwd"})
_TRUE = frozenset({"true", "1", "yes", "on"})
# Hermes' docker volume spec: host:container[:mode]; a drive-letter host keeps its colon.
_VOLUME_SPEC = re.compile(r"^(?P<host>.+):(?P<container>/[^:]+)(?::[^:]*)?$")
# Host-shaped paths a container can't use as its working folder (tools/terminal_tool_config.py).
_HOST_SHAPED = re.compile(r"^(?:/Users/|/home/|[A-Za-z]:[\\/])")
_CONTAINER_HOME = "/root"
_MAX_PATH_BYTES = 4_096
_MAX_VOLUMES = 64
_MAX_PROFILES = 256
HERMES_WORKSPACE = "hermes-workspace"
# Credential stores Hermes' own Files tab never lists or reads, by name, wherever
# they are (hermes_cli/web_routers/files.py, agent/file_safety.py), plus the
# Google Meet node token Hermes keeps in its workspace folder.
_SECRET_FILE_NAMES = frozenset({
    "auth.json", "auth.lock", "credentials", "config.yaml", ".anthropic_oauth.json",
    "google_token.json", "google_oauth_pending.json", "google_oauth.json",
    "webhook_subscriptions.json", "bws_cache.json", "bws_cache.enc.json", ".git-credentials",
    "node_token.json",
})
_SECRET_FOLDER_NAMES = frozenset({"mcp-tokens", "pairing", "vault", "browser-profile"})


@dataclass(frozen=True)
class Workspace:
    root: Path
    # "config" (terminal.cwd), "default" (Hermes' own choice), "docker-volume" or
    # "hermes-workspace" (the workspace folder in Hermes' home, where it runs the agent).
    origin: str
    # Inside one of Hermes' folders, so secret-bearing names are hidden.
    in_hermes_folder: bool = False


def is_secret_name(name: str) -> bool:
    """Whether a file or folder named *name* holds Hermes credentials."""
    lowered = name.lower()
    return (lowered == ".env" or lowered.startswith(".env.") or lowered == ".envrc"
            or lowered in _SECRET_FILE_NAMES or lowered in _SECRET_FOLDER_NAMES)


def available() -> bool:
    try:
        from hermes_cli.config import read_user_config_raw
        from hermes_cli.profiles import get_profile_dir, profile_exists
    except ImportError:
        return False
    return all(callable(value) for value in (read_user_config_raw, get_profile_dir, profile_exists))


def resolve(profile_id: str | None) -> Workspace:
    """The serving profile's workspace, or a NativeAPIError that says why not."""
    if profile_id is None or PROFILE_ID.fullmatch(profile_id) is None:
        raise NativeAPIError(501, "workspace_identity_unavailable", "This host cannot identify its serving profile workspace.")
    try:
        from hermes_cli.config import read_user_config_raw
        from hermes_cli.profiles import get_profile_dir, profile_exists
    except ImportError:
        raise NativeAPIError(501, "workspace_files_unavailable", "Configured workspace files are unavailable on this host.") from None
    if not profile_exists(profile_id):
        raise NativeAPIError(409, "workspace_identity_changed", "The serving profile workspace changed; reconnect before retrying.")
    try:
        raw = read_user_config_raw(get_profile_dir(profile_id) / "config.yaml")
    except (OSError, UnicodeError, ValueError, TypeError):
        raise NativeAPIError(409, "workspace_config_invalid", "The serving profile configuration must be repaired locally.") from None
    return resolve_config(profile_id, raw)


def resolve_config(profile_id: str, raw: Any, *, agent_files: bool = True) -> Workspace:
    """Resolve from the profile's raw config.yaml mapping.

    ``agent_files=False`` asks only for a host folder that belongs to the agent
    (for plugin storage), not for the folder its tools work in, so the
    terminal backend doesn't matter.
    """
    if _IS_WINDOWS:
        raise NativeAPIError(501, "workspace_windows_unsupported", "Workspace files aren't supported on Windows hosts yet.")
    terminal = raw.get("terminal") if isinstance(raw, dict) else None
    terminal = terminal if isinstance(terminal, dict) else {}
    launch = _is_launch_profile(profile_id)
    configured = _configured(terminal.get("cwd"))
    backend = _backend(terminal, launch) if agent_files else "local"
    if backend == "local":
        if configured is None:
            return _default(profile_id, launch, own_workspace=True)
        root = _existing_folder(configured)
        if root is not None and root in _hermes_folder_paths(profile_id):
            return _own_workspace(profile_id) or _configured_workspace(root, profile_id, "config")
        return _configured_workspace(root, profile_id, "config")
    if backend == "ssh":
        raise NativeAPIError(409, "workspace_on_remote",
                             "This agent works on another computer over SSH, so its files aren't on this one.")
    if backend == "docker":
        mounted = _docker_folder(terminal, configured, profile_id, launch)
        if mounted is not None:
            return mounted
    raise NativeAPIError(409, "workspace_in_container",
                         "This agent works inside a container, so its files aren't on this computer.")


def hermes_folders(profile_id: str | None) -> frozenset[tuple[int, int]]:
    """Identities of Hermes' own folders, which listings and reads never enter."""
    identities = set()
    for folder in _hermes_folder_paths(profile_id):
        try:
            info = folder.stat()
        except OSError:
            continue
        identities.add((info.st_dev, info.st_ino))
    return frozenset(identities)


def _configured(value: Any) -> str | None:
    if not isinstance(value, str) or value.strip() in CWD_PLACEHOLDERS:
        return None
    return value.strip()


def _is_launch_profile(profile_id: str) -> bool:
    """Whether *profile_id* is the profile this Hermes process was started for.

    Only its terminal settings may come from this process's environment.
    """
    try:
        from hermes_constants import get_process_hermes_home, profile_name_for_home
        return profile_name_for_home(get_process_hermes_home()) == profile_id
    except Exception:
        return False


def _backend(terminal: dict, launch: bool) -> str:
    # config.yaml's terminal section wins over the environment, as in Hermes'
    # apply_terminal_config_to_env; "backend" is current, "env_type" legacy.
    value = terminal.get("backend") or terminal.get("env_type")
    if not isinstance(value, str) or not value.strip():
        value = os.environ.get("TERMINAL_ENV", "") if launch else ""
    return value.strip().lower() or "local"


def _existing_folder(raw: str | None) -> Path | None:
    """*raw* as Hermes resolves a host folder, if it exists."""
    if not raw or len(raw.encode("utf-8", "surrogatepass")) > _MAX_PATH_BYTES:
        return None
    try:
        # abspath anchors a relative path on this process's folder: the one
        # Hermes was started in, which is what Hermes anchors it on too.
        root = Path(os.path.abspath(os.path.expanduser(raw))).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    return root if root.is_dir() else None


def _configured_workspace(root: Path | None, profile_id: str, origin: str) -> Workspace:
    if root is None:
        raise NativeAPIError(409, "workspace_unavailable", "The configured workspace is unavailable.")
    hermes = _hermes_folder_paths(profile_id)
    if root in hermes:
        raise _hermes_folder_refusal()
    return Workspace(root, origin, any(folder in root.parents for folder in hermes))


def _default(profile_id: str, launch: bool, *, own_workspace: bool = False) -> Workspace:
    root = _existing_folder(_hermes_new_chat_folder(profile_id, launch))
    hermes = _hermes_folder_paths(profile_id)
    protected = [*hermes, *_install_folders()]
    if root is not None and root.parent != root \
            and not any(root == folder or folder in root.parents for folder in protected):
        return Workspace(root, "default")
    # Hermes' chats start somewhere that isn't the agent's own (hosted Hermes
    # runs in its home): its workspace folder is where the agent's files belong.
    if own_workspace and (workspace := _own_workspace(profile_id)) is not None:
        return workspace
    if root in hermes:
        raise _hermes_folder_refusal()
    raise NativeAPIError(409, "workspace_not_configured",
                         "Hermes runs this agent without a working folder of its own; set terminal.cwd for it.")


def _hermes_folder_refusal() -> NativeAPIError:
    return NativeAPIError(409, "workspace_hermes_folder",
                          "This agent works in Hermes' own folder, which holds its settings and keys, and "
                          "there's no workspace folder in it for the agent's files.")


def _own_workspace(profile_id: str) -> Workspace | None:
    """The workspace folder Hermes makes in the agent's profile home, if it has one."""
    try:
        from hermes_cli.profiles import get_profile_dir
        home = Path(get_profile_dir(profile_id)).resolve(strict=True)
    except Exception:
        return None
    root = _existing_folder(str(home / "workspace"))
    # A link that leads out of the home (or back to a Hermes folder) isn't it.
    if root is None or home not in root.parents or root in _hermes_folder_paths(profile_id):
        return None
    return Workspace(root, HERMES_WORKSPACE, True)


def _hermes_new_chat_folder(profile_id: str, launch: bool) -> str | None:
    """The folder Hermes starts a new chat in when none is chosen."""
    # Ask Hermes itself when its chat gateway runs in this process. Importing
    # it here would redirect stdout, so only use it when it's already loaded.
    server = sys.modules.get("tui_gateway.server")
    handle = getattr(server, "handle_request", None)
    if callable(handle):
        request_id = "loopdy-workspace-" + uuid.uuid4().hex
        params = {"key": "project"} if launch else {"key": "project", "profile": profile_id}
        try:
            response = handle({"jsonrpc": "2.0", "id": request_id, "method": "config.get", "params": params})
        except Exception:
            response = None
        result = response.get("result") if isinstance(response, dict) and response.get("id") == request_id else None
        cwd = result.get("cwd") if isinstance(result, dict) else None
        if isinstance(cwd, str) and cwd.strip():
            return cwd.strip()
    # The same rule on hosts without it (tui_gateway _completion_cwd).
    environment = _configured(os.environ.get("TERMINAL_CWD"))
    if environment is not None and (found := _existing_folder(environment)) is not None:
        return str(found)
    try:
        return os.getcwd()
    except OSError:
        return None


def _docker_folder(terminal: dict, configured: str | None, profile_id: str, launch: bool) -> Workspace | None:
    """The host folder Hermes bind-mounts as the agent's working folder, if any."""
    mount = terminal.get("docker_mount_cwd_to_workspace")
    if mount is None and launch:
        mount = os.environ.get("TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE")
    on_host = _existing_folder(configured) if configured is not None else None
    if _flag(mount):
        if configured is None:
            # Hermes mounts the folder a new chat starts in at /workspace.
            return _default(profile_id, launch)
        if on_host is not None:
            return _configured_workspace(on_host, profile_id, "config")
    volumes = _volumes(terminal, launch)
    # A configured host folder Hermes also mounts into the container.
    if on_host is not None and any(on_host == host or host in on_host.parents for host, _ in volumes):
        return _configured_workspace(on_host, profile_id, "config")
    # Otherwise the agent works at its in-container folder (Hermes drops host
    # paths for containers and uses /root); a volume may hold that folder.
    inside = configured if configured is not None and configured.startswith("/") \
        and not _HOST_SHAPED.match(configured) else _CONTAINER_HOME
    inside = "/" + inside.strip("/")
    for host, target in volumes:
        if inside == target or inside.startswith(target + "/"):
            relative = inside[len(target):].lstrip("/")
            found = _existing_folder(str(host / relative) if relative else str(host))
            if found is not None:
                return _configured_workspace(found, profile_id, "docker-volume")
    return None


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return isinstance(value, str) and value.strip().lower() in _TRUE


def _volumes(terminal: dict, launch: bool) -> list[tuple[Path, str]]:
    """Bind mounts (host folder, container folder) from docker_volumes."""
    values = terminal.get("docker_volumes")
    if values is None and launch:
        try:
            values = json.loads(os.environ.get("TERMINAL_DOCKER_VOLUMES", "null"))
        except ValueError:
            values = None
    if not isinstance(values, list):
        return []
    mounts = []
    for value in values[:_MAX_VOLUMES]:
        match = _VOLUME_SPEC.match(value.strip()) if isinstance(value, str) else None
        if match is None:
            continue
        host = os.path.expanduser(match.group("host"))
        # A named volume ("data:/data") isn't a host folder.
        if not os.path.isabs(host):
            continue
        found = _existing_folder(host)
        if found is not None:
            mounts.append((found, "/" + match.group("container").strip("/")))
    return mounts


def _hermes_folder_paths(profile_id: str | None) -> list[Path]:
    """Hermes' own folders: its homes and every profile's, resolved."""
    candidates: list[Any] = [os.environ.get("HERMES_HOME"), Path.home() / ".hermes"]
    try:
        from hermes_constants import get_default_hermes_root, get_process_hermes_home
        candidates += [get_process_hermes_home(), get_default_hermes_root()]
    except Exception:
        pass
    if profile_id is not None and PROFILE_ID.fullmatch(profile_id) is not None:
        try:
            from hermes_cli.profiles import get_profile_dir
            candidates.append(get_profile_dir(profile_id))
        except Exception:
            pass
    folders: list[Path] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            folder = Path(candidate).expanduser().resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if folder.is_dir() and folder not in folders:
            folders.append(folder)
    for home in list(folders):
        try:
            with os.scandir(home / "profiles") as entries:
                for count, entry in enumerate(entries):
                    if count >= _MAX_PROFILES:
                        break
                    if entry.is_dir(follow_symlinks=False):
                        folder = Path(entry.path).resolve()
                        if folder not in folders:
                            folders.append(folder)
        except OSError:
            continue
    return folders


def _install_folders() -> list[Path]:
    """Hermes' own code, never the agent's working folder by default."""
    try:
        import hermes_constants
        return [Path(hermes_constants.__file__).resolve().parent]
    except Exception:
        return []
