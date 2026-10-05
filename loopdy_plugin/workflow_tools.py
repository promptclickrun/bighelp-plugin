"""The `bighelp_workflows` agent tool: agents see, build, run and manage the same workflows as the bighelp app.

Reading, drafting and running are open to any agent that has the tool. Publishing (which also chooses the agent for
each role) and putting a workflow on a schedule decide what agents will do without anyone watching, so each asks
the person in Hermes' own approval prompt and refuses when no one can answer. Signing off stays with the person,
in the app. Inside a workflow stage the tool only reads, so a run can't start or change runs.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from . import workflow_api as api
from .workflow_runner import in_workflow_stage
from .workflow_store import Mutation, WorkflowError, WorkflowStore

logger = logging.getLogger("hermes.plugins.bighelp")

TOOL = "bighelp_workflows"
TOOLSET = "bighelp_workflows"
READ_ACTIONS = ("list", "get", "templates", "runs", "run")
WRITE_ACTIONS = ("create", "save_draft", "publish", "start", "control", "set_trigger", "archive")
APPROVAL_PREVIEW = 1_500

SCHEMA = {
    "name": TOOL,
    "description": (
        "The person's bighelp workflows on this computer: multi-step flows where agents hand work from stage to "
        "stage (agent, check, decision and sign-off stages, delivery stages that send earlier outputs to a place "
        "`hermes send --to` takes, and parallel blocks that run 2 to 5 agent stages at once; a decision can read "
        "all their verdicts). Outputs can be Markdown files, text, numbers, decisions, notes, files and pictures. "
        "Actions: list (workflows, runs waiting for the person, "
        "active runs), get (one workflow's definition, roles and problems), templates, create (from a template "
        "id, or from a definition), save_draft, publish (with role -> agent choices; the person approves), start "
        "(a run of the newest published version, with inputs), runs, run (one run in full: stages, outputs, "
        "sign-offs), control (pause, resume, cancel or retry a run), set_trigger (manual, or a cron schedule like "
        "'0 9 * * 1-5' with saved inputs; the person approves a schedule) and archive. The person signs off "
        "in the bighelp app, never through this tool. Call get before save_draft and send its draftVersion. "
        "Call it directly when it is in your tool list; when Hermes has hidden it, use tool_search, "
        "tool_describe and tool_call to run this exact tool."),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": list(READ_ACTIONS + WRITE_ACTIONS)},
            "workflowId": {"type": "string", "maxLength": 80},
            "runId": {"type": "string", "maxLength": 80},
            "templateId": {"type": "string", "maxLength": 64},
            "name": {"type": "string", "maxLength": 80},
            "definition": {"type": "object", "description": "A workflow definition, as get returns it."},
            "baseDraftVersion": {"type": "integer", "minimum": 0},
            "bindings": {"type": "object", "description": "Role key -> agent (profile) id, for publish.",
                         "additionalProperties": {"type": "string", "maxLength": 64}},
            "inputs": {"type": "object", "description": "The workflow's inputs by key, for start or a schedule."},
            "runAction": {"type": "string", "enum": ["pause", "resume", "cancel", "retry"]},
            "trigger": {"type": "string", "enum": ["manual", "schedule"]},
            "schedule": {"type": "string", "maxLength": 200,
                         "description": "A cron expression for set_trigger, in this computer's time zone."},
            "filter": {"type": "string", "enum": ["all", "active", "for_you", "attention"]},
            "includeArchived": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["action"],
    },
}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": code, "message": message}


def _ask(description: str, rule_key: str) -> tuple[bool, str]:
    """Hermes' approval prompt for one change. Fails closed when no person can answer."""
    from .template_tools import unattended_reason
    if unattended_reason() is not None:
        return False, "No one can approve this right now. Ask the person to do it in the bighelp app."
    try:
        from tools.approval import request_tool_approval
    except ImportError:
        return False, "This Hermes can't ask for approval for plugin tools."
    try:
        answer = request_tool_approval(TOOL, description[:APPROVAL_PREVIEW], rule_key=rule_key)
    except Exception:
        return False, "The approval prompt failed, so nothing changed."
    if isinstance(answer, dict) and answer.get("approved") is True:
        return True, ""
    message = answer.get("message") if isinstance(answer, dict) else None
    return False, message or "The person said no, so nothing changed."


