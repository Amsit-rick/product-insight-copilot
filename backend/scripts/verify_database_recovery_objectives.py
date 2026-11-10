"""Verify database snapshot integrity and measured recovery objectives."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


MAX_EVIDENCE_BYTES = 512 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_SCHEMA = "database-migration-rollback-rehearsal-v1"


def _read_evidence(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"missing evidence file: {path.name}")
    if path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError(f"evidence file is too large: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid evidence file: {path.name}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != EXPECTED_SCHEMA:
        raise ValueError(f"evidence schema mismatch: {path.name}")
    return payload


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or number == float("inf") or number != number:
        return None
    return number


def verify_database_recovery_objectives(
    *,
    evidence_path: Path,
    expected_current_sha: str,
    expected_rollback_sha: str,
    max_rpo_seconds: float,
    max_rto_seconds: float,
    max_age_hours: int = 24,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(expected_current_sha) or not SHA_RE.fullmatch(
        expected_rollback_sha
    ):
        raise ValueError("expected release SHAs must be full lowercase Git SHAs")
    if max_rpo_seconds <= 0 or max_rto_seconds <= 0:
        raise ValueError("recovery objectives must be positive")
    if max_age_hours < 1:
        raise ValueError("max_age_hours must be at least 1")

    payload = _read_evidence(evidence_path.resolve())
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    rollback = (
        payload.get("rollback") if isinstance(payload.get("rollback"), dict) else {}
    )
    observed_now = (now or datetime.now(UTC)).astimezone(UTC)
    generated_at = _timestamp(payload.get("generated_at"))
    earliest = observed_now - timedelta(hours=max_age_hours)
    latest = observed_now + timedelta(minutes=5)

    snapshot_started = _timestamp(payload.get("snapshot_started_at"))
    snapshot_completed = _timestamp(payload.get("snapshot_completed_at"))
    failure_simulated = _timestamp(payload.get("failure_simulated_at"))
    restore_started = _timestamp(payload.get("restore_started_at"))
    restore_completed = _timestamp(payload.get("restore_completed_at"))
    timeline = (
        snapshot_started,
        snapshot_completed,
        failure_simulated,
        restore_started,
        restore_completed,
    )
    recovery_timeline_verified = all(value is not None for value in timeline)
    if recovery_timeline_verified:
        assert all(value is not None for value in timeline)
        recovery_timeline_verified = (
            snapshot_started
            <= snapshot_completed
            <= failure_simulated
            <= restore_started
            <= restore_completed
        )

    observed_rpo = _number(payload.get("observed_rpo_seconds"))
    observed_rto = _number(payload.get("observed_rto_seconds"))
    rpo_measurement_verified = False
    rto_measurement_verified = False
    if recovery_timeline_verified and observed_rpo is not None and observed_rto is not None:
        assert snapshot_completed and failure_simulated and restore_started and restore_completed
        measured_rpo = (failure_simulated - snapshot_completed).total_seconds()
        measured_rto = (restore_completed - restore_started).total_seconds()
        rpo_measurement_verified = abs(measured_rpo - observed_rpo) <= 0.05
        rto_measurement_verified = abs(measured_rto - observed_rto) <= 0.05

    snapshot_size = payload.get("snapshot_size_bytes")
    snapshot_integrity_verified = all(
        (
            payload.get("snapshot_created") is True,
            payload.get("snapshot_format") == "postgres_custom",
            isinstance(snapshot_size, int) and not isinstance(snapshot_size, bool),
            isinstance(snapshot_size, int) and snapshot_size > 0,
            HASH_RE.fullmatch(str(payload.get("snapshot_sha256") or "")) is not None,
        )
    )
    restore_guarantees_verified = all(
        payload.get(field) is True
        for field in (
            "restore_target_isolated",
            "restore_verified",
            "migration_history_restored",
            "business_data_restored",
            "candidate_schema_absent_after_restore",
        )
    )
    checks = {
        "execution_verified": payload.get("executed") is True
        and payload.get("passed") is True,
        "evidence_fresh": generated_at is not None
        and earliest <= generated_at <= latest,
        "release_sha_verified": current.get("source_git_sha") == expected_current_sha
        and rollback.get("source_git_sha") == expected_rollback_sha,
        "snapshot_integrity_verified": snapshot_integrity_verified,
        "recovery_timeline_verified": recovery_timeline_verified,
        "rpo_measurement_verified": rpo_measurement_verified,
        "rto_measurement_verified": rto_measurement_verified,
        "restore_guarantees_verified": restore_guarantees_verified,
        "rpo_objective_met": rpo_measurement_verified
        and observed_rpo is not None
        and observed_rpo <= max_rpo_seconds,
        "rto_objective_met": rto_measurement_verified
        and observed_rto is not None
        and observed_rto <= max_rto_seconds,
        "self_claims_safe": payload.get("production_ready") is False,
    }
    blockers = [name for name, passed in checks.items() if not passed]
    passed = not blockers
    enterprise_backup_governance_verified = passed and all(
        payload.get(field) is True
        for field in (
            "backup_encrypted",
            "key_management_verified",
            "offsite_copy_verified",
            "retention_policy_enforced",
            "restore_access_audited",
        )
    )
    return {
        "schema_version": "database-recovery-objectives-verification-v1",
        "generated_at": observed_now.isoformat(),
        "executed": True,
        "passed": passed,
        "checks": checks,
        "blockers": blockers,
        "current_source_git_sha": current.get("source_git_sha"),
        "rollback_source_git_sha": rollback.get("source_git_sha"),
        "snapshot_sha256": payload.get("snapshot_sha256"),
        "snapshot_size_bytes": snapshot_size,
        "objectives": {
            "rpo_target_seconds": float(max_rpo_seconds),
            "rto_target_seconds": float(max_rto_seconds),
            "observed_rpo_seconds": observed_rpo,
            "observed_rto_seconds": observed_rto,
        },
        "local_recovery_objectives_verified": passed,
        "enterprise_backup_governance_verified": enterprise_backup_governance_verified,
        "enterprise_controls": {
            field: payload.get(field) is True
            for field in (
                "backup_encrypted",
                "key_management_verified",
                "offsite_copy_verified",
                "retention_policy_enforced",
                "restore_access_audited",
            )
        },
        "production_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--expected-current-sha", required=True)
    parser.add_argument("--expected-rollback-sha", required=True)
    parser.add_argument("--max-rpo-seconds", type=float, required=True)
    parser.add_argument("--max-rto-seconds", type=float, required=True)
    parser.add_argument("--max-age-hours", type=int, default=24)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_database_recovery_objectives(
            evidence_path=args.evidence,
            expected_current_sha=args.expected_current_sha,
            expected_rollback_sha=args.expected_rollback_sha,
            max_rpo_seconds=args.max_rpo_seconds,
            max_rto_seconds=args.max_rto_seconds,
            max_age_hours=args.max_age_hours,
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"数据库恢复目标校验失败：{exc}")
        return 2
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
