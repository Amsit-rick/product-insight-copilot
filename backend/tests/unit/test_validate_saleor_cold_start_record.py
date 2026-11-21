from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "validate_saleor_cold_start_record.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "validate_saleor_cold_start_record_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _artifact() -> dict:
    return {
        "schema_version": "saleor-main-workflow-slice5-v1",
        "environment": "local_high_fidelity_sandbox",
        "enterprise_endpoint_verified": False,
        "real_payment_verified": False,
        "real_carrier_verified": False,
        "production_ready": False,
        "summary": {
            "business_passed": 10,
            "business_total": 10,
            "fault_passed": 6,
            "fault_total": 6,
        },
    }


def _digest(payload: dict) -> str:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
    return hashlib.sha256(encoded).hexdigest()


def _record(artifact: dict, *, independent: bool = True) -> dict:
    return {
        "schema_version": "saleor-cold-start-acceptance-v1",
        "performed_at": "2026-11-16T18:00:00+08:00",
        "operator": {
            "role": "independent_qa",
            "independent_from_implementation": independent,
        },
        "environment": {
            "fresh_checkout": True,
            "preexisting_commerce_volumes": False,
        },
        "steps": {
            "start_completed": True,
            "health_checked": True,
            "main_workflow_completed": True,
            "stop_or_cleanup_documented": True,
        },
        "observed": {
            "fact_provider_status": "saleor_ready",
            "scenario_manifest_status": "ready",
        },
        "artifact_sha256": _digest(artifact),
    }


def test_valid_independent_record_binds_the_slice5_artifact():
    module = _load_module()
    artifact = _artifact()

    result = module.validate_record(_record(artifact), artifact)

    assert result["status"] == "verified_independent"
    assert result["checks_passed"] == result["checks_total"]
    assert result["production_ready"] is False
    assert result["enterprise_endpoint_verified"] is False


def test_developer_self_attestation_is_rejected():
    module = _load_module()
    artifact = _artifact()

    result = module.validate_record(_record(artifact, independent=False), artifact)

    assert result["status"] == "blocked"
    assert "operator_independent" in result["failed_checks"]


def test_record_is_rejected_when_artifact_digest_does_not_match():
    module = _load_module()
    artifact = _artifact()
    record = _record(artifact)
    record["artifact_sha256"] = "0" * 64

    result = module.validate_record(record, artifact)

    assert result["status"] == "blocked"
    assert "artifact_digest_matches" in result["failed_checks"]


def test_enterprise_or_production_claims_are_rejected():
    module = _load_module()
    artifact = _artifact()
    artifact["production_ready"] = True
    record = _record(artifact)

    result = module.validate_record(record, artifact)

    assert result["status"] == "blocked"
    assert "production_boundary_preserved" in result["failed_checks"]


def test_cli_writes_a_sanitized_validation_report(tmp_path, monkeypatch):
    module = _load_module()
    artifact = _artifact()
    artifact_path = tmp_path / "artifact.json"
    record_path = tmp_path / "record.json"
    output_path = tmp_path / "validation.json"
    artifact_path.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    record = _record(artifact)
    record["artifact_sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            str(SCRIPT),
            "--record",
            str(record_path),
            "--artifact",
            str(artifact_path),
            "--json-out",
            str(output_path),
        ],
    )

    assert module.main() == 0
    output = json.loads(output_path.read_text(encoding="utf-8"))
    assert output["status"] == "verified_independent"
    assert "operator" not in output
    assert output["production_ready"] is False


def test_cli_fails_closed_for_invalid_json(tmp_path, monkeypatch):
    module = _load_module()
    record_path = tmp_path / "record.json"
    artifact_path = tmp_path / "artifact.json"
    record_path.write_text("not-json", encoding="utf-8")
    artifact_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            str(SCRIPT),
            "--record",
            str(record_path),
            "--artifact",
            str(artifact_path),
        ],
    )

    assert module.main() == 2
