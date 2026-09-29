from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

from loopdy_plugin import agent_guide


ROOT = Path(__file__).resolve().parents[1]


class _Context:
    def __init__(self, *, sections=True, duplicate=False):
        self.skills = {}
        self.sections = {}
        self.duplicate = duplicate
        if sections:
            self.register_system_prompt_section = self._register_section

    def register_skill(self, name, path, **kwargs):
        self.skills[name] = {"path": Path(path), **kwargs}

    def _register_section(self, section_id, content):
        if self.duplicate:
            raise ValueError("already registered")
        self.sections[section_id] = content


def _provided_tools() -> set[str]:
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    return set(manifest["provides_tools"])


class AgentGuideTests(unittest.TestCase):
    def test_bighelp_chats_get_the_brief_instead_of_a_terminal(self) -> None:
        # The app's chats carry source "bighelp". Unlabelled, Hermes called them its terminal UI and
        # agents doubted they could send files, cards or reminders.
        brief = agent_guide.prompt_section({"platform": "bighelp"})
        self.assertEqual(brief, agent_guide.CHAT_BRIEF)
        self.assertEqual(agent_guide.prompt_section({"platform": " BigHelp "}), brief)
        self.assertIn("not a terminal", brief)
        self.assertIn("MEDIA:", brief)
        self.assertIn('deliver "local"', brief)
        self.assertIn('skill_view("loopdy:bighelp")', brief)

    def test_other_chats_get_one_pointer_and_unattended_runs_get_nothing(self) -> None:
        for platform in ("telegram", "tui", "cli", "desktop", "imsg", "loopdy"):
            with self.subTest(platform=platform):
                self.assertEqual(agent_guide.prompt_section({"platform": platform}),
                                 agent_guide.ELSEWHERE_NOTE)
        for platform in ("cron", "subagent", "kanban", "tool", "bot_room", "webhook", "api_server", "", None):
            with self.subTest(platform=platform):
                self.assertEqual(agent_guide.prompt_section({"platform": platform}), "")
        self.assertEqual(agent_guide.prompt_section({}), "")

    def test_brief_fits_hermes_section_limits(self) -> None:
        # Hermes skips a section over 4,000 characters and caps all plugins together at 8,000.
        self.assertLessEqual(len(agent_guide.CHAT_BRIEF), 2_000)
        self.assertLessEqual(len(agent_guide.ELSEWHERE_NOTE), 500)
        self.assertRegex(agent_guide.SECTION_ID, r"^[a-z0-9._-]{1,128}$")

    def test_guidance_names_only_tools_the_plugin_provides(self) -> None:
        provided = _provided_tools()
        texts = [agent_guide.CHAT_BRIEF, agent_guide.ELSEWHERE_NOTE]
        texts += [(ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
                  for name in ("bighelp", "generative-ui", "bighelp-feed-and-ideas")]
        for text in texts:
            for tool in set(re.findall(r"\b(?:bighelp|iphone)_[a-z_]+\b", text)):
                if tool == "bighelp_render_":
                    continue
                with self.subTest(tool=tool):
                    self.assertIn(tool, provided)

    def test_no_agent_tool_is_named_loopdy(self) -> None:
        # Agents read tool names; "loopdy_*" left them unsure what the tools were for.
        self.assertEqual([tool for tool in _provided_tools() if tool.startswith("loopdy_")], [])

    def test_registers_the_brief_and_every_guide_skill(self) -> None:
        context = _Context()
        agent_guide.register(context)

        self.assertIs(context.sections["bighelp"], agent_guide.prompt_section)
        self.assertEqual(set(context.skills), {"bighelp", "generative-ui", "custom-theme-authoring"})
        for name, skill in context.skills.items():
            with self.subTest(skill=name):
                self.assertTrue(skill["path"].is_file())
                front = skill["path"].read_text(encoding="utf-8").split("---")[1]
                self.assertEqual(yaml.safe_load(front)["name"], name)
                self.assertEqual(skill["frontmatter"]["name"], name)

    def test_older_hermes_and_repeat_registration_keep_the_plugin_loading(self) -> None:
        older = _Context(sections=False)
        agent_guide.register(older)
        self.assertIn("bighelp", older.skills)

        again = _Context(duplicate=True)
        agent_guide.register(again)
        self.assertEqual(again.sections, {})

    def test_bighelp_skill_links_every_other_skill_the_plugin_ships(self) -> None:
        skill = (ROOT / "skills" / "bighelp" / "SKILL.md").read_text(encoding="utf-8")
        for folder in (ROOT / "skills").iterdir():
            if folder.is_dir() and folder.name != "bighelp":
                with self.subTest(skill=folder.name):
                    self.assertIn(f'skill_view("loopdy:{folder.name}")', skill)

    def test_reminder_guidance_matches_how_alerts_reach_the_phone(self) -> None:
        skill = (ROOT / "skills" / "bighelp" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('`deliver: "local"`', skill)
        self.assertIn("retired Link inbox", skill)
        self.assertIn("`[SILENT]`", skill)
        self.assertIn("`cronjob`", skill)


class HermesSectionContractTests(unittest.TestCase):
    def test_section_id_and_size_are_accepted_by_this_hermes(self) -> None:
        try:
            from hermes_cli.plugins_dispatch import MAX_SYSTEM_PROMPT_SECTION_CHARS
            from hermes_cli.plugins_dispatch import is_valid_system_prompt_section_id
        except ImportError:
            self.skipTest("Hermes without plugin prompt sections")
        self.assertTrue(is_valid_system_prompt_section_id(agent_guide.SECTION_ID))
        self.assertLessEqual(len(agent_guide.CHAT_BRIEF), MAX_SYSTEM_PROMPT_SECTION_CHARS)


if __name__ == "__main__":
    unittest.main()
