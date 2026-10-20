"""The release workflow publishes exactly the version in plugin.yaml, with only the files hosts need."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import publish_release  # noqa: E402

RELEASE_FILES = """\
# Shipped
plugin.yaml
__init__.py
.gitignore
after-install.md
loopdy_plugin/
dashboard/
skills/
spec/
# Not shipped
!tests/
!docs/
!fixtures/
!release-files.txt
"""


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                           *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")


def plugin_files(version: str) -> dict[str, str]:
    return {
        "plugin.yaml": f"manifest_version: 1\nname: loopdy\nversion: \"{version}\"\n",
        "__init__.py": "from .loopdy_plugin.registration import register\n",
        ".gitignore": "__pycache__/\n",
        "after-install.md": "# Installed\n",
        "loopdy_plugin/__init__.py": "from .tools import register\n",
        "loopdy_plugin/tools.py": "from .version import PLUGIN_VERSION\n\n\ndef register(ctx):\n    pass\n",
        "loopdy_plugin/registration.py": "from . import tools\nfrom .tools import register\n",
        "loopdy_plugin/version.py": f"PLUGIN_VERSION = \"{version}\"\n",
        "loopdy_plugin/assets/picture.png": "png\n",
        "dashboard/manifest.json": json.dumps({"name": "loopdy", "version": version, "api": "plugin_api.py"}),
        "dashboard/plugin_api.py": "from loopdy_plugin.version import PLUGIN_VERSION\n",
        "skills/bighelp/SKILL.md": "# bighelp\n",
        "spec/card.schema.json": "{}\n",
        "tests/test_tools.py": "import unittest\n",
        "docs/TOOLS.md": "# Tools\n",
        "fixtures/vector.json": "{}\n",
        "release-files.txt": RELEASE_FILES,
    }


class FakeGitHub:
    """`gh` as the publish script uses it, backed by a bare repository as origin.

    Like GitHub, a release made on a new tag creates the tag at its target, and the latest
    release is the newest one marked latest, never a pre-release.
    """

    def __init__(self, origin: Path):
        self.origin = origin
        self.releases: dict[str, dict] = {}
        self.latest: str | None = None

    def __call__(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        def answer(code: int = 0, out: str = "") -> subprocess.CompletedProcess:
            if check and code:
                raise SystemExit(f"gh {args[0]} {args[1]} failed")
            return subprocess.CompletedProcess(["gh", *args], code, out, "")

        if args[:2] == ("release", "view"):
            if args[2] == "--repo":
                return answer(0, json.dumps({"tagName": self.latest})) if self.latest else answer(1)
            return answer(0 if args[2] in self.releases else 1)
        if args[0] == "api" and args[1].endswith("/pulls"):
            return answer(0, json.dumps([{"title": "1.1.0: a test", "body": "What changed.", "merged_at": "now"}]))
        if args[:2] == ("release", "create"):
            tag, options = args[2], list(args[3:])
            target = options[options.index("--target") + 1]
            git(self.origin, "update-ref", f"refs/tags/{tag}", target)
            notes = Path(options[options.index("--notes-file") + 1]).read_text(encoding="utf-8")
            self.releases[tag] = {"target": target, "prerelease": "--prerelease" in options, "notes": notes}
            if "--latest" in options and "--prerelease" not in options:
                self.latest = tag
            return answer()
        raise AssertionError(f"Unexpected gh call: {args}")


class PublishReleaseTests(unittest.TestCase):
    def test_it_publishes_the_manifest_version(self):
        self.assertEqual({publish_release.plugin_version()}, publish_release.code_versions())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin.yaml").write_text("name: loopdy\nversion: 3.2.1\n", encoding="utf-8")
            self.assertEqual(publish_release.plugin_version(root), "3.2.1")
            (root / "plugin.yaml").write_text("name: loopdy\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                publish_release.plugin_version(root)

    def test_it_refuses_a_stale_dashboard_version(self):
        publish_release.check_dashboard_version(publish_release.plugin_version())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dashboard").mkdir()
            (root / "dashboard" / "manifest.json").write_text('{"version": "2.8.0"}', encoding="utf-8")
            with self.assertRaises(SystemExit):
                publish_release.check_dashboard_version("3.1.0", root)
            publish_release.check_dashboard_version("2.8.0", root)

    def test_it_finds_plugin_version_in_any_module(self):
        publish_release.check_code_version(publish_release.plugin_version())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write(root, {"loopdy_plugin/link_contracts.py": "X = 1\n",
                         "loopdy_plugin/version.py": 'PLUGIN_VERSION: str = "3.2.1"  # bumped by the release PR\n'})
            publish_release.check_code_version("3.2.1", root)
            with self.assertRaises(SystemExit):
                publish_release.check_code_version("3.2.2", root)
            write(root, {"loopdy_plugin/old.py": 'PLUGIN_VERSION = "3.2.0"\n'})
            with self.assertRaises(SystemExit):
                publish_release.check_code_version("3.2.1", root)
            (root / "loopdy_plugin" / "old.py").unlink()
            (root / "loopdy_plugin" / "version.py").unlink()
            with self.assertRaises(SystemExit):
                publish_release.check_code_version("3.2.1", root)

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
                write(root, {path: text})
                git(root, "add", path)
                git(root, "commit", "-q", "-m", path)
                return git(root, "rev-parse", "HEAD")

            git(root, "init", "-q")
            commit("plugin.yaml", "manifest_version: 1\nname: loopdy\nversion: 1.0.0\n")
            commit("fix.py", "one\n")
            release = commit("plugin.yaml", "manifest_version: 1\nname: loopdy\nversion: 1.1.0\n")
            commit("fix.py", "two\n")
            commit("plugin.yaml", "manifest_version: 2\nname: loopdy\nversion: 1.1.0\n")
            self.assertEqual(publish_release.version_commit(root), release)

    def test_versions_compare_numerically(self):
        self.assertGreater(publish_release.version_key("3.10.0"), publish_release.version_key("3.9.9"))
        self.assertEqual(publish_release.version_key("3.0"), publish_release.version_key("3.0.0"))

    def test_release_files_are_top_level_names(self):
        shipped, left_out = publish_release.read_release_files(RELEASE_FILES)
        self.assertIn("loopdy_plugin/", shipped)
        self.assertIn("plugin.yaml", shipped)
        self.assertIn("tests/", left_out)
        for wrong in ("loopdy_plugin/assets/", "*.py", "../x", "!", "plugin.yaml\n!plugin.yaml", "# nothing"):
            with self.subTest(wrong=wrong), self.assertRaises(SystemExit):
                publish_release.read_release_files(wrong)

    def test_every_top_level_entry_is_shipped_or_left_out(self):
        shipped, left_out = publish_release.read_release_files(RELEASE_FILES)
        paths = ["plugin.yaml", "loopdy_plugin/a.py", "tests/test_a.py", "dashboard/manifest.json"]
        kept = publish_release.release_paths(paths, shipped - {"__init__.py", ".gitignore", "after-install.md",
                                                               "skills/", "spec/"}, left_out)
        self.assertEqual(kept, ["plugin.yaml", "loopdy_plugin/a.py", "dashboard/manifest.json"])
        # Something new must be put on a list before it can ship or stay behind.
        with self.assertRaisesRegex(SystemExit, "Add templates/ to"):
            publish_release.release_paths(paths + ["templates/a.md"], shipped, left_out)
        # A file and a folder of the same name are different entries.
        with self.assertRaisesRegex(SystemExit, "Add skills to"):
            publish_release.release_paths(paths + ["skills"], shipped, left_out)
        # A shipped entry that's gone (a rename, say) stops the release too.
        with self.assertRaisesRegex(SystemExit, "spec/"):
            publish_release.release_paths(paths, shipped, left_out)

    def test_this_repository_ships_only_runtime_files(self):
        shipped, left_out = publish_release.read_release_files(
            (ROOT / publish_release.RELEASE_FILES).read_text(encoding="utf-8"))
        kept = publish_release.release_paths(git(ROOT, "ls-files", "-z").strip("\0").split("\0"), shipped, left_out)
        tops = {path.split("/")[0] for path in kept}
        self.assertLessEqual({"plugin.yaml", "__init__.py", "loopdy_plugin", "dashboard", "skills", "spec",
                              "after-install.md", ".gitignore", "LICENSE"}, tops)
        self.assertFalse(tops & {"tests", "fixtures", "docs", "scripts", ".github", "AGENTS.md", "release-files.txt"})
        self.assertLessEqual(len(kept), publish_release.MAX_FILES)

    def test_the_release_tree_has_only_the_listed_files_and_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            root, out = Path(directory) / "repo", Path(directory) / "release"
            root.mkdir()
            git(root, "init", "-q")
            write(root, plugin_files("1.0.0"))
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "1.0.0")
            tree = publish_release.build_release_tree("HEAD", out, root)
            self.assertEqual(publish_release.check_release_tree(out, "1.0.0")[0], 13)
            self.assertFalse((out / "tests").exists() or (out / "docs").exists() or (out / "fixtures").exists())
            self.assertFalse(list(out.rglob("__pycache__")))
            self.assertEqual(git(root, "status", "--porcelain"), "")
            self.assertIn("tests", git(root, "ls-tree", "--name-only", "HEAD"))
            self.assertNotIn("tests", git(root, "ls-tree", "--name-only", tree))

    def test_a_broken_release_tree_stops_the_release(self):
        breakages = {
            "a required file is missing": lambda out: (out / "after-install.md").unlink(),
            "a test sits in a shipped folder": lambda out: write(out, {"loopdy_plugin/tests/test_a.py": "x = 1\n"}),
            "a module is missing": lambda out: (out / "loopdy_plugin" / "version.py").unlink(),
            "an import names a missing module": lambda out: write(out, {"loopdy_plugin/b.py": "from .gone import x\n"}),
            "the package doesn't import": lambda out: write(out, {"loopdy_plugin/tools.py": "raise ImportError\n"}),
            "a skill has no SKILL.md": lambda out: write(out, {"skills/empty/notes.md": "x\n"}),
            "versions disagree": lambda out: write(out, {"loopdy_plugin/version.py": 'PLUGIN_VERSION = "0.9.0"\n'}),
            "a file is too big": lambda out: write(out, {"spec/big.json": "x" * (publish_release.MAX_FILE_BYTES + 1)}),
        }
        for reason, breakage in breakages.items():
            with self.subTest(reason), tempfile.TemporaryDirectory() as directory:
                out = Path(directory)
                write(out, {name: text for name, text in plugin_files("1.0.0").items()
                            if name.split("/")[0] not in {"tests", "docs", "fixtures", "release-files.txt"}})
                publish_release.check_release_tree(out, "1.0.0")
                breakage(out)
                with self.assertRaisesRegex(SystemExit, "aren't right"):
                    publish_release.check_release_tree(out, "1.0.0")
        with tempfile.TemporaryDirectory() as directory, patch.object(publish_release, "MAX_FILES", 3):
            write(Path(directory), plugin_files("1.0.0"))
            with self.assertRaisesRegex(SystemExit, "files, over 3"):
                publish_release.check_release_tree(Path(directory), "1.0.0")

    def test_test_releases_count_up_per_version(self):
        refs = {"refs/tags/v1.1.0": "a", "refs/tags/v1.2.0-rc.1": "b", "refs/tags/v1.2.0-rc.3": "c",
                "refs/tags/v1.2.0-rc.x": "d", "refs/tags/v1.2.00-rc.9": "e"}
        self.assertEqual(publish_release.next_prerelease_tag("1.2.0", refs), "v1.2.0-rc.4")
        self.assertEqual(publish_release.next_prerelease_tag("1.3.0", refs), "v1.3.0-rc.1")
        self.assertEqual(publish_release.release_flags(True), ["--prerelease", "--latest=false"])
        self.assertEqual(publish_release.release_flags(False), ["--latest"])

    def test_releases_and_test_releases_from_start_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            origin, root = Path(directory) / "origin.git", Path(directory) / "repo"
            git(Path(directory), "init", "-q", "--bare", str(origin))
            git(Path(directory), "clone", "-q", str(origin), str(root))
            github = FakeGitHub(origin)

            def merge(version: str, extra: dict[str, str] | None = None) -> str:
                write(root, {**plugin_files(version), **(extra or {})})
                git(root, "add", "-A")
                git(root, "commit", "-q", "-m", f"{version}: a release PR")
                git(root, "push", "-q", "origin", "HEAD:refs/heads/main")
                return git(root, "rev-parse", "HEAD")

            def publish(prerelease: bool = False) -> int:
                with patch.object(publish_release, "_gh", github), \
                        patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull}):
                    return publish_release.publish("owner/plugin", prerelease, root)

            def tagged(tag: str) -> str:
                return git(origin, "rev-parse", f"refs/tags/{tag}^{{commit}}")

            first = merge("1.0.0")
            self.assertEqual(publish(), 0)
            release = tagged("v1.0.0")
            self.assertEqual(github.latest, "v1.0.0")
            self.assertEqual(git(origin, "rev-parse", "refs/heads/releases"), release)
            self.assertEqual(git(origin, "log", "-1", "--format=%P", release), "")
            self.assertEqual(git(origin, "log", "-1", "--format=%s|%an", release),
                             f"bighelp 1.0.0 from {first}|github-actions[bot]")
            files = git(origin, "ls-tree", "-r", "--name-only", release).splitlines()
            self.assertIn("loopdy_plugin/version.py", files)
            self.assertFalse([name for name in files if name.split("/")[0] in {"tests", "docs", "fixtures"}])
            self.assertEqual(publish(), 0)  # Already released: nothing happens.
            self.assertEqual(len(github.releases), 1)

            # A later merge that doesn't bump the version doesn't change what the release is built from.
            source = merge("1.1.0")
            merge("1.1.0", {"loopdy_plugin/later.py": "x = 1\n"})
            self.assertEqual(publish(prerelease=True), 0)
            candidate = tagged("v1.1.0-rc.1")
            self.assertTrue(github.releases["v1.1.0-rc.1"]["prerelease"])
            self.assertIn(f"--ref {candidate}", github.releases["v1.1.0-rc.1"]["notes"])
            self.assertEqual(github.latest, "v1.0.0")
            self.assertEqual(git(origin, "log", "-1", "--format=%P", candidate), release)
            self.assertEqual(git(origin, "log", "-1", "--format=%s", candidate), f"bighelp 1.1.0 from {source}")
            self.assertNotIn("loopdy_plugin/later.py", git(origin, "ls-tree", "-r", "--name-only", candidate))

            # Another test release counts up; the release then tags the same tested commit.
            self.assertEqual(publish(prerelease=True), 0)
            self.assertEqual(tagged("v1.1.0-rc.2"), candidate)
            self.assertEqual(publish(), 0)
            self.assertEqual(tagged("v1.1.0"), candidate)
            self.assertEqual(github.latest, "v1.1.0")
            self.assertFalse(github.releases["v1.1.0"]["prerelease"])
            self.assertNotIn("Test release", github.releases["v1.1.0"]["notes"])
            self.assertEqual(git(origin, "rev-parse", "refs/heads/releases"), candidate)

            # No test release of a released version, and nothing older than the latest release.
            self.assertEqual(publish(prerelease=True), 1)
            merge("1.0.5")
            self.assertEqual(publish(), 1)
            self.assertEqual(publish(prerelease=True), 1)
            self.assertEqual(len(github.releases), 4)

    def test_a_tag_on_another_commit_stops_the_release(self):
        with tempfile.TemporaryDirectory() as directory:
            origin, root = Path(directory) / "origin.git", Path(directory) / "repo"
            git(Path(directory), "init", "-q", "--bare", str(origin))
            git(Path(directory), "clone", "-q", str(origin), str(root))
            write(root, plugin_files("1.0.0"))
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "1.0.0")
            git(root, "push", "-q", "origin", "HEAD:refs/heads/main", "HEAD:refs/tags/v1.0.0")
            with patch.object(publish_release, "_gh", FakeGitHub(origin)), \
                    self.assertRaisesRegex(SystemExit, "already on"):
                publish_release.publish("owner/plugin", False, root)
            self.assertNotIn("releases", git(origin, "branch", "--list", "releases"))


if __name__ == "__main__":
    unittest.main()
