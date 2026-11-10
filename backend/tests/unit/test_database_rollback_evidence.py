from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "backend" / "scripts" / "verify_database_rollback_evidence.py"
NOW = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
CURRENT_SHA = "a" * 40
ROLLBACK_SHA = "b" * 40
CURRENT_INVENTORY = "c" * 64
ROLLBACK_INVENTORY = "d" * 64


def _module():
    assert SCRIPT.is_file(), "database rollback evidence verifier is missing"
    spec = importlib.util.spec_from_file_location(
        "database_rollback_evidence_under_test", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_evidence(path: Path, *, generated_at: datetime = NOW) -> Path:
    payload = {
        "schema_version": "database-migration-rollback-rehearsal-v1",
        "generated_at": generated_at.isoformat(),
        "executed": True,
        "passed": True,
        "current": {
            "source_git_sha": CURRENT_SHA,
            "migration_count": 16,
            "migration_inventory_sha256": CURRENT_INVENTORY,
        },
        "rollback": {
            "source_git_sha": ROLLBACK_SHA,
            "migration_count": 15,
            "migration_inventory_sha256": ROLLBACK_INVENTORY,
        },
        "snapshot_created": True,
        "candidate_migration_applied": True,
        "schema_change_verified": True,
        "post_migration_write_verified": True,
        "baseline_schema_sha256": "1" * 64,
        "candidate_schema_sha256": "2" * 64,
        "restored_schema_sha256": "1" * 64,
        "restore_verified": True,
        "migration_history_restored": True,
        "business_data_restored": True,
        "candidate_schema_absent_after_restore": True,
        "database_migration_change_detected": True,
        "database_migration_rollback_required": True,
        "database_migration_rollback_exercised": True,
        "database_release_safety_verified": True,
        "database_release_basis": "snapshot_restore_rehearsed",
        "production_ready": False,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _verify(module, path: Path, **overrides):
    inputs = {
        "evidence_path": path,
        "expected_current_sha": CURRENT_SHA,
        "expected_rollback_sha": ROLLBACK_SHA,
        "expected_current_inventory_sha256": CURRENT_INVENTORY,
        "expected_rollback_inventory_sha256": ROLLBACK_INVENTORY,
        "expected_current_migration_count": 16,
        "expected_rollback_migration_count": 15,
        "max_age_hours": 24,
        "now": NOW,
    }
    inputs.update(overrides)
    return module.verify_database_rollback_evidence(**inputs)


def test_valid_snapshot_restore_evidence_passes(tmp_path: Path):
    module = _module()
    result = _verify(module, _write_evidence(tmp_path / "rollback.json"))

    assert result["passed"] is True
    assert result["blockers"] == []
    assert all(result["checks"].values())
    assert result["database_release_safety_verified"] is True
    assert result["production_ready"] is False


def test_release_or_inventory_mismatch_fails_closed(tmp_path: Path):
    module = _module()
    evidence = _write_evidence(tmp_path / "rollback.json")

    result = _verify(module, evidence, expected_current_sha="e" * 40)
    assert result["checks"]["release_sha_verified"] is False
    assert "release_sha_verified" in result["blockers"]

    result = _verify(
        module,
        evidence,
        expected_rollback_inventory_sha256="f" * 64,
    )
    assert result["checks"]["migration_inventory_verified"] is False
    assert "migration_inventory_verified" in result["blockers"]


def test_stale_or_self_authorizing_evidence_fails_closed(tmp_path: Path):
    module = _module()
    evidence = _write_evidence(
        tmp_path / "rollback.json", generated_at=NOW - timedelta(hours=25)
    )

    result = _verify(module, evidence)
    assert result["checks"]["evidence_fresh"] is False

    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["generated_at"] = NOW.isoformat()
    payload["production_ready"] = True
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    result = _verify(module, evidence)
    assert result["checks"]["self_claims_safe"] is False
    assert result["passed"] is False


def test_missing_restore_guarantee_fails_closed(tmp_path: Path):
    module = _module()
    evidence = _write_evidence(tmp_path / "rollback.json")
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["business_data_restored"] = False
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    result = _verify(module, evidence)

    assert result["checks"]["restore_guarantees_verified"] is False
    assert "restore_guarantees_verified" in result["blockers"]


def test_schema_fingerprint_must_return_to_baseline(tmp_path: Path):
    module = _module()
    evidence = _write_evidence(tmp_path / "rollback.json")
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["restored_schema_sha256"] = "3" * 64
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    result = _verify(module, evidence)

    assert result["checks"]["schema_fingerprints_verified"] is False
    assert "schema_fingerprints_verified" in result["blockers"]

