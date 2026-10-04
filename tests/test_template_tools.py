from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from loopdy_plugin import agent_profiles, template_tools
from loopdy_plugin.template_catalog import CatalogUnavailable, parse_catalog
from loopdy_plugin.template_tools import (
    CREATE_TOOL,
    FILL_TOOL,
    GET_TOOL,
    SEARCH_TOOL,
    SETTING,
    TOOLSET,
    TemplateTools,
)

import template_catalog_fixtures as fixtures

ROOT = Path(__file__).resolve().parents[1]
VALUES = {"user_name": "Robin", "agent_role": "release lead"}


class _Profiles:
    def __init__(self, existing=()):
        self.existing = set(existing)
        self.created = []
        self.fail_with = None

    def exists(self, profile_id):
        return profile_id in self.existing

    def validate(self, profile_id):
        agent_profiles.validate_profile_id(profile_id)

    def create(self, **kwargs):
        if self.fail_with is not None:
            raise self.fail_with
        if kwargs["agent_id"] in self.existing:
            raise FileExistsError(kwargs["agent_id"])
        self.created.append(kwargs)
        self.existing.add(kwargs["agent_id"])


class _Approvals:
    def __init__(self, answer=None, unattended=None):
        self.answer = answer if answer is not None else {"approved": True, "message": None}
        self.unattended_reason = unattended
        self.asked = []

    def ask(self, description, rule_key):
        self.asked.append({"description": description, "rule_key": rule_key})
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def unattended(self):
        return self.unattended_reason


def _tools(*, allowed=True, profiles=None, approvals=None, catalog=None):
    profiles = profiles or _Profiles()
    approvals = approvals or _Approvals()
    parsed = catalog if catalog is not None else parse_catalog(fixtures.catalog())

    def read_catalog():
        if isinstance(parsed, Exception):
            raise parsed
        return parsed

    tools = TemplateTools(catalog=read_catalog, allowed=lambda: allowed, approve=approvals.ask,
                          unattended=approvals.unattended, profiles=profiles)
    return tools, profiles, approvals


class SearchTests(unittest.TestCase):
    def test_lists_newest_first_with_short_summaries(self):
        tools, _, _ = _tools()
        result = tools.search({})
        self.assertEqual([t["id"] for t in result["templates"]],
                         ["bp-1234abcd-5678", "garden-helper", "release-coordinator",
                          "feed-productivity-1", "anchor"])
        garden = result["templates"][1]
        self.assertEqual(garden["kind"], "agent")
        self.assertEqual(garden["source"], "community")
        self.assertEqual(garden["credit"], "leafy_dev")
        self.assertEqual(garden["role"], "Plans the vegetable patch")
        self.assertEqual(garden["fields"], [
            {"key": "agent_name", "label": "Name", "required": True},
            {"key": "plot_size", "label": "Plot size", "required": True},
            {"key": "household", "label": "Household", "required": True},
        ])
        self.assertNotIn("instructions", garden)
        blueprint = result["templates"][0]
        self.assertEqual(blueprint["kind"], "blueprint")
        self.assertEqual(blueprint["board"], "ideas")
        self.assertIn("[hobby]", blueprint["text"])

    def test_filters_and_sorts(self):
        tools, _, _ = _tools()
        ids = lambda **args: [t["id"] for t in tools.search(args)["templates"]]
        self.assertEqual(ids(kind="agent", sort="name"), ["anchor", "garden-helper", "release-coordinator"])
        self.assertEqual(ids(source="community"), ["bp-1234abcd-5678", "garden-helper"])
        self.assertEqual(ids(source="bighelp", kind="blueprint"), ["feed-productivity-1"])
        self.assertEqual(ids(category="WORK"), ["release-coordinator"])
        self.assertEqual(ids(query="vegetable"), ["garden-helper"])
        self.assertEqual(ids(query="releases"), ["release-coordinator"])
        self.assertEqual(ids(query="monday"), ["feed-productivity-1"])
        self.assertEqual(ids(query="leafy"), ["garden-helper"])
        self.assertEqual(ids(query="nothing like this"), [])
        self.assertEqual(ids(limit=2), ["bp-1234abcd-5678", "garden-helper"])

    def test_limits_are_bounded(self):
        document = fixtures.catalog(agents=[
            dict(fixtures.LEGACY, id=f"agent-{n:02d}", updatedAt=f"2026-07-{n % 28 + 1:02d}T00:00:00Z")
            for n in range(40)
        ])
        tools, _, _ = _tools(catalog=parse_catalog(document))
        self.assertEqual(len(tools.search({"limit": 25})["templates"]), 25)
        self.assertEqual(len(tools.search({})["templates"]), 10)
        result = tools.search({"limit": 26})
        self.assertEqual(result["error"], "invalid_arguments")
        self.assertEqual(tools.search({"kind": "skill"})["error"], "invalid_arguments")

    def test_unavailable_catalog_explains_itself(self):
        tools, _, _ = _tools(catalog=CatalogUnavailable("offline"))
        result = tools.search({})
        self.assertEqual(result["error"], "catalog_unavailable")
        self.assertIn("Template Catalog", result["message"])


