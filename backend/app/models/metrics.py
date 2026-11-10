"""指标字典、指标快照与导入记录。

对应设计文档 8 节：两种数据来源统一收敛为 MetricService，Agent 不直接访问数据库。
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, Float, Integer, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.models.base import Base, TimestampMixin, UUIDPkMixin
from app.models.enums import ImportStatus


class MetricDefinition(UUIDPkMixin, TimestampMixin, Base):
    """指标口径：名称、分子分母、周期与允许维度，导入校验依赖此表。"""

    __tablename__ = "metric_definitions"

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, default="*")
    name: Mapped[str] = mapped_column(String(64), index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    numerator: Mapped[str | None] = mapped_column(String(255))
    denominator: Mapped[str | None] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(16), default="ratio")
    period: Mapped[str] = mapped_column(String(16), default="day")
    allowed_dimensions: Mapped[list] = mapped_column(JSON, default=list)
    value_type: Mapped[str] = mapped_column(String(16), default="float")
    higher_is_better: Mapped[bool] = mapped_column(Boolean, default=True)
    source_version: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class MetricSnapshot(UUIDPkMixin, TimestampMixin, Base):
    """标准化指标表：一行 = 一个指标在某个维度组合与日期上的取值。"""

    __tablename__ = "metric_snapshots"
    __table_args__ = (
        Index("ix_metric_lookup", "tenant_id", "metric_name", "stat_date"),
        Index("ix_metric_version", "tenant_id", "app_version"),
    )

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, default="*")
    metric_name: Mapped[str] = mapped_column(String(64), index=True)
    platform: Mapped[str | None] = mapped_column(String(32), index=True)
    app_version: Mapped[str | None] = mapped_column(String(32), index=True)
    channel: Mapped[str | None] = mapped_column(String(64), index=True)
    device: Mapped[str | None] = mapped_column(String(64), index=True)
    segment: Mapped[dict] = mapped_column(JSON, default=dict)
    stat_date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[float] = mapped_column(Float)
    sample_size: Mapped[int | None] = mapped_column(Integer)
    source_version: Mapped[str | None] = mapped_column(String(64))
    dataset_version: Mapped[str | None] = mapped_column(String(64), index=True)
    quality_flags: Mapped[list] = mapped_column(JSON, default=list)


class MetricImport(UUIDPkMixin, TimestampMixin, Base):
    """导入批次：记录格式校验、口径校验与数据质量检查结果。"""

    __tablename__ = "metric_imports"

    tenant_id: Mapped[str] = mapped_column(String(36), index=True, default="*")
    file_name: Mapped[str] = mapped_column(String(255))
    dataset_version: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default=ImportStatus.PENDING.value)
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_valid: Mapped[int] = mapped_column(Integer, default=0)
    rows_invalid: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(36))
