"""工作流状态定义（设计文档 7.2 节）。

设计要点：
- **Reducer**：四个分析 Agent 节点是并行执行的，它们只能写带 reducer 的键
  （`agent_results` 合并、`agent_evidence` / `agent_metric_results` 追加），
  否则 LangGraph 会因并发写同一键而报错。
- **覆盖式键**（如 `evidence` / `conflicts` / `risk_level`）：只由串行的汇总节点写入，
  每轮覆盖，避免断点恢复时重复累加。
- 状态中只放可 JSON 序列化的数据，保证 checkpoint 可以落盘、任务可以恢复。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


def merge_dict(left: dict | None, right: dict | None) -> dict:
    """按 agent_code 合并：同一 Agent 的新结果覆盖旧结果。"""
    merged: dict = dict(left or {})
    merged.update(right or {})
    return merged


def add_list(left: list | None, right: list | None) -> list:
    return [*(left or []), *(right or [])]


def add_int(left: int | None, right: int | None) -> int:
    return int(left or 0) + int(right or 0)


class AnalysisState(TypedDict, total=False):
    # ---------------- 身份与输入 ----------------
    task_id: str
    tenant_id: str
    user_id: str
    query: str
    filters: dict
    kb_codes: list[str]
    max_security_rank: int
    # 发起人的角色码（由入口节点从数据库解析后写入，供指标级密级与工具层复用；
    # 绝不由请求体提供——权限只从服务端会话解析）
    roles: list[str]
    force_human_review: bool
    workflow_version: str
    prompt_version: str

    # ---------------- 校验与计划 ----------------
    halt: bool
    halt_reason: str
    injection: dict | None
    intent: str | None
    intent_confidence: float
    intent_reason: str | None
    plan: list[dict]

    # ---------------- 并行 Agent 产出（reducer） ----------------
    agent_results: Annotated[dict[str, dict], merge_dict]
    agent_evidence: Annotated[list[dict], add_list]
    agent_metric_results: Annotated[list[dict], add_list]
    agent_errors: Annotated[list[dict], add_list]

    # ---------------- 汇总（覆盖式） ----------------
    evidence: list[dict]
    metric_results: list[dict]
    statements: list[dict]
    conflicts: list[dict]
    coverage: dict
    validation_problems: list[str]

    # ---------------- 判定与分支 ----------------
    sufficiency: dict
    verification: dict
    risk_level: str
    risk_reasons: list[str]
    needs_human_review: bool
    branch: str
    retrieval_rounds: Annotated[int, add_int]
    conflict_rounds: Annotated[int, add_int]

    # ---------------- 人工审核与报告 ----------------
    approval_status: str
    approval: dict | None
    report_draft: dict | None
    report_validated: bool
    report_problems: list[str]
    final_report: dict | None
    report_id: str | None

    # ---------------- 有界反思与修订 ----------------
    reflection_rounds: int
    max_reflection_rounds: int
    reflection_history: Annotated[dict[str, dict], merge_dict]
    latest_reflection: dict | None
    draft_version: int
    draft_kind: str | None
    revision_history: Annotated[dict[str, dict], merge_dict]
    unresolved_reflection_issues: list[dict]
    reflection_stop_reason: str | None
    protected_decision_snapshot: dict | None
    protected_decision_hash: str | None
    decision_version: int
    review_stage: str | None
    reflection_idempotency_key: str | None

    # ---------------- 错误与告警 ----------------
    errors: Annotated[list[dict], add_list]
    warnings: Annotated[list[str], add_list]

    # ---------------- 轮次统计（用于成本与限流观测） ----------------
    node_visits: Annotated[dict[str, int], merge_dict]


def initial_state(
    *,
    task_id: str,
    tenant_id: str,
    user_id: str,
    query: str,
    filters: dict | None = None,
    kb_codes: list[str] | None = None,
    max_security_rank: int = 1,
    force_human_review: bool = False,
    workflow_version: str = "",
    prompt_version: str = "",
) -> AnalysisState:
    """构造一次工作流运行的初始状态。"""
    return AnalysisState(
        task_id=task_id,
        tenant_id=tenant_id,
        user_id=user_id,
        query=query,
        filters=filters or {},
        kb_codes=list(kb_codes or []),
        max_security_rank=max_security_rank,
        force_human_review=force_human_review,
        workflow_version=workflow_version,
        prompt_version=prompt_version,
        halt=False,
        halt_reason="",
        injection=None,
        intent=None,
        intent_confidence=0.0,
        intent_reason=None,
        plan=[],
        agent_results={},
        agent_evidence=[],
        agent_metric_results=[],
        agent_errors=[],
        evidence=[],
        metric_results=[],
        statements=[],
        conflicts=[],
        coverage={},
        validation_problems=[],
        sufficiency={},
        verification={},
        risk_level="low",
        risk_reasons=[],
        needs_human_review=False,
        branch="",
        approval_status="not_required",
        approval=None,
        report_draft=None,
        report_validated=False,
        report_problems=[],
        final_report=None,
        report_id=None,
        reflection_rounds=0,
        max_reflection_rounds=1,
        reflection_history={},
        latest_reflection=None,
        draft_version=0,
        draft_kind=None,
        revision_history={},
        unresolved_reflection_issues=[],
        reflection_stop_reason=None,
        protected_decision_snapshot=None,
        protected_decision_hash=None,
        decision_version=0,
        review_stage=None,
        reflection_idempotency_key=None,
        errors=[],
        warnings=[],
        node_visits={},
    )


def bump_visits(state: AnalysisState, node: str) -> dict[str, int]:
    """节点访问计数：用于限制循环与成本观测。"""
    visits = dict(state.get("node_visits") or {})
    visits[node] = visits.get(node, 0) + 1
    return visits


def state_digest(state: AnalysisState) -> dict[str, Any]:
    """给人工审核与跨进程恢复用的状态摘要（只保留结论与恢复所需内容）。"""
    return {
        "task_id": state.get("task_id"),
        "tenant_id": state.get("tenant_id"),
        "user_id": state.get("user_id"),
        "query": state.get("query"),
        "filters": state.get("filters") or {},
        "intent": state.get("intent"),
        "risk_level": state.get("risk_level"),
        "risk_reasons": state.get("risk_reasons") or [],
        "sufficiency": state.get("sufficiency") or {},
        "conflicts": state.get("conflicts") or [],
        "coverage": state.get("coverage") or {},
        "evidence": state.get("evidence") or [],
        "statements": state.get("statements") or [],
        "metric_results": state.get("metric_results") or [],
        "agent_results": {
            code: {
                "summary": payload.get("summary"),
                "findings": payload.get("findings"),
                "confidence": payload.get("confidence"),
                "data_gaps": payload.get("data_gaps"),
                "ok": payload.get("ok"),
            }
            for code, payload in (state.get("agent_results") or {}).items()
        },
        "warnings": state.get("warnings") or [],
        "report_draft": state.get("report_draft"),
        "reflection_rounds": int(state.get("reflection_rounds") or 0),
        "max_reflection_rounds": int(state.get("max_reflection_rounds") or 1),
        "reflection_history": state.get("reflection_history") or {},
        "latest_reflection": state.get("latest_reflection"),
        "draft_version": int(state.get("draft_version") or 0),
        "draft_kind": state.get("draft_kind"),
        "revision_history": state.get("revision_history") or {},
        "unresolved_reflection_issues": state.get("unresolved_reflection_issues") or [],
        "reflection_stop_reason": state.get("reflection_stop_reason"),
        "protected_decision_snapshot": state.get("protected_decision_snapshot"),
        "protected_decision_hash": state.get("protected_decision_hash"),
        "decision_version": int(state.get("decision_version") or 0),
        "review_stage": state.get("review_stage"),
        "reflection_idempotency_key": state.get("reflection_idempotency_key"),
        "workflow_version": state.get("workflow_version"),
        "prompt_version": state.get("prompt_version"),
    }
