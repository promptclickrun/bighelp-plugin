"""Hermes registration and CLI surfaces for bighelp."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from .adapter import (
    BighelpAdapter,
    check_requirements,
    env_enablement,
    get_service,
    is_connected,
    profile_display_name,
    release_service,
    standalone_send,
    validate_config,
)
from .device_tools import register as register_device_tools
from .approval import BighelpApprovalTransport
from .events import BighelpEvent
from .hooks import normalize_hook
from .legacy_cleanup import remove_retired_link_keys
from .workflow_runner import in_workflow_stage
from .naming import CLI_COMMAND, LEGACY_CLI_COMMAND, register_legacy_toolset_alias
from .plugin_update import (
    PluginUpdateManager,
    local_cli_device_id,
    new_operation_id,
    production_manager,
)
from .providers.apns import load_apns_config
from .targets import parse_target, validate_target
from .tools import register as register_tools


NOTIFICATION_HOOKS = (
    "pre_tool_call",
    "on_session_end",
    "subagent_start",
    "subagent_stop",
    "kanban_task_claimed",
    "kanban_task_completed",
    "kanban_task_blocked",
)
HOOKS = (
    "pre_llm_call",
    "post_llm_call",
    "post_tool_call",
    "pre_approval_request",
    "post_approval_response",
    *NOTIFICATION_HOOKS,
)
logger = logging.getLogger("hermes.plugins.bighelp")


def register(ctx: Any, *, service: Any | None = None) -> None:
    active_service = service or get_service()
    profile = str(getattr(ctx, "profile_name", "default") or "default")
    update_manager = production_manager(profile)
    # Hosts paired with the retired Link still carry its keys in .env. Nothing reads them now.
    remove_retired_link_keys()

    def adapter_factory(config):
        return BighelpAdapter(config, service=active_service, plugin_update_manager=update_manager)

    register_tools(ctx, store=active_service.store, profile=profile)
    # The phone runs iPhone tools over the native channel, through Hermes' public
    # execution middleware. Hosts without that middleware have no way to reach a phone.
    from .native_device_tools import register_middleware as register_native_device_tools
    if register_native_device_tools(ctx):
        register_device_tools(ctx)
    from .agent_guide import register as register_agent_guide
    register_agent_guide(ctx)
    # Feed, Ideas, Goals, Activity and Approvals history for the bighelp app.
    # The tool writes only when an agent is asked to; the observers only record.
    try:
        from .agent_board import register as register_agent_board
        register_agent_board(ctx)
    except (OSError, ImportError, sqlite3.Error):
        logger.warning("bighelp agent board unavailable")
    # Who is writing in each bighelp chat, as the app reports it.
    from .people import register as register_people
    register_people(ctx)
    # Teamwork (opt-in from the app): keeps every agent's team brief current as agents come and go.
    from .team import register as register_team
    register_team(ctx)
    # bighelp's Template Catalog for agents. Making an agent needs a plugin setting and the person's approval.
    from .template_tools import register as register_template_tools
    register_template_tools(ctx)
    # Workflows for agents: read, draft and run; publishing and schedules ask the person.
    from .workflow_tools import register as register_workflow_tools
    register_workflow_tools(ctx)
    # Template Catalog submissions, after a one-time GitHub sign-in on this host.
    from .catalog_submissions import register as register_catalog_submissions
    register_catalog_submissions(ctx)
    register_legacy_toolset_alias()
    # Workflow runs live in their own coordinator; after a reboot it starts again here when runs wait for it.
    if os.environ.get("HERMES_SESSION_SOURCE") != "workflow":
        try:
            from .workflow_api import ensure_on_start
            ensure_on_start()
        except ImportError:
            logger.debug("bighelp workflows unavailable")

    ctx.register_platform(
        name="loopdy",
        label="bighelp",
        adapter_factory=adapter_factory,
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[],
        install_hint=(
            "Only for the legacy push setup: run `hermes bighelp configure-apns`. "
            "The bighelp app does not need this platform."
        ),
        env_enablement_fn=env_enablement,
        cron_deliver_env_var="LOOPDY_HOME_TARGET",
        standalone_sender_fn=partial(standalone_send, service=active_service),
        parse_target_ref_fn=parse_target,
        validate_target_ref_fn=validate_target,
        # Leave complete 64 KiB card envelopes intact. The adapter splits
        # ordinary text more tightly.
        max_message_length=100_000,
        emoji="L",
        pii_safe=True,
        allow_update_command=False,
    )

    approval = BighelpApprovalTransport(
        active_service.store,
        active_service,
        target=_home_target(),
        profile=profile,
        agent_name=profile_display_name(profile),
    )
    ctx.register_approval_transport("loopdy", approval.present)

    ctx.register_hook(
        "pre_llm_call",
        partial(_pre_llm_call, service=active_service, profile=profile),
    )
    ctx.register_hook(
        "post_llm_call",
        partial(_post_llm_call, service=active_service, profile=profile),
    )
    ctx.register_hook(
        "post_tool_call",
        partial(_post_tool_call, service=active_service),
    )
    for hook_name in NOTIFICATION_HOOKS:
        _register_notification_observer(
            ctx, hook_name,
            partial(
                _deliver_hook,
                hook_name,
                service=active_service,
                profile=profile,
                profile_getter=lambda: ctx.profile_name,
            ),
        )

    cli_handler = partial(
        handle_cli,
        service=active_service,
        profile=profile,
        plugin_update_manager=update_manager,
    )
    ctx.register_cli_command(
        name=CLI_COMMAND,
        help="Update and inspect the bighelp plugin",
        description="Manage the bighelp plugin on this Hermes host.",
        setup_fn=setup_cli,
        handler_fn=cli_handler,
    )
    # Scripts written before 3.0.0 run `hermes loopdy …`; keep them working, unlisted.
    ctx.register_cli_command(
        name=LEGACY_CLI_COMMAND,
        help=argparse.SUPPRESS,
        description="Old name for `hermes bighelp`.",
        setup_fn=setup_cli,
        handler_fn=cli_handler,
    )
    # Independent, public native observers. The platform adapter is not
    # constructed to enable these notifications, and no core hook is patched.
    try:
        from .managed_notifications import get_managed_notifications
        managed = get_managed_notifications()
        policy = getattr(active_service, "managed_notification_policy", None)
        if callable(policy): managed.preference_policy = policy
        active_service.managed_alert_owner = managed.owns_alert
        def observe_managed(hook, **payload):
            managed.observe(hook, profile=str(ctx.profile_name or profile), **payload)
        for hook in ("pre_llm_call", "post_llm_call", "pre_tool_call", "post_tool_call",
                     "on_session_end", "subagent_start", "subagent_stop"):
            _register_notification_observer(ctx, hook, partial(observe_managed, hook))
        # Registration is additive: retain the explicitly selected legacy
        # transport. Older SDKs can warn-and-register unknown names, so
        # registration alone is not proof of an emitter.
        try:
            from hermes_cli.plugins import VALID_HOOKS
            approval_hooks_supported = {"pre_approval_request", "post_approval_response"} <= VALID_HOOKS
        except ImportError:
            approval_hooks_supported = False
        if approval_hooks_supported:
            for hook in ("pre_approval_request", "post_approval_response"):
                ctx.register_hook(hook, partial(observe_managed, hook))
        managed.producer_loaded(
            profile,
            approval_hooks_loaded=approval_hooks_supported,
            clarification_producer_loaded=True,
        )
        ctx.on_unload(managed.close)
    except (OSError, ValueError, sqlite3.Error):
        # Notification storage/identity failure must never break foreground chat.
        logger.warning("Managed notification producer unavailable")
    from .room_activity import register_room_activity
    stop_room_activity = register_room_activity(ctx)

    def unload():
        stop_room_activity()
        release_service(active_service)

    ctx.on_unload(unload)

def _register_notification_observer(ctx: Any, hook: str, callback: Any) -> None:
    """Keep tool-start telemetry out of Hermes's fail-closed policy hooks.

    Hermes suppresses concurrent invocations of a bounded hook callback. For
    pre_tool_call that suppression vetoes the tool, even when the callback only
    updates notifications. Request middleware retains the authentic coordinates
    and normal policy/approval path without making telemetry an authorization gate.
    Older hosts without request middleware retain their legacy hook integration.
    """
    register_middleware = getattr(ctx, "register_middleware", None)
    if hook != "pre_tool_call" or not callable(register_middleware):
        ctx.register_hook(hook, callback)
        return

    def observe_tool_request(**payload: Any) -> None:
        try:
            callback(**payload)
        except Exception:
            # Never reflect notification state, payloads, or exception bodies.
            logger.warning("bighelp tool-start notification observation unavailable")
        # None preserves the tool name, arguments, result, and policy decisions.
        return None

    register_middleware("tool_request", observe_tool_request)


def _pre_llm_call(*, service: Any, profile: str, **payload: Any) -> None:
    _queue_live_activity_update(
        service,
        session_id=str(payload.get("session_id") or ""),
        profile=str(payload.get("profile_name") or profile),
        phase="thinking",
    )


def _post_llm_call(*, service: Any, profile: str, **payload: Any) -> None:
    if in_workflow_stage():
        return
    # A finished turn cannot clear session-wide attention: a newer turn may
    # already be waiting. Explicit clarify/approval resolution owns cleanup.
    _queue_live_activity_update(
        service,
        session_id=str(payload.get("session_id") or payload.get("task_id") or ""),
        profile=str(payload.get("profile_name") or profile),
        phase="completed",
        active_session_count=0,
    )


def _post_tool_call(*, service: Any, **payload: Any) -> None:
    if str(payload.get("tool_name") or "").strip() != "clarify":
        return
    service.store.dismiss_attention_for_session(
        str(payload.get("session_id") or payload.get("task_id") or "")
    )


def _deliver_hook(
    hook_name: str,
    *,
    service: Any,
    profile: str,
    profile_getter: Any | None = None,
    **payload: Any,
) -> None:
    # A workflow stage's turns are machinery: the run alerts when it needs the person or ends.
    if in_workflow_stage():
        return
    if hook_name in {"subagent_start", "subagent_stop"} and callable(profile_getter):
        # PluginContext resolves the current profile scope, not load-time defaults.
        profile = str(profile_getter() or profile)
    event = normalize_hook(hook_name, profile=profile, **payload)
    if event is not None:
        if hook_name == "subagent_stop":
            event = _with_subagent_start_metadata(event, service=service, payload=payload)
        if not event.detail.get("agent_name"):
            event = replace(
                event,
                detail={
                    **event.detail,
                    "agent_name": profile_display_name(event.profile),
                },
            )
        if event.type == "session.completed":
            # Home history only: there is no push delivery and no sessionId, so
            # finishing one turn never ends a newer turn's Live Activity.
            service.store.record_event(event, target=_home_target())
        else:
            service.enqueue(event, target=_home_target())
    _live_activity_from_hook(hook_name, service=service, profile=profile, payload=payload)


def _with_subagent_start_metadata(
    event: BighelpEvent, *, service: Any, payload: dict[str, Any]
) -> BighelpEvent:
    # Hermes stop observers need not repeat child_goal/child_subagent_id.
    # Recover only bounded metadata from the exact plugin-owned start record,
    # not runtime internals or the child's raw final summary.
    start = normalize_hook("subagent_start", profile=event.profile, **payload)
    if start is None or not event.detail.get("child_session_id"):
        return event
    stored = service.store.get_event(start.event_id)
    if not isinstance(stored, dict):
        return event
    detail = stored.get("detail")
    if (
        stored.get("type") != "delegation.started"
        or stored.get("profile") != event.profile
        or stored.get("session_id") != event.session_id
        or not isinstance(detail, dict)
        or detail.get("child_session_id") != event.detail["child_session_id"]
    ):
        return event
    restored = dict(event.detail)
    for key in ("title", "delegation_id", "turn_id"):
        value = detail.get(key)
        if isinstance(value, str) and value:
            restored[key] = value
    return replace(
        event, detail=restored,
        delegation_id=restored.get("delegation_id", event.delegation_id),
    )


def _live_activity_from_hook(
    hook_name: str, *, service: Any, profile: str, payload: dict[str, Any]
) -> None:
    session_id = str(payload.get("session_id") or payload.get("task_id") or "").strip()
    if not session_id:
        return
    selected_profile = str(payload.get("profile_name") or profile or "default")
    phase = ""
    if hook_name == "pre_tool_call":
        tool_name = str(payload.get("tool_name") or "tool")[:80]
        if tool_name == "clarify":
            phase = "waiting"
        else:
            phase = "running"
    elif hook_name == "on_session_end":
        failed = bool(payload.get("failed") or payload.get("interrupted"))
        if not failed:
            return
        phase = "failed"
    if phase:
        _queue_live_activity_update(
            service,
            session_id=session_id,
            profile=selected_profile,
            phase=phase,
            active_session_count=0 if phase in {"completed", "failed"} else 1,
        )


def _queue_live_activity_update(service: Any, **update: Any) -> None:
    if not str(update.get("session_id") or "").strip():
        return
    if not service.enqueue_live_activity_update(**update):
        logger.warning("bighelp Live Activity update was not queued")


def setup_cli(parser: Any) -> None:
    actions = parser.add_subparsers(dest="bighelp_action", required=True)
    from .catalog_submissions import setup_cli as setup_catalog_cli
    setup_catalog_cli(actions)

    actions.add_parser("status", help="Show provider health and registered devices")

    files = actions.add_parser(
        "files",
        help="Manage explicit read-only workspace file grants",
    )
    file_actions = files.add_subparsers(dest="bighelp_files_action", required=True)
    grant = file_actions.add_parser("grant", help="Grant one host-local workspace root")
    grant.add_argument("workspace_id")
    grant.add_argument("--root", required=True)
    grant.add_argument("--label", required=True)
    revoke = file_actions.add_parser("revoke", help="Revoke one workspace root")
    revoke.add_argument("workspace_id")
    revoke.add_argument("--yes", action="store_true", help="Confirm revocation")
    file_actions.add_parser("roots", help="List granted workspace labels")
    list_files = file_actions.add_parser("list", help="List a granted directory")
    list_files.add_argument("workspace_id")
    list_files.add_argument("--path", default="")
    list_files.add_argument("--offset", type=int, default=0)
    list_files.add_argument("--limit", type=int, default=100)
    list_files.add_argument("--query", default="")
    list_files.add_argument("--revision")
    read = file_actions.add_parser("read", help="Read one bounded file chunk")
    read.add_argument("workspace_id")
    read.add_argument("path")
    read.add_argument("--offset", type=int, default=0)
    read.add_argument("--limit", type=int, default=65_536)
    read.add_argument("--revision")
    file_status = file_actions.add_parser("status", help="Show read-only Git status")
    file_status.add_argument("workspace_id")
    diff = file_actions.add_parser("diff", help="Show a bounded read-only Git diff")
    diff.add_argument("workspace_id")
    diff.add_argument("path")
    diff.add_argument("--side", choices=("staged", "worktree"), required=True)
    diff.add_argument("--expected-status-token", required=True)
    diff.add_argument("--offset", type=int, default=0)
    diff.add_argument("--limit", type=int, default=300)

    update = actions.add_parser(
        "update",
        help="Install the latest bighelp plugin release",
    )
    update.add_argument(
        "--restart",
        action="store_true",
        help="Explicitly authorize one gateway restart after installation",
    )
    actions.add_parser(
        "update-status",
        help="Read the latest durable plugin update status",
    )

    provider = actions.add_parser("provider", help="Show or select the push provider")
    provider.add_argument("mode", nargs="?", choices=("managed", "direct"))

    configure_apns = actions.add_parser(
        "configure-apns",
        help="Configure host-only credentials for direct APNs delivery",
    )
    configure_apns.add_argument("--team-id", required=True)
    configure_apns.add_argument("--key-id", required=True)
    configure_apns.add_argument("--topic", required=True)
    configure_apns.add_argument(
        "--environment",
        choices=("production", "sandbox"),
        default="production",
    )
    configure_apns.add_argument("--key-path", required=True)

    remove_apns = actions.add_parser(
        "remove-apns",
        help="Remove direct APNs configuration from this Hermes host",
    )
    remove_apns.add_argument("--yes", action="store_true", help="Confirm removal")

    test = actions.add_parser("test", help="Send an opaque test wakeup")
    test.add_argument("--target", default=_home_target())


def handle_cli(
    args: Any,
    *,
    service: Any,
    profile: str,
    plugin_update_manager: PluginUpdateManager | None = None,
) -> None:
    action = str(getattr(args, "bighelp_action", "") or "")
    if action == "files":
        _handle_files_cli(args)
        return
    if action == "catalog":
        from .catalog_submissions import handle_cli as handle_catalog_cli
        handle_catalog_cli(args)
        return
    if action in {"update", "update-status"}:
        manager = plugin_update_manager or production_manager(profile)
        if action == "update-status":
            _print_json(manager.status())
            return
        current = manager.status()
        if current["phase"] not in {
            "idle",
            "complete",
            "up_to_date",
            "installed_restart_required",
            "blocked",
            "failed",
        }:
            _print_json(current)
            return
        _print_json(
            manager.start(
                operation_id=new_operation_id(),
                device_id=local_cli_device_id(profile),
                restart=bool(getattr(args, "restart", False)),
            )
        )
        return
    if action == "status":
        apns = service.store.load_apns_config()
        health = service.health()
        if health.get("mode") == "managed" and not health.get("configured"):
            # Nothing builds this legacy "managed" provider any more: the app's notifications are
            # managed_notifications.py, counted under "notifications".
            health = {**health, "detail": "Not used: the bighelp app sets up notifications (see notifications)"}
        _print_json(
            {
                "devices": [
                    _public_device(value) for value in service.store.list_devices()
                ],
                "home_target": _home_target(),
                "provider": service.store.provider_mode(),
                "provider_health": health,
                "apns": _redacted_apns_config(apns),
                "notifications": _app_notifications_status(),
            }
        )
        return
    if action == "provider":
        requested_mode = getattr(args, "mode", None)
        if requested_mode:
            service.set_provider_mode(requested_mode)
        _print_json(
            {
                "provider": service.store.provider_mode(),
                "provider_health": service.health(),
            }
        )
        return
    if action == "configure-apns":
        config = load_apns_config(
            {
                "team_id": args.team_id,
                "key_id": args.key_id,
                "topic": args.topic,
                "environment": args.environment,
                "key_path": args.key_path,
            },
            os.environ,
        )
        service.store.save_apns_config(config.stored_values())
        service.set_provider_mode("direct")
        _print_json(
            {
                "provider": "direct",
                "apns": _redacted_apns_config(config.stored_values()),
            }
        )
        return
    if action == "remove-apns":
        if not bool(getattr(args, "yes", False)):
            raise ValueError("Pass --yes to confirm APNs configuration removal")
        service.store.clear_apns_config()
        if service.store.provider_mode() == "direct":
            service.set_provider_mode("managed")
        _print_json({"provider": service.store.provider_mode(), "apns": None})
        return

    if action == "test":
        target = str(args.target or "all").strip()
        verdict = validate_target(target)
        if verdict is not True:
            raise ValueError(str(verdict))
        _print_json(service.test_notification(target=target, profile=profile))
        return
    raise ValueError("Unknown bighelp command")


def _app_notifications_status() -> dict[str, Any]:
    """The app's notifications: a count only, never grant IDs, keys or phones."""
    try:
        from .managed_notifications import get_managed_notifications
        return {"available": True, "active_grants": get_managed_notifications().active_grant_count()}
    except Exception:
        return {"available": False}