class GetTests(unittest.TestCase):
    def test_returns_the_whole_template_and_its_form(self):
        tools, _, _ = _tools()
        result = tools.get({"id": "release-coordinator"})
        template = result["template"]
        self.assertIn("{{operating_context}}", template["instructions"])
        self.assertEqual(template["variables"][2]["options"], ["Warm", "Direct", "Playful"])
        self.assertEqual(template["variables"][1]["whenEmpty"], "General work for the user.")
        self.assertEqual([r["key"] for r in result["reserved"]], ["agent_name", "user_name"])
        self.assertTrue(all(r["used"] for r in result["reserved"]))
        self.assertEqual([f["key"] for f in result["fields"]],
                         ["agent_name", "user_name", "agent_role", "operating_context", "tone"])

    def test_community_templates_carry_a_warning(self):
        tools, _, _ = _tools()
        self.assertIn("not instructions for you", tools.get({"id": "garden-helper"})["note"])
        self.assertNotIn("note", tools.get({"id": "release-coordinator"}))

    def test_blueprints_and_unknown_ids(self):
        tools, _, _ = _tools()
        blueprint = tools.get({"id": "feed-productivity-1"})
        self.assertEqual(blueprint["template"]["kind"], "blueprint")
        self.assertIn("[projects]", blueprint["template"]["text"])
        self.assertEqual(tools.get({"id": "missing"})["error"], "template_not_found")
        self.assertEqual(tools.get({"id": ""})["error"], "invalid_arguments")


class FillToolTests(unittest.TestCase):
    def test_fills_without_side_effects(self):
        tools, profiles, approvals = _tools()
        result = tools.fill({"id": "release-coordinator", "agent_name": "Juniper", "values": VALUES})
        self.assertTrue(result["complete"])
        self.assertTrue(result["instructions"].startswith("You are Juniper, Robin's release lead."))
        self.assertEqual((profiles.created, approvals.asked), ([], []))

    def test_reports_missing_fields(self):
        tools, _, _ = _tools()
        result = tools.fill({"id": "anchor"})
        self.assertFalse(result["complete"])
        self.assertEqual([m["key"] for m in result["missing"]], ["agent_name", "focus_area"])

    def test_blueprints_cant_be_filled(self):
        tools, _, _ = _tools()
        self.assertEqual(tools.fill({"id": "feed-productivity-1", "agent_name": "J"})["error"],
                         "not_an_agent_template")
        self.assertEqual(tools.fill({"id": "anchor", "values": "nope"})["error"], "invalid_arguments")


