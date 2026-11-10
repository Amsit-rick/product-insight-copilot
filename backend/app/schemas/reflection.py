"""反思与修订的强类型契约。

这里只保存结构化问题、修订差异和停止原因，不接收或持久化模型思维链。
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Severity = Literal["low", "medium", "high", "critical"]
ReflectionVerdict = Literal["pass", "revise", "retrieve_more", "human_review"]

_PROTECTED_CHANGE_PATTERNS = (
    r"\b(issue_type|action_type|amount|amount_source|risk_tier|approval_requirement|policy_refs)\b",
    r"(问题类型|处理动作|退款金额|赔付金额|金额来源|风险等级|审批要求|政策引用).{0,12}(修改|改为|调整|替换|覆盖)",
    r"(修改|改为|调整|替换|覆盖).{0,12}(问题类型|处理动作|退款金额|赔付金额|金额来源|风险等级|审批要求|政策引用)",
)


def _requests_protected_change(text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in _PROTECTED_CHANGE_PATTERNS)


class ReflectionIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=80)
    severity: Severity
    category: Literal[
        "evidence",
        "policy",
        "amount",
        "action",
        "approval",
        "citation",
        "consistency",
        "reply",
        "sensitive_data",
    ]
    target: Literal["ticket_reply", "diagnostic_report"]
    source: Literal["code", "model"]
    rule_code: str | None = Field(default=None, max_length=80)
    message: str = Field(min_length=1, max_length=800)
    evidence_refs: list[str] = Field(default_factory=list)
    auto_fixable: bool = False

    @model_validator(mode="after")
    def validate_source_contract(self):
        if self.source == "code" and not self.rule_code:
            raise ValueError("代码门禁问题必须提供 rule_code")
        return self


class ReflectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: ReflectionVerdict
    issues: list[ReflectionIssue] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    stop_reason: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_verdict_contract(self):
        severities = {item.severity for item in self.issues}
        if self.verdict == "pass" and severities.intersection({"high", "critical"}):
            raise ValueError("pass 不能包含 high/critical 问题")
        if self.verdict == "revise" and not any(item.auto_fixable for item in self.issues):
            raise ValueError("revise 至少需要一个可自动修复问题")
        if self.verdict == "retrieve_more" and not self.missing_evidence:
            raise ValueError("retrieve_more 必须列出 missing_evidence")
        if any(item.severity == "critical" for item in self.issues) and self.verdict != "human_review":
            raise ValueError("critical 问题必须转人工")
        protected_requests = [item for item in self.required_changes if _requests_protected_change(item)]
        if protected_requests:
            raise ValueError("required_changes 不得要求修改受保护业务字段")
        return self


class TicketReplyRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revised_text: str = Field(min_length=1, max_length=12000)
    addressed_issue_codes: list[str]
    remaining_issue_codes: list[str] = Field(default_factory=list)
    changed_sections: list[str] = Field(default_factory=list)


class DiagnosticReportDraft(BaseModel):
    """报告修订的结构化载体；允许保留当前报告的扩展展示字段。"""

    model_config = ConfigDict(extra="allow")

    summary: str = ""
    facts: list[dict[str, Any]] = Field(default_factory=list)
    inferences: list[dict[str, Any]] = Field(default_factory=list)
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    priority: Literal["P0", "P1", "P2", "P3"] = "P2"
    confidence: float = Field(default=0.6, ge=0, le=1)


class ReportRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revised_report: DiagnosticReportDraft
    addressed_issue_codes: list[str]
    remaining_issue_codes: list[str] = Field(default_factory=list)
    changed_sections: list[str] = Field(default_factory=list)


__all__ = [
    "DiagnosticReportDraft",
    "ReflectionIssue",
    "ReflectionResult",
    "ReflectionVerdict",
    "ReportRevision",
    "TicketReplyRevision",
]
