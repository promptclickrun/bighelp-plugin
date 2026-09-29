"""Updates follow the latest published release, never a moving branch."""
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from loopdy_plugin import plugin_update as api
with patch.dict(sys.modules, {"plugin_update": api}):
    worker = importlib.import_module("loopdy_plugin.plugin_update_worker")

COMMIT = "a" * 40
TAG_OBJECT = "b" * 40


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _release(**changes):
    body = {"tag_name": "v3.0.0", "draft": False, "prerelease": False, **changes}
    return _Response(json.dumps(body).encode())


def _ls_remote(stdout, returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class LatestReleaseTests(unittest.TestCase):
    def test_the_release_commit_comes_from_git_and_prefers_the_peeled_tag(self):
        calls = []

        def run(command, **_kwargs):
            calls.append(command)
            return _ls_remote(f"{TAG_OBJECT}\trefs/tags/v3.0.0\n{COMMIT}\trefs/tags/v3.0.0^{{}}\n")

        with patch.object(worker.urllib.request, "urlopen", return_value=_release()) as opened, \
                patch.object(worker, "_run_capture", side_effect=run):
            self.assertEqual(worker._resolve_latest_release(Path("/tmp")), (COMMIT, "3.0.0"))
        self.assertEqual(opened.call_args.args[0].full_url, api.RELEASES_URL)
        self.assertEqual(calls[0][:3], ["git", "ls-remote", api.SOURCE_URL])

    def test_a_lightweight_tag_points_straight_at_its_commit(self):
        with patch.object(worker.urllib.request, "urlopen", return_value=_release()), \
                patch.object(worker, "_run_capture", return_value=_ls_remote(f"{COMMIT}\trefs/tags/v3.0.0\n")):
            self.assertEqual(worker._resolve_latest_release(Path("/tmp"))[0], COMMIT)

    def test_drafts_prereleases_and_odd_tags_are_refused(self):
        for release in (_release(draft=True), _release(prerelease=True), _release(tag_name="latest"),
                        _release(tag_name="v3.0.0/../main"), _Response(b"not json")):
            with self.subTest(release=release.getvalue()[:40]), \
                    patch.object(worker.urllib.request, "urlopen", return_value=release):
                with self.assertRaises(worker.UpdateFailed):
                    worker._latest_release_tag()

    def test_an_unreachable_github_is_a_plain_failure(self):
        with patch.object(worker.urllib.request, "urlopen", side_effect=OSError("offline")):
            with self.assertRaises(worker.UpdateFailed):
                worker._latest_release_tag()

    def test_a_tag_without_a_commit_is_refused(self):
        with patch.object(worker.urllib.request, "urlopen", return_value=_release()), \
                patch.object(worker, "_run_capture", return_value=_ls_remote("")):
            with self.assertRaises(worker.UpdateFailed):
                worker._resolve_latest_release(Path("/tmp"))

    def test_installed_version_reads_the_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin.yaml").write_text('name: loopdy\nversion: "3.1.0"\n', encoding="utf-8")
            self.assertEqual(worker._installed_version(root), "3.1.0")
            self.assertIsNone(worker._installed_version(root / "missing"))
        self.assertGreater(worker._version_key("3.0.1"), worker._version_key("3.0"))
        self.assertEqual(worker._version_key("3.0"), worker._version_key("3.0.0"))


if __name__ == "__main__":
    unittest.main()
