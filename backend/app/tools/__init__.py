"""受控工具层（设计文档 7.3 / 7.4 节）。

Agent 不能直接访问数据库或任意 HTTP：只能通过注册表暴露的工具取数，
并且每次调用都要经过三道闸门：

1. 白名单：该 Agent 是否被允许使用这个工具（`PolicyEngine.AGENT_TOOLS`）；
2. 参数校验：知识库是否在该 Agent 与当前用户的可见交集内、指标能力是否合法；
3. 预算与审计：单次 Agent 运行的调用次数上限 + 全量落 `tool_calls` 审计表。

任何一道不通过都不会执行工具，`ToolOutcome.policy_decision` 记录为 `deny`。
"""

from app.tools.metric_tool import MetricQueryTool, metric_query
from app.tools.registry import (
    ToolBudgetExceeded,
    ToolContext,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
    get_tool_registry,
    reset_tool_registry,
)
from app.tools.retrieval_tool import RetrievalSearchTool, retrieval_search

__all__ = [
    "MetricQueryTool",
    "RetrievalSearchTool",
    "ToolBudgetExceeded",
    "ToolContext",
    "ToolOutcome",
    "ToolRegistry",
    "ToolSpec",
    "get_tool_registry",
    "metric_query",
    "reset_tool_registry",
    "retrieval_search",
]
