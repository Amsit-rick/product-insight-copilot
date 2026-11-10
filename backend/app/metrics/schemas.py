"""指标服务的请求与结果契约。

设计文档 8.3 节：Agent 只能通过这四个受控能力取数，不得生成 SQL。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class TrendRequest(BaseModel):
    metric_name: str
    start_date: date
    end_date: date
    platform: str | None = None
    app_version: str | None = None
    channel: str | None = None
    device: str | None = None
    dataset_version: str | None = None


class VersionCompareRequest(BaseModel):
    metric_name: str
    baseline_version: str
    target_version: str
    start_date: date
    end_date: date
    platform: str | None = None
    channel: str | None = None
    # 发布日：用于把时间窗切分为发布前后
    release_date: date | None = None


class BreakdownRequest(BaseModel):
    metric_name: str
    dimension: str = Field(default="platform", pattern="^(platform|channel|device|app_version)$")
    start_date: date
    end_date: date
    platform: str | None = None
    app_version: str | None = None
    channel: str | None = None
    top_n: int = 10


class AnomalyRequest(BaseModel):
    metric_name: str
    start_date: date
    end_date: date
    platform: str | None = None
    app_version: str | None = None
    channel: str | None = None
    method: str = Field(default="zscore", pattern="^(zscore|iqr)$")
    threshold: float = 2.0


@dataclass
class MetricResult:
    """统一返回结构：可序列化、带数据版本与质量标记。"""

    capability: str
    metric_name: str
    params: dict
    rows: list[dict]
    summary: dict
    dataset_version: str | None = None
    source_version: str | None = None
    quality_flags: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return bool(self.rows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "metric_name": self.metric_name,
            "params": self.params,
            "rows": self.rows,
            "summary": self.summary,
            "dataset_version": self.dataset_version,
            "source_version": self.source_version,
            "quality_flags": self.quality_flags,
            "warnings": self.warnings,
            "available": self.available,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "MetricResult":
        """从缓存载荷重建（缓存里存的就是 as_dict() 的结果，往返不丢信息）。"""
        return cls(
            capability=payload.get("capability", ""),
            metric_name=payload.get("metric_name", ""),
            params=payload.get("params") or {},
            rows=payload.get("rows") or [],
            summary=payload.get("summary") or {},
            dataset_version=payload.get("dataset_version"),
            source_version=payload.get("source_version"),
            quality_flags=payload.get("quality_flags") or [],
            warnings=payload.get("warnings") or [],
        )
