"""发布前自检（preflight）的单元测试。

自检脚本的价值全在"判定是否可信"：把不该放的放过去（漏报）比不做更危险，
因为它给人一种"检查过了"的错觉。所以这里逐条钉住判定边界：

- 生产相关风险（默认密钥、本地 CORS、threadpool 执行、进程内 checkpointer、不限额）必须是 WARN + production_critical；
- 硬问题（缺密钥、迁移缺表）必须 FAIL；
- `--strict` 下"生产不宜"的 WARN 必须升格为 FAIL。

被导入的是脚本模块（不是包），用 importlib 从文件加载，避免污染 sys.path 的语义。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.config import Settings

BACKEND = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "preflight_check", BACKEND / "scripts" / "preflight_check.py"
)
assert _SPEC and _SPEC.loader
preflight = importlib.util.module_from_spec(_SPEC)
# 必须先注册进 sys.modules 再 exec：脚本里定义了 @dataclass，
# 而 dataclasses 会通过 sys.modules[cls.__module__] 查模块，不注册就会报
# "AttributeError: 'NoneType' object has no attribute '__dict__'"
sys.modules[_SPEC.name] = preflight
_SPEC.loader.exec_module(preflight)

FAIL = preflight.FAIL
PASS = preflight.PASS
WARN = preflight.WARN


def make_settings(**overrides) -> Settings:
    base = {
        "app_env": "local",
        "debug": False,
        "cors_origins": "https://pic.example.com",
        "jwt_secret": "x" * 40,
        "llm_provider": "openai_compatible",
        "llm_api_key": "sk-test",
        "embedding_provider": "openai_compatible",
        "embedding_base_url": "https://embedding.example.test/v1",
        "embedding_api_key": "embedding-test",
        "reranker_endpoint": "https://reranker.example.test/rerank",
        "alert_webhook_enabled": True,
        "alert_webhook_url": "https://alerts.example.test/pic",
        "alert_webhook_secret": "webhook-test-secret",
        "cache_backend": "redis",
        "storage_backend": "minio",
        "minio_endpoint": "minio.internal:9000",
        "minio_access_key": "minio-test",
        "minio_secret_key": "minio-secret-test",
        "task_execution_backend": "queue",
        "worker_embedded": False,
        "checkpointer_backend": "sqlite",
        "task_token_limit": 100000,
        "login_attempt_rate_limit_per_minute": 10,
        "auto_seed": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# --------------------------------------------------------------------- 模型网关
def test_stub_模型在生产环境属于高风险():
    result = preflight.check_llm_config(make_settings(llm_provider="stub"))

    assert result.status == WARN
    assert result.production_critical is True
    assert "stub" in result.detail


def test_配了provider却没密钥必须失败():
    result = preflight.check_llm_config(make_settings(llm_api_key=""))

    assert result.status == FAIL
    assert "LLM_API_KEY" in result.detail


def test_stub_embedding在生产环境属于高风险():
    result = preflight.check_embedding_config(make_settings(embedding_provider="stub"))

    assert result.status == WARN
    assert result.production_critical is True


def test_真实embedding缺少端点或密钥必须失败():
    missing_endpoint = preflight.check_embedding_config(
        make_settings(embedding_base_url="", llm_base_url="")
    )
    missing_key = preflight.check_embedding_config(
        make_settings(embedding_api_key="", llm_api_key="")
    )

    assert missing_endpoint.status == FAIL
    assert missing_key.status == FAIL


def test_未配置reranker属于生产风险():
    result = preflight.check_reranker_config(make_settings(reranker_endpoint=""))

    assert result.status == WARN
    assert result.production_critical is True


def test_reranker端点禁止内嵌口令():
    result = preflight.check_reranker_config(
        make_settings(reranker_endpoint="https://user:password@reranker.example.test/rerank")
    )

    assert result.status == FAIL
    assert "口令" in result.detail


def test_外部告警关闭或无签名属于生产风险():
    disabled = preflight.check_alert_webhook(make_settings(alert_webhook_enabled=False))
    unsigned = preflight.check_alert_webhook(make_settings(alert_webhook_secret=""))

    assert disabled.status == WARN
    assert disabled.production_critical is True
    assert unsigned.status == WARN
    assert unsigned.production_critical is True


def test_外部告警必须使用https且禁止url口令和片段():
    insecure = preflight.check_alert_webhook(
        make_settings(alert_webhook_url="http://alerts.example.test/pic")
    )
    embedded_password = preflight.check_alert_webhook(
        make_settings(alert_webhook_url="https://user:password@alerts.example.test/pic")
    )
    fragmented = preflight.check_alert_webhook(
        make_settings(alert_webhook_url="https://alerts.example.test/pic#ignored")
    )

    assert insecure.status == FAIL
    assert embedded_password.status == FAIL
    assert fragmented.status == FAIL
    assert "片段" in fragmented.detail


# --------------------------------------------------------------- 事实适配器
def test_mock_事实适配器属于生产风险():
    result = preflight.check_fact_adapter(make_settings(ticket_use_mock_adapters=True))

    assert result.status == WARN
    assert result.production_critical is True


def test_真实事实适配器缺端点必须失败():
    result = preflight.check_fact_adapter(
        make_settings(ticket_use_mock_adapters=False, ticket_fact_order_url="")
    )

    assert result.status == FAIL
    assert "TICKET_FACT_ORDER_URL" in result.detail


def test_事实端点必须显式包含订单号占位符():
    result = preflight.check_fact_adapter(
        make_settings(
            ticket_use_mock_adapters=False,
            ticket_fact_order_url="https://facts.example.test/orders",
            ticket_fact_payment_url="https://facts.example.test/payments/{order_id}",
            ticket_fact_logistics_url="https://facts.example.test/logistics/{order_id}",
            ticket_fact_aftersale_url="https://facts.example.test/aftersales/{order_id}",
        )
    )

    assert result.status == FAIL
    assert "{order_id}" in result.detail


def test_真实事实适配器配置完整时通过且不回显鉴权头():
    result = preflight.check_fact_adapter(
        make_settings(
            ticket_use_mock_adapters=False,
            ticket_fact_order_url="https://facts.example.test/orders/{order_id}",
            ticket_fact_payment_url="https://facts.example.test/payments/{order_id}",
            ticket_fact_logistics_url="https://facts.example.test/logistics/{order_id}",
            ticket_fact_aftersale_url="https://facts.example.test/aftersales/{order_id}",
            ticket_fact_auth_headers={"Authorization": "Bearer super-secret"},
        )
    )

    assert result.status == PASS
    assert "super-secret" not in result.detail


def test_指标源默认关闭属于生产风险():
    result = preflight.check_metric_source(
        make_settings(metric_source_http_enabled=False, metric_source_http_url="")
    )

    assert result.status == WARN
    assert result.production_critical is True


def test_指标源启用但端点不安全必须失败():
    result = preflight.check_metric_source(
        make_settings(
            app_env="prod",
            metric_source_http_enabled=True,
            metric_source_http_url="http://metrics.example.test/snapshots",
        )
    )

    assert result.status == FAIL
    assert "HTTPS" in result.detail


def test_指标源配置完整时通过且不回显鉴权头():
    settings = make_settings(
        metric_source_http_enabled=True,
        metric_source_http_url="https://metrics.example.test/snapshots",
        metric_source_http_auth_headers={"Authorization": "Bearer metric-secret"},
        proactive_anomaly_metrics=["next_day_retention"],
        proactive_anomaly_max_data_age_by_metric={"next_day_retention": 1},
    )

    result = preflight.check_metric_source(settings)

    assert result.status == PASS
    assert "metric-secret" not in result.detail
    assert "1 个指标" in result.detail


def test_随仓库提供的事实验收计划有效():
    result = preflight.check_fact_probe_plan(make_settings())

    assert result.status == PASS
    assert "schema=fact-integration-probe-v1" in result.detail


def test_容器内同级docs布局的事实验收计划有效(monkeypatch, tmp_path):
    backend_dir = tmp_path / "app"
    plan_path = backend_dir / "docs" / "integrations" / "fact-probe-plan.example.json"
    plan_path.parent.mkdir(parents=True)
    source = BACKEND.parent / "docs" / "integrations" / "fact-probe-plan.example.json"
    plan_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(preflight, "BACKEND", backend_dir)

    result = preflight.check_fact_probe_plan(make_settings())

    assert result.status == PASS
    assert "场景 3 个" in result.detail


def test_事实验收计划损坏时阻断(monkeypatch, tmp_path):
    plan_path = tmp_path / "docs" / "integrations" / "fact-probe-plan.example.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(preflight, "BACKEND", tmp_path / "backend")

    result = preflight.check_fact_probe_plan(make_settings())

    assert result.status == FAIL
    assert "计划无效" in result.detail


def test_密钥不回显():
    result = preflight.check_llm_config(make_settings(llm_api_key="sk-super-secret"))

    assert result.status == PASS
    assert "sk-super-secret" not in result.detail


# ----------------------------------------------------------------------- 密钥
@pytest.mark.parametrize("secret", ["", "   "])
def test_空密钥失败(secret):
    assert preflight.check_jwt_secret(make_settings(jwt_secret=secret)).status == FAIL


def test_默认密钥判为生产风险():
    result = preflight.check_jwt_secret(make_settings(jwt_secret="dev-only-secret-change-me-in-production"))

    assert result.status == WARN
    assert result.production_critical is True


@pytest.mark.parametrize(
    "secret",
    [
        "please-generate-a-32byte-random-secret",
        "prefix-please-generate-a-longer-secret-value",
        "prefix-change-me-and-pad-to-more-than-32-bytes",
    ],
)
def test_长占位密钥也不能绕过检查(secret):
    result = preflight.check_jwt_secret(make_settings(jwt_secret=secret))

    assert result.status == WARN
    assert result.production_critical is True


def test_密钥过短提示加长():
    result = preflight.check_jwt_secret(make_settings(jwt_secret="short-secret"))

    assert result.status == WARN
    assert "32" in result.detail


def test_合规密钥通过():
    assert preflight.check_jwt_secret(make_settings(jwt_secret="y" * 48)).status == PASS


def test_生产_cookie_安全配置通过():
    result = preflight.check_auth_cookies(make_settings(app_env="prod"))

    assert result.status == PASS
    assert "HttpOnly" in result.detail


def test_显式关闭_secure_cookie_属于生产风险():
    result = preflight.check_auth_cookies(
        make_settings(app_env="prod", auth_cookie_secure=False)
    )

    assert result.status == WARN
    assert result.production_critical is True


def test_会话与_csrf_cookie_不能重名():
    result = preflight.check_auth_cookies(
        make_settings(auth_cookie_name="same", auth_csrf_cookie_name="same")
    )

    assert result.status == FAIL


# ------------------------------------------------------------- 调试与 CORS
def test_生产开_debug_是风险():
    assert preflight.check_debug(make_settings(debug=True)).production_critical is True
    assert preflight.check_debug(make_settings(debug=False)).status == PASS


@pytest.mark.parametrize(
    ("origins", "expect"),
    [
        ("http://localhost:5173", WARN),
        ("*", WARN),
        ("", WARN),
        ("https://pic.example.com", PASS),
    ],
)
def test_cors_判定(origins, expect):
    assert preflight.check_cors(make_settings(cors_origins=origins)).status == expect


def test_本地_cors_是生产风险():
    result = preflight.check_cors(make_settings(cors_origins="http://127.0.0.1:5173"))

    assert result.production_critical is True


# ------------------------------------------------------------------- 预算限流
def test_预算全为零视为不限额():
    result = preflight.check_budget(
        make_settings(task_token_limit=0, task_cost_limit=0.0, tenant_daily_cost_limit=0.0)
    )

    assert result.status == WARN
    assert result.production_critical is True


def test_设置了任一预算即通过():
    result = preflight.check_budget(
        make_settings(task_token_limit=0, task_cost_limit=5.0, tenant_daily_cost_limit=0.0)
    )

    assert result.status == PASS
    assert "5.0" in result.detail


def test_关闭限流是风险():
    result = preflight.check_rate_limit(make_settings(task_submit_rate_limit_per_minute=0))

    assert result.status == WARN
    assert result.production_critical is True


def test_非_redis_限流只提示不阻断():
    result = preflight.check_rate_limit(make_settings(cache_backend="memory"))

    assert result.status == WARN
    assert result.production_critical is False


def test_redis_限流通过():
    assert preflight.check_rate_limit(make_settings(cache_backend="redis")).status == PASS


def test_关闭登录限流是生产风险():
    result = preflight.check_login_rate_limit(
        make_settings(login_attempt_rate_limit_per_minute=0)
    )

    assert result.status == WARN
    assert result.production_critical is True


def test_登录限流使用_redis_时通过():
    result = preflight.check_login_rate_limit(
        make_settings(login_attempt_rate_limit_per_minute=8, cache_backend="redis")
    )

    assert result.status == PASS
    assert "8/分钟" in result.detail


def test_local_login_counter_blocks_strict_release(monkeypatch):
    monkeypatch.setattr(preflight, "CHECKS", [("配置", "登录限流", preflight.check_login_rate_limit)])
    results = preflight.run_checks(make_settings(cache_backend="memory"), strict=True, with_eval=False)
    assert results[0].status == FAIL


def test_自动播种演示账号是生产风险():
    result = preflight.check_auto_seed(make_settings(auto_seed=True))

    assert result.status == WARN
    assert result.production_critical is True


def test_关闭自动播种后通过():
    assert preflight.check_auto_seed(make_settings(auto_seed=False)).status == PASS


def test_默认不信任转发头通过():
    result = preflight.check_proxy_trust(make_settings(forwarded_allow_ips=""))

    assert result.status == PASS
    assert "TCP" in result.detail


def test_可信代理通配符是生产风险():
    result = preflight.check_proxy_trust(make_settings(forwarded_allow_ips="*"))

    assert result.status == WARN
    assert result.production_critical is True


def test_明确可信代理网段通过():
    result = preflight.check_proxy_trust(
        make_settings(forwarded_allow_ips="10.42.0.0/16,192.0.2.10")
    )

    assert result.status == PASS
    assert "2 个" in result.detail


# --------------------------------------------------------------- 执行与恢复
@pytest.mark.parametrize(
    ("backend", "embedded", "expect", "critical"),
    [
        ("inline", False, WARN, True),
        ("threadpool", False, WARN, True),
        ("queue", True, WARN, False),
        ("queue", False, PASS, False),
    ],
)
def test_执行后端判定(backend, embedded, expect, critical):
    result = preflight.check_execution_backend(
        make_settings(task_execution_backend=backend, worker_embedded=embedded)
    )

    assert result.status == expect
    assert result.production_critical is critical


def test_队列后端配内存_checkpointer_是硬风险():
    result = preflight.check_checkpointer(
        make_settings(task_execution_backend="queue", checkpointer_backend="memory")
    )

    assert result.status == WARN
    assert result.production_critical is True


def test_sqlite_checkpointer_通过():
    assert preflight.check_checkpointer(make_settings(checkpointer_backend="sqlite")).status == PASS


def test_未知_checkpointer_后端直接失败():
    """防御性分支：`checkpointer_backend` 是 Literal，正常配置路径进不来非法值，
    但检查函数仍要对"字典/环境变量被绕过"这种情况给出 FAIL，而不是当成通过。"""
    settings = make_settings().model_copy(update={"checkpointer_backend": "redis"})

    assert preflight.check_checkpointer(settings).status == FAIL


def test_postgres_checkpointer_按依赖是否装好给结论():
    """装了就 PASS；没装必须 FAIL（不能静默降级成内存）。"""
    try:
        import langgraph.checkpoint.postgres  # noqa: F401

        installed = True
    except ImportError:
        installed = False

    result = preflight.check_checkpointer(make_settings(checkpointer_backend="postgres"))

    if installed:
        assert result.status == PASS
    else:
        assert result.status == FAIL
        assert "langgraph-checkpoint-postgres" in result.detail


# ------------------------------------------------------------------- 存储
def test_本地存储属于生产风险():
    result = preflight.check_storage(make_settings(storage_backend="local"))

    assert result.status == WARN
    assert result.production_critical is True


def test_minio缺少密钥必须失败():
    result = preflight.check_storage(make_settings(minio_access_key="", minio_secret_key=""))

    assert result.status == FAIL
    assert "密钥" in result.detail


def test_生产环境_minio_指向本地是风险():
    result = preflight.check_storage(
        make_settings(storage_backend="minio", minio_endpoint="localhost:9000", app_env="prod")
    )

    assert result.status == WARN
    assert result.production_critical is True


def test_minio检查结果不回显端点():
    settings = make_settings(minio_endpoint="storage.internal.example:9000")
    result = preflight.check_storage(settings)

    assert result.status == PASS
    assert settings.minio_endpoint not in result.detail


# ------------------------------------------------------------------- 聚合
def test_strict_模式把生产风险升格为失败():
    settings = make_settings(
        jwt_secret="dev-only-secret-change-me-in-production",
        task_execution_backend="threadpool",
        checkpointer_backend="memory",
        task_token_limit=0,
    )

    normal = preflight.run_checks(settings, strict=False, with_eval=False)
    strict = preflight.run_checks(settings, strict=True, with_eval=False)

    normal_fail = {item.name for item in normal if item.status == FAIL}
    strict_fail = {item.name for item in strict if item.status == FAIL}
    assert "JWT 密钥" not in normal_fail
    assert {"JWT 密钥", "任务执行", "成本预算"} <= strict_fail


def test_单项异常不会中断整份自检(monkeypatch):
    def boom(_settings):
        raise RuntimeError("检查炸了")

    monkeypatch.setattr(preflight, "CHECKS", [("测试", "会炸的检查", boom), *preflight.CHECKS[1:]])
    results = preflight.run_checks(make_settings(), strict=False, with_eval=False)

    crashed = next(item for item in results if item.name == "会炸的检查")
    assert crashed.status == FAIL
    assert "检查自身出错" in crashed.detail
    assert len(results) == len(preflight.CHECKS), "其它检查仍应执行"


def test_数据库表检查能发现缺表(monkeypatch):
    """缺表必须 FAIL：这是"部署少建了表"的直接信号。"""

    class FakeInspector:
        def get_table_names(self):
            return ["users"]

    monkeypatch.setattr("sqlalchemy.inspect", lambda _engine: FakeInspector())

    result = preflight.check_database_tables(make_settings())

    assert result.status == FAIL
    assert "缺少" in result.detail


def test_迁移覆盖检查与模型一致():
    """当前仓库状态下，迁移应覆盖全部模型表（这条同时是发布门禁）。"""
    result = preflight.check_migrations_cover_models(make_settings())

    assert result.status == PASS, result.detail
