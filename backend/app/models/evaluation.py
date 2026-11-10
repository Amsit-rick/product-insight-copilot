"""评测集与评测运行记录（设计文档 16.2 节）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base, TimestampMixin, UUIDPkMixin


class EvaluationCase(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_cases"

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, default="*")
    category: Mapped[str] = mapped_column(String(64), index=True)  # 单知识库/跨库/无答案/冲突/越权/注入…
    question: Mapped[str] = mapped_column(Text)
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    expected_kb: Mapped[str | None] = mapped_column(String(64))
    expected_doc_ids: Mapped[list] = mapped_column(JSON, default=list)
    expected_intent: Mapped[str | None] = mapped_column(String(64))
    expected_metric: Mapped[dict | None] = mapped_column(JSON)
    expect_refusal: Mapped[bool] = mapped_column(Boolean, default=False)
    expect_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    expect_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    security_level: Mapped[str] = mapped_column(String(32), default="internal")
    notes: Mapped[str | None] = mapped_column(Text)


class EvaluationRun(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "evaluation_runs"

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, default="*")
    name: Mapped[str] = mapped_column(String(128))
    dataset_version: Mapped[str] = mapped_column(String(64))
    workflow_version: Mapped[str | None] = mapped_column(String(32))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    embedding_version: Mapped[str | None] = mapped_column(String(32))
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="running")
    # 聚合指标：检索命中率、引用正确率、结构化合规率、拒答准确率…
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    details: Mapped[list] = mapped_column(JSON, default=list)
    avg_latency_ms: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
