"""Verify scheduler lease competition and crash takeover on a real PostgreSQL database.

The rehearsal is deliberately local-only evidence. It starts an independent
holder process, proves that a second contender is blocked, kills the holder
without releasing the lease, waits for expiry, and proves takeover. It uses a
random lease name and removes only the row it created.

The database URL is read from ``SCHEDULER_REHEARSAL_DATABASE_URL`` so it never
appears in child-process arguments or the redacted JSON evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

DATABASE_URL_ENV = "SCHEDULER_REHEARSAL_DATABASE_URL"
SCHEMA_VERSION = "scheduler-failover-rehearsal-v1"
REQUIRED_CHECKS = (
    "holder_acquired",
    "concurrent_contender_blocked",
    "crashed_holder_did_not_release",
    "takeover_after_expiry",
    "takeover_released",
    "rehearsal_row_cleaned",
)


def _write_json(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"JSON 证据={output}")


def build_result(
    *,
    executed: bool,
    lease_seconds: int,
    checks: dict[str, bool] | None = None,
    takeover_wait_seconds: float | None = None,
    reason: str | None = None,
    error_type: str | None = None,
) -> dict[str, Any]:
    safe_checks = {name: bool((checks or {}).get(name)) for name in REQUIRED_CHECKS}
    passed = executed and all(safe_checks.values())
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "executed": executed,
        "passed": passed,
        "scope": "local_single_host",
        "database_backend": "postgresql" if executed else "not_executed",
        "process_isolation": executed,
        "lease_seconds": int(lease_seconds),
        "checks": safe_checks,
        "takeover_wait_seconds": (
            round(max(0.0, float(takeover_wait_seconds)), 3)
            if takeover_wait_seconds is not None
            else None
        ),
        "local_postgres_failover_verified": passed,
        "cross_host_failover_verified": False,
        "production_ready": False,
        "limitations": [
            "single_host_process_rehearsal",
            "not_cross_host",
            "not_managed_postgres_failover",
            "does_not_validate_real_metric_source",
        ],
    }
    if reason:
        payload["reason"] = reason
    if error_type:
        payload["error_type"] = error_type
    return payload


def _session_factory(database_url: str):
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    factory = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    return engine, factory


def _child_hold(
    *,
    lease_name: str,
    owner_token: str,
    ready_file: str,
    lease_seconds: int,
    hold_seconds: float,
) -> int:
    database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        return 3

    from app.services.scheduler_lease import SqlSchedulerLease

    engine, factory = _session_factory(database_url)
    try:
        acquired = SqlSchedulerLease(factory).acquire(
            lease_name,
            owner_token,
            lease_seconds=lease_seconds,
        )
        Path(ready_file).write_text(
            "acquired" if acquired else "blocked",
            encoding="utf-8",
        )
        if not acquired:
            return 2
        time.sleep(hold_seconds)
        SqlSchedulerLease(factory).release(lease_name, owner_token)
        return 0
    finally:
        engine.dispose()


def _wait_for_holder_ready(
    process: subprocess.Popen,
    ready_file: Path,
    *,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if ready_file.exists():
            return ready_file.read_text(encoding="utf-8").strip() == "acquired"
        if process.poll() is not None:
            return False
        time.sleep(0.05)
    return False


def _delete_rehearsal_row(
    factory: sessionmaker[Session],
    lease_name: str,
) -> bool:
    from app.models.scheduler import SchedulerLease

    session = factory()
    try:
        session.query(SchedulerLease).filter(
            SchedulerLease.name == lease_name
        ).delete(synchronize_session=False)
        session.commit()
        return session.get(SchedulerLease, lease_name) is None
    except Exception:
        session.rollback()
        return False
    finally:
        session.close()


def run_rehearsal(database_url: str, *, lease_seconds: int) -> dict[str, Any]:
    from app.services.scheduler_lease import SqlSchedulerLease

    engine, factory = _session_factory(database_url)
    lease_name = f"scheduler-rehearsal-{uuid.uuid4().hex[:24]}"
    holder_owner = f"holder-{uuid.uuid4().hex}"
    contender_owner = f"contender-{uuid.uuid4().hex}"
    checks = {name: False for name in REQUIRED_CHECKS}
    holder: subprocess.Popen | None = None
    takeover_wait_seconds: float | None = None

    try:
        if not inspect(engine).has_table("scheduler_leases"):
            return build_result(
                executed=True,
                lease_seconds=lease_seconds,
                checks=checks,
                reason="scheduler_leases_table_missing",
            )

        lease = SqlSchedulerLease(factory)
        with tempfile.TemporaryDirectory(prefix="scheduler-failover-") as tmp:
            ready_file = Path(tmp) / "holder.ready"
            child_env = dict(os.environ)
            child_env[DATABASE_URL_ENV] = database_url
            holder = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--_child-hold",
                    "--lease-name",
                    lease_name,
                    "--owner-token",
                    holder_owner,
                    "--ready-file",
                    str(ready_file),
                    "--lease-seconds",
                    str(lease_seconds),
                    "--hold-seconds",
                    str(lease_seconds + 30),
                ],
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            checks["holder_acquired"] = _wait_for_holder_ready(
                holder,
                ready_file,
                timeout_seconds=10.0,
            )
            if not checks["holder_acquired"]:
                return build_result(
                    executed=True,
                    lease_seconds=lease_seconds,
                    checks=checks,
                    reason="holder_failed_to_acquire",
                )

            contender_acquired = lease.acquire(
                lease_name,
                contender_owner,
                lease_seconds=lease_seconds,
            )
            checks["concurrent_contender_blocked"] = not contender_acquired
            if contender_acquired:
                lease.release(lease_name, contender_owner)

            holder.kill()
            holder.wait(timeout=10)
            holder = None

            immediate_takeover = lease.acquire(
                lease_name,
                contender_owner,
                lease_seconds=lease_seconds,
            )
            checks["crashed_holder_did_not_release"] = not immediate_takeover
            if immediate_takeover:
                checks["takeover_released"] = lease.release(
                    lease_name,
                    contender_owner,
                )
            else:
                takeover_started = time.monotonic()
                deadline = takeover_started + lease_seconds + 10
                while time.monotonic() < deadline:
                    if lease.acquire(
                        lease_name,
                        contender_owner,
                        lease_seconds=lease_seconds,
                    ):
                        checks["takeover_after_expiry"] = True
                        takeover_wait_seconds = time.monotonic() - takeover_started
                        break
                    time.sleep(0.1)
                if checks["takeover_after_expiry"]:
                    checks["takeover_released"] = lease.release(
                        lease_name,
                        contender_owner,
                    )

        checks["rehearsal_row_cleaned"] = _delete_rehearsal_row(factory, lease_name)
        return build_result(
            executed=True,
            lease_seconds=lease_seconds,
            checks=checks,
            takeover_wait_seconds=takeover_wait_seconds,
        )
    except Exception as exc:
        return build_result(
            executed=True,
            lease_seconds=lease_seconds,
            checks=checks,
            takeover_wait_seconds=takeover_wait_seconds,
            reason="rehearsal_failed",
            error_type=type(exc).__name__,
        )
    finally:
        if holder is not None and holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)
        if not checks["rehearsal_row_cleaned"]:
            checks["rehearsal_row_cleaned"] = _delete_rehearsal_row(factory, lease_name)
        engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="主动异常调度器 PostgreSQL 崩溃接管演练")
    parser.add_argument(
        "--allow-real-database-call",
        action="store_true",
        help=f"显式授权连接 {DATABASE_URL_ENV} 指向的隔离 PostgreSQL",
    )
    parser.add_argument("--lease-seconds", type=int, default=3)
    parser.add_argument("--json-out", help="输出不含连接串、owner 或租约名的 JSON 证据")
    parser.add_argument("--_child-hold", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--lease-name", help=argparse.SUPPRESS)
    parser.add_argument("--owner-token", help=argparse.SUPPRESS)
    parser.add_argument("--ready-file", help=argparse.SUPPRESS)
    parser.add_argument("--hold-seconds", type=float, default=30.0, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    lease_seconds = min(60, max(2, int(args.lease_seconds)))

    if args._child_hold:
        if not all((args.lease_name, args.owner_token, args.ready_file)):
            return 3
        return _child_hold(
            lease_name=args.lease_name,
            owner_token=args.owner_token,
            ready_file=args.ready_file,
            lease_seconds=lease_seconds,
            hold_seconds=max(float(args.hold_seconds), lease_seconds + 1),
        )

    if not args.allow_real_database_call:
        result = build_result(
            executed=False,
            lease_seconds=lease_seconds,
            reason="database_call_not_authorized",
        )
        print("真实数据库调用=SKIP (database_call_not_authorized)")
        _write_json(args.json_out, result)
        return 0

    database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        result = build_result(
            executed=False,
            lease_seconds=lease_seconds,
            reason="postgres_database_required",
        )
        print("演练=FAIL (需要通过环境变量配置 PostgreSQL)")
        _write_json(args.json_out, result)
        return 1

    result = run_rehearsal(database_url, lease_seconds=lease_seconds)
    print("=== 主动异常调度器 PostgreSQL 崩溃接管演练 ===")
    for name in REQUIRED_CHECKS:
        print(f"  [{'PASS' if result['checks'][name] else 'FAIL'}] {name}")
    print("本地 PostgreSQL 接管=" + ("PASS" if result["passed"] else "FAIL"))
    print("跨主机接管=NOT_PROVEN")
    print("production_ready=false")
    _write_json(args.json_out, result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
