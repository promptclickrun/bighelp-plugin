"""One workflow stage as one Hermes chat turn: the brief, the process and its stream.

Patterns copied from Hermes' Kanban dispatcher (not imported, so Hermes internals stay untouched): a profile-scoped
HERMES_HOME, `--cli`, an explicit `--toolsets` pin, a scrubbed environment, `start_new_session=True` so the whole
process group can be stopped, and a pid + start-time fingerprint so a reused pid is never signalled.
"""
from __future__ import annotations

import json
import os
import re
import signal
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from .workflow_store import clean_text, open_private, secure_dir


REPORT_ENV = "HERMES_QUIET_TURN_REPORT_FILE"
SESSION_SOURCE = "workflow"
# Hermes' own default toolset list is the whole platform. A stage with no tools gets only the to-do tool.
EMPTY_TOOLSET = "todo"
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_STREAM_READ = 256 * 1024
MAX_INLINE_BYTES = 100 * 1024
LIVE_LINE_CHARS = 300
_SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "USER", "LOGNAME", "SHELL",
             "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "PYTHONUTF8")
_PROFILE_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


# MARK: Profiles and the Hermes command

def profile_home(hermes_root: Path, profile: str) -> Path | None:
    """The agent's HERMES_HOME, or None when it no longer exists (same rules as Hermes' `-p`)."""
    if profile == "default":
        return hermes_root if hermes_root.is_dir() else None
    if _PROFILE.fullmatch(profile) is None:
        return None
    home = hermes_root / "profiles" / profile
    if not home.is_dir() or (hermes_root / "profiles" / ".deleted" / profile).exists():
        return None
    if not any((home / marker).is_file() or (home / marker).is_symlink() for marker in _PROFILE_MARKERS):
        return None
    return home


FULL_FEATURES = frozenset({"cli", "source", "toolsets", "query", "query_file", "format", "quiet", "report"})
# The stream runner needs all three; without them a stage runs as a plain `chat -q` turn (the text runner).
STREAM_FEATURES = frozenset({"query_file", "format", "report"})
MAX_INLINE_QUERY_BYTES = 96 * 1024
STDERR_TAIL_BYTES = 4096
STDERR_TRIM_AT = 64 * 1024
_FLAG = re.compile(r"(?<![\w-])(--?[A-Za-z][A-Za-z0-9-]*)")


def runner_mode(features: frozenset[str]) -> str:
    return "stream" if STREAM_FEATURES <= features else "text"


def _looks_like_path(value: str) -> bool:
    expanded = os.path.expanduser(value)
    return expanded.startswith("~") or os.path.isabs(expanded) or bool(os.path.dirname(expanded))


def _which_no_cwd(command: str, path: str) -> str | None:
    """A PATH lookup that never searches the current folder."""
    for folder in path.split(os.pathsep):
        if not folder or folder == ".":
            continue
        candidate = os.path.join(os.path.expanduser(folder), command)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def module_argv() -> list[str]:
    return [sys.executable, "-m", "hermes_cli.main"]


def is_module_argv(argv: list[str]) -> bool:
    return argv[1:3] == ["-m", "hermes_cli.main"]


def hermes_argv(environ: Mapping[str, str] = os.environ, *, importable: bool | None = None) -> list[str]:
    """How to start Hermes, like Hermes' Kanban dispatcher (`_resolve_hermes_argv`).

    `$HERMES_BIN` first (a path, or a name looked up on PATH without the current folder), then this interpreter's
    `-m hermes_cli.main` when `hermes_cli` imports here, and only then a `hermes` launcher on PATH. The module form
    wins over PATH so a planted `hermes` can't stand in for the running install.
    """
    configured = environ.get("HERMES_BIN", "").strip()
    if configured:
        if _looks_like_path(configured):
            path = os.path.abspath(os.path.expanduser(configured))
            return [path] if os.access(path, os.X_OK) else module_argv()
        found = _which_no_cwd(configured, environ.get("PATH", ""))
        return [found] if found else module_argv()
    if importable is None:
        importable = hermes_import_root() is not None
    if importable:
        return module_argv()
    found = _which_no_cwd("hermes", environ.get("PATH", ""))
    return [found] if found else module_argv()


