"""售后工单工作流的专用节点（需求书 §2.2 / §3.2）。

图结构：

    START
      -> load_ticket_context      （代码：装载工单与事实快照，注入 fact_digest）
      -> triage                   （Agent 1：问题分类）
      -> L1/L2: order_fact → policy_rag → decide_action → review → finalize
      -> L3/L4: order_fact ∥ policy_rag → resolution → response ∥ review
      -> finalize_recommendation  （代码：规则汇总 + 落库建议 + 工单状态迁移）
      -> L1/L2: 规则模板回复与跟进；L3/L4: Follow-up Agent
      -> END

三个"代码节点"承担需求书里绝不能交给模型的判断：

- `load_ticket_context`：把工单与已核验事实装进状态。**事实来自数据库快照，不是模型记忆**，
  并显式带上 `is_mock`，所以界面与审计都能看出这是模拟数据（§1 数据说明）。
- `finalize_recommendation`：风险等级与审批要求由 `ticket_risk` 规则给（模型只能建议），
  金额只允许引用上游 `amount` 字段；写库走 `TicketService`（审计 + 状态机单点）。
- `followup`：只组装提醒内容；**真正的定时触发由队列/worker 的定时任务承担**，
  节点本身不 sleep、不阻塞（否则会把一次分析拖成小时级）。
"""

from __future__ import annotations

import re
from typing import Any

from app.graph.nodes.analysis import agent_node
from app.graph.runtime import NodeRuntime
from app.graph.state import AnalysisState
from app.logging_utils import get_logger
from app.models.enums import ApprovalRequirement, MessageSender, RecommendationStatus, TicketStatus
from app.services.ticket_risk import assess, requires_human_approval
from app.services.ticket_service import TicketService

logger = get_logger(__name__)

# 7 个 Agent 的节点名（与 AgentCode 一致，便于前端按名字渲染步骤）
TRIAGE_NODES = ("triage_agent", "order_fact_agent", "policy_rag_agent")
RESOLUTION_NODE = "resolution_agent"
FINAL_NODES = ("response_agent", "review_agent")
FOLLOWUP_NODE = "followup_agent"


def _result_of(state: AnalysisState, code: str) -> dict:
    return ((state.get("agent_results") or {}).get(code) or {})


def _agent_payload(state: AnalysisState, code: str) -> dict:
    """取 Agent 的结构化输出。

    `AgentResult.as_state()` 把 Schema 校验后的字段放在 `blocks` 里（除 summary/findings/data_gaps），
    所以这里读 `blocks` —— 踩过坑：读 `payload`/`output` 都取不到值，导致方案永远退化成默认动作。
    """
    result = _result_of(state, code)
    return result.get("blocks") or {}


