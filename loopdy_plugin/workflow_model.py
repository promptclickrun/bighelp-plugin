"""Workflow definitions: shape checks, validation and the built-in templates (docs/WORKFLOWS.md).

Pure functions only. Host facts (does this agent exist, is this toolset known) come in as callables, so the
dashboard, the coordinator and tests can all use this module without Hermes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from typing import Any, Callable


SCHEMA_VERSION = 1
KEY = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
TOOLSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
MAX_DEFINITION_BYTES = 64 * 1024
MAX_STAGES = 20
MAX_ROLES = 20
MAX_INPUTS = 20
MAX_OUTPUTS = 10
MAX_RULES = 10
MAX_USES = 20
MAX_TOOLS = 20
MAX_INSTRUCTIONS = 8000
MAX_TITLE = 80
MAX_DESCRIPTION = 1000
MAX_STAGE_MINUTES = 60
DEFAULT_STAGE_MINUTES = 20
MAX_REVISIONS = 5
DEFAULT_MAX_REVISIONS = 2
STAGE_KINDS = ("agent", "check", "decision", "signoff")
INPUT_TYPES = ("text", "long_text", "number", "choice")
INPUT_TEXT_LIMITS = {"text": 2000, "long_text": 20000}
OUTPUT_TYPES = ("markdown_file", "text", "number", "decision", "notes")
RULE_TYPES = ("word_range", "has_title", "not_empty", "number_range")
DECISION_VALUES = ("pass", "changes")
# A stage must never reach people or start other work on its own. The platform bundles (hermes-*) include
# messaging, so they are refused too. `clarify` would wait for an answer nobody can give.
DENIED_TOOLSETS = frozenset({
    "messaging", "send_message", "cronjob", "kanban", "delegation", "homeassistant", "clarify",
    "all", "*", "bot_room", "discord", "discord_admin", "yuanbao", "feishu_doc", "feishu_drive",
})
WARNED_TOOLSETS = frozenset({"terminal"})
RULE_TARGETS = {
    "word_range": ("markdown_file", "text"),
    "has_title": ("markdown_file", "text"),
    "not_empty": ("markdown_file", "text", "notes"),
    "number_range": ("number",),
}


class DefinitionError(ValueError):
    """The definition has the wrong shape. The message is safe to show."""


class InputsError(ValueError):
    """Run inputs are missing or have the wrong type. The message is safe to show."""


def _fail(message: str) -> None:
    raise DefinitionError(message)


def _object(value: Any, where: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> dict:
    if type(value) is not dict:
        _fail(f"{where} must be an object.")
    unknown = set(value) - set(required) - set(optional)
    if unknown:
        _fail(f"{where} has an unknown field: {sorted(unknown)[0][:40]}.")
    for name in required:
        if name not in value:
            _fail(f"{where} needs {name}.")
    return value


def _text(value: Any, where: str, maximum: int, *, empty: bool = False) -> str:
    if type(value) is not str or "\x00" in value or len(value) > maximum or (not empty and not value.strip()):
        _fail(f"{where} must be text of at most {maximum} characters.")
    return value


def _key(value: Any, where: str) -> str:
    if type(value) is not str or KEY.fullmatch(value) is None:
        _fail(f"{where} must be a short lowercase key.")
    return value


def _integer(value: Any, where: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail(f"{where} must be a whole number from {minimum} to {maximum}.")
    return value


def _number(value: Any, where: str) -> float | int:
    if type(value) not in (int, float) or not math.isfinite(value):
        _fail(f"{where} must be a number.")
    return value


def _list(value: Any, where: str, maximum: int) -> list:
    if type(value) is not list or len(value) > maximum:
        _fail(f"{where} must be a list of at most {maximum} items.")
    return value


def _bool(value: Any, where: str) -> bool:
    if type(value) is not bool:
        _fail(f"{where} must be true or false.")
    return value


def encoded_size(definition: dict) -> int:
    return len(canonical_json(definition).encode("utf-8"))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def definition_sha256(definition: dict) -> str:
    return hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()


def parse_definition(value: Any) -> dict:
    """A normalized copy of a well-formed definition, or DefinitionError. Semantics are left to validate()."""
    top = _object(value, "The workflow", ("schemaVersion", "name", "stages"),
                  ("description", "roles", "inputs", "limits"))
    if top["schemaVersion"] != SCHEMA_VERSION or type(top["schemaVersion"]) is not int:
        _fail("This workflow needs a newer bighelp plugin.")
    result: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "name": _text(top["name"], "The name", MAX_TITLE, empty=True).strip(),
        "description": _text(top.get("description", ""), "The description", MAX_DESCRIPTION, empty=True),
    }
    roles = []
    for index, role in enumerate(_list(top.get("roles", []), "Roles", MAX_ROLES)):
        _object(role, f"Role {index + 1}", ("key", "label"))
        roles.append({"key": _key(role["key"], f"Role {index + 1} key"),
                      "label": _text(role["label"], f"Role {index + 1} label", MAX_TITLE)})
    result["roles"] = roles
    inputs = []
    for index, item in enumerate(_list(top.get("inputs", []), "Inputs", MAX_INPUTS)):
        where = f"Input {index + 1}"
        _object(item, where, ("key", "label", "type"), ("required", "choices", "sample"))
        kind = item["type"]
        if kind not in INPUT_TYPES:
            _fail(f"{where} has an unknown type.")
        parsed = {"key": _key(item["key"], f"{where} key"), "label": _text(item["label"], f"{where} label", MAX_TITLE),
                  "type": kind, "required": _bool(item.get("required", False), f"{where} required")}
        if "choices" in item:
            choices = _list(item["choices"], f"{where} choices", 20)
            parsed["choices"] = [_text(choice, f"{where} choice", MAX_TITLE) for choice in choices]
        if "sample" in item:
            sample = item["sample"]
            if kind == "number":
                parsed["sample"] = _number(sample, f"{where} sample")
            else:
                parsed["sample"] = _text(sample, f"{where} sample", INPUT_TEXT_LIMITS.get(kind, MAX_TITLE), empty=True)
        inputs.append(parsed)
    result["inputs"] = inputs
    limits = _object(top.get("limits", {}), "Limits", (), ("stageMinutes", "maxRevisions"))
    result["limits"] = {
        "stageMinutes": _integer(limits.get("stageMinutes", DEFAULT_STAGE_MINUTES), "The stage time limit",
                                 1, MAX_STAGE_MINUTES),
        "maxRevisions": _integer(limits.get("maxRevisions", DEFAULT_MAX_REVISIONS), "The revision limit",
                                 0, MAX_REVISIONS),
    }
    stages = []
    for index, stage in enumerate(_list(top["stages"], "Stages", MAX_STAGES)):
        stages.append(_parse_stage(stage, f"Stage {index + 1}"))
    result["stages"] = stages
    if encoded_size(result) > MAX_DEFINITION_BYTES:
        _fail("The workflow is too large.")
    return result


def _parse_stage(stage: Any, where: str) -> dict:
    if type(stage) is not dict:
        _fail(f"{where} must be an object.")
    kind = stage.get("kind")
    common = ("key", "kind", "title")
    if kind == "agent":
        _object(stage, where, common + ("role", "instructions", "outputs"), ("tools", "uses", "minutes"))
    elif kind == "check":
        _object(stage, where, common + ("rules",))
    elif kind == "decision":
        _object(stage, where, common + ("on", "changes"), ("pass",))
    elif kind == "signoff":
        _object(stage, where, common + ("file",))
    else:
        _fail(f"{where} has an unknown kind.")
    result: dict[str, Any] = {"key": _key(stage["key"], f"{where} key"), "kind": kind,
                              "title": _text(stage["title"], f"{where} title", MAX_TITLE)}
    if kind == "agent":
        result["role"] = _key(stage["role"], f"{where} role")
        result["instructions"] = _text(stage["instructions"], f"{where} instructions", MAX_INSTRUCTIONS, empty=True)
        tools = _list(stage.get("tools", []), f"{where} tools", MAX_TOOLS)
        for tool in tools:
            if type(tool) is not str or TOOLSET.fullmatch(tool) is None:
                _fail(f"{where} has an invalid tool name.")
        result["tools"] = list(tools)
        uses = _list(stage.get("uses", []), f"{where} uses", MAX_USES)
        for use in uses:
            if type(use) is not str or not _is_reference(use):
                _fail(f"{where} has an invalid reference.")
        result["uses"] = list(uses)
        outputs = []
        for number, output in enumerate(_list(stage["outputs"], f"{where} outputs", MAX_OUTPUTS)):
            label = f"{where} output {number + 1}"
            _object(output, label, ("name", "type"), ("values",))
            if output["type"] not in OUTPUT_TYPES:
                _fail(f"{label} has an unknown type.")
            parsed = {"name": _key(output["name"], f"{label} name"), "type": output["type"]}
            if "values" in output:
                if output["type"] != "decision":
                    _fail(f"{label} can have values only as a decision.")
                values = _list(output["values"], f"{label} values", 10)
                parsed["values"] = [_key(item, f"{label} value") for item in values]
            elif output["type"] == "decision":
                parsed["values"] = list(DECISION_VALUES)
            outputs.append(parsed)
        result["outputs"] = outputs
        if "minutes" in stage:
            result["minutes"] = _integer(stage["minutes"], f"{where} time limit", 1, MAX_STAGE_MINUTES)
    elif kind == "check":
        rules = []
        for number, rule in enumerate(_list(stage["rules"], f"{where} rules", MAX_RULES)):
            label = f"{where} rule {number + 1}"
            if type(rule) is not dict or rule.get("type") not in RULE_TYPES:
                _fail(f"{label} has an unknown type.")
            ranged = rule["type"] in ("word_range", "number_range")
            _object(rule, label, ("type", "of") + (("min", "max") if ranged else ()))
            if type(rule["of"]) is not str or not _is_reference(rule["of"]) or rule["of"].startswith("inputs."):
                _fail(f"{label} must read a stage output.")
            parsed = {"type": rule["type"], "of": rule["of"]}
            if ranged:
                parsed["min"] = _number(rule["min"], f"{label} minimum")
                parsed["max"] = _number(rule["max"], f"{label} maximum")
            rules.append(parsed)
        result["rules"] = rules
    elif kind == "decision":
        if type(stage["on"]) is not str or not _is_reference(stage["on"]) or stage["on"].startswith("inputs."):
            _fail(f"{where} must read a stage output.")
        result["on"] = stage["on"]
        target = stage.get("pass", "next")
        if target != "next":
            _key(target, f"{where} pass")
        result["pass"] = target
        changes = _object(stage["changes"], f"{where} changes", ("goTo",), ("maxRevisions",))
        result["changes"] = {"goTo": _key(changes["goTo"], f"{where} goTo")}
        if "maxRevisions" in changes:
            result["changes"]["maxRevisions"] = _integer(changes["maxRevisions"], f"{where} revision limit",
                                                         0, MAX_REVISIONS)
    else:
        if type(stage["file"]) is not str or not _is_reference(stage["file"]) or stage["file"].startswith("inputs."):
            _fail(f"{where} must name a stage's file.")
        result["file"] = stage["file"]
    return result


def _is_reference(value: str) -> bool:
    head, dot, tail = value.partition(".")
    return bool(dot) and KEY.fullmatch(tail) is not None and (head == "inputs" or KEY.fullmatch(head) is not None)


def split_reference(value: str) -> tuple[str, str]:
    head, _, tail = value.partition(".")
    return head, tail


def stage_index(definition: dict) -> dict[str, int]:
    return {stage["key"]: index for index, stage in enumerate(definition["stages"])}


def stage_by_key(definition: dict, key: str) -> dict | None:
    for stage in definition["stages"]:
        if stage["key"] == key:
            return stage
    return None


def output_spec(definition: dict, reference: str) -> dict | None:
    """The output a `<stage>.<name>` reference names, with its stage key, or None."""
    stage_key, name = split_reference(reference)
    stage = stage_by_key(definition, stage_key)
    if stage is None or stage["kind"] != "agent":
        return None
    for output in stage["outputs"]:
        if output["name"] == name:
            return {**output, "stageKey": stage_key}
    return None


def stage_minutes(definition: dict, stage: dict) -> int:
    return int(stage.get("minutes", definition["limits"]["stageMinutes"]))


def max_revisions(definition: dict, stage: dict) -> int:
    return int(stage["changes"].get("maxRevisions", definition["limits"]["maxRevisions"]))


def next_stage_key(definition: dict, key: str) -> str | None:
    stages = definition["stages"]
    index = stage_index(definition)[key]
    return stages[index + 1]["key"] if index + 1 < len(stages) else None


def _issue(code: str, message: str, severity: str = "error", stage: str | None = None) -> dict:
    value = {"code": code, "message": message[:300], "severity": severity}
    if stage is not None:
        value["stageKey"] = stage
    return value


HOST_CODES = frozenset({"role_unbound", "agent_missing", "toolset_unknown"})


def validate(definition: dict, *, bindings: dict[str, str | None] | None = None,
             profile_exists: Callable[[str], bool] | None = None,
             toolset_known: Callable[[str], bool] | None = None) -> dict:
    """{valid, host, issues}. `valid`: the definition has no errors. `host`: it can also run on this computer."""
    issues: list[dict] = []
    stages = definition["stages"]
    roles = {role["key"]: role["label"] for role in definition["roles"]}
    inputs = {item["key"]: item for item in definition["inputs"]}
    if not definition["name"].strip():
        issues.append(_issue("name_missing", "Give the workflow a name."))
    if not stages:
        issues.append(_issue("no_stages", "Add at least one stage."))
    seen: set[str] = set()
    for collection, label in ((definition["roles"], "role"), (definition["inputs"], "input"), (stages, "stage")):
        keys: set[str] = set()
        for item in collection:
            if item["key"] in keys:
                issues.append(_issue("duplicate_key", f"Two {label}s use the key {item['key']}."))
            keys.add(item["key"])
    for item in definition["inputs"]:
        if item["type"] == "choice" and not item.get("choices"):
            issues.append(_issue("choices_missing", f"{item['label']} needs at least one choice."))
    used_roles: set[str] = set()
    index = {}
    for position, stage in enumerate(stages):
        index.setdefault(stage["key"], position)
    for position, stage in enumerate(stages):
        key, title = stage["key"], stage["title"]

        def earlier_output(reference: str) -> dict | None:
            spec = output_spec(definition, reference)
            if spec is None or index.get(spec["stageKey"], len(stages)) >= position:
                return None
            return spec

        if stage["kind"] == "agent":
            used_roles.add(stage["role"])
            if stage["role"] not in roles:
                issues.append(_issue("role_unknown", f"{title} uses a role this workflow doesn't have.", stage=key))
            if not stage["instructions"].strip():
                issues.append(_issue("instructions_missing", f"Tell {title} what to do.", stage=key))
            if not stage["outputs"]:
                issues.append(_issue("no_outputs", f"{title} must hand off at least one output.", stage=key))
            names = [output["name"] for output in stage["outputs"]]
            if len(names) != len(set(names)):
                issues.append(_issue("duplicate_key", f"{title} has two outputs with one name.", stage=key))
            for output in stage["outputs"]:
                if output["type"] == "decision" and not output.get("values"):
                    issues.append(_issue("decision_values", f"{title} needs decision values.", stage=key))
            for use in stage["uses"]:
                head, tail = split_reference(use)
                if head == "inputs":
                    if tail not in inputs:
                        issues.append(_issue("use_unknown", f"{title} uses {use}, which isn't an input.", stage=key))
                    continue
                if earlier_output(use) is not None:
                    continue
                if output_spec(definition, use) is not None:
                    issues.append(_issue("use_forward", f"{title} uses {use}, which comes later.", stage=key))
                else:
                    issues.append(_issue("use_unknown", f"{title} uses {use}, which no earlier stage makes.",
                                         stage=key))
            for tool in stage["tools"]:
                if tool in DENIED_TOOLSETS or tool.startswith("hermes-"):
                    issues.append(_issue("tool_not_allowed", f"{title} can't use {tool} in a workflow.", stage=key))
                elif tool in WARNED_TOOLSETS:
                    issues.append(_issue("tool_terminal", f"{title} can run commands on this computer.",
                                         "warning", stage=key))
                if toolset_known is not None and tool not in DENIED_TOOLSETS and not toolset_known(tool):
                    issues.append(_issue("toolset_unknown", f"This computer has no tool called {tool}.", stage=key))
        elif stage["kind"] == "check":
            for rule in stage["rules"]:
                spec = earlier_output(rule["of"])
                if spec is None or spec["type"] not in RULE_TARGETS[rule["type"]]:
                    issues.append(_issue("check_target", f"{title} can't check {rule['of']} that way.", stage=key))
                if "min" in rule and rule["min"] > rule["max"]:
                    issues.append(_issue("check_range", f"{title} has a minimum above its maximum.", stage=key))
        elif stage["kind"] == "decision":
            spec = earlier_output(stage["on"])
            if spec is None or spec["type"] != "decision":
                issues.append(_issue("decision_source", f"{title} must read an earlier decision.", stage=key))
            elif sorted(spec.get("values", [])) != sorted(DECISION_VALUES):
                issues.append(_issue("decision_values", f"{title} needs the values pass and changes.", stage=key))
            target = stage["changes"]["goTo"]
            target_stage = stage_by_key(definition, target)
            if target_stage is None or target_stage["kind"] != "agent" or index[target] >= position:
                issues.append(_issue("goto_invalid", f"{title} must send changes back to an earlier agent stage.",
                                     stage=key))
            if stage["pass"] != "next" and index.get(stage["pass"], -1) <= position:
                issues.append(_issue("pass_invalid", f"{title} must pass to a later stage.", stage=key))
        elif stage["kind"] == "signoff":
            spec = earlier_output(stage["file"])
            if spec is None or spec["type"] != "markdown_file":
                issues.append(_issue("signoff_file", f"{title} must show a file an earlier stage wrote.", stage=key))
        seen.add(key)
    for role_key, label in roles.items():
        if role_key not in used_roles:
            issues.append(_issue("role_unused", f"No stage uses {label}.", "warning"))
    if bindings is not None:
        for role_key, label in roles.items():
            if role_key not in used_roles:
                continue
            agent = bindings.get(role_key)
            if not agent:
                issues.append(_issue("role_unbound", f"Choose an agent for {label}."))
            elif profile_exists is not None and not profile_exists(agent):
                issues.append(_issue("agent_missing", f"The agent for {label} no longer exists."))
    valid = not any(item["severity"] == "error" and item["code"] not in HOST_CODES for item in issues)
    host = bindings is not None and not any(item["severity"] == "error" and item["code"] in HOST_CODES
                                            for item in issues)
    return {"valid": valid, "host": host, "issues": issues}


def parse_inputs(definition: dict, value: Any) -> dict:
    """Run inputs checked against the definition; missing optional text becomes ""."""
    if type(value) is not dict:
        raise InputsError("Inputs must be an object.")
    fields = {item["key"]: item for item in definition["inputs"]}
    unknown = set(value) - set(fields)
    if unknown:
        raise InputsError("There is an input this workflow doesn't have.")
    result: dict[str, Any] = {}
    for key, item in fields.items():
        present = key in value and value[key] is not None and value[key] != ""
        if not present:
            if item["required"]:
                raise InputsError(f"Fill in {item['label']}.")
            result[key] = None if item["type"] == "number" else ""
            continue
        given = value[key]
        if item["type"] == "number":
            if type(given) not in (int, float) or not math.isfinite(given):
                raise InputsError(f"{item['label']} must be a number.")
        elif item["type"] == "choice":
            if given not in item.get("choices", []):
                raise InputsError(f"Choose one of the choices for {item['label']}.")
        else:
            limit = INPUT_TEXT_LIMITS[item["type"]]
            if type(given) is not str or len(given) > limit or "\x00" in given:
                raise InputsError(f"{item['label']} must be text of at most {limit} characters.")
        result[key] = given
    return result


RESEARCH_DRAFT_REVIEW = {
    "schemaVersion": 1,
    "name": "Research, draft, review",
    "description": ("One agent researches the topic, one writes a draft and one reviews it. "
                    "You approve the final file."),
    "roles": [
        {"key": "researcher", "label": "Researcher"},
        {"key": "writer", "label": "Writer"},
        {"key": "reviewer", "label": "Reviewer"},
    ],
    "inputs": [
        {"key": "topic", "label": "Topic", "type": "text", "required": True,
         "sample": "How small teams use checklists"},
        {"key": "audience", "label": "Who it is for", "type": "text", "required": False,
         "sample": "New team leads"},
        {"key": "length", "label": "Length", "type": "choice", "required": True,
         "choices": ["Short", "Medium", "Long"], "sample": "Medium"},
    ],
    "limits": {"stageMinutes": 20, "maxRevisions": 2},
    "stages": [
        {"key": "research", "kind": "agent", "title": "Research the topic", "role": "researcher",
         "instructions": ("Research the topic for the audience. Find the main facts, a few good examples and the "
                          "sources you used. Write a short research brief in Markdown with a title, the key points "
                          "and a list of sources."),
         "tools": ["web"], "uses": ["inputs.topic", "inputs.audience"],
         "outputs": [{"name": "brief", "type": "markdown_file"}]},
        {"key": "draft", "kind": "agent", "title": "Write the draft", "role": "writer",
         "instructions": ("Write the piece from the research brief. Start with a title line (# Title). Keep to the "
                          "length asked for: Short is about 400 words, Medium about 800, Long about 1,500. "
                          "Also give the word count."),
         "tools": [], "uses": ["inputs.topic", "inputs.audience", "inputs.length", "research.brief"],
         "outputs": [{"name": "draft", "type": "markdown_file"}, {"name": "word_count", "type": "number"}]},
        {"key": "length_check", "kind": "check", "title": "Check length and title",
         "rules": [{"type": "word_range", "of": "draft.draft", "min": 150, "max": 3000},
                   {"type": "has_title", "of": "draft.draft"}]},
        {"key": "review", "kind": "agent", "title": "Review the draft", "role": "reviewer",
         "instructions": ("Review the draft against the research brief. Check facts, clarity and length. Decide "
                          "pass if it is ready for the person to approve, or changes if the writer must fix "
                          "something. Give short, specific notes; mark a note major when it must be fixed."),
         "tools": [], "uses": ["inputs.topic", "inputs.audience", "research.brief", "draft.draft"],
         "outputs": [{"name": "decision", "type": "decision", "values": ["pass", "changes"]},
                     {"name": "notes", "type": "notes"}],
         "minutes": 15},
        {"key": "review_decision", "kind": "decision", "title": "Pass or send back", "on": "review.decision",
         "pass": "next", "changes": {"goTo": "draft", "maxRevisions": 2}},
        {"key": "signoff", "kind": "signoff", "title": "Your sign-off", "file": "draft.draft"},
    ],
}
TEMPLATES = {"research-draft-review": RESEARCH_DRAFT_REVIEW}


def template(template_id: str) -> dict | None:
    value = TEMPLATES.get(template_id)
    return copy.deepcopy(value) if value is not None else None


def template_summaries() -> list[dict]:
    return [{"id": key, "name": value["name"], "description": value["description"],
             "stageCount": len(value["stages"]), "roles": copy.deepcopy(value["roles"])}
            for key, value in TEMPLATES.items()]
