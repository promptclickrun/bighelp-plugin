"""The release workflow publishes exactly the version in plugin.yaml."""
from pathlib import Path
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

    def test_notes_come_from_the_merged_pull_request(self):
        notes = publish_release.release_notes({"title": "3.0.0: bighelp names", "body": "What changed."}, "commit")
        self.assertEqual(notes, "## 3.0.0: bighelp names\n\nWhat changed.")
        self.assertEqual(publish_release.release_notes(None, "Direct push\n"), "Direct push")
        self.assertLessEqual(len(publish_release.release_notes({"title": "t", "body": "x" * 200_000}, "")),
                             publish_release.MAX_NOTES)

    def test_versions_compare_numerically(self):
        self.assertGreater(publish_release.version_key("3.10.0"), publish_release.version_key("3.9.9"))
        self.assertEqual(publish_release.version_key("3.0"), publish_release.version_key("3.0.0"))


if __name__ == "__main__":
    unittest.main()
