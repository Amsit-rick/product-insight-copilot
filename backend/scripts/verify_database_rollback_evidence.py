"""Verify database migration rollback rehearsal evidence before rollout."""

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
SCHEMA_VERSION = "database-migration-rollback-rehearsal-v1"


def _read_evidence(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError("database rollback evidence is missing")
    if path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError("database rollback evidence is too large")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("database rollback evidence is invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("database rollback evidence schema mismatch")
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


def _valid_sha(value: str) -> bool:
    return SHA_RE.fullmatch(value) is not None


def _valid_hash(value: str) -> bool:
    return HASH_RE.fullmatch(value) is not None


def verify_database_rollback_evidence(
    *,
    evidence_path: Path,
    expected_current_sha: str,
    expected_rollback_sha: str,
    expected_current_inventory_sha256: str,
    expected_rollback_inventory_sha256: str,
    expected_current_migration_count: int,
    expected_rollback_migration_count: int,
    max_age_hours: int = 24,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not _valid_sha(expected_current_sha) or not _valid_sha(expected_rollback_sha):
        raise ValueError("expected release SHAs must be full lowercase Git SHAs")
    if not _valid_hash(expected_current_inventory_sha256) or not _valid_hash(
        expected_rollback_inventory_sha256
    ):
        raise ValueError("expected migration inventory hashes must be SHA-256")
    if expected_current_migration_count < 1 or expected_rollback_migration_count < 1:
        raise ValueError("expected migration counts must be positive")
    if max_age_hours < 1:
        raise ValueError("max_age_hours must be at least 1")

    payload = _read_evidence(evidence_path.resolve())
    current = payload.get("current") if isinstance(payload.get("current"), dict) else {}
    rollback = (
        payload.get("rollback") if isinstance(payload.get("rollback"), dict) else {}
    )
    generated_at = _timestamp(payload.get("generated_at"))
    observed_now = (now or datetime.now(UTC)).astimezone(UTC)
    earliest = observed_now - timedelta(hours=max_age_hours)
    latest = observed_now + timedelta(minutes=5)

    release_sha_verified = all(
        (
            current.get("source_git_sha") == expected_current_sha,
            rollback.get("source_git_sha") == expected_rollback_sha,
        )
    )
    migration_inventory_verified = all(
        (
            current.get("migration_inventory_sha256")
            == expected_current_inventory_sha256,
            rollback.get("migration_inventory_sha256")
            == expected_rollback_inventory_sha256,
            current.get("migration_count") == expected_current_migration_count,
            rollback.get("migration_count") == expected_rollback_migration_count,
        )
    )
    migration_change_verified = all(
        (
            expected_current_inventory_sha256
            != expected_rollback_inventory_sha256,
            expected_current_migration_count != expected_rollback_migration_count,
            payload.get("database_migration_change_detected") is True,
            payload.get("database_migration_rollback_required") is True,
        )
    )
    restore_guarantees_verified = all(
        payload.get(field) is True
        for field in (
            "snapshot_created",
            "candidate_migration_applied",
            "schema_change_verified",
            "post_migration_write_verified",
            "restore_verified",
            "migration_history_restored",
            "business_data_restored",
            "candidate_schema_absent_after_restore",
            "database_migration_rollback_exercised",
            "database_release_safety_verified",
        )
    ) and payload.get("database_release_basis") == "snapshot_restore_rehearsed"
    baseline_schema_sha = str(payload.get("baseline_schema_sha256") or "")
    candidate_schema_sha = str(payload.get("candidate_schema_sha256") or "")
    restored_schema_sha = str(payload.get("restored_schema_sha256") or "")
    schema_fingerprints_verified = all(
        _valid_hash(value)
        for value in (
            baseline_schema_sha,
            candidate_schema_sha,
            restored_schema_sha,
        )
    ) and all(
        (
            baseline_schema_sha != candidate_schema_sha,
            restored_schema_sha == baseline_schema_sha,
        )
    )
    checks = {
        "execution_verified": payload.get("executed") is True
        and payload.get("passed") is True,
        "evidence_fresh": generated_at is not None
        and earliest <= generated_at <= latest,
        "release_sha_verified": release_sha_verified,
        "migration_inventory_verified": migration_inventory_verified,
        "migration_change_verified": migration_change_verified,
        "schema_fingerprints_verified": schema_fingerprints_verified,
        "restore_guarantees_verified": restore_guarantees_verified,
        "self_claims_safe": payload.get("production_ready") is False,
    }
    blockers = [name for name, passed in checks.items() if not passed]
    passed = not blockers
    return {
        "schema_version": "database-migration-rollback-verification-v1",
        "generated_at": observed_now.isoformat(),
        "passed": passed,
        "checks": checks,
        "blockers": blockers,
        "current_source_git_sha": current.get("source_git_sha"),
        "rollback_source_git_sha": rollback.get("source_git_sha"),
        "database_release_safety_verified": passed,
        "production_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="校验数据库迁移快照恢复演练证据与 rollout 候选绑定"
    )
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--expected-current-sha", required=True)
    parser.add_argument("--expected-rollback-sha", required=True)
    parser.add_argument("--expected-current-inventory-sha256", required=True)
    parser.add_argument("--expected-rollback-inventory-sha256", required=True)
    parser.add_argument("--expected-current-migration-count", type=int, required=True)
    parser.add_argument("--expected-rollback-migration-count", type=int, required=True)
    parser.add_argument("--max-age-hours", type=int, default=24)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_database_rollback_evidence(
            evidence_path=args.evidence,
            expected_current_sha=args.expected_current_sha,
            expected_rollback_sha=args.expected_rollback_sha,
            expected_current_inventory_sha256=args.expected_current_inventory_sha256,
            expected_rollback_inventory_sha256=args.expected_rollback_inventory_sha256,
            expected_current_migration_count=args.expected_current_migration_count,
            expected_rollback_migration_count=args.expected_rollback_migration_count,
            max_age_hours=args.max_age_hours,
        )
    except ValueError as exc:
        print(f"数据库回滚证据校验失败：{exc}")
        return 2
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    print(encoded)
    if args.json_out:
        output = args.json_out.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
        print(f"数据库回滚证据校验摘要={output}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

