from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "verify_image_signature.py"
spec = importlib.util.spec_from_file_location("verify_image_signature_under_test", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
REFERENCE = f"127.0.0.1:15005/pic-api@{DIGEST}"
COSIGN_IMAGE = "ghcr.io/sigstore/cosign/cosign@sha256:" + "c" * 64


def _payload():
    return [
        {
            "critical": {
                "identity": {"docker-reference": REFERENCE},
                "image": {"docker-manifest-digest": DIGEST},
                "type": "https://sigstore.dev/cosign/sign/v1",
            },
            "optional": {"release_sha": SHA},
        }
    ]


def _validate(payload=None):
    return module.validate_image_signature(
        verification_payload=_payload() if payload is None else payload,
        image_reference=REFERENCE,
        expected_release_sha=SHA,
        public_key_fingerprint="d" * 16,
        cosign_image=COSIGN_IMAGE,
        cosign_version="v3.1.4",
        evidence_scope="local_loopback_registry",
    )


def test_digest_bound_cosign_signature_is_accepted_without_production_claims():
    result = _validate()

    assert result["signature_verified"] is True
    assert result["release_annotation_verified"] is True
    assert result["signature_count"] == 1
    assert result["transparency_log_verified"] is False
    assert result["kms_verified"] is False
    assert result["enterprise_registry_verified"] is False
    assert result["cross_host_verified"] is False


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("docker-reference", f"127.0.0.1:15005/other@{DIGEST}", "引用"),
        ("docker-manifest-digest", "sha256:" + "e" * 64, "manifest digest"),
        ("release_sha", "f" * 40, "release_sha"),
    ),
)
def test_signature_claim_mismatch_fails_closed(field: str, value: str, message: str):
    payload = deepcopy(_payload())
    if field == "docker-reference":
        payload[0]["critical"]["identity"][field] = value
    elif field == "docker-manifest-digest":
        payload[0]["critical"]["image"][field] = value
    else:
        payload[0]["optional"][field] = value

    with pytest.raises(ValueError, match=message):
        _validate(payload)


def test_empty_or_malformed_signature_output_is_rejected():
    with pytest.raises(ValueError, match="没有返回有效签名"):
        _validate([])
    with pytest.raises(ValueError, match="不是对象"):
        _validate(["bad"])


def test_cosign_tool_image_must_be_digest_pinned():
    with pytest.raises(ValueError, match="按 sha256 digest 固定"):
        module.validate_image_signature(
            verification_payload=_payload(),
            image_reference=REFERENCE,
            expected_release_sha=SHA,
            public_key_fingerprint="d" * 16,
            cosign_image="ghcr.io/sigstore/cosign/cosign:v3.1.4",
            cosign_version="v3.1.4",
            evidence_scope="local_loopback_registry",
        )
