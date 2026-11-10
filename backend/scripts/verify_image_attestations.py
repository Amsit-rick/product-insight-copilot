"""Verify BuildKit provenance and SPDX SBOM attached to a registry image.

The verifier uses Docker Buildx to read remote OCI descriptors and attestation
payloads. It validates supply-chain metadata but does not claim signature,
enterprise-registry, cross-host, or production readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
INDEX_MEDIA_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


def _run(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"命令执行失败 ({' '.join(command[:4])}): {detail[:800]}")
    return completed.stdout.strip()


def _inspect_json(image_reference: str, field: str) -> dict[str, Any]:
    raw = _run(
        [
            "docker",
            "buildx",
            "imagetools",
            "inspect",
            "--format",
            f"{{{{json .{field}}}}}",
            image_reference,
        ]
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Buildx {field} 输出不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Buildx {field} 输出必须是对象")
    return payload


def _require_mapping(value: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 缺失或不是对象")
    return value


def _require_list(value: Any, *, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} 缺失或不是数组")
    return value


def _authority(image_reference: str) -> str:
    first = image_reference.split("/", 1)[0].lower()
    if first == "localhost" or "." in first or ":" in first:
        return first
    return "docker.io"


def _scope(image_reference: str) -> str:
    authority = _authority(image_reference)
    host = authority.rsplit(":", 1)[0].strip("[]")
    if host in {"localhost", "127.0.0.1", "::1"}:
        return "local_loopback_registry"
    return "remote_registry"


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def validate_image_attestations(
    *,
    manifest: dict[str, Any],
    provenance: dict[str, Any],
    sbom: dict[str, Any],
    image: dict[str, Any],
    expected_release_sha: str,
    expected_platform: str = "linux/amd64",
    minimum_sbom_packages: int = 1,
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(expected_release_sha):
        raise ValueError("expected release SHA 必须是 40 位小写十六进制")
    try:
        expected_os, expected_architecture = expected_platform.split("/", 1)
    except ValueError as exc:
        raise ValueError("expected platform 必须是 os/architecture") from exc
    if minimum_sbom_packages < 1:
        raise ValueError("minimum SBOM packages 必须大于 0")

    if manifest.get("schemaVersion") != 2:
        raise ValueError("OCI manifest schemaVersion 必须为 2")
    if manifest.get("mediaType") not in INDEX_MEDIA_TYPES:
        raise ValueError("镜像必须是包含 attestation 的 OCI index/manifest list")
    index_digest = str(manifest.get("digest") or "")
    if not DIGEST_RE.fullmatch(index_digest):
        raise ValueError("OCI index digest 不合法")

    descriptors = _require_list(manifest.get("manifests"), field="manifest.manifests")
    platform_descriptors = [
        descriptor
        for descriptor in descriptors
        if isinstance(descriptor, dict)
        and (descriptor.get("platform") or {}).get("os") == expected_os
        and (descriptor.get("platform") or {}).get("architecture") == expected_architecture
    ]
    if len(platform_descriptors) != 1:
        raise ValueError("目标平台必须且只能有一个应用 manifest")
    platform_digest = str(platform_descriptors[0].get("digest") or "")
    if not DIGEST_RE.fullmatch(platform_digest):
        raise ValueError("目标平台 manifest digest 不合法")

    attestation_descriptors = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            continue
        annotations = descriptor.get("annotations") or {}
        if (
            annotations.get("vnd.docker.reference.type") == "attestation-manifest"
            and annotations.get("vnd.docker.reference.digest") == platform_digest
        ):
            attestation_descriptors.append(descriptor)
    if len(attestation_descriptors) != 1:
        raise ValueError("目标平台必须且只能关联一个 attestation manifest")
    attestation_digest = str(attestation_descriptors[0].get("digest") or "")
    if not DIGEST_RE.fullmatch(attestation_digest):
        raise ValueError("attestation manifest digest 不合法")

    config = _require_mapping(image.get("config"), field="image.config")
    labels = _require_mapping(config.get("Labels"), field="image.config.Labels")
    if labels.get("org.opencontainers.image.revision") != expected_release_sha:
        raise ValueError("OCI revision 与期望发布 SHA 不一致")
    user = str(config.get("User") or "").strip()
    if user.lower().split(":", 1)[0] in {"", "0", "root"}:
        raise ValueError("镜像运行用户不能是 root")

    slsa = _require_mapping(provenance.get("SLSA"), field="provenance.SLSA")
    build_definition = _require_mapping(
        slsa.get("buildDefinition"), field="provenance.SLSA.buildDefinition"
    )
    build_type = str(build_definition.get("buildType") or "")
    if not build_type.startswith("https://github.com/moby/buildkit/"):
        raise ValueError("provenance buildType 不是受支持的 BuildKit SLSA 类型")
    external = _require_mapping(
        build_definition.get("externalParameters"),
        field="provenance.SLSA.buildDefinition.externalParameters",
    )
    request = _require_mapping(external.get("request"), field="provenance request")
    build_args = _require_mapping(request.get("args"), field="provenance request.args")
    if build_args.get("build-arg:APP_RELEASE_SHA") != expected_release_sha:
        raise ValueError("provenance APP_RELEASE_SHA 与期望发布 SHA 不一致")
    root = _require_mapping(request.get("root"), field="provenance request.root")
    root_request = _require_mapping(root.get("request"), field="provenance root.request")
    root_args = _require_mapping(root_request.get("args"), field="provenance root.request.args")
    if root_args.get("vcs:revision") != expected_release_sha:
        raise ValueError("provenance VCS revision 与期望发布 SHA 不一致")
    source = str(root_args.get("vcs:source") or "").strip()
    if not source:
        raise ValueError("provenance 缺少 VCS source")

    run_details = _require_mapping(slsa.get("runDetails"), field="provenance.SLSA.runDetails")
    builder = _require_mapping(run_details.get("builder"), field="provenance builder")
    builder_id = str(builder.get("id") or "").strip()
    metadata = _require_mapping(run_details.get("metadata"), field="provenance run metadata")
    metadata_vcs_value = metadata.get("vcs")
    metadata_vcs_available = metadata_vcs_value is not None
    if metadata_vcs_available:
        metadata_vcs = _require_mapping(metadata_vcs_value, field="provenance metadata.vcs")
        if metadata_vcs.get("revision") != expected_release_sha:
            raise ValueError("provenance metadata VCS revision 与期望发布 SHA 不一致")
        metadata_source = str(metadata_vcs.get("source") or "").strip()
        if not metadata_source:
            raise ValueError("provenance metadata 缺少 VCS source")
        if metadata_source != source:
            raise ValueError("provenance VCS source 在请求与运行元数据中不一致")
    for field in ("startedOn", "finishedOn"):
        if not str(metadata.get(field) or "").strip():
            raise ValueError(f"provenance run metadata 缺少 {field}")
    if not str(metadata.get("invocationId") or "").strip():
        raise ValueError("provenance run metadata 缺少 invocationId")
    resolved_dependencies = _require_list(
        build_definition.get("resolvedDependencies"),
        field="provenance resolvedDependencies",
    )
    if not resolved_dependencies:
        raise ValueError("provenance 缺少已解析依赖")

    spdx = _require_mapping(sbom.get("SPDX"), field="sbom.SPDX")
    if spdx.get("spdxVersion") != "SPDX-2.3":
        raise ValueError("SBOM 必须是 SPDX-2.3")
    packages = _require_list(spdx.get("packages"), field="sbom.SPDX.packages")
    if len(packages) < minimum_sbom_packages:
        raise ValueError("SBOM 包数量低于门槛")

    return {
        "index_digest": index_digest,
        "platform_manifest_digest": platform_digest,
        "attestation_manifest_digest": attestation_digest,
        "platform": expected_platform,
        "image_user": user,
        "build_type": build_type,
        "builder_identity_available": bool(builder_id),
        "builder_id_fingerprint": _fingerprint(builder_id) if builder_id else None,
        "metadata_vcs_available": metadata_vcs_available,
        "source_vcs_fingerprint": _fingerprint(source),
        "resolved_dependency_count": len(resolved_dependencies),
        "sbom_format": "SPDX-2.3",
        "sbom_package_count": len(packages),
    }


def verify_remote_image_attestations(
    *,
    image_reference: str,
    expected_release_sha: str,
    expected_platform: str = "linux/amd64",
    minimum_sbom_packages: int = 1,
    evidence_scope: str = "local_loopback_registry",
) -> dict[str, Any]:
    reference = image_reference.strip()
    if not reference or any(char.isspace() for char in reference) or "://" in reference:
        raise ValueError("image reference 必须是无协议、无空白的 Docker/OCI 引用")
    if evidence_scope not in {"local_loopback_registry", "remote_ci_local_registry"}:
        raise ValueError("不支持的 evidence scope")
    actual_scope = _scope(reference)
    if actual_scope != "local_loopback_registry":
        raise ValueError("本地或远程 CI 证据只允许 localhost 或回环 Registry")

    details = validate_image_attestations(
        manifest=_inspect_json(reference, "Manifest"),
        provenance=_inspect_json(reference, "Provenance"),
        sbom=_inspect_json(reference, "SBOM"),
        image=_inspect_json(reference, "Image"),
        expected_release_sha=expected_release_sha,
        expected_platform=expected_platform,
        minimum_sbom_packages=minimum_sbom_packages,
    )
    return {
        "schema_version": "image-attestation-verification-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "executed": True,
        "passed": True,
        "scope": evidence_scope,
        "registry_endpoint_fingerprint": _fingerprint(_authority(reference)),
        "image_reference": reference,
        "source_git_sha": expected_release_sha,
        **details,
        "provenance_verified": True,
        "sbom_verified": True,
        "signature_verified": False,
        "enterprise_registry_verified": False,
        "cross_host_verified": False,
        "production_ready": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--expected-platform", default="linux/amd64")
    parser.add_argument("--minimum-sbom-packages", type=int, default=1)
    parser.add_argument(
        "--evidence-scope",
        choices=("local_loopback_registry", "remote_ci_local_registry"),
        default="local_loopback_registry",
    )
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = verify_remote_image_attestations(
            image_reference=args.image_reference,
            expected_release_sha=args.expected_release_sha,
            expected_platform=args.expected_platform,
            minimum_sbom_packages=args.minimum_sbom_packages,
            evidence_scope=args.evidence_scope,
        )
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
