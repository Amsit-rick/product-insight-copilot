#!/usr/bin/env python
"""Validate an independent Saleor cold-start acceptance record.

This validator checks a declaration and binds it to the sanitized Slice 5
artifact. It cannot prove a person's identity, so the output deliberately says
"declared" and must not be treated as enterprise or production evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RECORD_SCHEMA = "saleor-cold-start-acceptance-v1"
ARTIFACT_SCHEMA = "saleor-main-workflow-slice5-v1"


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _timezone_aware(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_record(
    record: dict[str, Any],
    artifact: dict[str, Any],
    *,
    artifact_sha256: str | None = None,
) -> dict[str, Any]:
    operator = record.get("operator") if isinstance(record.get("operator"), dict) else {}
    environment = (
        record.get("environment") if isinstance(record.get("environment"), dict) else {}
    )
    steps = record.get("steps") if isinstance(record.get("steps"), dict) else {}
    observed = record.get("observed") if isinstance(record.get("observed"), dict) else {}
    summary = artifact.get("summary") if isinstance(artifact.get("summary"), dict) else {}

    bound_digest = artifact_sha256 or _digest(artifact)
    checks = {
        "record_schema_supported": record.get("schema_version") == RECORD_SCHEMA,
        "performed_at_has_timezone": _timezone_aware(record.get("performed_at")),
        "operator_role_declared": bool(str(operator.get("role") or "").strip()),
        "operator_independent": operator.get("independent_from_implementation") is True,
        "fresh_checkout_used": environment.get("fresh_checkout") is True,
        "no_preexisting_commerce_volumes": (
            environment.get("preexisting_commerce_volumes") is False
        ),
        "required_steps_completed": all(
            steps.get(name) is True
            for name in (
                "start_completed",
                "health_checked",
                "main_workflow_completed",
                "stop_or_cleanup_documented",
            )
        ),
        "saleor_health_observed": observed.get("fact_provider_status") == "saleor_ready",
        "scenario_manifest_observed": (
            observed.get("scenario_manifest_status") == "ready"
        ),
        "artifact_schema_supported": artifact.get("schema_version") == ARTIFACT_SCHEMA,
        "artifact_digest_matches": record.get("artifact_sha256") == bound_digest,
        "business_scenarios_passed": (
            summary.get("business_passed") == 10
            and summary.get("business_total") == 10
        ),
        "fault_scenarios_passed": (
            summary.get("fault_passed") == 6 and summary.get("fault_total") == 6
        ),
        "local_environment_preserved": (
            artifact.get("environment") == "local_high_fidelity_sandbox"
        ),
        "enterprise_boundary_preserved": (
            artifact.get("enterprise_endpoint_verified") is False
            and artifact.get("real_payment_verified") is False
            and artifact.get("real_carrier_verified") is False
        ),
        "production_boundary_preserved": artifact.get("production_ready") is False,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "schema_version": "saleor-cold-start-validation-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "verified_independent" if not failed else "blocked",
        "checks_total": len(checks),
        "checks_passed": len(checks) - len(failed),
        "failed_checks": failed,
        "independent_operator_declared": (
            operator.get("independent_from_implementation") is True
        ),
        "environment": artifact.get("environment"),
        "artifact_sha256": bound_digest,
        "enterprise_endpoint_verified": False,
        "real_payment_verified": False,
        "real_carrier_verified": False,
        "production_ready": False,
    }


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        artifact = _read_json(args.artifact)
        report = validate_record(
            _read_json(args.record),
            artifact,
            artifact_sha256=hashlib.sha256(args.artifact.read_bytes()).hexdigest(),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Saleor cold-start record validation failed: {exc}", file=sys.stderr)
        return 2
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_bytes(_canonical_bytes(report))
    print(
        f"status={report['status']} "
        f"checks={report['checks_passed']}/{report['checks_total']} "
        "production_ready=false"
    )
    return 0 if report["status"] == "verified_independent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