def worker_argv(base: list[str], profile: str, tools: list[str], features: frozenset[str] = FULL_FEATURES,
                *, query: str | None = None) -> list[str]:
    """The stage's Hermes command, with only the flags this Hermes has.

    The stream runner reads brief.md and writes JSON lines. The text runner reads brief.md too when Hermes has
    `--query-file`, and otherwise gets `query` (the brief itself, or a short prompt that points to brief.md).
    """
    argv = [*base, "-p", profile]
    if "cli" in features:
        argv.append("--cli")
    argv.append("chat")
    if "source" in features:
        argv += ["--source", SESSION_SOURCE]
    if "toolsets" in features:
        argv += ["--toolsets", ",".join(tools) if tools else EMPTY_TOOLSET]
    if runner_mode(features) == "stream":
        return argv + ["--query-file", "brief.md", "--format", "stream-json"]
    if "query_file" in features:
        argv += ["--query-file", "brief.md"]
    else:
        argv += ["-q", query or text_query(None)]
    if "quiet" in features:
        argv.append("-Q")
    return argv


def text_query(brief: str | None) -> str:
    """The `-q` text when Hermes can't read a query file: the brief when it is small enough, else a pointer."""
    if brief is not None and len(brief.encode("utf-8")) <= MAX_INLINE_QUERY_BYTES:
        return brief
    return ("You are one stage of a bighelp workflow. Your full task is in the file brief.md in your working "
            "folder. Read it first and follow it exactly, including how to hand off at the end.")


def worker_env(base: Mapping[str, str], *, home: Path, profile: str, attempt_dir: Path,
               import_path: list[str] | tuple[str, ...] = (), report: bool = True) -> dict[str, str]:
    """An allowlist, not a copy: the agent's own keys come from its profile's .env, never from this process.

    `import_path` is where `hermes_cli` (and its dependencies) live for a module-form worker. A bundled bare
    interpreter only finds them in-process, so the scrubbed child gets them pinned like Hermes' own Kanban and
    cron workers do; without it the worker dies with ModuleNotFoundError before its first line.
    """
    env = {key: base[key] for key in _SAFE_ENV if key in base}
    env.update({
        "HERMES_HOME": str(home), "HERMES_PROFILE": profile, "TERMINAL_CWD": str(attempt_dir),
        "HERMES_SESSION_SOURCE": SESSION_SOURCE, "PYTHONUTF8": "1",
    })
    if report:
        env[REPORT_ENV] = str(attempt_dir / "report.json")
    if import_path:
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(import_path))
    return env


def hermes_import_root() -> str | None:
    """Where `hermes_cli` lives in this process (a bundled interpreter may know it only in-process)."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("hermes_cli")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    return str(Path(spec.origin).resolve().parent.parent)


def hermes_import_path(root: str | None = None) -> list[str]:
    """What a module-form worker needs on PYTHONPATH: the Hermes tree, and its dependency folder when Hermes
    manages one. Uses Hermes' own pin (`cron.scheduler_worker_env`) when it exists. Empty when nothing is needed
    (an installed wheel) or Hermes isn't importable here."""
    root = root if root is not None else hermes_import_root()
    if not root:
        return []
    try:
        from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
        pinned = pin_hermes_tree_on_pythonpath({}, Path(root))
        return [item for item in pinned.get("PYTHONPATH", "").split(os.pathsep) if item]
    except Exception:
        pass
    try:
        import sysconfig
        if Path(sysconfig.get_paths()["purelib"]).resolve() == Path(root).resolve():
            return []
    except (KeyError, OSError):
        pass
    return [root]


