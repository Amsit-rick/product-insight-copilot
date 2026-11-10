"""Agent 层：产品 / 反馈 / 版本研发 / 指标解读 / 报告。

所有 Agent 共享同一个受控骨架（`BaseAgent`）：工具白名单、超时、重试、
Schema 校验、证据引用校验与 `agent_runs` 落库都在骨架里完成。
"""

from app.agents.base import (
    AgentOutputModel,
    AgentTimeout,
    BaseAgent,
    Collected,
    ModelFinding,
    ToolCallDenied,
    make_evidence,
    metric_evidence,
)
from app.agents.after_sales import AFTER_SALES_AGENT_CLASSES, AFTER_SALES_AGENTS
from app.agents.feedback_agent import FeedbackAgent
from app.agents.metric_agent import MetricAgent
from app.agents.product_agent import ProductAgent
from app.agents.release_agent import ReleaseAgent
from app.agents.report_agent import ReportAgent
from app.agents.report_reflection import ReportReflectionAgent, ReportRevisionAgent
from app.agents.schemas import AgentInput, AgentResult, FindingCandidate
from app.models.enums import AgentCode

AGENT_CLASSES: dict[str, type[BaseAgent]] = {
    AgentCode.PRODUCT.value: ProductAgent,
    AgentCode.FEEDBACK.value: FeedbackAgent,
    AgentCode.RELEASE.value: ReleaseAgent,
    AgentCode.METRIC.value: MetricAgent,
    AgentCode.REPORT.value: ReportAgent,
    "report_reflection_agent": ReportReflectionAgent,
    "report_revision_agent": ReportRevisionAgent,
    # 电商售后工单的 7 个 Agent（需求书 §3.2）
    **AFTER_SALES_AGENT_CLASSES,
}

# 并行分析阶段的四个 Agent（报告 Agent 在汇总阶段单独运行）
ANALYSIS_AGENTS: tuple[str, ...] = (
    AgentCode.PRODUCT.value,
    AgentCode.FEEDBACK.value,
    AgentCode.RELEASE.value,
    AgentCode.METRIC.value,
)

# 售后工单的 7 个 Agent 由 AFTER_SALES_AGENTS 导出（见 after_sales.py）


def build_agent(code: str) -> BaseAgent:
    cls = AGENT_CLASSES.get(code)
    if cls is None:
        raise KeyError(f"未知 Agent: {code}")
    return cls()


def agent_catalog() -> list[dict]:
    """给前端/文档用的能力清单：职责 + 可访问知识库 + 可用工具。"""
    from app.services.policy_engine import AGENT_KBS, AGENT_TOOLS

    catalog = []
    for code, cls in AGENT_CLASSES.items():
        catalog.append(
            {
                "code": code,
                "display_name": cls.display_name,
                "knowledge_bases": sorted(AGENT_KBS.get(code, set())),
                "tools": sorted(AGENT_TOOLS.get(code, set())),
                "output_schema": cls.output_model.__name__,
            }
        )
    return catalog


__all__ = [
    "AFTER_SALES_AGENTS",
    "AGENT_CLASSES",
    "ANALYSIS_AGENTS",
    "AgentInput",
    "AgentOutputModel",
    "AgentResult",
    "AgentTimeout",
    "BaseAgent",
    "Collected",
    "FeedbackAgent",
    "FindingCandidate",
    "MetricAgent",
    "ModelFinding",
    "ProductAgent",
    "ReleaseAgent",
    "ReportAgent",
    "ReportReflectionAgent",
    "ReportRevisionAgent",
    "ToolCallDenied",
    "agent_catalog",
    "build_agent",
    "make_evidence",
    "metric_evidence",
]
