"""正式交付证据快照：只读、失败关闭、版本绑定。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)


def _write_evidence(
    directory: Path,
    *,
    generated_at: datetime = NOW,
    commit_sha: str = "abc123def456",
    production_ready: bool = False,
    gates: list[dict] | None = None,
    write_manifest: bool = True,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    default_gate_status = "verified_external" if production_ready else "blocked_external"
    report = {
        "schema_version": "after-sales-release-readiness-v5",
        "generated_at": generated_at.isoformat(),
        "decision": "ready_for_release" if production_ready else "not_ready_machine_gates",
        "production_ready": production_ready,
        "release_evidence": {
            "machine_gates_ready": production_ready,
            "target_release_admission_verified": production_ready,
        },
        "release_decision": {
            "status": "approved" if production_ready else "missing",
            "gate_status": "verified_human" if production_ready else "blocked_external",
        },
        "local_evidence": {"source_control": {"commit_sha": commit_sha}},
        "external_gates": gates if gates is not None else [
            {"key": key, "status": default_gate_status, "required": "正式证据"}
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
    delivery = {
        "schema_version": "after-sales-delivery-check-v5",
        "generated_at": generated_at.isoformat(),
        "source_report_generated_at": generated_at.isoformat(),
        "local_demo_ready": True,
        "candidate_ready": production_ready,
        "production_ready": production_ready,
        "candidate_blockers": [] if production_ready else ["machine_gates"],
        "release_checks": [
            {
                "name": "机器发布门禁",
                "status": "PASS" if production_ready else "FAIL",
                "detail": "已通过" if production_ready else "机器门禁尚未全部通过",
            }
        ],
    }
    report_path = directory / "after-sales-release-readiness.json"
    check_path = directory / "after-sales-delivery-check.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8"
    )
    check_path.write_text(
        json.dumps(delivery, ensure_ascii=False), encoding="utf-8"
    )
    if write_manifest:
        manifest = {
            "schema_version": "delivery-evidence-manifest-v1",
            "generated_at": generated_at.isoformat(),
            "release_sha": commit_sha,
            "files": {
                report_path.name: {
                    "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
                    "generated_at": generated_at.isoformat(),
                },
                check_path.name: {
                    "sha256": hashlib.sha256(check_path.read_bytes()).hexdigest(),
                    "generated_at": generated_at.isoformat(),
                },
            },
        }
        (directory / "delivery-evidence-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )


def _snapshot(directory: Path, **kwargs):
    from app.services.delivery_readiness import build_delivery_readiness_snapshot

    return build_delivery_readiness_snapshot(directory, now=NOW, **kwargs)


def test_missing_evidence_fails_closed(tmp_path: Path):
    result = _snapshot(tmp_path, expected_release_sha="abc123def456")

    assert result["status"] == "missing"
    assert result["evidence_valid"] is False
    assert result["trusted_production_ready"] is False
    assert set(result["reasons"]) == {
        "evidence_manifest_missing",
        "release_report_missing",
        "delivery_check_missing",
    }


def test_current_version_bound_evidence_is_trusted(tmp_path: Path):
    _write_evidence(tmp_path, production_ready=True)

    result = _snapshot(tmp_path, expected_release_sha="abc123def456")

    assert result["status"] == "current"
    assert result["evidence_valid"] is True
    assert result["evidence_fresh"] is True
    assert result["release_sha_verified"] is True
    assert result["trusted_production_ready"] is True


def test_stale_or_commit_mismatched_evidence_cannot_unlock_release(tmp_path: Path):
    _write_evidence(tmp_path, generated_at=NOW - timedelta(hours=25), production_ready=True)

    stale = _snapshot(tmp_path, expected_release_sha="abc123def456", max_age_hours=24)
    mismatch = _snapshot(tmp_path, expected_release_sha="different-sha", max_age_hours=48)

    assert stale["status"] == "stale"
    assert stale["trusted_production_ready"] is False
    assert mismatch["status"] == "commit_mismatch"
    assert mismatch["trusted_production_ready"] is False


def test_missing_runtime_release_sha_is_explicitly_unbound(tmp_path: Path):
    _write_evidence(tmp_path, production_ready=True)

    result = _snapshot(tmp_path, expected_release_sha="")

    assert result["status"] == "version_unbound"
    assert result["release_sha_verified"] is None
    assert result["trusted_production_ready"] is False


def test_invalid_json_and_missing_gate_are_not_silently_accepted(tmp_path: Path):
    _write_evidence(tmp_path, gates=[])
    current = _snapshot(tmp_path, expected_release_sha="abc123def456")

    assert len(current["external_gates"]) == 6
    assert {item["key"] for item in current["external_gates"]} == {
        "fact_platform_sandbox",
        "metric_source",
        "message_channel",
        "production_infrastructure",
        "real_model_holdout",
        "usability_test",
    }
    assert all(item["status"] == "not_reported" for item in current["external_gates"])
    assert current["trusted_production_ready"] is False

    (tmp_path / "after-sales-release-readiness.json").write_text("{", encoding="utf-8")
    invalid = _snapshot(tmp_path, expected_release_sha="abc123def456")
    assert invalid["status"] == "invalid"
    assert invalid["trusted_production_ready"] is False


def test_manifest_is_required_and_tampered_file_fails_closed(tmp_path: Path):
    _write_evidence(tmp_path, production_ready=True, write_manifest=False)

    missing = _snapshot(tmp_path, expected_release_sha="abc123def456")
    assert missing["status"] == "invalid"
    assert missing["trusted_production_ready"] is False
    assert "evidence_manifest_missing" in missing["reasons"]

    _write_evidence(tmp_path, production_ready=True)
    report_path = tmp_path / "after-sales-release-readiness.json"
    report_path.write_text(report_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    tampered = _snapshot(tmp_path, expected_release_sha="abc123def456")
    assert tampered["status"] == "invalid"
    assert tampered["trusted_production_ready"] is False
    assert "evidence_bundle_hash_mismatch" in tampered["reasons"]


def test_manifest_rejects_cross_batch_or_release_mismatch(tmp_path: Path):
    _write_evidence(tmp_path, production_ready=True)
    manifest_path = tmp_path / "delivery-evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["release_sha"] = "different-release"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _snapshot(tmp_path, expected_release_sha="abc123def456")

    assert result["status"] == "invalid"
    assert result["trusted_production_ready"] is False
    assert "evidence_bundle_release_mismatch" in result["reasons"]

    _write_evidence(tmp_path, production_ready=True)
    check_path = tmp_path / "after-sales-delivery-check.json"
    check = json.loads(check_path.read_text(encoding="utf-8"))
    check["source_report_generated_at"] = (NOW - timedelta(minutes=1)).isoformat()
    check_path.write_text(json.dumps(check), encoding="utf-8")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][check_path.name]["sha256"] = hashlib.sha256(check_path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    cross_batch = _snapshot(tmp_path, expected_release_sha="abc123def456")
    assert cross_batch["status"] == "invalid"
    assert "evidence_bundle_generation_mismatch" in cross_batch["reasons"]


def test_manifest_verifies_remote_ci_attachment_hash_and_safety_claims(tmp_path: Path):
    _write_evidence(tmp_path, production_ready=False)
    attachment_path = tmp_path / "remote-ci-production-shape.json"
    attachment = {
        "schema_version": "target-environment-probe-v1",
        "generated_at": NOW.isoformat(),
        "expected_env": "test",
        "executed": True,
        "passed": True,
        "runtime_shape_ready": True,
        "target_environment_ready": False,
        "cross_host_or_target_environment_verified": False,
        "production_ready": False,
    }
    attachment_path.write_text(json.dumps(attachment), encoding="utf-8")
    manifest_path = tmp_path / "delivery-evidence-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][attachment_path.name] = {
        "sha256": hashlib.sha256(attachment_path.read_bytes()).hexdigest(),
        "generated_at": NOW.isoformat(),
        "schema_version": "target-environment-probe-v1",
        "role": "remote_ci_runtime_shape",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    verified = _snapshot(tmp_path, expected_release_sha="abc123def456")
    assert verified["status"] == "current"
    assert verified["attachment_count"] == 1

    attachment["production_ready"] = True
    attachment_path.write_text(json.dumps(attachment), encoding="utf-8")
    manifest["files"][attachment_path.name]["sha256"] = hashlib.sha256(
        attachment_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    unsafe = _snapshot(tmp_path, expected_release_sha="abc123def456")
    assert unsafe["status"] == "invalid"
    assert "evidence_bundle_attachment_invalid" in unsafe["reasons"]