class WorkflowTools:
    def __init__(self, *, store: Callable[[], WorkflowStore] | None = None,
                 approve: Callable[[str, str], tuple[bool, str]] = _ask,
                 facts: Callable[[], Any] | None = None):
        self._store = store or (lambda: WorkflowStore(api.workflows_root()))
        self._approve = approve
        self._facts = facts or (lambda: api._host_facts())

    def call(self, payload: Any) -> dict[str, Any]:
        args = payload if isinstance(payload, dict) else {}
        action = args.get("action")
        if action not in READ_ACTIONS + WRITE_ACTIONS:
            return _error("action_invalid", "Choose one of the listed actions.")
        if action in WRITE_ACTIONS and in_workflow_stage():
            return _error("not_in_a_stage", "A workflow stage can read workflows but not change or start them.")
        reason = api.probe()
        if reason is not None:
            return _error("workflows_unavailable", f"Workflows can't run on this computer ({reason}).")
        try:
            return getattr(self, "_" + action)(args, self._store())
        except WorkflowError as error:
            return _error(error.code, error.message)
        except ValidationError:
            return _error("invalid_request", "Some of the values are missing or not allowed.")

    @staticmethod
    def _body(model: type[BaseModel], **values: Any) -> Any:
        return model.model_validate({key: value for key, value in values.items() if value is not None})

    @staticmethod
    def _mutation() -> Mutation:
        return Mutation(request_id=str(uuid.uuid4()))

    # MARK: Reading

    def _list(self, args, store):
        body = self._body(api._List, includeArchived=args.get("includeArchived"))
        return api._list(store, body, None)

    def _get(self, args, store):
        return api._get(store, self._body(api._Get, workflowId=args.get("workflowId")), None)

    def _templates(self, args, store):
        return store.list_templates()

    def _runs(self, args, store):
        body = self._body(api._Runs, workflowId=args.get("workflowId"), filter=args.get("filter"),
                          limit=args.get("limit"))
        return api._runs(store, body, None)

    def _run(self, args, store):
        return api._run(store, self._body(api._RunBody, runId=args.get("runId")), None)

    # MARK: Changing

    def _create(self, args, store):
        if args.get("templateId"):
            body = self._body(api._Use, templateId=args["templateId"], name=args.get("name"))
            return api._use(store, body, self._mutation())
        body = self._body(api._Save, baseDraftVersion=0, definition=args.get("definition"))
        return api._save(store, body, self._mutation())

    def _save_draft(self, args, store):
        body = self._body(api._Save, workflowId=args.get("workflowId"), baseDraftVersion=args.get("baseDraftVersion"),
                          definition=args.get("definition"))
        return api._save(store, body, self._mutation())

    def _publish(self, args, store):
        workflow_id = args.get("workflowId")
        current = api._get(store, self._body(api._Get, workflowId=workflow_id), None)["workflow"]
        bindings = args.get("bindings") or {}
        if not isinstance(bindings, dict):
            return _error("invalid_request", "bindings maps role keys to agent ids.")
        roles = {binding["role"]: binding["agentId"] for binding in current["bindings"]}
        roles.update(bindings)
        stages = ", ".join(stage["title"] for stage in current["definition"]["stages"][:12])
        who = ", ".join(f"{role}: {agent or 'no agent'}" for role, agent in roles.items())
        approved, message = self._approve(
            f"Publish the workflow \"{current['name']}\" so it can run. Stages: {stages}. Agents: {who}.",
            f"bighelp-workflow-publish:{workflow_id}")
        if not approved:
            return _error("not_approved", message)
        for role, agent in bindings.items():
            api._bind(store, self._body(api._Bind, workflowId=workflow_id, role=role, agentId=agent),
                      self._mutation())
        return api._publish(store, self._body(api._Publish, workflowId=workflow_id,
                                              draftVersion=current["draftVersion"]), self._mutation())

    def _start(self, args, store):
        workflow_id = args.get("workflowId")
        current = api._get(store, self._body(api._Get, workflowId=workflow_id, revision="draft"), None)["workflow"]
        revision = current["latestRevision"]
        if not revision:
            return _error("not_published", "Publish this workflow before you run it.")
        body = self._body(api._Start, workflowId=workflow_id, revision=revision, inputs=args.get("inputs") or {},
                          clientRunToken=str(uuid.uuid4()))
        result = api._start(store, body, self._mutation())
        self._ensure_runner(store)
        return result

    def _control(self, args, store):
        run = api._run(store, self._body(api._RunBody, runId=args.get("runId")), None)["run"]
        body = self._body(api._Control, runId=args.get("runId"), action=args.get("runAction"),
                          expectedVersion=run["version"])
        result = api._control(store, body, self._mutation())
        self._ensure_runner(store)
        return result

    def _set_trigger(self, args, store):
        from .workflow_trigger import check_schedule, set_trigger
        workflow_id = args.get("workflowId")
        kind = args.get("trigger")
        if kind == "schedule":
            schedule = check_schedule(args.get("schedule"))
            name = api._get(store, self._body(api._Get, workflowId=workflow_id), None)["workflow"]["name"]
            approved, message = self._approve(
                f"Run the workflow \"{name}\" on a schedule ({schedule}) with these inputs: "
                f"{json.dumps(args.get('inputs') or {}, ensure_ascii=False)[:600]}",
                f"bighelp-workflow-schedule:{workflow_id}")
            if not approved:
                return _error("not_approved", message)
            return set_trigger(store, workflow_id, {"kind": "schedule", "schedule": schedule,
                                                    "inputs": args.get("inputs") or {}}, self._facts())
        if kind == "manual":
            return set_trigger(store, workflow_id, {"kind": "manual"}, self._facts())
        return _error("trigger_invalid", "A trigger is manual or schedule.")

    def _archive(self, args, store):
        return api._archive(store, self._body(api._WorkflowBody, workflowId=args.get("workflowId")),
                            self._mutation())

    @staticmethod
    def _ensure_runner(store: WorkflowStore) -> None:
        try:
            from .workflow_coordinator import ensure_coordinator
            ensure_coordinator(store.root)
        except Exception as error:
            logger.warning("bighelp workflows: coordinator start failed (%s)", type(error).__name__)


def register(ctx: Any, *, tools: WorkflowTools | None = None) -> None:
    selected = tools or WorkflowTools()

    def handle(payload: Any = None, **_kwargs: Any) -> str:
        try:
            result = selected.call(payload)
        except Exception as error:
            logger.warning("bighelp workflow tool failed (%s)", type(error).__name__)
            result = _error("workflow_tool_failed", "The workflow tool failed. Try again later.")
        return json.dumps(result, ensure_ascii=False)

    ctx.register_tool(name=TOOL, toolset=TOOLSET, schema=SCHEMA, handler=handle)
