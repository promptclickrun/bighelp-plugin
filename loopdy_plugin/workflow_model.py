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


SCHEMA_VERSION = 2
SCHEMA_VERSIONS = (1, 2)
MAX_LAYOUT_COORDINATE = 100_000
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
STAGE_KINDS = ("agent", "check", "decision", "signoff", "parallel")
# A parallel block runs this many agent stages at once (schemaVersion 2 only).
MIN_BRANCHES = 2
MAX_BRANCHES = 5
# Every stage, the agents inside parallel blocks included.
MAX_ALL_STAGES = 40
# A decision can read the verdicts of several stages (a parallel block's agents).
MAX_DECISION_SOURCES = MAX_BRANCHES
DECISION_REQUIRES = ("all", "any")
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
    if type(value) is dict and type(value.get("schemaVersion")) is int and value["schemaVersion"] > SCHEMA_VERSION:
        _fail("This workflow needs a newer bighelp plugin.")
    version = value.get("schemaVersion") if type(value) is dict else None
    top = _object(value, "The workflow", ("schemaVersion", "name", "stages"),
                  ("description", "roles", "inputs", "limits") + (("layout",) if version == 2 else ()))
    if type(top["schemaVersion"]) is not int or top["schemaVersion"] not in SCHEMA_VERSIONS:
        _fail("This workflow needs a newer bighelp plugin.")
    result: dict[str, Any] = {
        "schemaVersion": top["schemaVersion"],
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
        stages.append(_parse_stage(stage, f"Stage {index + 1}", graph=version == 2))
    result["stages"] = stages
    if len(all_stages(result)) > MAX_ALL_STAGES:
        _fail(f"A workflow has at most {MAX_ALL_STAGES} stages, counting the agents in parallel blocks.")
    if "layout" in top:
        result["layout"] = _parse_layout(top["layout"])
    if encoded_size(result) > MAX_DEFINITION_BYTES:
        _fail("The workflow is too large.")
    return result


def _point(value: Any, where: str) -> dict:
    _object(value, where, ("x", "y"))
    point = {}
    for axis in ("x", "y"):
        number = _number(value[axis], f"{where} {axis}")
        if abs(number) > MAX_LAYOUT_COORDINATE:
            _fail(f"{where} {axis} is too far out.")
        point[axis] = number
    return point


def _parse_layout(value: Any) -> dict:
    """Where the app draws each box. Stored as given; the engine never reads it."""
    _object(value, "The layout", (), ("inputs", "stages"))
    result: dict[str, Any] = {}
    if "inputs" in value:
        result["inputs"] = _point(value["inputs"], "The inputs position")
    if "stages" in value:
        stages = value["stages"]
        if type(stages) is not dict or len(stages) > MAX_STAGES:
            _fail(f"The stage positions must be an object with at most {MAX_STAGES} entries.")
        result["stages"] = {_key(key, "A stage position key"): _point(point, f"The position of {key}")
                            for key, point in stages.items()}
    return result


def _parse_stage(stage: Any, where: str, *, graph: bool = False) -> dict:
    if type(stage) is not dict:
        _fail(f"{where} must be an object.")
    kind = stage.get("kind")
    common = ("key", "kind", "title")
    # schemaVersion 2: every stage but a decision may name the stage after it (`next`), or null to end there.
    edge = ("next",) if graph and kind != "decision" else ()
    if kind == "agent":
        _object(stage, where, common + ("role", "instructions", "outputs"), ("tools", "uses", "minutes") + edge)
    elif kind == "check":
        _object(stage, where, common + ("rules",), edge)
    elif kind == "decision":
        _object(stage, where, common + ("on", "changes"), ("pass", "require"))
    elif kind == "signoff":
        _object(stage, where, common + ("file",), edge)
    elif kind == "parallel":
        if not graph:
            _fail(f"{where} is a parallel block, which needs schemaVersion 2.")
        _object(stage, where, common + ("branches",), edge)
    else:
        _fail(f"{where} has an unknown kind.")
    result: dict[str, Any] = {"key": _key(stage["key"], f"{where} key"), "kind": kind,
                              "title": _text(stage["title"], f"{where} title", MAX_TITLE)}
    if "next" in stage:
        result["next"] = None if stage["next"] is None else _key(stage["next"], f"{where} next")
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
    elif kind == "parallel":
        branches = []
        for number, branch in enumerate(_list(stage["branches"], f"{where} agents", MAX_BRANCHES)):
            label = f"{where} agent {number + 1}"
            if type(branch) is not dict or branch.get("kind") != "agent":
                _fail(f"{label} must be an agent stage.")
            if "next" in branch:
                _fail(f"{label} can't name a stage after it: the block goes on when all its agents are done.")
            branches.append(_parse_stage(branch, label))
        result["branches"] = branches
    elif kind == "decision":
        # One verdict (`"review.decision"`), or several (a parallel block's agents) with `require`.
        sources = stage["on"] if type(stage["on"]) is list else [stage["on"]]
        if not 1 <= len(sources) <= MAX_DECISION_SOURCES:
            _fail(f"{where} reads 1 to {MAX_DECISION_SOURCES} decisions.")
        for source in sources:
            if type(source) is not str or not _is_reference(source) or source.startswith("inputs."):
                _fail(f"{where} must read a stage output.")
        result["on"] = stage["on"] if type(stage["on"]) is str else list(sources)
        if "require" in stage:
            if stage["require"] not in DECISION_REQUIRES:
                _fail(f"{where} must require all or any of its decisions to pass.")
            result["require"] = stage["require"]
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


def all_stages(definition: dict) -> list[dict]:
    """Every stage in list order, each parallel block followed by its agents."""
    result = []
    for stage in definition["stages"]:
        result.append(stage)
        result.extend(stage.get("branches", ()))
    return result


def stage_by_key(definition: dict, key: str) -> dict | None:
    """A stage by key, the agents inside parallel blocks included."""
    for stage in all_stages(definition):
        if stage["key"] == key:
            return stage
    return None


def parent_key(definition: dict, key: str) -> str | None:
    """The parallel block an agent belongs to, or None for a stage in the flow itself."""
    for stage in definition["stages"]:
        if any(branch["key"] == key for branch in stage.get("branches", ())):
            return stage["key"]
    return None


def top_key(definition: dict, key: str) -> str:
    """The stage in the flow itself: a parallel block's agent stands for its block."""
    return parent_key(definition, key) or key


def decision_sources(stage: dict) -> list[str]:
    """The decisions a decision stage reads, one or several."""
    return list(stage["on"]) if type(stage["on"]) is list else [stage["on"]]


def decision_passes(stage: dict, values: list[Any]) -> bool:
    """A decision passes unless its verdicts ask for changes: all must pass (the default), or any."""
    passed = [value != "changes" for value in values]
    return any(passed) if stage.get("require") == "any" else all(passed)


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


def following_key(definition: dict, key: str) -> str | None:
    """The stage after `key` in the list (schemaVersion 1's only order)."""
    stages = definition["stages"]
    index = stage_index(definition)[key]
    return stages[index + 1]["key"] if index + 1 < len(stages) else None


def next_stage_key(definition: dict, key: str) -> str | None:
    """Where the run goes when `key` passes: its `next` (None ends the run), else the following stage.

    For a decision this is where `pass: "next"` goes.
    """
    stage = stage_by_key(definition, key)
    if stage is not None and stage["kind"] != "decision" and "next" in stage:
        return stage["next"]
    return following_key(definition, key)


def pass_target(definition: dict, stage: dict) -> str | None:
    return next_stage_key(definition, stage["key"]) if stage["pass"] == "next" else stage["pass"]


def forward_graph(definition: dict) -> dict[str, list[str]]:
    """Each stage's ways on. Going back (a decision's changes, a sign-off's changes) is not part of it."""
    keys = set(stage_index(definition))
    graph: dict[str, list[str]] = {}
    for stage in definition["stages"]:
        target = pass_target(definition, stage) if stage["kind"] == "decision" else next_stage_key(
            definition, stage["key"])
        graph.setdefault(stage["key"], [])
        if target is not None and target in keys and target not in graph[stage["key"]]:
            graph[stage["key"]].append(target)
    return graph


def _reachable(graph: dict[str, list[str]], start: str) -> set[str]:
    seen, todo = set(), [start]
    while todo:
        key = todo.pop()
        if key in seen or key not in graph:
            continue
        seen.add(key)
        todo.extend(graph[key])
    return seen


def stages_between(definition: dict, start: str, end: str) -> list[str]:
    """Stages on some way from `start` to `end` (both included), in list order: what a loop back runs again."""
    graph = forward_graph(definition)
    reverse: dict[str, list[str]] = {key: [] for key in graph}
    for key, targets in graph.items():
        for target in targets:
            reverse[target].append(key)
    on_way = _reachable(graph, start) & _reachable(reverse, end)
    on_way |= {start, end}
    return [stage["key"] for stage in definition["stages"] if stage["key"] in on_way]


def _dominators(graph: dict[str, list[str]], start: str) -> dict[str, set[str]]:
    """For each stage the start reaches: the stages every way from the start to it passes (itself included)."""
    reach = _reachable(graph, start)
    predecessors = {key: [other for other in reach if key in graph[other]] for key in reach}
    result = {key: set(reach) for key in reach}
    result[start] = {start}
    changed = True
    while changed:
        changed = False
        for key in reach - {start}:
            sets = [result[other] for other in predecessors[key]]
            value = {key} | (set.intersection(*sets) if sets else set())
            if value != result[key]:
                result[key], changed = value, True
    return result


def _cycle_stage(graph: dict[str, list[str]], order: list[str]) -> str | None:
    """The first stage (in list order) on a loop that no decision or sign-off made."""
    state: dict[str, int] = {}
    found: list[str] = []

    def visit(key: str) -> None:
        state[key] = 1
        for target in graph.get(key, []):
            if state.get(target) == 1:
                found.append(target)
            elif target not in state:
                visit(target)
        state[key] = 2

    for key in order:
        if key not in state:
            visit(key)
    if not found:
        return None
    return min(found, key=order.index)


def _issue(code: str, message: str, severity: str = "error", stage: str | None = None) -> dict:
    value = {"code": code, "message": message[:300], "severity": severity}
    if stage is not None:
        value["stageKey"] = stage
    return value


HOST_CODES = frozenset({"role_unbound", "agent_missing", "toolset_unknown", "tool_scope_unsupported"})


def validate(definition: dict, *, bindings: dict[str, str | None] | None = None,
             profile_exists: Callable[[str], bool] | None = None,
             toolset_known: Callable[[str], bool] | None = None, tool_scope: bool = True) -> dict:
    """{valid, host, issues}. `valid`: the definition has no errors. `host`: it can also run on this computer."""
    issues: list[dict] = []
    stages = definition["stages"]
    graph_mode = definition["schemaVersion"] >= 2
    roles = {role["key"]: role["label"] for role in definition["roles"]}
    inputs = {item["key"]: item for item in definition["inputs"]}
    if not definition["name"].strip():
        issues.append(_issue("name_missing", "Give the workflow a name."))
    if not stages:
        issues.append(_issue("no_stages", "Add at least one stage."))
    for collection, label in ((definition["roles"], "role"), (definition["inputs"], "input"),
                              (all_stages(definition), "stage")):
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
    graph = forward_graph(definition) if graph_mode else {}
    reach = _reachable(graph, stages[0]["key"]) if graph_mode and stages else set()
    dominators = _dominators(graph, stages[0]["key"]) if graph_mode and stages else {}
    if graph_mode:
        issues.extend(_graph_issues(definition, graph, reach, index))
    def earlier_output(reference: str, anchor: str, position: int) -> dict | None:
        """The output, when its stage always runs before `anchor` (a parallel block for its agents)."""
        spec = output_spec(definition, reference)
        if spec is None:
            return None
        source = top_key(definition, spec["stageKey"])
        if not graph_mode:
            return spec if index.get(source, len(stages)) < position else None
        if anchor not in reach:
            return spec  # Already reported as unreachable.
        return spec if source != anchor and source in dominators.get(anchor, ()) else None

    def check_agent(stage: dict, anchor: str, position: int) -> None:
        key, title = stage["key"], stage["title"]
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
            if earlier_output(use, anchor, position) is not None:
                continue
            if output_spec(definition, use) is None:
                issues.append(_issue("use_unknown", f"{title} uses {use}, which no earlier stage makes.",
                                     stage=key))
            elif anchor != key and top_key(definition, split_reference(use)[0]) == anchor:
                issues.append(_issue("uses_parallel", f"{title} uses {use}, which runs at the same time.",
                                     stage=key))
            elif graph_mode:
                issues.append(_issue("uses_not_before", f"{title} uses {use}, which doesn't always come first.",
                                     stage=key))
            else:
                issues.append(_issue("use_forward", f"{title} uses {use}, which comes later.", stage=key))
        for tool in stage["tools"]:
            if tool in DENIED_TOOLSETS or tool.startswith("hermes-"):
                issues.append(_issue("tool_not_allowed", f"{title} can't use {tool} in a workflow.", stage=key))
            elif tool in WARNED_TOOLSETS:
                issues.append(_issue("tool_terminal", f"{title} can run commands on this computer.",
                                     "warning", stage=key))
            if toolset_known is not None and tool not in DENIED_TOOLSETS and not toolset_known(tool):
                issues.append(_issue("toolset_unknown", f"This computer has no tool called {tool}.", stage=key))
        if not tool_scope:
            issues.append(_issue("tool_scope_unsupported",
                                 f"This computer's Hermes can't limit the tools of {title}. Update Hermes.",
                                 stage=key))

    for position, stage in enumerate(stages):
        key, title = stage["key"], stage["title"]
        if stage["kind"] == "agent":
            check_agent(stage, key, position)
        elif stage["kind"] == "parallel":
            if not MIN_BRANCHES <= len(stage["branches"]) <= MAX_BRANCHES:
                issues.append(_issue("parallel_branches",
                                     f"{title} runs {MIN_BRANCHES} to {MAX_BRANCHES} agents at once.", stage=key))
            for branch in stage["branches"]:
                check_agent(branch, key, position)
        elif stage["kind"] == "check":
            for rule in stage["rules"]:
                spec = earlier_output(rule["of"], key, position)
                if spec is None or spec["type"] not in RULE_TARGETS[rule["type"]]:
                    issues.append(_issue("check_target", f"{title} can't check {rule['of']} that way.", stage=key))
                if "min" in rule and rule["min"] > rule["max"]:
                    issues.append(_issue("check_range", f"{title} has a minimum above its maximum.", stage=key))
        elif stage["kind"] == "decision":
            for source in decision_sources(stage):
                spec = earlier_output(source, key, position)
                if spec is None or spec["type"] != "decision":
                    issues.append(_issue("decision_source", f"{title} must read an earlier decision.", stage=key))
                elif sorted(spec.get("values", [])) != sorted(DECISION_VALUES):
                    issues.append(_issue("decision_values", f"{title} needs the values pass and changes.",
                                         stage=key))
            target = stage["changes"]["goTo"]
            target_stage = stage_by_key(definition, target)
            if (target_stage is None or target_stage["kind"] not in ("agent", "parallel")
                    or parent_key(definition, target) is not None):
                issues.append(_issue("goto_invalid", f"{title} must send changes back to an agent stage.",
                                     stage=key))
            elif graph_mode and (target == key or key not in _reachable(graph, target)):
                issues.append(_issue("goto_not_earlier", f"{title} must send changes back to a stage before it.",
                                     stage=key))
            elif not graph_mode and index[target] >= position:
                issues.append(_issue("goto_invalid", f"{title} must send changes back to an earlier agent stage.",
                                     stage=key))
            if graph_mode:
                if stage["pass"] != "next" and (stage["pass"] not in index or stage["pass"] == key):
                    issues.append(_issue("pass_invalid", f"{title} must pass to another stage.", stage=key))
            elif stage["pass"] != "next" and index.get(stage["pass"], -1) <= position:
                issues.append(_issue("pass_invalid", f"{title} must pass to a later stage.", stage=key))
        elif stage["kind"] == "signoff":
            spec = earlier_output(stage["file"], key, position)
            if spec is None or spec["type"] != "markdown_file":
                issues.append(_issue("signoff_file", f"{title} must show a file an earlier stage wrote.", stage=key))
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


def _graph_issues(definition: dict, graph: dict[str, list[str]], reach: set[str], index: dict[str, int]) -> list[dict]:
    """schemaVersion 2: where each stage goes. The run starts at the first stage and moves one stage at a time."""
    issues = []
    stages = definition["stages"]
    for stage in stages:
        if stage["kind"] != "decision" and stage.get("next") is not None and stage["next"] not in index:
            issues.append(_issue("next_unknown", f"{stage['title']} goes on to a stage this workflow doesn't have.",
                                 stage=stage["key"]))
    looped = _cycle_stage(graph, [stage["key"] for stage in stages])
    if looped is not None:
        title = stages[index[looped]]["title"]
        issues.append(_issue("cycle", f"{title} is part of a loop. Only a decision's changes can go back.",
                             stage=looped))
    for stage in stages[1:]:
        if stage["key"] not in reach:
            issues.append(_issue("unreachable_stage", f"Nothing leads to {stage['title']}.", stage=stage["key"]))
    if stages and not any(not graph.get(key) for key in reach):
        issues.append(_issue("no_end", "The workflow never ends. Let a stage end the run."))
    return issues


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
def _take(key: str, role: str, title: str, angle: str) -> dict:
    return {"key": key, "kind": "agent", "title": title, "role": role,
            "instructions": (f"Answer the question from this angle: {angle}. Be concrete and short. Then say pass when "
                             "your answer stands on its own, or changes when the question needs a sharper focus, "
                             "with notes saying why."),
            "tools": ["web"], "uses": ["inputs.question"],
            "outputs": [{"name": "answer", "type": "markdown_file"},
                        {"name": "decision", "type": "decision", "values": ["pass", "changes"]},
                        {"name": "notes", "type": "notes"}]}


# Three agents look at one question at the same time, then one writes the answer from all three.
THREE_TAKES = {
    "schemaVersion": 2,
    "name": "Three takes, one answer",
    "description": "Three agents answer at the same time from different angles. When all three agree, one writes "
                   "the answer from them, and you sign it off.",
    "roles": [{"key": "researcher", "label": "Researcher"}, {"key": "skeptic", "label": "Skeptic"},
              {"key": "practitioner", "label": "Practitioner"}, {"key": "writer", "label": "Writer"}],
    "inputs": [{"key": "question", "label": "Question", "type": "long_text", "required": True,
                "sample": "Should a small team keep a shared checklist for releases?"}],
    "limits": {"stageMinutes": 20, "maxRevisions": 1},
    "stages": [
        {"key": "takes", "kind": "parallel", "title": "Three takes at once", "next": "agree",
         "branches": [_take("facts", "researcher", "The facts", "what is known, with sources"),
                      _take("risks", "skeptic", "The risks", "what could go wrong, and what is missing"),
                      _take("practice", "practitioner", "In practice", "what to actually do, step by step")]},
        {"key": "agree", "kind": "decision", "title": "All three agree", "require": "all",
         "on": ["facts.decision", "risks.decision", "practice.decision"],
         "pass": "answer", "changes": {"goTo": "takes"}},
        {"key": "answer", "kind": "agent", "title": "One answer", "role": "writer",
         "instructions": "Write one clear answer from the three takes. Keep what they agree on, and say where they "
                         "differ.",
         "uses": ["inputs.question", "facts.answer", "risks.answer", "practice.answer"],
         "outputs": [{"name": "answer", "type": "markdown_file"}], "next": "signoff"},
        {"key": "signoff", "kind": "signoff", "title": "Your sign-off", "file": "answer.answer", "next": None},
    ],
}

TEMPLATES = {"research-draft-review": RESEARCH_DRAFT_REVIEW, "three-takes": THREE_TAKES}


def template(template_id: str) -> dict | None:
    value = TEMPLATES.get(template_id)
    return copy.deepcopy(value) if value is not None else None


def template_summaries() -> list[dict]:
    return [{"id": key, "name": value["name"], "description": value["description"],
             "stageCount": len(value["stages"]), "roles": copy.deepcopy(value["roles"])}
            for key, value in TEMPLATES.items()]
