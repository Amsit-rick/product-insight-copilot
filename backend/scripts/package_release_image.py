"""Export a locally built immutable image and a SHA-256 release manifest."""

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
DIGEST_RE = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")


def _command(command: list[str]) -> str:
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
        raise RuntimeError((completed.stderr or completed.stdout).strip()[:600])
    return completed.stdout.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _select_repo_digest(values: list[str]) -> str:
    matches = sorted(value for value in values if DIGEST_RE.fullmatch(value))
    if not matches:
        raise RuntimeError("镜像没有可验证的 name@sha256 RepoDigest")
    return matches[0]


def build_manifest(
    *,
    image: str,
    inspect_payload: dict[str, Any],
    archive: Path,
    expected_release_sha: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    labels = (inspect_payload.get("Config") or {}).get("Labels") or {}
    revision = str(labels.get("org.opencontainers.image.revision") or "")
    if not expected_release_sha or revision != expected_release_sha:
        raise RuntimeError(
            f"镜像 revision 标签不匹配：expected={expected_release_sha!r}, actual={revision!r}"
        )
    image_id = str(inspect_payload.get("Id") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("镜像配置摘要缺失或格式错误")
    repo_digest = _select_repo_digest(
        [str(value) for value in inspect_payload.get("RepoDigests") or []]
    )
    if not archive.is_file() or archive.stat().st_size <= 0:
        raise RuntimeError("镜像归档不存在或为空")
    generated_at = (now or datetime.now(UTC)).isoformat()
    return {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "source_git_sha": expected_release_sha,
        "image_input": image,
        "image_revision": revision,
        "image_config_digest": image_id,
        "image_repo_digest": repo_digest,
        "archive_name": archive.name,
        "archive_sha256": _sha256(archive),
        "archive_size_bytes": archive.stat().st_size,
        "artifact_ready": True,
        "registry_published": False,
        "production_ready": False,
    }


def package_release_image(
    *,
    image: str,
    archive: Path,
    manifest: Path,
    expected_release_sha: str,
) -> dict[str, Any]:
    inspected = json.loads(_command(["docker", "image", "inspect", image]))
    if not isinstance(inspected, list) or len(inspected) != 1:
        raise RuntimeError("docker image inspect 未返回唯一镜像")
    archive.parent.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    archive.unlink(missing_ok=True)
    _command(["docker", "image", "save", "--output", str(archive), image])
    payload = build_manifest(
        image=image,
        inspect_payload=inspected[0],
        archive=archive,
        expected_release_sha=expected_release_sha,
    )
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--archive", type=Path, default=ROOT / ".tmp" / "release" / "pic-api.tar")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / ".tmp" / "release" / "pic-api.manifest.json",
    )
    parser.add_argument("--expected-release-sha", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    expected_sha = args.expected_release_sha or _command(["git", "rev-parse", "HEAD"])
    try:
        payload = package_release_image(
            image=args.image,
            archive=args.archive.resolve(),
            manifest=args.manifest.resolve(),
            expected_release_sha=expected_sha,
        )
    except (RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
