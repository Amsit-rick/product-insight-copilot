from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "backend" / "scripts" / "verify_database_recovery_objectives.py"
NOW = datetime(2026, 11, 10, 16, 0, tzinfo=UTC)
CURRENT_SHA = "a" * 40
ROLLBACK_SHA = "b" * 40


def _module():
    assert SCRIPT.is_file(), "database recovery objective verifier is missing"
    spec = importlib.util.spec_from_file_location(
        "database_recovery_objectives_under_test", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_evidence(path: Path, *, enterprise: bool = False) -> Path:
    snapshot_started = NOW - timedelta(seconds=12)
    snapshot_completed = NOW - timedelta(seconds=10)
    failure_simulated = NOW - timedelta(seconds=7)
    restore_started = NOW - timedelta(seconds=6)
    restore_completed = NOW - timedelta(seconds=2)
    payload = {
        "schema_version": "database-migration-rollback-rehearsal-v1",
        "generated_at": NOW.isoformat(),
        "executed": True,
        "passed": True,
        "current": {"source_git_sha": CURRENT_SHA},
        "rollback": {"source_git_sha": ROLLBACK_SHA},
        "snapshot_created": True,
        "snapshot_format": "postgres_custom",
        "snapshot_sha256": "c" * 64,
        "snapshot_size_bytes": 4096,
        "snapshot_started_at": snapshot_started.isoformat(),
        "snapshot_completed_at": snapshot_completed.isoformat(),
        "failure_simulated_at": failure_simulated.isoformat(),
        "restore_started_at": restore_started.isoformat(),
        "restore_completed_at": restore_completed.isoformat(),
        "observed_rpo_seconds": 3.0,
        "observed_rto_seconds": 4.0,
        "restore_target_isolated": True,
        "restore_verified": True,
        "migration_history_restored": True,
        "business_data_restored": True,
        "candidate_schema_absent_after_restore": True,
        "backup_encrypted": enterprise,
        "key_management_verified": enterprise,
        "offsite_copy_verified": enterprise,
        "retention_policy_enforced": enterprise,
        "restore_access_audited": enterprise,
        "production_ready": False,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _verify(module, path: Path, **overrides):
    inputs = {
        "evidence_path": path,
        "expected_current_sha": CURRENT_SHA,
        "expected_rollback_sha": ROLLBACK_SHA,
        "max_rpo_seconds": 300.0,
        "max_rto_seconds": 900.0,
        "max_age_hours": 24,
        "now": NOW,
    }
    inputs.update(overrides)
    return module.verify_database_recovery_objectives(**inputs)


def test_local_recovery_objectives_pass_without_claiming_enterprise_governance(
    tmp_path: Path,
):
    module = _module()
    result = _verify(module, _write_evidence(tmp_path / "recovery.json"))

    assert result["passed"] is True
    assert result["local_recovery_objectives_verified"] is True
    assert result["enterprise_backup_governance_verified"] is False
    assert result["objectives"] == {
        "rpo_target_seconds": 300.0,
        "rto_target_seconds": 900.0,
        "observed_rpo_seconds": 3.0,
        "observed_rto_seconds": 4.0,
    }
    assert result["production_ready"] is False


def test_snapshot_integrity_or_timeline_tampering_fails_closed(tmp_path: Path):
    module = _module()
    evidence = _write_evidence(tmp_path / "recovery.json")
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["snapshot_sha256"] = "not-a-hash"
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    result = _verify(module, evidence)
    assert result["checks"]["snapshot_integrity_verified"] is False
    assert result["passed"] is False

    payload["snapshot_sha256"] = "c" * 64
    payload["restore_completed_at"] = (NOW - timedelta(seconds=8)).isoformat()
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    result = _verify(module, evidence)
    assert result["checks"]["recovery_timeline_verified"] is False


def test_rpo_or_rto_above_objective_fails_closed(tmp_path: Path):
    module = _module()
    evidence = _write_evidence(tmp_path / "recovery.json")

    result = _verify(module, evidence, max_rpo_seconds=2.0)
    assert result["checks"]["rpo_objective_met"] is False
    assert "rpo_objective_met" in result["blockers"]

    result = _verify(module, evidence, max_rto_seconds=3.0)
    assert result["checks"]["rto_objective_met"] is False
    assert "rto_objective_met" in result["blockers"]


def test_release_binding_freshness_and_self_claims_are_enforced(tmp_path: Path):
    module = _module()
    evidence = _write_evidence(tmp_path / "recovery.json")

    result = _verify(module, evidence, expected_current_sha="d" * 40)
    assert result["checks"]["release_sha_verified"] is False

    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["generated_at"] = (NOW - timedelta(hours=25)).isoformat()
    payload["production_ready"] = True
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    result = _verify(module, evidence)
    assert result["checks"]["evidence_fresh"] is False
    assert result["checks"]["self_claims_safe"] is False


def test_enterprise_governance_requires_all_independent_controls(tmp_path: Path):
    module = _module()
    evidence = _write_evidence(tmp_path / "recovery.json", enterprise=True)

    result = _verify(module, evidence)
    assert result["passed"] is True
    assert result["enterprise_backup_governance_verified"] is True

    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["offsite_copy_verified"] = False
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    result = _verify(module, evidence)
    assert result["passed"] is True
    assert result["enterprise_backup_governance_verified"] is False