def _parser_features() -> frozenset[str] | None:
    try:
        from hermes_cli._parser import build_top_level_parser
        parser, _subparsers, chat = build_top_level_parser()
        top = {option for action in parser._actions for option in action.option_strings}
        flags = {option for action in chat._actions for option in action.option_strings}
        formats = {choice for action in chat._actions if "--format" in action.option_strings
                   for choice in (action.choices or ())}
    except Exception:
        return None
    return _features_from_flags(top | flags, flags, "stream-json" in formats)


def _features_from_flags(every: set[str], chat: set[str], stream_json: bool) -> frozenset[str]:
    found = set()
    for name, options in (("source", ("--source",)), ("toolsets", ("--toolsets",)), ("query", ("-q", "--query")),
                          ("query_file", ("--query-file",)), ("quiet", ("-Q",))):
        if any(option in chat for option in options):
            found.add(name)
    if "--cli" in every:
        found.add("cli")
    if "--format" in chat and stream_json:
        found.add("format")
    return frozenset(found)


def _help_features(argv: list[str], env: dict[str, str]) -> frozenset[str] | None:
    """`hermes chat --help` for a Hermes whose parser can't be read in-process."""
    try:
        result = subprocess.run([*argv, "chat", "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=30, check=False, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout[: 256 * 1024].decode("utf-8", "replace")
    flags = set(_FLAG.findall(text))
    return _features_from_flags(flags, flags, "stream-json" in text)


def detect_hermes_features(argv: list[str] | None = None, import_path: list[str] | None = None) -> frozenset[str]:
    """Which chat flags and turn report this Hermes has. Empty when Hermes can't be asked at all."""
    found = _parser_features()
    if found is None:
        argv = argv or hermes_argv()
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "TMPDIR") if key in os.environ}
        path = hermes_import_path() if import_path is None else import_path
        if path and is_module_argv(argv):
            env["PYTHONPATH"] = os.pathsep.join(path)
        found = _help_features(argv, env)
    if found is None:
        return frozenset()
    try:
        from hermes_cli.quiet_single_query import TURN_REPORT_FILE_ENV
        if TURN_REPORT_FILE_ENV == REPORT_ENV:
            found = found | {"report"}
    except Exception:
        pass
    return found


def encode_features(features: frozenset[str]) -> str:
    return ",".join(sorted(features & FULL_FEATURES)) or "none"


def decode_features(value: str) -> frozenset[str]:
    return frozenset(item for item in value.split(",") if item in FULL_FEATURES)


# MARK: Processes

def _read_small(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read(4096)
    except OSError:
        return None


def boot_id() -> str:
    linux = _read_small("/proc/sys/kernel/random/boot_id")
    if linux:
        return linux.strip()
    try:
        import psutil  # type: ignore
        return str(int(psutil.boot_time()))
    except Exception:
        pass
    try:
        output = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=2,
                                check=False).stdout
        match = re.search(r"sec\s*=\s*(\d+)", output)
        if match:
            return match.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _start_time(pid: int) -> str | None:
    stat_text = _read_small(f"/proc/{pid}/stat")
    if stat_text:
        fields = stat_text.rsplit(")", 1)[-1].split()
        return fields[19] if len(fields) > 19 else None
    try:
        import psutil  # type: ignore
        return f"{psutil.Process(pid).create_time():.2f}"
    except Exception:
        pass
    try:
        output = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                                timeout=2, check=False).stdout.strip()
        return output or None
    except (OSError, subprocess.SubprocessError):
        return None


def same_value(first: str, second: str, tolerance: float = 2.0) -> bool:
    try:
        return abs(float(first) - float(second)) <= tolerance
    except ValueError:
        return first == second


def same_process(recorded: str | None, current: str | None) -> bool:
    if not recorded or not current or "|" not in recorded or "|" not in current:
        return False
    boot_a, start_a = recorded.split("|", 1)
    boot_b, start_b = current.split("|", 1)
    return same_value(boot_a, boot_b) and same_value(start_a, start_b)