def load_ticket_context(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """装载工单与事实快照到 state（模型看到的事实只能来自这里）。"""
    with runtime.node_session(state, "load_ticket_context") as ctx:
        ctx.emit("装载工单上下文", {"ticket_id": (state.get("filters") or {}).get("ticket_id")})
        ticket_id = (state.get("filters") or {}).get("ticket_id")
        service = TicketService(ctx.session)
        if not ticket_id:
            return {"filters": {}, "errors": [{"code": "missing_ticket", "message": "缺少工单 ID"}]}
        try:
            ticket = service.get(ticket_id)
        except Exception as exc:  # noqa: BLE001
            return {
                "halt": True,
                "halt_reason": f"工单不存在或不可访问: {exc}",
                "errors": [{"code": "ticket_not_found", "message": str(exc)}],
            }

        facts = service.facts(ticket.id)
        digest: dict[str, Any] = {}
        is_mock = True
        for fact in facts:
            is_mock = is_mock and bool(fact.is_mock)
            for key, value in (fact.payload or {}).items():
                digest.setdefault(key, value)
        # 从事实里挑出方案生成必需的字段（显式列出，避免模型从一堆键里乱猜）
        fact_digest = {
            "order_status": digest.get("order_status"),
            "order_amount": digest.get("order_amount"),
            "paid_amount": digest.get("paid_amount"),
            "refundable_amount": digest.get("refundable_amount"),
            "carrier": digest.get("carrier"),
            "latest_track": digest.get("latest_track"),
            "last_track_at": digest.get("last_track_at"),
            "signed_at": digest.get("signed_at"),
            "stalled_hours": _to_int(digest.get("stalled_hours")),
            "package_weight": digest.get("package_weight"),
            "promotion_result": digest.get("promotion_result"),
            "promotion_threshold": digest.get("promotion_threshold"),
            "eligible_amount": digest.get("eligible_amount"),
            "excluded_items": digest.get("excluded_items"),
            "aftersale_history": digest.get("aftersale_history"),
            "is_mock": is_mock and bool(facts),
            "fact_count": len(facts),
        }
        filters = dict(state.get("filters") or {})
        filters.update(
            {
                "fact_digest": fact_digest,
                "order_id": ticket.order_id,
                "customer_id": ticket.customer_id,
                "issue_type": ticket.issue_type,
                "sentiment": ticket.sentiment,
            }
        )
        from app.services.reflection_release import release_state_for_rollout
        from app.services.reflection_rollout import (
            ReflectionRolloutRequest,
            decide_reflection_rollout,
        )

        release_state = None
        if runtime.settings.reflection_enabled and runtime.settings.reflection_ticket_enabled:
            release_state = release_state_for_rollout(
                ctx.session, str(state.get("tenant_id") or ticket.tenant_id), runtime.settings
            )
        rollout = decide_reflection_rollout(
            ReflectionRolloutRequest(
                tenant_id=str(state.get("tenant_id") or ticket.tenant_id),
                task_id=str(state.get("task_id") or ""),
                target="ticket_reply",
                risk_level=ticket.risk_tier or state.get("risk_level"),
                issue_type=ticket.issue_type or filters.get("issue_type"),
            ),
            settings=runtime.settings,
            release_state=release_state,
        )
        # 服务端判定覆盖请求侧输入；灰度和轮数都不能由调用方强开或放大。
        filters["reflection_enabled"] = rollout.enabled
        filters["reflection_rollout"] = rollout.as_dict()
        filters["max_reflection_rounds"] = int(runtime.settings.max_reflection_rounds)
        ctx.emit(
            "反思灰度判定完成",
            {
                "target": rollout.target,
                "enabled": rollout.enabled,
                "reason": rollout.reason,
                "bucket": rollout.bucket,
                "percent": rollout.percent,
                "release_status": rollout.release_status,
            },
        )
        # 给 Agent 节点写"执行计划"：Agent 的入参 kb_codes 来自 plan_item ∩ state.kb_codes，
        # 没有计划就一个库都拿不到（政策检索会静默返回空）。这里为 7 个 Agent 显式声明范围，
        # 与 PolicyEngine.AGENT_KBS 保持一致 —— 越权的部分仍会被策略层拒绝。
        plan = [
            {"agent": "triage_agent", "enabled": True, "knowledge_bases": [], "goal": "问题分类"},
            {
                "agent": "order_fact_agent",
                "enabled": True,
                "knowledge_bases": [],
                "goal": "只读查询订单/支付/物流/售后事实",
            },
            {
                "agent": "policy_rag_agent",
                "enabled": True,
                "knowledge_bases": ["policy_kb"],
                "goal": "检索适用政策条款",
            },
            {
                "agent": "resolution_agent",
                "enabled": True,
                "knowledge_bases": ["policy_kb"],
                "goal": "结合事实与政策给出处理动作",
            },
            {
                "agent": "response_agent",
                "enabled": True,
                "knowledge_bases": ["policy_kb"],
                "goal": "生成回复草稿与内部备注",
            },
            {"agent": "review_agent", "enabled": True, "knowledge_bases": [], "goal": "质检与风险分级"},
            {
                "agent": "revision_agent",
                "enabled": bool(filters.get("reflection_enabled")),
                "knowledge_bases": [],
                "goal": "只修订客户回复表达，不修改受保护业务决策",
            },
            {"agent": "followup_agent", "enabled": True, "knowledge_bases": [], "goal": "跟进计划"},
        ]
        return {"filters": filters, "plan": plan, "warnings": []}


def _to_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_missing_information(decision: dict, resolution: dict) -> list[str]:
    """规则结果优先；只有规则未提供该字段时才采用模型结果。"""
    if "missing_information" in decision:
        return [str(item) for item in (decision.get("missing_information") or [])]
    return [str(item) for item in (resolution.get("missing_information") or [])]


def reconcile_review_warnings(
    warnings: list[str], *, policy_count: int, fact_digest: dict, action_type: str | None = None
) -> list[str]:
    """剔除与已核验事实或政策命中状态直接矛盾的模型质检警告。"""
    has_paid_amount = bool(fact_digest.get("paid_amount"))
    has_logistics_status = bool(fact_digest.get("latest_track"))
    has_track_time = bool(fact_digest.get("signed_at") or fact_digest.get("last_track_at"))
    has_facts = bool(fact_digest.get("fact_count")) or any(
        value not in (None, "", [], {})
        for key, value in fact_digest.items()
        if key not in {"is_mock", "fact_count"}
    )
    result: list[str] = []
    for raw in warnings:
        warning = str(raw).strip()
        if not warning:
            continue
        if "followup 字段" in warning:
            continue
        if warning.startswith(("模型分类为", "模型建议动作", "模型判定")):
            warning = "系统识别结果存在分歧，已按订单事实和业务规则处理，请主管复核。"
        if policy_count > 0 and any(
            phrase in warning
            for phrase in (
                "未命中任何政策",
                "未命中适用政策",
                "证据覆盖为 0",
                "证据覆盖为0",
                "无任何政策",
                "无政策条款",
                "既无政策",
            )
        ):
            continue
        if (policy_count > 0 or has_facts) and (
            "证据为空" in warning
            or "无证据支撑" in warning
            or re.search(r"政策命中数\s*0", warning)
        ):
            continue
        if has_facts and any(
            phrase in warning
            for phrase in ("无任何政策或事实证据", "无订单事实", "既无政策也无订单事实")
        ):
            continue
        action_match = re.search(
            r"(?:动作\s*[（(]?\s*|等待外部处理[（(])"
            r"(refund|return_goods|exchange|reship|compensate|coupon|ask_customer|contact_logistics|reply_only)",
            warning,
        )
        if (
            action_type
            and action_match
            and action_match.group(1) != action_type
            and ("未闭环" in warning or "等待外部处理" in warning)
        ):
            continue
        # 对“缺少必要信息：A、B”逐项核销，不能因为 A 已存在就把仍缺失的 B 一起删掉。
        if "：" in warning and warning.startswith("缺少必要信息"):
            prefix, raw_items = warning.split("：", 1)
            items = [item.strip() for item in re.split(r"[、，,；;]", raw_items) if item.strip()]
            remaining = []
            for item in items:
                satisfied = (
                    (has_paid_amount and "实付金额" in item)
                    or (
                        has_logistics_status
                        and "物流" in item
                        and ("状态" in item or "轨迹" in item)
                        and "时间" not in item
                    )
                    or (
                        has_track_time
                        and ("最后一次有效轨迹时间" in item or "签收时间" in item)
                    )
                )
                if not satisfied:
                    remaining.append(item)
            if not remaining:
                continue
            warning = f"{prefix}：{'、'.join(remaining)}"
        elif (
            has_paid_amount
            and policy_count > 0
            and ("无支付事实" in warning or "缺少支付事实" in warning)
            and "政策金额" in warning
        ):
            continue
        elif (
            has_paid_amount
            and policy_count > 0
            and ("无支付事实" in warning or "缺少支付事实" in warning)
            and ("政策条款" in warning or "政策金额" in warning)
        ):
            continue
        elif has_paid_amount and "无支付事实" in warning:
            warning = warning.replace("无支付事实与", "").replace("无支付事实", "").strip("：，、 ")
            if not warning:
                continue
        elif has_paid_amount and "实付金额" in warning and "缺" in warning:
            continue
        elif (
            has_logistics_status
            and "物流" in warning
            and ("状态" in warning or "轨迹" in warning)
            and "时间" not in warning
            and ("缺" in warning or "无法" in warning)
        ):
            continue
        elif has_track_time and (
            "最后一次有效轨迹时间" in warning or "签收时间" in warning
        ) and ("缺" in warning or "无法" in warning):
            continue
        if warning not in result:
            result.append(warning)
    return result


def build_fact_digest_filters(state: AnalysisState) -> dict:
    """把上游 Agent 的结论注入 filters，供下游 Agent 使用。

    为什么走 filters 而不是新状态键：Agent 的入参契约是 `AgentInput(filters=...)`，
    这样 7 个 Agent 全部复用同一套骨架（重试/权限/Schema/agent_runs），不需要为
    "Agent 之间传参"再开一条通道。
    """
    return {}


def decide_action_node(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """**规则决策节点**：在政策检索之后、方案 Agent 之前算出受控决策。

    存在的唯一理由：模型会把受控字段改坏。实测（真实模型下）它把 `action_type`
    从 `refund` 润色成 `ask_customer`、把 `issue_type` 写成中文标签，
    结果风险等级从 L3 掉到 L2 —— 本该主管审批的退款变成可自动发送的草稿。

    所以这里用 `decide_action()` 纯规则算出 action/amount/issue_type，
    写进 `filters["decision"]`；后续 Agent 与落库节点只读它，模型只能润色文字。
    """
    from app.agents.after_sales import classify_issue, decide_action

    with runtime.node_session(state, "decide_action") as ctx:
        filters = dict(state.get("filters") or {})
        facts = filters.get("fact_digest") or {}
        query = state.get("query") or ""

        # 问题类型同样由规则定：模型的分类只作为参考写进 warnings
        issue_type = classify_issue(query)
        policies = _agent_payload(state, "policy_rag_agent").get("policies") or []
        policy_text = " ".join(str(item.get("snippet") or "") for item in policies)
        decision = decide_action(
            query=query, issue_type=issue_type, facts=facts, policy_text=policy_text
        )
        filters["decision"] = decision
        filters["issue_type"] = issue_type
        filters["policy_count"] = len(policies)
        filters["conflict"] = bool(filters.get("conflict")) or bool(decision.get("conflict"))

        rule_risk = assess(
            action_type=decision.get("action_type") or "reply_only",
            amount=_to_float(decision.get("amount")) or 0,
            flags=list(filters.get("risk_flags") or []),
            conflict=bool(filters.get("conflict")),
            sensitive_customer=bool(filters.get("sensitive_customer")),
        )
        was_multi_agent = bool(filters.get("multi_agent"))
        multi_agent = was_multi_agent or rule_risk.tier not in ("L1", "L2")
        filters["multi_agent"] = multi_agent
        from app.models.workflow import AnalysisTask
        from app.skills.after_sales import select_business_skills

        skills = select_business_skills(issue_type, multi_agent)
        filters["business_skills"] = skills
        task = ctx.session.get(AnalysisTask, state.get("task_id"))
        if task is not None:
            task_filters = dict(task.filters or {})
            task_filters.update({"multi_agent": multi_agent, "business_skills": skills})
            task.filters = task_filters
            ctx.session.commit()
        if multi_agent and not was_multi_agent:
            # 创建时的风险标签可能低估正文中的退款/补偿诉求；规则判定升级后同步运行模式。
            ctx.emit(
                f"规则风险升级至 {rule_risk.tier}，切换完整多 Agent 复核",
                {"risk_tier": rule_risk.tier, "multi_agent": True},
                level="warn",
            )

        model_issue = _agent_payload(state, "triage_agent").get("issue_type")
        warnings: list[str] = []
        if model_issue and model_issue != issue_type:
            warnings.append(f"模型分类为 {model_issue}，规则分类为 {issue_type}，以规则为准")

        ctx.emit(
            f"规则决策：{decision['action_type']}"
            + (f" / 金额 {decision['amount']}" if decision["amount"] not in (None, "0") else ""),
            {
                "issue_type": issue_type,
                "amount_source": decision["amount_source"],
                "missing": decision["missing_information"],
            },
            level="warn" if decision["missing_information"] else "info",
        )
        return {"filters": filters, "warnings": warnings}


def _uses_multi_agent(state: AnalysisState) -> bool:
    return bool((state.get("filters") or {}).get("multi_agent"))


def _reflection_enabled(state: AnalysisState) -> bool:
    return bool((state.get("filters") or {}).get("reflection_enabled"))


def route_after_triage(state: AnalysisState) -> list[str]:
    """高风险并行取证；候选低风险先串行核实事实与政策。"""
    if _uses_multi_agent(state):
        return ["order_fact_agent", "policy_rag_agent"]
    return ["order_fact_agent"]


def route_after_order_fact(state: AnalysisState) -> str:
    return "decide_action" if _uses_multi_agent(state) else "policy_rag_agent"


def route_after_decision(state: AnalysisState) -> str:
    return "resolution_agent" if _uses_multi_agent(state) else "pass_resolution"


def route_after_resolution(state: AnalysisState) -> list[str]:
    if _reflection_enabled(state):
        # 反思必须读取已经生成的回复；禁止再与 response_agent 并行。
        return ["response_agent"]
    if _uses_multi_agent(state):
        return ["response_agent", "review_agent"]
    return ["review_agent"]


def route_after_response(state: AnalysisState) -> str:
    return "prepare_ticket_reflection" if _reflection_enabled(state) else "finalize_recommendation"


def route_after_review(state: AnalysisState) -> str:
    return "evaluate_ticket_reflection" if _reflection_enabled(state) else "finalize_recommendation"


def route_after_ticket_reflection(state: AnalysisState) -> str:
    verdict = (state.get("latest_reflection") or {}).get("verdict") or "human_review"
    return {
        "pass": "finalize_recommendation",
        "revise": "revision_agent",
        "retrieve_more": "ticket_retrieve_more",
        "human_review": "finalize_recommendation",
    }.get(verdict, "finalize_recommendation")


def route_after_finalize(state: AnalysisState) -> str:
    return "followup_agent" if _uses_multi_agent(state) else "persist_followup"


def pass_triage(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """核准确定性问题类型和业务 Skill，再持久化给后续 Agent。"""
    from app.agents.after_sales import classify_issue
    from app.models.workflow import AnalysisTask
    from app.skills.after_sales import select_business_skills

    triage = _agent_payload(state, "triage_agent")
    filters = dict(state.get("filters") or {})
    issue_type = classify_issue(state.get("query") or "")
    skills = select_business_skills(issue_type, bool(filters.get("multi_agent")))
    filters.update(
        {
            "issue_type": issue_type,
            "business_skills": skills,
            "sentiment": triage.get("sentiment") or filters.get("sentiment"),
            "urgency": triage.get("urgency"),
            "missing_fields": triage.get("missing_fields") or [],
        }
    )
    with runtime.node_session(state, "pass_triage") as ctx:
        task = ctx.session.get(AnalysisTask, state.get("task_id"))
        if task is not None:
            task_filters = dict(task.filters or {})
            task_filters.update({"issue_type": issue_type, "business_skills": skills})
            task.filters = task_filters
            ctx.session.commit()
        ctx.emit("已按问题类型加载对应的业务核验清单", {"skill_count": len(skills)})
    return {"filters": filters}


def pass_resolution(state: AnalysisState) -> dict:
    """把**规则决策**（不是模型输出）注入 filters，供下游 response/review 使用。"""
    filters = dict(state.get("filters") or {})
    decision = filters.get("decision") or {}
    model_output = _agent_payload(state, RESOLUTION_NODE)
    filters["resolution"] = {
        "action_type": decision.get("action_type"),
        "amount": decision.get("amount"),
        "amount_source": decision.get("amount_source"),
        "missing_information": decision.get("missing_information") or [],
        "rationale": decision.get("rationale"),
    }
    filters["policy_count"] = int(filters.get("policy_count") or 0)
    filters["issue_type"] = decision.get("issue_type") or filters.get("issue_type")
    warnings: list[str] = []
    if model_output.get("action_type") and model_output["action_type"] != decision.get("action_type"):
        # 留痕：模型与规则不一致时必须能查出来，否则"为什么最后是退款"无从解释
        warnings.append(
            f"模型建议动作 {model_output['action_type']}，规则判定 {decision.get('action_type')}，以规则为准"
        )
    return {"filters": filters, "warnings": warnings}


def prepare_ticket_reflection(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """冻结受保护决策并把最终客户回复交给质检 Agent。"""
    from app.services.reflection_guard import build_protected_snapshot, compare_protected_snapshot

    filters = dict(state.get("filters") or {})
    decision = dict(filters.get("decision") or {})
    response = _agent_payload(state, "response_agent")
    policy_refs = _agent_payload(state, "policy_rag_agent").get("policies") or []
    amount = _to_float(decision.get("amount")) or 0
    risk = assess(
        action_type=decision.get("action_type") or "reply_only",
        amount=amount,
        flags=list(filters.get("risk_flags") or []),
        conflict=bool(filters.get("conflict")),
        sensitive_customer=bool(filters.get("sensitive_customer")),
    )
    decision.update(
        {
            "risk_tier": risk.tier,
            "approval_requirement": risk.approval_requirement,
            "policy_refs": policy_refs,
        }
    )

    previous = state.get("protected_decision_snapshot")
    decision_version = int(state.get("decision_version") or 0)
    if previous is None:
        decision_version = 1
    else:
        comparison = compare_protected_snapshot(previous, decision)
        if not comparison.unchanged:
            decision_version += 1
    snapshot = build_protected_snapshot(decision, decision_version=decision_version)

    draft_version = int(state.get("draft_version") or 0) + 1
    evidence_refs = {
        str(item.get("local_ref"))
        for item in (state.get("agent_evidence") or [])
        if item.get("local_ref")
    }
    evidence_refs.update(
        str(item.get("evidence_ref")) for item in policy_refs if item.get("evidence_ref")
    )
    approval_status = state.get("approval_status") or "not_required"
    if risk.approval_requirement != ApprovalRequirement.NONE.value:
        # The graph's initial state says not_required before the action is
        # known. Once the deterministic risk rule requires approval, reflection
        # must see the real current state: required, but not approved yet.
        approval_status = "pending"
    filters.update(
        {
            "decision": decision,
            "draft_reply": response.get("customer_reply") or "",
            "draft_internal_note": response.get("internal_note") or "",
            "approval_status": approval_status,
            "reflection_evidence_refs": sorted(evidence_refs),
            "reflection_issues": [],
        }
    )
    with runtime.node_session(state, "prepare_ticket_reflection") as ctx:
        ctx.emit(
            "正在复核客户回复",
            {"draft_version": draft_version, "decision_version": decision_version},
        )
    return {
        "filters": filters,
        "draft_kind": "ticket_reply",
        "draft_version": draft_version,
        "protected_decision_snapshot": snapshot.as_dict(),
        "protected_decision_hash": snapshot.digest,
        "decision_version": decision_version,
        "max_reflection_rounds": int(filters.get("max_reflection_rounds") or 1),
        "review_stage": "ticket_reply_reflection",
    }


def _reflection_result_from_review(review: dict) -> dict:
    from app.schemas.reflection import ReflectionResult

    try:
        return ReflectionResult(
            verdict=review.get("verdict") or "pass",
            issues=review.get("issues") or [],
            required_changes=review.get("required_changes") or [],
            missing_evidence=review.get("missing_evidence") or [],
            confidence=float(review.get("confidence") or 0.0),
            stop_reason=review.get("stop_reason"),
        ).model_dump()
    except Exception:  # 模型输出非法时不能静默当作通过
        return {
            "verdict": "human_review",
            "issues": [
                {
                    "code": "reflection_schema_invalid",
                    "severity": "high",
                    "category": "consistency",
                    "target": "ticket_reply",
                    "source": "code",
                    "rule_code": "reflection_schema_invalid",
                    "message": "反思结果未通过结构校验",
                    "evidence_refs": [],
                    "auto_fixable": False,
                }
            ],
            "required_changes": [],
            "missing_evidence": [],
            "confidence": 0.0,
            "stop_reason": "schema_invalid",
        }


def evaluate_ticket_reflection(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """合并模型批评与代码硬门禁，代码结论拥有最终否决权。"""
    from app.services.reflection_guard import inspect_ticket_reply, reflection_idempotency_key

    filters = dict(state.get("filters") or {})
    review = _agent_payload(state, "review_agent")
    model_result = _reflection_result_from_review(review)
    code_result = inspect_ticket_reply(
        reply=str(filters.get("draft_reply") or ""),
        protected_decision=filters.get("decision") or {},
        approval_status=filters.get("approval_status"),
        evidence_refs=set(filters.get("reflection_evidence_refs") or []),
    ).model_dump()
    rank = {"pass": 0, "revise": 1, "retrieve_more": 2, "human_review": 3}
    winner = code_result if rank[code_result["verdict"]] >= rank[model_result["verdict"]] else model_result
    combined_issues: dict[str, dict] = {}
    known_refs = set(filters.get("reflection_evidence_refs") or [])
    for item in [*(model_result.get("issues") or []), *(code_result.get("issues") or [])]:
        item = dict(item)
        if item.get("source") == "model":
            item["evidence_refs"] = [ref for ref in item.get("evidence_refs") or [] if ref in known_refs]
        combined_issues[str(item.get("code"))] = item
    latest = {
        **winner,
        "issues": list(combined_issues.values()),
        "required_changes": list(
            dict.fromkeys(
                [*(model_result.get("required_changes") or []), *(code_result.get("required_changes") or [])]
            )
        ),
        "draft_version": int(state.get("draft_version") or 1),
        "decision_version": int(state.get("decision_version") or 1),
        "protected_hash_before": state.get("protected_decision_hash"),
        "protected_hash_after": state.get("protected_decision_hash"),
    }

    max_revisions = int(state.get("max_reflection_rounds") or 0)
    draft_version = int(state.get("draft_version") or 1)
    if latest["verdict"] in {"revise", "retrieve_more"} and draft_version > max_revisions:
        latest["verdict"] = "human_review"
        latest["stop_reason"] = "max_reflection_rounds_exhausted"

    history_key = f"ticket_reply:{draft_version}:1"
    history = dict(state.get("reflection_history") or {})
    if history_key in history:
        latest = history[history_key]
        reflection_rounds = int(state.get("reflection_rounds") or len(history))
    else:
        history[history_key] = latest
        reflection_rounds = len(history)
    idem_key = reflection_idempotency_key(
        task_id=str(state.get("task_id") or ""),
        draft_kind="ticket_reply",
        draft_version=draft_version,
        reflection_round=1,
    )
    filters["reflection_issues"] = latest.get("issues") or []

    with runtime.node_session(state, "evaluate_ticket_reflection") as ctx:
        issue_count = len(latest.get("issues") or [])
        ctx.emit(
            "回复质量复核已完成" if latest["verdict"] == "pass" else f"回复复核发现 {issue_count} 项问题",
            {"verdict": latest["verdict"], "draft_version": draft_version, "issue_count": issue_count},
            level="info" if latest["verdict"] == "pass" else "warn",
        )
    return {
        "filters": filters,
        "latest_reflection": latest,
        "reflection_history": history,
        "reflection_rounds": reflection_rounds,
        "unresolved_reflection_issues": latest.get("issues") or [],
        "reflection_stop_reason": latest.get("stop_reason"),
        "reflection_idempotency_key": idem_key,
        "needs_human_review": latest["verdict"] == "human_review",
    }


def apply_ticket_revision(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """应用修订结果，并再次核对受保护决策没有变化。"""
    from app.services.reflection_guard import compare_protected_snapshot

    filters = dict(state.get("filters") or {})
    revision = _agent_payload(state, "revision_agent")
    comparison = compare_protected_snapshot(
        state.get("protected_decision_snapshot") or {}, filters.get("decision") or {}
    )
    if not comparison.unchanged:
        issue = {
            "code": "protected_decision_changed",
            "severity": "critical",
            "category": "action",
            "target": "ticket_reply",
            "source": "code",
            "rule_code": "protected_decision_changed",
            "message": "修订前后受保护业务字段发生变化",
            "evidence_refs": [],
            "auto_fixable": False,
        }
        latest = {
            "verdict": "human_review",
            "issues": [issue],
            "required_changes": [],
            "missing_evidence": [],
            "confidence": 1.0,
            "stop_reason": "protected_decision_changed",
        }
        return {
            "latest_reflection": latest,
            "unresolved_reflection_issues": [issue],
            "reflection_stop_reason": latest["stop_reason"],
            "needs_human_review": True,
        }

    revised_text = str(revision.get("revised_text") or filters.get("draft_reply") or "")
    new_version = int(state.get("draft_version") or 1) + 1
    filters.update(
        {
            "draft_reply": revised_text,
            "reflection_issues": revision.get("remaining_issue_codes") or [],
        }
    )
    response_result = dict(_result_of(state, "response_agent"))
    response_blocks = dict(response_result.get("blocks") or {})
    response_blocks["customer_reply"] = revised_text
    response_result["blocks"] = response_blocks
    revision_id = f"ticket_reply:{new_version}"
    revision_history = dict(state.get("revision_history") or {})
    revision_history[revision_id] = {
        "draft_version": new_version,
        "addressed_issue_codes": revision.get("addressed_issue_codes") or [],
        "remaining_issue_codes": revision.get("remaining_issue_codes") or [],
        "changed_sections": revision.get("changed_sections") or [],
        "protected_hash_before": comparison.digest_before,
        "protected_hash_after": comparison.digest_after,
    }
    with runtime.node_session(state, "apply_ticket_revision") as ctx:
        ctx.emit(
            "已完成一次回复修订，正在再次复核",
            {"draft_version": new_version, "addressed": revision.get("addressed_issue_codes") or []},
        )
    return {
        "filters": filters,
        "agent_results": {"response_agent": response_result},
        "draft_version": new_version,
        "revision_history": revision_history,
        "latest_reflection": None,
    }


def ticket_retrieve_more(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """受控补查入口；只允许回到现有政策检索，不给反思 Agent 工具权限。"""
    filters = dict(state.get("filters") or {})
    filters["reflection_retrieval_requested"] = True
    with runtime.node_session(state, "ticket_retrieve_more") as ctx:
        ctx.emit(
            "发现资料缺口，正在补查适用政策",
            {"draft_version": state.get("draft_version")},
            level="warn",
        )
    return {"filters": filters}


def finalize_recommendation(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """规则汇总 + 落库建议 + 工单状态迁移（不交给模型）。"""
    with runtime.node_session(state, "finalize_recommendation") as ctx:
        ticket_id = (state.get("filters") or {}).get("ticket_id")
        service = TicketService(ctx.session)
        ticket = service.get(ticket_id)

        triage = _agent_payload(state, "triage_agent")
        resolution = _agent_payload(state, RESOLUTION_NODE)
        decision = (state.get("filters") or {}).get("decision") or {}
        response = _agent_payload(state, "response_agent")
        review = _agent_payload(state, "review_agent")
        policy = _agent_payload(state, "policy_rag_agent")
        latest_reflection = state.get("latest_reflection") or {}
        reflection_forced_human = latest_reflection.get("verdict") == "human_review"

        # 受控字段以**规则决策**为准（模型润色不得改变动作与金额）
        action_type = decision.get("action_type") or resolution.get("action_type") or "reply_only"
        amount_raw = decision.get("amount", resolution.get("amount"))
        rationale = decision.get("rationale") or resolution.get("rationale")
        missing_information = resolve_missing_information(decision, resolution)
        rule_issue_type = decision.get("issue_type")
        amount = None
        if amount_raw not in (None, "", "0"):
            try:
                amount = float(amount_raw)
            except (TypeError, ValueError):
                amount = None

        if not response.get("customer_reply"):
            from app.agents.after_sales import _reply_template

            facts = state_filters = state.get("filters") or {}
            fact_digest = state_filters.get("fact_digest") or {}
            response = {
                **response,
                "customer_reply": _reply_template(
                    action_type, ticket.order_id or "您的订单", fact_digest, decision
                ),
                "internal_note": f"内部备注：动作={action_type}；风险待规则复核；依据={decision.get('amount_source') or '—'}。",
            }
        if reflection_forced_human:
            response = {
                **response,
                "customer_reply": "该工单需要人工复核，系统未生成可直接发送的客户回复。",
                "internal_note": "自动质量复核未通过，请根据问题清单人工确认后再回复客户。",
            }

        # 风险等级以**规则**为准：模型给的 risk_tier 只作为参考（写进 warnings 以便对比）
        state_filters = state.get("filters") or {}
        policy_refs = policy.get("policies") or []
        rule_risk = assess(
            action_type=action_type,
            amount=amount,
            flags=list(state_filters.get("risk_flags") or []),
            # 责任争议与敏感客户也是 L3 的判据：少传这两项，"敏感客户"就会被判成 L2
            # （评测集里立刻暴露：sensitive 组 2 条 gate 召回失败）。
            conflict=bool(state_filters.get("conflict")),
            sensitive_customer=bool(state_filters.get("sensitive_customer")),
        )
        # 信息不足不得停在"可自动"档位：问题类型未识别、或存在缺失信息时，至少升到 L2（客服确认）。
        # 这是需求书 §4.3「任何低置信度、证据冲突或接口过期均进入人工处理」的落地。
        if rule_risk.tier == "L1" and (
            (rule_issue_type in (None, "other")) or missing_information or not policy_refs
        ):
            from app.services.ticket_risk import approval_for_tier

            reason_bits = []
            if rule_issue_type in (None, "other"):
                reason_bits.append("问题类型未识别")
            if missing_information:
                reason_bits.append("缺少关键信息：" + "、".join(missing_information[:2]))
            if not policy_refs:
                reason_bits.append("未找到适用的售后政策依据")
            rule_risk = type(rule_risk)(
                tier="L2",
                reason="；".join(reason_bits) + "（不得自动出结论，需客服确认）",
                approval_requirement=approval_for_tier("L2"),
            )
        model_tier = review.get("risk_tier")
        raw_warnings = list(review.get("warnings") or []) + list(state.get("warnings") or [])
        if model_tier and model_tier != rule_risk.tier:
            raw_warnings.append(f"模型判定 {model_tier}，规则判定 {rule_risk.tier}，以规则为准")
        warnings = reconcile_review_warnings(
            raw_warnings,
            policy_count=len(policy_refs),
            fact_digest=(state.get("filters") or {}).get("fact_digest") or {},
            action_type=action_type,
        )
        warnings.extend(
            str(item.get("message"))
            for item in (latest_reflection.get("issues") or [])
            if item.get("message") and str(item.get("message")) not in warnings
        )

        evidence_refs = [
            item.get("local_ref")
            for item in (state.get("agent_evidence") or [])
            if item.get("local_ref")
        ]
        status = (
            RecommendationStatus.PENDING_APPROVAL.value
            if requires_human_approval(rule_risk.tier) or reflection_forced_human
            else RecommendationStatus.DRAFT.value
        )
        effective_approval = rule_risk.approval_requirement
        if reflection_forced_human and effective_approval == ApprovalRequirement.NONE.value:
            effective_approval = ApprovalRequirement.STAFF_CONFIRM.value
        row = service.save_recommendation(
            ticket.id,
            {
                "analysis_task_id": state.get("task_id"),
                "action_type": action_type,
                "amount": amount,
                # 金额来源必须落库：审批人要能看出"89.00 是订单实付"还是"30 元是政策补偿标准"。
                # 以前只写在 rationale 的散文里，界面看不到、事后也无法按来源统计。
                "amount_source": decision.get("amount_source"),
                "rationale": rationale,
                "evidence_ids": evidence_refs,
                "policy_refs": policy_refs,
                "missing_information": missing_information,
                "risk_tier": rule_risk.tier,
                "risk_reason": rule_risk.reason,
                "approval_requirement": effective_approval,
                "confidence": (review.get("confidence") if review.get("confidence") else 0.7),
                "customer_reply": response.get("customer_reply"),
                "internal_note": response.get("internal_note"),
                "warnings": warnings,
                "status": status,
            },
        )

        # 工单字段回填：问题类型以规则为准（模型输出可能是中文标签，直接落库会污染统计口径）
        if rule_issue_type:
            ticket.issue_type = rule_issue_type
        if triage.get("sentiment"):
            ticket.sentiment = triage["sentiment"]
        urgency = triage.get("urgency")
        if urgency in ("low", "medium", "high", "urgent"):
            ticket.priority = urgency
        ticket.risk_tier = rule_risk.tier
        ctx.session.commit()

        # AI 草稿落到消息流（与"客服实际发送"分开存，满足 FR-04）
        if response.get("customer_reply"):
            service.add_message(
                ticket,
                content=response["customer_reply"],
                sender_type=MessageSender.AGENT.value,
                visible_to_customer=False,
                meta={
                    "recommendation_id": row.id,
                    "source": "response_agent",
                    "draft_version": int(state.get("draft_version") or 1),
                    "reflection_verdict": latest_reflection.get("verdict"),
                },
                idempotency_key=(
                    f"analysis-draft:{state.get('task_id')}:{int(state.get('draft_version') or 1)}"
                ),
            )

        ctx.emit(
            f"建议已生成：{row.action_type} / {rule_risk.tier} / {row.status}",
            {
                "recommendation_id": row.id,
                "amount": str(row.amount) if row.amount is not None else None,
                "approval_requirement": rule_risk.approval_requirement,
                "policy_count": len(policy_refs),
                "warnings": warnings[:5],
            },
            level="warn" if warnings else "info",
        )
        return {
            "recommendation_id": row.id,
            "risk_level": rule_risk.tier,
            "needs_human_review": requires_human_approval(rule_risk.tier) or reflection_forced_human,
            "filters": {**(state.get("filters") or {}), "risk_tier": rule_risk.tier},
            "warnings": warnings,
        }


def followup(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """跟进计划：只组装提醒，不阻塞等待（定时触发交给队列/worker）。"""
    with runtime.node_session(state, "followup") as ctx:
        payload = _agent_payload(state, FOLLOWUP_NODE)
        if not payload:
            filters = state.get("filters") or {}
            resolution = filters.get("resolution") or {}
            action = resolution.get("action_type")
            waiting_external = action in {
                "contact_logistics",
                "reship",
                "return_goods",
                "exchange",
            }
            if not waiting_external:
                payload = {"required": False}
            else:
                risk_tier = filters.get("risk_tier") or state.get("risk_level") or "L1"
                hours = 24 if risk_tier in ("L1", "L2") else 4
                payload = {
                    "required": True,
                    "next_check_hours": hours,
                    "escalate_after_hours": hours * 2,
                    "reminder": f"跟进：工单仍在等待外部处理（动作 {action}），请检查是否已闭环",
                }
        ticket_id = (state.get("filters") or {}).get("ticket_id")
        if not payload.get("required"):
            ctx.emit("无需跟进", {})
            return {"followup": {"required": False}}

        service = TicketService(ctx.session)
        ticket = service.get(ticket_id)
        plan = {
            "required": True,
            "next_check_hours": int(payload.get("next_check_hours") or 24),
            "escalate_after_hours": payload.get("escalate_after_hours"),
            "reminder": payload.get("reminder"),
        }
        attributes = dict(ticket.attributes or {})
        attributes["followup"] = plan
        ticket.attributes = attributes
        ctx.session.commit()
        ctx.emit("已登记跟进计划", plan)
        return {"followup": plan}


# --------------------------------------------------------------------------- 节点工厂
def analysis_agent_node(agent_code: str):
    """复用诊断工作流的 Agent 节点工厂（重试/Schema/权限/agent_runs 全部沿用）。"""
    return agent_node(agent_code)


__all__ = [
    "FINAL_NODES",
    "FOLLOWUP_NODE",
    "RESOLUTION_NODE",
    "TRIAGE_NODES",
    "analysis_agent_node",
    "finalize_recommendation",
    "followup",
    "load_ticket_context",
    "pass_resolution",
    "pass_triage",
]
