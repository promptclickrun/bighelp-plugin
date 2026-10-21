from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from loopdy_plugin import plugin_links

SOURCE = "https://example.com/acme/weather-plugin"
REVISION = "0123456789abcdef0123456789abcdef01234567"


class PluginLinkTests(unittest.TestCase):
    """Hermes copies the default agent's plugins/ into every agent cloned from it (``_clone_plugins``). A plugin
    that ships its own buildable package then exists twice, and uv refuses every later plugin install on the host:
    "Two workspace members are both named `weather-mem`"."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="loopdy-plugin-links-", dir="/private/tmp")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.default = self.root / "home" / "plugins"
        self.agent = self.root / "home" / "profiles" / "ralph" / "plugins"

    def install(self, plugins: Path, name: str = "weather", *, revision: str = REVISION, source: str = SOURCE,
                record: bool = True, files: dict[str, str] | None = None) -> Path:
        folder = plugins / name
        for relative, text in (files or {"plugin.yaml": f"name: {name}\n", "__init__.py": "def register(ctx): pass\n",
                                         "pyproject.toml": "[build-system]\n[project]\nname = 'weather-mem'\n"}).items():
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        metadata_path = plugins / ".install-metadata.json"
        metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
        if record:
            metadata[name] = {"pinned": True, "revision": revision, "source": source}
        metadata_path.write_text(json.dumps(metadata))
        return folder

    def clone(self, name: str = "weather") -> Path:
        """What Hermes' clone does: copytree(symlinks=True) of the default agent's plugins/."""
        self.agent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.default / name, self.agent / name, symlinks=True)
        records = json.loads((self.default / ".install-metadata.json").read_text())
        mine_path = self.agent / ".install-metadata.json"
        mine = json.loads(mine_path.read_text()) if mine_path.exists() else {}
        if name in records:
            mine[name] = records[name]
        mine_path.write_text(json.dumps(mine))
        (self.agent / ".install-metadata.json.lock").touch()  # Hermes' installer lock, cloned with the folder
        return self.agent / name

    def test_an_exact_copy_becomes_a_link_to_the_default_agents_plugin(self):
        self.install(self.default)
        copy = self.clone()
        (self.default / "weather" / ".git").mkdir()
        (self.default / "weather" / ".git" / "FETCH_HEAD").write_text("fetched since the clone\n")
        (copy / "__pycache__").mkdir()
        (copy / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"\0")
        metadata = (self.agent / ".install-metadata.json").read_bytes()

        result = plugin_links.link_plugin_copies(self.default, self.agent)

        self.assertEqual(result, {"linked": ["weather"], "kept": []})
        self.assertTrue(copy.is_symlink())
        self.assertEqual(os.readlink(copy), str(self.default / "weather"))
        self.assertEqual((copy / "plugin.yaml").read_text(), "name: weather\n")
        self.assertEqual((self.agent / ".install-metadata.json").read_bytes(), metadata, "The install record stays")
        self.assertEqual(sorted(p.name for p in self.agent.iterdir()),
                         [".install-metadata.json", ".install-metadata.json.lock", "weather"],
                         "No leftovers next to the agent's plugins")
        self.assertEqual(plugin_links.link_plugin_copies(self.default, self.agent), {"linked": [], "kept": []},
                         "A link is left as it is")

    def test_a_copy_that_differs_is_left_alone(self):
        self.install(self.default)
        self.clone()
        (self.agent / "weather" / "__init__.py").write_text("def register(ctx): print('edited')\n")
        self.install(self.default, "rss")
        self.clone("rss")
        self.install(self.agent, "rss", revision="f" * 40)
        self.install(self.default, "notes", record=False)
        self.clone("notes")

        result = plugin_links.link_plugin_copies(self.default, self.agent)

        self.assertEqual(result["linked"], [])
        self.assertEqual(result["kept"], [{"name": "notes", "reason": "not_installed"},
                                          {"name": "rss", "reason": "different_install"},
                                          {"name": "weather", "reason": "changed"}])
        for name in ("weather", "rss", "notes"):
            self.assertFalse((self.agent / name).is_symlink(), name)

    def test_the_agents_own_plugins_and_hidden_entries_are_untouched(self):
        self.install(self.default)
        own = self.install(self.agent, "only-mine")
        staging = self.agent / ".install-weather-abc"
        staging.mkdir(parents=True)
        result = plugin_links.link_plugin_copies(self.default, self.agent)
        self.assertEqual(result, {"linked": [], "kept": []})
        self.assertTrue(own.is_dir() and not own.is_symlink())
        self.assertTrue(staging.is_dir())

    def test_a_failed_swap_puts_the_copy_back(self):
        self.install(self.default)
        copy = self.clone()
        real_rename = os.rename
        failed = []

        def rename(source, destination):
            if Path(destination) == copy and not failed:  # the link can't take the copy's place
                failed.append(destination)
                raise OSError("disk went away")
            return real_rename(source, destination)

        with mock.patch.object(plugin_links.os, "rename", side_effect=rename):
            result = plugin_links.link_plugin_copies(self.default, self.agent)
        self.assertEqual(result, {"linked": [], "kept": [{"name": "weather", "reason": "error"}]})
        self.assertTrue(copy.is_dir() and not copy.is_symlink())
        self.assertEqual((copy / "plugin.yaml").read_text(), "name: weather\n")
        self.assertEqual(sorted(p.name for p in self.agent.iterdir()),
                         [".install-metadata.json", ".install-metadata.json.lock", "weather"])

    def test_a_tree_too_big_to_compare_is_left_alone(self):
        self.install(self.default, files={f"f{i}.py": "x\n" for i in range(5)})
        self.clone()
        with mock.patch.object(plugin_links, "MAXIMUM_FILES", 3):
            result = plugin_links.link_plugin_copies(self.default, self.agent)
        self.assertEqual(result["kept"], [{"name": "weather", "reason": "too_large"}])

    def test_hermes_package_manager_sees_one_package_once_linked(self):
        try:
            from pm.workspace import _member_key
        except Exception as error:  # pragma: no cover - only without a Hermes checkout on the path
            self.skipTest(f"Hermes' package manager is unavailable: {type(error).__name__}")
        self.install(self.default)
        copy = self.clone()
        self.assertNotEqual(_member_key(copy), _member_key(self.default / "weather"),
                            "A copy is a second workspace member with the same package name")
        plugin_links.link_plugin_copies(self.default, self.agent)
        self.assertEqual(_member_key(copy), _member_key(self.default / "weather"), "A link is the same member")

    def test_after_hermes_own_clone_the_agent_shares_and_its_clones_keep_sharing(self):
        """Hermes' create_profile(clone_from="default"), as the app calls it for New agent and Simple Bot's Create."""
        if not plugin_links.available():
            self.skipTest("this Hermes has no profile helpers")
        from hermes_cli import profiles
        home = self.root / "hermes"
        (home / "profiles").mkdir(parents=True)
        (home / "config.yaml").write_text("model:\n  default: fixture-model\nplugins:\n  enabled:\n    - weather\n")
        self.install(home / "plugins")
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            profiles.create_profile(name="benson", clone_from="default", no_alias=True)
            copy = home / "profiles" / "benson" / "plugins" / "weather"
            self.assertTrue(copy.is_dir() and not copy.is_symlink(), "Hermes' clone really copies the plugin")
            self.assertEqual(plugin_links.link_agent("benson"), {"linked": ["weather"], "kept": []})
            self.assertTrue(copy.is_symlink())
            profiles.create_profile(name="carla", clone_from="benson", no_alias=True)
            self.assertTrue((home / "profiles" / "carla" / "plugins" / "weather").is_symlink(),
                            "An agent cloned from a sharing one keeps the link")
            self.assertEqual(plugin_links.link_every_agent(), {"linked": 0, "kept": 0, "failed": 0})

    def test_only_on_hosts_with_symlinks(self):
        with mock.patch.object(plugin_links.os, "name", "nt"):
            self.assertFalse(plugin_links.available())


if __name__ == "__main__":
    unittest.main()
