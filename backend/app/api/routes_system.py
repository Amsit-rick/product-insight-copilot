"""系统与审计接口：健康检查、Agent 能力清单、工作流结构、审计日志。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents import agent_catalog
from app.adapters.registry import describe_fact_adapter
from app.api.deps import Principal, get_current_principal
from app.config import get_settings
from app.db.session import get_db
from app.graph.builder import ANALYSIS_BRANCHES, NODE_SEQUENCE
from app.models.workflow import AuditLog
from app.services.cache import get_cache
from app.services.caching import cache_stats
from app.services.canary_report import canary_status, compare
from app.services.cost import cost_summary
from app.services.delivery_readiness import build_delivery_readiness_snapshot
from app.services.model_gateway import get_embedding_client, get_model_gateway
from app.services.policy_engine import AGENT_KBS, AGENT_TOOLS
from app.services.storage import storage_readiness_check
from app.tools import get_tool_registry

router = APIRouter(tags=["系统与审计"])


def _database_check() -> dict:
    """执行最小只读查询；公开响应只给异常类型，避免泄露连接信息。"""
    from app.db.session import SessionLocal

    session = None
    try:
        session = SessionLocal()
        session.execute(text("SELECT 1"))
        return {"ok": True, "required": True}
    except Exception as exc:
        return {"ok": False, "required": True, "error": type(exc).__name__}
    finally:
        if session is not None:
            session.close()


def _cache_readiness_check(settings) -> dict:
    description = get_cache().describe()
    production = settings.app_env == "prod"
    healthy = bool(description.get("healthy"))
    redis_active = description.get("backend") == "redis"
    ok = healthy and (redis_active or not production)
    payload = {
        "ok": ok,
        "required": production,
        "backend": description.get("backend"),
        "configured": description.get("configured"),
    }
    if not ok:
        payload["error"] = "cache_degraded" if not healthy else "production_cache_not_shared"
    return payload


def _execution_readiness_check(settings) -> dict:
    execution = _execution_status(settings)
    production = settings.app_env == "prod"
    backend = execution.get("backend")
    queue = execution.get("queue") or {}
    queue_readable = backend != "queue" or "error" not in queue
    checkpointer_shared = bool((execution.get("checkpointer") or {}).get("shared"))
    checkpointer_error = None
    if production and checkpointer_shared:
        try:
            # describe_checkpointer 在尚未初始化时只能反映“配置看起来共享”；
            # readiness 必须真的建连，不能把不可达的 Postgres 冒充可恢复。
            from app.graph.checkpointer import build_checkpointer, describe_checkpointer

            build_checkpointer(settings)
            checkpointer_shared = bool(describe_checkpointer(settings).get("shared"))
        except Exception as exc:
            checkpointer_shared = False
            checkpointer_error = type(exc).__name__
    production_ready = backend == "queue" and queue_readable and checkpointer_shared
    payload = {
        "ok": production_ready if production else queue_readable,
        "required": production,
        "backend": backend,
        "queue_readable": queue_readable,
        "checkpointer_shared": checkpointer_shared,
    }
    if not payload["ok"]:
        payload["error"] = (
            "production_execution_not_shared" if production else "queue_unavailable"
        )
    if checkpointer_error:
        payload["checkpointer_error"] = checkpointer_error
    return payload


def _readiness_report(settings) -> tuple[dict, int]:
    checks = {
        "database": _database_check(),
        "cache": _cache_readiness_check(settings),
        "execution": _execution_readiness_check(settings),
        "storage": storage_readiness_check(settings),
    }
    ready = all(check["ok"] or not check["required"] for check in checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "app": settings.app_name,
        "env": settings.app_env,
        "release_sha": settings.app_release_sha or None,
        "checks": checks,
    }, (200 if ready else 503)


@router.get("/health/live", summary="进程存活探针")
def health_live() -> dict:
    settings = get_settings()
    return {"status": "alive", "app": settings.app_name, "env": settings.app_env}


@router.get("/health/ready", summary="关键依赖就绪探针")
def health_ready() -> JSONResponse:
    payload, status_code = _readiness_report(get_settings())
    return JSONResponse(status_code=status_code, content=payload)


def _vector_store_status(session=None) -> dict:
    """向量后端的配置值与**实际生效值**（失败不影响 /health 返回）。"""
    owns_session = session is None
    if owns_session:
        from app.db.session import SessionLocal

        session = SessionLocal()
    try:
        from app.rag.vectorstore import describe_vector_store, get_vector_store

        get_vector_store(session)  # 触发一次选择（幂等，结果会被缓存）
        return describe_vector_store()
    except Exception as exc:  # pragma: no cover - 向量库异常不该让 /health 挂掉
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        if owns_session and session is not None:
            session.close()


@router.get("/health", summary="健康检查")
def health() -> dict:
    settings = get_settings()
    gateway = get_model_gateway()
    embedder = get_embedding_client()
    storage = storage_readiness_check(settings)
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.app_env,
        "database": "sqlite" if settings.is_sqlite else "postgres",
        "llm": {
            "provider": settings.llm_provider,
            "enabled": gateway.enabled,
            "model": settings.llm_model if gateway.enabled else "stub",
            # 多模型路由概览：哪些 Agent 用了独立模型（不含密钥）
            "routing": gateway.routing_table(),
            "override_count": len(settings.agent_model_overrides or {}),
            # 运行期模型健康计数：JSON 解析失败、重试、确定性回退（不含 Prompt/输出）
            "runtime_metrics": gateway.stats(),
            # 模型灰度：按任务分桶，百分比与盐值都公开（不含密钥）
            "canary": gateway.canary_table(),
            # 灰度效果对比的样本门槛（供前端提示"什么时候可以下结论"）
            "canary_experiment": canary_status(settings),
        },
        "embedding": {
            "provider": settings.embedding_provider,
            "enabled": embedder.enabled,
            "model": embedder.model_name,
            "dim": embedder.dim,
            "failure_mode": settings.embedding_failure_mode,
            "runtime_metrics": embedder.cache_info(),
        },
        "reranker": {
            # 只展示接入状态与失败策略，不回显端点或密钥。
            "configured": bool(settings.reranker_endpoint),
            "authenticated": bool(settings.reranker_api_key),
            "failure_mode": settings.reranker_failure_mode,
        },
        "storage": storage["backend"],
        "storage_status": storage,
        "fact_adapter": describe_fact_adapter(settings),
        # 向量后端：**配置值**与**实际生效值**都报。只报配置值的话，
        # vector_backend=auto 时看不出到底在用 pgvector 还是 Python 余弦（排查检索性能第一条要看的就是它）。
        "vector_backend": settings.vector_backend,
        "vector_store": _vector_store_status(),
        "workflow_version": settings.workflow_version,
        "prompt_version": settings.prompt_version,
        "reflection": {
            "enabled": settings.reflection_enabled,
            "ticket_enabled": settings.reflection_ticket_enabled,
            "report_enabled": settings.reflection_report_enabled,
            "rollout_percent": settings.reflection_rollout_percent,
            "rollout_tenants": settings.reflection_rollout_tenants,
            "rollout_risk_levels": settings.reflection_rollout_risk_levels,
            "rollout_issue_types": settings.reflection_rollout_issue_types,
            "release_min_sample": settings.reflection_release_min_sample,
            "auto_guard": settings.reflection_rollout_auto_guard,
            "max_rounds": settings.max_reflection_rounds,
            "timeout_seconds": settings.reflection_timeout_seconds,
            "max_tokens": settings.reflection_max_tokens,
            "cost_limit": settings.reflection_cost_limit,
            "stores_chain_of_thought": False,
        },
        "alerting": _alerting_status(settings),
        # 阶段六：稳定性相关的运行时状态（缓存命中率、并发与预算约束）
        "limits": {
            "model_max_concurrency": settings.model_max_concurrency,
            "task_submit_rate_limit_per_minute": settings.task_submit_rate_limit_per_minute,
            "login_attempt_rate_limit_per_minute": settings.login_attempt_rate_limit_per_minute,
            "task_token_limit": settings.task_token_limit,
            "task_cost_limit": settings.task_cost_limit,
            "tenant_daily_cost_limit": settings.tenant_daily_cost_limit,
            "budget_exceeded_action": settings.budget_exceeded_action,
            "retrieval_cache_ttl_seconds": settings.retrieval_cache_ttl_seconds,
            "metric_cache_ttl_seconds": settings.metric_cache_ttl_seconds,
            "fallback_models": settings.llm_fallback_models,
        },
        # cache 里既有"后端是什么/是否降级"，也有命中统计（含按命名空间细分与最近窗口）：
        # 一个总命中率说明不了问题——冷启动跑新问题时检索必然 0% 命中，那是预期而非故障。
        "cache": {**get_cache().describe(), **cache_stats(), "vector_cache": embedder.cache_info()},
        "cost": {
            "currency": settings.currency,
            "priced_models": sorted((settings.model_prices or {}).keys()),
        },
        # 多副本生产化：执行后端、队列积压、断点恢复是否跨进程共享
        "execution": _execution_status(settings),
    }


@router.get("/delivery-readiness", summary="正式交付证据快照（管理员只读）")
def delivery_readiness(principal: Principal = Depends(get_current_principal)) -> dict:
    principal.require("admin:manage")
    settings = get_settings()
    return build_delivery_readiness_snapshot(
        settings.delivery_evidence_dir,
        expected_release_sha=settings.app_release_sha,
        max_age_hours=settings.delivery_evidence_max_age_hours,
    )


def _execution_status(settings) -> dict:
    """任务执行与断点恢复的运行时状态。

    这三项决定了"能不能多副本跑"：执行后端是否可分发、队列有没有积压、
    以及 checkpointer 是不是进程内内存（内存意味着中断恢复不能跨副本）。
    """
    from app.graph.checkpointer import describe_checkpointer
    from app.services.task_dispatch import resolve_backend

    payload: dict = {
        "backend": resolve_backend(settings),
        "configured_backend": settings.task_execution_backend,
        "worker_embedded": settings.worker_embedded,
        "worker_concurrency": settings.worker_concurrency,
        "checkpointer": describe_checkpointer(settings),
    }
    try:
        from app.services.task_queue import get_task_queue

        payload["queue"] = get_task_queue(settings).stats()
    except Exception as exc:  # 队列表还没建/查询失败不应让 /health 挂掉
        payload["queue"] = {"error": f"{type(exc).__name__}: {exc}"}
    return payload


def _alerting_status(settings) -> dict:
    """只公开是否配置和队列计数，绝不回显 URL 或签名密钥。"""
    payload = {
        "webhook": {
            "enabled": settings.alert_webhook_enabled,
            "configured": bool(settings.alert_webhook_url.strip()),
            "signed": bool(settings.alert_webhook_secret),
            "max_attempts": settings.alert_webhook_max_attempts,
        }
    }
    try:
        from app.services.alert_delivery import SqlAlertDeliveryQueue

        payload["delivery_queue"] = SqlAlertDeliveryQueue(settings).stats()
    except Exception as exc:
        payload["delivery_queue"] = {"error": type(exc).__name__}
    return payload


@router.get("/costs/summary", summary="成本汇总（近 N 天，供看板与成本控制）")
def costs_summary(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    days: int = Query(default=7, ge=1, le=90),
) -> dict:
    principal.require("task:read")
    return cost_summary(session, principal.tenant_id, days=days)


@router.get("/experiments/canary", summary="灰度效果对比（灰度侧 vs 对照侧）")
def canary_experiment(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    days: int = Query(default=14, ge=1, le=90),
    agent_code: str | None = Query(default=None, description="只看某个 Agent，如 report_agent"),
) -> dict:
    """按 model_source 分组对比灰度模型与对照模型。

    注意：**样本不足时这里只会报"样本不足"**，不会给"谁更好"的结论——
    几个任务的差异基本是噪声，用噪声做选型决策比不做更危险。
    """
    principal.require("task:read")
    return compare(session, principal.tenant_id, days=days, agent_code=agent_code)


@router.get("/agents", summary="Agent 能力清单（职责、权限边界与所用模型）")
def agents(principal: Principal = Depends(get_current_principal)) -> dict:
    gateway = get_model_gateway()
    catalog = agent_catalog()
    for item in catalog:
        item["knowledge_base_scope"] = sorted(AGENT_KBS.get(item["code"], set()))
        item["tool_scope"] = sorted(AGENT_TOOLS.get(item["code"], set()))
        # 每个 Agent 实际生效的模型目标（按 Agent 覆盖 > 全局默认；不含密钥）
        item["model_profile"] = gateway.profile_for(item["code"]).describe()
    return {
        "agents": catalog,
        "analysis_branches": list(ANALYSIS_BRANCHES),
        "tools": get_tool_registry().describe(),
        "code_node_models": [
            gateway.profile_for("intent_classifier").describe(),
            gateway.profile_for("answerability_check").describe(),
        ],
    }


@router.get("/workflow", summary="工作流结构与受控参数")
def workflow(principal: Principal = Depends(get_current_principal)) -> dict:
    settings = get_settings()
    return {
        "nodes": list(NODE_SEQUENCE),
        "analysis_branches": list(ANALYSIS_BRANCHES),
        "limits": {
            "agent_timeout_seconds": settings.agent_timeout_seconds,
            "agent_max_retries": settings.agent_max_retries,
            "agent_max_tool_calls": settings.agent_max_tool_calls,
            "agent_max_tokens": settings.agent_max_tokens,
            "workflow_max_retrieve_rounds": settings.workflow_max_retrieve_rounds,
            "min_evidence_count": settings.min_evidence_count,
            "confidence_low_threshold": settings.confidence_low_threshold,
            "risk_score_high_threshold": settings.risk_score_high_threshold,
        },
        "rules": [
            "工作流决定怎么走，规则决定什么能做，Agent 只负责理解与归纳",
            "Agent 只能调用分配给自己的知识库与工具，越权调用会被策略引擎拒绝并记审计",
            "证据不足、存在冲突或高风险结论必须人工审核后才能成为决策依据",
        ],
    }


@router.get("/audit-logs", summary="审计日志（管理员）")
def audit_logs(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    action: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    principal.require("admin:manage")
    query = session.query(AuditLog).filter(AuditLog.tenant_id == principal.tenant_id)
    if action:
        query = query.filter(AuditLog.action == action)
    rows = query.order_by(AuditLog.created_at.desc()).limit(limit).all()
    return {
        "items": [
            {
                "id": row.id,
                "action": row.action,
                "actor_id": row.actor_id,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "outcome": row.outcome,
                "detail": row.detail,
                "created_at": row.created_at,
            }
            for row in rows
        ],
        "total": len(rows),
    }
