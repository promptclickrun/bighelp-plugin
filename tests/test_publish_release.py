"""The release workflow publishes exactly the version in plugin.yaml."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_release  # noqa: E402

from loopdy_plugin.link_contracts import PLUGIN_VERSION  # noqa: E402


class PublishReleaseTests(unittest.TestCase):
    def test_it_publishes_the_manifest_version(self):
        self.assertEqual(publish_release.plugin_version(), PLUGIN_VERSION)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin.yaml").write_text("name: loopdy\nversion: 3.2.1\n", encoding="utf-8")
            self.assertEqual(publish_release.plugin_version(root), "3.2.1")
            (root / "plugin.yaml").write_text("name: loopdy\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                publish_release.plugin_version(root)

    def test_notes_come_from_the_release_pull_request(self):
        notes = publish_release.release_notes({"title": "3.0.0: bighelp names", "body": "What changed."}, "commit")
        self.assertEqual(notes, "## 3.0.0: bighelp names\n\nWhat changed.")
        self.assertEqual(publish_release.release_notes(None, "Direct push\n"), "Direct push")
        self.assertLessEqual(len(publish_release.release_notes({"title": "t", "body": "x" * 200_000}, "")),
                             publish_release.MAX_NOTES)

    def test_the_release_is_the_commit_that_set_the_version(self):
        # Later merges wait for the next release; so does the release PR's own description.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def commit(path: str, text: str) -> str:
                (root / path).write_text(text, encoding="utf-8")
                subprocess.run(["git", "add", path], cwd=root, check=True)
                subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                                "commit", "-q", "-m", path], cwd=root, check=True)
                return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                                      capture_output=True, text=True).stdout.strip()

            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            commit("plugin.yaml", "manifest_version: 1\nname: loopdy\nversion: 1.0.0\n")
            commit("fix.py", "one\n")
            release = commit("plugin.yaml", "manifest_version: 1\nname: loopdy\nversion: 1.1.0\n")
            commit("fix.py", "two\n")
            commit("plugin.yaml", "manifest_version: 2\nname: loopdy\nversion: 1.1.0\n")
            self.assertEqual(publish_release.version_commit(root), release)

    def test_versions_compare_numerically(self):
        self.assertGreater(publish_release.version_key("3.10.0"), publish_release.version_key("3.9.9"))
        self.assertEqual(publish_release.version_key("3.0"), publish_release.version_key("3.0.0"))


if __name__ == "__main__":
    unittest.main()
