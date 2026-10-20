"""Publish the plugin version in plugin.yaml as a GitHub Release, once.

Runs only when the maintainer starts .github/workflows/release.yml. Pull requests
collect on main unreleased; a release PR bumps the version and lists what's in it.
The release is main as of that PR's merge, with its description as the notes, so a
change merged after it waits for the next release. The bighelp app and
`hermes bighelp update` install the latest release.

A release holds only what a host needs (release-files.txt). The script copies those
files from the commit that set the version into a release commit on the `releases`
branch, checks them, and tags that commit. Hosts install the tagged commit.

--prerelease publishes a test release, v<version>-rc.<n>. GitHub never makes a
pre-release the latest release, so the app and the updater don't offer it.
--export FOLDER only builds and checks the release files; it publishes nothing.
"""
import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r'(?m)^version:\s*"?([0-9]{1,6}(?:\.[0-9]{1,6}){1,3})"?\s*$')
# Wherever the module that holds it lives under loopdy_plugin/.
CODE_VERSION = re.compile(r'''(?m)^PLUGIN_VERSION\s*(?::[^=\n]+)?=\s*["']([^"'\n]+)["']''')
MAX_NOTES = 100_000

RELEASE_FILES = "release-files.txt"
RELEASE_BRANCH = "releases"
# The identity that the workflow's token pushes as.
BOT = {"GIT_AUTHOR_NAME": "github-actions[bot]",
       "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
       "GIT_COMMITTER_NAME": "github-actions[bot]",
       "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com"}
# Hermes' plugin scan flags more than 400 files, 10 MB in all or 1 MB in one file.
MAX_FILES = 300
MAX_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
# What the plugin reads on a host. .gitignore keeps the installed checkout clean for the updater.
REQUIRED_FILES = ("plugin.yaml", "__init__.py", "after-install.md", ".gitignore",
                  "loopdy_plugin/__init__.py", "dashboard/manifest.json")
REQUIRED_FOLDERS = ("loopdy_plugin/assets", "skills", "spec")
# Never in a release, at any depth.
DEV_ONLY = re.compile(r"(^|/)(tests?|fixtures|docs|\.github)/|(^|/)(test_[^/]*|[^/]*_test|conftest)\.py$")
ENTRY = re.compile(r"[A-Za-z0-9_.-]+/?")


def manifest_version(text: str) -> str:
    match = VERSION.search(text)
    if not match:
        raise SystemExit("plugin.yaml has no version")
    return match.group(1)


def plugin_version(root: Path = ROOT) -> str:
    return manifest_version((root / "plugin.yaml").read_text(encoding="utf-8"))


def check_dashboard_version(version: str, root: Path = ROOT) -> None:
    """Hermes' dashboard shows dashboard/manifest.json's version, so it must match plugin.yaml."""
    dashboard = json.loads((root / "dashboard" / "manifest.json").read_text(encoding="utf-8")).get("version")
    if dashboard != version:
        raise SystemExit(f"dashboard/manifest.json says {dashboard}, but plugin.yaml says {version}")


def code_versions(root: Path = ROOT) -> set[str]:
    """Every PLUGIN_VERSION written under loopdy_plugin/."""
    return {match.group(1) for path in sorted((root / "loopdy_plugin").rglob("*.py"))
            for match in CODE_VERSION.finditer(path.read_text(encoding="utf-8"))}


def check_code_version(version: str, root: Path = ROOT) -> None:
    """The app reads PLUGIN_VERSION from /native/context, so it must match plugin.yaml."""
    found = code_versions(root)
    if found != {version}:
        raise SystemExit(f"PLUGIN_VERSION says {', '.join(sorted(found)) or 'nothing'}, but plugin.yaml says {version}")


def check_versions(root: Path = ROOT) -> str:
    version = plugin_version(root)
    check_dashboard_version(version, root)
    check_code_version(version, root)
    return version


def version_key(version: str) -> tuple[int, ...]:
    parts = [int(part) for part in version.split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def _git(*args: str, root: Path = ROOT, env: dict | None = None, input: str | None = None) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
                          env=env, input=input).stdout


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


