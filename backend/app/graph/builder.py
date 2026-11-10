"""工作流编排（设计文档 7.1 主图）。

    START
      -> validate_request        （代码：权限 + 参数 + 注入检测）
      -> classify_intent         （规则 + 可选模型复核）
      -> create_plan             （受限执行计划）
      -> product_agent / feedback_agent / release_agent / metric_agent   ← 并行分支
      -> validate_agent_outputs  （代码：Schema 与引用完整性复核）
      -> merge_evidence          （代码：证据编号、引用重映射、冲突检测）
      -> verify_evidence         （代码：充分性 + 风险 + 分支判定）
           |-> retrieve_more     （有界补检索，最多 workflow_max_retrieve_rounds 次）
           |-> resolve_conflict  （按来源优先级裁决，转人工）
           |-> human_approval    （中断等待人工，可恢复）
           |-> generate_report
      -> validate_report         （代码：结构 / 引用 / 类型 / 敏感内容）
      -> persist_result          （报告 + 引用 + 审计）
      -> END

四个 Agent 节点是并行分支，因此只写带 reducer 的状态键；汇总节点写覆盖式键。
Checkpointer 由调用方注入：默认进程内 `InMemorySaver`，可替换为 SQLite Saver 做跨进程恢复。
"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

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
    apply_post_report_approval,
    generate_report,
    human_approval,
    persist_result,
    reflect_report,
    revise_report_node,
    route_after_generate_report,
    route_after_human_approval,
    route_after_reflect_report,
    validate_report,
)
from app.graph.runtime import NodeRuntime
from app.graph.state import AnalysisState

ANALYSIS_BRANCHES = ("product_agent", "feedback_agent", "release_agent", "metric_agent")

NODE_SEQUENCE = (
    "validate_request",
    "classify_intent",
    "create_plan",
    *ANALYSIS_BRANCHES,
    "validate_agent_outputs",
    "merge_evidence",
    "verify_evidence",
    "retrieve_more",
    "resolve_conflict",
    "human_approval",
    "generate_report",
    "reflect_report",
    "revise_report",
    "apply_post_report_approval",
    "validate_report",
    "persist_result",
)


def build_graph(runtime: NodeRuntime, checkpointer=None):
    """编译工作流。节点通过闭包注入运行时依赖，保证节点函数本身保持纯逻辑。"""
    checkpointer = checkpointer or InMemorySaver()

    def wrap(func):
        def node(state: AnalysisState, *args, **kwargs):  # noqa: ANN001, ANN202
            return func(state, runtime=runtime)

        node.__name__ = func.__name__
        return node

    graph = StateGraph(AnalysisState)
    graph.add_node("validate_request", wrap(validate_request))
    graph.add_node("classify_intent", wrap(classify_intent))
    graph.add_node("create_plan", wrap(create_plan))
    graph.add_node("product_agent", wrap(product_agent_node))
    graph.add_node("feedback_agent", wrap(feedback_agent_node))
    graph.add_node("release_agent", wrap(release_agent_node))
    graph.add_node("metric_agent", wrap(metric_agent_node))
    graph.add_node("validate_agent_outputs", wrap(validate_agent_outputs))
    graph.add_node("merge_evidence", wrap(merge_evidence))
    graph.add_node("verify_evidence", wrap(verify_evidence))
    graph.add_node("retrieve_more", wrap(retrieve_more))
    graph.add_node("resolve_conflict", wrap(resolve_conflict))
    graph.add_node("human_approval", wrap(human_approval))
    graph.add_node("generate_report", wrap(generate_report))
    graph.add_node("reflect_report", wrap(reflect_report))
    graph.add_node("revise_report", wrap(revise_report_node))
    graph.add_node("apply_post_report_approval", wrap(apply_post_report_approval))
    graph.add_node("validate_report", wrap(validate_report))
    graph.add_node("persist_result", wrap(persist_result))

    graph.add_edge(START, "validate_request")
    graph.add_conditional_edges(
        "validate_request",
        route_after_validate,
        {"halt": END, "continue": "classify_intent"},
    )
    graph.add_edge("classify_intent", "create_plan")

    # 并行分支：计划完成后四个 Agent 同时执行，再汇合到输出校验
    for branch in ANALYSIS_BRANCHES:
        graph.add_edge("create_plan", branch)
        graph.add_edge(branch, "validate_agent_outputs")

    graph.add_edge("validate_agent_outputs", "merge_evidence")
    graph.add_edge("merge_evidence", "verify_evidence")
    graph.add_conditional_edges(
        "verify_evidence",
        route_after_verify,
        {
            "retrieve_more": "retrieve_more",
            "resolve_conflict": "resolve_conflict",
            "human_approval": "human_approval",
            "generate_report": "generate_report",
        },
    )
    # 补检索后重新合并证据（新证据同样要参与冲突检测与引用编号）
    graph.add_edge("retrieve_more", "merge_evidence")
    # 冲突裁决后回到判定节点：冲突已记录 + 强制人工审核
    graph.add_edge("resolve_conflict", "verify_evidence")
    graph.add_conditional_edges(
        "human_approval",
        route_after_human_approval,
        {
            "generate_report": "generate_report",
            "apply_post_report_approval": "apply_post_report_approval",
        },
    )
    graph.add_conditional_edges(
        "generate_report",
        route_after_generate_report,
        {"reflect_report": "reflect_report", "validate_report": "validate_report"},
    )
    graph.add_conditional_edges(
        "reflect_report",
        route_after_reflect_report,
        {
            "validate_report": "validate_report",
            "revise_report": "revise_report",
            "retrieve_more": "retrieve_more",
            "human_approval": "human_approval",
        },
    )
    graph.add_edge("revise_report", "reflect_report")
    graph.add_edge("apply_post_report_approval", "validate_report")
    graph.add_edge("validate_report", "persist_result")
    graph.add_edge("persist_result", END)

    return graph.compile(checkpointer=checkpointer)


def get_graph(runtime: NodeRuntime, checkpointer=None):
    """编译并返回可执行图。

    传入同一个 checkpointer 才能对同一 thread_id 做中断恢复；
    编译本身开销很小，因此不做跨运行缓存，避免运行时依赖被意外共享。
    """
    return build_graph(runtime, checkpointer=checkpointer)


__all__ = [
    "ANALYSIS_BRANCHES",
    "NODE_SEQUENCE",
    "build_graph",
    "get_graph",
]
