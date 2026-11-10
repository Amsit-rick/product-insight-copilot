"""Safely recover one stale self-hosted supply-chain runner project."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = (ROOT / ".tmp").resolve()
COMPOSE_FILE = ROOT / "deploy" / "docker-compose.local-registry.yml"
PROJECT_RE = re.compile(r"^pic-[a-z0-9][a-z0-9-]{2,62}$")


def _confined_lock_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == TMP_ROOT or TMP_ROOT not in resolved.parents:
        raise ValueError("lock directory must be a child of the repository .tmp directory")
    if not resolved.name.endswith(".lock"):
        raise ValueError("lock directory name must end with .lock")
    return resolved


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def inspect_recovery(
    *, project: str, lock_dir: Path, minimum_age_seconds: int, now: float | None = None
) -> dict[str, Any]:
    if not PROJECT_RE.fullmatch(project):
        raise ValueError("invalid Compose project name")
    if minimum_age_seconds < 300:
        raise ValueError("minimum lock age must be at least 300 seconds")
    confined = _confined_lock_dir(lock_dir)
    lock_exists = confined.exists()
    lock_age_seconds = None
    owner_pid = None
    if lock_exists:
        if not confined.is_dir():
            raise ValueError("runner lock path is not a directory")
        entries = list(confined.iterdir())
        if any(entry.name != "owner.json" or not entry.is_file() for entry in entries):
            raise ValueError("runner lock contains unexpected files")
        owner_path = confined / "owner.json"
        if not owner_path.is_file():
            raise ValueError("runner lock owner metadata is missing")
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            owner_pid = int(owner.get("pid") or 0)
            started_at = int(owner.get("started_at_epoch") or 0)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("runner lock owner metadata is invalid") from exc
        current = now if now is not None else time.time()
        lock_age_seconds = max(0, int(current - started_at))
        if lock_age_seconds < minimum_age_seconds:
            raise ValueError("runner lock is not old enough for recovery")
        if _pid_running(owner_pid):
            raise ValueError("runner lock owner process is still running")
    return {
        "project": project,
        "lock_dir": confined,
        "lock_exists": lock_exists,
        "lock_age_seconds": lock_age_seconds,
        "owner_pid": owner_pid,
    }


def recover_runner(
    *, project: str, lock_dir: Path, minimum_age_seconds: int, apply: bool
) -> dict[str, Any]:
    inspection = inspect_recovery(
        project=project,
        lock_dir=lock_dir,
        minimum_age_seconds=minimum_age_seconds,
    )
    command = [
        "docker",
        "compose",
        "-p",
        project,
        "-f",
        str(COMPOSE_FILE),
        "down",
        "-v",
        "--remove-orphans",
    ]
    if apply:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("exact runner project cleanup failed")
        confined = inspection["lock_dir"]
        if inspection["lock_exists"]:
            (confined / "owner.json").unlink()
            confined.rmdir()
    return {
        "schema_version": "self-hosted-runner-recovery-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "executed": apply,
        "passed": True,
        "mode": "apply" if apply else "dry_run",
        "project": project,
        "lock_was_present": inspection["lock_exists"],
        "lock_age_seconds": inspection["lock_age_seconds"],
        "compose_cleanup_planned": True,
        "compose_cleanup_executed": apply,
        "global_docker_cleanup_used": False,
        "production_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--lock-dir", type=Path, required=True)
    parser.add_argument("--minimum-age-seconds", type=int, default=1800)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    try:
        payload = recover_runner(
            project=args.project,
            lock_dir=args.lock_dir,
            minimum_age_seconds=args.minimum_age_seconds,
            apply=args.apply,
        )
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
