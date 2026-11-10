from __future__ import annotations

import json
from datetime import datetime

from app.config import Settings
from app.services.object_storage_probe import run_object_storage_probe


def _settings(**overrides) -> Settings:
    values = {
        "llm_provider": "stub",
        "storage_backend": "minio",
        "minio_endpoint": "minio.internal:9000",
        "minio_access_key": "probe-access-key",
        "minio_secret_key": "probe-secret-key",
        "minio_bucket": "pic-documents",
        "minio_secure": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class MemoryStorage:
    def __init__(self) -> None:
        self.items: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        self.items[key] = data
        return key

    def get(self, key: str) -> bytes:
        return self.items[key]

    def delete(self, key: str) -> None:
        self.items.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.items


def test_storage_probe_does_not_touch_backend_without_explicit_authorization():
    class ForbiddenStorage:
        def put(self, *args, **kwargs):
            raise AssertionError("backend must not be called")

    result = run_object_storage_probe(
        _settings(), allow_real_calls=False, storage=ForbiddenStorage()
    )

    assert result["executed"] is False
    assert result["production_ready"] is False
    assert result["reason"] == "real_storage_calls_not_authorized"


def test_full_storage_probe_verifies_write_read_and_delete_without_leaking_config():
    settings = _settings()
    result = run_object_storage_probe(
        settings,
        allow_real_calls=True,
        action="full",
        probe_id="storage-probe-12345678",
        storage=MemoryStorage(),
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert result["executed"] is True
    assert result["passed"] is True
    assert result["checks"] == {
        "write_succeeded": True,
        "read_matches": True,
        "delete_succeeded": True,
    }
    assert result["shared_storage_ready"] is True
    assert result["production_ready"] is False
    assert datetime.fromisoformat(result["generated_at"]).tzinfo is not None
    assert settings.minio_endpoint not in serialized
    assert settings.minio_access_key not in serialized
    assert settings.minio_secret_key not in serialized


def test_probe_refuses_local_backend_for_shared_storage_evidence():
    result = run_object_storage_probe(
        _settings(storage_backend="local"), allow_real_calls=True
    )

    assert result["executed"] is False
    assert result["reason"] == "shared_storage_not_configured"
    assert result["shared_storage_ready"] is False
    assert result["production_ready"] is False

