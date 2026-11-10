"""售后工单工作流（需求书 §3.2 的 7 个 Agent 拓扑）。

拓扑与并行理由：

    START → load_ticket_context            （装载工单与已核验事实）
          → triage                          （分类：后续所有检索都依赖它）
          → order_fact ∥ policy_rag         （互不依赖 ⇒ 并行，省一半等待）
          → resolution                      （需要事实 + 政策）
          → response ∥ review               （草稿与质检互不依赖 ⇒ 并行）
          → finalize_recommendation         （代码：规则判风险 + 落库 + 状态迁移）
          → followup                        （跟进计划，不阻塞）
          → END

与诊断工作流的关系：**两套拓扑共用同一个运行器**（`WorkflowRunner` 按 `filters["workflow"]`
选图），因此队列、事件流、`agent_runs` 轨迹、成本核算、审计、断点恢复全部复用，
不需要为售后再造一套执行栈。
"""

from __future__ import annotations

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.graph.nodes.after_sales import (
    apply_ticket_revision,
    analysis_agent_node,
    decide_action_node,
    evaluate_ticket_reflection,
    finalize_recommendation,
    followup,
    load_ticket_context,
    pass_resolution,
    pass_triage,
    prepare_ticket_reflection,
    route_after_decision,
    route_after_finalize,
    route_after_order_fact,
    route_after_resolution,
    route_after_response,
    route_after_review,
    route_after_ticket_reflection,
    route_after_triage,
    ticket_retrieve_more,
)
from app.graph.runtime import NodeRuntime
from app.graph.state import AnalysisState

# 节点名与 Agent 代码一致，便于前端按名字渲染"哪一步在跑"
NODE_SEQUENCE = (
    "load_ticket_context",
    "triage_agent",
    "pass_triage",
    "order_fact_agent",
    "policy_rag_agent",
    "decide_action",
    "resolution_agent",
    "pass_resolution",
    "response_agent",
    "review_agent",
    "evaluate_ticket_reflection",
    "revision_agent",
    "apply_ticket_revision",
    "ticket_retrieve_more",
    "finalize_recommendation",
    "followup_agent",
    "persist_followup",
)

PARALLEL_AFTER_TRIAGE = ("order_fact_agent", "policy_rag_agent")
PARALLEL_AFTER_RESOLUTION = ("response_agent", "review_agent")


def build_after_sales_graph(runtime: NodeRuntime, checkpointer=None):
    """编译售后工单工作流。"""
    checkpointer = checkpointer or InMemorySaver()

    def wrap(func):
        def node(state: AnalysisState, *args, **kwargs):  # noqa: ANN001, ANN202
            return func(state, runtime=runtime)

        node.__name__ = func.__name__
        return node

    graph = StateGraph(AnalysisState)
    graph.add_node("load_ticket_context", wrap(load_ticket_context))
    for code in (
        "triage_agent",
        "order_fact_agent",
        "policy_rag_agent",
        "resolution_agent",
        "response_agent",
        "review_agent",
        "revision_agent",
        "followup_agent",
    ):
        graph.add_node(code, wrap(analysis_agent_node(code)))
    # 代码节点：把上游 Agent 的结论注入 filters（Agent 之间不直接耦合）
    graph.add_node("pass_triage", wrap(pass_triage))
    # 规则决策节点：受控字段（动作/金额/问题类型）在这里定，模型不得改写
    graph.add_node("decide_action", wrap(decide_action_node))
    graph.add_node("pass_resolution", wrap(lambda state, *, runtime: pass_resolution(state)))
    graph.add_node("prepare_ticket_reflection", wrap(prepare_ticket_reflection))
    graph.add_node("evaluate_ticket_reflection", wrap(evaluate_ticket_reflection))
    graph.add_node("apply_ticket_revision", wrap(apply_ticket_revision))
    graph.add_node("ticket_retrieve_more", wrap(ticket_retrieve_more))
    graph.add_node("finalize_recommendation", wrap(finalize_recommendation))
    graph.add_node("persist_followup", wrap(followup))

    graph.add_edge(START, "load_ticket_context")
    graph.add_edge("load_ticket_context", "triage_agent")
    # 分类结果先注入 filters，事实查询与政策检索才能用上问题类型
    graph.add_edge("triage_agent", "pass_triage")
    # 高风险：事实与政策并行取证。低风险：单链先查订单事实，再查适用政策。
    graph.add_conditional_edges("pass_triage", route_after_triage)
    graph.add_conditional_edges("order_fact_agent", route_after_order_fact)
    graph.add_edge("policy_rag_agent", "decide_action")
    # 方案 Agent 在规则决策之后运行：它只负责把动作说成人话（含 rationale）
    graph.add_conditional_edges("decide_action", route_after_decision)
    graph.add_edge("resolution_agent", "pass_resolution")
    # 关闭反思时保持旧路径；开启后必须先生成草稿，再让质检读取草稿。
    graph.add_conditional_edges("pass_resolution", route_after_resolution)
    graph.add_conditional_edges("response_agent", route_after_response)
    graph.add_edge("prepare_ticket_reflection", "review_agent")
    graph.add_conditional_edges("review_agent", route_after_review)
    graph.add_conditional_edges("evaluate_ticket_reflection", route_after_ticket_reflection)
    graph.add_edge("revision_agent", "apply_ticket_revision")
    graph.add_edge("apply_ticket_revision", "review_agent")
    graph.add_edge("ticket_retrieve_more", "policy_rag_agent")
    graph.add_conditional_edges("finalize_recommendation", route_after_finalize)
    # FollowUpAgent 产出跟进计划，再由代码节点落库（不 sleep、不阻塞）
    graph.add_edge("followup_agent", "persist_followup")
    graph.add_edge("persist_followup", END)

    return graph.compile(checkpointer=checkpointer)


__all__ = ["NODE_SEQUENCE", "PARALLEL_AFTER_RESOLUTION", "PARALLEL_AFTER_TRIAGE", "build_after_sales_graph"]
