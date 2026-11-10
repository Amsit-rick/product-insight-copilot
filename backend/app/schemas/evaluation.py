"""评测契约（设计文档 16.2 节）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class EvaluationCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    category: str
    question: str
    filters: dict = Field(default_factory=dict)
    expected_kb: str | None = None
    expected_doc_ids: list[str] = Field(default_factory=list)
    expected_intent: str | None = None
    expected_metric: dict | None = None
    expect_refusal: bool = False
    expect_conflict: bool = False
    expect_human_review: bool = False
    security_level: str = "internal"
    notes: str | None = None


class EvaluationRunRequest(BaseModel):
    dataset_version: str = Field(default="eval-1.0.0", max_length=64)
    name: str | None = Field(default=None, max_length=128)
    limit: int | None = Field(default=None, ge=1, le=200, description="只跑前 N 条，便于快速回归")
    categories: list[str] | None = None
    use_llm: bool = Field(default=False, description="是否启用真实模型（默认关闭以保证可重复）")


class EvaluationCaseResult(BaseModel):
    case_id: str
    category: str
    question: str
    passed: bool = False
    checks: dict[str, bool] = Field(default_factory=dict)
    detail: dict = Field(default_factory=dict)
    latency_ms: int = 0


class EvaluationRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    dataset_version: str
    workflow_version: str | None = None
    prompt_version: str | None = None
    embedding_version: str | None = None
    case_count: int = 0
    status: str = "running"
    metrics: dict = Field(default_factory=dict)
    details: list[dict] = Field(default_factory=list)
    avg_latency_ms: float | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
