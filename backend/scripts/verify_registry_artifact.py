"""Push an image to a registry, verify its remote digest, then pull by digest.

Plain HTTP is accepted only for an explicitly authorized loopback registry. The
result never claims enterprise-registry, cross-host, or production readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$")
TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class RegistryLocation(NamedTuple):
    authority: str
    api_base_url: str
    loopback: bool


def _run(command: list[str], *, allow_failure: bool = False) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0 and not allow_failure:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"命令执行失败 ({' '.join(command[:3])}): {detail[:600]}")
    return completed.stdout.strip()


def resolve_registry_location(
    authority: str,
    *,
    allow_insecure_localhost: bool,
) -> RegistryLocation:
    normalized = authority.strip().rstrip("/")
    if not normalized or "://" in normalized or any(char.isspace() for char in normalized):
        raise ValueError("registry 必须是 host[:port]，不能包含协议、路径或空白")
    parsed = urlparse(f"//{normalized}")
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("registry 不能包含凭据、路径、查询参数或片段")
    hostname = parsed.hostname.lower()
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname == "localhost"
    if allow_insecure_localhost and not loopback:
        raise ValueError("明文 Registry 仅允许 localhost 或回环 IP")
    scheme = "http" if allow_insecure_localhost else "https"
    return RegistryLocation(
        authority=normalized,
        api_base_url=f"{scheme}://{normalized}",
        loopback=loopback,
    )


def _inspect_image(image: str) -> dict[str, Any]:
    payload = json.loads(_run(["docker", "image", "inspect", image]))
    if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
        raise RuntimeError("docker image inspect 未返回唯一镜像")
    return payload[0]


def _revision(payload: dict[str, Any]) -> str:
    labels = (payload.get("Config") or {}).get("Labels") or {}
    return str(labels.get("org.opencontainers.image.revision") or "")


def _manifest_digest(
    location: RegistryLocation,
    repository: str,
    reference: str,
    *,
    timeout_seconds: float,
) -> str:
    repository_path = quote(repository, safe="/")
    reference_path = quote(reference, safe="._-")
    request = Request(
        f"{location.api_base_url}/v2/{repository_path}/manifests/{reference_path}",
        method="HEAD",
        headers={
            "Accept": ", ".join(
                (
                    "application/vnd.oci.image.index.v1+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.docker.distribution.manifest.v2+json",
                )
            ),
            "User-Agent": "pic-registry-verifier/1",
        },
    )
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - validated URL
        digest = str(response.headers.get("Docker-Content-Digest") or "")
    if not DIGEST_RE.fullmatch(digest):
        raise RuntimeError("Registry 未返回合法 Docker-Content-Digest")
    return digest


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def verify_registry_artifact(
    *,
    source_image: str,
    registry: str,
    repository: str,
    tag: str,
    expected_release_sha: str,
    allow_insecure_localhost: bool,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    if not REPOSITORY_RE.fullmatch(repository):
        raise ValueError("repository 格式不合法")
    if not TAG_RE.fullmatch(tag):
        raise ValueError("tag 格式不合法")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_release_sha):
        raise ValueError("expected release SHA 必须是 40 位小写十六进制")

    location = resolve_registry_location(
        registry,
        allow_insecure_localhost=allow_insecure_localhost,
    )
    source = _inspect_image(source_image)
    if _revision(source) != expected_release_sha:
        raise RuntimeError("源镜像 revision 与期望提交不一致")

    tagged_reference = f"{location.authority}/{repository}:{tag}"
    _run(["docker", "image", "tag", source_image, tagged_reference])
    _run(["docker", "image", "push", tagged_reference])
    digest = _manifest_digest(
        location,
        repository,
        tag,
        timeout_seconds=timeout_seconds,
    )
    digest_reference = f"{location.authority}/{repository}@{digest}"

    # Remove registry-local references while preserving the original source tag.
    # The following pull must resolve the manifest through the registry API.
    _run(["docker", "image", "rm", tagged_reference], allow_failure=True)
    _run(["docker", "image", "rm", digest_reference], allow_failure=True)
    _run(["docker", "image", "pull", digest_reference])

    pulled = _inspect_image(digest_reference)
    pulled_revision = _revision(pulled)
    if pulled_revision != expected_release_sha:
        raise RuntimeError("按 digest 拉取后的镜像 revision 不匹配")
    repo_digests = [str(value) for value in pulled.get("RepoDigests") or []]
    if digest_reference not in repo_digests:
        raise RuntimeError("拉取后的镜像未绑定期望 Registry digest")

    user = str((pulled.get("Config") or {}).get("User") or "")
    if user.strip().lower().split(":", 1)[0] in {"", "0", "root"}:
        raise RuntimeError("拉取后的镜像未使用非 root 用户")

    return {
        "schema_version": "registry-artifact-verification-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "executed": True,
        "passed": True,
        "registry_scope": "local_loopback" if location.loopback else "remote_https",
        "registry_endpoint_fingerprint": _fingerprint(location.authority),
        "repository": repository,
        "tag": tag,
        "digest": digest,
        "digest_reference": digest_reference,
        "source_image": source_image,
        "source_git_sha": expected_release_sha,
        "pulled_image_id": str(pulled.get("Id") or ""),
        "pulled_revision": pulled_revision,
        "pulled_user": user,
        "registry_published": True,
        "registry_pull_verified": True,
        "enterprise_registry_verified": False,
        "cross_host_verified": False,
        "production_ready": False,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-image", required=True)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--repository", default="pic-api")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--allow-insecure-localhost", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = verify_registry_artifact(
            source_image=args.source_image,
            registry=args.registry,
            repository=args.repository,
            tag=args.tag,
            expected_release_sha=args.expected_release_sha,
            allow_insecure_localhost=args.allow_insecure_localhost,
            timeout_seconds=args.timeout_seconds,
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