class OSProcessHost:
    """Real processes. Tests use a fake with the same methods."""

    def __init__(self):
        self._children: dict[int, subprocess.Popen] = {}

    def spawn(self, argv: list[str], *, env: dict[str, str], cwd: Path, stdout_path: Path,
              stderr_path: Path | None = None) -> int:
        descriptor = open_private(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        errors = subprocess.DEVNULL
        try:
            if stderr_path is not None:
                errors = open_private(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            process = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=descriptor,
                                       stderr=errors, start_new_session=True, close_fds=True)
        finally:
            os.close(descriptor)
            if errors != subprocess.DEVNULL:
                os.close(errors)
        self._children[process.pid] = process
        return process.pid

    def poll(self, pid: int) -> int | None:
        """The exit code of our own child once it ended (and reaps it); None while running or not ours."""
        process = self._children.get(pid)
        if process is None:
            return None
        code = process.poll()
        if code is not None:
            self._children.pop(pid, None)
        return code

    def owns(self, pid: int) -> bool:
        return pid in self._children

    def alive(self, pid: int) -> bool:
        process = self._children.get(pid)
        if process is not None:
            return process.poll() is None
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        state = _read_small(f"/proc/{pid}/status")
        if state is not None:
            return not any(line.startswith("State:") and "Z" in line.split(":", 1)[1] for line in state.splitlines())
        if sys.platform == "darwin":
            try:
                output = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                                        timeout=2, check=False)
                return output.returncode == 0 and "Z" not in output.stdout
            except (OSError, subprocess.SubprocessError):
                return True
        return True

    def fingerprint(self, pid: int) -> str | None:
        start = _start_time(pid)
        return None if start is None else f"{boot_id()}|{start}"

    def boot_id(self) -> str:
        return boot_id()

    def signal_group(self, pid: int, number: int) -> None:
        try:
            os.killpg(pid, number)
        except ProcessLookupError:
            pass


# MARK: Attempt folder and brief

def attempt_dir(runs_dir: Path, run_id: str, stage_key: str, iteration: int, number: int) -> Path:
    return runs_dir / run_id / stage_key / f"{iteration}-{number}"


def prepare_attempt(path: Path) -> None:
    for folder in (path.parent.parent.parent, path.parent.parent, path.parent, path, path / "inputs", path / "out"):
        secure_dir(folder)


