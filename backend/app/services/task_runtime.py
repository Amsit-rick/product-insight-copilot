"""Business-facing task phases and durable runtime metadata helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaskPhase:
    code: str
    label: str
    order: int


PHASES = {
    "intake": TaskPhase("intake", "理解问题", 10),
    "planning": TaskPhase("planning", "制定计划", 20),
    "fact_gathering": TaskPhase("fact_gathering", "查询事实与知识", 30),
    "evidence_validation": TaskPhase("evidence_validation", "校验证据", 40),
    "resolution": TaskPhase("resolution", "形成处理建议", 50),
    "reflection": TaskPhase("reflection", "复核与修订", 60),
    "human_approval": TaskPhase("human_approval", "等待人工决定", 70),
    "finalization": TaskPhase("finalization", "生成最终结果", 80),
    "completed": TaskPhase("completed", "已完成", 90),
    "unknown": TaskPhase("unknown", "处理中", 0),
}

NODE_PHASES = {
    "validate_request": "intake",
    "load_ticket_context": "intake",
    "classify_intent": "intake",
    "triage_agent": "intake",
    "pass_triage": "intake",
    "create_plan": "planning",
    "product_agent": "fact_gathering",
    "feedback_agent": "fact_gathering",
    "release_agent": "fact_gathering",
    "metric_agent": "fact_gathering",
    "order_fact_agent": "fact_gathering",
    "policy_rag_agent": "fact_gathering",
    "retrieve_more": "fact_gathering",
    "ticket_retrieve_more": "fact_gathering",
    "validate_agent_outputs": "evidence_validation",
    "merge_evidence": "evidence_validation",
    "verify_evidence": "evidence_validation",
    "resolve_conflict": "evidence_validation",
    "decide_action": "resolution",
    "resolution_agent": "resolution",
    "response_agent": "resolution",
    "pass_resolution": "resolution",
    "prepare_ticket_reflection": "reflection",
    "review_agent": "reflection",
    "evaluate_ticket_reflection": "reflection",
    "revision_agent": "reflection",
    "apply_ticket_revision": "reflection",
    "reflect_report": "reflection",
    "revise_report": "reflection",
    "human_approval": "human_approval",
    "apply_post_report_approval": "human_approval",
    "generate_report": "finalization",
    "validate_report": "finalization",
    "finalize_recommendation": "finalization",
    "followup_agent": "finalization",
    "persist_result": "completed",
    "persist_followup": "completed",
    "followup": "completed",
}


def task_phase_for_node(node: str | None) -> TaskPhase:
    return PHASES[NODE_PHASES.get(node or "", "unknown")]


def advance_task_runtime(task, *, current_node: str | None = None) -> None:  # noqa: ANN001
    """Advance phase/checkpoint counters without depending on a concrete ORM class."""
    if current_node is not None:
        same_node = task.current_node == current_node
        task.current_node = current_node
        task.current_phase = task_phase_for_node(current_node).code
        task.phase_attempt = int(task.phase_attempt or 0) + 1 if same_node else 1
        task.checkpoint_version = int(task.checkpoint_version or 0) + 1
    task.state_revision = int(task.state_revision or 0) + 1
