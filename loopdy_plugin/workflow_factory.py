"""Dev Factory: the git and GitHub steps of coding workflows (schemaVersion 3, docs/DEV_FACTORY.md).

Standard library only, on purpose. The coordinator imports it, and the pull request step runs this file as its
own process in the project folder (`python -I workflow_factory.py pull-request <attempt folder>`), where a stray
`json.py` in the project must never stand in for the standard library.

Every call is a fixed argv (never a shell) with a timeout. Preparing a worktree and reading changes use the
person's own git setup (hooks, LFS) like a terminal would, with prompts turned off. The pull request step also
uses their git and GitHub CLI sign-ins, which workspace_git.py turns off on purpose.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Mapping


GIT_SECONDS = 60
WORKTREE_SECONDS = 180
PUSH_SECONDS = 180
GH_SECONDS = 90
# Hermes keeps worktrees in <repo>/.worktrees/. The main checkout sees that folder as untracked: it is never
# part of a project's changes or a commit.
WORKTREES = ".worktrees"
EXCLUDE_WORKTREES = (".", f":(exclude){WORKTREES}")
MAX_UNTRACKED_FILES = 5000
MAX_HASHED_FILE = 64 * 1024 * 1024
MAX_HASHED_TOTAL = 512 * 1024 * 1024
MAX_TITLE = 200
MAX_RESULT_BYTES = 64 * 1024
BRANCH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}\Z")
_PULL_URL = re.compile(r"https://[^\s/]+/[^\s]+/pull/(\d{1,9})\Z")
_LOCALE = ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "USER", "LOGNAME")
# Toolchains a project's tests commonly need. Paths and settings only, never secrets.
TOOLCHAIN_ENV = ("JAVA_HOME", "ANDROID_HOME", "ANDROID_SDK_ROOT", "GOPATH", "GOROOT", "CARGO_HOME", "RUSTUP_HOME",
                 "DEVELOPER_DIR", "NVM_DIR", "PYENV_ROOT", "VOLTA_HOME", "PNPM_HOME", "XDG_CACHE_HOME",
                 "XDG_CONFIG_HOME", "XDG_DATA_HOME", "SSH_AUTH_SOCK")
# What git push and the GitHub CLI need to use the person's own sign-ins.
_SIGN_IN = ("SSH_AUTH_SOCK", "XDG_CONFIG_HOME", "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST")


class FactoryError(Exception):
    """A step that can't go on. `code` is fixed; `detail` is a short, safe hint (or empty)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code, self.detail = code, detail


# MARK: Environments

def _pick(environ: Mapping[str, str], keys: tuple[str, ...]) -> dict[str, str]:
    return {key: environ[key] for key in keys if environ.get(key)}


def git_env(environ: Mapping[str, str]) -> dict[str, str]:
    """git as the person runs it (their config, hooks and LFS), but never waiting for a prompt."""
    env = _pick(environ, ("PATH", "HOME") + _LOCALE + ("SSH_AUTH_SOCK", "XDG_CONFIG_HOME"))
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never", "GIT_OPTIONAL_LOCKS": "0"})
    return env


def command_env(environ: Mapping[str, str]) -> dict[str, str]:
    """A command stage's environment: an allowlist with PATH, HOME and toolchain settings, and nothing else."""
    env = _pick(environ, ("PATH", "HOME", "SHELL") + _LOCALE + TOOLCHAIN_ENV)
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env.update({"CI": "1", "NO_COLOR": "1", "TERM": "dumb", "GIT_TERMINAL_PROMPT": "0"})
    return env


def pull_request_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The pull request step: the person's git and GitHub CLI sign-ins, with prompts and colors off."""
    env = _pick(environ, ("PATH", "HOME") + _LOCALE + _SIGN_IN)
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("LANG", "C.UTF-8")
    env.update({"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never", "GH_PROMPT_DISABLED": "1",
                "GH_NO_UPDATE_NOTIFIER": "1", "GH_SPINNER_DISABLED": "1", "NO_COLOR": "1", "PYTHONUTF8": "1"})
    return env


# MARK: git

