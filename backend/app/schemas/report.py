"""报告与引用契约（设计文档 10 节）。

报告必须区分事实 / 推断 / 假设 / 建议，且每条事实与推断都能定位到证据。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CitationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    statement_id: str | None = None
    statement_type: str
    source_kind: str
    chunk_id: str | None = None
    document_id: str | None = None
    kb_code: str | None = None
    source_name: str | None = None
    page: int | None = None
    document_version: str | None = None
    content: str | None = None
    relevance_score: float | None = None
    metric_ref: dict | None = None
    permission_checked: bool = True


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    tenant_id: str
    version: int = 1
    summary: str | None = None
    impact_scope: list[dict] = Field(default_factory=list)
    facts: list[dict] = Field(default_factory=list)
    inferences: list[dict] = Field(default_factory=list)
    hypotheses: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    conflicts: list[dict] = Field(default_factory=list)
    recommendations: list[dict] = Field(default_factory=list)
    priority: str | None = None
    confidence: float | None = None
    needs_human_review: bool = False
    risk_level: str = "low"
    approval_status: str = "not_required"
    content_md: str | None = None
    # 不确定性与数据缺口：报告必须显式呈现，不然"证据不足"会被摘要掩盖
    data_gaps: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_by: str | None = None
    created_at: datetime
    updated_at: datetime
    citations: list[CitationOut] = Field(default_factory=list)
    citation_marks: dict[str, str] = Field(default_factory=dict)
    validation_problems: list[str] = Field(default_factory=list)
    reflection: dict = Field(default_factory=dict)


class ReportExport(BaseModel):
    report_id: str
    format: str
    content: str
    file_name: str
