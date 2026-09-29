"""Publish the plugin version in plugin.yaml as a GitHub Release, once.

Runs only when the maintainer starts .github/workflows/release.yml. Pull requests
collect on main unreleased; a release PR bumps the version and lists what's in it.
The release is main as of that PR's merge, with its description as the notes, so a
change merged after it waits for the next release. The bighelp app and
`hermes bighelp update` install the latest release.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r'(?m)^version:\s*"?([0-9]{1,6}(?:\.[0-9]{1,6}){1,3})"?\s*$')
MAX_NOTES = 100_000


def manifest_version(text: str) -> str:
    match = VERSION.search(text)
    if not match:
        raise SystemExit("plugin.yaml has no version")
    return match.group(1)


def plugin_version(root: Path = ROOT) -> str:
    return manifest_version((root / "plugin.yaml").read_text(encoding="utf-8"))


def version_key(version: str) -> tuple[int, ...]:
    parts = [int(part) for part in version.split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def _git(*args: str, root: Path = ROOT) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def version_commit(root: Path = ROOT) -> str:
    """The last commit that changed plugin.yaml's version: the release PR's merge."""
    commit = _git("log", "-1", "--format=%H", "-G", "^version:", "--", "plugin.yaml", root=root).strip()
    if not commit:
        raise SystemExit("No commit sets the plugin version")
    return commit


def release_notes(pull: dict | None, commit_message: str) -> str:
    """The release pull request's title and description, else the commit message."""
    if pull and pull.get("title"):
        notes = f"## {pull['title']}\n\n{(pull.get('body') or '').strip()}".strip()
    else:
        notes = commit_message.strip()
    return notes[:MAX_NOTES]


def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"gh {args[0]} {args[1]} failed: {result.stderr.strip() or result.returncode}")
    return result


def main() -> int:
    repository = os.environ["GITHUB_REPOSITORY"]
    version = plugin_version()
    tag = f"v{version}"
    if _gh("release", "view", tag, "--repo", repository, check=False).returncode == 0:
        print(f"{tag} is already released; nothing to publish.")
        return 0
    latest = _gh("release", "view", "--repo", repository, "--json", "tagName", check=False)
    if latest.returncode == 0:
        current = json.loads(latest.stdout)["tagName"].removeprefix("v")
        if version_key(version) <= version_key(current):
            print(f"plugin.yaml says {version}, which isn't newer than the latest release {current}.",
                  file=sys.stderr)
            return 1
    commit = version_commit()
    if manifest_version(_git("show", f"{commit}:plugin.yaml")) != version:
        raise SystemExit(f"The commit that set the version doesn't say {version}")
    pulls = json.loads(_gh("api", f"repos/{repository}/commits/{commit}/pulls").stdout or "[]")
    pull = next((item for item in pulls if item.get("merged_at")), None)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as notes:
        notes.write(release_notes(pull, _git("log", "-1", "--format=%B", commit)))
    try:
        _gh("release", "create", tag, "--repo", repository, "--target", commit,
            "--title", f"bighelp plugin {version}", "--notes-file", notes.name, "--latest")
    finally:
        os.unlink(notes.name)
    print(f"Published {tag} at {commit}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
