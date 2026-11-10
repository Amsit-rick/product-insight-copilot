"""工单分析编排：把"启动一次分析"这件事做完整，并保证状态机与幂等。

需求书 §6 要求 `POST /tickets/{id}/analyze` 返回 202 + `run_id`，进度用 SSE 获取。
本模块负责：

1. 状态推进：`NEW → ENRICHING → ANALYZING`（走状态机，不直接写 status）；
2. 创建一次分析运行（`AnalysisTask`）——**一次分析 = 一个 task**，
   于是执行栈（队列、事件、Agent 轨迹、成本、审计）全部复用；
3. 派发执行（inline / threadpool / queue 由配置决定）。
4. 幂等：同一工单在同一状态下重复点击不会叠加分析次数；
   已完成的分析不会被覆盖（`last_analysis_task_id` 只在真正新建运行时改写）。

成本闸门（为什么加）：7 个 Agent 全流程每单约 25~35k tokens，
高频工单量下会失控。所以**低风险（L1/L2）走"单链快路径"**，
只有 L3/L4 或检出冲突时才升级到多 Agent 全流程 —— 闸门值可配（`ticket_multi_agent_daily_budget`）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.enums import TaskStatus, TicketStatus
from app.models.identity import User
from app.models.ticket import Ticket
from app.models.workflow import AnalysisTask
from app.models.enums import IssueType
from app.services.audit import record_audit
from app.services.task_dispatch import dispatch_task, resolve_backend
from app.services.task_service import TaskService
from app.services.ticket_state import can_transition

# 运行已"停下来"的状态：这些状态下重跑是合法的（失败要能重试、被取消要能重开）
_FINISHED_TASK_STATUSES = {
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
}


class AnalysisInProgress(Exception):
    code = "analysis_in_progress"


class FactIncomplete(Exception):
    """事实不完整（缺订单号/订单不存在/接口超时）→ 不能分析，应先向客户或人工补齐。

    为什么是"拒绝对分析"而不是"照样跑一遍"：没有事实的分析只能产出"信息不足"，
    既烧钱又给客服一个没有信息量的结果；而需求书 §2.3 专门为这种情况留了
    `NEED_CUSTOMER_INFO` 状态 —— 语义上就该停在这里等补充。
    """

    code = "need_customer_info"

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("缺少必要事实：" + "、".join(missing or ["未知原因"]))


def start_analysis(
    session: Session,
    *,
    ticket: Ticket,
    user: User,
    use_multi_agent: bool | None = None,
) -> str:
    """启动一次分析，返回分析运行 ID（= AnalysisTask.id）。"""
    settings = get_settings()

    # 正在分析中：直接返回当前运行，不重复起（用户连点两次不该烧两份钱）。
    # 但"分析中"必须确认上一次还真的在跑：运行已失败/取消时若照样早退，
    # 工单会永久卡在 analyzing，前端「重新分析」变成哑按钮（演示脚本一次暴露 6 张）。
    if ticket.status == TicketStatus.ANALYZING.value and ticket.last_analysis_task_id:
        last = session.get(AnalysisTask, ticket.last_analysis_task_id)
        if last is not None and last.status not in _FINISHED_TASK_STATUSES:
            return ticket.last_analysis_task_id

    # 工单服务（事实落库与状态迁移都走它，保证审计与状态机不被绕过）
    from app.services.ticket_service import TicketService

    ticket_service = TicketService(session)

    # 只有可明确识别为解释/一般咨询的问题才默认走快路径。
    # 工单创建时 risk_tier 常为空，不能因此把退款等高风险诉求误分到快路径。
    from app.agents.after_sales import classify_issue, wants_refund_explicitly

    query = _build_query(ticket)
    issue_type = classify_issue(query)
    if use_multi_agent is None:
        explicit_high_risk = wants_refund_explicitly(query) or issue_type in {
            IssueType.REFUND.value,
            IssueType.RETURN.value,
            IssueType.EXCHANGE.value,
            IssueType.MISSING_ITEM.value,
            IssueType.NOT_SHIPPED.value,
            IssueType.WRONG_ITEM.value,
            IssueType.DAMAGED.value,
            IssueType.LOGISTICS_STALLED.value,
        }
        flags = set((ticket.attributes or {}).get("risk_flags") or [])
        sensitive = bool((ticket.attributes or {}).get("sensitive_customer"))
        known_low_risk = ticket.risk_tier in ("L1", "L2") or ticket.risk_tier is None
        use_multi_agent = (
            not known_low_risk
            or explicit_high_risk
            or sensitive
            or bool(flags)
            or issue_type != IssueType.PROMOTION_NOT_APPLIED.value
        )

    # 先补事实再分析：需求书 §2.2 的顺序（分类 → 事实 → 政策 → 建议）。
    # 事实缺失 → `enrich_ticket` 已把工单推进到"等客户补充"，这里直接停下来：
    # 没有事实的分析只能产出"信息不足"，白烧 token（踩过：加了事实补全后状态又被
    # 后面的推进逻辑覆盖成 ANALYZING，"缺信息"的信号被自己冲掉）。
    from app.services.order_fact_service import enrich_ticket

    collection = enrich_ticket(ticket_service, ticket, settings=settings)
    if not collection.has_order_facts:
        raise FactIncomplete(collection.missing)

    service = TaskService(session)
    from app.skills.after_sales import select_business_skills

    task, created = service.create(
        user=user,
        query=_build_query(ticket),
        filters={
            "ticket_id": ticket.id,
            "order_id": ticket.order_id,
            "issue_type": issue_type,
            "shop_id": ticket.shop_id,
            "platform": ticket.platform,
            "multi_agent": bool(use_multi_agent),
            "business_skills": select_business_skills(issue_type, bool(use_multi_agent)),
            # 业务详情和恢复流程读取持久化 filters，不能只依赖图内临时 state。
            # 先放服务端默认占位；task_id 生成后立即用稳定灰度判定覆盖。
            "reflection_enabled": False,
            "max_reflection_rounds": int(settings.max_reflection_rounds),
            # 工作流选择：运行器按这个名字取图（售后拓扑 vs 诊断拓扑）
            "workflow": "after_sales",
            # 售后政策的可见范围：政策库（检索层会按生效期与适用范围过滤）
            "knowledge_bases": ["policy_kb"],
            # 风控标记与敏感客户：来自工单属性，是 L4/L3 判定的输入。
            # 必须显式传下去 —— 少了它，"批量退款/疑似欺诈"只会被判成普通 L2（评测里会暴露）。
            "risk_flags": list((ticket.attributes or {}).get("risk_flags") or []),
            "sensitive_customer": bool((ticket.attributes or {}).get("sensitive_customer")),
        },
        title=f"[售后工单] {ticket.subject[:40]}",
        # 幂等键：同一工单 + 同一分析轮次只允许一条运行
        idempotency_key=f"ticket-analysis:{ticket.id}:{ticket.analysis_count + 1}",
    )

    if created:
        from app.services.reflection_release import release_state_for_rollout
        from app.services.reflection_rollout import (
            ReflectionRolloutRequest,
            decide_reflection_rollout,
        )

        release_state = None
        if settings.reflection_enabled and settings.reflection_ticket_enabled:
            release_state = release_state_for_rollout(session, ticket.tenant_id, settings)
        rollout = decide_reflection_rollout(
            ReflectionRolloutRequest(
                tenant_id=ticket.tenant_id,
                task_id=task.id,
                target="ticket_reply",
                risk_level=ticket.risk_tier,
                issue_type=issue_type,
            ),
            settings=settings,
            release_state=release_state,
        )
        task.filters = {
            **(task.filters or {}),
            "reflection_enabled": rollout.enabled,
            "reflection_rollout": rollout.as_dict(),
            "max_reflection_rounds": int(settings.max_reflection_rounds),
        }
        ticket.analysis_count += 1
        ticket.last_analysis_task_id = task.id

    # 状态推进：NEW → ENRICHING → ANALYZING（只走允许的边）
    if can_transition(ticket.status, TicketStatus.ENRICHING.value):
        ticket.status = TicketStatus.ENRICHING.value
    if can_transition(ticket.status, TicketStatus.ANALYZING.value):
        ticket.status = TicketStatus.ANALYZING.value

    record_audit(
        session,
        action="ticket_analysis_started",
        tenant_id=ticket.tenant_id,
        actor_id=user.id,
        resource_type="ticket",
        resource_id=ticket.id,
        detail={
            "run_id": task.id,
            "multi_agent": bool(use_multi_agent),
            "analysis_count": ticket.analysis_count,
            "mock_adapters": settings.ticket_use_mock_adapters,
        },
    )
    session.commit()

    if created:
        backend = dispatch_task(task.id)
        record_audit(
            session,
            action="ticket_analysis_dispatched",
            tenant_id=ticket.tenant_id,
            actor_id=user.id,
            resource_type="ticket",
            resource_id=ticket.id,
            detail={"run_id": task.id, "backend": backend or resolve_backend()},
            commit=True,
        )
    return task.id


def _build_query(ticket: Ticket) -> str:
    """分析用的查询文本：工单原话 + 关键定位信息。

    把订单号/商品等写进查询文本，是因为检索与事实查询都要靠它定位；
    少了这些字段，模型只能猜（这也是"缺订单号要退回客户补信息"的原因）。
    """
    parts = [ticket.content.strip()]
    if ticket.order_id:
        parts.append(f"订单号：{ticket.order_id}")
    if ticket.issue_type:
        parts.append(f"问题类型：{ticket.issue_type}")
    if ticket.attributes:
        pairs = "，".join(f"{key}={value}" for key, value in list(ticket.attributes.items())[:6])
        if pairs:
            parts.append(f"补充信息：{pairs}")
    return "\n".join(parts)
