"""生产运行时安全门禁。

Preflight 适合发布前给出完整报告，但不能假设每次启动都有人先运行它。
这里仅拦截会直接造成账号或令牌泄露风险的配置，并且必须在建表、播种之前执行。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

JWT_SECRET_PLACEHOLDERS = frozenset(
    {
        "dev-only-secret-change-me-in-production",
        "please-generate-a-32byte-random-secret",
        "change-me",
        "secret",
        "test",
    }
)
JWT_SECRET_PLACEHOLDER_MARKERS = ("change-me", "please-generate")
DATABASE_PASSWORD_PLACEHOLDERS = frozenset(
    {
        "pic_dev_password",
        "please-change-me",
        "change-me",
        "password",
    }
)
STORAGE_CREDENTIAL_PLACEHOLDERS = frozenset(
    {
        "picminio",
        "picminio123",
        "please-change-me",
        "replace-with-least-privilege-access-key",
        "replace-with-least-privilege-secret-key",
    }
)
LOCAL_CORS_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class UnsafeProductionConfig(RuntimeError):
    """生产环境包含明确不安全的配置。"""


def jwt_secret_error(secret: str | None) -> str | None:
    """返回 JWT 密钥的安全问题，不回显密钥本身。"""

    normalized = (secret or "").strip()
    lowered = normalized.lower()
    if not normalized:
        return "JWT_SECRET 为空"
    if lowered in JWT_SECRET_PLACEHOLDERS or any(
        marker in lowered for marker in JWT_SECRET_PLACEHOLDER_MARKERS
    ):
        return "JWT_SECRET 仍是示例占位值"
    if len(normalized) < 32:
        return f"JWT_SECRET 长度 {len(normalized)} < 32"
    return None


def production_config_errors(settings) -> list[str]:  # noqa: ANN001
    """列出生产环境的启动阻断项；非生产环境不施加此门禁。"""

    if settings.app_env != "prod":
        return []

    errors: list[str] = []
    if settings.debug:
        errors.append("APP_ENV=prod 时 DEBUG 必须为 false")
    if not settings.is_postgres:
        errors.append("APP_ENV=prod 时 DATABASE_URL 必须使用 PostgreSQL")
    else:
        parsed_database = urlparse(settings.database_url)
        database_password = (parsed_database.password or "").strip().lower()
        if database_password in DATABASE_PASSWORD_PLACEHOLDERS:
            errors.append("APP_ENV=prod 时 DATABASE_URL 禁止使用示例数据库口令")
    if settings.cache_backend != "redis":
        errors.append("APP_ENV=prod 时 CACHE_BACKEND 必须为 redis，禁止使用进程内或单机回退缓存")
    if settings.auto_seed:
        errors.append("APP_ENV=prod 时 AUTO_SEED 必须为 false，禁止自动创建演示账号")
    if settings.login_attempt_rate_limit_per_minute <= 0:
        errors.append("APP_ENV=prod 时 LOGIN_ATTEMPT_RATE_LIMIT_PER_MINUTE 必须大于 0")
    if settings.task_execution_backend != "queue":
        errors.append("APP_ENV=prod 时 TASK_EXECUTION_BACKEND 必须为 queue，禁止使用进程内执行后端")
    if settings.worker_embedded:
        errors.append("APP_ENV=prod 时 WORKER_EMBEDDED 必须为 false，API 与 Worker 必须独立部署")
    if settings.checkpointer_backend != "postgres":
        errors.append(
            "APP_ENV=prod 时 CHECKPOINTER_BACKEND 必须为 postgres，"
            "禁止使用无法跨副本续接的 memory/sqlite 检查点"
        )
    if not settings.auth_cookie_secure_effective:
        errors.append("APP_ENV=prod 时 AUTH_COOKIE_SECURE 必须为 true")
    cookie_names = (settings.auth_cookie_name.strip(), settings.auth_csrf_cookie_name.strip())
    if not all(cookie_names):
        errors.append("认证 Cookie 名称不能为空")
    elif cookie_names[0] == cookie_names[1]:
        errors.append("会话 Cookie 与 CSRF Cookie 名称不能相同")
    if not settings.auth_csrf_header_name.strip():
        errors.append("AUTH_CSRF_HEADER_NAME 不能为空")
    if settings.storage_backend != "minio":
        errors.append("APP_ENV=prod 时 STORAGE_BACKEND 必须为 minio 或企业 S3 兼容共享存储")
    else:
        endpoint = settings.minio_endpoint.strip()
        if not endpoint:
            errors.append("APP_ENV=prod 时 MINIO_ENDPOINT 不能为空")
        else:
            parsed_storage = urlparse(
                endpoint if "://" in endpoint else f"{'https' if settings.minio_secure else 'http'}://{endpoint}"
            )
            if not settings.minio_secure or parsed_storage.scheme.lower() != "https":
                errors.append("APP_ENV=prod 时 MINIO_SECURE 必须为 true 且对象存储端点必须使用 HTTPS")
        if not settings.minio_access_key or not settings.minio_secret_key:
            errors.append("APP_ENV=prod 时 MINIO_ACCESS_KEY / MINIO_SECRET_KEY 不能为空")
        storage_credentials = {
            settings.minio_access_key.strip().lower(),
            settings.minio_secret_key.strip().lower(),
        }
        if storage_credentials & STORAGE_CREDENTIAL_PLACEHOLDERS:
            errors.append("APP_ENV=prod 时对象存储禁止使用示例或默认凭据")

    cors_origins = [
        item.strip() for item in (settings.cors_origins or "").split(",") if item.strip()
    ]
    if not cors_origins:
        errors.append("APP_ENV=prod 时 CORS_ORIGINS 不能为空")
    for origin in cors_origins:
        if origin == "*":
            errors.append("APP_ENV=prod 时 CORS_ORIGINS 禁止使用 *")
            continue
        parsed_origin = urlparse(origin)
        if parsed_origin.scheme.lower() != "https" or not parsed_origin.hostname:
            errors.append("APP_ENV=prod 时 CORS_ORIGINS 必须全部使用完整 HTTPS 来源")
            continue
        if parsed_origin.hostname.lower() in LOCAL_CORS_HOSTS:
            errors.append("APP_ENV=prod 时 CORS_ORIGINS 禁止使用本机开发地址")
        if parsed_origin.username or parsed_origin.password or parsed_origin.fragment:
            errors.append("APP_ENV=prod 时 CORS_ORIGINS 禁止包含凭据或 URL 片段")

    proxy_entries = [
        item.strip() for item in (settings.forwarded_allow_ips or "").split(",") if item.strip()
    ]
    if "*" in proxy_entries:
        errors.append("APP_ENV=prod 时 FORWARDED_ALLOW_IPS 禁止使用 *")
    for entry in proxy_entries:
        if entry == "*":
            continue
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            errors.append(f"FORWARDED_ALLOW_IPS 包含非法 IP/CIDR：{entry}")

    secret_error = jwt_secret_error(settings.jwt_secret)
    if secret_error:
        errors.append(secret_error)
    if settings.alert_webhook_enabled:
        parsed = urlparse(settings.alert_webhook_url.strip())
        if not parsed.scheme or not parsed.netloc:
            errors.append("启用告警 Webhook 时 ALERT_WEBHOOK_URL 必须配置为完整地址")
        elif parsed.scheme.lower() != "https":
            errors.append("APP_ENV=prod 时告警 Webhook 必须使用 HTTPS")
        if parsed.username or parsed.password:
            errors.append("ALERT_WEBHOOK_URL 禁止内嵌用户名或口令")
        if parsed.fragment:
            errors.append("ALERT_WEBHOOK_URL 禁止包含 URL 片段")
        if not settings.alert_webhook_secret:
            errors.append("APP_ENV=prod 启用告警 Webhook 时必须配置 ALERT_WEBHOOK_SECRET")
    return errors


def assert_runtime_safe(settings) -> None:  # noqa: ANN001
    """在任何启动副作用发生前拒绝不安全的生产配置。"""

    errors = production_config_errors(settings)
    if errors:
        raise UnsafeProductionConfig("生产配置安全检查失败：" + "；".join(errors))