def is_repository_root(path: str) -> bool:
    """A folder with its own `.git`: a repository's root (a cheap check; preparing asks git itself)."""
    return isinstance(path, str) and os.path.isabs(path) and os.path.isdir(path) and os.path.lexists(
        os.path.join(path, ".git"))


def _program(name: str, env: Mapping[str, str]) -> str:
    found = shutil.which(name, path=env.get("PATH"))
    if found is None:
        raise FactoryError(f"{name}_missing")
    return found


def git_available(environ: Mapping[str, str]) -> bool:
    return shutil.which("git", path=environ.get("PATH")) is not None


def _git(path: str, args: list[str], env: Mapping[str, str], *, timeout: float = GIT_SECONDS,
         code: str = "git_failed") -> str:
    """stdout of `git -C <path> <args>`, or FactoryError(code) with git's last error line as the hint."""
    try:
        result = subprocess.run([_program("git", env), "-C", path, *args], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=timeout, env=dict(env), check=False)
    except subprocess.TimeoutExpired:
        raise FactoryError(code, "git took too long.") from None
    except OSError:
        raise FactoryError(code) from None
    if result.returncode != 0:
        raise FactoryError(code, last_line(result.stderr))
    return result.stdout.decode("utf-8", "replace")


def last_line(data: bytes) -> str:
    lines = [line.strip() for line in data.decode("utf-8", "replace").splitlines() if line.strip()]
    return lines[-1][:300] if lines else ""


def current_branch(path: str, env: Mapping[str, str]) -> str:
    """The checked-out branch, or "HEAD" when none is."""
    return _git(path, ["rev-parse", "--abbrev-ref", "HEAD"], env).strip()


def slug(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30].strip("-")
    return value or "workflow"


def prepare_workspace(repo: str, placement: str, base: str | None, name: str,
                      env: Mapping[str, str]) -> dict:
    """{placement, path, branch, base} for one run. A worktree gets branch `factory/<name>` in
    `<repo>/.worktrees/factory-<name>`; `local` is the repository itself on its current branch."""
    if not is_repository_root(repo):
        raise FactoryError("not_repository")
    try:
        top = _git(repo, ["rev-parse", "--show-toplevel"], env).strip()
    except FactoryError:
        raise FactoryError("not_repository") from None
    if os.path.realpath(top) != os.path.realpath(repo):
        raise FactoryError("not_repository")
    try:
        current = current_branch(repo, env)
    except FactoryError:
        raise FactoryError("no_commits") from None
    if placement == "local":
        if current == "HEAD":
            raise FactoryError("not_on_branch")
        return {"placement": "local", "path": repo, "branch": current, "base": base or current}
    base = base or current
    if base == "HEAD" or BRANCH.fullmatch(base) is None:
        raise FactoryError("not_on_branch")
    try:
        _git(repo, ["rev-parse", "--verify", "--quiet", f"{base}^{{commit}}"], env)
    except FactoryError:
        raise FactoryError("base_missing", base) from None
    branch = f"factory/{name}"
    folder = os.path.join(repo, WORKTREES, f"factory-{name}")
    if os.path.lexists(folder):
        raise FactoryError("worktree_exists")
    os.makedirs(os.path.join(repo, WORKTREES), exist_ok=True)
    _git(repo, ["worktree", "add", "-b", branch, folder, base], env, timeout=WORKTREE_SECONDS,
         code="worktree_failed")
    return {"placement": "worktree", "path": folder, "branch": branch, "base": base}


