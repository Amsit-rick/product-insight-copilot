"""指标服务契约（设计文档 8 节）。

对外只暴露固定能力，任何 SQL 都是 MetricService 的实现细节。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class MetricDefinitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    display_name: str
    unit: str
    period: str
    description: str | None = None
    numerator: str | None = None
    denominator: str | None = None
    allowed_dimensions: list[str] = Field(default_factory=list)
    higher_is_better: bool = True
    source_version: str | None = None


class MetricQueryResponse(BaseModel):
    """统一的指标查询返回：行数据 + 汇总 + 数据版本 + 质量标记。"""

    capability: str
    metric_name: str
    params: dict = Field(default_factory=dict)
    rows: list[dict] = Field(default_factory=list)
    summary: dict = Field(default_factory=dict)
    dataset_version: str | None = None
    source_version: str | None = None
    quality_flags: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    available: bool = False
    citations: list[dict] = Field(default_factory=list)


class MetricImportIssue(BaseModel):
    row: int | None = None
    level: str = "error"
    code: str
    message: str
    raw: dict | None = None


class MetricImportResponse(BaseModel):
    import_id: str
    file_name: str
    dataset_version: str
    status: str
    rows_total: int = 0
    rows_valid: int = 0
    rows_invalid: int = 0
    duplicates: int = 0
    missing_days: list[str] = Field(default_factory=list)
    quality_flags: list[str] = Field(default_factory=list)
    issues: list[MetricImportIssue] = Field(default_factory=list)
    metric_names: list[str] = Field(default_factory=list)
    date_range: dict[str, str | None] = Field(default_factory=dict)
    defined_metrics_unknown: list[str] = Field(default_factory=list)


class MetricTrendQuery(BaseModel):
    metric_name: str
    start_date: date
    end_date: date
    platform: str | None = None
    app_version: str | None = None
    channel: str | None = None
    device: str | None = None


class MetricCompareQuery(BaseModel):
    metric_name: str
    baseline_version: str
    target_version: str
    start_date: date
    end_date: date
    platform: str | None = None
    channel: str | None = None
    release_date: date | None = None


class MetricBreakdownQuery(BaseModel):
    metric_name: str
    start_date: date
    end_date: date
    dimension: str = Field(default="platform", pattern="^(platform|channel|device|app_version)$")
    platform: str | None = None
    app_version: str | None = None
    channel: str | None = None
    top_n: int = Field(default=10, ge=1, le=50)


class MetricAnomalyQuery(BaseModel):
    metric_name: str
    start_date: date
    end_date: date
    platform: str | None = None
    app_version: str | None = None
    channel: str | None = None
    method: str = Field(default="zscore", pattern="^(zscore|iqr)$")
    threshold: float = Field(default=2.0, gt=0, le=10)


class MetricCoverageOut(BaseModel):
    metric_name: str
    min_date: date | None = None
    max_date: date | None = None
    rows: int = 0
    detail: dict[str, Any] = Field(default_factory=dict)
