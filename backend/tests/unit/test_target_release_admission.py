from __future__ import annotations

import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "backend" / "scripts" / "verify_target_release_admission.py"
SPEC = importlib.util.spec_from_file_location("target_release_admission_under_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _evidence(tmp_path: Path, *, enterprise: bool, mixed_sha: bool = False):
    generated_at = NOW.isoformat()
    runner = _write(
        tmp_path / "runner-readiness.json",
        {
            "schema_version": "self-hosted-runner-readiness-v1",
            "generated_at": generated_at,
            "executed": True,
            "passed": True,
            "source_git_sha": SHA,
            "supply_chain_runner_ready": True,
            "production_ready": False,
        },
    )
    attestation = _write(
        tmp_path / "image-attestations.json",
        {
            "schema_version": "image-attestation-verification-v1",
            "generated_at": generated_at,
            "executed": True,
            "passed": True,
            "source_git_sha": SHA,
            "index_digest": DIGEST,
            "platform_manifest_digest": DIGEST,
            "provenance_verified": True,
            "sbom_verified": True,
            "enterprise_registry_verified": enterprise,
            "cross_host_verified": enterprise,
            "production_ready": False,
        },
    )
    signature = _write(
        tmp_path / "image-signature.json",
        {
            "schema_version": "image-signature-verification-v1",
            "generated_at": generated_at,
            "executed": True,
            "passed": True,
            "source_git_sha": SHA,
            "image_digest": DIGEST,
            "signature_verified": True,
            "release_annotation_verified": True,
            "enterprise_registry_verified": enterprise,
            "cross_host_verified": enterprise,
            "production_ready": False,
        },
    )
    bundle = _write(
        tmp_path / "supply-chain-bundle.json",
        {
            "schema_version": "self-hosted-supply-chain-bundle-v1",
            "generated_at": generated_at,
            "executed": True,
            "passed": True,
            "release_sha": SHA,
            "image_digest": DIGEST,
            "files": {
                "runner-readiness.json": {"sha256": hashlib.sha256(runner.read_bytes()).hexdigest()},
                "image-attestations.json": {
                    "sha256": hashlib.sha256(attestation.read_bytes()).hexdigest()
                },
                "image-signature.json": {
                    "sha256": hashlib.sha256(signature.read_bytes()).hexdigest()
                },
            },
            "provenance_verified": True,
            "sbom_verified": True,
            "signature_verified": True,
            "production_ready": False,
        },
    )
    secure_registry = _write(
        tmp_path / "secure-registry.json",
        {
            "schema_version": "secure-registry-rehearsal-v1",
            "generated_at": generated_at,
            "executed": True,
            "passed": True,
            "source_git_sha": SHA,
            "tls_verified": True,
            "untrusted_tls_rejected": True,
            "basic_auth_required": True,
            "wrong_credentials_rejected": True,
            "push_verified": True,
            "digest_pull_verified": True,
            "enterprise_registry_verified": enterprise,
            "production_ready": False,
        },
    )
    rollout = _write(
        tmp_path / "rollout.json",
        {
            "schema_version": "registry-rollout-rollback-evidence-v1",
            "generated_at": generated_at,
            "executed": True,
            "passed": True,
            "current": {
                "source_git_sha": "c" * 40 if mixed_sha else SHA,
                "digest": DIGEST,
                "registry_published": True,
                "registry_pull_verified": True,
                "deployment_verified": True,
                "queue_verified": True,
            },
            "database_migration_change_detected": False,
            "database_migration_rollback_required": False,
            "database_migration_rollback_exercised": False,
            "database_release_safety_verified": True,
            "enterprise_registry_verified": enterprise,
            "cross_host_verified": enterprise,
            "production_ready": False,
        },
    )
    target = _write(
        tmp_path / "target-environment.json",
        {
            "schema_version": "target-environment-probe-v1",
            "generated_at": generated_at,
            "executed": True,
            "passed": True,
            "runtime_shape_ready": True,
            "target_environment_ready": enterprise,
            "cross_host_or_target_environment_verified": enterprise,
            "expected_release_sha": SHA,
            "production_ready": False,
        },
    )
    database_recovery = _write(
        tmp_path / "database-recovery-objectives.json",
        {
            "schema_version": "database-recovery-objectives-verification-v1",
            "generated_at": generated_at,
            "executed": True,
            "passed": True,
            "current_source_git_sha": SHA,
            "rollback_source_git_sha": "d" * 40,
            "local_recovery_objectives_verified": True,
            "enterprise_backup_governance_verified": enterprise,
            "production_ready": False,
        },
    )
    return {
        "runner_readiness_path": runner,
        "attestation_path": attestation,
        "signature_path": signature,
        "bundle_path": bundle,
        "secure_registry_path": secure_registry,
        "rollout_path": rollout,
        "target_environment_path": target,
        "database_recovery_path": database_recovery,
        "expected_release_sha": SHA,
        "now": NOW,
    }


def test_consistent_local_evidence_is_visible_but_does_not_grant_admission(tmp_path: Path):
    result = MODULE.verify_target_release_admission(
        **_evidence(tmp_path, enterprise=False)
    )

    assert result["checks"]["bundle_hashes_verified"] is True
    assert result["checks"]["self_claims_safe"] is True
    assert result["checks"]["supply_chain_verified"] is True
    assert result["checks"]["secure_registry_controls_verified"] is True
    assert result["checks"]["local_rollout_verified"] is True
    assert result["machine_admission_ready"] is False
    assert "enterprise_registry_verified" in result["blockers"]
    assert "cross_host_verified" in result["blockers"]
    assert "enterprise_backup_governance_verified" in result["blockers"]
    assert result["release_authorized"] is False
    assert result["production_ready"] is False
    assert len(result["admission_bundle_id"]) == 64
    assert set(result["evidence_files"]) == {
        "runner_readiness",
        "attestation",
        "signature",
        "bundle",
        "secure_registry",
        "rollout",
        "target_environment",
        "database_recovery",
    }


def test_mixed_release_or_tampered_evidence_fails_closed(tmp_path: Path):
    inputs = _evidence(tmp_path, enterprise=False, mixed_sha=True)
    result = MODULE.verify_target_release_admission(**inputs)
    assert result["checks"]["release_sha_verified"] is False

    inputs["attestation_path"].write_text("{}", encoding="utf-8")
    try:
        MODULE.verify_target_release_admission(**inputs)
    except ValueError as exc:
        assert "schema mismatch" in str(exc)
    else:
        raise AssertionError("tampered evidence must be rejected")


def test_enterprise_machine_evidence_still_requires_human_approval(tmp_path: Path):
    result = MODULE.verify_target_release_admission(
        **_evidence(tmp_path, enterprise=True)
    )

    assert result["machine_admission_ready"] is True
    assert result["status"] == "machine_ready_awaiting_human_approval"
    assert result["blockers"] == []
    assert result["human_approval_required"] is True
    assert result["release_authorized"] is False
    assert result["production_ready"] is False


def test_evidence_cannot_self_declare_production_ready(tmp_path: Path):
    inputs = _evidence(tmp_path, enterprise=True)
    target = json.loads(inputs["target_environment_path"].read_text(encoding="utf-8"))
    target["production_ready"] = True
    inputs["target_environment_path"].write_text(json.dumps(target), encoding="utf-8")

    result = MODULE.verify_target_release_admission(**inputs)

    assert result["checks"]["self_claims_safe"] is False
    assert result["machine_admission_ready"] is False
    assert "self_claims_safe" in result["blockers"]


def test_database_release_safety_is_required_for_local_rollout(tmp_path: Path):
    inputs = _evidence(tmp_path, enterprise=False)
    rollout = json.loads(inputs["rollout_path"].read_text(encoding="utf-8"))
    rollout["database_release_safety_verified"] = False
    inputs["rollout_path"].write_text(json.dumps(rollout), encoding="utf-8")

    result = MODULE.verify_target_release_admission(**inputs)

    assert result["checks"]["local_rollout_verified"] is False
    assert "local_rollout_verified" in result["blockers"]


def test_database_recovery_objectives_are_required_for_admission(tmp_path: Path):
    inputs = _evidence(tmp_path, enterprise=True)
    recovery = json.loads(inputs["database_recovery_path"].read_text(encoding="utf-8"))
    recovery["local_recovery_objectives_verified"] = False
    inputs["database_recovery_path"].write_text(json.dumps(recovery), encoding="utf-8")

    result = MODULE.verify_target_release_admission(**inputs)

    assert result["checks"]["database_recovery_objectives_verified"] is False
    assert "database_recovery_objectives_verified" in result["blockers"]
