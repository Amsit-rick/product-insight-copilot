from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.package_delivery_evidence import build_delivery_evidence_manifest, write_manifest_atomic


NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def _write_pair(directory: Path, *, source_time: datetime | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": "after-sales-release-readiness-v5",
        "generated_at": NOW.isoformat(),
        "release_evidence": {"commit_sha": "abc123def456"},
    }
    check = {
        "schema_version": "after-sales-delivery-check-v5",
        "generated_at": (NOW + timedelta(seconds=1)).isoformat(),
        "source_report_generated_at": (source_time or NOW).isoformat(),
    }
    (directory / "after-sales-release-readiness.json").write_text(json.dumps(report), encoding="utf-8")
    (directory / "after-sales-delivery-check.json").write_text(json.dumps(check), encoding="utf-8")


def test_manifest_binds_release_and_both_file_hashes(tmp_path: Path):
    _write_pair(tmp_path)

    manifest = build_delivery_evidence_manifest(tmp_path, generated_at=NOW + timedelta(seconds=2))

    assert manifest["schema_version"] == "delivery-evidence-manifest-v1"
    assert manifest["release_sha"] == "abc123def456"
    assert len(manifest["bundle_id"]) == 64
    for filename in ("after-sales-release-readiness.json", "after-sales-delivery-check.json"):
        expected = hashlib.sha256((tmp_path / filename).read_bytes()).hexdigest()
        assert manifest["files"][filename]["sha256"] == expected


def test_manifest_rejects_cross_batch_delivery_check(tmp_path: Path):
    _write_pair(tmp_path, source_time=NOW - timedelta(minutes=1))

    with pytest.raises(ValueError, match="不属于当前预发布报告批次"):
        build_delivery_evidence_manifest(tmp_path, generated_at=NOW + timedelta(seconds=2))


def test_atomic_writer_replaces_manifest_without_temp_residue(tmp_path: Path):
    _write_pair(tmp_path)
    target = tmp_path / "delivery-evidence-manifest.json"
    target.write_text("old", encoding="utf-8")
    manifest = build_delivery_evidence_manifest(tmp_path, generated_at=NOW + timedelta(seconds=2))

    written = write_manifest_atomic(manifest, target)

    assert written == target.resolve()
    assert json.loads(target.read_text(encoding="utf-8"))["bundle_id"] == manifest["bundle_id"]
    assert not list(tmp_path.glob(".delivery-evidence-manifest.json.*.tmp"))


def _write_remote_ci_attachments(directory: Path) -> tuple[str, ...]:
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
    for name, payload in attachments.items():
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    return tuple(attachments)


def test_manifest_binds_remote_ci_attachments_without_promoting_production(tmp_path: Path):
    _write_pair(tmp_path)
    attachment_names = _write_remote_ci_attachments(tmp_path)

    manifest = build_delivery_evidence_manifest(
        tmp_path,
        generated_at=NOW + timedelta(seconds=2),
        attachment_names=attachment_names,
    )

    assert set(attachment_names).issubset(manifest["files"])
    for name in attachment_names:
        assert manifest["files"][name]["sha256"] == hashlib.sha256(
            (tmp_path / name).read_bytes()
        ).hexdigest()


def test_manifest_rejects_remote_ci_attachment_that_claims_production(tmp_path: Path):
    _write_pair(tmp_path)
    attachment_names = _write_remote_ci_attachments(tmp_path)
    path = tmp_path / "remote-ci-production-shape.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["production_ready"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="不得声明生产就绪"):
        build_delivery_evidence_manifest(
            tmp_path,
            generated_at=NOW + timedelta(seconds=2),
            attachment_names=attachment_names,
        )