def read_release_files(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """(shipped, left out) top-level entries from release-files.txt. Folders end in "/"."""
    shipped: set[str] = set()
    left_out: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.removeprefix("!")
        if not ENTRY.fullmatch(name) or name.rstrip("/") in {".", ".."}:
            raise SystemExit(f"{RELEASE_FILES} line {number}: {line!r} isn't one top-level file or folder")
        if name in shipped or name in left_out:
            raise SystemExit(f"{RELEASE_FILES} lists {name} twice")
        (left_out if line.startswith("!") else shipped).add(name)
    if not shipped:
        raise SystemExit(f"{RELEASE_FILES} ships nothing")
    return frozenset(shipped), frozenset(left_out)


def release_paths(paths: list[str], shipped: frozenset[str], left_out: frozenset[str]) -> list[str]:
    """The paths a release ships. Every top-level entry must be on one of the two lists."""
    keep, present, unlisted = [], set(), set()
    for path in paths:
        top, slash, _ = path.partition("/")
        entry = top + slash
        present.add(entry)
        if entry in shipped:
            keep.append(path)
        elif entry not in left_out:
            unlisted.add(entry)
    if unlisted:
        raise SystemExit(f"Add {', '.join(sorted(unlisted))} to {RELEASE_FILES}: ship it, or leave it out with \"!\"")
    if missing := shipped - present:
        raise SystemExit(f"{RELEASE_FILES} ships {', '.join(sorted(missing))}, but the source doesn't have it")
    return keep


def build_release_tree(source: str, destination: Path, root: Path = ROOT) -> str:
    """Write the release files of commit `source` into `destination`, and return their Git tree."""
    shipped, left_out = read_release_files((root / RELEASE_FILES).read_text(encoding="utf-8"))
    entries = {}
    for record in _git("ls-tree", "-r", "-z", "--full-tree", source, root=root).split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            mode, _kind, name = meta.split()
            entries[path] = f"{mode} {name}"
    keep = release_paths(list(entries), shipped, left_out)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise SystemExit(f"{destination} isn't empty")
    with tempfile.TemporaryDirectory() as scratch:
        # A separate index, so the checkout and its own index stay as they are.
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git("update-index", "-z", "--index-info", root=root, env=env,
             input="".join(f"{entries[path]}\t{path}\0" for path in keep))
        tree = _git("write-tree", root=root, env=env).strip()
        _git("checkout-index", "--all", f"--prefix={destination.resolve()}/", root=root, env=env)
    return tree


def _top_level_names(module: ast.Module) -> set[str]:
    names = set()
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(target.id for target in targets if isinstance(target, ast.Name))
    return names


def unresolved_imports(tree: Path) -> list[str]:
    """Imports of the plugin's own modules that name a module the release doesn't have."""
    def module(parts: list[str]) -> Path | None:
        folder = tree.joinpath(*parts)
        if (folder / "__init__.py").is_file():
            return folder / "__init__.py"
        if parts and folder.with_name(parts[-1] + ".py").is_file():
            return folder.with_name(parts[-1] + ".py")
        return None

    problems = []
    for path in sorted(tree.rglob("*.py")):
        name = path.relative_to(tree).as_posix()
        try:
            parsed = ast.parse(path.read_text(encoding="utf-8"), filename=name)
        except (SyntaxError, UnicodeDecodeError, ValueError):
            problems.append(f"{name} doesn't parse")
            continue
        package = list(path.relative_to(tree).parts[:-1])
        for node in ast.walk(parsed):
            if isinstance(node, ast.Import):
                targets = [(alias.name.split("."), []) for alias in node.names
                           if alias.name.split(".")[0] == "loopdy_plugin"]
            elif isinstance(node, ast.ImportFrom):
                if node.level > len(package) + 1:
                    problems.append(f"{name}:{node.lineno} imports from outside the plugin")
                    continue
                if node.level:
                    base = package[:len(package) - node.level + 1]
                elif (node.module or "").split(".")[0] == "loopdy_plugin":
                    base = []
                else:
                    continue
                targets = [(base + (node.module.split(".") if node.module else []),
                            [alias.name for alias in node.names if alias.name != "*"])]
            else:
                continue
            for parts, names in targets:
                found = module(parts)
                if found is None:
                    problems.append(f"{name}:{node.lineno} imports {'.'.join(parts) or 'the plugin'}, which is missing")
                elif found.name == "__init__.py":
                    defined = _top_level_names(ast.parse(found.read_text(encoding="utf-8")))
                    problems += [f"{name}:{node.lineno} imports {'.'.join(parts + [item])}, which is missing"
                                 for item in names if module(parts + [item]) is None and item not in defined]
    return problems


def check_release_tree(tree: Path, version: str) -> tuple[int, int]:
    """Stop unless `tree` has what the plugin reads on a host and nothing else. Returns (files, bytes)."""
    files = sorted(path for path in tree.rglob("*") if path.is_file() or path.is_symlink())
    names = [path.relative_to(tree).as_posix() for path in files]
    sizes = {name: path.lstat().st_size for name, path in zip(names, files)}
    problems = [f"{name} is missing" for name in REQUIRED_FILES if not (tree / name).is_file()]
    problems += [f"{name}/ is missing or empty" for name in REQUIRED_FOLDERS
                 if not any(item.startswith(name + "/") for item in names)]
    skills = sorted((tree / "skills").iterdir()) if (tree / "skills").is_dir() else []
    problems += [f"skills/{skill.name}/ has no SKILL.md" for skill in skills
                 if skill.is_dir() and not (skill / "SKILL.md").is_file()]
    problems += [f"{name} is for development only" for name in names if DEV_ONLY.search(name)]
    problems += [f"{name} is {size // 1024} KB, over {MAX_FILE_BYTES // 1024} KB"
                 for name, size in sizes.items() if size > MAX_FILE_BYTES]
    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} files, over {MAX_FILES}")
    if sum(sizes.values()) > MAX_BYTES:
        problems.append(f"{sum(sizes.values()) // 1024} KB in all, over {MAX_BYTES // 1024} KB")
    try:
        if plugin_version(tree) != version:
            problems.append(f"plugin.yaml doesn't say {version}")
        check_dashboard_version(version, tree)
        check_code_version(version, tree)
        api = json.loads((tree / "dashboard" / "manifest.json").read_text(encoding="utf-8")).get("api")
        if api and not (tree / "dashboard" / api).is_file():
            problems.append(f"dashboard/{api} is missing")
    except (SystemExit, OSError, ValueError) as error:
        problems.append(str(error))
    problems += unresolved_imports(tree)
    if not problems:
        # A fresh interpreter that sees only the release files: no site path, no work folder.
        with tempfile.TemporaryDirectory() as empty:
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c",
                 "import sys; sys.path.insert(0, sys.argv[1]); import loopdy_plugin; print(loopdy_plugin.__file__)",
                 str(tree.resolve())],
                cwd=empty, capture_output=True, text=True, timeout=120)
        loaded = Path(result.stdout.strip() or ".").resolve()
        if result.returncode != 0:
            lines = result.stderr.strip().splitlines()
            problems.append(f"loopdy_plugin doesn't import: {lines[-1] if lines else result.returncode}")
        elif loaded != (tree / "loopdy_plugin" / "__init__.py").resolve():
            problems.append(f"loopdy_plugin imported from {loaded}, not from the release files")
    if problems:
        raise SystemExit("The release files aren't right:\n- " + "\n- ".join(problems))
    return len(files), sum(sizes.values())