def _handle_files_cli(args: Any) -> None:
    from hermes_constants import get_hermes_home

    from .workspace_files import WorkspaceFilesError, WorkspaceFilesService

    action = str(getattr(args, "bighelp_files_action", "") or "")
    try:
        service = WorkspaceFilesService(
            get_hermes_home() / "plugin-data" / "loopdy" / "workspace-files"
        )
        if action == "grant":
            result = service.grant(
                str(args.workspace_id),
                root=Path(str(args.root)),
                label=str(args.label),
            )
        elif action == "revoke":
            if not bool(getattr(args, "yes", False)):
                raise WorkspaceFilesError(
                    "INVALID_REQUEST", "Pass --yes to confirm workspace revocation"
                )
            result = service.revoke(str(args.workspace_id))
        elif action == "roots":
            result = service.roots()
        elif action == "list":
            result = service.list_directory(
                str(args.workspace_id),
                path=str(args.path),
                offset=int(args.offset),
                limit=int(args.limit),
                query=str(args.query),
                revision=getattr(args, "revision", None),
            )
        elif action == "read":
            result = service.read_file(
                str(args.workspace_id),
                path=str(args.path),
                offset=int(args.offset),
                limit=int(args.limit),
                revision=getattr(args, "revision", None),
            )
        elif action == "status":
            result = service.git_status(str(args.workspace_id))
        elif action == "diff":
            result = service.git_diff(
                str(args.workspace_id),
                path=str(args.path),
                side=str(args.side),
                expected_status_token=str(args.expected_status_token),
                offset=int(args.offset),
                limit=int(args.limit),
            )
        else:
            raise WorkspaceFilesError(
                "INVALID_REQUEST", "Unknown bighelp Files command"
            )
    except WorkspaceFilesError as error:
        _print_json(error.envelope())
        raise SystemExit(1) from None
    except Exception:
        _print_json(
            WorkspaceFilesError(
                "FILES_UNAVAILABLE", "Workspace Files request failed"
            ).envelope()
        )
        raise SystemExit(1) from None
    _print_json(result)


def _home_target() -> str:
    target = os.getenv("LOOPDY_HOME_TARGET", "all").strip() or "all"
    return target if validate_target(target) is True else "all"


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _redacted_apns_config(config: Any) -> dict[str, Any] | None:
    if not isinstance(config, dict):
        return None
    return {
        "team_id": str(config.get("team_id") or ""),
        "key_id": str(config.get("key_id") or ""),
        "topic": str(config.get("topic") or ""),
        "environment": str(config.get("environment") or ""),
        "key_file": os.path.basename(str(config.get("key_path") or "")),
    }


def _public_device(device: Any) -> dict[str, Any]:
    value = dict(device) if isinstance(device, dict) else {}
    endpoint = str(value.pop("endpoint_id", "") or "")
    value.pop("recipient_public_key", None)
    value["token_fingerprint"] = (
        hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:12] if endpoint else ""
    )
    return value
