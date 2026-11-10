"""生产启动门禁：危险配置必须在任何数据库副作用之前被拒绝。"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.runtime_safety import (
    UnsafeProductionConfig,
    assert_runtime_safe,
    production_config_errors,
)


def make_settings(**overrides) -> Settings:
    base = {
        "app_env": "prod",
        "debug": False,
        "database_url": "postgresql+psycopg://pic:strong-database-password@db.internal:5432/pic",
        "cache_backend": "redis",
        "cors_origins": "https://pic.example.com",
        "auto_seed": False,
        "jwt_secret": "a-production-secret-with-more-than-32-random-chars",
        "task_execution_backend": "queue",
        "worker_embedded": False,
        "checkpointer_backend": "postgres",
        "storage_backend": "minio",
        "minio_endpoint": "storage.internal:9000",
        "minio_access_key": "production-storage-access",
        "minio_secret_key": "production-storage-secret",
        "minio_bucket": "pic-documents",
        "minio_secure": True,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_非生产环境保留本地自动播种体验():
    settings = make_settings(
        app_env="dev",
        auto_seed=True,
        jwt_secret="dev-only-secret-change-me-in-production",
    )

    assert production_config_errors(settings) == []
    assert_runtime_safe(settings)


def test_生产环境拒绝自动播种演示账号():
    settings = make_settings(auto_seed=True)

    with pytest.raises(UnsafeProductionConfig, match="AUTO_SEED"):
        assert_runtime_safe(settings)


def test_生产环境拒绝调试模式和单机依赖():
    debug = make_settings(debug=True)
    sqlite = make_settings(database_url="sqlite:///data/pic.db")
    memory_cache = make_settings(cache_backend="memory")

    assert any("DEBUG" in item for item in production_config_errors(debug))
    assert any("DATABASE_URL" in item for item in production_config_errors(sqlite))
    assert any("CACHE_BACKEND" in item for item in production_config_errors(memory_cache))


def test_生产环境拒绝示例数据库口令():
    settings = make_settings(
        database_url="postgresql+psycopg://pic:pic_dev_password@db.internal:5432/pic"
    )

    errors = production_config_errors(settings)

    assert any("数据库口令" in item for item in errors)
    assert all("pic_dev_password" not in item for item in errors)


@pytest.mark.parametrize(
    "origins",
    [
        "",
        "*",
        "http://pic.example.com",
        "https://localhost:5173",
        "https://user:password@pic.example.com",
        "https://pic.example.com/#fragment",
    ],
)
def test_生产环境拒绝危险_cors(origins):
    errors = production_config_errors(make_settings(cors_origins=origins))

    assert any("CORS_ORIGINS" in item for item in errors)


def test_生产环境接受多个明确_https_cors来源():
    settings = make_settings(
        cors_origins="https://pic.example.com,https://admin.example.com"
    )

    assert production_config_errors(settings) == []


def test_生产环境拒绝关闭登录限流():
    settings = make_settings(login_attempt_rate_limit_per_minute=0)

    with pytest.raises(UnsafeProductionConfig, match="LOGIN_ATTEMPT_RATE_LIMIT_PER_MINUTE"):
        assert_runtime_safe(settings)


def test_生产环境拒绝关闭_secure_cookie():
    settings = make_settings(auth_cookie_secure=False)

    with pytest.raises(UnsafeProductionConfig, match="AUTH_COOKIE_SECURE"):
        assert_runtime_safe(settings)


def test_生产环境拒绝进程内任务执行后端():
    settings = make_settings(task_execution_backend="threadpool")

    with pytest.raises(UnsafeProductionConfig, match="TASK_EXECUTION_BACKEND"):
        assert_runtime_safe(settings)


def test_生产环境拒绝_api_进程内嵌_worker():
    settings = make_settings(worker_embedded=True)

    with pytest.raises(UnsafeProductionConfig, match="WORKER_EMBEDDED"):
        assert_runtime_safe(settings)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
def test_生产环境只接受_postgres_checkpointer(backend):
    settings = make_settings(checkpointer_backend=backend)

    with pytest.raises(UnsafeProductionConfig, match="CHECKPOINTER_BACKEND"):
        assert_runtime_safe(settings)


def test_生产环境拒绝会话与_csrf_cookie_重名():
    settings = make_settings(auth_cookie_name="same", auth_csrf_cookie_name="same")

    with pytest.raises(UnsafeProductionConfig, match="Cookie 名称"):
        assert_runtime_safe(settings)


@pytest.mark.parametrize("value", ["*", "10.0.0.0/not-a-prefix", "proxy.internal"])
def test_生产环境拒绝过宽或非法可信代理(value):
    settings = make_settings(forwarded_allow_ips=value)

    with pytest.raises(UnsafeProductionConfig, match="FORWARDED_ALLOW_IPS"):
        assert_runtime_safe(settings)


@pytest.mark.parametrize("value", ["", "10.0.0.10", "10.42.0.0/16", "::1/128"])
def test_生产环境接受空值或明确代理网段(value):
    assert production_config_errors(make_settings(forwarded_allow_ips=value)) == []


@pytest.mark.parametrize(
    "secret",
    [
        "",
        "too-short",
        "dev-only-secret-change-me-in-production",
        "please-generate-a-32byte-random-secret",
        "long-prefix-change-me-with-extra-padding-123456",
    ],
)
def test_生产环境拒绝空短弱或占位_jwt_密钥(secret):
    errors = production_config_errors(make_settings(jwt_secret=secret))

    assert any("JWT_SECRET" in item for item in errors)
    assert all(secret not in item for item in errors if secret)


def test_合规生产配置可以启动():
    settings = make_settings()

    assert production_config_errors(settings) == []
    assert_runtime_safe(settings)


def test_生产环境启用告警_webhook_时必须配置_https地址():
    missing = make_settings(alert_webhook_enabled=True, alert_webhook_url="")
    insecure = make_settings(
        alert_webhook_enabled=True,
        alert_webhook_url="http://alerts.internal/hooks",
    )

    assert any("ALERT_WEBHOOK_URL" in item for item in production_config_errors(missing))
    assert any("HTTPS" in item for item in production_config_errors(insecure))


def test_生产环境告警_webhook_配置为_https时可通过门禁():
    settings = make_settings(
        alert_webhook_enabled=True,
        alert_webhook_url="https://alerts.example.com/hooks/operations",
        alert_webhook_secret="production-webhook-signing-secret",
    )

    assert production_config_errors(settings) == []


def test_生产环境告警_webhook_必须签名且禁止_url片段():
    unsigned = make_settings(
        alert_webhook_enabled=True,
        alert_webhook_url="https://alerts.example.com/hooks/operations",
        alert_webhook_secret="",
    )
    fragmented = make_settings(
        alert_webhook_enabled=True,
        alert_webhook_url="https://alerts.example.com/hooks/operations#ignored",
        alert_webhook_secret="production-webhook-signing-secret",
    )

    assert any("ALERT_WEBHOOK_SECRET" in item for item in production_config_errors(unsigned))
    assert any("片段" in item for item in production_config_errors(fragmented))


def test_生产环境必须配置共享对象存储():
    local = make_settings(storage_backend="local")
    insecure = make_settings(
        minio_endpoint="http://storage.internal:9000",
        minio_secure=False,
    )
    incomplete = make_settings(
        storage_backend="minio",
        minio_endpoint="",
        minio_access_key="",
        minio_secret_key="",
    )

    assert any("STORAGE_BACKEND" in item for item in production_config_errors(local))
    assert any("MINIO_SECURE" in item for item in production_config_errors(insecure))
    errors = production_config_errors(incomplete)
    assert any("MINIO_ENDPOINT" in item for item in errors)
    assert any("MINIO_ACCESS_KEY" in item for item in errors)


def test_生产环境拒绝对象存储默认凭据且不回显():
    settings = make_settings(
        minio_access_key="picminio",
        minio_secret_key="picminio123",
    )

    errors = production_config_errors(settings)

    assert any("默认凭据" in item for item in errors)
    assert all("picminio" not in item for item in errors)
