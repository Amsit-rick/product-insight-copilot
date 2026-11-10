"""阶段六：成本核算与预算熔断的单元测试。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config import Settings
from app.models.base import utcnow
from app.models.identity import Tenant, User
from app.models.workflow import AgentRun, AnalysisTask
from app.services.cost import (
    UsageRecord,
    budget_state,
    compute_cost,
    cost_summary,
    price_of,
    record_amount,
    reflection_budget_state,
    reflection_task_cost,
    task_cost,
    tenant_cost_since,
    tenant_daily_budget_exceeded,
)


def make_settings(**overrides) -> Settings:
    base = {
        "llm_provider": "stub",
        "model_prices": {"model-a": {"input": 2.0, "output": 8.0, "cache_hit_input": 0.5}},
        "model_price_default": {"input": 10.0, "output": 20.0, "cache_hit_input": 5.0},
        "currency": "CNY",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# --------------------------------------------------------------------------- 计价
def test_按百万token单价计算金额():
    settings = make_settings()
    # 1,000,000 输入 x 2 元 + 500,000 输出 x 8 元 = 2 + 4 = 6 元
    breakdown = compute_cost([UsageRecord("model-a", 1_000_000, 500_000)], settings)

    assert breakdown.amount == pytest.approx(6.0)
    assert breakdown.total_tokens == 1_500_000
    assert breakdown.per_model["model-a"]["amount"] == pytest.approx(6.0)


def test_命中缓存的输入按更便宜的单价计算():
    settings = make_settings()
    # 输入 1,000,000 中 800,000 命中缓存：200,000 x 2 + 800,000 x 0.5 = 0.4 + 0.4 = 0.8 元
    amount, _ = record_amount(
        UsageRecord("model-a", 1_000_000, 0, cache_hit_tokens=800_000), settings
    )

    assert amount == pytest.approx(0.8)


def test_缓存命中数不会超过输入总数():
    settings = make_settings()
    amount, _ = record_amount(
        UsageRecord("model-a", 100, 0, cache_hit_tokens=10_000), settings
    )

    # 最多按 100 个输入 token 计费，且都按缓存价
    assert amount == pytest.approx(100 * 0.5 / 1_000_000, rel=1e-6)


def test_未登记模型用兜底单价并标记():
    settings = make_settings()
    breakdown = compute_cost([UsageRecord("模型-未知", 1_000_000, 0)], settings)

    assert breakdown.amount == pytest.approx(10.0)
    assert breakdown.unpriced_models == ["模型-未知"]


def test_支持前缀通配的单价覆盖():
    settings = make_settings(model_prices={"qwen-*": {"input": 1.0, "output": 3.0}})

    assert price_of("qwen-max", settings)["input"] == 1.0
    assert price_of("qwen-turbo", settings)["output"] == 3.0
    # 未命中通配前缀则回退到默认单价
    assert price_of("deepseek-chat", settings)["input"] == settings.model_price_default["input"]


# --------------------------------------------------------------------------- 任务成本
def _seed_task(session, *, tokens: int = 0, model: str = "model-a") -> str:
    """建一个租户 + 用户 + 任务，并可选地塞入一条 Agent 运行记录（带 token 用量）。"""
    tenant = session.query(Tenant).first()
    if tenant is None:
        tenant = Tenant(code="t-cost", name="成本测试租户")
        session.add(tenant)
        session.flush()
    user = session.query(User).filter(User.username == "cost-user").first()
    if user is None:
        user = User(
            tenant_id=tenant.id,
            username="cost-user",
            hashed_password="x",
            security_level=1,
        )
        session.add(user)
        session.flush()
    task = AnalysisTask(
        tenant_id=tenant.id,
        user_id=user.id,
        query="成本核算测试",
        filters={},
        status="completed",
        created_at=utcnow(),
    )
    session.add(task)
    session.flush()
    if tokens:
        session.add(
            AgentRun(
                task_id=task.id,
                agent_code="metric_agent",
                status="succeeded",
                model=model,
                prompt_tokens=tokens,
                completion_tokens=0,
                total_tokens=tokens,
                output={"model_used": True, "cache_hit_tokens": 0},
            )
        )
    session.flush()
    return task.id


def test_任务成本按agent运行记录聚合(session):
    task_id = _seed_task(session, tokens=1_000_000)
    settings = make_settings()

    breakdown = task_cost(session, task_id, settings)

    assert breakdown.amount == pytest.approx(2.0)
    assert breakdown.calls == 1


def test_未调用模型的运行不计费(session):
    task_id = _seed_task(session, tokens=0)
    settings = make_settings()

    breakdown = task_cost(session, task_id, settings)

    assert breakdown.amount == 0.0
    assert breakdown.calls == 0


def test_token上限触发超预算(session):
    task_id = _seed_task(session, tokens=5_000)
    settings = make_settings(task_token_limit=1_000)

    state = budget_state(session, task_id, settings)

    assert state["exceeded"] is True
    assert "token" in state["reasons"][0]
    assert state["action"] == "degrade"


def test_金额上限触发超预算(session):
    task_id = _seed_task(session, tokens=1_000_000)  # model-a 输入价 2 元/M
    settings = make_settings(task_cost_limit=1.0)

    state = budget_state(session, task_id, settings)

    assert state["exceeded"] is True
    assert "成本" in state["reasons"][0]


def test_未设置上限时不触发(session):
    task_id = _seed_task(session, tokens=10_000_000)
    settings = make_settings()  # 上限都是 0

    assert budget_state(session, task_id, settings)["exceeded"] is False


def test_反思成本只统计显式反思和修订运行(session):
    task_id = _seed_task(session)
    common = {
        "task_id": task_id,
        "status": "succeeded",
        "model": "model-a",
        "prompt_tokens": 250_000,
        "completion_tokens": 0,
        "total_tokens": 250_000,
    }
    session.add_all(
        [
            AgentRun(
                **common,
                agent_code="metric_agent",
                output={"model_used": True, "cache_hit_tokens": 0},
            ),
            AgentRun(
                **common,
                agent_code="review_agent",
                output={"model_used": True, "cache_hit_tokens": 0},
            ),
            AgentRun(
                **common,
                agent_code="review_agent",
                output={
                    "model_used": True,
                    "cache_hit_tokens": 0,
                    "reflection_stage": "reflection",
                },
            ),
            AgentRun(
                **common,
                agent_code="revision_agent",
                output={
                    "model_used": True,
                    "cache_hit_tokens": 0,
                    "reflection_stage": "revision",
                },
            ),
        ]
    )
    session.flush()

    breakdown = reflection_task_cost(session, task_id, make_settings())

    assert breakdown.calls == 2
    assert breakdown.input_tokens == 500_000
    assert breakdown.amount == pytest.approx(1.0)


def test_反思成本达到专属上限时触发降级(session):
    task_id = _seed_task(session)
    session.add(
        AgentRun(
            task_id=task_id,
            agent_code="report_reflection_agent",
            status="succeeded",
            model="model-a",
            prompt_tokens=500_000,
            completion_tokens=0,
            total_tokens=500_000,
            output={
                "model_used": True,
                "cache_hit_tokens": 0,
                "reflection_stage": "reflection",
            },
        )
    )
    session.flush()

    state = reflection_budget_state(
        session,
        task_id,
        make_settings(reflection_cost_limit=1.0),
    )

    assert state["exceeded"] is True
    assert state["action"] == "degrade"
    assert state["amount"] == pytest.approx(1.0)
    assert state["limits"]["reflection_cost_limit"] == 1.0


def test_租户日预算超额(session):
    task_id = _seed_task(session, tokens=1_000_000)
    task = session.get(AnalysisTask, task_id)
    settings = make_settings(tenant_daily_cost_limit=0.5)

    state = tenant_daily_budget_exceeded(session, task.tenant_id, settings)

    assert state["exceeded"] is True
    assert state["limit"] == 0.5
    assert state["amount"] == pytest.approx(2.0)


def test_未配置日限额时仍如实统计今日金额(session):
    """不限额 ≠ 不统计：早期版本在 limit=0 时直接返回 amount=0.0，看板永远显示 0。"""
    task_id = _seed_task(session, tokens=1_000_000)
    task = session.get(AnalysisTask, task_id)
    settings = make_settings()  # tenant_daily_cost_limit 默认 0（不限额）

    state = tenant_daily_budget_exceeded(session, task.tenant_id, settings)

    assert state["exceeded"] is False, "不限额不应触发熔断"
    assert state["limit"] == 0
    assert state["amount"] == pytest.approx(2.0), "不限额时也必须返回真实花销"


def test_租户日预算只统计窗口内的任务(session):
    task_id = _seed_task(session, tokens=1_000_000)
    task = session.get(AnalysisTask, task_id)
    task.created_at = utcnow() - timedelta(days=3)
    session.flush()
    settings = make_settings()

    breakdown = tenant_cost_since(
        session, task.tenant_id, since=utcnow() - timedelta(days=1), settings=settings
    )

    assert breakdown.amount == 0.0


def test_成本汇总返回窗口与限额(session):
    _seed_task(session, tokens=1_000_000)
    task = session.query(AnalysisTask).first()

    summary = cost_summary(session, task.tenant_id, days=7)

    assert summary["window_days"] == 7
    assert "cost" in summary and "today" in summary
    assert summary["cost"]["total_tokens"] >= 0


# --------------------------------------------------------------------------- 预算动作
def test_预算动作可配置为abort():
    settings = make_settings(budget_exceeded_action="abort")

    assert settings.budget_exceeded_action == "abort"


def test_日预算窗口起点早于当前时间():
    """日预算窗口起点是今天 00:00，必须早于当前时间。"""
    since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    assert since <= utcnow()