def _hash_stream(argv: list[str], env: Mapping[str, str], digest, timeout: float) -> None:
    """Feed a command's stdout into `digest` without keeping it, within `timeout`."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               env=dict(env))
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    try:
        while True:
            chunk = process.stdout.read(1 << 16)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        timer.cancel()
        process.stdout.close()
    if process.wait() != 0:
        raise FactoryError("changes_unreadable")


def _untracked(path: str, name: str, digest, budget: list[int]) -> int:
    """Hash one untracked file (or link) into `digest` without following links. Returns its line count."""
    full = os.path.join(path, name)
    digest.update(name.encode("utf-8", "surrogateescape") + b"\0")
    try:
        info = os.lstat(full)
    except OSError:
        return 0
    if stat.S_ISLNK(info.st_mode):
        digest.update(b"link\0" + os.fsencode(os.readlink(full)) + b"\0")
        return 0
    if not stat.S_ISREG(info.st_mode):
        return 0
    if info.st_size > MAX_HASHED_FILE or info.st_size > budget[0]:
        # Too big to read every time: its size and change time stand in for its bytes.
        digest.update(f"big\0{info.st_size}\0{info.st_mtime_ns}\0".encode("ascii"))
        return 0
    budget[0] -= info.st_size
    lines, binary, first = 0, False, True
    try:
        descriptor = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return 0
    try:
        while True:
            chunk = os.read(descriptor, 1 << 16)
            if not chunk:
                break
            if first:
                binary, first = b"\0" in chunk[:8000], False
            digest.update(chunk)
            lines += chunk.count(b"\n")
    finally:
        os.close(descriptor)
    digest.update(b"\0")
    return 0 if binary else lines


def changes(path: str, base: str, env: Mapping[str, str]) -> tuple[dict, str]:
    """What a run changed in its folder since it left `base` (commits and uncommitted work, new files too):
    `{files, additions, deletions, branch, base}` and a SHA-256 of the exact state (HEAD, status, diff and new
    files), so a sign-off binds to what the person saw."""
    git = _program("git", env)
    head = _git(path, ["rev-parse", "HEAD"], env, code="changes_unreadable").strip()
    branch = current_branch(path, env)
    try:
        start = _git(path, ["merge-base", base, "HEAD"], env).strip()
    except FactoryError:
        start = head
    numstat = _git(path, ["diff", "--no-renames", "--numstat", "-z", start, "--", *EXCLUDE_WORKTREES], env,
                   code="changes_unreadable")
    status = _git(path, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *EXCLUDE_WORKTREES],
                  env, code="changes_unreadable")
    untracked = [name for name in _git(path, ["ls-files", "--others", "--exclude-standard", "-z", "--",
                                              *EXCLUDE_WORKTREES], env, code="changes_unreadable").split("\0")
                 if name][:MAX_UNTRACKED_FILES]
    files = additions = deletions = 0
    for record in numstat.split("\0"):
        parts = record.split("\t", 2)
        if len(parts) != 3:
            continue
        files += 1
        additions += int(parts[0]) if parts[0].isdigit() else 0
        deletions += int(parts[1]) if parts[1].isdigit() else 0
    digest = hashlib.sha256()
    digest.update(b"head\0" + head.encode("ascii", "replace") + b"\0status\0" + status.encode("utf-8") + b"\0diff\0")
    _hash_stream([git, "-C", path, "diff", "--no-renames", "--binary", start, "--", *EXCLUDE_WORKTREES], env,
                 digest, GIT_SECONDS)
    digest.update(b"\0untracked\0")
    budget = [MAX_HASHED_TOTAL]
    for name in untracked:
        files += 1
        additions += _untracked(path, name, digest, budget)
    return ({"files": files, "additions": additions, "deletions": deletions, "branch": branch, "base": base},
            digest.hexdigest())


# MARK: The pull request step (its own process)

def _run(argv: list[str], env: Mapping[str, str], timeout: float, cwd: str) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout,
                                env=dict(env), cwd=cwd, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, b"", b"timed out")
    except OSError:
        return subprocess.CompletedProcess(argv, 127, b"", b"couldn't start")
    # The step's own output goes to its log in the attempt folder; the app never sees it.
    sys.stdout.write(f"$ {' '.join(os.path.basename(argv[0]) if index == 0 else part for index, part in enumerate(argv))}"
                     f" (exit {result.returncode})\n")
    sys.stdout.write(result.stdout.decode("utf-8", "replace")[-4000:])
    sys.stdout.write(result.stderr.decode("utf-8", "replace")[-4000:])
    sys.stdout.flush()
    return result


def _pull_url(text: str) -> tuple[str, int] | None:
    for line in reversed(text.splitlines()):
        match = _PULL_URL.fullmatch(line.strip())
        if match is not None and len(line.strip()) <= 300:
            return line.strip(), int(match.group(1))
    return None


def open_pull_request(folder: Path, cwd: str, env: Mapping[str, str]) -> dict:
    """Commit everything in `cwd`, push its branch and open a pull request with the GitHub CLI.

    `folder` holds request.json ({base, title, draft}) and body.md. Returns {"ok": true, "url", "number"} or
    {"ok": false, "code", "detail"?}. Nothing is committed before gh is known to be installed and signed in.
    """
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    base, title, draft = request.get("base"), request.get("title"), request.get("draft") is True
    if not isinstance(base, str) or BRANCH.fullmatch(base) is None or not isinstance(title, str) or not title:
        return {"ok": False, "code": "request_invalid"}
    gh = shutil.which("gh", path=env.get("PATH"))
    if gh is None:
        return {"ok": False, "code": "gh_missing"}
    if _run([gh, "auth", "status"], env, GH_SECONDS, cwd).returncode != 0:
        return {"ok": False, "code": "gh_signed_out"}
    git = shutil.which("git", path=env.get("PATH"))
    if git is None:
        return {"ok": False, "code": "git_missing"}
    head = _run([git, "rev-parse", "--abbrev-ref", "HEAD"], env, GIT_SECONDS, cwd)
    branch = head.stdout.decode("utf-8", "replace").strip()
    if head.returncode != 0 or branch == "HEAD" or BRANCH.fullmatch(branch) is None:
        return {"ok": False, "code": "not_on_branch"}
    if branch == base:
        return {"ok": False, "code": "same_branch"}
    if _run([git, "add", "-A", "--", *EXCLUDE_WORKTREES], env, GIT_SECONDS, cwd).returncode != 0:
        return {"ok": False, "code": "commit_failed"}
    if _run([git, "diff", "--cached", "--quiet"], env, GIT_SECONDS, cwd).returncode != 0:
        commit = _run([git, "commit", "-m", title], env, GIT_SECONDS * 3, cwd)
        if commit.returncode != 0:
            who = b"tell me who you are" in (commit.stderr + commit.stdout).lower()
            return {"ok": False, "code": "commit_identity" if who else "commit_failed"}
    ahead = _run([git, "rev-list", "--count", f"{base}..HEAD"], env, GIT_SECONDS, cwd)
    if ahead.returncode == 0 and ahead.stdout.strip() == b"0":
        return {"ok": False, "code": "no_changes"}
    push = _run([git, "push", "-u", "origin", branch], env, PUSH_SECONDS, cwd)
    if push.returncode != 0:
        return {"ok": False, "code": "push_failed", "detail": last_line(push.stderr)}
    argv = [gh, "pr", "create", "--base", base, "--head", branch, "--title", title,
            "--body-file", str(folder / "body.md")]
    created = _run(argv + (["--draft"] if draft else []), env, GH_SECONDS, cwd)
    found = _pull_url(created.stdout.decode("utf-8", "replace")) if created.returncode == 0 else None
    if found is None:
        # Opened before (a retry after a lost answer): use the open one for this branch.
        view = _run([gh, "pr", "view", branch, "--json", "url,number"], env, GH_SECONDS, cwd)
        try:
            value = json.loads(view.stdout.decode("utf-8", "replace")) if view.returncode == 0 else None
        except ValueError:
            value = None
        if isinstance(value, dict) and isinstance(value.get("url"), str):
            found = _pull_url(value["url"])
    if found is None:
        return {"ok": False, "code": "create_failed", "detail": last_line(created.stderr)}
    return {"ok": True, "url": found[0], "number": found[1]}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "pull-request":
        print("usage: workflow_factory.py pull-request <attempt folder>", file=sys.stderr)
        return 2
    folder = Path(argv[1])
    try:
        result = open_pull_request(folder, os.getcwd(), dict(os.environ))
    except (OSError, ValueError) as error:
        result = {"ok": False, "code": "step_failed", "detail": type(error).__name__}
    temporary = folder / "result.json.tmp"
    temporary.write_text(json.dumps(result), encoding="utf-8")
    os.replace(temporary, folder / "result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