def write_private(path: Path, data: bytes, *, read_only: bool = False) -> None:
    descriptor = open_private(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        if read_only:
            os.fchmod(descriptor, 0o400)
    finally:
        os.close(descriptor)


_TYPE_NAMES = {"markdown_file": "Markdown file", "text": "text", "number": "number", "decision": "decision",
               "notes": "notes"}


def handoff_help(outputs: list[dict]) -> list[str]:
    lines = []
    for output in outputs:
        kind = output["type"]
        if kind == "markdown_file":
            how = ('{"content": "<the Markdown>"} or, if you wrote the file in your out folder, '
                   '{"path": "out/<name>.md"}. At most 512 KB.')
        elif kind == "text":
            how = "a JSON string, at most 8 KB."
        elif kind == "number":
            how = "a number."
        elif kind == "decision":
            how = "one of: " + ", ".join(f'"{value}"' for value in output.get("values", [])) + "."
        else:
            how = ('a list of at most 20 notes like {"severity": "major", "text": "..."}; '
                   'severity is "minor" or "major". Use [] for no notes.')
        lines.append(f"- `{output['name']}` ({_TYPE_NAMES[kind]}): {how}")
    return lines


def _example(outputs: list[dict]) -> str:
    example: dict[str, Any] = {}
    for output in outputs:
        kind = output["type"]
        example[output["name"]] = {
            "markdown_file": {"content": "# Title\n\n..."}, "text": "...", "number": 0,
            "decision": (output.get("values") or ["pass"])[0], "notes": [],
        }[kind]
    return json.dumps({"outputs": example}, ensure_ascii=False)


def render_brief(*, workflow_name: str, run_number: int, stage: dict, iteration: int,
                 uses: list[dict], change_notes: dict | None) -> str:
    """The query file. `uses` items: {reference, label, type, text?, file?}."""
    lines = [f"# Workflow stage: {stage['title']}", "",
             f"You are one stage of the bighelp workflow \"{workflow_name}\" (run {run_number}"
             + (f", round {iteration}" if iteration > 1 else "") + ").",
             "Work only on this stage. Nobody can answer questions while it runs, so make sensible choices "
             "yourself. Your working folder is this stage's own folder; read-only copies of earlier work are in "
             "inputs/ and any file you hand off goes in out/.", "", "## Your task", "",
             stage["instructions"].strip() or "Do the stage's task.", ""]
    if uses:
        lines += ["## What you have", ""]
        for item in uses:
            lines.append(f"### {item['label']} ({item['reference']})")
            lines.append("")
            if item.get("file") and item.get("text") is None:
                lines.append(f"Too long to include here. Read it from {item['file']}.")
            else:
                if item.get("file"):
                    lines.append(f"(Also in {item['file']}.)")
                    lines.append("")
                lines.append(item.get("text") or "(empty)")
            lines.append("")
    if change_notes and (change_notes.get("notes") or change_notes.get("from") == "you"):
        source = "The person who runs this workflow" if change_notes.get("from") == "you" else "The review"
        lines += ["## Changes asked for", "", f"{source} sent this back for changes. Fix these in this round:", ""]
        for note in change_notes.get("notes") or []:
            lines.append(f"- [{note.get('severity', 'major')}] {note.get('text', '')}")
        if not change_notes.get("notes"):
            lines.append("- Improve the work; no specific notes were given.")
        lines.append("")
    lines += ["## How to hand off", "",
              "End your final reply with exactly one fenced code block tagged bighelp-handoff that holds one JSON "
              "object. Do not put anything else in that block, and do not write a second one.", "",
              "```bighelp-handoff", _example(stage["outputs"]), "```", "", "Outputs:"]
    lines += handoff_help(stage["outputs"])
    lines.append("")
    return "\n".join(lines)


# MARK: Stream and report

def read_report(path: Path, pid: int | None) -> dict | None:
    """Hermes' turn report when it is complete and written by `pid` (when known)."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REPORT_BYTES:
            return None
        data = b""
        while len(data) <= MAX_REPORT_BYTES:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(descriptor)
    try:
        record = json.loads(data.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        return None
    if (type(record) is not dict or type(record.get("exit_code")) is not int
            or (pid is not None and record.get("pid") != pid)):
        return None
    return record


def read_stream(path: Path, offset: int) -> tuple[int, list[dict]]:
    """New complete JSON lines from `offset`, at most 256 KB per call."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return offset, []
    try:
        os.lseek(descriptor, offset, os.SEEK_SET)
        data = os.read(descriptor, MAX_STREAM_READ)
    finally:
        os.close(descriptor)
    end = data.rfind(b"\n")
    if end < 0:
        if len(data) >= MAX_STREAM_READ:
            return offset + len(data), []
        return offset, []
    records = []
    for line in data[: end + 1].splitlines():
        try:
            record = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            continue
        if type(record) is dict:
            records.append(record)
    return offset + end + 1, records


class LiveText:
    """Turns stream records into short sanitized live lines and token counts."""

    def __init__(self):
        self.buffer = ""

    def lines(self, record: dict) -> list[tuple[str, str]]:
        kind = record.get("type")
        if kind == "system" and record.get("subtype") == "init":
            model = record.get("model") if isinstance(record.get("model"), str) else ""
            return [("init", clean_text(f"Started{' with ' + model if model else ''}.", LIVE_LINE_CHARS))]
        if kind == "text" and isinstance(record.get("text"), str):
            self.buffer += record["text"]
            result = []
            while "\n" in self.buffer or len(self.buffer) >= LIVE_LINE_CHARS:
                cut = self.buffer.find("\n")
                cut = LIVE_LINE_CHARS if cut < 0 or cut > LIVE_LINE_CHARS else cut
                line, self.buffer = self.buffer[:cut], self.buffer[cut:].lstrip("\n")
                if line.strip():
                    result.append(("text", clean_text(line, LIVE_LINE_CHARS)))
            return result
        if kind == "tool_use":
            name = record.get("name") if isinstance(record.get("name"), str) else "tool"
            preview = ""
            if isinstance(record.get("input"), dict):
                preview = json.dumps(record["input"], ensure_ascii=False)[:160]
            return [("tool", clean_text(f"{name} {preview}".strip(), LIVE_LINE_CHARS))]
        if kind == "tool_result":
            name = record.get("name") if isinstance(record.get("name"), str) else "tool"
            return [("tool", clean_text(f"{name} {'failed' if record.get('is_error') else 'finished'}.",
                                        LIVE_LINE_CHARS))]
        if kind == "result":
            flushed = [("text", clean_text(self.buffer, LIVE_LINE_CHARS))] if self.buffer.strip() else []
            self.buffer = ""
            return flushed + [("result", "Finished." if record.get("exit_code") == 0 else "Ended with an error.")]
        return []


def tokens(record: dict) -> tuple[int, int] | None:
    if record.get("type") != "result" or not isinstance(record.get("tokens"), dict):
        return None
    values = record["tokens"]

    def number(key: str) -> int:
        value = values.get(key)
        return value if type(value) is int and 0 <= value < 10**12 else 0

    return number("input"), number("output")


# MARK: Worker errors

def trim_stderr(path: Path, *, above: int = STDERR_TRIM_AT, keep: int = STDERR_TAIL_BYTES) -> bytes:
    """Keep only the last `keep` bytes of the worker's error output once it is over `above`; returns that tail.

    The worker appends to the same file, so its next lines land after the kept tail.
    """
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    except OSError:
        return b""
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return b""
        start = max(0, info.st_size - keep)
        os.lseek(descriptor, start, os.SEEK_SET)
        tail = os.read(descriptor, keep)
        if info.st_size > above:
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, tail)
        return tail
    finally:
        os.close(descriptor)