def release_message(version: str, source: str) -> str:
    # No pre-release number: a release can then reuse its test release's commit.
    return (f"bighelp {version} from {source}\n\n"
            f"Only the files a host needs ({RELEASE_FILES}), from {source} on main.\n")


def release_commit(version: str, source: str, tree: str, parent: str | None, root: Path = ROOT) -> str:
    """The commit to tag: the branch tip when it is this same build, else a new commit on top of it."""
    message = release_message(version, source)
    if parent and (_git("rev-parse", f"{parent}^{{tree}}", root=root).strip() == tree
                   and _git("log", "-1", "--format=%B", parent, root=root).strip() == message.strip()):
        return parent
    return _git("commit-tree", "--no-gpg-sign", tree, *(["-p", parent] if parent else []), "-F", "-",
                root=root, env={**os.environ, **BOT}, input=message).strip()


def remote_refs(root: Path = ROOT) -> dict[str, str]:
    """Branches and tags on origin, each with its commit."""
    refs: dict[str, str] = {}
    for line in _git("ls-remote", "origin", f"refs/heads/{RELEASE_BRANCH}", "refs/tags/*", root=root).splitlines():
        commit, _, name = line.partition("\t")
        if name.endswith("^{}"):
            refs[name.removesuffix("^{}")] = commit
        else:
            refs.setdefault(name, commit)
    return refs


def next_prerelease_tag(version: str, refs: dict[str, str]) -> str:
    pattern = re.compile(rf"refs/tags/v{re.escape(version)}-rc\.([0-9]+)")
    numbers = [int(match.group(1)) for name in refs if (match := pattern.fullmatch(name))]
    return f"v{version}-rc.{max(numbers, default=0) + 1}"


def release_flags(prerelease: bool) -> list[str]:
    # A pre-release is never the latest release, so releases/latest stays on the last real one.
    return ["--prerelease", "--latest=false"] if prerelease else ["--latest"]


