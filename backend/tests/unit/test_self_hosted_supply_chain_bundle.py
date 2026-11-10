from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "backend" / "scripts" / "package_self_hosted_supply_chain_evidence.py"
SPEC = importlib.util.spec_from_file_location("self_hosted_bundle_under_test", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
NOW = datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)


def _payloads():
    return {
        "runner.json": {
            "schema_version": "self-hosted-runner-readiness-v1",
            "passed": True,
            "provider": "gitee_go",
            "scope": "self_hosted_supply_chain_runner",
            "source_git_sha": SHA,
            "host_group_fingerprint": "c" * 16,
            "supply_chain_runner_ready": True,
            "production_ready": False,
        },
        "attestation.json": {
            "schema_version": "image-attestation-verification-v1",
            "passed": True,
            "source_git_sha": SHA,
            "index_digest": DIGEST,
            "scope": "remote_ci_local_registry",
            "provenance_verified": True,
            "sbom_verified": True,
            "production_ready": False,
        },
        "signature.json": {
            "schema_version": "image-signature-verification-v1",
            "passed": True,
            "source_git_sha": SHA,
            "image_digest": DIGEST,
            "scope": "remote_ci_local_registry",
            "signature_verified": True,
            "release_annotation_verified": True,
            "production_ready": False,
        },
    }


def _write(tmp_path: Path, payloads=None):
    payloads = payloads or _payloads()
    paths = {}
    for name, payload in payloads.items():
        path = tmp_path / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths[name] = path
    return paths


def _package(paths):
    return MODULE.package_evidence(
        runner_path=paths["runner.json"],
        attestation_path=paths["attestation.json"],
        signature_path=paths["signature.json"],
        expected_release_sha=SHA,
        generated_at=NOW,
    )


def test_bundle_binds_runner_attestation_signature_and_release_sha(tmp_path: Path):
    bundle = _package(_write(tmp_path))

    assert bundle["passed"] is True
    assert bundle["release_sha"] == SHA
    assert bundle["image_digest"] == DIGEST
    assert len(bundle["bundle_id"]) == 64
    assert len(bundle["files"]) == 3
    assert bundle["provenance_verified"] is True
    assert bundle["sbom_verified"] is True
    assert bundle["signature_verified"] is True
    assert bundle["self_hosted_runner_verified"] is True
    assert bundle["local_runner_rehearsal_verified"] is False
    assert bundle["production_ready"] is False


def test_bundle_preserves_local_rehearsal_scope_without_promoting_self_hosted(tmp_path: Path):
    payloads = _payloads()
    payloads["runner.json"].update(
        {
            "provider": "local_docker_desktop",
            "scope": "local_host_rehearsal",
            "host_group_fingerprint": None,
        }
    )
    payloads["attestation.json"]["scope"] = "local_loopback_registry"
    payloads["signature.json"]["scope"] = "local_loopback_registry"

    bundle = _package(_write(tmp_path, payloads))

    assert bundle["runner_scope"] == "local_host_rehearsal"
    assert bundle["self_hosted_runner_verified"] is False
    assert bundle["local_runner_rehearsal_verified"] is True
    assert bundle["production_ready"] is False


@pytest.mark.parametrize("mutation", ("runner_not_ready", "sha_mismatch", "digest_mismatch", "production_claim"))
def test_bundle_rejects_untrusted_or_inconsistent_evidence(tmp_path: Path, mutation: str):
    payloads = deepcopy(_payloads())
    if mutation == "runner_not_ready":
        payloads["runner.json"]["supply_chain_runner_ready"] = False
    elif mutation == "sha_mismatch":
        payloads["attestation.json"]["source_git_sha"] = "d" * 40
    elif mutation == "digest_mismatch":
        payloads["signature.json"]["image_digest"] = "sha256:" + "e" * 64
    else:
        payloads["signature.json"]["production_ready"] = True

    with pytest.raises(ValueError):
        _package(_write(tmp_path, payloads))
