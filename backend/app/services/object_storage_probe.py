"""Redacted conformance probe for shared S3-compatible object storage."""

from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone
from typing import Any

from app.config import Settings
from app.services.storage import MinioStorage, minio_configuration_errors

SCHEMA_VERSION = "object-storage-probe-v1"
PROBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
ACTIONS = {"full", "write", "read", "delete"}


def inspect_object_storage_configuration(settings: Settings) -> dict[str, Any]:
    checks = {
        "shared_backend": settings.storage_backend == "minio",
        "endpoint_configured": bool(settings.minio_endpoint.strip()),
        "credentials_configured": bool(
            settings.minio_access_key and settings.minio_secret_key
        ),
        "bucket_configured": bool(settings.minio_bucket.strip()),
    }
    if settings.storage_backend == "minio":
        checks["configuration_valid"] = not minio_configuration_errors(settings)
    else:
        checks["configuration_valid"] = False
    return {"passed": all(checks.values()), "checks": checks}


def run_object_storage_probe(
    settings: Settings,
    *,
    allow_real_calls: bool = False,
    action: str = "full",
    probe_id: str = "storage-probe-default",
    storage: Any | None = None,
) -> dict[str, Any]:
    configuration = inspect_object_storage_configuration(settings)
    base = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "configuration": configuration,
        "action": action,
    }
    if not allow_real_calls:
        return {
            **base,
            "executed": False,
            "passed": False,
            "shared_storage_ready": False,
            "production_ready": False,
            "reason": "real_storage_calls_not_authorized",
        }
    if not configuration["passed"]:
        return {
            **base,
            "executed": False,
            "passed": False,
            "shared_storage_ready": False,
            "production_ready": False,
            "reason": "shared_storage_not_configured",
        }
    if action not in ACTIONS:
        raise ValueError(f"unsupported storage probe action: {action}")
    if not PROBE_ID_RE.fullmatch(probe_id):
        raise ValueError("probe_id must contain 8-64 safe characters")

    backend = storage or MinioStorage(settings=settings)
    key = f"integration-probes/{probe_id}.bin"
    payload = hashlib.sha256(f"pic-object-storage:{probe_id}".encode("utf-8")).digest()
    checks: dict[str, bool] = {}
    started = time.perf_counter()
    try:
        if action in {"full", "write"}:
            backend.put(key, payload, "application/octet-stream")
            checks["write_succeeded"] = backend.exists(key)
        if action in {"full", "read"}:
            checks["read_matches"] = backend.get(key) == payload
        if action in {"full", "delete"}:
            backend.delete(key)
            checks["delete_succeeded"] = not backend.exists(key)
    except Exception as exc:
        return {
            **base,
            "executed": True,
            "passed": False,
            "shared_storage_ready": False,
            "production_ready": False,
            "failure_type": type(exc).__name__,
        }
    passed = bool(checks) and all(checks.values())
    return {
        **base,
        "executed": True,
        "passed": passed,
        "shared_storage_ready": passed and action == "full",
        # 该探针只证明对象存储链路，不代表模型、事实接口、Webhook、备份等生产条件完成。
        "production_ready": False,
        "checks": checks,
        "payload_bytes": len(payload),
        "latency_ms": int((time.perf_counter() - started) * 1000),
    }


__all__ = [
    "SCHEMA_VERSION",
    "inspect_object_storage_configuration",
    "run_object_storage_probe",
]
