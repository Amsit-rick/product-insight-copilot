"""诊断任务契约（对应设计文档 4.2 / 7.2 / 13 节）。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import IntentCode, KnowledgeBaseCode, RiskLevel, TaskStatus

ALLOWED_FILTER_KEYS = {
    "product",
    "platform",
    "app_version",
    "channel",
    "device",
    "date_from",
    "date_to",
    "department",
}


class TaskCreateRequest(BaseModel):
    """新建诊断：问题 + 产品 + 版本 + 时间范围 + 知识库与指标范围。"""

    query: str = Field(min_length=6, max_length=2000, description="产品问题描述")
    title: str | None = Field(default=None, max_length=255)
    product: str | None = Field(default=None, max_length=64)
    platform: str | None = Field(default=None, max_length=32)
    app_version: str | None = Field(default=None, max_length=32)
    channel: str | None = Field(default=None, max_length=64)
    device: str | None = Field(default=None, max_length=64)
    date_from: date | None = None
    date_to: date | None = None
    department: str | None = Field(default=None, max_length=64)
    knowledge_bases: list[str] | None = Field(
        default=None, description="不传则按角色权限取全部可见知识库"
    )
    metrics: list[str] | None = Field(default=None, description="需要查询的指标名")
    priority: str | None = Field(default=None, pattern="^(P0|P1|P2|P3)$")
    force_human_review: bool = False
    idempotency_key: str | None = Field(default=None, max_length=128)

    @field_validator("knowledge_bases")
    @classmethod
    def _check_kb(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        valid = {kb.value for kb in KnowledgeBaseCode}
        unknown = [v for v in value if v not in valid]
        if unknown:
            raise ValueError(f"未知知识库: {unknown}")
        return value

    @field_validator("date_to")
    @classmethod
    def _check_range(cls, value: date | None, info) -> date | None:
        start = info.data.get("date_from")
        if value and start and value < start:
            raise ValueError("date_to 不能早于 date_from")
        return value

    def to_filters(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        filters: dict[str, Any] = {
            key: data.get(key) for key in ALLOWED_FILTER_KEYS if data.get(key) is not None
        }
        filters["knowledge_bases"] = data.get("knowledge_bases")
        filters["metrics"] = data.get("metrics")
        return filters


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    tenant_id: str
    user_id: str
    title: str | None = None
    query: str
    status: TaskStatus
    current_node: str | None = None
    current_phase: str | None = None
    checkpoint_version: int = 0
    phase_attempt: int = 0
    resume_count: int = 0
    resumed_from_checkpoint: bool = False
    wake_reason: str | None = None
    state_revision: int = 0
    intent: IntentCode | str | None = None
    plan: list[dict] = Field(default_factory=list)
    filters: dict = Field(default_factory=dict)
    risk_level: RiskLevel | str = RiskLevel.LOW
    retry_count: int = 0
    workflow_version: str | None = None
    prompt_version: str | None = None
    waiting_reason: str | None = None
    idempotency_key: str | None = None
    evidence_count: int = 0
    report_id: str | None = None
    error: dict | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TaskEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    node: str | None = None
    level: str
    message: str
    payload: dict = Field(default_factory=dict)
    created_at: datetime


class TaskRetryRequest(BaseModel):
    # 默认在原有重试次数上继续累计；只有显式要求时才清零（例如换了数据源重跑）
    reset_retry_count: bool = False
    reason: str | None = Field(default=None, max_length=255)
    expected_revision: int | None = Field(default=None, ge=0)


class TaskCancelRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)


class TaskApproveRequest(BaseModel):
    """人工审核：通过 / 驳回 / 修改后通过。"""

    action: str = Field(pattern="^(approve|reject|modify)$")
    comment: str | None = Field(default=None, max_length=2000)
    modifications: dict | None = Field(
        default=None, description="修改后的报告字段（仅 action=modify 时生效）"
    )
    expected_revision: int | None = Field(default=None, ge=0)
