"""Every `skill_view("loopdy:<name>")` the plugin tells an agent to load must be a skill it registers.

3.25.0 sent every Teamwork agent to `loopdy:profile-agent-collaboration`, which was never registered, so the
first instruction of each handoff failed with "Skill not found in plugin 'loopdy'".
"""
from __future__ import annotations

from pathlib import Path
import re
import unittest

from loopdy_plugin import agent_guide, team

ROOT = Path(__file__).resolve().parents[1]
REFERENCE = re.compile(r'loopdy:([a-z0-9][a-z0-9-]*)')


def registered() -> set[str]:
    names = {name for name, _description in agent_guide.SKILLS}
    names.add("bighelp-feed-and-ideas")  # agent_board registers it with its own description
    return names


class SkillReferenceTests(unittest.TestCase):
    def test_every_skill_folder_is_registered(self):
        folders = {path.parent.name for path in (ROOT / "skills").glob("*/SKILL.md")}
        self.assertEqual(folders, registered(), "A skill folder agents can't load, or a registration with no folder")
        self.assertIn('register_skill("bighelp-feed-and-ideas"', (ROOT / "loopdy_plugin" / "agent_board.py").read_text())

    def test_every_loopdy_skill_an_agent_is_told_to_load_exists(self):
        texts = {path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
                 for path in [*(ROOT / "skills").rglob("*.md"), *(ROOT / "loopdy_plugin").rglob("*.py")]}
        agents = [{"id": "default", "name": "Ada", "role": "Lead", "about": ""}, {"id": "nova", "name": "Nova", "role": "Research", "about": ""}]
        texts["team brief"] = team.brief("default", agents)
        known = registered()
        for where, text in texts.items():
            for name in REFERENCE.findall(text):
                if name in {"bighelp"} or name in known:
                    continue
                with self.subTest(where=where):
                    self.fail(f"{where} points agents at loopdy:{name}, which the plugin doesn't register")


if __name__ == "__main__":
    unittest.main()
