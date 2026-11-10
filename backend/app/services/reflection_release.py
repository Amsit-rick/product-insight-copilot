"""反思灰度的独立发布判定器。

只消费结构化指标，不调用模型。扩大流量需要人工确认；安全硬门禁失败可自动回滚。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
from math import ceil
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.base import utcnow
from app.models.enums import TaskStatus
from app.models.workflow import AgentRun, AnalysisTask, ReflectionReleaseState

ReleaseRecommendation = Literal["hold", "promote", "pause", "rollback"]


@dataclass(frozen=True)
class ReflectionReleaseMetrics:
    treatment_sample: int = 0
    control_sample: int = 0
    treatment_error_rate: float = 0.0
    control_error_rate: float = 0.0
    safety_violation_rate: float = 0.0
    protected_field_violation_rate: float = 0.0
    bounded_termination_rate: float = 1.0
    treatment_p95_latency_ms: float = 0.0
    control_p95_latency_ms: float = 0.0
    treatment_avg_tokens: float = 0.0
    control_avg_tokens: float = 0.0
    treatment_human_review_rate: float = 0.0
    control_human_review_rate: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReflectionReleaseDecision:
    recommendation: ReleaseRecommendation
    current_percent: int
    recommended_percent: int
    reason: str
    requires_human_approval: bool
    auto_apply: bool
    metrics: ReflectionReleaseMetrics

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["metrics"] = self.metrics.as_dict()
        return payload


def _ratio_exceeded(treatment: float, control: float, multiplier: float) -> bool:
    if treatment <= 0 or control <= 0:
        return False
    return treatment > control * multiplier


def _next_percent(current: int) -> int:
    for step in (10, 25, 50, 100):
        if current < step:
            return step
    return 100


def evaluate_reflection_release(
    metrics: ReflectionReleaseMetrics,
    *,
    current_percent: int,
    settings: Settings,
) -> ReflectionReleaseDecision:
    current = max(0, min(100, int(current_percent)))

    if (
        metrics.treatment_sample < settings.reflection_release_min_sample
        or metrics.control_sample < settings.reflection_release_min_sample
    ):
        return ReflectionReleaseDecision(
            recommendation="hold",
            current_percent=current,
            recommended_percent=current,
            reason=(
                "样本不足：反思组和对照组均需至少 "
                f"{settings.reflection_release_min_sample} 个任务，当前为 "
                f"{metrics.treatment_sample}/{metrics.control_sample}"
            ),
            requires_human_approval=False,
            auto_apply=False,
            metrics=metrics,
        )

    safety_failed = (
        metrics.safety_violation_rate > 0
        or metrics.protected_field_violation_rate > 0
        or metrics.bounded_termination_rate < settings.reflection_release_bounded_termination_min
        or metrics.treatment_error_rate > settings.reflection_release_error_rate_limit
    )
    if safety_failed:
        return ReflectionReleaseDecision(
            recommendation="rollback",
            current_percent=current,
            recommended_percent=0,
            reason="安全硬指标未达标：存在安全/受保护字段违规、未有界终止或错误率超限",
            requires_human_approval=False,
            auto_apply=bool(settings.reflection_rollout_auto_guard),
            metrics=metrics,
        )

    if _ratio_exceeded(
        metrics.treatment_p95_latency_ms,
        metrics.control_p95_latency_ms,
        settings.reflection_release_latency_multiplier,
    ) or _ratio_exceeded(
        metrics.treatment_avg_tokens,
        metrics.control_avg_tokens,
        settings.reflection_release_token_multiplier,
    ):
        return ReflectionReleaseDecision(
            recommendation="pause",
            current_percent=current,
            recommended_percent=current,
            reason="延迟或 Token 成本相对对照组严重超限，停止继续扩大",
            requires_human_approval=False,
            auto_apply=False,
            metrics=metrics,
        )

    if current >= 100:
        return ReflectionReleaseDecision(
            recommendation="hold",
            current_percent=current,
            recommended_percent=100,
            reason="已达到全量覆盖，继续保持并监控安全、质量和成本指标",
            requires_human_approval=False,
            auto_apply=False,
            metrics=metrics,
        )

    quality_not_worse = (
        metrics.treatment_error_rate <= metrics.control_error_rate
        and metrics.treatment_human_review_rate <= metrics.control_human_review_rate
    )
    if quality_not_worse:
        return ReflectionReleaseDecision(
            recommendation="promote",
            current_percent=current,
            recommended_percent=_next_percent(current),
            reason="样本、安全、质量和成本门禁均达标，可提交管理员确认后阶梯扩大",
            requires_human_approval=True,
            auto_apply=False,
            metrics=metrics,
        )

    return ReflectionReleaseDecision(
        recommendation="hold",
        current_percent=current,
        recommended_percent=current,
        reason="安全门禁通过，但质量指标尚未优于对照组，保持当前比例继续观察",
        requires_human_approval=False,
        auto_apply=False,
        metrics=metrics,
    )


def get_or_create_release_state(
    session: Session, tenant_id: str, settings: Settings
) -> ReflectionReleaseState:
    row = (
        session.query(ReflectionReleaseState)
        .filter(ReflectionReleaseState.tenant_id == tenant_id)
        .one_or_none()
    )
    if row is None:
        try:
            with session.begin_nested():
                row = ReflectionReleaseState(
                    tenant_id=tenant_id,
                    status="active",
                    current_percent=int(settings.reflection_rollout_percent),
                    recommendation="hold",
                    metrics={},
                    version=1,
                )
                session.add(row)
                session.flush()
        except IntegrityError:
            # 多 worker 首次同时命中同一租户时，唯一约束决定胜者；败者读取已创建行。
            row = (
                session.query(ReflectionReleaseState)
                .filter(ReflectionReleaseState.tenant_id == tenant_id)
                .one()
            )
    return row


def release_state_payload(row: ReflectionReleaseState) -> dict[str, Any]:
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "status": row.status,
        "current_percent": row.current_percent,
        "recommended_percent": row.recommended_percent,
        "recommendation": row.recommendation,
        "reason": row.reason,
        "metrics": row.metrics or {},
        "version": row.version,
        "updated_by": row.updated_by,
        "last_evaluated_at": row.last_evaluated_at.isoformat() if row.last_evaluated_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _rollout_from_task(task: AnalysisTask) -> dict[str, Any] | None:
    digest = (task.result or {}).get("state_digest") or {}
    filters = digest.get("filters") or task.filters or {}
    rollout = filters.get("report_reflection_rollout") or filters.get("reflection_rollout")
    return rollout if isinstance(rollout, dict) else None


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ceil(percentile * len(ordered)) - 1))
    return float(ordered[index])


def _reflection_history(task: AnalysisTask) -> dict[str, Any]:
    return dict(((task.result or {}).get("state_digest") or {}).get("reflection_history") or {})


def _has_safety_violation(task: AnalysisTask) -> bool:
    for result in _reflection_history(task).values():
        for issue in (result or {}).get("issues") or []:
            if str(issue.get("severity") or "").lower() == "critical":
                return True
    return False


def _has_protected_violation(task: AnalysisTask) -> bool:
    digest = (task.result or {}).get("state_digest") or {}
    if digest.get("reflection_stop_reason") == "protected_decision_changed":
        return True
    return any(
        issue.get("code") == "protected_decision_changed"
        for result in _reflection_history(task).values()
        for issue in (result or {}).get("issues") or []
    )


def collect_reflection_release_metrics(
    session: Session,
    tenant_id: str,
    *,
    days: int = 14,
) -> ReflectionReleaseMetrics:
    cutoff = utcnow() - timedelta(days=max(1, int(days)))
    terminal = {
        TaskStatus.COMPLETED.value,
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
        TaskStatus.WAITING_HUMAN.value,
    }
    candidates = (
        session.query(AnalysisTask)
        .filter(
            AnalysisTask.tenant_id == tenant_id,
            AnalysisTask.created_at >= cutoff,
            AnalysisTask.status.in_(terminal),
        )
        .all()
    )
    treatment: list[AnalysisTask] = []
    control: list[AnalysisTask] = []
    for task in candidates:
        rollout = _rollout_from_task(task)
        if not rollout:
            continue
        if rollout.get("enabled") is True:
            treatment.append(task)
        elif rollout.get("reason") == "control_group":
            control.append(task)

    task_ids = [task.id for task in [*treatment, *control]]
    run_totals: dict[str, dict[str, float]] = {
        task_id: {"latency": 0.0, "tokens": 0.0} for task_id in task_ids
    }
    if task_ids:
        for run in session.query(AgentRun).filter(AgentRun.task_id.in_(task_ids)).all():
            totals = run_totals.setdefault(run.task_id, {"latency": 0.0, "tokens": 0.0})
            totals["latency"] += float(run.latency_ms or 0)
            totals["tokens"] += float(run.total_tokens or 0)

    def rate(rows: list[AnalysisTask], predicate) -> float:
        return sum(1 for row in rows if predicate(row)) / len(rows) if rows else 0.0

    treatment_latency = [run_totals[row.id]["latency"] for row in treatment]
    control_latency = [run_totals[row.id]["latency"] for row in control]
    treatment_tokens = [run_totals[row.id]["tokens"] for row in treatment]
    control_tokens = [run_totals[row.id]["tokens"] for row in control]

    def human_review(task: AnalysisTask) -> bool:
        digest = (task.result or {}).get("state_digest") or {}
        return task.status == TaskStatus.WAITING_HUMAN.value or bool(digest.get("needs_human_review"))

    return ReflectionReleaseMetrics(
        treatment_sample=len(treatment),
        control_sample=len(control),
        treatment_error_rate=rate(treatment, lambda row: row.status == TaskStatus.FAILED.value),
        control_error_rate=rate(control, lambda row: row.status == TaskStatus.FAILED.value),
        safety_violation_rate=rate(treatment, _has_safety_violation),
        protected_field_violation_rate=rate(treatment, _has_protected_violation),
        bounded_termination_rate=rate(treatment, lambda row: row.status in terminal),
        treatment_p95_latency_ms=_percentile(treatment_latency, 0.95),
        control_p95_latency_ms=_percentile(control_latency, 0.95),
        treatment_avg_tokens=(sum(treatment_tokens) / len(treatment_tokens) if treatment_tokens else 0.0),
        control_avg_tokens=(sum(control_tokens) / len(control_tokens) if control_tokens else 0.0),
        treatment_human_review_rate=rate(treatment, human_review),
        control_human_review_rate=rate(control, human_review),
    )


def refresh_release_decision(
    session: Session,
    tenant_id: str,
    settings: Settings,
    *,
    days: int = 14,
    force: bool = False,
) -> tuple[ReflectionReleaseState, ReflectionReleaseDecision, bool]:
    """刷新发布建议；安全失败时按配置自动回滚。返回是否发生自动回滚。"""
    row = get_or_create_release_state(session, tenant_id, settings)
    if (
        not force
        and row.last_evaluated_at is not None
        and utcnow() - row.last_evaluated_at < timedelta(minutes=5)
        and row.metrics
    ):
        metrics = ReflectionReleaseMetrics(**row.metrics)
    else:
        metrics = collect_reflection_release_metrics(session, tenant_id, days=days)
    decision = evaluate_reflection_release(
        metrics, current_percent=row.current_percent, settings=settings
    )
    row.metrics = metrics.as_dict()
    row.recommendation = decision.recommendation
    row.recommended_percent = decision.recommended_percent
    row.reason = decision.reason
    row.last_evaluated_at = utcnow()
    auto_rolled_back = False
    if decision.recommendation == "rollback" and decision.auto_apply and row.current_percent > 0:
        row.status = "rolled_back"
        row.current_percent = 0
        row.version += 1
        auto_rolled_back = True
    session.flush()
    return row, decision, auto_rolled_back


def release_state_for_rollout(
    session: Session, tenant_id: str, settings: Settings
) -> dict[str, Any]:
    """工作流取发布状态的单一入口；必要时执行自动安全回滚并留痕。"""
    row, decision, auto_rolled_back = refresh_release_decision(
        session, tenant_id, settings, force=False
    )
    if auto_rolled_back:
        from app.services.operational_alerts import emit_reflection_auto_rollback

        emit_reflection_auto_rollback(
            session,
            tenant_id=tenant_id,
            release_state_id=row.id,
            reason=decision.reason,
        )
    return release_state_payload(row)


__all__ = [
    "ReflectionReleaseDecision",
    "ReflectionReleaseMetrics",
    "collect_reflection_release_metrics",
    "evaluate_reflection_release",
    "get_or_create_release_state",
    "refresh_release_decision",
    "release_state_for_rollout",
    "release_state_payload",
]
