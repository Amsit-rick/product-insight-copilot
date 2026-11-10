from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.verify_delivery_evidence_bundle import verify_delivery_evidence_bundle


NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
SHA = "a" * 40


def _write_bundle(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": "after-sales-release-readiness-v5",
        "generated_at": NOW.isoformat(),
        "decision": "not_ready_machine_gates",
        "production_ready": False,
        "release_evidence": {
            "commit_sha": SHA,
            "machine_gates_ready": False,
            "target_release_admission_verified": False,
        },
        "release_decision": {"status": "missing", "gate_status": "blocked_external"},
        "local_evidence": {"source_control": {"commit_sha": SHA}},
        "external_gates": [
            {"key": key, "status": "blocked_external", "required": "外部验收"}
            for key in (
                "fact_platform_sandbox",
                "metric_source",
                "message_channel",
                "production_infrastructure",
                "real_model_holdout",
                "usability_test",
            )
        ],
    }
    check = {
        "schema_version": "after-sales-delivery-check-v5",
        "generated_at": NOW.isoformat(),
        "source_report_generated_at": NOW.isoformat(),
        "local_demo_ready": True,
        "candidate_ready": False,
        "production_ready": False,
        "candidate_blockers": ["machine_gates"],
        "release_checks": [],
    }
    report_path = directory / "after-sales-release-readiness.json"
    check_path = directory / "after-sales-delivery-check.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    check_path.write_text(json.dumps(check), encoding="utf-8")
    files = {
        report_path.name: {
            "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "generated_at": NOW.isoformat(),
        },
        check_path.name: {
            "sha256": hashlib.sha256(check_path.read_bytes()).hexdigest(),
            "generated_at": NOW.isoformat(),
        },
    }
    manifest = {
        "schema_version": "delivery-evidence-manifest-v1",
        "generated_at": NOW.isoformat(),
        "release_sha": SHA,
        "bundle_id": "b" * 64,
        "files": files,
    }
    (directory / "delivery-evidence-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_verifier_accepts_integrity_valid_bundle_without_claiming_production_ready(tmp_path: Path):
    _write_bundle(tmp_path)

    result = verify_delivery_evidence_bundle(tmp_path, expected_release_sha=SHA, now=NOW)

    assert result["verified"] is True
    assert result["trusted_production_ready"] is False
    assert result["release_sha_verified"] is True
    assert result["reasons"] == []


def test_verifier_rejects_wrong_runtime_sha_and_tampered_file(tmp_path: Path):
    _write_bundle(tmp_path)

    mismatch = verify_delivery_evidence_bundle(tmp_path, expected_release_sha="c" * 40, now=NOW)
    assert mismatch["verified"] is False
    assert "release_commit_mismatch" in mismatch["reasons"]

    report = tmp_path / "after-sales-release-readiness.json"
    report.write_text(report.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    tampered = verify_delivery_evidence_bundle(tmp_path, expected_release_sha=SHA, now=NOW)
    assert tampered["verified"] is False
    assert "evidence_bundle_hash_mismatch" in tampered["reasons"]


def test_verifier_requires_expected_release_sha(tmp_path: Path):
    _write_bundle(tmp_path)

    result = verify_delivery_evidence_bundle(tmp_path, expected_release_sha="", now=NOW)

    assert result["verified"] is False
    assert result["reasons"] == ["expected_release_sha_missing"]


def test_verifier_reports_complete_remote_ci_evidence_without_promoting_release(tmp_path: Path):
    _write_bundle(tmp_path)
    attachments = {
        "remote-ci-object-storage.json": {
            "schema_version": "object-storage-probe-v1",
            "generated_at": NOW.isoformat(),
            "executed": True,
            "passed": True,
            "shared_storage_ready": True,
            "production_ready": False,
        },
        "remote-ci-production-shape.json": {
            "schema_version": "target-environment-probe-v1",
            "generated_at": NOW.isoformat(),
            "expected_env": "test",
            "executed": True,
            "passed": True,
            "runtime_shape_ready": True,
            "target_environment_ready": False,
            "cross_host_or_target_environment_verified": False,
            "production_ready": False,
        },
        "remote-ci-scheduler-failover.json": {
            "schema_version": "scheduler-failover-rehearsal-v1",
            "generated_at": NOW.isoformat(),
            "executed": True,
            "passed": True,
            "scope": "local_single_host",
            "local_postgres_failover_verified": True,
            "cross_host_failover_verified": False,
            "production_ready": False,
        },
        "remote-ci-image-attestations.json": {
            "schema_version": "image-attestation-verification-v1",
            "generated_at": NOW.isoformat(),
            "executed": True,
            "passed": True,
            "scope": "remote_ci_local_registry",
            "provenance_verified": True,
            "sbom_verified": True,
            "signature_verified": False,
            "enterprise_registry_verified": False,
            "cross_host_verified": False,
            "production_ready": False,
        },
        "remote-ci-image-signature.json": {
            "schema_version": "image-signature-verification-v1",
            "generated_at": NOW.isoformat(),
            "executed": True,
            "passed": True,
            "scope": "remote_ci_local_registry",
            "signature_verified": True,
            "release_annotation_verified": True,
            "transparency_log_verified": False,
            "keyless_identity_verified": False,
            "kms_verified": False,
            "key_management": "ephemeral_local_key",
            "enterprise_registry_verified": False,
            "cross_host_verified": False,
            "production_ready": False,
        },
    }
    manifest_path = tmp_path / "delivery-evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    roles = {
        "remote-ci-object-storage.json": "remote_ci_object_storage",
        "remote-ci-production-shape.json": "remote_ci_runtime_shape",
        "remote-ci-scheduler-failover.json": "remote_ci_scheduler_failover",
        "remote-ci-image-attestations.json": "remote_ci_image_attestations",
        "remote-ci-image-signature.json": "remote_ci_image_signature",
    }
    for name, payload in attachments.items():
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        manifest["files"][name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "generated_at": NOW.isoformat(),
            "schema_version": payload["schema_version"],
            "role": roles[name],
        }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_delivery_evidence_bundle(tmp_path, expected_release_sha=SHA, now=NOW)

    assert result["verified"] is True
    assert result["attachment_count"] == 5
    assert result["remote_ci_evidence_complete"] is True
    assert result["trusted_production_ready"] is False