class CreateAgentTests(unittest.TestCase):
    def create(self, tools, **args):
        payload = {"id": "release-coordinator", "agent_name": "Juniper Bloom", "values": VALUES}
        payload.update(args)
        return tools.create_agent(payload)

    def test_refused_without_permission_and_nothing_is_created(self):
        tools, profiles, approvals = _tools(allowed=False)
        result = self.create(tools)
        self.assertFalse(result["created"])
        self.assertEqual(result["error"], "permission_off")
        self.assertIn(SETTING, result["message"])
        self.assertIn("Ask the person", result["message"])
        self.assertEqual((profiles.created, approvals.asked), ([], []))

    def test_creates_after_the_person_approves(self):
        tools, profiles, approvals = _tools()
        result = self.create(tools)
        self.assertEqual(result, {"created": True, "profile_id": "juniper-bloom", "agent_name": "Juniper Bloom",
                                  "template_id": "release-coordinator"})
        self.assertEqual(len(approvals.asked), 1)
        description = approvals.asked[0]["description"]
        for text in ("Juniper Bloom", "juniper-bloom", "Release Coordinator", "You are Juniper Bloom"):
            self.assertIn(text, description)
        self.assertTrue(approvals.asked[0]["rule_key"].startswith("bighelp-template-agent:juniper-bloom:"))
        created = profiles.created[0]
        self.assertEqual(created["agent_id"], "juniper-bloom")
        self.assertEqual(created["display_name"], "Juniper Bloom")
        self.assertEqual(created["description"], "Keeps Robin's releases on track.")
        self.assertEqual(created["instructions"], (
            "You are Juniper Bloom, Robin's release lead.\nWhere you work: General work for the user.\n"
            "Tone: Direct. Sign off as Juniper Bloom."))

    def test_approval_is_required(self):
        denied = {"approved": False, "message": "BLOCKED: Action denied by user."}
        tools, profiles, approvals = _tools(approvals=_Approvals(answer=denied))
        result = self.create(tools)
        self.assertEqual(result["error"], "not_approved")
        self.assertIn("denied by user", result["message"])
        self.assertEqual(profiles.created, [])

        tools, profiles, _ = _tools(approvals=_Approvals(answer=RuntimeError("/secret/path")))
        result = self.create(tools)
        self.assertEqual(result["error"], "not_approved")
        self.assertNotIn("/secret/path", json.dumps(result))
        self.assertEqual(profiles.created, [])

        tools, profiles, _ = _tools(approvals=_Approvals(answer={"approved": "yes"}))
        self.assertEqual(self.create(tools)["error"], "not_approved")
        self.assertEqual(profiles.created, [])

    def test_no_one_to_ask_means_no_agent(self):
        for reason in ("approvals_off", "unattended"):
            with self.subTest(reason=reason):
                tools, profiles, approvals = _tools(approvals=_Approvals(unattended=reason))
                result = self.create(tools)
                self.assertEqual(result["error"], "approval_unavailable")
                self.assertEqual((profiles.created, approvals.asked), ([], []))

    def test_never_overwrites_a_profile(self):
        tools, profiles, approvals = _tools(profiles=_Profiles(existing={"juniper-bloom"}))
        result = self.create(tools)
        self.assertEqual(result["error"], "profile_exists")
        self.assertIn("juniper-bloom-2", result["message"])
        self.assertEqual((profiles.created, approvals.asked), ([], []))

        # Made while the person was deciding.
        profiles = _Profiles()
        tools, _, approvals = _tools(profiles=profiles,
                                     approvals=_Approvals())
        approvals.ask = lambda description, rule_key: profiles.existing.add("juniper-bloom") or {"approved": True}
        tools.approve = approvals.ask
        self.assertEqual(self.create(tools)["error"], "profile_exists")
        self.assertEqual(profiles.created, [])

    def test_profile_id_is_validated(self):
        for profile_id in ("Bad Id", "../escape", "default", "hermes", "-leading", "x" * 65, "café"):
            with self.subTest(profile_id=profile_id):
                tools, profiles, approvals = _tools()
                result = self.create(tools, profile_id=profile_id)
                self.assertEqual(result["error"], "invalid_profile_id")
                self.assertEqual((profiles.created, approvals.asked), ([], []))
        tools, profiles, _ = _tools()
        self.assertTrue(self.create(tools, profile_id="ops_helper")["created"])
        self.assertEqual(profiles.created[0]["agent_id"], "ops_helper")
        tools, _, _ = _tools()
        self.assertEqual(self.create(tools, agent_name="!!!")["error"], "invalid_profile_id")

    def test_missing_fields_are_refused(self):
        tools, profiles, approvals = _tools()
        result = self.create(tools, values={"user_name": "Robin"})
        self.assertEqual(result["error"], "missing_fields")
        self.assertEqual([m["key"] for m in result["missing"]], ["agent_role"])
        self.assertEqual((profiles.created, approvals.asked), ([], []))
        result = self.create(tools, values={"user_name": "Robin", "agent_role": "x", "tone": "Grumpy"})
        self.assertEqual(result["error"], "missing_fields")
        self.assertEqual(result["invalid"][0]["key"], "tone")

    def test_only_agent_templates(self):
        tools, profiles, _ = _tools()
        self.assertEqual(self.create(tools, id="feed-productivity-1")["error"], "not_an_agent_template")
        self.assertEqual(self.create(tools, id="nope")["error"], "template_not_found")
        self.assertEqual(self.create(tools, agent_name=None)["error"], "invalid_arguments")
        self.assertEqual(profiles.created, [])

    def test_failures_dont_leak_details(self):
        tools, profiles, _ = _tools()
        profiles.fail_with = OSError("/Users/someone/.hermes/profiles: permission denied")
        result = self.create(tools)
        self.assertEqual(result["error"], "create_failed")
        self.assertNotIn("/Users", json.dumps(result))


