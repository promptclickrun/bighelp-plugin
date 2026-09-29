"""Publish the plugin version on main as a GitHub Release, once.

Runs from .github/workflows/release.yml after every push to main. The bighelp app
and `hermes bighelp update` install the latest release, so a merge that bumps the
version reaches hosts and one that doesn't (its release already exists) stops here.
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


def plugin_version(root: Path = ROOT) -> str:
    match = VERSION.search((root / "plugin.yaml").read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("plugin.yaml has no version")
    return match.group(1)


def version_key(version: str) -> tuple[int, ...]:
    parts = [int(part) for part in version.split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def release_notes(pull: dict | None, commit_message: str) -> str:
    """The merged pull request's title and description, else the commit message."""
    if pull and pull.get("title"):
        notes = f"## {pull['title']}\n\n{(pull.get('body') or '').strip()}".strip()
    else:
        notes = commit_message.strip()
    return notes[:MAX_NOTES]


def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], check=check, capture_output=True, text=True)


def main() -> int:
    repository, commit = os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_SHA"]
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
    pulls = json.loads(_gh("api", f"repos/{repository}/commits/{commit}/pulls").stdout or "[]")
    pull = next((item for item in pulls if item.get("merged_at")), None)
    message = subprocess.run(["git", "log", "-1", "--format=%B", commit],
                             check=True, capture_output=True, text=True).stdout
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as notes:
        notes.write(release_notes(pull, message))
    try:
        _gh("release", "create", tag, "--repo", repository, "--target", commit,
            "--title", f"bighelp plugin {version}", "--notes-file", notes.name, "--latest")
    finally:
        os.unlink(notes.name)
    print(f"Published {tag} at {commit}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
