"""对象存储：本地目录 / MinIO（S3 兼容）。

原始文档与解析结果统一通过此抽象读写，业务代码不感知后端差异。
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from app.config import Settings, get_settings

BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not key or "\\" in key or "\x00" in key:
        raise ValueError("storage key must be a non-empty POSIX path")
    path = PurePosixPath(key)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("unsafe storage key")
    return path.as_posix()


def minio_endpoint_url(settings: Settings) -> str:
    raw = settings.minio_endpoint.strip()
    if not raw:
        raise ValueError("MINIO_ENDPOINT is required")
    candidate = raw if "://" in raw else f"{'https' if settings.minio_secure else 'http'}://{raw}"
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("MINIO_ENDPOINT must be an HTTP(S) endpoint")
    if parsed.username or parsed.password:
        raise ValueError("MINIO_ENDPOINT must not embed credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("MINIO_ENDPOINT must not include query or fragment")
    return candidate.rstrip("/")


def minio_configuration_errors(settings: Settings) -> list[str]:
    errors: list[str] = []
    try:
        minio_endpoint_url(settings)
    except ValueError as exc:
        errors.append(str(exc))
    if not settings.minio_access_key or not settings.minio_secret_key:
        errors.append("MinIO access credentials are required")
    if not BUCKET_RE.fullmatch(settings.minio_bucket or ""):
        errors.append("MINIO_BUCKET is invalid")
    return errors


class StorageBackend:
    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:  # pragma: no cover
        raise NotImplementedError

    def get(self, key: str) -> bytes:  # pragma: no cover
        raise NotImplementedError

    def delete(self, key: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def exists(self, key: str) -> bool:  # pragma: no cover
        raise NotImplementedError

    def check(self) -> None:  # pragma: no cover
        raise NotImplementedError

    def local_path(self, key: str) -> Path:  # pragma: no cover
        raise NotImplementedError


class LocalStorage(StorageBackend):
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / _validate_key(key)).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("storage key escapes configured root")
        return path

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return key

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def check(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise RuntimeError("local storage root is unavailable")

    def local_path(self, key: str) -> Path:
        return self._path(key)


class MinioStorage(StorageBackend):
    """S3 兼容实现（MinIO / OSS）。需要 boto3。"""

    def __init__(self, settings: Settings | None = None, client: Any | None = None) -> None:
        settings = settings or get_settings()
        errors = minio_configuration_errors(settings)
        if errors:
            raise ValueError("; ".join(errors))
        self._bucket = settings.minio_bucket
        if client is None:
            import boto3  # 延迟导入
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=minio_endpoint_url(settings),
                aws_access_key_id=settings.minio_access_key,
                aws_secret_access_key=settings.minio_secret_key,
                config=Config(
                    connect_timeout=5,
                    read_timeout=10,
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
        self._client = client
        self.check()

    def check(self) -> None:
        self._client.head_bucket(Bucket=self._bucket)

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        key = _validate_key(key)
        self._client.put_object(
            Bucket=self._bucket, Key=key, Body=data, ContentType=content_type or "application/octet-stream"
        )
        return key

    def get(self, key: str) -> bytes:
        key = _validate_key(key)
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def delete(self, key: str) -> None:
        key = _validate_key(key)
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def exists(self, key: str) -> bool:
        key = _validate_key(key)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = str((response.get("Error") or {}).get("Code") or "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def local_path(self, key: str) -> Path:
        raise NotImplementedError("MinIO 后端不提供本地路径")


_storage: StorageBackend | None = None


def get_storage() -> StorageBackend:
    global _storage
    if _storage is None:
        settings = get_settings()
        if settings.storage_backend == "minio":
            _storage = MinioStorage(settings=settings)
        else:
            _storage = LocalStorage(settings.storage_local_dir)
    return _storage


def reset_storage_cache() -> None:
    global _storage
    _storage = None


def storage_readiness_check(settings: Settings) -> dict[str, Any]:
    production = settings.app_env == "prod"
    if settings.storage_backend == "local":
        payload: dict[str, Any] = {
            "ok": not production,
            "required": production,
            "backend": "local",
            "shared": False,
        }
        if production:
            payload["error"] = "production_storage_not_shared"
        return payload
    try:
        backend = get_storage()
        backend.check()
        return {
            "ok": True,
            "required": True,
            "backend": "minio",
            "shared": True,
        }
    except Exception as exc:
        return {
            "ok": False,
            "required": True,
            "backend": "minio",
            "shared": True,
            "error": type(exc).__name__,
        }


def save_upload(filename: str, data: bytes, tenant_id: str, content_type: str | None = None) -> str:
    """保存上传文件，返回 storage_key。"""
    import uuid

    safe_name = Path(filename).name
    key = f"{tenant_id}/{uuid.uuid4().hex}_{safe_name}"
    get_storage().put(key, data, content_type)
    return key


def copy_to_temp(key: str, suffix: str = "") -> Path:
    """把对象内容落到临时文件，供解析器使用。"""
    import tempfile

    storage = get_storage()
    data = storage.get(key)
    fd, path = tempfile.mkstemp(suffix=suffix)
    import os

    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return Path(path)


def move_to_storage(src: Path, key: str) -> str:
    data = src.read_bytes()
    return get_storage().put(key, data)
