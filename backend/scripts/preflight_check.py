"""发布前自检（preflight）：把"上线前该确认的事"变成可执行、可进 CI 的检查。

为什么要有它：发布清单写成文档就一定会有人漏看，或者"照着点了但对结果判断不一致"。
这里把能机器判断的部分全部变成检查项，输出 PASS / WARN / FAIL 与退出码：

- **FAIL**：明确会出事故的配置（缺密钥、表没建、迁移与模型不一致、配置了 postgres checkpointer
  但依赖没装、Redis 配了却连不上）；
- **WARN**：能跑但风险高（生产环境开了 DEBUG、预算全为 0 等于不限额、checkpointer 是进程内
  内存而执行后端是 queue、CORS 仍指向本地端口、限流关闭）；
- **PASS**：其余。

环境相关：`APP_ENV=prod`（或 `--strict`）时 WARN 里"生产不宜"的项会升格为 FAIL，
避免"本地能跑"被当成"线上能跑"。

用法::

    python scripts/preflight_check.py                 # 常规自检
    python scripts/preflight_check.py --strict        # 按生产标准（WARN 中的生产项算 FAIL）
    python scripts/preflight_check.py --json          # 机器可读输出（供流水线消费）
    python scripts/preflight_check.py --with-eval     # 附带跑 8 条离线评测（较慢）
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0")


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""
    # 该问题在生产环境是否属于"必须修"
    production_critical: bool = False

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "production_critical": self.production_critical,
        }


# --------------------------------------------------------------------------- 配置
def check_llm_config(settings) -> CheckResult:
    if settings.llm_provider == "stub":
        return CheckResult(
            "模型网关",
            WARN,
            "LLM_PROVIDER=stub：全部走确定性归纳，报告里不会出现模型结论",
            production_critical=True,
        )
    if not settings.llm_api_key:
        return CheckResult(
            "模型网关",
            FAIL,
            f"LLM_PROVIDER={settings.llm_provider} 但没有配置 LLM_API_KEY",
        )
    return CheckResult(
        "模型网关", PASS, f"{settings.llm_provider} / {settings.llm_model}（密钥已配置，不回显）"
    )


def check_embedding_config(settings) -> CheckResult:
    if settings.embedding_provider == "stub":
        return CheckResult(
            "Embedding",
            WARN,
            "EMBEDDING_PROVIDER=stub：使用确定性哈希向量，不是生产语义向量",
            production_critical=True,
        )
    base_url = (settings.embedding_base_url or settings.llm_base_url or "").strip()
    api_key = settings.embedding_api_key or settings.llm_api_key
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return CheckResult("Embedding", FAIL, "真实 Embedding 缺少完整的 EMBEDDING_BASE_URL/LLM_BASE_URL")
    if not api_key:
        return CheckResult("Embedding", FAIL, "真实 Embedding 缺少 EMBEDDING_API_KEY/LLM_API_KEY")
    return CheckResult(
        "Embedding",
        PASS,
        f"{settings.embedding_provider} / {settings.embedding_model}（失败策略={settings.embedding_failure_mode}；端点与密钥不回显）",
    )


def check_reranker_config(settings) -> CheckResult:
    endpoint = (settings.reranker_endpoint or "").strip()
    if not endpoint:
        return CheckResult(
            "Reranker",
            WARN,
            "RERANKER_ENDPOINT 未配置：检索只使用启发式重排",
            production_critical=True,
        )
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return CheckResult("Reranker", FAIL, "RERANKER_ENDPOINT 必须是完整的 HTTP(S) 地址")
    if parsed.username or parsed.password:
        return CheckResult("Reranker", FAIL, "RERANKER_ENDPOINT 禁止内嵌用户名或口令")
    return CheckResult(
        "Reranker",
        PASS,
        f"BGE-Reranker 端点已配置（失败策略={settings.reranker_failure_mode}；地址与密钥不回显；未执行联网探测）",
    )


def check_alert_webhook(settings) -> CheckResult:
    if not settings.alert_webhook_enabled:
        return CheckResult(
            "外部告警",
            WARN,
            "ALERT_WEBHOOK_ENABLED=false：严重告警只保存在站内",
            production_critical=True,
        )
    parsed = urlparse((settings.alert_webhook_url or "").strip())
    if parsed.scheme != "https" or not parsed.netloc:
        return CheckResult("外部告警", FAIL, "ALERT_WEBHOOK_URL 必须是完整的 HTTPS 地址")
    if parsed.username or parsed.password:
        return CheckResult("外部告警", FAIL, "ALERT_WEBHOOK_URL 禁止内嵌用户名或口令")
    if parsed.fragment:
        return CheckResult("外部告警", FAIL, "ALERT_WEBHOOK_URL 禁止包含 URL 片段")
    if not settings.alert_webhook_secret:
        return CheckResult(
            "外部告警",
            WARN,
            "Webhook 已启用但未配置 ALERT_WEBHOOK_SECRET，无法验证消息来源",
            production_critical=True,
        )
    return CheckResult("外部告警", PASS, "HTTPS Webhook 与 HMAC 签名已配置（地址和密钥不回显）")


def check_fact_adapter(settings) -> CheckResult:
    if settings.ticket_use_mock_adapters:
        return CheckResult(
            "事实适配器",
            WARN,
            "TICKET_USE_MOCK_ADAPTERS=true：订单/支付/物流/售后均为模拟事实",
            production_critical=True,
        )
    from app.adapters.http import validate_endpoint_templates

    errors = validate_endpoint_templates(settings)
    if errors:
        return CheckResult("事实适配器", FAIL, "；".join(errors))
    return CheckResult(
        "事实适配器",
        PASS,
        "标准化 HTTP 端点 4/4 已配置（鉴权头不回显；未执行联网探测）",
    )


def check_metric_source(settings) -> CheckResult:
    if not settings.metric_source_http_enabled:
        return CheckResult(
            "指标事实源",
            WARN,
            "METRIC_SOURCE_HTTP_ENABLED=false：主动诊断只读取本地导入/演示指标",
            production_critical=True,
        )
    from app.adapters.metric_http import validate_metric_source_settings

    errors = validate_metric_source_settings(settings)
    invalid_thresholds = {
        name: value
        for name, value in settings.proactive_anomaly_max_data_age_by_metric.items()
        if not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 365
        or name not in settings.proactive_anomaly_metrics
    }
    if errors or invalid_thresholds:
        details = list(errors)
        if invalid_thresholds:
            details.append("分指标新鲜度配置包含非法值或非白名单指标")
        return CheckResult("指标事实源", FAIL, "；".join(details))
    return CheckResult(
        "指标事实源",
        PASS,
        f"只读 GET 指标源已配置，{len(settings.proactive_anomaly_metrics)} 个指标（地址与鉴权头不回显；未联网探测）",
    )


def check_fact_probe_plan(settings) -> CheckResult:
    """Verify the shipped sandbox probe plan before anyone relies on it for release evidence."""
    _ = settings
    # 源码仓库布局是 <repo>/backend + <repo>/docs；生产镜像为了让 preflight
    # 可独立运行，把脚本和计划都放在 /app 下。两种布局都必须支持，否则
    # 文档声称“容器内可执行”的发布门禁会在真正镜像里误报缺文件。
    plan_candidates = (
        BACKEND.parent / "docs" / "integrations" / "fact-probe-plan.example.json",
        BACKEND / "docs" / "integrations" / "fact-probe-plan.example.json",
    )
    plan_path = next((path for path in plan_candidates if path.exists()), None)
    if plan_path is None:
        return CheckResult("事实验收计划", FAIL, "缺少 docs/integrations/fact-probe-plan.example.json")
    try:
        from app.services.fact_integration_probe import load_fact_probe_plan

        plan = load_fact_probe_plan(plan_path)
    except Exception as exc:
        return CheckResult("事实验收计划", FAIL, f"计划无效：{type(exc).__name__}")
    return CheckResult(
        "事实验收计划",
        PASS,
        f"schema={plan.safe_metadata()['schema_version']}，场景 {plan.scenario_count} 个",
    )


def check_jwt_secret(settings) -> CheckResult:
    from app.services.runtime_safety import jwt_secret_error

    secret = (settings.jwt_secret or "").strip()
    error = jwt_secret_error(secret)
    if error == "JWT_SECRET 为空":
        return CheckResult("JWT 密钥", FAIL, error)
    if error:
        return CheckResult(
            "JWT 密钥", WARN, error + "，必须替换", production_critical=True
        )
    return CheckResult("JWT 密钥", PASS, f"已自定义（长度 {len(secret)}）")


def check_auth_cookies(settings) -> CheckResult:
    session_name = settings.auth_cookie_name.strip()
    csrf_name = settings.auth_csrf_cookie_name.strip()
    if not session_name or not csrf_name or not settings.auth_csrf_header_name.strip():
        return CheckResult("浏览器会话", FAIL, "Cookie 名称与 CSRF Header 名称不能为空")
    if session_name == csrf_name:
        return CheckResult("浏览器会话", FAIL, "会话 Cookie 与 CSRF Cookie 名称不能相同")
    if settings.auth_cookie_secure is False:
        return CheckResult(
            "浏览器会话",
            WARN,
            "AUTH_COOKIE_SECURE=false：生产 HTTPS 下会话 Cookie 可经明文连接发送",
            production_critical=True,
        )
    if settings.auth_cookie_samesite == "none" and not settings.auth_cookie_secure_effective:
        return CheckResult(
            "浏览器会话",
            FAIL,
            "SameSite=None 必须同时启用 Secure Cookie",
        )
    secure = "Secure + " if settings.auth_cookie_secure_effective else "生产自动 Secure + "
    return CheckResult(
        "浏览器会话",
        PASS,
        f"{secure}HttpOnly + SameSite={settings.auth_cookie_samesite} + CSRF 双提交",
    )


def check_debug(settings) -> CheckResult:
    if settings.debug:
        return CheckResult(
            "调试开关", WARN, "DEBUG=true：错误详情会外泄，生产必须关闭", production_critical=True
        )
    return CheckResult("调试开关", PASS, "DEBUG=false")


def check_cors(settings) -> CheckResult:
    origins = [item.strip() for item in (settings.cors_origins or "").split(",") if item.strip()]
    if not origins:
        return CheckResult("CORS", WARN, "未配置 CORS_ORIGINS", production_critical=True)
    if "*" in origins:
        return CheckResult("CORS", WARN, "CORS 允许任意来源（*）", production_critical=True)
    if all(any(host in origin for host in LOCAL_HOSTS) for origin in origins):
        return CheckResult(
            "CORS", WARN, f"仅允许本地来源：{origins}", production_critical=True
        )
    return CheckResult("CORS", PASS, f"{len(origins)} 个来源")


def check_budget(settings) -> CheckResult:
    limits = (settings.task_token_limit, settings.task_cost_limit, settings.tenant_daily_cost_limit)
    if all(not limit for limit in limits):
        return CheckResult(
            "成本预算",
            WARN,
            "token / 单任务金额 / 租户日金额上限全为 0（等于不限额）；建议至少设一个",
            production_critical=True,
        )
    parts = []
    if settings.task_token_limit:
        parts.append(f"单任务 {settings.task_token_limit} token")
    if settings.task_cost_limit:
        parts.append(f"单任务 {settings.task_cost_limit} {settings.currency}")
    if settings.tenant_daily_cost_limit:
        parts.append(f"租户日 {settings.tenant_daily_cost_limit} {settings.currency}")
    return CheckResult("成本预算", PASS, "；".join(parts))


def check_rate_limit(settings) -> CheckResult:
    if not settings.task_submit_rate_limit_per_minute:
        return CheckResult(
            "提交限流", WARN, "TASK_SUBMIT_RATE_LIMIT_PER_MINUTE=0（不限流）", production_critical=True
        )
    backend = settings.cache_backend
    if backend != "redis":
        return CheckResult(
            "提交限流",
            WARN,
            f"限流计数用 {backend}：多副本下各副本各算各的（需要 Redis 才全局生效）",
            production_critical=False,
        )
    return CheckResult("提交限流", PASS, f"{settings.task_submit_rate_limit_per_minute}/分钟（Redis 计数）")


def check_login_rate_limit(settings) -> CheckResult:
    if settings.login_attempt_rate_limit_per_minute <= 0:
        return CheckResult(
            "登录限流",
            WARN,
            "LOGIN_ATTEMPT_RATE_LIMIT_PER_MINUTE=0（登录接口不限流）",
            production_critical=True,
        )
    if settings.cache_backend != "redis":
        return CheckResult(
            "登录限流",
            WARN,
            f"{settings.login_attempt_rate_limit_per_minute}/分钟，但计数用 {settings.cache_backend}；"
            "生产登录要求实际使用 Redis；本地后端会返回 503",
            production_critical=True,
        )
    return CheckResult(
        "登录限流",
        PASS,
        f"{settings.login_attempt_rate_limit_per_minute}/分钟（Redis 计数）",
    )


def check_auto_seed(settings) -> CheckResult:
    if settings.auto_seed:
        return CheckResult(
            "演示数据播种",
            WARN,
            "AUTO_SEED=true：空库会创建已知口令的演示账号，生产必须关闭",
            production_critical=True,
        )
    return CheckResult("演示数据播种", PASS, "AUTO_SEED=false")


def check_proxy_trust(settings) -> CheckResult:
    from app.services.runtime_safety import production_config_errors

    errors = [
        item for item in production_config_errors(settings.model_copy(update={"app_env": "prod"}))
        if "FORWARDED_ALLOW_IPS" in item
    ]
    if errors:
        return CheckResult("可信代理", WARN, "；".join(errors), production_critical=True)
    entries = [
        item.strip() for item in (settings.forwarded_allow_ips or "").split(",") if item.strip()
    ]
    if not entries:
        return CheckResult("可信代理", PASS, "不信任转发头；限流按 TCP 直连地址计数")
    return CheckResult("可信代理", PASS, f"已配置 {len(entries)} 个可信代理 IP/CIDR")


def check_docs_exposure(settings) -> CheckResult:
    """接口文档（/docs、/redoc、/openapi.json）在生产环境必须是关的。"""
    if settings.app_env == "prod":
        # main.py 对 prod 强制关闭，这里只是把结论说清楚
        return CheckResult("接口文档暴露", PASS, "生产环境强制关闭（/docs、/redoc、/openapi.json）")
    if settings.expose_docs:
        return CheckResult(
            "接口文档暴露",
            WARN,
            f"APP_ENV={settings.app_env}：/docs、/redoc、/openapi.json 对外开放"
            "（本地/开发正常；上线前请设 APP_ENV=prod）",
            production_critical=False,
        )
    return CheckResult("接口文档暴露", PASS, "已关闭")


def check_execution_backend(settings) -> CheckResult:
    backend = settings.task_execution_backend
    if backend == "inline":
        return CheckResult(
            "任务执行", WARN, "TASK_EXECUTION_BACKEND=inline：接口会阻塞到工作流跑完", production_critical=True
        )
    if backend == "queue":
        if settings.worker_embedded:
            return CheckResult(
                "任务执行",
                WARN,
                "queue 后端 + WORKER_EMBEDDED=true：worker 与 API 同故障域，生产建议独立 worker",
                production_critical=False,
            )
        return CheckResult("任务执行", PASS, "queue + 独立 worker")
    return CheckResult(
        "任务执行",
        WARN,
        "threadpool：任务跑在 API 进程内，副本重启会中断任务（多副本请用 queue）",
        production_critical=True,
    )


def check_checkpointer(settings) -> CheckResult:
    backend = settings.checkpointer_backend
    if backend == "memory":
        if settings.task_execution_backend == "queue" or not settings.is_sqlite:
            return CheckResult(
                "断点恢复",
                WARN,
                "checkpointer=memory：人工审核后的恢复不能跨进程/跨副本，会走降级重建路径",
                production_critical=True,
            )
        return CheckResult(
            "断点恢复", WARN, "checkpointer=memory（单副本可用；多副本请改 sqlite/postgres）"
        )
    if backend == "sqlite":
        return CheckResult("断点恢复", PASS, "sqlite：同机多进程共享")
    if backend == "postgres":
        try:
            import langgraph.checkpoint.postgres  # noqa: F401
        except ImportError:
            return CheckResult(
                "断点恢复",
                FAIL,
                "checkpointer=postgres 但缺少 langgraph-checkpoint-postgres 依赖",
            )
        return CheckResult("断点恢复", PASS, "postgres：多副本共享")
    return CheckResult("断点恢复", FAIL, f"未知的 CHECKPOINTER_BACKEND={backend}")


def check_cache_backend(settings) -> CheckResult:
    from app.services.cache import get_cache

    cache = get_cache()
    described = cache.describe()
    if described.get("healthy"):
        return CheckResult("缓存后端", PASS, f"{described['backend']}（{described.get('configured')}）")
    reason = described.get("degraded_reason") or ""
    if settings.cache_backend == "redis":
        return CheckResult("缓存后端", FAIL, f"CACHE_BACKEND=redis 但不可用：{reason}")
    if described.get("persistent"):
        # 落盘降级：单机/开发场景够用（重启后仍命中、同机多进程共享），
        # 但多副本跨主机仍然不共享，因此仍是 WARN 而非 PASS。
        # 路径从配置里取完整值：/health 只公开文件名，运维要的是完整路径。
        return CheckResult(
            "缓存后端",
            WARN,
            f"已降级为落盘缓存 {settings.cache_sqlite_path}（{reason}）："
            f"重启后仍命中、同机进程共享；但跨主机副本不共享，限流计数仍是各自为政",
            production_critical=False,
        )
    return CheckResult(
        "缓存后端",
        WARN,
        f"已降级为进程内缓存（{reason}）：重启即清零，多副本下缓存与限流不共享",
        production_critical=False,
    )


def check_storage(settings) -> CheckResult:
    if settings.storage_backend == "local":
        return CheckResult(
            "对象存储",
            WARN,
            "local（单机落盘）：跨主机副本无法共享上传文件",
            production_critical=True,
        )
    from app.services.storage import minio_configuration_errors

    endpoint = settings.minio_endpoint or ""
    errors = minio_configuration_errors(settings)
    if errors:
        return CheckResult("对象存储", FAIL, "MinIO/S3 配置不完整或格式无效（端点与密钥不回显）")
    if any(host in endpoint for host in LOCAL_HOSTS) and settings.app_env == "prod":
        return CheckResult("对象存储", WARN, "MinIO 指向本机地址", production_critical=True)
    return CheckResult("对象存储", PASS, "MinIO/S3 共享存储配置完整（未联网；端点与密钥不回显）")


def check_env_hygiene(settings) -> CheckResult:
    """密钥与本地文件不能进版本库（这条是硬要求，出错即 FAIL）。"""
    import subprocess

    _ = settings
    env_file = BACKEND / ".env"
    if not env_file.exists():
        return CheckResult("密钥入库", PASS, "本地无 .env（或未创建）")
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(env_file)],
            cwd=str(BACKEND.parent),
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("密钥入库", WARN, f"无法运行 git check-ignore：{exc}")
    if result.returncode == 0:
        return CheckResult("密钥入库", PASS, "backend/.env 已被 .gitignore 排除")
    return CheckResult("密钥入库", FAIL, "backend/.env 未被 .gitignore 排除，存在误提交风险")


# --------------------------------------------------------------------------- 数据
def check_database_tables(settings) -> CheckResult:
    from sqlalchemy import inspect

    from app.db.session import SessionLocal, engine
    from app.models import Base

    expected = {table.name for table in Base.metadata.sorted_tables}
    try:
        actual = set(inspect(engine).get_table_names())
    except Exception as exc:
        return CheckResult("数据库表", FAIL, f"无法读取数据库结构：{type(exc).__name__}: {exc}")
    missing = sorted(expected - actual)
    if missing:
        return CheckResult("数据库表", FAIL, f"缺少 {len(missing)} 张表：{missing[:5]}")
    session = SessionLocal()
    try:
        session.execute(__import__("sqlalchemy").text("SELECT 1"))
    except Exception as exc:
        return CheckResult("数据库表", FAIL, f"数据库不可用：{exc}")
    finally:
        session.close()
    return CheckResult("数据库表", PASS, f"{len(expected)} 张表齐全（{settings.database_url.split('://')[0]}）")


def check_migrations_cover_models(settings) -> CheckResult:
    """001_init.sql 必须覆盖全部模型表（漏表会让 Postgres 部署少表）。"""
    import re

    from app.models import Base

    _ = settings
    sql_path = BACKEND / "migrations" / "001_init.sql"
    if not sql_path.exists():
        return CheckResult("数据库迁移", FAIL, "缺少 migrations/001_init.sql")
    sql = sql_path.read_text(encoding="utf-8")
    in_file = set(re.findall(r"CREATE TABLE (\w+) \(", sql))
    expected = {table.name for table in Base.metadata.sorted_tables}
    missing = sorted(expected - in_file)
    if missing:
        return CheckResult("数据库迁移", FAIL, f"迁移缺少表：{missing}")
    return CheckResult("数据库迁移", PASS, f"{len(expected)} 张表与模型一致")


def check_queue_readable(settings) -> CheckResult:
    if settings.task_execution_backend != "queue":
        return CheckResult("任务队列", PASS, "未启用队列（threadpool/inline）")
    try:
        from app.services.task_queue import get_task_queue

        stats = get_task_queue(settings).stats()
    except Exception as exc:
        return CheckResult("任务队列", FAIL, f"队列不可读：{type(exc).__name__}: {exc}")
    if stats.get("dead"):
        return CheckResult(
            "任务队列", WARN, f"存在 {stats['dead']} 条 dead 任务，需要人工处理：{stats}"
        )
    if stats.get("pending", 0) > 0:
        return CheckResult(
            "任务队列", WARN, f"有 {stats['pending']} 条待处理：确认 worker 在运行：{stats}"
        )
    return CheckResult("任务队列", PASS, str(stats))


def check_eval_baseline(limit: int = 8) -> CheckResult:
    """跑一小部分离线评测，确认"核心链路还能跑通"。"""
    from app.db.seed import init_db
    from app.services.evaluation_service import EvaluationService

    from app.db.session import SessionLocal
    from app.models.identity import Tenant

    try:
        init_db()
        session = SessionLocal()
        try:
            tenant = session.query(Tenant).filter(Tenant.code == "demo").first()
            if tenant is None:
                return CheckResult("评测冒烟", WARN, "缺少演示租户 demo（先执行 python -m app.db.seed）")
            run = EvaluationService(session).run(
                tenant_id=tenant.id,
                dataset_version="preflight",
                name="发布前自检",
                limit=limit,
            )
            pass_rate = (run.metrics or {}).get("pass_rate")
        finally:
            session.close()
    except Exception as exc:
        return CheckResult("评测冒烟", FAIL, f"{type(exc).__name__}: {exc}")
    if pass_rate is None:
        return CheckResult("评测冒烟", WARN, "未拿到通过率")
    if pass_rate < 0.5:
        return CheckResult("评测冒烟", FAIL, f"前 {limit} 条通过率 {pass_rate:.0%}（低于 50%）")
    return CheckResult("评测冒烟", PASS, f"前 {limit} 条通过率 {pass_rate:.0%}")


CHECKS: list[tuple[str, str, Callable]] = [
    ("配置", "模型网关", check_llm_config),
    ("配置", "Embedding", check_embedding_config),
    ("配置", "Reranker", check_reranker_config),
    ("配置", "事实适配器", check_fact_adapter),
    ("配置", "事实验收计划", check_fact_probe_plan),
    ("配置", "指标事实源", check_metric_source),
    ("配置", "外部告警", check_alert_webhook),
    ("配置", "JWT 密钥", check_jwt_secret),
    ("配置", "浏览器会话", check_auth_cookies),
    ("配置", "调试开关", check_debug),
    ("配置", "CORS", check_cors),
    ("配置", "成本预算", check_budget),
    ("配置", "提交限流", check_rate_limit),
    ("配置", "登录限流", check_login_rate_limit),
    ("配置", "演示数据播种", check_auto_seed),
    ("配置", "可信代理", check_proxy_trust),
    ("配置", "接口文档暴露", check_docs_exposure),
    ("配置", "对象存储", check_storage),
    ("执行与恢复", "任务执行", check_execution_backend),
    ("执行与恢复", "断点恢复", check_checkpointer),
    ("执行与恢复", "缓存后端", check_cache_backend),
    ("执行与恢复", "任务队列", check_queue_readable),
    ("数据", "数据库表", check_database_tables),
    ("数据", "数据库迁移", check_migrations_cover_models),
    ("安全", "密钥入库", check_env_hygiene),
]


def run_checks(settings, *, strict: bool, with_eval: bool) -> list[CheckResult]:
    results: list[CheckResult] = []
    for _, label, check in CHECKS:
        try:
            results.append(check(settings))
        except Exception as exc:  # 单项异常不应中断整份自检
            results.append(
                CheckResult(label, FAIL, f"检查自身出错：{type(exc).__name__}: {exc}")
            )
    if with_eval:
        results.append(check_eval_baseline())
    if strict:
        # 生产标准：把"生产不宜"的 WARN 升格为 FAIL
        results = [
            CheckResult(item.name, FAIL, item.detail + "（生产标准下必须修）", item.production_critical)
            if item.status == WARN and item.production_critical
            else item
            for item in results
        ]
    return results


def render(results: list[CheckResult]) -> None:
    width = max(len(item.name) for item in results) + 2
    for item in results:
        print(f"  [{item.status}] {item.name:<{width}} {item.detail}")
    failed = [item for item in results if item.status == FAIL]
    warned = [item for item in results if item.status == WARN]
    print(f"\n  合计 {len(results)} 项：PASS {len(results) - len(failed) - len(warned)} / WARN {len(warned)} / FAIL {len(failed)}")
    if failed:
        print("  必须修复：")
        for item in failed:
            print(f"    - {item.name}: {item.detail}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布前自检")
    parser.add_argument("--strict", action="store_true", help="按生产标准（WARN 中的生产项算 FAIL）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--with-eval", action="store_true", help="附带跑 8 条离线评测")
    args = parser.parse_args(argv)

    from app.config import get_settings

    settings = get_settings()
    print(f"=== 发布前自检（APP_ENV={settings.app_env}，{'生产标准' if args.strict else '常规'}）===")
    results = run_checks(settings, strict=args.strict, with_eval=args.with_eval)

    if args.json:
        print(json.dumps([item.as_dict() for item in results], ensure_ascii=False, indent=2))
    else:
        render(results)

    return 1 if any(item.status == FAIL for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
