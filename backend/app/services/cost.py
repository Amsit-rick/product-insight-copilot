"""成本核算与预算控制（设计文档 §15 阶段六 / §17「成本控制」）。

为什么单独成模块：
- 成本是**跨表聚合**的概念（一次任务由多个 Agent 运行组成，一次租户消耗由多个任务组成），
  散落在各处计算必然对不齐；
- 预算判断要在"调用模型之前"发生，因此需要一个能快速回答
  "这次任务已经花了多少 / 有没有超"的入口。

计价口径：
    金额 = 输入 token × 输入单价 + 输出 token × 输出单价（命中缓存的部分按缓存单价）
单价表来自 `settings.model_prices`（示例默认值，务必按供应商控制台覆盖）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models.workflow import AgentRun, AnalysisTask

MILLION = 1_000_000
REFLECTION_AGENT_CODES = {
    "revision_agent",
    "report_reflection_agent",
    "report_revision_agent",
}


@dataclass
class UsageRecord:
    """一次模型调用的用量（对应 agent_runs 的一行）。"""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0


@dataclass
class CostBreakdown:
    currency: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_hit_tokens: int = 0
    amount: float = 0.0
    calls: int = 0
    per_model: dict[str, dict] = field(default_factory=dict)
    unpriced_models: list[str] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict:
        return {
            "currency": self.currency,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
            "total_tokens": self.total_tokens,
            "amount": round(self.amount, 6),
            "calls": self.calls,
            "per_model": {
                name: {**detail, "amount": round(detail["amount"], 6)}
                for name, detail in self.per_model.items()
            },
            "unpriced_models": self.unpriced_models,
        }


def price_of(model: str, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    prices = settings.model_prices or {}
    if model in prices:
        return prices[model]
    # 前缀匹配：便于用 "deepseek-chat-*" 这类写法覆盖同一系列的型号
    for name, price in prices.items():
        if name.endswith("*") and model.startswith(name[:-1]):
            return price
    return settings.model_price_default or {"input": 0.0, "output": 0.0}


def record_amount(record: UsageRecord, settings: Settings | None = None) -> tuple[float, bool]:
    """返回 (金额, 是否用了兜底单价)。"""
    settings = settings or get_settings()
    price = price_of(record.model, settings)
    priced = record.model in (settings.model_prices or {}) or any(
        name.endswith("*") and record.model.startswith(name[:-1])
        for name in (settings.model_prices or {})
    )
    cached = min(record.cache_hit_tokens, record.input_tokens)
    fresh_input = max(0, record.input_tokens - cached)
    cache_price = price.get("cache_hit_input", price.get("input", 0.0))
    amount = (
        fresh_input * price.get("input", 0.0)
        + cached * cache_price
        + record.output_tokens * price.get("output", 0.0)
    ) / MILLION
    return amount, not priced


def compute_cost(
    records: list[UsageRecord], settings: Settings | None = None, *, currency: str | None = None
) -> CostBreakdown:
    settings = settings or get_settings()
    breakdown = CostBreakdown(currency=currency or settings.currency)
    for record in records:
        amount, unpriced = record_amount(record, settings)
        breakdown.calls += 1
        breakdown.input_tokens += record.input_tokens
        breakdown.output_tokens += record.output_tokens
        breakdown.cache_hit_tokens += record.cache_hit_tokens
        breakdown.amount += amount
        detail = breakdown.per_model.setdefault(
            record.model or "stub",
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "amount": 0.0},
        )
        detail["calls"] += 1
        detail["input_tokens"] += record.input_tokens
        detail["output_tokens"] += record.output_tokens
        detail["amount"] += amount
        if unpriced and record.model not in breakdown.unpriced_models:
            breakdown.unpriced_models.append(record.model)
    return breakdown


def _records_from_rows(rows: list[AgentRun]) -> list[UsageRecord]:
    records: list[UsageRecord] = []
    for row in rows:
        output = row.output or {}
        if (row.total_tokens or 0) == 0 and not output.get("model_used"):
            # 没真正调用模型的运行（stub 降级）不计费
            continue
        records.append(
            UsageRecord(
                model=row.model or "stub",
                input_tokens=row.prompt_tokens or 0,
                output_tokens=row.completion_tokens or 0,
                cache_hit_tokens=int(output.get("cache_hit_tokens") or 0),
            )
        )
    return records


def task_cost(session: Session, task_id: str, settings: Settings | None = None) -> CostBreakdown:
    rows = session.query(AgentRun).filter(AgentRun.task_id == task_id).all()
    return compute_cost(_records_from_rows(rows), settings)


def reflection_task_cost(
    session: Session, task_id: str, settings: Settings | None = None
) -> CostBreakdown:
    """只统计反思与修订阶段的模型成本。

    ``review_agent`` 也承担旧流程质检职责，因此不能只按 agent_code 计入；
    新反思流程会在 AgentRun.output 中写入 ``reflection_stage`` 标记。
    独立的反思/修订 Agent 代码始终属于该预算域，可兼容标记上线前的记录。
    """
    rows = session.query(AgentRun).filter(AgentRun.task_id == task_id).all()
    reflection_rows = []
    for row in rows:
        output = row.output or {}
        stage = output.get("reflection_stage")
        if row.agent_code in REFLECTION_AGENT_CODES or stage in {"reflection", "revision"}:
            reflection_rows.append(row)
    return compute_cost(_records_from_rows(reflection_rows), settings)


def tenant_cost_since(
    session: Session,
    tenant_id: str,
    *,
    since: datetime,
    settings: Settings | None = None,
) -> CostBreakdown:
    rows = (
        session.query(AgentRun)
        .join(AnalysisTask, AnalysisTask.id == AgentRun.task_id)
        .filter(AnalysisTask.tenant_id == tenant_id)
        .filter(AnalysisTask.created_at >= since)
        .all()
    )
    return compute_cost(_records_from_rows(rows), settings)


def budget_state(
    session: Session, task_id: str, settings: Settings | None = None
) -> dict[str, Any]:
    """当前任务的预算状态（供 Agent 在调用模型前判断）。"""
    settings = settings or get_settings()
    breakdown = task_cost(session, task_id, settings)
    reasons: list[str] = []
    if settings.task_token_limit and breakdown.total_tokens >= settings.task_token_limit:
        reasons.append(
            f"任务 token 用量 {breakdown.total_tokens} 已达上限 {settings.task_token_limit}"
        )
    if settings.task_cost_limit and breakdown.amount >= settings.task_cost_limit:
        reasons.append(
            f"任务成本 {breakdown.amount:.4f} {breakdown.currency} "
            f"已达上限 {settings.task_cost_limit}"
        )
    return {
        "exceeded": bool(reasons),
        "reasons": reasons,
        "tokens": breakdown.total_tokens,
        "amount": round(breakdown.amount, 6),
        "currency": breakdown.currency,
        "action": settings.budget_exceeded_action,
        "limits": {
            "task_token_limit": settings.task_token_limit,
            "task_cost_limit": settings.task_cost_limit,
            "tenant_daily_cost_limit": settings.tenant_daily_cost_limit,
        },
    }


def reflection_budget_state(
    session: Session, task_id: str, settings: Settings | None = None
) -> dict[str, Any]:
    """反思专属金额预算状态；超限时只停止额外模型调用，保留代码硬门禁。"""
    settings = settings or get_settings()
    breakdown = reflection_task_cost(session, task_id, settings)
    limit = float(settings.reflection_cost_limit or 0)
    exceeded = bool(limit) and breakdown.amount >= limit
    reasons = []
    if exceeded:
        reasons.append(
            f"反思成本 {breakdown.amount:.4f} {breakdown.currency} 已达专属上限 {limit}"
        )
    return {
        "exceeded": exceeded,
        "reasons": reasons,
        "tokens": breakdown.total_tokens,
        "amount": round(breakdown.amount, 6),
        "currency": breakdown.currency,
        # 反思预算超限不得让整个业务任务失败；退回确定性检查后由安全路由决定是否转人工。
        "action": "degrade",
        "limits": {"reflection_cost_limit": limit},
    }


def tenant_daily_budget_exceeded(
    session: Session, tenant_id: str, settings: Settings | None = None
) -> dict[str, Any]:
    """租户当日预算是否已超（在创建任务/调用模型前检查）。

    注意：**即使没配限额也要如实统计金额**。早期版本在 `tenant_daily_cost_limit=0`（不限额）时
    直接返回 `amount=0.0`，于是工作台"今日成本"永远显示 0——看板和验证脚本都被这个假 0 骗过。
    不限额只影响 `exceeded`，不影响金额。
    """
    settings = settings or get_settings()
    since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    breakdown = tenant_cost_since(session, tenant_id, since=since, settings=settings)
    limit = settings.tenant_daily_cost_limit
    return {
        "exceeded": bool(limit) and breakdown.amount >= limit,
        "amount": round(breakdown.amount, 6),
        "limit": limit,
        "currency": breakdown.currency,
        "since": since.isoformat(),
    }


def cost_summary(session: Session, tenant_id: str, *, days: int = 7) -> dict:
    """近 N 天的成本概览（工作台/看板用）。"""
    settings = get_settings()
    since = datetime.utcnow() - timedelta(days=days)
    breakdown = tenant_cost_since(session, tenant_id, since=since, settings=settings)
    daily = (
        session.query(
            func.date(AnalysisTask.created_at), func.count(AnalysisTask.id)
        )
        .filter(AnalysisTask.tenant_id == tenant_id)
        .filter(AnalysisTask.created_at >= since)
        .group_by(func.date(AnalysisTask.created_at))
        .all()
    )
    return {
        "window_days": days,
        "cost": breakdown.as_dict(),
        "tasks_per_day": {str(day): int(count) for day, count in daily},
        "daily_limit": settings.tenant_daily_cost_limit,
        "today": tenant_daily_budget_exceeded(session, tenant_id, settings),
    }
