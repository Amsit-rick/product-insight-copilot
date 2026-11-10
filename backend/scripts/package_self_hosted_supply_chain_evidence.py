"""Bind runner, attestation, and signature evidence into one release-SHA bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError(f"invalid evidence file: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid evidence JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"evidence must be an object: {path.name}")
    if payload.get("production_ready") is not False:
        raise ValueError(f"evidence must not claim production readiness: {path.name}")
    return payload


def package_evidence(
    *,
    runner_path: Path,
    attestation_path: Path,
    signature_path: Path,
    expected_release_sha: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(expected_release_sha):
        raise ValueError("expected release SHA must be 40 lowercase hexadecimal characters")
    runner = _read_json(runner_path)
    attestation = _read_json(attestation_path)
    signature = _read_json(signature_path)
    if runner.get("schema_version") != "self-hosted-runner-readiness-v1":
        raise ValueError("unsupported runner evidence schema")
    if runner.get("passed") is not True or runner.get("supply_chain_runner_ready") is not True:
        raise ValueError("runner evidence is not ready")
    runner_scope = runner.get("scope")
    if runner_scope not in {"self_hosted_supply_chain_runner", "local_host_rehearsal"}:
        raise ValueError("runner evidence scope is unsupported")
    if attestation.get("schema_version") != "image-attestation-verification-v1":
        raise ValueError("unsupported attestation evidence schema")
    if signature.get("schema_version") != "image-signature-verification-v1":
        raise ValueError("unsupported signature evidence schema")
    expected_image_scope = (
        "remote_ci_local_registry"
        if runner_scope == "self_hosted_supply_chain_runner"
        else "local_loopback_registry"
    )
    if attestation.get("scope") != expected_image_scope:
        raise ValueError("attestation scope does not match runner scope")
    if signature.get("scope") != expected_image_scope:
        raise ValueError("signature scope does not match runner scope")
    for name, payload in (
        ("runner", runner),
        ("attestation", attestation),
        ("signature", signature),
    ):
        if payload.get("source_git_sha") != expected_release_sha:
            raise ValueError(f"{name} evidence release SHA mismatch")
    if attestation.get("passed") is not True:
        raise ValueError("attestation evidence did not pass")
    if attestation.get("provenance_verified") is not True or attestation.get("sbom_verified") is not True:
        raise ValueError("provenance or SBOM is not verified")
    if signature.get("passed") is not True:
        raise ValueError("signature evidence did not pass")
    if signature.get("signature_verified") is not True or signature.get("release_annotation_verified") is not True:
        raise ValueError("signature claims are not verified")
    image_digest = str(attestation.get("index_digest") or "")
    if not DIGEST_RE.fullmatch(image_digest):
        raise ValueError("attestation image digest is invalid")
    if signature.get("image_digest") != image_digest:
        raise ValueError("attestation and signature image digests differ")

    paths = (runner_path, attestation_path, signature_path)
    files = {
        path.name: {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "schema_version": payload["schema_version"],
        }
        for path, payload in zip(paths, (runner, attestation, signature), strict=True)
    }
    binding = json.dumps(
        {"release_sha": expected_release_sha, "files": files},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": "self-hosted-supply-chain-bundle-v1",
        "generated_at": (generated_at or datetime.now(UTC)).isoformat(),
        "executed": True,
        "passed": True,
        "release_sha": expected_release_sha,
        "bundle_id": hashlib.sha256(binding).hexdigest(),
        "runner_provider": runner.get("provider"),
        "runner_scope": runner_scope,
        "host_group_fingerprint": runner.get("host_group_fingerprint"),
        "image_digest": image_digest,
        "files": files,
        "provenance_verified": True,
        "sbom_verified": True,
        "signature_verified": True,
        "self_hosted_runner_verified": runner_scope == "self_hosted_supply_chain_runner",
        "local_runner_rehearsal_verified": runner_scope == "local_host_rehearsal",
        "enterprise_registry_verified": False,
        "cross_host_verified": False,
        "production_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", type=Path, required=True)
    parser.add_argument("--attestation", type=Path, required=True)
    parser.add_argument("--signature", type=Path, required=True)
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--json-out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        payload = package_evidence(
            runner_path=args.runner,
            attestation_path=args.attestation,
            signature_path=args.signature,
            expected_release_sha=args.expected_release_sha,
        )
    except (ValueError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
