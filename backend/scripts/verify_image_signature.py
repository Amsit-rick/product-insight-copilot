"""Validate redacted Cosign verification output for one digest-bound image."""

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
COSIGN_IMAGE_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
SIGNATURE_TYPE = "https://sigstore.dev/cosign/sign/v1"


def _read_json(path: str | Path) -> Any:
    source = Path(path).resolve()
    if not source.is_file():
        raise ValueError(f"缺少 Cosign 验签输出：{source.name}")
    if source.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("Cosign 验签输出超过 2 MiB")
    try:
        return json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Cosign 验签输出不是合法 JSON") from exc


def _public_key_fingerprint(path: str | Path) -> str:
    source = Path(path).resolve()
    if not source.is_file():
        raise ValueError("缺少 Cosign 公钥")
    raw = source.read_bytes()
    if len(raw) > 64 * 1024:
        raise ValueError("Cosign 公钥文件过大")
    text = raw.decode("utf-8", errors="strict")
    if "-----BEGIN PUBLIC KEY-----" not in text or "PRIVATE KEY" in text:
        raise ValueError("Cosign 公钥不是受支持的 PEM 公钥")
    return hashlib.sha256(raw).hexdigest()[:16]


def validate_image_signature(
    *,
    verification_payload: Any,
    image_reference: str,
    expected_release_sha: str,
    public_key_fingerprint: str,
    cosign_image: str,
    cosign_version: str,
    evidence_scope: str,
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(expected_release_sha):
        raise ValueError("expected release SHA 必须是 40 位小写十六进制")
    reference = image_reference.strip()
    if "@" not in reference or any(char.isspace() for char in reference):
        raise ValueError("image reference 必须使用 name@sha256:digest")
    repository, digest = reference.rsplit("@", 1)
    if not repository or not DIGEST_RE.fullmatch(digest):
        raise ValueError("image reference digest 不合法")
    if not COSIGN_IMAGE_RE.fullmatch(cosign_image):
        raise ValueError("Cosign 工具镜像必须按 sha256 digest 固定")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", cosign_version):
        raise ValueError("Cosign version 必须是 vMAJOR.MINOR.PATCH")
    if evidence_scope not in {"local_loopback_registry", "remote_ci_local_registry"}:
        raise ValueError("不支持的 evidence scope")
    if not re.fullmatch(r"[0-9a-f]{16}", public_key_fingerprint):
        raise ValueError("公钥指纹不合法")
    if not isinstance(verification_payload, list) or not verification_payload:
        raise ValueError("Cosign 没有返回有效签名")

    for entry in verification_payload:
        if not isinstance(entry, dict):
            raise ValueError("Cosign 签名条目不是对象")
        critical = entry.get("critical")
        optional = entry.get("optional")
        if not isinstance(critical, dict) or not isinstance(optional, dict):
            raise ValueError("Cosign 签名缺少 critical/optional claims")
        identity = critical.get("identity")
        image = critical.get("image")
        if not isinstance(identity, dict) or not isinstance(image, dict):
            raise ValueError("Cosign 签名缺少 identity/image claims")
        if critical.get("type") != SIGNATURE_TYPE:
            raise ValueError("Cosign 签名类型不受支持")
        if identity.get("docker-reference") != reference:
            raise ValueError("Cosign 签名引用与目标镜像 digest 不一致")
        if image.get("docker-manifest-digest") != digest:
            raise ValueError("Cosign 签名 manifest digest 与目标不一致")
        if optional.get("release_sha") != expected_release_sha:
            raise ValueError("Cosign release_sha 注解与期望提交不一致")

    return {
        "image_reference": reference,
        "image_digest": digest,
        "source_git_sha": expected_release_sha,
        "signature_count": len(verification_payload),
        "public_key_fingerprint": public_key_fingerprint,
        "cosign_image": cosign_image,
        "cosign_version": cosign_version,
        "signature_verified": True,
        "release_annotation_verified": True,
        "transparency_log_verified": False,
        "keyless_identity_verified": False,
        "kms_verified": False,
        "enterprise_registry_verified": False,
        "cross_host_verified": False,
        "scope": evidence_scope,
    }


def verify_image_signature(
    *,
    verification_json: str | Path,
    public_key: str | Path,
    image_reference: str,
    expected_release_sha: str,
    cosign_image: str,
    cosign_version: str,
    evidence_scope: str,
) -> dict[str, Any]:
    details = validate_image_signature(
        verification_payload=_read_json(verification_json),
        image_reference=image_reference,
        expected_release_sha=expected_release_sha,
        public_key_fingerprint=_public_key_fingerprint(public_key),
        cosign_image=cosign_image,
        cosign_version=cosign_version,
        evidence_scope=evidence_scope,
    )
    return {
        "schema_version": "image-signature-verification-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "executed": True,
        "passed": True,
        **details,
        "key_management": "ephemeral_local_key",
        "production_ready": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verification-json", required=True, type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--cosign-image", required=True)
    parser.add_argument("--cosign-version", required=True)
    parser.add_argument(
        "--evidence-scope",
        choices=("local_loopback_registry", "remote_ci_local_registry"),
        default="local_loopback_registry",
    )
    parser.add_argument("--json-out", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = verify_image_signature(
            verification_json=args.verification_json,
            public_key=args.public_key,
            image_reference=args.image_reference,
            expected_release_sha=args.expected_release_sha,
            cosign_image=args.cosign_image,
            cosign_version=args.cosign_version,
            evidence_scope=args.evidence_scope,
        )
    except (ValueError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