class HermesApprovalGateTests(unittest.TestCase):
    """The default gate is Hermes' own: no person to ask means no agent."""

    def test_default_gate_fails_closed_without_a_person(self):
        import tools.approval as approval

        with patch.object(approval, "_is_interactive_cli", return_value=False), \
                patch.object(approval, "_is_gateway_approval_context", return_value=False), \
                patch.dict(os.environ, {"HERMES_EXEC_ASK": ""}):
            result = template_tools.request_approval("Create agent", "bighelp-template-agent:x:1")
        self.assertFalse(result["approved"])

    def test_default_gate_asks_the_person(self):
        import tools.approval as approval
        import tools.approval_prompt as approval_prompt

        answers = []
        for choice in ("deny", "once"):
            with patch.object(approval, "_is_interactive_cli", return_value=True), \
                    patch.object(approval, "_is_gateway_approval_context", return_value=False), \
                    patch.object(approval, "is_approved", return_value=False), \
                    patch.object(approval, "prompt_dangerous_approval", return_value=choice, create=True), \
                    patch.object(approval_prompt, "prompt_dangerous_approval", return_value=choice):
                answers.append(template_tools.request_approval("Create agent Juniper",
                                                               f"bighelp-template-agent:juniper:{choice}"))
        self.assertEqual([a["approved"] for a in answers], [False, True])

    def test_approvals_off_or_scheduled_runs_are_unattended(self):
        import tools.approval as approval
        import tools.approval_context as approval_context

        with patch.object(approval, "is_approval_bypass_active", return_value=True):
            self.assertEqual(template_tools.unattended_reason(), "approvals_off")
        with patch.object(approval, "is_approval_bypass_active", return_value=False), \
                patch.object(approval_context, "_is_cron_approval_context", return_value=True):
            self.assertEqual(template_tools.unattended_reason(), "unattended")
        with patch.object(approval, "is_approval_bypass_active", return_value=False), \
                patch.object(approval_context, "_is_cron_approval_context", return_value=False), \
                patch.object(approval_context, "_is_single_query_approval_context", return_value=False), \
                patch.object(approval_context, "_is_unattended_platform_approval_context", return_value=False):
            self.assertIsNone(template_tools.unattended_reason())