def prerelease_notes(repository: str, commit: str) -> str:
    return ("**Test release.** The bighelp app and `hermes bighelp update` don't offer it. "
            "To install it on a test host:\n\n"
            f"```\nhermes plugins install https://github.com/{repository} --ref {commit} --force\n```\n\n")


def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"gh {args[0]} {args[1]} failed: {result.stderr.strip() or result.returncode}")
    return result


def _latest_tag(repository: str) -> str | None:
    latest = _gh("release", "view", "--repo", repository, "--json", "tagName", check=False)
    return json.loads(latest.stdout)["tagName"] if latest.returncode == 0 else None


def publish(repository: str, prerelease: bool = False, root: Path = ROOT) -> int:
    version = check_versions(root)
    stable = f"v{version}"
    if _gh("release", "view", stable, "--repo", repository, check=False).returncode == 0:
        if prerelease:
            print(f"{stable} is already released. A test release comes before its release.", file=sys.stderr)
            return 1
        print(f"{stable} is already released; nothing to publish.")
        return 0
    if latest := _latest_tag(repository):
        current = latest.removeprefix("v")
        if version_key(version) <= version_key(current):
            print(f"plugin.yaml says {version}, which isn't newer than the latest release {current}.",
                  file=sys.stderr)
            return 1
    source = version_commit(root)
    if manifest_version(_git("show", f"{source}:plugin.yaml", root=root)) != version:
        raise SystemExit(f"The commit that set the version doesn't say {version}")
    refs = remote_refs(root)
    tag = next_prerelease_tag(version, refs) if prerelease else stable
    with tempfile.TemporaryDirectory() as scratch:
        tree = build_release_tree(source, Path(scratch) / "release", root)
        files, size = check_release_tree(Path(scratch) / "release", version)
    tip = refs.get(f"refs/heads/{RELEASE_BRANCH}")
    if tip:
        _git("fetch", "--quiet", "--no-tags", "origin", f"refs/heads/{RELEASE_BRANCH}", root=root)
    commit = release_commit(version, source, tree, tip, root)
    if refs.get(f"refs/tags/{tag}", commit) != commit:
        raise SystemExit(f"The tag {tag} is already on {refs[f'refs/tags/{tag}']}, not on the release commit {commit}")
    if commit != tip:
        _git("push", "--quiet", "origin", f"{commit}:refs/heads/{RELEASE_BRANCH}", root=root)
    pulls = json.loads(_gh("api", f"repos/{repository}/commits/{source}/pulls").stdout or "[]")
    pull = next((item for item in pulls if item.get("merged_at")), None)
    notes = release_notes(pull, _git("log", "-1", "--format=%B", source, root=root))
    if prerelease:
        notes = prerelease_notes(repository, commit) + notes
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as file:
        file.write(notes[:MAX_NOTES])
    try:
        _gh("release", "create", tag, "--repo", repository, "--target", commit,
            "--title", f"bighelp plugin {tag.removeprefix('v')}", "--notes-file", file.name,
            *release_flags(prerelease))
    finally:
        os.unlink(file.name)
    if remote_refs(root).get(f"refs/tags/{tag}") != commit:
        raise SystemExit(f"{tag} isn't on the release commit {commit}")
    latest = _latest_tag(repository)
    if prerelease and latest == tag:
        raise SystemExit(f"The test release {tag} became the latest release")
    if not prerelease and latest != tag:
        raise SystemExit(f"{tag} isn't the latest release; {latest} is")
    print(f"Published {tag} at {commit}: {files} files, {size // 1024} KB, from {source}.")
    return 0


def export(folder: Path, source: str | None, root: Path = ROOT) -> int:
    commit = _git("rev-parse", "--verify", f"{source or version_commit(root)}^{{commit}}", root=root).strip()
    version = manifest_version(_git("show", f"{commit}:plugin.yaml", root=root))
    tree = build_release_tree(commit, folder, root)
    files, size = check_release_tree(folder, version)
    print(f"{folder}: bighelp {version}, {files} files, {size // 1024} KB, tree {tree}, from {commit}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prerelease", action="store_true",
                        help="publish a test release (v<version>-rc.<n>) that the app and updater don't offer")
    parser.add_argument("--export", type=Path, metavar="FOLDER",
                        help="only build and check the release files in FOLDER; publish nothing")
    parser.add_argument("--source", metavar="COMMIT",
                        help="with --export: the commit to build (default: the commit that set the version)")
    args = parser.parse_args(argv)
    if args.export:
        return export(args.export, args.source)
    if args.source:
        parser.error("--source works only with --export")
    return publish(os.environ["GITHUB_REPOSITORY"], args.prerelease)


if __name__ == "__main__":
    sys.exit(main())
