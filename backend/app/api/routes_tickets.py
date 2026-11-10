"""售后工单接口（需求书 §6 API 契约）。

    POST /tickets                          创建工单（external_id / idempotency_key 幂等）
    GET  /tickets                           工单列表（状态/类型/优先级/负责人/关键词筛选 + 分页）
    GET  /tickets/stats                     工作台指标卡
    GET  /tickets/state-machine             状态机全貌（前端据此渲染允许的操作）
    GET  /tickets/{id}                      工单详情（消息 + 事实行 + 建议 + 时间线 + Agent 步骤）
    POST /tickets/{id}/analyze              启动分析（返回 run_id，进度走 SSE）
    POST /tickets/{id}/messages             客服发送回复 / 内部备注
    POST /tickets/{id}/assign               转派
    POST /tickets/{id}/transition           通用状态迁移（后端校验合法性）
    GET  /tickets/{id}/recommendation       当前处理建议
    POST /recommendations/{id}/decision     审批（批准 / 驳回，可修改后通过，幂等）
    POST /recommendations/{id}/send         发送已批准回复

写权限的边界：`ticket:approve` 才允许审批（需求书 §3.3 的 L3/L4），
`ticket:reply` 才允许发送回复 —— 权限判断放在这里，不交给 Agent。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.deps import Pagination, Principal, get_current_principal, get_pagination
from app.config import get_settings
from app.db.session import get_db
from app.models.base import utcnow
from app.models.enums import (
    ApprovalRequirement,
    MessageSender,
    RecommendationStatus,
    TicketStatus,
)
from app.models.ticket import Recommendation, ReplyTemplate, Ticket, TicketMessage
from app.models.workflow import AnalysisTask
from app.schemas.common import ListResponse, PageMeta
from app.schemas.ticket import (
    AssignmentRecommendationsOut,
    FactOut,
    MessageOut,
    RecommendationDecisionRequest,
    RecommendationSendRequest,
    RecommendationOut,
    ReplyTemplateCreateRequest,
    ReplyTemplateOut,
    ReplyTemplateUpdateRequest,
    TicketMentionOut,
    TicketAssignRequest,
    TicketCreateRequest,
    TicketDetailOut,
    TicketOut,
    TicketOrderUpdateRequest,
    TicketReplyRequest,
    TicketStateMachineOut,
    TicketStatsOut,
    TicketTransitionRequest,
)
from app.services.audit import record_audit
from app.services.csv_export import spreadsheet_safe_text
from app.services.ticket_risk import approval_for_tier, auto_draft_allowed
from app.services.ticket_service import (
    RecommendationNotFound,
    TicketNotFound,
    TicketService,
    ticket_data_origin,
)
from app.services.ticket_state import InvalidTransition, describe, TERMINAL
from app.services.ticket_view import (
    answerability,
    build_handoff_summary,
    build_analysis,
    build_fact_rows,
    build_timeline,
    customer_reply_for_display,
    handler_label_for_ticket,
    next_action_for_ticket,
    sla_snapshot,
    validate_customer_reply_for_send,
)


TICKET_PRIORITY_LABELS = {
    "low": "低",
    "medium": "中",
    "high": "高",
    "urgent": "紧急",
}
TICKET_STATUS_LABELS = {
    "new": "待处理",
    "enriching": "补充订单信息",
    "analyzing": "核实处理中",
    "waiting_review": "等待主管审批",
    "ready_to_reply": "等待客服回复",
    "resolved": "已解决",
    "need_customer_info": "等待客户补充",
    "escalated": "已升级",
    "failed": "处理失败",
    "cancelled": "已取消",
}

router = APIRouter(tags=["售后工单"])

# 建议里这些动作会动钱/动物流：未经批准一律不允许执行（需求书 §1.2 / §4.3）
WRITE_ACTIONS = {"refund", "return_goods", "exchange", "reship", "compensate", "coupon"}


@router.get("/ticket-templates", response_model=list[ReplyTemplateOut], summary="常用回复模板")
def list_ticket_templates(
    issue_type: str | None = Query(default=None, max_length=32),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> list[ReplyTemplateOut]:
    """返回当前租户启用模板；没有自定义模板时由前端使用内置模板兜底。"""
    principal.require("ticket:read")
    query = session.query(ReplyTemplate).filter(
        ReplyTemplate.tenant_id == principal.tenant_id,
        ReplyTemplate.active.is_(True),
    )
    if issue_type:
        query = query.filter(or_(ReplyTemplate.issue_type == issue_type, ReplyTemplate.issue_type.is_(None)))
    rows = query.order_by(ReplyTemplate.updated_at.desc(), ReplyTemplate.title.asc()).all()
    return [ReplyTemplateOut.model_validate(row) for row in rows]


@router.post("/ticket-templates", response_model=ReplyTemplateOut, status_code=status.HTTP_201_CREATED, summary="新建回复模板")
def create_ticket_template(
    payload: ReplyTemplateCreateRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> ReplyTemplateOut:
    principal.require("ticket:template_manage")
    row = ReplyTemplate(
        tenant_id=principal.tenant_id,
        title=payload.title.strip(),
        content=payload.content.strip(),
        issue_type=payload.issue_type,
        active=True,
        created_by=principal.user.id,
        updated_by=principal.user.id,
    )
    session.add(row)
    session.flush()
    record_audit(
        session,
        action="ticket_template_created",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        resource_type="ticket_template",
        resource_id=row.id,
        detail={"title": row.title, "issue_type": row.issue_type},
        commit=True,
    )
    return ReplyTemplateOut.model_validate(row)


@router.patch("/ticket-templates/{template_id}", response_model=ReplyTemplateOut, summary="修改回复模板")
def update_ticket_template(
    template_id: str,
    payload: ReplyTemplateUpdateRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> ReplyTemplateOut:
    principal.require("ticket:template_manage")
    row = (
        session.query(ReplyTemplate)
        .filter(ReplyTemplate.id == template_id, ReplyTemplate.tenant_id == principal.tenant_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "template_not_found", "message": "回复模板不存在"})
    before = {"title": row.title, "issue_type": row.issue_type, "active": row.active}
    for field in ("title", "content", "issue_type", "active"):
        if field not in payload.model_fields_set:
            continue
        value = getattr(payload, field)
        setattr(row, field, value.strip() if isinstance(value, str) else value)
    row.updated_by = principal.user.id
    session.flush()
    record_audit(
        session,
        action="ticket_template_updated",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        resource_type="ticket_template",
        resource_id=row.id,
        detail={"before": before, "after": {"title": row.title, "issue_type": row.issue_type, "active": row.active}},
        commit=True,
    )
    return ReplyTemplateOut.model_validate(row)


@router.delete("/ticket-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT, summary="停用回复模板")
def delete_ticket_template(
    template_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> None:
    principal.require("ticket:template_manage")
    row = (
        session.query(ReplyTemplate)
        .filter(ReplyTemplate.id == template_id, ReplyTemplate.tenant_id == principal.tenant_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "template_not_found", "message": "回复模板不存在"})
    row.active = False
    row.updated_by = principal.user.id
    record_audit(
        session,
        action="ticket_template_deactivated",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        resource_type="ticket_template",
        resource_id=row.id,
        commit=True,
    )


@router.get("/ticket-mentions", response_model=list[TicketMentionOut], summary="待我关注的工单协作")
def list_ticket_mentions(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[TicketMentionOut]:
    principal.require("ticket:read")
    rows = (
        session.query(TicketMessage, Ticket)
        .join(Ticket, Ticket.id == TicketMessage.ticket_id)
        .filter(
            Ticket.tenant_id == principal.tenant_id,
            TicketMessage.sender_type == MessageSender.STAFF.value,
            TicketMessage.visible_to_customer.is_(False),
        )
        .order_by(TicketMessage.created_at.desc())
        .limit(200)
        .all()
    )
    items: list[TicketMentionOut] = []
    for message, ticket in rows:
        meta = message.meta or {}
        mention_ids = [str(item) for item in (meta.get("mention_user_ids") or [])]
        if principal.user.id not in mention_ids:
            continue
        read_by = {str(item) for item in (meta.get("read_by") or [])}
        is_read = principal.user.id in read_by
        if unread_only and is_read:
            continue
        items.append(
            TicketMentionOut(
                id=message.id,
                ticket_id=ticket.id,
                ticket_external_id=ticket.external_id,
                subject=ticket.subject,
                content=message.content,
                actor_id=message.actor_id,
                created_at=message.created_at,
                read=is_read,
                mention_user_ids=mention_ids,
            )
        )
        if len(items) >= limit:
            break
    return items


@router.post("/ticket-mentions/{message_id}/read", response_model=TicketMentionOut, summary="标记工单协作已读")
def mark_ticket_mention_read(
    message_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TicketMentionOut:
    principal.require("ticket:read")
    row = (
        session.query(TicketMessage, Ticket)
        .join(Ticket, Ticket.id == TicketMessage.ticket_id)
        .filter(
            TicketMessage.id == message_id,
            Ticket.tenant_id == principal.tenant_id,
            TicketMessage.sender_type == MessageSender.STAFF.value,
            TicketMessage.visible_to_customer.is_(False),
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "mention_not_found", "message": "待关注消息不存在"})
    message, ticket = row
    meta = dict(message.meta or {})
    mention_ids = [str(item) for item in (meta.get("mention_user_ids") or [])]
    if principal.user.id not in mention_ids:
        raise HTTPException(status_code=404, detail={"code": "mention_not_found", "message": "待关注消息不存在"})
    read_by = list(dict.fromkeys(str(item) for item in (meta.get("read_by") or [])))
    if principal.user.id not in read_by:
        read_by.append(principal.user.id)
        meta["read_by"] = read_by
        message.meta = meta
        record_audit(
            session,
            action="ticket_mention_read",
            tenant_id=principal.tenant_id,
            actor_id=principal.user.id,
            resource_type="ticket_message",
            resource_id=message.id,
            detail={"ticket_id": ticket.id},
            commit=True,
        )
    return TicketMentionOut(
        id=message.id,
        ticket_id=ticket.id,
        ticket_external_id=ticket.external_id,
        subject=ticket.subject,
        content=message.content,
        actor_id=message.actor_id,
        created_at=message.created_at,
        read=True,
        mention_user_ids=mention_ids,
    )


# --------------------------------------------------------------------------- 工具
def _ticket_out(service: TicketService, ticket, quality_summary: dict | None = None) -> TicketOut:
    from app.services.ticket_state import allowed_transitions

    payload = TicketOut.model_validate(ticket)
    payload.data_origin = ticket_data_origin(ticket)
    payload.allowed_transitions = allowed_transitions(ticket.status)
    sla = sla_snapshot(ticket)
    payload.sla_due_at = sla["due_at"]
    payload.sla_remaining_seconds = sla["remaining_seconds"]
    payload.sla_status = sla["status"]
    payload.next_action = next_action_for_ticket(ticket)
    payload.handler_label = handler_label_for_ticket(ticket)
    # 列表优先读取工单上的当前摘要；内部消息保留完整历史，旧记录在迁移未回填时回退读取。
    if quality_summary is None and getattr(ticket, "quality_reviewed_at", None):
        quality_summary = {
            "quality_status": ticket.quality_status or "unreviewed",
            "quality_issue_codes": list(ticket.quality_issue_codes or []),
            "quality_reviewed_at": ticket.quality_reviewed_at,
            "quality_reviewer_id": ticket.quality_reviewer_id,
        }
    if quality_summary is None:
        quality_message = (
            service.session.query(TicketMessage)
            .filter(
                TicketMessage.ticket_id == ticket.id,
                TicketMessage.visible_to_customer.is_(False),
            )
            .order_by(TicketMessage.created_at.desc())
            .limit(30)
            .all()
        )
        latest_quality = next(
            (message for message in quality_message if (message.meta or {}).get("message_type") == "quality_review"),
            None,
        )
        if latest_quality:
            meta = latest_quality.meta or {}
            quality_summary = {
                "quality_status": str(meta.get("quality_result") or "unreviewed"),
                "quality_issue_codes": [str(item) for item in (meta.get("quality_issue_codes") or [])],
                "quality_reviewed_at": latest_quality.created_at,
                "quality_reviewer_id": latest_quality.actor_id,
            }
    if quality_summary:
        payload.quality_status = quality_summary.get("quality_status", "unreviewed")
        payload.quality_issue_codes = quality_summary.get("quality_issue_codes", [])
        payload.quality_reviewed_at = quality_summary.get("quality_reviewed_at")
        payload.quality_reviewer_id = quality_summary.get("quality_reviewer_id")
    return payload


def _recommendation_out(row: Recommendation | None) -> RecommendationOut | None:
    if row is None:
        return None
    payload = RecommendationOut.model_validate(row)
    labels = {
        "none": "无需审批（客服发送前确认）",
        "staff_confirm": "客服确认后执行",
        "supervisor": "需主管审批",
        "risk_control": "转风控，禁止自动执行",
    }
    payload.approval_label = labels.get(payload.approval_requirement)
    payload.auto_draft_allowed = auto_draft_allowed(payload.risk_tier)
    # 注意 staff_confirm（L2）不算"被阻塞"：它的口径是客服确认后即可执行，
    # 前端据此把按钮文案换成「客服确认并发送」并把确认动作记进审计。
    # 若这里一律置灰，L2 的写操作建议就永远发不出去（踩过）。
    payload.write_action_blocked = (
        payload.action_type in WRITE_ACTIONS
        and payload.status
        not in (RecommendationStatus.APPROVED.value, RecommendationStatus.SENT.value)
        and payload.approval_requirement != ApprovalRequirement.STAFF_CONFIRM.value
    )
    gate = answerability(
        missing_information=payload.missing_information,
        approval_requirement=payload.approval_requirement,
        status=payload.status,
        evidence_sufficient=bool(payload.policy_refs),
    )
    payload.answerability_code = gate["code"]
    payload.answerability_label = gate["label"]
    payload.answerability_reason = gate["reason"]
    payload.reply_commitment_blocked = gate["code"] == "human_review"
    payload.customer_reply = customer_reply_for_display(
        payload.customer_reply,
        approval_requirement=payload.approval_requirement,
        status=payload.status,
    )
    return payload


def _load_ticket(service: TicketService, ticket_id: str, principal: Principal):
    try:
        return service.get(ticket_id, tenant_id=principal.tenant_id)
    except TicketNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": f"工单不存在: {ticket_id}"},
        ) from exc


# --------------------------------------------------------------------------- 工单
@router.post("/tickets", response_model=TicketOut, status_code=status.HTTP_201_CREATED, summary="创建工单")
def create_ticket(
    payload: TicketCreateRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TicketOut:
    principal.require("ticket:create")
    if payload.data_origin != "business" and get_settings().app_env == "prod":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "non_business_ticket_origin_forbidden",
                "message": "生产环境只允许创建业务工单",
            },
        )
    service = TicketService(session)
    ticket, reused = service.create(
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        subject=payload.subject,
        content=payload.content,
        external_id=payload.external_id,
        customer_id=payload.customer_id,
        order_id=payload.order_id,
        channel=payload.channel,
        priority=payload.priority,
        issue_type=payload.issue_type,
        shop_id=payload.shop_id,
        platform=payload.platform,
        attributes=payload.attributes,
        data_origin=payload.data_origin,
        idempotency_key=payload.idempotency_key,
    )
    out = _ticket_out(service, ticket)
    if reused:
        # 幂等命中：告诉调用方"你没创建第二张单"，便于排查渠道重复推送
        record_audit(
            session,
            action="ticket_create_idempotent_hit",
            tenant_id=principal.tenant_id,
            actor_id=principal.user.id,
            resource_type="ticket",
            resource_id=ticket.id,
            detail={"external_id": payload.external_id, "idempotency_key": payload.idempotency_key},
            commit=True,
        )
    return out


@router.get("/tickets", response_model=ListResponse[TicketOut], summary="工单列表")
def list_tickets(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    pagination: Pagination = Depends(get_pagination),
    status_filter: list[str] | None = Query(default=None, alias="status"),
    issue_type: list[str] | None = Query(default=None),
    priority: list[str] | None = Query(default=None),
    assignee_id: str | None = Query(default=None),
    mine: bool = Query(default=False, description="只看分配给我的"),
    scope: str = Query(default="all", pattern="^(all|mine|unassigned)$"),
    attention: str | None = Query(
        default=None,
        pattern="^(at_risk|waiting_reply|waiting_customer|waiting_review|resolved_today)$",
        description="业务处理卡对应的行动队列",
    ),
    keyword: str | None = Query(default=None, max_length=64),
    include_test: bool = Query(default=False, description="是否包含自动化测试工单"),
) -> ListResponse[TicketOut]:
    principal.require("ticket:read")
    service = TicketService(session)
    # mine 兼容旧客户端；scope=mine 时永远使用令牌主体，忽略客户端 assignee_id。
    effective_scope = "mine" if mine else scope
    rows, total = service.list_tickets(
        tenant_id=principal.tenant_id,
        statuses=status_filter,
        issue_types=issue_type,
        priorities=priority,
        assignee_id=principal.user.id if effective_scope == "mine" else assignee_id,
        unassigned=effective_scope == "unassigned",
        attention=attention,
        keyword=keyword,
        include_test=include_test,
        limit=pagination.page_size,
        offset=pagination.offset,
    )
    return ListResponse[TicketOut](
        items=[_ticket_out(service, row) for row in rows],
        meta=PageMeta(total=total, page=pagination.page, page_size=pagination.page_size),
    )


@router.get("/tickets/quality-reviews", response_model=ListResponse[TicketOut], summary="质检工单查询")
def list_quality_reviews(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    pagination: Pagination = Depends(get_pagination),
    quality_status: str | None = Query(default=None, pattern="^(unreviewed|pass|needs_followup|block)$"),
    quality_issue_code: str | None = Query(default=None, max_length=64),
    keyword: str | None = Query(default=None, max_length=64),
) -> ListResponse[TicketOut]:
    principal.require("ticket:quality_review")
    service = TicketService(session)
    rows, total, summaries = service.list_quality_tickets(
        tenant_id=principal.tenant_id,
        quality_status=quality_status,
        quality_issue_code=quality_issue_code,
        keyword=keyword,
        limit=pagination.page_size,
        offset=pagination.offset,
    )
    return ListResponse[TicketOut](
        items=[_ticket_out(service, row, summaries.get(row.id)) for row in rows],
        meta=PageMeta(total=total, page=pagination.page, page_size=pagination.page_size),
    )


@router.get("/tickets/quality-reviews/export", summary="导出质检结果")
def export_quality_reviews(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    quality_status: str | None = Query(default=None, pattern="^(unreviewed|pass|needs_followup|block)$"),
    quality_issue_code: str | None = Query(default=None, max_length=64),
    keyword: str | None = Query(default=None, max_length=64),
) -> Response:
    """导出当前筛选下的质检结果，并把导出行为写入审计日志。

    单次导出限制 5000 条，避免把后台查询和浏览器下载变成不可控的大任务；
    超过上限时提示用户缩小筛选范围，而不是静默截断。
    """
    principal.require("ticket:quality_review")
    import csv
    import io

    service = TicketService(session)
    rows, total, summaries = service.list_quality_tickets(
        tenant_id=principal.tenant_id,
        quality_status=quality_status,
        quality_issue_code=quality_issue_code,
        keyword=keyword,
        limit=5000,
        offset=0,
    )
    if total > 5000:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "quality_export_too_large", "message": "筛选结果超过 5000 条，请缩小筛选条件后再导出"},
        )

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["工单号", "问题摘要", "订单号", "优先级", "当前状态", "质检结论", "问题标记", "质检时间"])
    labels = {
        "unreviewed": "未抽查",
        "pass": "抽查通过",
        "needs_followup": "需要补证据",
        "block": "阻断发送",
    }
    issue_labels = {
        "missing_evidence": "事实证据不足",
        "wrong_policy": "政策依据不匹配",
        "unsafe_reply": "回复存在承诺风险",
        "wrong_action": "建议动作不合适",
        "unclear_next_step": "下一步不清晰",
    }
    for ticket in rows:
        summary = summaries.get(ticket.id, {})
        quality = summary.get("quality_status", "unreviewed")
        issues = summary.get("quality_issue_codes", [])
        writer.writerow(
            [spreadsheet_safe_text(value) for value in [
                ticket.external_id or ticket.id,
                ticket.subject,
                ticket.order_id or "",
                TICKET_PRIORITY_LABELS.get(ticket.priority, ticket.priority),
                TICKET_STATUS_LABELS.get(ticket.status, ticket.status),
                labels.get(quality, quality),
                "、".join(issue_labels.get(code, code) for code in issues),
                summary.get("quality_reviewed_at") or "",
            ]]
        )
    record_audit(
        session,
        action="ticket_quality_export",
        tenant_id=principal.tenant_id,
        actor_id=principal.user.id,
        actor_name=principal.user.username,
        resource_type="ticket_quality_review",
        detail={
            "count": total,
            "quality_status": quality_status,
            "quality_issue_code": quality_issue_code,
            "keyword": keyword,
        },
        commit=True,
    )
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=after-sales-quality-review.csv"},
    )


@router.get("/tickets/stats", response_model=TicketStatsOut, summary="工作台指标卡")
def ticket_stats(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    scope: str = Query(default="all", pattern="^(all|mine|unassigned)$"),
) -> TicketStatsOut:
    principal.require("ticket:read")
    return TicketStatsOut(
        **TicketService(session).stats(
            tenant_id=principal.tenant_id,
            assignee_id=principal.user.id if scope == "mine" else None,
            unassigned=scope == "unassigned",
        )
    )


@router.get("/tickets/assignees", summary="可转派的处理人列表")
def list_assignees(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    """转派弹窗的候选人：本租户内**真正能处理工单**的账号。

    为什么要单独一个接口而不是复用 `GET /auth/users`：那个接口要求 `admin:manage`
    （只有管理员能列全部账号），而"转派"是客服自己就要做的事。踩过的坑：
    弹窗写着"选择负责人"却没有候选列表，前端只能固定提交 `assignee_id: null` ——
    结果负责人列永远是"未分配"，转派成了一个只写审计的空操作。
    """
    principal.require("ticket:assign")
    from app.auth.rbac import can_access
    from app.models.identity import User

    users = (
        session.query(User)
        .filter(
            User.tenant_id == principal.tenant_id,
            # 停用账号不能再接新工单，即使它仍保留历史角色权限。
            # 否则转派弹窗会把工单送到无法登录处理的账号。
            User.is_active.is_(True),
        )
        .order_by(User.username)
        .all()
    )
    items = [
        {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "department": user.department,
            # 能不能审批决定了"要不要把 L3 工单转给他"
            "can_approve": can_access(user.role_codes, "ticket:approve"),
        }
        for user in users
        if can_access(user.role_codes, "ticket:reply") or can_access(user.role_codes, "ticket:approve")
    ]
    return {"items": items, "total": len(items)}


@router.get(
    "/tickets/{ticket_id}/assignment-recommendations",
    response_model=AssignmentRecommendationsOut,
    summary="派单负责人推荐（只读）",
)
def assignment_recommendations(
    ticket_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> AssignmentRecommendationsOut:
    principal.require("ticket:assign")
    service = TicketService(session)
    ticket = _load_ticket(service, ticket_id, principal)
    return AssignmentRecommendationsOut(**service.assignment_recommendations(ticket))


@router.get("/tickets/state-machine", response_model=TicketStateMachineOut, summary="状态机全貌")
def ticket_state_machine(
    principal: Principal = Depends(get_current_principal),
) -> TicketStateMachineOut:
    principal.require("ticket:read")
    return TicketStateMachineOut(transitions=describe(), terminal=sorted(TERMINAL))


@router.get("/tickets/{ticket_id}", response_model=TicketDetailOut, summary="工单详情")
def ticket_detail(
    ticket_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TicketDetailOut:
    principal.require("ticket:read")
    settings = get_settings()
    service = TicketService(session)
    ticket = _load_ticket(service, ticket_id, principal)

    messages = service.messages(ticket.id)
    facts = service.facts(ticket.id)
    recommendation = service.latest_recommendation(ticket.id)
    audits = _recent_audits(session, ticket.id)
    analysis_task = (
        session.get(AnalysisTask, ticket.last_analysis_task_id)
        if ticket.last_analysis_task_id
        else None
    )

    fact_rows = build_fact_rows(facts, ttl_seconds=settings.ticket_fact_ttl_seconds)
    now = utcnow()
    fact_items = [
        FactOut.model_validate(row).model_copy(
            update={
                "stale": bool(
                    row.observed_at
                    and (now - row.observed_at).total_seconds() > settings.ticket_fact_ttl_seconds
                )
            }
        )
        for row in facts
    ]
    # 详情页也带上成本：需求书 §8 要求可观测 Token/延迟/成本，
    # 而"这一单花了多少钱"正是运维与业务方最常问的数字。
    from app.services.ticket_observability import ticket_cost

    return TicketDetailOut(
        ticket=_ticket_out(service, ticket),
        messages=[MessageOut.model_validate(row) for row in messages],
        facts=fact_items,
        recommendation=_recommendation_out(recommendation),
        fact_rows=fact_rows,
        timeline=build_timeline(ticket, messages, audits=audits),
        analysis=build_analysis(
            ticket,
            recommendation,
            agent_runs=_agent_steps(session, ticket),
            task_filters=dict(analysis_task.filters or {}) if analysis_task else None,
            task_status=analysis_task.status if analysis_task else None,
            can_view_technical=principal.can("admin:manage") or "engineer" in principal.roles,
        ),
        handoff_summary=build_handoff_summary(ticket, recommendation, fact_rows=fact_rows),
        cost=ticket_cost(session, ticket) if principal.can("admin:manage") or "engineer" in principal.roles else None,
    )


def _recent_audits(session: Session, ticket_id: str) -> list[dict]:
    from app.models.workflow import AuditLog

    rows = (
        session.query(AuditLog)
        .filter(AuditLog.resource_type.in_(["ticket", "recommendation"]), AuditLog.resource_id == ticket_id)
        .order_by(AuditLog.created_at.asc())
        .limit(50)
        .all()
    )
    return [
        {
            "action": row.action,
            "detail": row.detail or {},
            "at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
    ]


def _agent_steps(session: Session, ticket) -> list[dict]:
    """最近一次分析的 Agent 步骤（原型右栏 4 行；真实状态来自 agent_runs）。"""
    if not ticket.last_analysis_task_id:
        return []
    from app.models.workflow import AgentRun

    rows = (
        session.query(AgentRun)
        .filter(AgentRun.task_id == ticket.last_analysis_task_id)
        .order_by(AgentRun.created_at.asc())
        .all()
    )
    label = {
        "triage_agent": ("分", "问题分类 Agent"),
        "order_fact_agent": ("单", "订单查询 Agent"),
        "policy_rag_agent": ("知", "政策检索 Agent"),
        "review_agent": ("审", "风险与质检 Agent"),
        "resolution_agent": ("案", "方案生成 Agent"),
        "response_agent": ("复", "回复草稿 Agent"),
        "followup_agent": ("跟", "跟进 Agent"),
        "revision_agent": ("改", "回复修订 Agent"),
    }
    steps: list[dict] = []
    for row in rows:
        short, name = label.get(row.agent_code, (row.agent_code[:1], row.agent_code))
        # `AgentRun.error` 可能是字符串（旧工作流写入）也可能是 dict（新节点写入），两种都要能渲染
        error_value = row.error
        if isinstance(error_value, dict):
            error_code = error_value.get("code") or error_value.get("message")
        else:
            error_code = error_value or None
        steps.append(
            {
                "agent_code": row.agent_code,
                "short": short,
                "name": name,
                "status": row.status,
                "model": row.model,
                "latency_ms": row.latency_ms,
                "total_tokens": row.total_tokens,
                "attempt": row.attempt,
                "error": error_code,
                "reflection": ((row.output or {}).get("structured") or {}),
            }
        )
    return steps


@router.get("/tickets/{ticket_id}/cost", summary="本单成本（累计 + 单次均值 + 批量推算）")
def ticket_cost_endpoint(
    ticket_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    daily_volume: int = Query(default=0, ge=0, le=1000000, description="按该日单量推算批量开销"),
) -> dict:
    """每单成本。`daily_volume > 0` 时附带批量场景推算（评审"能不能用在高频"时的依据）。"""
    principal.require("ticket:read")
    if not (principal.can("admin:manage") or "engineer" in principal.roles):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "technical_observability_forbidden", "message": "成本与 Token 明细仅对管理员和工程人员开放"},
        )
    from app.services.ticket_observability import estimate_batch_cost, ticket_cost

    service = TicketService(session)
    ticket = _load_ticket(service, ticket_id, principal)
    payload = ticket_cost(session, ticket)
    if daily_volume:
        payload["batch_estimate"] = estimate_batch_cost(
            payload["amount_per_analysis"], daily_volume
        )
    return payload


@router.get("/runs/{run_id}/events", summary="分析运行事件流（SSE）")
def run_events(
    run_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    max_seconds: float = Query(default=120.0, ge=1.0, le=600.0),
):
    """SSE 事件流（需求书 §6：前端用它渲染节点进度）。

    鉴权在建立连接时完成（`ticket:read`），随后按 `analysis_events` 增量推送。
    """
    principal.require("ticket:read")
    from fastapi.responses import StreamingResponse

    from app.db.session import SessionLocal
    from app.services.ticket_observability import event_stream

    task = session.get(AnalysisTask, run_id)
    if task is None or task.tenant_id != principal.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "run_not_found", "message": f"运行不存在: {run_id}"},
        )
    business_view = bool((task.filters or {}).get("ticket_id")) and not (
        principal.can("admin:manage") or "engineer" in principal.roles
    )
    return StreamingResponse(
        event_stream(SessionLocal, run_id, max_seconds=max_seconds, business_view=business_view),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/tickets/{ticket_id}/analyze", status_code=status.HTTP_202_ACCEPTED, summary="启动分析")
def analyze_ticket(
    ticket_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> dict:
    """启动一次分析。返回 run_id（= 分析任务 ID），进度通过 SSE 获取（需求书 §6）。"""
    principal.require("ticket:reply")
    service = TicketService(session)
    ticket = _load_ticket(service, ticket_id, principal)

    from app.services.ticket_analysis import FactIncomplete, start_analysis

    try:
        run_id = start_analysis(session, ticket=ticket, user=principal.user)
    except FactIncomplete as exc:
        # 缺事实 → 明确告知缺什么（前端据此提示客服向客户索取），而不是假装在分析
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc), "missing": exc.missing},
        ) from exc
    return {"ticket_id": ticket.id, "run_id": run_id, "status": ticket.status}


@router.patch("/tickets/{ticket_id}/order", response_model=TicketOut, summary="补录订单号")
def update_ticket_order_id(
    ticket_id: str,
    payload: TicketOrderUpdateRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TicketOut:
    principal.require("ticket:reply")
    order_id = payload.order_id.strip()
    if not order_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "order_id_required", "message": "请填写订单号"},
        )
    service = TicketService(session)
    ticket = _load_ticket(service, ticket_id, principal)
    service.update_order_id(ticket, order_id=order_id, actor_id=principal.user.id)
    return _ticket_out(service, ticket)


@router.post("/tickets/{ticket_id}/messages", response_model=MessageOut, summary="发送回复/备注")
def add_ticket_message(
    ticket_id: str,
    payload: TicketReplyRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> MessageOut:
    if payload.quality_review:
        principal.require("ticket:quality_review")
        if payload.message_type != "internal_note" or payload.visible_to_customer:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "quality_review_must_be_internal", "message": "质检结果只能保存为内部记录"},
            )
        if not payload.quality_result:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "quality_result_required", "message": "请选择质检结论"},
            )
    else:
        principal.require("ticket:reply")
    service = TicketService(session)
    ticket = _load_ticket(service, ticket_id, principal)
    content = payload.content.strip()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "empty_message", "message": "消息内容不能为空"},
        )
    is_internal_note = payload.message_type == "internal_note" or not payload.visible_to_customer
    mention_ids = list(dict.fromkeys(payload.mention_user_ids))
    if mention_ids:
        from app.auth.rbac import can_access
        from app.models.identity import User

        mentioned = session.query(User).filter(User.id.in_(mention_ids), User.tenant_id == principal.tenant_id).all()
        mentioned_by_id = {user.id: user for user in mentioned}
        invalid_mentions = [
            user_id
            for user_id in mention_ids
            if user_id not in mentioned_by_id
            or not mentioned_by_id[user_id].is_active
            or not can_access(mentioned_by_id[user_id].role_codes, "ticket:read")
        ]
        if invalid_mentions:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={"code": "invalid_mentions", "message": "被 @ 的协作者必须是本租户内启用的工单用户"},
            )
    meta = {
        "message_type": "quality_review" if payload.quality_review else ("internal_note" if is_internal_note else "reply"),
        "mention_user_ids": mention_ids,
    }
    if payload.quality_review:
        ticket.quality_status = payload.quality_result
        ticket.quality_issue_codes = list(dict.fromkeys(payload.quality_issue_codes))
        ticket.quality_reviewed_at = utcnow()
        ticket.quality_reviewer_id = principal.user.id
        meta.update({
            "quality_result": payload.quality_result,
            "quality_issue_codes": list(dict.fromkeys(payload.quality_issue_codes)),
        })
    message = service.add_message(
        ticket,
        content=content,
        sender_type=MessageSender.STAFF.value,
        actor_id=principal.user.id,
        visible_to_customer=not is_internal_note,
        meta=meta,
    )
    # 首次人工回复即视为"已响应"：把草稿就绪的工单推进到已解决（状态机允许）
    if not is_internal_note and ticket.status == TicketStatus.READY_TO_REPLY.value:
        service.transition(
            ticket,
            TicketStatus.RESOLVED.value,
            actor_id=principal.user.id,
            reason="客服已发送回复",
        )
    return MessageOut.model_validate(message)


@router.post("/tickets/{ticket_id}/assign", response_model=TicketOut, summary="转派")
def assign_ticket(
    ticket_id: str,
    payload: TicketAssignRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TicketOut:
    principal.require("ticket:assign")
    service = TicketService(session)
    ticket = _load_ticket(service, ticket_id, principal)
    if payload.assignee_id:
        from app.auth.rbac import can_access
        from app.models.identity import User

        assignee = session.get(User, payload.assignee_id)
        eligible = bool(
            assignee
            and assignee.tenant_id == principal.tenant_id
            and assignee.is_active
            and (
                can_access(assignee.role_codes, "ticket:reply")
                or can_access(assignee.role_codes, "ticket:approve")
            )
        )
        if not eligible:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "invalid_ticket_assignee",
                    "message": "负责人必须是本租户内可处理工单的启用账号",
                },
            )
    service.assign(
        ticket,
        assignee_id=payload.assignee_id,
        actor_id=principal.user.id,
        reason=payload.reason,
    )
    return _ticket_out(service, ticket)


@router.post("/tickets/{ticket_id}/transition", response_model=TicketOut, summary="状态迁移")
def transition_ticket(
    ticket_id: str,
    payload: TicketTransitionRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> TicketOut:
    # 权限按"目标状态"区分，而不是一律要求 ticket:reply：
    # 主管（reviewer）有 ticket:approve 但没有 ticket:reply，若一律要求 reply，
    # 主管就无法升级/关闭工单 —— 这是权限模型与岗位职责对不上。
    if payload.target in (TicketStatus.ESCALATED.value, TicketStatus.RESOLVED.value):
        principal.require("ticket:approve")
    else:
        principal.require("ticket:reply")
    service = TicketService(session)
    ticket = _load_ticket(service, ticket_id, principal)
    try:
        service.transition(
            ticket,
            payload.target,
            actor_id=principal.user.id,
            reason=payload.reason,
        )
    except InvalidTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return _ticket_out(service, ticket)


@router.get(
    "/tickets/{ticket_id}/recommendation",
    response_model=RecommendationOut | None,
    summary="当前处理建议",
)
def get_recommendation(
    ticket_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
):
    principal.require("ticket:read")
    service = TicketService(session)
    ticket = _load_ticket(service, ticket_id, principal)
    return _recommendation_out(service.latest_recommendation(ticket.id))


# --------------------------------------------------------------------------- 建议
@router.post(
    "/recommendations/{recommendation_id}/decision",
    response_model=RecommendationOut,
    summary="审批建议（批准/驳回，幂等）",
)
def decide_recommendation(
    recommendation_id: str,
    payload: RecommendationDecisionRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> RecommendationOut:
    principal.require("ticket:approve")
    service = TicketService(session)
    try:
        # 建议表通过 ticket_id 关联租户；查询时就下推租户过滤，避免拿到别的租户
        # 的 UUID 后仍能进入审批写路径。
        row = service.get_recommendation(recommendation_id, tenant_id=principal.tenant_id)
    except RecommendationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": f"建议不存在: {recommendation_id}"},
        ) from exc

    # L3/L4 必须写理由（需求书 §3.3：记录审批理由）
    if not (payload.reason or "").strip() and approval_for_tier(row.risk_tier) in (
        "supervisor",
        "risk_control",
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "reason_required", "message": f"{row.risk_tier} 建议必须填写审批理由"},
        )
    try:
        row = service.decide(
            row,
            decision=payload.decision,
            actor_id=principal.user.id,
            actor_name=principal.user.username,
            reason=payload.reason,
            modified_reply=payload.modified_reply,
            idempotency_key=payload.idempotency_key,
        )
    except InvalidTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    return _recommendation_out(row)


@router.post(
    "/recommendations/{recommendation_id}/send",
    response_model=RecommendationOut,
    summary="发送已批准回复",
)
def send_recommendation(
    recommendation_id: str,
    payload: RecommendationSendRequest | None = None,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> RecommendationOut:
    principal.require("ticket:reply")
    service = TicketService(session)
    try:
        row = service.get_recommendation(recommendation_id, tenant_id=principal.tenant_id)
    except RecommendationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": f"建议不存在: {recommendation_id}"},
        ) from exc

    ticket = _load_ticket(service, row.ticket_id, principal)
    requested_content = payload.content.strip() if payload and payload.content is not None else None
    if payload and payload.content is not None and not requested_content:
        raise HTTPException(status_code=422, detail={"code": "empty_reply", "message": "回复内容不能为空"})
    # `sent` 是发送终态：重复点击、网络重试或浏览器重复提交只能返回既有结果，
    # 不能再次写一条客户消息。若请求试图替换已发送正文，必须显式拒绝。
    if row.status == RecommendationStatus.SENT.value:
        if requested_content is not None and requested_content != row.customer_reply:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "recommendation_already_sent",
                    "message": "回复已经发送，不能修改已发送内容",
                },
            )
        return _recommendation_out(row)
    # 门禁：未批准的建议不允许发送（需求书 FR-05：未审批不能调用退款/赔付类写操作）。
    # 例外是 L2（`staff_confirm`）的草稿：口径就是"客服确认后执行"，
    # 客服点这一下即是确认，由 confirm_and_send 落审批记录后再发（见服务层注释）。
    if row.status not in (RecommendationStatus.APPROVED.value, RecommendationStatus.SENT.value):
        if (
            row.status == RecommendationStatus.DRAFT.value
            and row.approval_requirement == ApprovalRequirement.STAFF_CONFIRM.value
        ):
            violations = validate_customer_reply_for_send(
                requested_content if requested_content is not None else row.customer_reply,
                ticket_status=ticket.status,
                approval_requirement=row.approval_requirement,
                recommendation_status=row.status,
            )
            if violations:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={"code": "reply_commitment_blocked", "message": "；".join(violations), "violations": violations},
                )
            if requested_content is not None:
                row.original_reply = row.original_reply or row.customer_reply
                row.customer_reply = requested_content
                if payload and payload.idempotency_key:
                    row.idempotency_key = payload.idempotency_key
                session.commit()
            row = service.confirm_and_send(
                row,
                actor_id=principal.user.id,
                actor_name=principal.user.username,
                reason="客服确认后发送",
            )
            return _recommendation_out(row)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "recommendation_not_approved",
                "message": f"建议状态为 {row.status}，未批准不能发送",
            },
        )
    violations = validate_customer_reply_for_send(
        requested_content if requested_content is not None else row.customer_reply,
        ticket_status=ticket.status,
        approval_requirement=row.approval_requirement,
        recommendation_status=row.status,
    )
    if violations:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "reply_commitment_blocked", "message": "；".join(violations), "violations": violations},
        )
    if requested_content is not None:
        row.original_reply = row.original_reply or row.customer_reply
        row.customer_reply = requested_content
        if payload and payload.idempotency_key:
            row.idempotency_key = payload.idempotency_key
        session.commit()
    row = service.mark_sent(row, actor_id=principal.user.id)
    return _recommendation_out(row)
