from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.services import storage as storage_module
from app.services.storage import LocalStorage, MinioStorage, storage_readiness_check


def _settings(**overrides) -> Settings:
    values = {
        "llm_provider": "stub",
        "app_env": "test",
        "storage_backend": "minio",
        "minio_endpoint": "minio:9000",
        "minio_access_key": "storage-test-access",
        "minio_secret_key": "storage-test-secret",
        "minio_bucket": "pic-documents",
        "minio_secure": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.head_bucket_calls = 0

    def head_bucket(self, *, Bucket: str) -> None:
        assert Bucket == "pic-documents"
        self.head_bucket_calls += 1

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:
        assert ContentType
        self.objects[(Bucket, Key)] = Body

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        data = self.objects[(Bucket, Key)]

        class Body:
            def read(self) -> bytes:
                return data

        return {"Body": Body()}

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop((Bucket, Key), None)

    def head_object(self, *, Bucket: str, Key: str) -> None:
        if (Bucket, Key) not in self.objects:
            error = RuntimeError("not found")
            error.response = {"Error": {"Code": "404"}}  # type: ignore[attr-defined]
            raise error


def test_local_storage_rejects_path_traversal(tmp_path):
    backend = LocalStorage(str(tmp_path))

    with pytest.raises(ValueError):
        backend.put("../outside.txt", b"forbidden")
    with pytest.raises(ValueError):
        backend.get("tenant\\..\\outside.txt")


def test_minio_backend_checks_bucket_and_supports_put_get_exists_delete():
    client = FakeS3Client()
    backend = MinioStorage(settings=_settings(), client=client)

    assert client.head_bucket_calls == 1
    assert backend.exists("tenant/probe.bin") is False
    assert backend.put("tenant/probe.bin", b"probe", "application/octet-stream") == "tenant/probe.bin"
    assert backend.exists("tenant/probe.bin") is True
    assert backend.get("tenant/probe.bin") == b"probe"
    backend.delete("tenant/probe.bin")
    assert backend.exists("tenant/probe.bin") is False


def test_explicit_minio_configuration_never_silently_falls_back(monkeypatch):
    storage_module.reset_storage_cache()
    monkeypatch.setattr(storage_module, "get_settings", lambda: _settings())

    class BrokenMinio:
        def __init__(self, *args, **kwargs) -> None:
            raise ConnectionError("minio unavailable")

    monkeypatch.setattr(storage_module, "MinioStorage", BrokenMinio)

    with pytest.raises(ConnectionError):
        storage_module.get_storage()
    assert storage_module._storage is None


def test_storage_readiness_is_required_and_shared_in_production(monkeypatch):
    local = storage_readiness_check(_settings(app_env="prod", storage_backend="local"))
    assert local == {
        "ok": False,
        "required": True,
        "backend": "local",
        "shared": False,
        "error": "production_storage_not_shared",
    }

    client = FakeS3Client()
    monkeypatch.setattr(
        storage_module,
        "get_storage",
        lambda: MinioStorage(settings=_settings(app_env="prod"), client=client),
    )
    shared = storage_readiness_check(_settings(app_env="prod"))
    assert shared == {
        "ok": True,
        "required": True,
        "backend": "minio",
        "shared": True,
    }


def test_storage_readiness_hides_endpoint_and_exception_detail(monkeypatch):
    def fail():
        raise ConnectionError("http://minio.internal:9000 secret detail")

    monkeypatch.setattr(storage_module, "get_storage", fail)
    result = storage_readiness_check(_settings(app_env="prod"))

    assert result["ok"] is False
    assert result["error"] == "ConnectionError"
    serialized = json.dumps(result)
    assert "minio.internal" not in serialized
    assert "storage-test-secret" not in serialized

