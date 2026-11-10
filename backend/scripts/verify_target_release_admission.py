"""Verify release evidence consistency before target-node deployment.

This gate evaluates machine evidence only. It never grants final release approval.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SCHEMAS = {
    "runner_readiness": "self-hosted-runner-readiness-v1",
    "attestation": "image-attestation-verification-v1",
    "signature": "image-signature-verification-v1",
    "bundle": "self-hosted-supply-chain-bundle-v1",
    "secure_registry": "secure-registry-rehearsal-v1",
    "rollout": "registry-rollout-rollback-evidence-v1",
    "target_environment": "target-environment-probe-v1",
    "database_recovery": "database-recovery-objectives-verification-v1",
}


def _read_json(path: Path, expected_schema: str) -> dict[str, Any]:
    if not path.is_file():
        raise ValueError(f"missing evidence file: {path.name}")
    if path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError(f"evidence file is too large: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid evidence file: {path.name}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != expected_schema:
        raise ValueError(f"evidence schema mismatch: {path.name}")
    return payload


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def _all_evidence_current(
    payloads: dict[str, dict[str, Any]], *, now: datetime, max_age_hours: int
) -> bool:
    if max_age_hours < 1:
        raise ValueError("max_age_hours must be at least 1")
    earliest = now - timedelta(hours=max_age_hours)
    latest = now + timedelta(minutes=5)
    for payload in payloads.values():
        generated_at = _timestamp(payload.get("generated_at"))
        if generated_at is None or not earliest <= generated_at <= latest:
            return False
    return True


def verify_target_release_admission(
    *,
    runner_readiness_path: Path,
    attestation_path: Path,
    signature_path: Path,
    bundle_path: Path,
    secure_registry_path: Path,
    rollout_path: Path,
    target_environment_path: Path,
    database_recovery_path: Path,
    expected_release_sha: str,
    max_age_hours: int = 24,
    now: datetime | None = None,
) -> dict[str, Any]:
    expected_sha = expected_release_sha.strip()
    if not SHA_RE.fullmatch(expected_sha):
        raise ValueError("expected release SHA must be 40 lowercase hexadecimal characters")

    paths = {
        "runner_readiness": runner_readiness_path.resolve(),
        "attestation": attestation_path.resolve(),
        "signature": signature_path.resolve(),
        "bundle": bundle_path.resolve(),
        "secure_registry": secure_registry_path.resolve(),
        "rollout": rollout_path.resolve(),
        "target_environment": target_environment_path.resolve(),
        "database_recovery": database_recovery_path.resolve(),
    }
    payloads = {
        name: _read_json(path, SCHEMAS[name]) for name, path in paths.items()
    }
    evidence_files = {name: _sha256(path) for name, path in paths.items()}
    runner = payloads["runner_readiness"]
    attestation = payloads["attestation"]
    signature = payloads["signature"]
    bundle = payloads["bundle"]
    registry = payloads["secure_registry"]
    rollout = payloads["rollout"]
    target = payloads["target_environment"]
    database_recovery = payloads["database_recovery"]
    current_rollout = rollout.get("current") if isinstance(rollout.get("current"), dict) else {}

    declared_files = bundle.get("files") if isinstance(bundle.get("files"), dict) else {}
    bundle_hashes_verified = all(
        isinstance(declared_files.get(name), dict)
        and declared_files[name].get("sha256") == _sha256(path)
        for name, path in (
            ("runner-readiness.json", paths["runner_readiness"]),
            ("image-attestations.json", paths["attestation"]),
            ("image-signature.json", paths["signature"]),
        )
    )

    release_shas = {
        str(runner.get("source_git_sha") or ""),
        str(attestation.get("source_git_sha") or ""),
        str(signature.get("source_git_sha") or ""),
        str(bundle.get("release_sha") or ""),
        str(current_rollout.get("source_git_sha") or ""),
        str(database_recovery.get("current_source_git_sha") or ""),
    }
    release_sha_verified = release_shas == {expected_sha}
    image_digest = str(bundle.get("image_digest") or "")
    platform_digest = str(attestation.get("platform_manifest_digest") or "")
    image_digest_verified = all(
        (
            DIGEST_RE.fullmatch(image_digest) is not None,
            DIGEST_RE.fullmatch(platform_digest) is not None,
            attestation.get("index_digest") == image_digest,
            signature.get("image_digest") == image_digest,
            current_rollout.get("digest") == platform_digest,
        )
    )
    evidence_fresh = _all_evidence_current(
        payloads,
        now=(now or datetime.now(UTC)).astimezone(UTC),
        max_age_hours=max_age_hours,
    )

    supply_chain_verified = all(
        (
            runner.get("executed") is True,
            runner.get("passed") is True,
            runner.get("supply_chain_runner_ready") is True,
            attestation.get("executed") is True,
            attestation.get("passed") is True,
            attestation.get("provenance_verified") is True,
            attestation.get("sbom_verified") is True,
            signature.get("executed") is True,
            signature.get("passed") is True,
            signature.get("signature_verified") is True,
            signature.get("release_annotation_verified") is True,
            bundle.get("executed") is True,
            bundle.get("passed") is True,
            bundle.get("provenance_verified") is True,
            bundle.get("sbom_verified") is True,
            bundle.get("signature_verified") is True,
            bundle_hashes_verified,
        )
    )
    secure_registry_controls_verified = all(
        (
            registry.get("executed") is True,
            registry.get("passed") is True,
            registry.get("tls_verified") is True,
            registry.get("untrusted_tls_rejected") is True,
            registry.get("basic_auth_required") is True,
            registry.get("wrong_credentials_rejected") is True,
            registry.get("push_verified") is True,
            registry.get("digest_pull_verified") is True,
        )
    )
    registry_control_release_bound = registry.get("source_git_sha") == expected_sha
    local_rollout_verified = all(
        (
            rollout.get("executed") is True,
            rollout.get("passed") is True,
            current_rollout.get("registry_published") is True,
            current_rollout.get("registry_pull_verified") is True,
            current_rollout.get("deployment_verified") is True,
            current_rollout.get("queue_verified") is True,
            rollout.get("database_release_safety_verified") is True,
        )
    )
    target_runtime_verified = all(
        (
            target.get("executed") is True,
            target.get("passed") is True,
            target.get("runtime_shape_ready") is True,
            target.get("expected_release_sha") == expected_sha,
        )
    )
    database_recovery_objectives_verified = all(
        (
            database_recovery.get("executed") is True,
            database_recovery.get("passed") is True,
            database_recovery.get("current_source_git_sha") == expected_sha,
            database_recovery.get("local_recovery_objectives_verified") is True,
        )
    )
    enterprise_registry_verified = all(
        (
            registry.get("enterprise_registry_verified") is True,
            attestation.get("enterprise_registry_verified") is True,
            signature.get("enterprise_registry_verified") is True,
            rollout.get("enterprise_registry_verified") is True,
        )
    )
    cross_host_verified = all(
        (
            attestation.get("cross_host_verified") is True,
            signature.get("cross_host_verified") is True,
            rollout.get("cross_host_verified") is True,
            target.get("target_environment_ready") is True,
            target.get("cross_host_or_target_environment_verified") is True,
        )
    )
    enterprise_backup_governance_verified = (
        database_recovery.get("enterprise_backup_governance_verified") is True
    )
    self_claims_safe = all(
        payload.get("production_ready") is False for payload in payloads.values()
    )
    checks = {
        "bundle_hashes_verified": bundle_hashes_verified,
        "self_claims_safe": self_claims_safe,
        "evidence_fresh": evidence_fresh,
        "release_sha_verified": release_sha_verified,
        "image_digest_verified": image_digest_verified,
        "supply_chain_verified": supply_chain_verified,
        "secure_registry_controls_verified": secure_registry_controls_verified,
        "registry_control_release_bound": registry_control_release_bound,
        "local_rollout_verified": local_rollout_verified,
        "target_runtime_verified": target_runtime_verified,
        "database_recovery_objectives_verified": database_recovery_objectives_verified,
        "enterprise_registry_verified": enterprise_registry_verified,
        "cross_host_verified": cross_host_verified,
        "enterprise_backup_governance_verified": enterprise_backup_governance_verified,
    }
    blockers = [name for name, passed in checks.items() if not passed]
    machine_admission_ready = not blockers
    admission_material = json.dumps(
        {
            "expected_release_sha": expected_sha,
            "image_digest": image_digest,
            "evidence_files": evidence_files,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": "target-release-admission-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "executed": True,
        "passed": machine_admission_ready,
        "status": (
            "machine_ready_awaiting_human_approval"
            if machine_admission_ready
            else "blocked"
        ),
        "expected_release_sha": expected_sha,
        "evidence_release_shas": sorted(value for value in release_shas if value),
        "image_digest": image_digest if DIGEST_RE.fullmatch(image_digest) else None,
        "registry_control_source_sha": registry.get("source_git_sha"),
        "admission_bundle_id": hashlib.sha256(admission_material).hexdigest(),
        "evidence_files": evidence_files,
        "checks": checks,
        "blockers": blockers,
        "machine_admission_ready": machine_admission_ready,
        "human_approval_required": True,
        "release_authorized": False,
        "production_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-readiness", type=Path, required=True)
    parser.add_argument("--attestation", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--secure-registry", type=Path, required=True)
    parser.add_argument("--rollout", type=Path, required=True)
    parser.add_argument("--target-environment", type=Path, required=True)
    parser.add_argument("--database-recovery", type=Path, required=True)
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--max-age-hours", type=int, default=24)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--require-machine-ready", action="store_true")
    args = parser.parse_args(argv)
    try:
        payload = verify_target_release_admission(
            runner_readiness_path=args.runner_readiness,
            attestation_path=args.attestation,
            signature_path=args.signature,
            bundle_path=args.bundle,
            secure_registry_path=args.secure_registry,
            rollout_path=args.rollout,
            target_environment_path=args.target_environment,
            database_recovery_path=args.database_recovery,
            expected_release_sha=args.expected_release_sha,
            max_age_hours=args.max_age_hours,
        )
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"[FAIL] {exc}")
        return 2
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.require_machine_ready and not payload["machine_admission_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
