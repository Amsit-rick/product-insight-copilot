"""阶段六的集成测试：限流、预算熔断、成本与缓存可观测性。

这些用例都跑在真实的 FastAPI 应用上（`client` 夹具），只是把模型强制设为 stub，
因此不会产生任何真实模型费用。
"""

from __future__ import annotations

import os

import pytest

from app.models.workflow import AgentRun, AnalysisTask
from app.services.task_service import TaskService

QUESTION = "Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？"
FILTERS = {
    "platform": "Android",
    "app_version": "3.2.0",
    "date_from": "2026-08-01",
    "date_to": "2026-08-31",
    "knowledge_bases": ["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
    "metrics": ["next_day_retention", "crash_rate"],
}


def _create(client, auth, payload: dict | None = None):
    return client.post(
        "/api/v1/tasks",
        headers=auth["pm"],
        json={"query": QUESTION, **FILTERS, **(payload or {})},
    )


# --------------------------------------------------------------------------- 限流
def test_超过每分钟建任务上限返回429(client, auth, monkeypatch):
    from app.config import get_settings
    from app.services import limits as limits_module

    monkeypatch.setenv("TASK_SUBMIT_RATE_LIMIT_PER_MINUTE", "2")
    settings = get_settings()
    monkeypatch.setattr(settings, "task_submit_rate_limit_per_minute", 2)
    # 固定窗口限流允许分钟边界突发；测试必须固定在同一窗口，否则恰好跨分钟时
    # 第三次请求会合法进入新窗口，形成与执行时刻相关的偶发失败。
    monkeypatch.setattr(limits_module.time, "time", lambda: 1_793_978_000.0)
    # 清掉窗口内的已有计数，避免其他用例的请求影响判断
    from app.services.limits import get_rate_limiter

    me = client.get("/api/v1/auth/me", headers=auth["pm"]).json()
    limiter = get_rate_limiter()
    key = f"task_create:{me['tenant_id']}:{me['id']}"
    limiter.reset(key)

    first = _create(client, auth)
    second = _create(client, auth)
    third = _create(client, auth)

    assert first.status_code == 202
    assert second.status_code == 202
    assert third.status_code == 429
    assert third.json()["detail"]["code"] == "rate_limited"
    assert int(third.headers["Retry-After"]) > 0


def test_限流关闭时不拦截(client, auth, monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "task_submit_rate_limit_per_minute", 0)

    for _ in range(3):
        assert _create(client, auth).status_code == 202


# --------------------------------------------------------------------------- 日预算
def test_租户日预算超限时拒绝建任务(client, auth, monkeypatch):
    from app.config import get_settings
    from app.db.session import SessionLocal
    from app.models.identity import Tenant

    session = SessionLocal()
    try:
        tenant = session.query(Tenant).filter(Tenant.code == "demo").first()
        task, _ = TaskService(session).create(user=_pm(session), query="预算占位任务", filters={})
        session.flush()
        # 造一条"很贵"的模型调用记录，把日成本顶上去
        session.add(
            AgentRun(
                task_id=task.id,
                agent_code="metric_agent",
                status="succeeded",
                model="deepseek-reasoner",
                prompt_tokens=10_000_000,
                completion_tokens=10_000_000,
                total_tokens=20_000_000,
                output={"model_used": True, "cache_hit_tokens": 0},
            )
        )
        session.commit()
    finally:
        session.close()

    settings = get_settings()
    monkeypatch.setattr(settings, "tenant_daily_cost_limit", 1.0)

    response = _create(client, auth)

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "budget_exceeded"


def _pm(session):
    from app.models.identity import User

    return session.query(User).filter(User.username == "pm").first()


# --------------------------------------------------------------------------- 任务级预算
def _prepare_task_with_usage(tokens: int) -> str:
    """建一个任务并预置一条 token 用量记录（模拟"已经花掉一部分预算"）。"""
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        task, _ = TaskService(session).create(user=_pm(session), query=QUESTION, filters=FILTERS)
        session.flush()
        if tokens:
            session.add(
                AgentRun(
                    task_id=task.id,
                    agent_code="product_agent",
                    status="succeeded",
                    model="deepseek-chat",
                    prompt_tokens=tokens,
                    completion_tokens=0,
                    total_tokens=tokens,
                    output={"model_used": True, "cache_hit_tokens": 0},
                )
            )
        session.commit()
        return task.id
    finally:
        session.close()


def test_预算超限时降级出报告而不失败(client, auth, monkeypatch):
    """degrade 策略：不再调用模型，但任务照常完成，并在报告里写明预算限制。"""
    from app.config import get_settings
    from app.services.workflow_runner import get_runner

    settings = get_settings()
    monkeypatch.setattr(settings, "task_token_limit", 100)
    monkeypatch.setattr(settings, "budget_exceeded_action", "degrade")

    task_id = _prepare_task_with_usage(tokens=5_000)
    get_runner().run(task_id)

    detail = client.get(f"/api/v1/tasks/{task_id}", headers=auth["pm"]).json()
    assert detail["status"] in {"waiting_human", "completed"}

    trace = client.get(f"/api/v1/tasks/{task_id}/trace", headers=auth["pm"]).json()
    for run in trace["agent_runs"]:
        if run["agent_code"] == "product_agent":
            continue  # 这条是我们预置的占位记录
        assert run["model_used"] is False, "预算超限后不应再调用模型"
    assert trace["budget"]["exceeded"] is True
    assert "token" in trace["budget"]["reasons"][0]


def test_预算超限时abort策略让任务失败可重试(client, auth, monkeypatch):
    from app.config import get_settings
    from app.services.workflow_runner import get_runner

    settings = get_settings()
    monkeypatch.setattr(settings, "task_token_limit", 100)
    monkeypatch.setattr(settings, "budget_exceeded_action", "abort")

    task_id = _prepare_task_with_usage(tokens=5_000)
    get_runner().run(task_id)

    detail = client.get(f"/api/v1/tasks/{task_id}", headers=auth["pm"]).json()
    assert detail["status"] == "failed"
    assert detail["error"]["code"] == "budget_exceeded"

    events = client.get(f"/api/v1/tasks/{task_id}/events", headers=auth["pm"]).json()
    assert any("budget_exceeded" in item["message"] for item in events["items"])

    # 失败后可重试（例如提高预算或换便宜模型）
    retried = client.post(f"/api/v1/tasks/{task_id}/retry", headers=auth["pm"], json={})
    assert retried.status_code == 200


# --------------------------------------------------------------------------- 成本与缓存可观测
def test_轨迹接口返回成本明细(client, auth):
    task_id = _prepare_task_with_usage(tokens=1_000_000)
    trace = client.get(f"/api/v1/tasks/{task_id}/trace", headers=auth["pm"]).json()

    cost = trace["cost"]
    assert cost["currency"] == "CNY"
    assert cost["total_tokens"] == 1_000_000
    assert cost["amount"] > 0
    assert "deepseek-chat" in cost["per_model"]
    assert trace["budget"]["limits"]["task_token_limit"] >= 0


def test_健康检查暴露缓存并发与预算配置(client):
    health = client.get("/health").json()

    assert health["cache"]["backend"] in {"memory", "redis"}
    assert "hit_rate" in health["cache"]
    limits = health["limits"]
    assert limits["model_max_concurrency"] >= 1
    assert limits["budget_exceeded_action"] in {"degrade", "abort"}
    assert "retrieval_cache_ttl_seconds" in limits


def test_成本汇总接口(client, auth):
    _prepare_task_with_usage(tokens=500_000)
    summary = client.get("/api/v1/costs/summary?days=7", headers=auth["pm"]).json()

    assert summary["window_days"] == 7
    assert summary["cost"]["total_tokens"] >= 500_000
    assert "today" in summary


def test_检索缓存命中率会被统计(client, auth):
    from app.services.caching import cache_stats

    before = cache_stats()["hits"]
    # 同一个问题跑两次诊断：第二次的检索应大量命中缓存
    for _ in range(2):
        response = _create(client, auth)
        assert response.status_code == 202

    assert cache_stats()["hits"] > before, "重复诊断应命中检索缓存"


@pytest.mark.skipif(
    os.environ.get("PIC_SKIP_SLOW") == "1", reason="显式跳过慢用例"
)
def test_缓存不影响检索结果正确性(client, auth):
    """缓存返沪的结果必须与首次一致（按 chunk_id 对齐）。"""
    from app.db.session import SessionLocal
    from app.models.identity import Tenant
    from app.rag.retrieval import RetrievalService

    session = SessionLocal()
    try:
        tenant = session.query(Tenant).filter(Tenant.code == "demo").first()
        service = RetrievalService()
        first = service.search(
            session,
            query="3.2.0 崩溃率上升 反馈",
            tenant_id=tenant.id,
            kb_codes=["feedback_kb", "engineering_kb"],
            max_security_rank=2,
            top_k=5,
        )
        second = service.search(
            session,
            query="3.2.0 崩溃率上升 反馈",
            tenant_id=tenant.id,
            kb_codes=["feedback_kb", "engineering_kb"],
            max_security_rank=2,
            top_k=5,
        )
        assert [hit.chunk_id for hit in first] == [hit.chunk_id for hit in second]
        assert [round(hit.relevance_score, 6) for hit in first] == [
            round(hit.relevance_score, 6) for hit in second
        ]
    finally:
        session.close()
