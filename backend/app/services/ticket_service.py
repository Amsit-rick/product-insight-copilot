"""售后工单业务服务：创建、筛选、状态迁移、消息、事实、建议与审批。

分层的理由（三处都踩过坑，所以写下来）：

1. **状态迁移只走 `ticket_state`**：服务层不许自己拼状态值，否则"前端不能改状态"这条约束
   会在某个接口里被绕过。
2. **每次迁移都写审计**：需求书 FR-06 要求"每次动作产生不可篡改审计日志"。
   审计文本要带**前后状态与理由**，否则事后查不出是谁把工单从待审批推到已解决的。
3. **创建带幂等**：渠道重复推送（`external_id` 相同）或网络重试（`idempotency_key` 相同）
   都不能产生第二张工单 —— 这是"写操作幂等"（§4.3）在工单入口的落地。

金额与事实的写入权限也在这里把住：`recommendation.amount` 只允许由本服务与适配器写，
分析节点只能"引用"。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.logging_utils import get_logger
from app.models.base import utcnow
from app.models.enums import (
    ApprovalRequirement,
    IssueType,
    MessageSender,
    RecommendationStatus,
    RoleCode,
    RiskTier,
    TicketChannel,
    TicketPriority,
    TicketStatus,
)
from app.models.ticket import OrderFact, Recommendation, Ticket, TicketMessage
from app.services.audit import record_audit
from app.services.ticket_state import (
    InvalidTransition,
    assert_transition,
    can_transition,
    is_terminal,
)

logger = get_logger(__name__)

TICKET_DATA_ORIGINS = frozenset({"business", "demo", "test"})
LEGACY_TEST_TICKET_PREFIXES = (
    "EVAL-",
    "E2E-",
    "DEBUG-",
    "REPRO-",
    "WF-TEST-",
    "CS-TEST-",
    "OBS-",
    "POL-TEST-",
    "VERIFY-",
)


def ticket_data_origin(ticket: Ticket) -> str:
    """Resolve the stored origin, with a narrow fallback for old automation records."""
    value = str((ticket.attributes or {}).get("data_origin") or "").strip().lower()
    if value in TICKET_DATA_ORIGINS:
        return value
    external_id = str(ticket.external_id or "")
    if any(external_id.startswith(prefix) for prefix in LEGACY_TEST_TICKET_PREFIXES):
        return "test"
    return "business"


def _test_ticket_expression():
    """Match explicit tests and legacy unmarked automation records in SQL."""
    stored_origin = Ticket.attributes["data_origin"].as_string()
    legacy_prefix = or_(
        *(Ticket.external_id.startswith(prefix) for prefix in LEGACY_TEST_TICKET_PREFIXES)
    )
    return or_(stored_origin == "test", and_(stored_origin.is_(None), legacy_prefix))


class TicketNotFound(Exception):
    code = "ticket_not_found"


class RecommendationNotFound(Exception):
    code = "recommendation_not_found"


class TicketService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ 创建
    def create(
        self,
        *,
        tenant_id: str,
        actor_id: str | None,
        subject: str,
        content: str,
        external_id: str | None = None,
        customer_id: str | None = None,
        order_id: str | None = None,
        channel: str = TicketChannel.MANUAL.value,
        priority: str = TicketPriority.MEDIUM.value,
        issue_type: str | None = None,
        shop_id: str | None = None,
        platform: str | None = None,
        attributes: dict | None = None,
        data_origin: str = "business",
        idempotency_key: str | None = None,
    ) -> tuple[Ticket, bool]:
        """创建工单。返回 (工单, 是否命中幂等)。"""
        existing = self._find_idempotent(
            tenant_id=tenant_id, external_id=external_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            return existing, True

        normalized_attributes = dict(attributes or {})
        normalized_attributes["data_origin"] = (
            data_origin if data_origin in TICKET_DATA_ORIGINS else "business"
        )
        ticket = Ticket(
            tenant_id=tenant_id,
            external_id=external_id,
            customer_id=customer_id,
            order_id=order_id,
            shop_id=shop_id,
            platform=platform,
            subject=subject[:255],
            content=content,
            channel=channel,
            status=TicketStatus.NEW.value,
            priority=priority,
            issue_type=issue_type,
            created_by=actor_id,
            attributes=normalized_attributes,
            idempotency_key=idempotency_key,
        )
        self.session.add(ticket)
        self.session.flush()

        # 客户原话落成第一条消息：工单内容与消息流必须一致（详情页左栏展示的就是它）
        self.session.add(
            TicketMessage(
                ticket_id=ticket.id,
                sender_type=MessageSender.CUSTOMER.value,
                content=content,
                visible_to_customer=True,
            )
        )
        record_audit(
            self.session,
            action="ticket_created",
            tenant_id=tenant_id,
            actor_id=actor_id,
            resource_type="ticket",
            resource_id=ticket.id,
            detail={
                "external_id": external_id,
                "channel": channel,
                "order_id": order_id,
                "issue_type": issue_type,
            },
        )
        self.session.commit()
        return ticket, False

    def _find_idempotent(
        self, *, tenant_id: str, external_id: str | None, idempotency_key: str | None
    ) -> Ticket | None:
        if idempotency_key:
            hit = (
                self.session.query(Ticket)
                .filter(Ticket.tenant_id == tenant_id, Ticket.idempotency_key == idempotency_key)
                .first()
            )
            if hit is not None:
                return hit
        if external_id:
            return (
                self.session.query(Ticket)
                .filter(Ticket.tenant_id == tenant_id, Ticket.external_id == external_id)
                .first()
            )
        return None

    # ------------------------------------------------------------------ 查询
    def get(self, ticket_id: str, *, tenant_id: str | None = None) -> Ticket:
        query = self.session.query(Ticket).filter(Ticket.id == ticket_id)
        if tenant_id:
            query = query.filter(Ticket.tenant_id == tenant_id)
        ticket = query.first()
        if ticket is None:
            raise TicketNotFound(ticket_id)
        return ticket

    def list_tickets(
        self,
        *,
        tenant_id: str,
        statuses: list[str] | None = None,
        issue_types: list[str] | None = None,
        priorities: list[str] | None = None,
        assignee_id: str | None = None,
        unassigned: bool = False,
        attention: str | None = None,
        keyword: str | None = None,
        include_test: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Ticket], int]:
        query = self.session.query(Ticket).filter(Ticket.tenant_id == tenant_id)
        if not include_test:
            query = query.filter(~_test_ticket_expression())
        if statuses:
            query = query.filter(Ticket.status.in_(statuses))
        if issue_types:
            query = query.filter(Ticket.issue_type.in_(issue_types))
        if priorities:
            query = query.filter(Ticket.priority.in_(priorities))
        if unassigned:
            query = query.filter(Ticket.assignee_id.is_(None))
        elif assignee_id:
            query = query.filter(Ticket.assignee_id == assignee_id)
        if attention:
            now = utcnow()
            if attention == "at_risk":
                from app.services.ticket_view import SLA_MINUTES_BY_PRIORITY, TERMINAL_TICKET_STATUSES

                risk_windows = [
                    and_(
                        Ticket.priority == priority,
                        Ticket.created_at <= now - timedelta(minutes=max(minutes - 30, 0)),
                    )
                    for priority, minutes in SLA_MINUTES_BY_PRIORITY.items()
                ]
                query = query.filter(
                    ~Ticket.status.in_(TERMINAL_TICKET_STATUSES),
                    or_(*risk_windows),
                )
            elif attention == "waiting_reply":
                query = query.filter(Ticket.status == TicketStatus.READY_TO_REPLY.value)
            elif attention == "waiting_customer":
                query = query.filter(Ticket.status == TicketStatus.NEED_CUSTOMER_INFO.value)
            elif attention == "waiting_review":
                query = query.filter(Ticket.status == TicketStatus.WAITING_REVIEW.value)
            elif attention == "resolved_today":
                day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                query = query.filter(
                    Ticket.status == TicketStatus.RESOLVED.value,
                    Ticket.updated_at >= day_start,
                )
        if keyword:
            like = f"%{keyword.strip()}%"
            # 搜索要覆盖工单号/订单号/问题关键词（原型里的 placeholder 就是这么承诺的）
            query = query.filter(
                or_(
                    Ticket.external_id.ilike(like),
                    Ticket.order_id.ilike(like),
                    Ticket.subject.ilike(like),
                    Ticket.content.ilike(like),
                    Ticket.customer_id.ilike(like),
                )
            )
        total = query.count()
        if attention == "resolved_today":
            order_columns = (Ticket.updated_at.desc(), Ticket.id.asc())
        elif attention == "at_risk":
            order_columns = (Ticket.created_at.asc(), Ticket.id.asc())
        else:
            terminal_rank = case(
                (Ticket.status.in_([TicketStatus.RESOLVED.value, TicketStatus.CANCELLED.value]), 1),
                else_=0,
            )
            priority_rank = case(
                (Ticket.priority == TicketPriority.URGENT.value, 0),
                (Ticket.priority == TicketPriority.HIGH.value, 1),
                (Ticket.priority == TicketPriority.MEDIUM.value, 2),
                (Ticket.priority == TicketPriority.LOW.value, 3),
                else_=4,
            )
            action_rank = case(
                (Ticket.status == TicketStatus.READY_TO_REPLY.value, 0),
                (Ticket.status == TicketStatus.WAITING_REVIEW.value, 1),
                (Ticket.status == TicketStatus.ESCALATED.value, 2),
                (Ticket.status == TicketStatus.FAILED.value, 3),
                (Ticket.status == TicketStatus.NEW.value, 4),
                (Ticket.status == TicketStatus.NEED_CUSTOMER_INFO.value, 5),
                else_=6,
            )
            order_columns = (
                terminal_rank.asc(),
                priority_rank.asc(),
                action_rank.asc(),
                Ticket.created_at.asc(),
                Ticket.id.asc(),
            )
        rows = query.order_by(*order_columns).limit(min(limit, 200)).offset(max(offset, 0)).all()
        return rows, total

    def list_quality_tickets(
        self,
        *,
        tenant_id: str,
        quality_status: str | None = None,
        quality_issue_code: str | None = None,
        keyword: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Ticket], int, dict[str, dict]]:
        """按最新质检记录筛选工单。

        当前摘要字段用于索引筛选，内部消息只作为历史回退和详情审计来源。该接口
        面向主管质检页，按租户和关键词取候选工单后服务端分页，避免前端只在当前
        50 条数据中筛选；兼容迁移前的旧工单记录。
        """
        query = self.session.query(Ticket).filter(Ticket.tenant_id == tenant_id)
        query = query.filter(~_test_ticket_expression())
        if keyword:
            like = f"%{keyword.strip()}%"
            query = query.filter(
                or_(
                    Ticket.external_id.ilike(like),
                    Ticket.order_id.ilike(like),
                    Ticket.subject.ilike(like),
                    Ticket.content.ilike(like),
                    Ticket.customer_id.ilike(like),
                )
            )
        # 结论筛选可命中摘要索引；问题标记仍需兼容 JSON 字段，先在服务层过滤。
        if quality_status:
            query = query.filter(Ticket.quality_status == quality_status)
        candidates = query.order_by(Ticket.created_at.desc()).all()
        if not candidates:
            return [], 0, {}

        from app.models.ticket import TicketMessage

        legacy_ids = [ticket.id for ticket in candidates if not ticket.quality_reviewed_at]
        messages = self.session.query(TicketMessage).filter(
            TicketMessage.ticket_id.in_(legacy_ids or ["__no_legacy_ticket__"]),
            TicketMessage.visible_to_customer.is_(False),
        ).order_by(TicketMessage.created_at.desc()).all()
        summaries: dict[str, dict] = {}
        for ticket in candidates:
            if ticket.quality_reviewed_at:
                summaries[ticket.id] = {
                    "quality_status": ticket.quality_status or "unreviewed",
                    "quality_issue_codes": [str(item) for item in (ticket.quality_issue_codes or [])],
                    "quality_reviewed_at": ticket.quality_reviewed_at,
                    "quality_reviewer_id": ticket.quality_reviewer_id,
                }
        for message in messages:
            meta = message.meta or {}
            if meta.get("message_type") != "quality_review" or message.ticket_id in summaries:
                continue
            summaries[message.ticket_id] = {
                "quality_status": str(meta.get("quality_result") or "unreviewed"),
                "quality_issue_codes": [str(item) for item in (meta.get("quality_issue_codes") or [])],
                "quality_reviewed_at": message.created_at,
                "quality_reviewer_id": message.actor_id,
            }

        filtered = []
        for ticket in candidates:
            summary = summaries.get(ticket.id, {"quality_status": "unreviewed", "quality_issue_codes": []})
            if quality_status and summary["quality_status"] != quality_status:
                continue
            if quality_issue_code and quality_issue_code not in summary["quality_issue_codes"]:
                continue
            filtered.append(ticket)
        total = len(filtered)
        rows = filtered[max(offset, 0) : max(offset, 0) + min(limit, 5000)]
        return rows, total, {ticket.id: summaries.get(ticket.id, {}) for ticket in rows}

    def assignment_recommendations(self, ticket: Ticket) -> dict:
        """返回确定性的只读派单建议；没有真实技能档案时不伪造“擅长某类工单”。"""
        from app.auth.rbac import can_access
        from app.models.identity import User

        terminal = [TicketStatus.RESOLVED.value, TicketStatus.CANCELLED.value]
        high_priorities = [TicketPriority.HIGH.value, TicketPriority.URGENT.value]
        load_rows = (
            self.session.query(
                Ticket.assignee_id,
                func.count(Ticket.id).label("active_count"),
                func.sum(case((Ticket.priority.in_(high_priorities), 1), else_=0)).label(
                    "high_count"
                ),
                func.sum(
                    case((Ticket.status == TicketStatus.WAITING_REVIEW.value, 1), else_=0)
                ).label("review_count"),
            )
            .filter(
                Ticket.tenant_id == ticket.tenant_id,
                Ticket.assignee_id.is_not(None),
                ~_test_ticket_expression(),
                Ticket.status.notin_(terminal),
            )
            .group_by(Ticket.assignee_id)
            .all()
        )
        loads = {
            row.assignee_id: (
                int(row.active_count or 0),
                int(row.high_count or 0),
                int(row.review_count or 0),
            )
            for row in load_rows
        }
        users = (
            self.session.query(User)
            .filter(User.tenant_id == ticket.tenant_id, User.is_active.is_(True))
            .order_by(User.username.asc())
            .all()
        )
        items: list[dict] = []
        needs_approval = ticket.risk_tier in (RiskTier.L3.value, RiskTier.L4.value) or (
            ticket.issue_type == IssueType.REFUND.value
        )
        for user in users:
            can_reply = can_access(user.role_codes, "ticket:reply")
            can_approve = can_access(user.role_codes, "ticket:approve")
            if not (can_reply or can_approve):
                continue
            active, high, review = loads.get(user.id, (0, 0, 0))
            # 整数分避免浮点排序漂移；user_id 是最终稳定 tie-breaker。
            score = 1000 - active * 70 - high * 40 - review * 25
            reasons = [f"当前活跃负载 {active} 单，其中高优先级 {high} 单"]
            if user.has_role(RoleCode.OPERATION):
                score += 90
                reasons.append("客服岗位与日常接单职责匹配")
            elif user.has_role(RoleCode.REVIEWER):
                score += 60
                reasons.append("售后主管岗位可承接复核与升级工单")
            else:
                score -= 100
                reasons.append("具备处理权限，但不是日常售后接单岗位")
            if active == 0:
                reasons.append("当前无活跃工单，可优先接单")
            if needs_approval and can_approve:
                score += 160
                reasons.append("具备审批权限，适合高风险工单")
            elif needs_approval:
                score -= 160
                reasons.append("当前工单需审批，该候选人仅适合协同处理")
            elif can_reply:
                score += 40
                reasons.append("具备客服回复权限")
            items.append(
                {
                    "user_id": user.id,
                    "username": user.username,
                    "full_name": user.full_name,
                    "department": user.department,
                    "can_approve": can_approve,
                    "active_ticket_count": active,
                    "high_priority_count": high,
                    "waiting_review_count": review,
                    "score": score,
                    "reasons": reasons,
                }
            )
        items.sort(key=lambda item: (-item["score"], item["username"], item["user_id"]))
        return {
            "ticket_id": ticket.id,
            "current_assignee_id": ticket.assignee_id,
            "issue_type_context": ticket.issue_type,
            "rule_version": "assignment-load-v1",
            "items": items,
        }

    # ------------------------------------------------------------------ 状态
    def transition(
        self,
        ticket: Ticket,
        target: str,
        *,
        actor_id: str | None = None,
        actor_name: str | None = None,
        reason: str | None = None,
        detail: dict | None = None,
    ) -> Ticket:
        """唯一允许改工单状态的入口：先过状态机，再写审计。"""
        current = ticket.status
        if current == target:
            return ticket
        assert_transition(current, target)
        ticket.status = target
        if is_terminal(target):
            ticket.closed_at = utcnow()
        if target == TicketStatus.ESCALATED.value and reason:
            ticket.escalation_reason = reason[:255]
        record_audit(
            self.session,
            action="ticket_status_changed",
            tenant_id=ticket.tenant_id,
            actor_id=actor_id,
            actor_name=actor_name,
            resource_type="ticket",
            resource_id=ticket.id,
            detail={"from": current, "to": target, "reason": reason, **(detail or {})},
        )
        self.session.commit()
        return ticket

    def sync_analysis_aborted(
        self,
        *,
        ticket_id: str,
        run_id: str,
        outcome: str = TicketStatus.FAILED.value,
        error: str | None = None,
    ) -> Ticket | None:
        """分析运行失败/被取消时把工单落到可重试状态。

        为什么必须做（踩过）：运行器的 `_fail` 只改 `AnalysisTask.status`，
        工单会**永久停在 analyzing** —— 前端显示"分析中"转圈，重试按钮又因为
        "分析中不重复起"而毫无反应。失败必须是**可见且可重试**的状态，
        所以这里把工单推到 FAILED（状态机允许 ANALYZING → FAILED、FAILED → ENRICHING）。
        """
        ticket = self.session.get(Ticket, ticket_id)
        if ticket is None:
            return None
        # 只回写"当前这次运行"的结局：否则会把一张已经重新分析成功的工单打回失败
        if ticket.last_analysis_task_id not in (None, run_id):
            return ticket
        # 只降级"还没得出结论"的工单（补事实中/分析中）：若建议已经落库、工单已到
        # 待回复/待审批，说明失败发生在后半段（如跟进计划），不该把已有结论的工单
        # 撤回成失败 —— 那会让客服丢掉一份本来可用的草稿。
        if ticket.status not in (TicketStatus.ENRICHING.value, TicketStatus.ANALYZING.value):
            return ticket
        if not can_transition(ticket.status, outcome):
            return ticket
        label = "失败" if outcome == TicketStatus.FAILED.value else "被取消"
        return self.transition(
            ticket,
            outcome,
            reason=f"分析运行{label}：{(error or '未知原因')[:120]}",
            detail={"run_id": run_id, "error": (error or "")[:500]},
        )

    def assign(self, ticket: Ticket, *, assignee_id: str | None, actor_id: str | None, reason: str | None) -> Ticket:
        ticket.assignee_id = assignee_id
        record_audit(
            self.session,
            action="ticket_assigned",
            tenant_id=ticket.tenant_id,
            actor_id=actor_id,
            resource_type="ticket",
            resource_id=ticket.id,
            detail={"assignee_id": assignee_id, "reason": reason},
        )
        self.session.commit()
        return ticket

    def update_order_id(
        self, ticket: Ticket, *, order_id: str, actor_id: str | None
    ) -> Ticket:
        """补录订单号并清除对应缺失标记，保留修改前后值供审计。"""
        previous = ticket.order_id
        ticket.order_id = order_id.strip()
        order_field_names = {"订单号", "order_id", "order id"}
        ticket.missing_fields = [
            item
            for item in (ticket.missing_fields or [])
            if str(item).strip().lower() not in order_field_names
        ]
        record_audit(
            self.session,
            action="ticket_order_id_updated",
            tenant_id=ticket.tenant_id,
            actor_id=actor_id,
            resource_type="ticket",
            resource_id=ticket.id,
            detail={"previous_order_id": previous, "order_id": ticket.order_id},
        )
        self.session.commit()
        return ticket

    # ------------------------------------------------------------------ 消息
    def add_message(
        self,
        ticket: Ticket,
        *,
        content: str,
        sender_type: str = MessageSender.STAFF.value,
        actor_id: str | None = None,
        visible_to_customer: bool = True,
        meta: dict | None = None,
        attachments: list | None = None,
        idempotency_key: str | None = None,
    ) -> TicketMessage:
        if idempotency_key:
            existing = (
                self.session.query(TicketMessage)
                .filter(
                    TicketMessage.ticket_id == ticket.id,
                    TicketMessage.idempotency_key == idempotency_key,
                )
                .first()
            )
            if existing is not None:
                return existing
        message = TicketMessage(
            ticket_id=ticket.id,
            sender_type=sender_type,
            actor_id=actor_id,
            content=content,
            visible_to_customer=visible_to_customer,
            meta=meta or {},
            attachments=attachments or [],
            idempotency_key=idempotency_key,
        )
        self.session.add(message)
        self.session.commit()
        return message

    def messages(self, ticket_id: str) -> list[TicketMessage]:
        return (
            self.session.query(TicketMessage)
            .filter(TicketMessage.ticket_id == ticket_id)
            .order_by(TicketMessage.created_at.asc())
            .all()
        )

    # ------------------------------------------------------------------ 事实
    def save_facts(self, ticket_id: str, facts: list[dict]) -> list[OrderFact]:
        rows: list[OrderFact] = []
        for item in facts:
            row = OrderFact(
                ticket_id=ticket_id,
                source=item.get("source", "order"),
                fact_type=item.get("fact_type", "snapshot"),
                payload=item.get("payload") or {},
                observed_at=item.get("observed_at") or utcnow(),
                request_id=item.get("request_id"),
                is_mock=bool(item.get("is_mock", True)),
                adapter=item.get("adapter"),
                error=item.get("error"),
            )
            self.session.add(row)
            rows.append(row)
        self.session.commit()
        return rows

    def facts(self, ticket_id: str) -> list[OrderFact]:
        return (
            self.session.query(OrderFact)
            .filter(OrderFact.ticket_id == ticket_id)
            .order_by(OrderFact.created_at.asc())
            .all()
        )

    # ------------------------------------------------------------------ 建议
    def save_recommendation(self, ticket_id: str, payload: dict) -> Recommendation:
        """写入/覆盖工单的当前建议。

        金额只允许由这里写：分析节点把金额当**引用值**传进来（来自规则或适配器），
        而不是让模型现算 —— 需求书 §1.2 明确不允许模型算金额。
        """
        analysis_task_id = payload.get("analysis_task_id")
        row = None
        if analysis_task_id:
            row = (
                self.session.query(Recommendation)
                .filter(
                    Recommendation.ticket_id == ticket_id,
                    Recommendation.analysis_task_id == analysis_task_id,
                )
                .first()
            )
        if row is None:
            row = Recommendation(ticket_id=ticket_id, analysis_task_id=analysis_task_id)
            self.session.add(row)

        amount = payload.get("amount")
        row.action_type = payload.get("action_type", row.action_type or "reply_only")
        row.amount = Decimal(str(amount)) if amount is not None else None
        row.amount_source = payload.get("amount_source", row.amount_source)
        row.currency = payload.get("currency", row.currency or "CNY")
        row.rationale = payload.get("rationale")
        row.evidence_ids = payload.get("evidence_ids") or []
        row.policy_refs = payload.get("policy_refs") or []
        row.missing_information = payload.get("missing_information") or []
        row.risk_tier = payload.get("risk_tier", row.risk_tier or RiskTier.L1.value)
        row.risk_reason = payload.get("risk_reason")
        row.approval_requirement = payload.get(
            "approval_requirement", row.approval_requirement or ApprovalRequirement.NONE.value
        )
        row.confidence = payload.get("confidence")
        row.customer_reply = payload.get("customer_reply")
        row.internal_note = payload.get("internal_note")
        row.warnings = payload.get("warnings") or []
        if payload.get("status"):
            row.status = payload["status"]
        elif row.status in (None, "", RecommendationStatus.DRAFT.value):
            # 需求书 §3.3：L3/L4 必须进审批；L1/L2 直接可用
            row.status = (
                RecommendationStatus.PENDING_APPROVAL.value
                if row.approval_requirement
                in (ApprovalRequirement.SUPERVISOR.value, ApprovalRequirement.RISK_CONTROL.value)
                else RecommendationStatus.DRAFT.value
            )
        ticket = self.get(ticket_id)
        ticket.risk_tier = row.risk_tier
        self.session.commit()

        # 需要审批的建议 → 工单进"待审批"（这是需求书 §2.3 状态机里 WAITING_REVIEW 的唯一来源）。
        # 放在这里而不是让调用方自己推状态：漏推的后果是"建议在等审批，工单却还在分析中"，
        # 客服看不到待办、审批人不知道要审 —— 这类不一致只能靠单一入口避免。
        if row.status == RecommendationStatus.PENDING_APPROVAL.value and can_transition(
            ticket.status, TicketStatus.WAITING_REVIEW.value
        ):
            self.transition(
                ticket,
                TicketStatus.WAITING_REVIEW.value,
                actor_id=None,
                reason=f"生成 {row.risk_tier} 建议，等待人工审批",
                detail={"recommendation_id": row.id},
            )
        elif row.status == RecommendationStatus.PENDING_APPROVAL.value:
            # 状态机不允许从当前态直接进"待审批"（例如工单还在 NEW）：
            # 不静默跳过 —— 否则会出现"建议在等审批，但工单没进待审批"，客服看不到待办。
            logger.warning(
                "建议 %s 需要审批，但工单 %s 当前状态 %s 无法进入 waiting_review",
                row.id,
                ticket.id,
                ticket.status,
            )
        # 低风险建议（草稿可用）→ 工单进"待回复"。
        # 少了这一步，分析完成后工单会卡在 analyzing：客服看不到待办、列表上也筛不出来
        # （实测踩过：7 个 Agent 跑完但工单状态没推进）。
        if row.status == RecommendationStatus.DRAFT.value and can_transition(
            ticket.status, TicketStatus.READY_TO_REPLY.value
        ):
            self.transition(
                ticket,
                TicketStatus.READY_TO_REPLY.value,
                actor_id=None,
                reason=f"生成 {row.risk_tier} 建议草稿，等待客服确认发送",
                detail={"recommendation_id": row.id},
            )
        return row

    def latest_recommendation(self, ticket_id: str) -> Recommendation | None:
        return (
            self.session.query(Recommendation)
            .filter(Recommendation.ticket_id == ticket_id)
            .order_by(Recommendation.created_at.desc())
            .first()
        )

    def get_recommendation(
        self, recommendation_id: str, *, tenant_id: str | None = None
    ) -> Recommendation:
        """读取建议，并在需要时把租户边界下推到同一条查询。

        Recommendation 本身只保存 ticket_id，没有重复保存 tenant_id。审批接口如果
        先按 UUID 全局读取，再在后续流程里处理工单，就会给跨租户 UUID 猜测留下
        写操作入口。因此租户过滤必须和建议读取绑定，不能依赖调用方事后补校验。
        """
        query = self.session.query(Recommendation)
        if tenant_id is not None:
            query = query.join(Ticket, Ticket.id == Recommendation.ticket_id).filter(
                Ticket.tenant_id == tenant_id
            )
        row = query.filter(Recommendation.id == recommendation_id).first()
        if row is None:
            raise RecommendationNotFound(recommendation_id)
        return row

    def decide(
        self,
        recommendation: Recommendation,
        *,
        decision: str,
        actor_id: str | None,
        actor_name: str | None = None,
        reason: str | None = None,
        modified_reply: str | None = None,
        idempotency_key: str | None = None,
    ) -> Recommendation:
        """审批建议：批准 / 驳回（可带修改后通过）。

        幂等：同一 `idempotency_key` 重复提交直接返回既有结果（§4.3 写操作必须幂等），
        否则点两次"批准"会产生两条审批记录、甚至两次写操作。
        """
        if idempotency_key and recommendation.idempotency_key == idempotency_key:
            return recommendation

        ticket = self.get(recommendation.ticket_id)
        if decision == "approve":
            recommendation.status = RecommendationStatus.APPROVED.value
            if modified_reply:
                # 修改后通过：保留原始草稿（FR-04），改动人写进审计
                recommendation.original_reply = recommendation.original_reply or recommendation.customer_reply
                recommendation.customer_reply = modified_reply
            self.transition(
                ticket,
                TicketStatus.READY_TO_REPLY.value,
                actor_id=actor_id,
                actor_name=actor_name,
                reason=reason or "审批通过",
                detail={"recommendation_id": recommendation.id},
            )
        elif decision == "reject":
            recommendation.status = RecommendationStatus.REJECTED.value
            self.transition(
                ticket,
                TicketStatus.ANALYZING.value,
                actor_id=actor_id,
                actor_name=actor_name,
                reason=reason or "审批驳回，重新分析",
                detail={"recommendation_id": recommendation.id},
            )
        else:
            raise ValueError(f"不支持的审批结论: {decision}")

        recommendation.decided_by = actor_id
        recommendation.decided_at = utcnow()
        recommendation.decision_reason = reason
        recommendation.idempotency_key = idempotency_key
        record_audit(
            self.session,
            action="recommendation_decided",
            tenant_id=ticket.tenant_id,
            actor_id=actor_id,
            actor_name=actor_name,
            resource_type="recommendation",
            resource_id=recommendation.id,
            detail={
                "decision": decision,
                "reason": reason,
                "risk_tier": recommendation.risk_tier,
                "modified": bool(modified_reply),
            },
        )
        self.session.commit()
        return recommendation

    def confirm_and_send(
        self,
        recommendation: Recommendation,
        *,
        actor_id: str | None,
        actor_name: str | None = None,
        reason: str | None = None,
    ) -> Recommendation:
        """客服确认后直接发送（L2 = `staff_confirm` 的执行路径）。

        为什么需要它（踩过）：按风险分级，L2 的口径是"客服确认即可执行"，
        但发送接口只认 `approved/sent`，界面上写操作按钮又因未批准被置灰——
        于是 L2 的写操作建议（补发/换货/小额补偿）**在系统里没有任何执行路径**：
        既不进待审批队列，也发不出去，客服只能绕过系统去后台改单。
        这里把"客服点确认"这件事本身记成审批记录（谁确认、什么时候、什么理由），
        门禁不放松：只有 `approval_requirement == staff_confirm` 的草稿能走这条路，
        L3/L4 仍然必须主管/风控审批。
        """
        if recommendation.approval_requirement != ApprovalRequirement.STAFF_CONFIRM.value:
            raise InvalidTransition(
                recommendation.status, f"confirm_and_send({recommendation.approval_requirement})"
            )
        if recommendation.status not in (
            RecommendationStatus.DRAFT.value,
            RecommendationStatus.PENDING_APPROVAL.value,
        ):
            raise InvalidTransition(recommendation.status, RecommendationStatus.APPROVED.value)

        ticket = self.get(recommendation.ticket_id)
        recommendation.status = RecommendationStatus.APPROVED.value
        recommendation.decided_by = actor_id
        recommendation.decided_at = utcnow()
        recommendation.decision_reason = reason or "客服确认后执行"
        record_audit(
            self.session,
            action="recommendation_confirmed_by_staff",
            tenant_id=ticket.tenant_id,
            actor_id=actor_id,
            actor_name=actor_name,
            resource_type="recommendation",
            resource_id=recommendation.id,
            detail={
                "decision": "approve",
                "confirm_mode": "staff",
                "risk_tier": recommendation.risk_tier,
                "action_type": recommendation.action_type,
                "reason": recommendation.decision_reason,
            },
        )
        self.session.commit()
        return self.mark_sent(recommendation, actor_id=actor_id)

    def mark_sent(self, recommendation: Recommendation, *, actor_id: str | None) -> Recommendation:
        """客服发送回复：草稿转成实际回复，工单进入已解决。"""
        # 发送是终态；服务层也做幂等保护，避免未来新增调用方绕过路由后重复写客户消息。
        if recommendation.status == RecommendationStatus.SENT.value:
            return recommendation
        ticket = self.get(recommendation.ticket_id)
        recommendation.status = RecommendationStatus.SENT.value
        recommendation.sent_at = utcnow()
        if recommendation.customer_reply:
            self.add_message(
                ticket,
                content=recommendation.customer_reply,
                sender_type=MessageSender.STAFF.value,
                actor_id=actor_id,
                meta={"recommendation_id": recommendation.id},
            )
        self.transition(
            ticket,
            TicketStatus.RESOLVED.value,
            actor_id=actor_id,
            reason="已发送回复",
            detail={"recommendation_id": recommendation.id},
        )
        self.session.commit()
        return recommendation

    # ------------------------------------------------------------------ 指标卡
    def stats(
        self,
        *,
        tenant_id: str,
        assignee_id: str | None = None,
        unassigned: bool = False,
    ) -> dict:
        """工作台顶部四张卡的数据（原型里是假数据，这里给出真实口径）。"""
        now = utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        visible = ~_test_ticket_expression()

        def scoped(query):
            query = query.filter(Ticket.tenant_id == tenant_id)
            if unassigned:
                return query.filter(Ticket.assignee_id.is_(None))
            if assignee_id:
                return query.filter(Ticket.assignee_id == assignee_id)
            return query

        excluded_test_records = (
            scoped(self.session.query(func.count(Ticket.id)))
            .filter(_test_ticket_expression())
            .scalar()
            or 0
        )
        today = (
            scoped(self.session.query(func.count(Ticket.id)))
            .filter(visible, Ticket.created_at >= day_start)
            .scalar()
            or 0
        )
        pending_statuses = {
            TicketStatus.NEW.value,
            TicketStatus.ENRICHING.value,
            TicketStatus.ANALYZING.value,
            TicketStatus.NEED_CUSTOMER_INFO.value,
        }
        pending = (
            scoped(self.session.query(func.count(Ticket.id)))
            .filter(visible, Ticket.status.in_(pending_statuses))
            .scalar()
            or 0
        )
        high_priority = (
            scoped(self.session.query(func.count(Ticket.id)))
            .filter(
                visible,
                Ticket.status.in_(pending_statuses),
                Ticket.priority.in_([TicketPriority.HIGH.value, TicketPriority.URGENT.value]),
            )
            .scalar()
            or 0
        )
        waiting_review = (
            scoped(self.session.query(func.count(Ticket.id)))
            .filter(visible, Ticket.status == TicketStatus.WAITING_REVIEW.value)
            .scalar()
            or 0
        )
        waiting_customer = (
            scoped(self.session.query(func.count(Ticket.id)))
            .filter(visible, Ticket.status == TicketStatus.NEED_CUSTOMER_INFO.value)
            .scalar()
            or 0
        )
        waiting_reply = (
            scoped(self.session.query(func.count(Ticket.id)))
            .filter(visible, Ticket.status == TicketStatus.READY_TO_REPLY.value)
            .scalar()
            or 0
        )
        resolved_today = (
            scoped(self.session.query(func.count(Ticket.id)))
            .filter(
                visible,
                Ticket.status == TicketStatus.RESOLVED.value,
                Ticket.updated_at >= day_start,
            )
            .scalar()
            or 0
        )
        # SLA 风险是页面排序辅助，不替代租户级 SLA 配置；精确到期时间由 ticket_view 统一计算。
        from app.services.ticket_view import sla_snapshot

        active_rows = (
            scoped(self.session.query(Ticket))
            .filter(visible, ~Ticket.status.in_(["resolved", "cancelled"]))
            .all()
        )
        at_risk = sum(1 for row in active_rows if sla_snapshot(row)["status"] in {"at_risk", "overdue"})
        approved = (
            scoped(
                self.session.query(func.count(Recommendation.id)).join(
                    Ticket, Ticket.id == Recommendation.ticket_id
                )
            )
            .filter(visible, Recommendation.status == RecommendationStatus.APPROVED.value)
            .scalar()
            or 0
        )
        decided = (
            scoped(
                self.session.query(func.count(Recommendation.id)).join(
                    Ticket, Ticket.id == Recommendation.ticket_id
                )
            )
            .filter(
                visible,
                Recommendation.status.in_(
                    [
                        RecommendationStatus.APPROVED.value,
                        RecommendationStatus.REJECTED.value,
                        RecommendationStatus.SENT.value,
                    ]
                ),
            )
            .scalar()
            or 0
        )
        return {
            "today_new": today,
            "pending": pending,
            "pending_high_priority": high_priority,
            "waiting_review": waiting_review,
            "waiting_customer": waiting_customer,
            "waiting_reply": waiting_reply,
            "at_risk": at_risk,
            "resolved_today": resolved_today,
            "excluded_test_records": excluded_test_records,
            "avg_first_response_minutes": self._avg_first_response_minutes(
                tenant_id,
                assignee_id=assignee_id,
                unassigned=unassigned,
            ),
            "ai_acceptance_rate": round(approved / decided, 4) if decided else None,
            "ai_decided_count": decided,
            "issue_type_options": [item.value for item in IssueType],
            "status_options": [item.value for item in TicketStatus],
        }

    def _avg_first_response_minutes(
        self,
        tenant_id: str,
        *,
        assignee_id: str | None = None,
        unassigned: bool = False,
    ) -> float | None:
        """平均首次响应：工单创建 → 第一条客服消息（AI 草稿不算，必须是人发出去的）。"""
        query = (
            self.session.query(Ticket.created_at, func.min(TicketMessage.created_at))
            .join(TicketMessage, TicketMessage.ticket_id == Ticket.id)
            .filter(
                Ticket.tenant_id == tenant_id,
                ~_test_ticket_expression(),
                TicketMessage.sender_type == MessageSender.STAFF.value,
            )
        )
        if unassigned:
            query = query.filter(Ticket.assignee_id.is_(None))
        elif assignee_id:
            query = query.filter(Ticket.assignee_id == assignee_id)
        rows = query.group_by(Ticket.id).all()
        deltas = [
            (first - created).total_seconds()
            for created, first in rows
            if created and first and (first - created).total_seconds() >= 0
        ]
        if not deltas:
            return None
        return round(sum(deltas) / len(deltas) / 60, 2)

    # ------------------------------------------------------------------ 事实过期
    def stale_facts(self, ticket_id: str, *, ttl_seconds: int) -> list[OrderFact]:
        """返回已过期的事实（§4.3：接口过期必须进人工，不能拿旧事实下结论）。"""
        deadline = utcnow() - timedelta(seconds=ttl_seconds)
        return [row for row in self.facts(ticket_id) if row.observed_at and row.observed_at < deadline]
