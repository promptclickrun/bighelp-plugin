#!/usr/bin/env python3
"""Receipt-backed local Hermes CLI handoffs; no installed-runtime imports."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import time

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def resolve_hermes_root() -> Path:
    env_home = os.environ.get("HERMES_ROOT") or os.environ.get("HERMES_HOME")
    if env_home:
        candidate = Path(env_home).expanduser()
        if candidate.parent.name == "profiles":
            return candidate.parent.parent
        return candidate
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (
            (parent / "config.yaml").is_file()
            and parent.name != "profiles"
            and parent.parent.name != "profiles"
        ):
            return parent
    for fallback in (Path("/opt/data"), Path("/srv/hermes")):
        if (fallback / "config.yaml").is_file():
            return fallback
    return Path.home() / ".hermes"


def discover_profiles(root: Path) -> set[str]:
    found = {"default"}
    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        for entry in profiles_dir.iterdir():
            if entry.is_dir() and PROFILE_ID_RE.fullmatch(entry.name):
                if (entry / "profile.yaml").is_file() or (entry / "config.yaml").is_file():
                    found.add(entry.name)
    return found


def resolve_hermes_cli(root: Path) -> str | None:
    found = shutil.which("hermes")
    if found:
        return found
    for candidate in (
        Path.home() / ".local/bin/hermes",
        root / ".local/bin/hermes",
        Path("/usr/local/bin/hermes"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


HOME = resolve_hermes_root()
PROFILES = discover_profiles(HOME)
ROOT = HOME / "state/team-handoffs"


def save(path: Path, value: dict) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("Receipt directory must be owned by this user and not be a symlink")
    path.chmod(0o700)


def process_identity(pid: int | None) -> str | None:
    if not isinstance(pid, int):
        return None
    result = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def final_event(output: str) -> dict | None:
    found = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            found = event
    return found


def inspect_receipt(path: Path) -> dict:
    record = json.loads(path.read_text(encoding="utf-8"))
    if record["status"] not in {"queued", "running", "orphan_running"}:
        return record
    owner = process_identity(record.get("owner_pid"))
    child = process_identity(record.get("pid"))
    owner_live = bool(owner and owner == record.get("owner_identity"))
    child_live = bool(
        child and (not record.get("child_identity") or child == record.get("child_identity"))
    )
    if owner_live:
        return record
    if child_live:
        record["status"] = "orphan_running"
    else:
        output = path.parent / "stdout.jsonl"
        event = final_event(output.read_text(encoding="utf-8")) if output.exists() else None
        if event and event.get("exit_code") == 0 and str(event.get("text", "")).strip():
            record.update(
                status="completed",
                reply=event["text"],
                exit_code=0,
                session_id=event.get("session_id"),
                recovered_from_child_result=True,
            )
        else:
            record["status"] = "interrupted_unknown"
        record["finished_at"] = time.time()
    save(path, record)
    return record


def run_job(
    *,
    root: Path,
    job_id: str,
    sender: str,
    target: str,
    message: str,
    workspace: Path,
    timeout: float,
    cli: Path,
    allowed_profiles: set[str] | None = None,
    hermes_home: Path | None = None,
) -> dict:
    valid_profiles = set(allowed_profiles) if allowed_profiles is not None else discover_profiles(hermes_home or HOME)
    if (
        not PROFILE_ID_RE.fullmatch(sender)
        or not PROFILE_ID_RE.fullmatch(target)
        or sender == target
        or sender not in valid_profiles
        or target not in valid_profiles
    ):
        raise ValueError("Invalid sender or local target; self-handoffs are refused")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", job_id):
        raise ValueError("Invalid job ID")
    if not message.strip() or len(message) > 16000:
        raise ValueError("Brief must contain 1 to 16000 characters")
    workspace = Path(workspace).resolve(strict=True)
    if not workspace.is_dir() or not 0 < timeout <= 14400:
        raise ValueError("Invalid workspace or timeout")
    root = Path(root)
    private_dir(root)
    job = root / job_id
    private_dir(job)
    receipt = job / "result.json"
    fingerprint = hashlib.sha256(
        json.dumps([sender, target, message, str(workspace)]).encode("utf-8")
    ).hexdigest()
    # Separate ID and target locks prevent replay and two jobs entering one Bot Chat.
    with (job / "job.lock").open("a", encoding="utf-8") as id_lock:
        os.chmod(id_lock.name, 0o600)
        try:
            fcntl.flock(id_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "job_busy", "job_id": job_id}
        if receipt.exists():
            old = inspect_receipt(receipt)
            if old.get("fingerprint") != fingerprint:
                raise ValueError("Job ID already belongs to a different brief")
            return old
        record = dict(
            job_id=job_id,
            sender=sender,
            target=target,
            fingerprint=fingerprint,
            status="queued",
            created_at=time.time(),
            receipt=str(receipt),
            owner_pid=os.getpid(),
            owner_identity=process_identity(os.getpid()),
        )
        save(receipt, record)
        print(json.dumps({"job_id": job_id, "receipt": str(receipt)}), flush=True)
        with (root / f"{target}.lock").open("a", encoding="utf-8") as target_lock:
            os.chmod(target_lock.name, 0o600)
            try:
                fcntl.flock(target_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                record.update(status="target_busy", finished_at=time.time())
                save(receipt, record)
                return record
            brief = job / "brief.txt"
            fd = os.open(brief, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(
                    f"Message from teammate @{sender} via authorized durable handoff {job_id}. "
                    "Do this bounded task yourself. Return your result here; the runner relays it. "
                    "Do not send an acknowledgement or delegate it back.\n"
                    f"Task working directory: {workspace}\n\n" + message
                )
            # Target CLI loads its own credentials. Never inherit sender auth/session state.
            allowed = {
                "HOME",
                "PATH",
                "USER",
                "LOGNAME",
                "SHELL",
                "TMPDIR",
                "LANG",
                "LC_ALL",
                "LC_CTYPE",
                "TERM",
            }
            env = {key: value for key, value in os.environ.items() if key in allowed}
            cli_dir = str(Path(cli).resolve().parent)
            path_parts = [part for part in env.get("PATH", "").split(os.pathsep) if part]
            if cli_dir not in path_parts:
                env["PATH"] = os.pathsep.join([cli_dir, *path_parts])
            env["HERMES_HOME"] = str(hermes_home or HOME)
            env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
            command = [
                str(cli),
                "-p",
                target,
                "chat",
                "-Q",
                "-c",
                "Bot Chat",
                "--create-if-missing",
                "--in",
                str(Path.home()),
                "--format",
                "stream-json",
                "--run-budget",
                str(max(1, int(timeout - 30))),
                "--query-file",
                str(brief),
            ]
            proc = None
            stdout_path, stderr_path = job / "stdout.jsonl", job / "stderr.txt"
            status = "failed"
            with (
                os.fdopen(
                    os.open(stdout_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                    "w",
                    encoding="utf-8",
                ) as out,
                os.fdopen(
                    os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                    "w",
                    encoding="utf-8",
                ) as err,
            ):
                try:
                    proc = subprocess.Popen(
                        command,
                        env=env,
                        cwd=Path.home(),
                        stdin=subprocess.DEVNULL,
                        stdout=out,
                        stderr=err,
                        text=True,
                        start_new_session=True,
                    )
                    record.update(
                        status="running",
                        pid=proc.pid,
                        child_identity=process_identity(proc.pid),
                        started_at=time.time(),
                    )
                    save(receipt, record)
                    proc.wait(timeout=timeout)
                    status = "exited"
                except subprocess.TimeoutExpired:
                    if proc is not None:
                        os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait()
                    status = "timed_out"
                except Exception as error:
                    if proc is not None and proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGTERM)
                        proc.wait(timeout=10)
                    err.write(type(error).__name__)
            stdout = stdout_path.read_text(encoding="utf-8")
            stderr = stderr_path.read_text(encoding="utf-8")
            event = final_event(stdout)
            if status == "exited" and proc is not None:
                status = (
                    "completed"
                    if proc.returncode == 0
                    and event
                    and event.get("exit_code") == 0
                    and str(event.get("text", "")).strip()
                    else "failed"
                )
            record.update(
                status=status,
                exit_code=proc.returncode if proc else None,
                finished_at=time.time(),
                reply=event.get("text", "") if event else "",
                session_id=event.get("session_id") if event else None,
                diagnostic=stderr,
            )
            save(receipt, record)
            return record


def main() -> int:
    profiles = sorted(discover_profiles(HOME))
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("--id", required=True)
    run.add_argument("--sender", required=True, choices=profiles)
    run.add_argument("--target", required=True, choices=profiles)
    run.add_argument("--message-file", required=True, type=Path)
    run.add_argument("--workspace", type=Path, default=HOME)
    run.add_argument("--timeout", type=float, default=3600)
    status = sub.add_parser("status")
    status.add_argument("--id", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == "status":
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", args.id):
            parser.error("Invalid job ID")
        result = inspect_receipt(ROOT / args.id / "result.json")
    else:
        cli = resolve_hermes_cli(HOME)
        if not cli:
            parser.error("Hermes CLI is not on PATH")
        result = run_job(
            root=ROOT,
            job_id=args.id,
            sender=args.sender,
            target=args.target,
            message=args.message_file.read_text(encoding="utf-8"),
            workspace=args.workspace,
            timeout=args.timeout,
            cli=Path(cli),
        )
    # Diagnostics remain private in the receipt; only the reply is relayed.
    print(
        json.dumps(
            {key: value for key, value in result.items() if key != "diagnostic"},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
