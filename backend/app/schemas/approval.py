"""人工审核契约（设计文档 4.2 / 7.1 节：高风险结论必须人工确认）。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    report_id: str | None = None
    status: str
    action: str | None = None
    reviewer_id: str | None = None
    comment: str | None = None
    diff: dict | None = None
    decided_at: datetime | None = None
    created_at: datetime
    task_state_revision: int | None = None


class ApprovalDecision(BaseModel):
    """审核决定。

    - approve：按原样放行，报告标记 approved；
    - reject：驳回，任务结束为 completed 但报告标记 rejected（保留留痕，不删除证据）；
    - modify：人工修改报告字段后放行，diff 进入审计日志。
    """

    action: str = Field(pattern="^(approve|reject|modify)$")
    comment: str | None = Field(default=None, max_length=2000)
    modifications: dict | None = None