_ERROR_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]{0,60}(?:\.[A-Za-z_][A-Za-z0-9_]{0,60}){0,4}"
                         r"(?:Error|Exception|Exit|Interrupt))\b(?::\s*(.*))?$")
_MISSING_MODULE = re.compile(r"No module named '([A-Za-z_][A-Za-z0-9_.]{0,100})'")


def stderr_summary(tail: bytes) -> str:
    """One fixed sentence about why a worker stopped, built from its error output but never quoting it.

    Only a Python error's class name and a missing module's name get through; paths and messages don't.
    """
    lines = [line.strip() for line in tail.decode("utf-8", "replace").splitlines() if line.strip()]
    for line in reversed(lines):
        match = _ERROR_LINE.match(line)
        if match is None:
            continue
        name = match.group(1).rsplit(".", 1)[-1]
        missing = _MISSING_MODULE.search(match.group(2) or "")
        if name in ("ModuleNotFoundError", "ImportError") and missing:
            return f"Hermes couldn't load the Python module {missing.group(1)}."
        return f"Hermes stopped with {name}."
    return "Hermes stopped before it started the turn." if lines else "Hermes stopped without saying why."


def read_tail(path: Path, limit: int) -> bytes | None:
    """The last `limit` bytes of a regular file, or None."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return None
        os.lseek(descriptor, max(0, info.st_size - limit), os.SEEK_SET)
        chunks = []
        while True:
            chunk = os.read(descriptor, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def stop_signals() -> tuple[int, int]:
    return signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)


