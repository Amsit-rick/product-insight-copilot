from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "verify_image_attestations.py"
spec = importlib.util.spec_from_file_location("verify_image_attestations_under_test", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

SHA = "a" * 40
PLATFORM_DIGEST = "sha256:" + "b" * 64
ATTESTATION_DIGEST = "sha256:" + "c" * 64
INDEX_DIGEST = "sha256:" + "d" * 64


def _payloads():
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.index.v1+json",
        "digest": INDEX_DIGEST,
        "manifests": [
            {
                "digest": PLATFORM_DIGEST,
                "platform": {"os": "linux", "architecture": "amd64"},
            },
            {
                "digest": ATTESTATION_DIGEST,
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": PLATFORM_DIGEST,
                },
            },
        ],
    }
    provenance = {
        "SLSA": {
            "buildDefinition": {
                "buildType": "https://github.com/moby/buildkit/blob/master/docs/attestations/slsa-definitions.md",
                "externalParameters": {
                    "request": {
                        "args": {"build-arg:APP_RELEASE_SHA": SHA},
                        "root": {
                            "request": {
                                "args": {
                                    "vcs:revision": SHA,
                                    "vcs:source": "https://example.test/repository.git",
                                }
                            }
                        },
                    }
                },
                "resolvedDependencies": [{"uri": "pkg:docker/python"}],
            },
            "runDetails": {
                "builder": {"id": ""},
                "metadata": {
                    "startedOn": "2026-11-09T17:35:35.570946727Z",
                    "finishedOn": "2026-11-09T17:35:36.568974866Z",
                    "invocationId": "buildkit-local-invocation",
                },
            },
        }
    }
    sbom = {"SPDX": {"spdxVersion": "SPDX-2.3", "packages": [{"name": "fastapi"}]}}
    image = {
        "config": {
            "User": "appuser",
            "Labels": {"org.opencontainers.image.revision": SHA},
        }
    }
    return manifest, provenance, sbom, image


def _validate(payloads):
    manifest, provenance, sbom, image = payloads
    return module.validate_image_attestations(
        manifest=manifest,
        provenance=provenance,
        sbom=sbom,
        image=image,
        expected_release_sha=SHA,
    )


def test_valid_buildkit_provenance_and_spdx_sbom_are_accepted():
    result = _validate(_payloads())

    assert result["index_digest"] == INDEX_DIGEST
    assert result["platform_manifest_digest"] == PLATFORM_DIGEST
    assert result["attestation_manifest_digest"] == ATTESTATION_DIGEST
    assert result["builder_identity_available"] is False
    assert result["builder_id_fingerprint"] is None
    assert result["metadata_vcs_available"] is False
    assert result["sbom_format"] == "SPDX-2.3"
    assert result["sbom_package_count"] == 1
    assert "invocationId" not in result


def test_non_empty_builder_id_is_fingerprinted_without_exposing_identity():
    payloads = list(deepcopy(_payloads()))
    builder_id = "https://github.com/moby/buildkit@v1"
    payloads[1]["SLSA"]["runDetails"]["builder"]["id"] = builder_id

    result = _validate(payloads)

    assert result["builder_identity_available"] is True
    assert result["builder_id_fingerprint"]
    assert builder_id not in result.values()


def test_attestation_must_reference_target_platform_manifest():
    payloads = list(deepcopy(_payloads()))
    payloads[0]["manifests"][1]["annotations"]["vnd.docker.reference.digest"] = (
        "sha256:" + "e" * 64
    )

    with pytest.raises(ValueError, match="关联一个 attestation"):
        _validate(payloads)


@pytest.mark.parametrize("field", ("build-arg:APP_RELEASE_SHA", "vcs:revision"))
def test_provenance_release_sha_must_match(field: str):
    payloads = list(deepcopy(_payloads()))
    request = payloads[1]["SLSA"]["buildDefinition"]["externalParameters"]["request"]
    if field == "build-arg:APP_RELEASE_SHA":
        request["args"][field] = "f" * 40
    else:
        request["root"]["request"]["args"][field] = "f" * 40

    with pytest.raises(ValueError, match="发布 SHA"):
        _validate(payloads)


def test_oci_revision_must_match_release_sha():
    payloads = list(deepcopy(_payloads()))
    payloads[3]["config"]["Labels"]["org.opencontainers.image.revision"] = "f" * 40

    with pytest.raises(ValueError, match="OCI revision"):
        _validate(payloads)


def test_metadata_vcs_revision_must_match_release_sha():
    payloads = list(deepcopy(_payloads()))
    payloads[1]["SLSA"]["runDetails"]["metadata"]["vcs"] = {
        "revision": "f" * 40,
        "source": "https://example.test/repository.git",
    }

    with pytest.raises(ValueError, match="metadata VCS revision"):
        _validate(payloads)


def test_metadata_vcs_is_verified_when_buildkit_emits_it():
    payloads = list(deepcopy(_payloads()))
    payloads[1]["SLSA"]["runDetails"]["metadata"]["vcs"] = {
        "revision": SHA,
        "source": "https://example.test/repository.git",
    }

    result = _validate(payloads)

    assert result["metadata_vcs_available"] is True


@pytest.mark.parametrize("field", ("startedOn", "finishedOn", "invocationId"))
def test_run_metadata_required_fields_fail_closed(field: str):
    payloads = list(deepcopy(_payloads()))
    payloads[1]["SLSA"]["runDetails"]["metadata"][field] = ""

    with pytest.raises(ValueError, match=field):
        _validate(payloads)


def test_root_image_is_rejected():
    payloads = list(deepcopy(_payloads()))
    payloads[3]["config"]["User"] = "root"

    with pytest.raises(ValueError, match="不能是 root"):
        _validate(payloads)


def test_sbom_package_threshold_fails_closed():
    manifest, provenance, sbom, image = deepcopy(_payloads())

    with pytest.raises(ValueError, match="包数量低于门槛"):
        module.validate_image_attestations(
            manifest=manifest,
            provenance=provenance,
            sbom=sbom,
            image=image,
            expected_release_sha=SHA,
            minimum_sbom_packages=2,
        )


def test_local_scope_rejects_remote_registry_before_docker_call():
    with pytest.raises(ValueError, match="只允许 localhost"):
        module.verify_remote_image_attestations(
            image_reference="registry.example.test/pic-api:release",
            expected_release_sha=SHA,
            evidence_scope="remote_ci_local_registry",
        )
