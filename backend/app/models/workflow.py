"""分析任务、执行轨迹、证据引用、报告与审批。

对应设计文档 7/10/12 节：任务状态在每个关键节点保存，支持失败重试与断点恢复。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base, TimestampMixin, UUIDPkMixin
from app.models.enums import (
    ApprovalStatus,
    NodeStatus,
    RiskLevel,
    StatementType,
    TaskStatus,
)


class AnalysisTask(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "analysis_tasks"
    __table_args__ = (Index("ix_task_tenant_status", "tenant_id", "status"),)

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    title: Mapped[str | None] = mapped_column(String(255))
    query: Mapped[str] = mapped_column(Text)
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default=TaskStatus.PENDING.value, index=True)
    current_node: Mapped[str | None] = mapped_column(String(64))
    current_phase: Mapped[str | None] = mapped_column(String(64))
    checkpoint_version: Mapped[int] = mapped_column(Integer, default=0)
    phase_attempt: Mapped[int] = mapped_column(Integer, default=0)
    resume_count: Mapped[int] = mapped_column(Integer, default=0)
    resumed_from_checkpoint: Mapped[bool] = mapped_column(Boolean, default=False)
    wake_reason: Mapped[str | None] = mapped_column(String(64))
    state_revision: Mapped[int] = mapped_column(Integer, default=0)
    intent: Mapped[str | None] = mapped_column(String(64))
    plan: Mapped[list] = mapped_column(JSON, default=list)
    risk_level: Mapped[str] = mapped_column(String(16), default=RiskLevel.LOW.value)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    workflow_version: Mapped[str | None] = mapped_column(String(32))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), index=True)
    result: Mapped[dict | None] = mapped_column(JSON)
    error: Mapped[dict | None] = mapped_column(JSON)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    waiting_reason: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class AnalysisEvent(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "analysis_events"
    __table_args__ = (Index("ix_event_task", "task_id", "created_at"),)

    task_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    node: Mapped[str | None] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class TaskExecutionUnit(UUIDPkMixin, TimestampMixin, Base):
    """持久化执行单元：承载 workflow、Agent、工具、审批和后台任务的父子关系。"""

    __tablename__ = "task_execution_units"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "owner_type",
            "owner_ref_id",
            name="uq_task_execution_owner",
        ),
        Index("ix_task_execution_parent", "task_id", "parent_unit_id"),
        Index("ix_task_execution_status", "task_id", "status"),
    )

    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parent_unit_id: Mapped[str | None] = mapped_column(String(36), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    unit_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    owner_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    owner_ref_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    execution_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="attached")
    cancellation_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cascade"
    )
    failure_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="continue")
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class AgentRun(UUIDPkMixin, TimestampMixin, Base):
    """Agent 执行记录：节点、模型、Prompt 版本、耗时、Token、输出摘要、错误与重试次数。"""

    __tablename__ = "agent_runs"
    __table_args__ = (Index("ix_agent_run_task", "task_id", "agent_code"),)

    task_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    agent_code: Mapped[str] = mapped_column(String(64))
    node: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default=NodeStatus.PENDING.value)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    input_summary: Mapped[str | None] = mapped_column(Text)
    output: Mapped[dict | None] = mapped_column(JSON)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ToolCall(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "tool_calls"
    __table_args__ = (Index("ix_tool_call_task", "task_id"),)

    task_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    agent_run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    tool_name: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict] = mapped_column(JSON, default=dict)
    result_summary: Mapped[str | None] = mapped_column(Text)
    # 完整、脱敏后的结构化结果，用于确定性历史播放；不保存模型思维链。
    result: Mapped[dict | None] = mapped_column(JSON)
    result_digest: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="success")
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # 策略引擎判定结果：allow / deny / require_approval
    policy_decision: Mapped[str | None] = mapped_column(String(32))
    replay_policy: Mapped[str | None] = mapped_column(String(16))
    side_effect_level: Mapped[str | None] = mapped_column(String(16))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), index=True)
    replayed: Mapped[bool] = mapped_column(Boolean, default=False)
    approval_reference: Mapped[str | None] = mapped_column(String(128))


class TaskReplaySnapshot(UUIDPkMixin, TimestampMixin, Base):
    """任务历史的不可变播放包，与重新调用当前模型/工具严格区分。"""

    __tablename__ = "task_replay_snapshots"
    __table_args__ = (
        UniqueConstraint("task_id", "digest", name="uq_replay_task_digest"),
        Index("ix_replay_task_created", "task_id", "created_at"),
    )

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    task_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(64), default="task-replay.v1")
    package: Mapped[dict] = mapped_column(JSON, default=dict)
    digest: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36), index=True)


class EvidenceCitation(UUIDPkMixin, TimestampMixin, Base):
    """证据引用：报告段落与文档切片 / 指标查询的对应关系，可定位、可审计。"""

    __tablename__ = "evidence_citations"
    __table_args__ = (Index("ix_citation_task", "task_id"),)

    task_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    report_id: Mapped[str | None] = mapped_column(String(36), index=True)
    statement_id: Mapped[str | None] = mapped_column(String(64))
    statement_type: Mapped[str] = mapped_column(
        String(32), default=StatementType.FACT.value, index=True
    )
    source_kind: Mapped[str] = mapped_column(String(16), default="document")
    chunk_id: Mapped[str | None] = mapped_column(String(36))
    document_id: Mapped[str | None] = mapped_column(String(36))
    kb_code: Mapped[str | None] = mapped_column(String(64))
    source_name: Mapped[str | None] = mapped_column(String(255))
    page: Mapped[int | None] = mapped_column(Integer)
    document_version: Mapped[str | None] = mapped_column(String(32))
    content: Mapped[str | None] = mapped_column(Text)
    relevance_score: Mapped[float | None] = mapped_column(Float)
    metric_ref: Mapped[dict | None] = mapped_column(JSON)
    permission_checked: Mapped[bool] = mapped_column(Boolean, default=True)


class Report(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "reports"

    task_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(36), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    summary: Mapped[str | None] = mapped_column(Text)
    impact_scope: Mapped[list] = mapped_column(JSON, default=list)
    facts: Mapped[list] = mapped_column(JSON, default=list)
    inferences: Mapped[list] = mapped_column(JSON, default=list)
    hypotheses: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    conflicts: Mapped[list] = mapped_column(JSON, default=list)
    recommendations: Mapped[list] = mapped_column(JSON, default=list)
    priority: Mapped[str | None] = mapped_column(String(8))
    confidence: Mapped[float | None] = mapped_column(Float)
    needs_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_level: Mapped[str] = mapped_column(String(16), default=RiskLevel.LOW.value)
    approval_status: Mapped[str] = mapped_column(
        String(32), default=ApprovalStatus.NOT_REQUIRED.value, index=True
    )
    content_json: Mapped[dict] = mapped_column(JSON, default=dict)
    content_md: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(36))


class Approval(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approval_task", "task_id"),)

    task_id: Mapped[str] = mapped_column(String(36), index=True, nullable=False)
    report_id: Mapped[str | None] = mapped_column(String(36), index=True)
    status: Mapped[str] = mapped_column(String(32), default=ApprovalStatus.PENDING.value)
    action: Mapped[str | None] = mapped_column(String(32))
    reviewer_id: Mapped[str | None] = mapped_column(String(36))
    comment: Mapped[str | None] = mapped_column(Text)
    # 人工修改后的报告差异，进入审计
    diff: Mapped[dict | None] = mapped_column(JSON)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditLog(UUIDPkMixin, TimestampMixin, Base):
    """审计日志：工具调用、证据引用、人工修改与审批结果全量留痕。"""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_tenant", "tenant_id", "created_at"),)

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, default="*")
    actor_id: Mapped[str | None] = mapped_column(String(36), index=True)
    actor_name: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16), default="success")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))


class OperationalAlert(UUIDPkMixin, TimestampMixin, Base):
    """需要管理员确认和处理的运行告警，不替代不可变审计日志。"""

    __tablename__ = "operational_alerts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "fingerprint", name="uq_operational_alert_fingerprint"),
        Index("ix_operational_alert_tenant_status", "tenant_id", "status"),
        Index("ix_operational_alert_severity", "severity", "last_occurred_at"),
    )

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="warning")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    source_type: Mapped[str | None] = mapped_column(String(64))
    source_id: Mapped[str | None] = mapped_column(String(64), index=True)
    fingerprint: Mapped[str] = mapped_column(String(160), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    acknowledged_by: Mapped[str | None] = mapped_column(String(36), index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_by: Mapped[str | None] = mapped_column(String(36), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON, default=dict)


class OperationalAlertDelivery(UUIDPkMixin, TimestampMixin, Base):
    """运行告警的外部投递队列，支持租约、重试和死信。"""

    __tablename__ = "operational_alert_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "alert_id",
            "occurrence_no",
            "channel",
            name="uq_operational_alert_delivery_occurrence",
        ),
        Index("ix_alert_delivery_status_visible", "status", "visible_at"),
        Index("ix_alert_delivery_tenant_status", "tenant_id", "status"),
    )

    alert_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(24), nullable=False, default="webhook")
    occurrence_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    visible_at: Mapped[datetime | None] = mapped_column(DateTime)
    claimed_by: Mapped[str | None] = mapped_column(String(128), index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime)


class ReflectionReleaseState(UUIDPkMixin, TimestampMixin, Base):
    """每个租户一条反思灰度发布状态，供所有 API/worker 副本共同读取。"""

    __tablename__ = "reflection_release_states"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_reflection_release_tenant"),
        Index("ix_reflection_release_status", "status"),
    )

    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    current_percent: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    recommended_percent: Mapped[int | None] = mapped_column(Integer)
    recommendation: Mapped[str] = mapped_column(String(32), default="hold", nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    updated_by: Mapped[str | None] = mapped_column(String(36), index=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime)
