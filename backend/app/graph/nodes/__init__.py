"""工作流节点集合。

按阶段分组，便于对照设计文档 7.1 的主图：
- `intake`：validate_request / classify_intent / create_plan
- `analysis`：四个 Agent 节点 + validate_agent_outputs
- `evidence`：merge_evidence / verify_evidence / retrieve_more / resolve_conflict
- `report`：human_approval / generate_report / validate_report / persist_result
"""

from app.graph.nodes.analysis import (
    feedback_agent_node,
    metric_agent_node,
    product_agent_node,
    release_agent_node,
    validate_agent_outputs,
)
from app.graph.nodes.evidence import (
    merge_evidence,
    resolve_conflict,
    retrieve_more,
    route_after_verify,
    verify_evidence,
)
from app.graph.nodes.intake import (
    classify_intent,
    create_plan,
    route_after_validate,
    validate_request,
)
from app.graph.nodes.report import (
    generate_report,
    human_approval,
    persist_result,
    validate_report,
)

__all__ = [
    "classify_intent",
    "create_plan",
    "feedback_agent_node",
    "generate_report",
    "human_approval",
    "merge_evidence",
    "metric_agent_node",
    "persist_result",
    "product_agent_node",
    "release_agent_node",
    "resolve_conflict",
    "retrieve_more",
    "route_after_validate",
    "route_after_verify",
    "validate_agent_outputs",
    "validate_report",
    "validate_request",
    "verify_evidence",
]
