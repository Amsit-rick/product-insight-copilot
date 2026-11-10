from __future__ import annotations

import hashlib
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "package_release_image.py"
spec = importlib.util.spec_from_file_location("package_release_image_under_test", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def _inspect(revision: str) -> dict:
    return {
        "Id": "sha256:" + "1" * 64,
        "RepoDigests": ["registry.example.test/pic-api@sha256:" + "2" * 64],
        "Config": {"Labels": {"org.opencontainers.image.revision": revision}},
    }


def test_manifest_binds_revision_digest_and_archive_hash(tmp_path: Path):
    archive = tmp_path / "pic-api.tar"
    archive.write_bytes(b"immutable-image")
    revision = "a" * 40

    payload = module.build_manifest(
        image="pic-api:test",
        inspect_payload=_inspect(revision),
        archive=archive,
        expected_release_sha=revision,
        now=datetime(2026, 11, 9, tzinfo=UTC),
    )

    assert payload["source_git_sha"] == revision
    assert payload["image_repo_digest"].endswith("2" * 64)
    assert payload["archive_sha256"] == hashlib.sha256(b"immutable-image").hexdigest()
    assert payload["registry_published"] is False
    assert payload["production_ready"] is False


def test_manifest_rejects_revision_mismatch(tmp_path: Path):
    archive = tmp_path / "pic-api.tar"
    archive.write_bytes(b"image")

    with pytest.raises(RuntimeError, match="revision 标签不匹配"):
        module.build_manifest(
            image="pic-api:test",
            inspect_payload=_inspect("old"),
            archive=archive,
            expected_release_sha="new",
        )
