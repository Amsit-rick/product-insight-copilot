"""报告阶段节点：人工审核（中断/恢复）→ 报告生成 → 报告校验 → 持久化。

关于人工审核的位置：按设计文档 7.1 的图，高风险分支在 `generate_report` **之前**
进入 `human_approval`。因此审核人看到的是"已校验的证据 + 结论草案 + 风险原因"，
通过后才生成正式报告；审核结论作为报告元数据写入，驳回也保留完整留痕。

`interrupt()` 会让节点在恢复时**从头重跑**，所以 interrupt 之前的副作用必须幂等：
待审核记录用 `ensure_pending_approval` 去重，事件写入做了去重检查。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.agents.schemas import AgentInput
from app.graph.runtime import NodeRuntime
from app.graph.state import AnalysisState, bump_visits, state_digest
from app.logging_utils import get_logger
from app.models.enums import StatementType, TaskStatus
from app.services.approval_service import (
    apply_decision,
    approval_status_for,
    ensure_pending_approval,
    latest_approval,
)
from app.services.report_service import persist_report

logger = get_logger(__name__)

MAX_FACTS = 30
MAX_INFERENCES = 15
MAX_HYPOTHESES = 8
MAX_RECOMMENDATIONS = 8


# --------------------------------------------------------------------------- 人工审核
def human_approval(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """中断等待人工审核。恢复时 `interrupt` 返回审核决定。"""
    from langgraph.types import interrupt

    reason = _approval_reason(state)
    payload = _approval_payload(state, reason)

    with runtime.node_session(state, "human_approval") as ctx:
        ctx.check_cancel()
        approval = ensure_pending_approval(
            ctx.session,
            task_id=state.get("task_id", ""),
            reason=reason,
            payload=payload,
        )
        ctx.wait_for_human(reason)
        if ctx.task is not None:
            ctx.task.risk_level = state.get("risk_level") or ctx.task.risk_level
            ctx.session.flush()
        if not _already_emitted(ctx, f"任务进入人工审核：{reason}"):
            ctx.emit(f"任务进入人工审核：{reason}", payload, level="warn")
        ctx.audit("task_waiting_human", detail={"reason": reason, "approval_id": approval.id})
        if ctx.task is not None:
            # 供跨进程恢复使用：把结论摘要落库，进程重启后仍可生成报告
            ctx.task.result = {
                **(ctx.task.result or {}),
                "state_digest": state_digest(state),
                "awaiting": "human_approval",
                "approval_payload": payload,
            }
            ctx.session.flush()

    decision = interrupt(payload)
    normalized = _normalize_decision(decision)

    with runtime.node_session(state, "human_approval") as ctx:
        apply_decision(
            ctx.session,
            task_id=state.get("task_id", ""),
            action=normalized["action"],
            reviewer_id=normalized.get("reviewer_id"),
            comment=normalized.get("comment"),
            modifications=normalized.get("modifications"),
        )
        ctx.resume_from_human()
        ctx.emit(
            f"人工审核完成：{normalized['action']}"
            + (f"，意见：{normalized['comment']}" if normalized.get("comment") else ""),
            {"action": normalized["action"], "modifications": list((normalized.get("modifications") or {}).keys())},
        )
        ctx.audit(
            "task_approval_decided",
            outcome=normalized["action"],
            detail={"comment": normalized.get("comment"), "modifications": normalized.get("modifications")},
        )
        return {
            "approval_status": approval_status_for(normalized["action"]),
            "approval": normalized,
            "needs_human_review": True,
            "node_visits": bump_visits(state, "human_approval"),
        }


def _approval_reason(state: AnalysisState) -> str:
    verification = state.get("verification") or {}
    reasons = verification.get("reasons") or state.get("risk_reasons") or []
    if verification.get("unresolved_conflicts"):
        return f"存在 {verification['unresolved_conflicts']} 处未裁决的证据冲突"
    if state.get("injection"):
        return "问题文本包含提示词注入特征，必须人工确认"
    if state.get("force_human_review"):
        return "任务被显式要求人工审核"
    return "；".join(str(r) for r in reasons[:2]) or "高风险结论，需要人工确认"


def _approval_payload(state: AnalysisState, reason: str) -> dict:
    coverage = state.get("coverage") or {}
    statements = state.get("statements") or []
    top_facts = [s for s in statements if s.get("type") == "fact"][:5]
    return {
        "type": "human_approval",
        "task_id": state.get("task_id"),
        "query": state.get("query"),
        "intent": state.get("intent"),
        "reason": reason,
        "risk_level": state.get("risk_level"),
        "risk_reasons": state.get("risk_reasons") or [],
        "conflicts": state.get("conflicts") or [],
        "coverage": {
            "evidence_count": coverage.get("evidence_count"),
            "fact_count": coverage.get("fact_count"),
            "metric_available": coverage.get("metric_available"),
            "data_gaps": coverage.get("data_gaps") or [],
        },
        "agent_summaries": {
            code: {
                "ok": payload_.get("ok"),
                "summary": payload_.get("summary"),
                "findings": len(payload_.get("findings") or []),
                "confidence": payload_.get("confidence"),
            }
            for code, payload_ in (state.get("agent_results") or {}).items()
        },
        "top_facts": [
            {"id": item.get("id"), "text": item.get("text"), "evidence_refs": item.get("evidence_refs")}
            for item in top_facts
        ],
        "actions": ["approve", "reject", "modify"],
    }


def _normalize_decision(decision) -> dict:
    if isinstance(decision, str):
        return {"action": decision if decision in {"approve", "reject", "modify"} else "approve"}
    if isinstance(decision, dict):
        action = str(decision.get("action") or "approve")
        if action not in {"approve", "reject", "modify"}:
            action = "approve"
        return {
            "action": action,
            "reviewer_id": decision.get("reviewer_id"),
            "comment": decision.get("comment"),
            "modifications": decision.get("modifications") or {},
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }
    return {"action": "approve", "modifications": {}}


def _already_emitted(ctx, message: str) -> bool:
    from app.models.workflow import AnalysisEvent

    return (
        ctx.session.query(AnalysisEvent.id)
        .filter(AnalysisEvent.task_id == ctx.state.get("task_id"))
        .filter(AnalysisEvent.node == ctx.node)
        .filter(AnalysisEvent.message == message[:2000])
        .first()
        is not None
    )


# --------------------------------------------------------------------------- 报告生成
def generate_report(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    from app.agents import build_agent

    digest = _report_digest(state)
    with runtime.node_session(state, "generate_report", load_task=False) as ctx:
        agent = build_agent("report_agent")
        result = agent.run(
            AgentInput(
                session=ctx.session,
                task_id=state.get("task_id", ""),
                tenant_id=state.get("tenant_id", "*"),
                user_id=state.get("user_id", ""),
                query=state.get("query", ""),
                filters=dict(state.get("filters") or {}),
                intent=state.get("intent"),
                plan_item={"digest": digest},
                kb_codes=set(),
                max_security_rank=int(state.get("max_security_rank") or 1),
                roles=list(state.get("roles") or []),
            )
        )
        facts = [f for f in result.findings if f.get("type") == "fact"]
        inferences = [f for f in result.findings if f.get("type") == "inference"]
        blocks = result.blocks or {}
        coverage = state.get("coverage") or {}
        sufficiency = state.get("sufficiency") or {}
        approval = state.get("approval") or {}

        report = {
            "summary": result.summary or "（报告摘要缺失）",
            "impact_scope": blocks.get("impact_scope") or [],
            "facts": facts[:MAX_FACTS],
            "inferences": inferences[:MAX_INFERENCES],
            "hypotheses": (blocks.get("hypotheses") or [])[:MAX_HYPOTHESES],
            "recommendations": (blocks.get("recommendations") or [])[:MAX_RECOMMENDATIONS],
            "evidence": state.get("evidence") or [],
            "conflicts": state.get("conflicts") or [],
            "priority": blocks.get("priority") or "P2",
            "confidence": float(blocks.get("confidence") or result.confidence or 0.0),
            "needs_human_review": bool(state.get("needs_human_review")),
            "risk_level": state.get("risk_level") or "low",
            "approval_status": state.get("approval_status") or "not_required",
            "data_gaps": list(coverage.get("data_gaps") or []),
            "warnings": list(state.get("warnings") or []),
            "meta": {
                "task_id": state.get("task_id"),
                "intent": state.get("intent"),
                "query": state.get("query"),
                "filters": state.get("filters") or {},
                "workflow_version": state.get("workflow_version"),
                "prompt_version": state.get("prompt_version"),
                "model_used": result.model_used,
                "model_name": result.model_name,
                "sufficiency": sufficiency,
                "verification": state.get("verification") or {},
                "retrieval_rounds": int(state.get("retrieval_rounds") or 0),
                "agents": {
                    code: {
                        "ok": payload.get("ok"),
                        "summary": payload.get("summary"),
                        "confidence": payload.get("confidence"),
                        "model_used": payload.get("model_used"),
                        "skipped": payload.get("skipped"),
                    }
                    for code, payload in (state.get("agent_results") or {}).items()
                },
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        }

        # 证据不足时必须在摘要里说清楚（拒答语义），不允许用模糊表达掩盖
        if not sufficiency.get("sufficient", True):
            reasons = "；".join(str(r) for r in (sufficiency.get("reasons") or [])[:3])
            report["summary"] = f"证据不足，结论仅供参考。{reasons}。{report['summary']}"
            report["needs_human_review"] = True

        # 可回答性不通过：把事实/推断降级为"待验证假设"，避免用不相关材料硬答
        if coverage.get("answerability", {}).get("answerable") is False:
            answer = coverage["answerability"]
            demoted = [
                {**item, "verify_by": "需要先补齐与该问题直接相关的材料"}
                for item in [*report["facts"], *report["inferences"]]
            ]
            report["hypotheses"] = [*report["hypotheses"], *demoted][:MAX_HYPOTHESES]
            report["facts"] = []
            report["inferences"] = []
            report["recommendations"] = report["recommendations"][:1]
            # 摘要必须说清"是谁判的"：模型复核判定时再打印关键词覆盖率会自相矛盾
            if answer.get("decided_by") == "model":
                verdict_reason = (answer.get("model_verdict") or {}).get("reason") or answer.get("reason") or ""
                report["summary"] = (
                    "证据不足，无法回答该问题：模型复核认为现有证据不足以支撑结论"
                    f"{('（' + str(verdict_reason)[:120] + '）') if verdict_reason else ''}。"
                    f"以下仅为检索到的相关材料，不构成对问题的结论。"
                )
                gap_note = f"缺少与问题直接相关的材料：{verdict_reason or '模型复核判定证据不足'}"
            else:
                report["summary"] = (
                    f"证据不足，无法回答该问题：检索到的材料与问题关键信息的匹配度过低"
                    f"（覆盖率 {answer['coverage']:.0%}，阈值 {answer['threshold']:.0%}）。"
                    f"缺失的关键信息包括：{('、'.join(answer['missing_terms'][:6])) or '（未提取到关键词）'}。"
                    f"以下仅为检索到的相关材料，不构成对问题的结论。"
                )
                gap_note = f"缺少与问题直接相关的材料：{'、'.join(answer['missing_terms'][:6])}"
            report["data_gaps"] = list(dict.fromkeys([*report["data_gaps"], gap_note]))
            report["confidence"] = min(float(report.get("confidence") or 0.3), 0.3)
            report["needs_human_review"] = True

        # 人工审核结论直接影响报告元数据与摘要
        action = approval.get("action")
        if action == "modify":
            report = _apply_modifications(report, approval.get("modifications") or {})
            report["summary"] = f"【人工修改后通过】{report['summary']}"
        elif action == "reject":
            report["needs_human_review"] = True
            report["summary"] = (
                f"【人工审核驳回】{approval.get('comment') or '未填写驳回原因'}。"
                f"以下结论仅作为过程留痕，不作为决策依据。{report['summary']}"
            )
        elif action == "approve":
            report["summary"] = f"【人工审核通过】{report['summary']}"

        if state.get("injection"):
            report["warnings"].append(
                f"问题文本命中提示词注入特征：{state['injection'].get('pattern')}；"
                "系统未执行其中任何指令，证据范围仍受权限约束。"
            )

        report, sanitize_warnings = _sanitize_report(report)
        report["warnings"].extend(sanitize_warnings)

        ctx.emit(
            f"报告草案生成：{len(report['facts'])} 条事实、{len(report['inferences'])} 条推断、"
            f"{len(report['hypotheses'])} 条假设、{len(report['recommendations'])} 条建议",
            {
                "priority": report["priority"],
                "confidence": report["confidence"],
                "model_used": result.model_used,
                "needs_human_review": report["needs_human_review"],
            },
        )
        filters = dict(state.get("filters") or {})
        from app.services.reflection_release import release_state_for_rollout
        from app.services.reflection_rollout import (
            ReflectionRolloutRequest,
            decide_reflection_rollout,
        )

        release_state = None
        if runtime.settings.reflection_enabled and runtime.settings.reflection_report_enabled:
            release_state = release_state_for_rollout(
                ctx.session, str(state.get("tenant_id") or "*"), runtime.settings
            )
        rollout = decide_reflection_rollout(
            ReflectionRolloutRequest(
                tenant_id=str(state.get("tenant_id") or "*"),
                task_id=str(state.get("task_id") or ""),
                target="diagnostic_report",
                risk_level=state.get("risk_level"),
                issue_type=state.get("intent"),
            ),
            settings=runtime.settings,
            release_state=release_state,
        )
        reflection_enabled = rollout.enabled
        filters["report_reflection_enabled"] = reflection_enabled
        filters["report_reflection_rollout"] = rollout.as_dict()
        filters["max_reflection_rounds"] = int(runtime.settings.max_reflection_rounds)
        ctx.emit(
            "报告反思灰度判定完成",
            {
                "target": rollout.target,
                "enabled": rollout.enabled,
                "reason": rollout.reason,
                "bucket": rollout.bucket,
                "percent": rollout.percent,
                "release_status": rollout.release_status,
            },
        )
        draft_version = int(state.get("draft_version") or 0) + 1 if reflection_enabled else int(
            state.get("draft_version") or 0
        )
        return {
            "report_draft": report,
            "needs_human_review": report["needs_human_review"],
            "filters": filters,
            "draft_kind": "diagnostic_report" if reflection_enabled else state.get("draft_kind"),
            "draft_version": draft_version,
            "max_reflection_rounds": int(runtime.settings.max_reflection_rounds),
            "review_stage": "post_report_reflection" if reflection_enabled else state.get("review_stage"),
            "node_visits": bump_visits(state, "generate_report"),
        }


def _report_digest(state: AnalysisState) -> dict:
    digest = state_digest(state)
    digest["statements"] = state.get("statements") or []
    digest["coverage"] = state.get("coverage") or {}
    digest["conflicts"] = state.get("conflicts") or []
    digest["evidence"] = state.get("evidence") or []
    digest["metric_results"] = state.get("metric_results") or []
    digest["filters"] = state.get("filters") or {}
    return digest


def _apply_modifications(report: dict, modifications: dict) -> dict:
    """人工修改只允许覆盖白名单字段，且改写内容同样进审计。"""
    allowed = {
        "summary",
        "impact_scope",
        "facts",
        "inferences",
        "hypotheses",
        "recommendations",
        "priority",
        "confidence",
    }
    updated = dict(report)
    for key, value in modifications.items():
        if key in allowed and value is not None:
            updated[key] = value
    return updated


def _sanitize_report(report: dict) -> tuple[dict, list[str]]:
    from app.services.policy_engine import get_policy_engine

    policy = get_policy_engine()
    warnings: list[str] = []
    for section in ("summary", "facts", "inferences", "hypotheses", "recommendations"):
        value = report.get(section)
        if isinstance(value, str):
            hit = policy.scan_output(value)
            if hit:
                report[section] = value.replace(hit, "[已过滤]")
                warnings.append(f"{section} 命中敏感内容「{hit}」，已过滤")
        elif isinstance(value, list):
            for item in value:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "")
                hit = policy.scan_output(text)
                if hit:
                    item["text"] = text.replace(hit, "[已过滤]")
                    warnings.append(f"{section} 命中敏感内容「{hit}」，已过滤")
    return report, warnings


def route_after_generate_report(state: AnalysisState) -> str:
    return (
        "reflect_report"
        if (state.get("filters") or {}).get("report_reflection_enabled")
        else "validate_report"
    )


def _report_reflection_result(blocks: dict) -> dict:
    from app.schemas.reflection import ReflectionResult

    try:
        return ReflectionResult(
            verdict=blocks.get("verdict") or "pass",
            issues=blocks.get("issues") or [],
            required_changes=blocks.get("required_changes") or [],
            missing_evidence=blocks.get("missing_evidence") or [],
            confidence=float(blocks.get("confidence") or 0.0),
            stop_reason=blocks.get("stop_reason"),
        ).model_dump()
    except Exception:
        return {
            "verdict": "human_review",
            "issues": [
                {
                    "code": "report_reflection_schema_invalid",
                    "severity": "high",
                    "category": "consistency",
                    "target": "diagnostic_report",
                    "source": "code",
                    "rule_code": "report_reflection_schema_invalid",
                    "message": "报告反思结果未通过结构校验",
                    "evidence_refs": [],
                    "auto_fixable": False,
                }
            ],
            "required_changes": [],
            "missing_evidence": [],
            "confidence": 0.0,
            "stop_reason": "schema_invalid",
        }


def reflect_report(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """独立复核已生成的报告草稿，代码门禁拥有最终否决权。"""
    from app.agents import build_agent
    from app.services.reflection_guard import reflection_idempotency_key
    from app.services.report_reflection import inspect_report

    report = state.get("report_draft") or {}
    evidence_ids = {
        str(item.get("evidence_id"))
        for item in (state.get("evidence") or [])
        if item.get("evidence_id")
    }
    filters = dict(state.get("filters") or {})
    filters.update(
        {
            "report_draft": report,
            "report_evidence_ids": sorted(evidence_ids),
            "report_conflicts": state.get("conflicts") or [],
        }
    )
    with runtime.node_session(state, "reflect_report", load_task=False) as ctx:
        agent = build_agent("report_reflection_agent")
        result = agent.run(
            AgentInput(
                session=ctx.session,
                task_id=state.get("task_id", ""),
                tenant_id=state.get("tenant_id", "*"),
                user_id=state.get("user_id", ""),
                query=state.get("query", ""),
                filters=filters,
                kb_codes=set(),
                max_security_rank=int(state.get("max_security_rank") or 1),
                roles=list(state.get("roles") or []),
            )
        )
        model_result = _report_reflection_result(result.blocks or {})
        code_result = inspect_report(
            report=report,
            evidence_ids=evidence_ids,
            conflicts=list(state.get("conflicts") or []),
        ).model_dump()
        rank = {"pass": 0, "revise": 1, "retrieve_more": 2, "human_review": 3}
        winner = code_result if rank[code_result["verdict"]] >= rank[model_result["verdict"]] else model_result
        issues = {
            str(item.get("code")): dict(item)
            for item in [*(model_result.get("issues") or []), *(code_result.get("issues") or [])]
        }
        draft_version = int(state.get("draft_version") or 1)
        latest = {
            **winner,
            "issues": list(issues.values()),
            "draft_version": draft_version,
            "route": winner["verdict"],
        }
        max_revisions = int(state.get("max_reflection_rounds") or 0)
        if latest["verdict"] in {"revise", "retrieve_more"} and draft_version > max_revisions:
            latest["verdict"] = "human_review"
            latest["route"] = "human_review"
            latest["stop_reason"] = "max_reflection_rounds_exhausted"

        history_key = f"diagnostic_report:{draft_version}:1"
        history = dict(state.get("reflection_history") or {})
        if history_key in history:
            latest = history[history_key]
        else:
            history[history_key] = latest
        filters["reflection_issues"] = latest.get("issues") or []
        idem_key = reflection_idempotency_key(
            task_id=str(state.get("task_id") or ""),
            draft_kind="diagnostic_report",
            draft_version=draft_version,
            reflection_round=1,
        )
        ctx.emit(
            "报告质量复核已完成" if latest["verdict"] == "pass" else f"报告复核发现 {len(latest.get('issues') or [])} 项问题",
            {"verdict": latest["verdict"], "draft_version": draft_version},
            level="info" if latest["verdict"] == "pass" else "warn",
        )
        return {
            "filters": filters,
            "latest_reflection": latest,
            "reflection_history": history,
            "reflection_rounds": len(history),
            "unresolved_reflection_issues": latest.get("issues") or [],
            "reflection_stop_reason": latest.get("stop_reason"),
            "reflection_idempotency_key": idem_key,
            "needs_human_review": latest["verdict"] == "human_review",
            "review_stage": "post_report_reflection",
            "node_visits": bump_visits(state, "reflect_report"),
        }


def route_after_reflect_report(state: AnalysisState) -> str:
    return {
        "pass": "validate_report",
        "revise": "revise_report",
        "retrieve_more": "retrieve_more",
        "human_review": "human_approval",
    }.get((state.get("latest_reflection") or {}).get("verdict"), "human_approval")


def revise_report_node(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    from app.agents import build_agent

    filters = dict(state.get("filters") or {})
    filters.update(
        {
            "report_draft": state.get("report_draft") or {},
            "report_evidence_ids": [
                item.get("evidence_id")
                for item in (state.get("evidence") or [])
                if item.get("evidence_id")
            ],
            "report_conflicts": state.get("conflicts") or [],
            "reflection_issues": (state.get("latest_reflection") or {}).get("issues") or [],
        }
    )
    with runtime.node_session(state, "revise_report", load_task=False) as ctx:
        result = build_agent("report_revision_agent").run(
            AgentInput(
                session=ctx.session,
                task_id=state.get("task_id", ""),
                tenant_id=state.get("tenant_id", "*"),
                user_id=state.get("user_id", ""),
                query=state.get("query", ""),
                filters=filters,
                kb_codes=set(),
                max_security_rank=int(state.get("max_security_rank") or 1),
                roles=list(state.get("roles") or []),
            )
        )
        revision = result.blocks or {}
        revised_report = revision.get("revised_report") or state.get("report_draft") or {}
        new_version = int(state.get("draft_version") or 1) + 1
        revision_id = f"diagnostic_report:{new_version}"
        history = dict(state.get("revision_history") or {})
        history[revision_id] = {
            "draft_version": new_version,
            "addressed_issue_codes": revision.get("addressed_issue_codes") or [],
            "remaining_issue_codes": revision.get("remaining_issue_codes") or [],
            "changed_sections": revision.get("changed_sections") or [],
        }
        ctx.emit(
            "已完成一次报告修订，正在再次复核",
            {"draft_version": new_version, "changed_sections": revision.get("changed_sections") or []},
        )
        return {
            "report_draft": revised_report,
            "draft_version": new_version,
            "revision_history": history,
            "latest_reflection": None,
            "node_visits": bump_visits(state, "revise_report"),
        }


def route_after_human_approval(state: AnalysisState) -> str:
    return (
        "apply_post_report_approval"
        if state.get("review_stage") == "post_report_reflection"
        else "generate_report"
    )


def apply_post_report_approval(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    report = dict(state.get("report_draft") or {})
    approval = state.get("approval") or {}
    action = approval.get("action")
    if action == "modify":
        report = _apply_modifications(report, approval.get("modifications") or {})
        report["summary"] = f"【人工修改后通过】{report.get('summary') or ''}"
    elif action == "reject":
        report["needs_human_review"] = True
        report["summary"] = f"【人工审核驳回】{approval.get('comment') or '未填写驳回原因'}。{report.get('summary') or ''}"
    else:
        report["summary"] = f"【人工审核通过】{report.get('summary') or ''}"
    with runtime.node_session(state, "apply_post_report_approval", load_task=False) as ctx:
        ctx.emit("已应用报告后人工审核结论", {"action": action or "approve"})
    return {
        "report_draft": report,
        "review_stage": "post_revision_safety",
        "node_visits": bump_visits(state, "apply_post_report_approval"),
    }


# --------------------------------------------------------------------------- 报告校验
def validate_report(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """代码节点：结构、引用完整性、类型合规与敏感内容四类校验。"""
    report = state.get("report_draft") or {}
    evidence_ids = {item.get("evidence_id") for item in (state.get("evidence") or [])}
    problems: list[str] = []

    if not report.get("summary"):
        problems.append("报告缺少结论摘要")
    if report.get("priority") not in {"P0", "P1", "P2", "P3"}:
        problems.append(f"优先级非法：{report.get('priority')}")
    confidence = report.get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
        problems.append(f"置信度非法：{confidence}")

    for section, kind in (
        ("facts", StatementType.FACT),
        ("inferences", StatementType.INFERENCE),
        ("hypotheses", StatementType.HYPOTHESIS),
        ("recommendations", StatementType.RECOMMENDATION),
    ):
        for item in report.get(section) or []:
            if not str(item.get("text") or "").strip():
                problems.append(f"{section} 存在空陈述")
                continue
            refs = item.get("evidence_refs") or []
            unknown = [r for r in refs if r not in evidence_ids]
            if unknown:
                problems.append(f"{section} 陈述 {item.get('id')} 引用了不存在的证据 {unknown}")
            # 事实与推断必须有引用；没有引用的降级为假设，不允许冒充事实
            if kind in {StatementType.FACT, StatementType.INFERENCE} and not refs and not item.get("supplemental"):
                problems.append(
                    f"{kind.value} 陈述 {item.get('id')} 缺少引用证据，已降级为假设"
                )
                item.setdefault("_demote_to_hypothesis", True)

    if state.get("conflicts") and not report.get("conflicts"):
        problems.append("存在证据冲突但报告未呈现冲突")

    if problems:
        demoted = [item for item in (report.get("facts") or []) if item.get("_demote_to_hypothesis")]
        if demoted:
            report["facts"] = [item for item in report["facts"] if not item.get("_demote_to_hypothesis")]
            for item in demoted:
                item.pop("_demote_to_hypothesis", None)
                item.setdefault("verify_by", "需要补充证据后再确认")
                report.setdefault("hypotheses", []).append(item)
        if any("降级" in p for p in problems):
            report["needs_human_review"] = True

    validated = not [p for p in problems if "降级" not in p]

    with runtime.node_session(state, "validate_report", load_task=False) as ctx:
        ctx.emit(
            f"报告校验{'通过' if validated else '存在问题'}：{len(problems)} 处问题",
            {"problems": problems[:10]},
            level="warn" if problems else "info",
        )
        return {
            "report_draft": report,
            "report_validated": validated,
            "report_problems": problems,
            "needs_human_review": bool(report.get("needs_human_review")),
            "node_visits": bump_visits(state, "validate_report"),
        }


# --------------------------------------------------------------------------- 持久化
def persist_result(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    report = dict(state.get("report_draft") or {})
    if (state.get("filters") or {}).get("report_reflection_enabled"):
        latest = state.get("latest_reflection") or {}
        report["reflection"] = {
            "enabled": True,
            "label": (
                "需要人工确认"
                if latest.get("verdict") == "human_review"
                else "已完成质量复核并修订"
                if state.get("revision_history")
                else "已完成质量复核"
            ),
            "verdict": latest.get("verdict"),
            "rounds": int(state.get("reflection_rounds") or 0),
            "issue_count": len(latest.get("issues") or []),
            "revised": bool(state.get("revision_history")),
            "issues": [
                {
                    key: item.get(key)
                    for key in ("code", "severity", "category", "message", "auto_fixable")
                }
                for item in (latest.get("issues") or [])
            ],
            "stop_reason": state.get("reflection_stop_reason"),
        }
    else:
        report["reflection"] = {
            "enabled": False,
            "label": "未启用自动复核",
            "verdict": None,
            "rounds": 0,
            "issue_count": 0,
            "revised": False,
            "issues": [],
            "stop_reason": None,
        }
    task_id = state.get("task_id", "")

    with runtime.node_session(state, "persist_result") as ctx:
        row = persist_report(
            ctx.session,
            task_id=task_id,
            tenant_id=state.get("tenant_id", "*"),
            report=report,
            evidence=state.get("evidence") or [],
            created_by=state.get("user_id"),
        )
        approval = latest_approval(ctx.session, task_id)
        if approval is not None and approval.report_id is None:
            approval.report_id = row.id

        final = {
            **report,
            "report_id": row.id,
            "version": row.version,
            "content_md": row.content_md,
            "citation_count": len(row.content_json.get("facts") or []),
        }
        if ctx.task is not None:
            ctx.task.status = TaskStatus.COMPLETED.value
            ctx.task.current_node = "persist_result"
            ctx.task.waiting_reason = None
            ctx.task.finished_at = ctx.task.finished_at or _utcnow()
            ctx.task.risk_level = report.get("risk_level") or "low"
            ctx.task.result = {
                "report_id": row.id,
                "version": row.version,
                "summary": report.get("summary"),
                "priority": report.get("priority"),
                "confidence": report.get("confidence"),
                "risk_level": report.get("risk_level"),
                "approval_status": report.get("approval_status"),
                "needs_human_review": report.get("needs_human_review"),
                "evidence_count": len(state.get("evidence") or []),
                "conflict_count": len(state.get("conflicts") or []),
                "report_problems": state.get("report_problems") or [],
                "reflection_rounds": int(state.get("reflection_rounds") or 0),
                "reflection_stop_reason": state.get("reflection_stop_reason"),
                "reflection_history": state.get("reflection_history") or {},
                "revision_history": state.get("revision_history") or {},
                "state_digest": state_digest(state),
            }
            ctx.session.flush()

        ctx.emit(
            f"报告已保存（v{row.version}），证据 {len(state.get('evidence') or [])} 条，"
            f"引用已写入可定位记录",
            {"report_id": row.id, "priority": report.get("priority")},
        )
        ctx.audit(
            "report_persisted",
            detail={
                "report_id": row.id,
                "version": row.version,
                "approval_status": report.get("approval_status"),
                "needs_human_review": report.get("needs_human_review"),
            },
        )
        return {
            "final_report": final,
            "report_id": row.id,
            "report_problems": state.get("report_problems") or [],
            "node_visits": bump_visits(state, "persist_result"),
        }


def _utcnow():
    from app.models.base import utcnow

    return utcnow()


__all__ = [
    "MAX_FACTS",
    "apply_post_report_approval",
    "generate_report",
    "human_approval",
    "persist_result",
    "reflect_report",
    "revise_report_node",
    "route_after_generate_report",
    "route_after_human_approval",
    "route_after_reflect_report",
    "validate_report",
]
