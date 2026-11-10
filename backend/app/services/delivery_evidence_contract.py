"""Shared contract for redacted remote-CI delivery evidence attachments."""

from __future__ import annotations

from datetime import datetime
from typing import Any


REMOTE_CI_ATTACHMENT_CONTRACTS: dict[str, dict[str, str]] = {
    "remote-ci-object-storage.json": {
        "schema_version": "object-storage-probe-v1",
        "role": "remote_ci_object_storage",
    },
    "remote-ci-production-shape.json": {
        "schema_version": "target-environment-probe-v1",
        "role": "remote_ci_runtime_shape",
    },
    "remote-ci-scheduler-failover.json": {
        "schema_version": "scheduler-failover-rehearsal-v1",
        "role": "remote_ci_scheduler_failover",
    },
    "remote-ci-image-attestations.json": {
        "schema_version": "image-attestation-verification-v1",
        "role": "remote_ci_image_attestations",
    },
    "remote-ci-image-signature.json": {
        "schema_version": "image-signature-verification-v1",
        "role": "remote_ci_image_signature",
    },
}


def _has_timezone(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def remote_ci_attachment_is_safe(filename: str, payload: Any) -> bool:
    """Validate a CI attachment without allowing it to claim production approval."""
    contract = REMOTE_CI_ATTACHMENT_CONTRACTS.get(filename)
    if not contract or not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != contract["schema_version"]:
        return False
    if not _has_timezone(payload.get("generated_at")):
        return False
    if payload.get("executed") is not True or payload.get("passed") is not True:
        return False
    if payload.get("production_ready") is not False:
        return False

    if filename == "remote-ci-object-storage.json":
        return payload.get("shared_storage_ready") is True
    if filename == "remote-ci-production-shape.json":
        return all(
            (
                payload.get("expected_env") == "test",
                payload.get("runtime_shape_ready") is True,
                payload.get("target_environment_ready") is False,
                payload.get("cross_host_or_target_environment_verified") is False,
                payload.get("distinct_hosts_attested") in {None, False},
            )
        )
    if filename == "remote-ci-scheduler-failover.json":
        return all(
            (
                payload.get("scope") == "local_single_host",
                payload.get("local_postgres_failover_verified") is True,
                payload.get("cross_host_failover_verified") is False,
            )
        )
    if filename == "remote-ci-image-attestations.json":
        return all(
            (
                payload.get("scope") == "remote_ci_local_registry",
                payload.get("provenance_verified") is True,
                payload.get("sbom_verified") is True,
                payload.get("signature_verified") is False,
                payload.get("enterprise_registry_verified") is False,
                payload.get("cross_host_verified") is False,
            )
        )
    if filename == "remote-ci-image-signature.json":
        return all(
            (
                payload.get("scope") == "remote_ci_local_registry",
                payload.get("signature_verified") is True,
                payload.get("release_annotation_verified") is True,
                payload.get("transparency_log_verified") is False,
                payload.get("keyless_identity_verified") is False,
                payload.get("kms_verified") is False,
                payload.get("key_management") == "ephemeral_local_key",
                payload.get("enterprise_registry_verified") is False,
                payload.get("cross_host_verified") is False,
            )
        )
    return False


__all__ = ["REMOTE_CI_ATTACHMENT_CONTRACTS", "remote_ci_attachment_is_safe"]
