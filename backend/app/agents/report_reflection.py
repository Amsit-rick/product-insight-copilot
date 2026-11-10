"""诊断报告反思与修订 Agent；两者都没有工具权限。"""

from __future__ import annotations

import json

from pydantic import Field

from app.agents.base import AgentOutputModel, BaseAgent, Collected
from app.agents.schemas import AgentInput
from app.schemas.reflection import DiagnosticReportDraft, ReflectionIssue
from app.tools.registry import ToolContext


class ReportReflectionOutput(AgentOutputModel):
    verdict: str = "pass"
    issues: list[ReflectionIssue] = Field(default_factory=list)
    required_changes: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    stop_reason: str | None = None


class ReportRevisionOutput(AgentOutputModel):
    revised_report: DiagnosticReportDraft
    addressed_issue_codes: list[str] = Field(default_factory=list)
    remaining_issue_codes: list[str] = Field(default_factory=list)
    changed_sections: list[str] = Field(default_factory=list)


class ReportReflectionAgent(BaseAgent):
    code = "report_reflection_agent"
    display_name = "报告复核 Agent"
    kb_scope: set[str] = set()
    output_model = ReportReflectionOutput

    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        return Collected()

    def tool_budget(self, ai: AgentInput) -> int:
        return 0

    def user_prompt(self, ai: AgentInput, collected: Collected) -> str:
        return (
            f"<报告草稿>{json.dumps(ai.filters.get('report_draft') or {}, ensure_ascii=False, default=str)}</报告草稿>\n"
            f"<有效证据编号>{json.dumps(ai.filters.get('report_evidence_ids') or [], ensure_ascii=False)}</有效证据编号>\n"
            f"<已知冲突>{json.dumps(ai.filters.get('report_conflicts') or [], ensure_ascii=False, default=str)}</已知冲突>\n"
            "只输出结构化 issues 与 verdict，不输出思维过程，不新增事实。"
        )

    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        from app.services.report_reflection import inspect_report

        result = inspect_report(
            report=ai.filters.get("report_draft") or {},
            evidence_ids=set(ai.filters.get("report_evidence_ids") or []),
            conflicts=list(ai.filters.get("report_conflicts") or []),
        )
        return {
            "summary": f"报告复核结论：{result.verdict}",
            "findings": [],
            **result.model_dump(),
        }


class ReportRevisionAgent(BaseAgent):
    code = "report_revision_agent"
    display_name = "报告修订 Agent"
    kb_scope: set[str] = set()
    output_model = ReportRevisionOutput

    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        return Collected()

    def tool_budget(self, ai: AgentInput) -> int:
        return 0

    def user_prompt(self, ai: AgentInput, collected: Collected) -> str:
        return (
            f"<报告草稿>{json.dumps(ai.filters.get('report_draft') or {}, ensure_ascii=False, default=str)}</报告草稿>\n"
            f"<问题清单>{json.dumps(ai.filters.get('reflection_issues') or [], ensure_ascii=False, default=str)}</问题清单>\n"
            "只能调整报告结构、陈述类型、引用绑定和不确定性表达，不得新增事实。"
        )

    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        from app.services.report_reflection import revise_report

        revision = revise_report(
            report=ai.filters.get("report_draft") or {},
            issues=ai.filters.get("reflection_issues") or [],
            evidence_ids=set(ai.filters.get("report_evidence_ids") or []),
            conflicts=list(ai.filters.get("report_conflicts") or []),
        )
        return {
            "summary": "已按问题清单修订报告草稿",
            "findings": [],
            **revision.model_dump(),
        }


__all__ = [
    "ReportReflectionAgent",
    "ReportReflectionOutput",
    "ReportRevisionAgent",
    "ReportRevisionOutput",
]