class HermesProfileTests(unittest.TestCase):
    """Creation goes through Hermes' public profile helpers in a throwaway home."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "hermes"
        self.home.mkdir()
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.home), "HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        from hermes_cli import profiles
        for name in ("seed_profile_skills", "create_wrapper_script"):
            patcher = patch.object(profiles, name, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_creates_a_new_profile_with_the_filled_soul_and_never_overwrites(self):
        agent_profiles.create_agent_profile(agent_id="juniper", display_name="Juniper",
                                            description="Keeps releases on track.",
                                            instructions="You are Juniper.")
        from hermes_cli import profiles
        directory = profiles.get_profile_dir("juniper")
        self.assertTrue(agent_profiles.HermesProfiles().exists("juniper"))
        self.assertEqual((directory / "SOUL.md").read_text(encoding="utf-8"), "You are Juniper.")
        with self.assertRaises(FileExistsError):
            agent_profiles.create_agent_profile(agent_id="juniper", display_name="Other", description="",
                                                instructions="Overwritten")
        self.assertEqual((directory / "SOUL.md").read_text(encoding="utf-8"), "You are Juniper.")

    def test_profile_ids_use_hermes_rules(self):
        for bad in ("hermes", "default", "Bad", "a/b", "", "x" * 65):
            with self.subTest(profile_id=bad), self.assertRaises(ValueError):
                agent_profiles.validate_profile_id(bad)
        agent_profiles.validate_profile_id("juniper-bloom_2")


class _Context:
    profile_name = "default"

    def __init__(self, config=None):
        self.tools = {}
        self.config = config

    def register_tool(self, *, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, **kwargs}


class _ConfigContext(_Context):
    def get_config(self, key, default=None):
        return (self.config or {}).get(key, default)


class RegistrationTests(unittest.TestCase):
    def test_four_tools_in_their_own_toolset_with_bounded_schemas(self):
        context = _Context()
        template_tools.register(context, tools=_tools()[0])
        self.assertEqual(set(context.tools), {SEARCH_TOOL, GET_TOOL, FILL_TOOL, CREATE_TOOL})
        self.assertEqual(TOOLSET, "bighelp_templates")
        for name, entry in context.tools.items():
            with self.subTest(tool=name):
                self.assertEqual(entry["toolset"], TOOLSET)
                schema = entry["schema"]
                self.assertEqual(schema["name"], name)
                self.assertGreater(len(schema["description"]), 80)
                parameters = schema["parameters"]
                self.assertEqual(parameters["type"], "object")
                self.assertIs(parameters["additionalProperties"], False)
                self._assert_bounded(parameters)
        search = context.tools[SEARCH_TOOL]["schema"]["parameters"]["properties"]
        self.assertEqual(search["limit"]["maximum"], 25)
        self.assertEqual(search["kind"]["enum"], ["agent", "blueprint"])
        self.assertEqual(search["source"]["enum"], ["bighelp", "community", "any"])
        self.assertEqual(search["sort"]["enum"], ["newest", "name"])
        create = context.tools[CREATE_TOOL]["schema"]
        self.assertEqual(create["parameters"]["required"], ["id", "agent_name", "values"])
        self.assertIn("approve", create["description"])
        self.assertIn(SETTING, create["description"])

    def _assert_bounded(self, schema):
        kind = schema.get("type")
        if kind == "string":
            self.assertIn("maxLength", schema)
        elif kind == "object":
            self.assertIn("maxProperties" if "properties" not in schema else "properties", schema)
            for child in schema.get("properties", {}).values():
                self._assert_bounded(child)
            extra = schema.get("additionalProperties")
            if isinstance(extra, dict):
                self.assertIn("maxProperties", schema)
                for option in extra.get("anyOf", [extra]):
                    self._assert_bounded(option)
        elif kind == "integer":
            self.assertIn("maximum", schema)

    def test_handlers_answer_json(self):
        context = _Context()
        template_tools.register(context, tools=_tools()[0])
        answer = json.loads(context.tools[GET_TOOL]["handler"]({"id": "anchor"}))
        self.assertEqual(answer["template"]["id"], "anchor")
        answer = json.loads(context.tools[SEARCH_TOOL]["handler"](None))
        self.assertIn("templates", answer)

    def test_permission_is_read_from_plugin_settings_each_time(self):
        context = _ConfigContext({})
        self.assertFalse(template_tools.creation_allowed(context))
        for value, allowed in ((True, True), ("true", True), (" TRUE ", True), (False, False), ("yes", False),
                               (1, False), (None, False)):
            with self.subTest(value=value):
                context.config = {SETTING: value}
                self.assertIs(template_tools.creation_allowed(context), allowed)
        self.assertFalse(template_tools.creation_allowed(_Context()))

        class _Broken(_Context):
            def get_config(self, key, default=None):
                raise ValueError("bad config")
        self.assertFalse(template_tools.creation_allowed(_Broken()))

    def test_manifest_declares_the_tools_and_the_off_by_default_setting(self):
        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        self.assertTrue({SEARCH_TOOL, GET_TOOL, FILL_TOOL, CREATE_TOOL} <= set(manifest["provides_tools"]))
        setting = manifest["config_schema"][SETTING]
        self.assertEqual((setting["type"], setting["default"]), ("bool", False))

    def test_plugin_registers_the_template_tools(self):
        from loopdy_plugin import registration

        class _Service:
            store = type("Store", (), {})()

        class _Full(_Context):
            def register_platform(self, **_kwargs): pass
            def register_approval_transport(self, *_args): pass
            def register_hook(self, *_args): pass
            def register_skill(self, *_args, **_kwargs): pass
            def register_cli_command(self, **_kwargs): pass
            def on_unload(self, *_args): pass

        context = _Full()
        registration.register(context, service=_Service())
        self.assertEqual(context.tools[CREATE_TOOL]["toolset"], TOOLSET)

    def test_docs_and_skill_point_to_the_tools(self):
        doc = (ROOT / "docs" / "TEMPLATE_TOOLS.md").read_text(encoding="utf-8")
        for text in (SEARCH_TOOL, GET_TOOL, FILL_TOOL, CREATE_TOOL, SETTING,
                     "services/catalog/docs/TEMPLATE_VARIABLES.md"):
            self.assertIn(text, doc)
        self.assertIn("docs/TEMPLATE_TOOLS.md", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn(SEARCH_TOOL, (ROOT / "skills" / "bighelp" / "SKILL.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
