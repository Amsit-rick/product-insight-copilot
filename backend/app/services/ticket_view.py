"""工单详情视图装配：把内部数据拼成前端页面直接能渲染的结构。

为什么要单独一层：原型详情页左栏是"用户与订单"**恰好 6 行键值但键名逐单变化**
（物流类看承运商/轨迹，优惠类看活动门槛/优惠结果），右栏是固定 4 行 Agent 状态。
如果让前端按固定 schema 渲染，就得为每种问题类型写一套；所以后端输出
`[{key, value}]`，前端只负责画。

三条约束：
1. 事实行必须带**来源与观测时间**（FR-02），并在过期时标 `stale=True`；
2. 展示值一律走脱敏出口（§8）；
3. 时间线按**正序**（原型是早→晚），并合并"消息事件 + 状态迁移 + 建议动作"。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import re

from app.models.base import utcnow
from app.models.ticket import OrderFact, Recommendation, Ticket, TicketMessage
from app.services.masking import mask_value


# 客服工作台只需要相对时限。具体 SLA 可按租户配置扩展，MVP 先按优先级提供稳定默认值。
SLA_MINUTES_BY_PRIORITY = {"urgent": 30, "high": 60, "medium": 240, "low": 480}
TERMINAL_TICKET_STATUSES = {"resolved", "cancelled"}


def sla_snapshot(ticket: Ticket, *, now: datetime | None = None) -> dict:
    """把技术时间戳转换成客服能行动的 SLA 状态。"""
    now = now or utcnow()
    if ticket.status in TERMINAL_TICKET_STATUSES:
        return {"due_at": None, "remaining_seconds": None, "status": "completed"}
    minutes = SLA_MINUTES_BY_PRIORITY.get(ticket.priority, SLA_MINUTES_BY_PRIORITY["medium"])
    due_at = (ticket.created_at or now) + timedelta(minutes=minutes)
    remaining = int((due_at - now).total_seconds())
    return {
        "due_at": due_at.isoformat(),
        "remaining_seconds": remaining,
        "status": "overdue" if remaining < 0 else "at_risk" if remaining <= 30 * 60 else "on_track",
    }


def next_action_for_ticket(ticket: Ticket) -> str:
    if ticket.status == "need_customer_info" and any(
        "事实来源暂不可用" in str(item) for item in (ticket.missing_fields or [])
    ):
        return "人工核实事实来源"
    return {
        "new": "开始分析",
        "enriching": "补充订单事实",
        "analyzing": "等待系统分析",
        "waiting_review": "等待主管审批",
        "ready_to_reply": "等待客服回复",
        "need_customer_info": "等待客户补充",
        "escalated": "等待升级处理",
        "resolved": "已完成",
        "cancelled": "已取消",
        "failed": "重新分析",
    }.get(ticket.status, "查看工单")


def handler_label_for_ticket(ticket: Ticket) -> str:
    if ticket.assignee_id:
        return "已分配客服"
    if ticket.status == "need_customer_info" and any(
        "事实来源暂不可用" in str(item) for item in (ticket.missing_fields or [])
    ):
        return "等待人工核实"
    return {
        "analyzing": "系统处理中",
        "enriching": "系统补充事实",
        "waiting_review": "售后主管",
        "need_customer_info": "等待客户",
        "ready_to_reply": "等待客服",
    }.get(ticket.status, "未分配")


def answerability(
    *,
    missing_information: list | None,
    approval_requirement: str | None,
    status: str | None,
    evidence_sufficient: bool = True,
) -> dict:
    missing = [str(item) for item in (missing_information or []) if str(item).strip()]
    if missing:
        return {
            "code": "need_information",
            "label": "信息不足",
            "reason": f"还缺少：{'、'.join(missing)}",
        }
    if approval_requirement in {"supervisor", "risk_control"} and status not in {"approved", "sent"}:
        return {
            "code": "human_review",
            "label": "需要人工决定",
            "reason": "涉及退款、赔付或责任判断，需完成审批后才能执行",
        }
    if not evidence_sufficient:
        return {
            "code": "staff_confirm",
            "label": "客服核对后回复",
            "reason": "未找到适用的售后政策依据，请先人工核对后再回复客户",
        }
    if approval_requirement == "staff_confirm" and status not in {"approved", "sent"}:
        return {
            "code": "staff_confirm",
            "label": "客服确认后执行",
            "reason": "客服确认回复内容后才能执行下一步操作",
        }
    return {
        "code": "ready_to_reply",
        "label": "可以直接处理",
        "reason": "关键事实和适用政策已齐全",
    }


def customer_reply_for_display(
    reply: str | None, *, approval_requirement: str | None, status: str | None
) -> str | None:
    """审批前只改展示稿，不改数据库原始草稿，阻断客服复制出确定性承诺。"""
    if not reply or status in {"approved", "sent"} or approval_requirement not in {"supervisor", "risk_control"}:
        return reply
    safe = reply
    safe = re.sub(r"已为您发起退款", "我们已将退款方案提交主管审核，当前尚未执行退款", safe)
    safe = re.sub(r"将为您发起退款", "将提交退款方案审核，当前尚未执行退款", safe)
    safe = re.sub(r"(?:现|现在)为您发起退款", "已将退款方案提交主管审核，当前尚未执行退款", safe)
    # 模型草稿可能在数字、连接号和“个”之间插入空格，统一拦截所有确定到账时限。
    safe = re.sub(
        r"预计\s*\d+\s*[-到至]\s*\d+\s*个工作日到账",
        "审批通过后会第一时间通知您，到账时间以实际处理结果为准",
        safe,
    )
    safe = re.sub(
        r"预计\s*\d+\s*个工作日到账",
        "审批通过后会第一时间通知您，到账时间以实际处理结果为准",
        safe,
    )
    return safe


def validate_customer_reply_for_send(
    reply: str | None,
    *,
    ticket_status: str | None,
    approval_requirement: str | None,
    recommendation_status: str | None,
) -> list[str]:
    """发送前的承诺校验，避免客服绕过页面按钮直接发出不该承诺的话。"""
    if not reply or not reply.strip():
        return ["回复内容不能为空"]
    violations: list[str] = []
    if (
        approval_requirement in {"supervisor", "risk_control"}
        and recommendation_status not in {"approved", "sent"}
    ):
        violations.append("退款、赔付或责任判断尚未完成审批，不能发送客户回复")
    if ticket_status == "need_customer_info":
        if re.search(r"已为您发起退款|将为您发起退款|已确认责任|已核实无误", reply):
            violations.append("当前还在等待客户补充信息，不能承诺已完成核实或退款")
        if re.search(r"预计\s*\d+\s*[-到至]\s*\d+\s*个工作日到账|预计\s*\d+\s*个工作日到账", reply):
            violations.append("信息不足时不能承诺确定的到账时间")
    return violations


def build_handoff_summary(
    ticket: Ticket,
    recommendation: Recommendation | None,
    *,
    fact_rows: list[dict] | None = None,
) -> dict:
    """把系统分析压缩成接手人能在 30 秒内读完的业务摘要。"""
    status_labels = {
        "new": "待处理", "enriching": "补充订单事实", "analyzing": "系统分析中",
        "waiting_review": "等待主管审批", "ready_to_reply": "等待客服回复",
        "need_customer_info": "等待客户补充", "escalated": "已升级处理",
        "resolved": "已完成", "failed": "处理失败", "cancelled": "已取消",
    }
    facts = []
    for row in fact_rows or []:
        value = row.get("value")
        if value not in (None, "", "—"):
            facts.append(f"{row.get('label') or row.get('key')}：{value}")
    missing = list((recommendation.missing_information if recommendation else None) or ticket.missing_fields or [])
    action_labels = {
        "refund": "退款方案", "return_goods": "退货处理", "exchange": "换货处理",
        "reship": "补发商品", "compensate": "赔付处理", "coupon": "优惠券补偿",
        "ask_customer": "向客户补充信息", "contact_logistics": "联系承运商",
        "reply_only": "解释说明",
    }
    action = recommendation.action_type if recommendation else None
    return {
        "status_label": status_labels.get(ticket.status, ticket.status),
        "customer_issue": ticket.content,
        "known_facts": facts[:8],
        "missing_information": [str(item) for item in missing],
        "suggested_action": action_labels.get(action or "", action or "待系统分析"),
        "risk_label": recommendation.risk_tier if recommendation else None,
        "reason": recommendation.risk_reason if recommendation else None,
        "next_step": (
            "请客服人工核实订单事实来源恢复后再继续处理"
            if any("事实来源暂不可用" in str(item) for item in missing)
            else f"先向客户补充：{'、'.join(str(item) for item in missing)}"
            if missing
            else next_action_for_ticket(ticket)
        ),
    }

# 渠道枚举只在接口和存储层使用技术值，客服时间线统一显示业务名称。
CHANNEL_LABELS = {
    "manual": "后台录入",
    "api": "接口导入",
    "wecom": "企业微信",
    "feishu": "飞书",
    "customer_service": "客服系统",
}


def channel_label(channel: str | None) -> str:
    if not channel:
        return "未注明来源"
    return CHANNEL_LABELS.get(channel, channel)


# 事实键 →（中文标签，展示顺序）。顺序决定详情页键值的排列，与原型一致：先订单、后支付、再物流、最后售后。
FACT_LABELS: dict[str, tuple[str, int]] = {
    "order_status": ("订单状态", 10),
    "order_amount": ("订单总额", 12),
    "order_items": ("订单商品", 14),
    "order_channel": ("下单渠道", 16),
    "paid_amount": ("支付金额", 20),
    "refundable_amount": ("可退金额", 22),
    "pay_channel": ("支付方式", 24),
    "carrier": ("承运商", 30),
    "tracking_no": ("运单号", 31),
    "latest_track": ("最新物流", 32),
    "signed_at": ("签收时间", 34),
    "stalled_hours": ("停滞时长", 36),
    "eta": ("预计送达", 38),
    "package_weight": ("包裹重量", 40),
    "address": ("收件信息", 44),
    "receiver_address": ("收件信息", 44),
    "receiver_phone": ("收件电话", 45),
    "evidence_attachments": ("证据附件", 50),
    "item_photo": ("商品本体照片", 51),
    "category": ("商品类目", 52),
    "special_category": ("是否特殊类目", 53),
    "claim_window": ("申请时效", 54),
    "gross_amount": ("商品总额", 60),
    "eligible_amount": ("可参与金额", 61),
    "excluded_items": ("排除商品", 62),
    "promotion_threshold": ("活动门槛", 63),
    "promotion_result": ("优惠结果", 64),
    "aftersale_history": ("历史售后", 70),
    "last_case_result": ("处理结果", 71),
}

DEFAULT_ORDER = 99


def build_fact_rows(
    facts: list[OrderFact], *, ttl_seconds: int, now: datetime | None = None
) -> list[dict]:
    """把事实快照摊平成展示行：同类键取**最新一条**（后拉取的事实覆盖先前结论）。"""
    now = now or utcnow()
    latest: dict[str, dict] = {}
    for fact in facts:
        for key, value in (fact.payload or {}).items():
            if key == "customer_id":
                continue
            label, order = FACT_LABELS.get(key, (key, DEFAULT_ORDER))
            current = latest.get(key)
            # 同一键多次出现时保留最新的观测（按 observed_at，缺失则按创建时间）
            if current is not None and _stamp(fact) < current["_stamp"]:
                continue
            latest[key] = {
                "key": key,
                "label": label,
                "value": mask_value(key, value),
                "order": order,
                "source": fact.source,
                "observed_at": fact.observed_at.isoformat() if fact.observed_at else None,
                "is_mock": fact.is_mock,
                "adapter": fact.adapter,
                "request_id": fact.request_id,
                "stale": bool(
                    fact.observed_at and (now - fact.observed_at).total_seconds() > ttl_seconds
                ),
                "_stamp": _stamp(fact),
            }
    rows = sorted(latest.values(), key=lambda row: (row["order"], row["key"]))
    for row in rows:
        row.pop("_stamp", None)
    return rows


def _stamp(fact: OrderFact) -> datetime:
    return fact.observed_at or fact.created_at or datetime.min


def build_timeline(
    ticket: Ticket,
    messages: list[TicketMessage],
    *,
    audits: list[dict] | None = None,
) -> list[dict]:
    """时间线（正序）：创建 → 消息 → 状态迁移/建议动作。"""
    items: list[dict] = [
        {
            "at": (ticket.created_at or utcnow()).isoformat(),
            "kind": "created",
            "title": f"工单创建（来源：{channel_label(ticket.channel)}）",
            "detail": None,
        }
    ]
    for message in messages:
        titles = {
            "customer": "客户诉求",
            "agent": "AI 生成回复草稿",
            "staff": "客服发送回复" if message.visible_to_customer else "客服内部备注",
            "system": "系统记录",
        }
        items.append(
            {
                "at": (message.created_at or utcnow()).isoformat(),
                "kind": f"message:{message.sender_type}",
                "title": titles.get(message.sender_type, "消息"),
                "detail": _clip(message.content),
            }
        )
    for audit in audits or []:
        detail = audit.get("detail") or {}
        action = str(audit.get("action") or "")
        # 创建和客户原话已有独立时间线节点，不再重复展示底层审计事件。
        if action in {"ticket_created", "ticket_create_idempotent_hit"}:
            continue
        status_labels = {
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
        if action == "ticket_status_changed":
            before = status_labels.get(str(detail.get("from")), "处理中")
            after = status_labels.get(str(detail.get("to")), "处理中")
            title = f"处理进度：{before} → {after}"
        elif action == "ticket_assigned":
            title = "工单已转派" if detail.get("assignee_id") else "工单已取消分配"
        else:
            title = {
                "ticket_order_id_updated": "已补录订单号",
                "ticket_analysis_dispatched": "已提交系统核实",
                "ticket_analysis_started": "开始核实订单与处理规则",
                "recommendation_decided": "主管已完成审批",
                "recommendation_confirmed_by_staff": "客服已确认处理方案",
                "recommendation_created": "已生成处理建议",
            }.get(action, "系统处理记录")
        items.append(
            {
                "at": audit.get("at"),
                "kind": "audit:" + action,
                "title": title,
                "detail": detail.get("reason"),
            }
        )
    items.sort(key=lambda item: item.get("at") or "")
    return items


def _clip(text: str | None, limit: int = 120) -> str | None:
    if not text:
        return None
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def build_analysis(
    ticket: Ticket,
    recommendation: Recommendation | None,
    *,
    agent_runs: list[dict] | None = None,
    task_filters: dict | None = None,
    task_status: str | None = None,
    can_view_technical: bool = False,
) -> dict:
    """构建面向客服的真实协作路径；技术轨迹只向管理员和工程角色返回。"""
    runs = agent_runs or []
    filters = task_filters or {}
    by_code = {run.get("agent_code"): run for run in runs if run.get("agent_code")}
    advanced_codes = {"resolution_agent", "response_agent", "followup_agent"}

    mode_value = filters.get("multi_agent")
    if mode_value is None:
        # 兼容旧任务：依据真实轨迹识别完整流程，不根据当前风险标签猜测历史路由。
        multi_agent = bool(advanced_codes.intersection(by_code)) or len(by_code) > 4
    else:
        multi_agent = bool(mode_value) or bool(advanced_codes.intersection(by_code))

    mode = "multi_agent" if multi_agent else "fast_path"
    expected_codes = (
        [
            "triage_agent",
            "order_fact_agent",
            "policy_rag_agent",
            "resolution_agent",
            "response_agent",
            "review_agent",
            "followup_agent",
        ]
        if multi_agent
        else ["triage_agent", "order_fact_agent", "policy_rag_agent", "review_agent"]
    )
    agent_count = sum(
        1 for code in expected_codes if by_code.get(code, {}).get("status") not in (None, "skipped")
    )

    role_details = {
        "triage_agent": ("问题分类", "确认客户遇到的售后问题及主要诉求", "识别诉求", "先识别问题，再决定核验范围"),
        "order_fact_agent": ("订单核验", "核对订单、支付、物流和售后记录", "核对订单事实", "避免仅凭客户描述猜测订单状态"),
        "policy_rag_agent": ("政策核验", "查找当前适用的售后处理规则", "核对适用规则", "确保处理建议符合有效政策"),
        "resolution_agent": ("方案整理", "结合已核实事实和规则整理处理建议", "整理处理方案", "供客服或主管审阅，不决定金额和审批权限"),
        "response_agent": ("回复准备", "根据核验结果准备客户可读的回复草稿", "准备回复草稿", "草稿仍需按风险要求由人工确认"),
        "review_agent": ("风险复核", "检查证据是否完整及回复承诺是否合适", "检查风险与回复", "发现风险时阻止不安全的处理或承诺"),
        "followup_agent": ("后续跟进", "整理需要继续跟进的事项和时间", "安排后续跟进", "确保未解决事项不会被遗漏"),
    }
    success_conclusions = {
        "triage_agent": "问题类型已确认，用于选择本次核验范围",
        "order_fact_agent": "订单、支付和物流核验结果已交给后续处理",
        "policy_rag_agent": "适用规则已查找，结果用于核对处理条件",
        "resolution_agent": "处理建议已整理，动作和金额仍以系统规则为准",
        "response_agent": "客户回复草稿已准备，发送仍受审批要求约束",
        "review_agent": "证据、风险和回复承诺检查已完成",
        "followup_agent": "后续跟进事项已核对，必要时安排复查",
    }
    steps = []
    for code in expected_codes:
        role, responsibility, task, why = role_details[code]
        run = by_code.get(code)
        status = (
            run.get("status", "pending")
            if run
            else "skipped" if task_status in {"completed", "cancelled"} else "pending"
        )
        if status == "succeeded":
            conclusion = success_conclusions[code]
            if code == "policy_rag_agent":
                if recommendation is None:
                    conclusion = "政策检索已完成，结果待核对"
                elif recommendation.policy_refs:
                    conclusion = "已找到适用规则，结果用于核对处理条件"
                else:
                    conclusion = "未找到适用规则，不能据此下结论"
        elif status == "failed":
            conclusion = "未能完成，请人工检查后再继续处理"
        elif status == "skipped":
            conclusion = "本次流程未执行此环节"
        elif status == "waiting_human":
            conclusion = "正在等待人工确认"
        elif status == "running":
            conclusion = "正在核验"
        else:
            conclusion = "尚未开始"

        parallel_group = None
        phase = "问题识别"
        if code in {"order_fact_agent", "policy_rag_agent"}:
            phase = "并行核验" if multi_agent else "依次核验"
            if multi_agent:
                parallel_group = "订单与政策核验"
        elif code in {"resolution_agent", "response_agent", "review_agent"}:
            phase = "方案与风险复核"
            if multi_agent and code in {"response_agent", "review_agent"}:
                parallel_group = "回复准备与风险复核"
        elif code == "followup_agent":
            phase = "后续跟进"
        steps.append(
            {
                "agent_code": code,
                "role": role,
                "responsibility": responsibility,
                "task": task,
                "why": why,
                "phase": phase,
                "parallel_group": parallel_group,
                "status": status,
                "conclusion": conclusion,
            }
        )

    if not ticket.last_analysis_task_id:
        route_reason = "尚未启动分析"
        mode = None
        mode_label = None
        steps = []
        agent_count = 0
    else:
        from app.agents.after_sales import classify_issue

        issue_type = filters.get("issue_type") or ticket.issue_type or classify_issue(
            f"{ticket.subject or ''} {ticket.content or ''}"
        )
        issue_labels = {
            "promotion_not_applied": "优惠规则解释",
            "refund": "退款申请",
            "return": "退货申请",
            "exchange": "换货申请",
            "logistics_stalled": "物流停滞",
            "missing_item": "少件问题",
            "damaged": "商品破损",
            "wrong_item": "错发问题",
            "not_shipped": "未发货问题",
        }
        flags = filters.get("risk_flags") or []
        if filters.get("sensitive_customer"):
            route_reason = "该工单涉及敏感客户，需要完整核验并进行风险复核。"
        elif flags:
            route_reason = "该工单带有风险标记，需要完整核验并进行风险复核。"
        elif multi_agent:
            route_reason = f"识别为{issue_labels.get(issue_type, '需要综合核验的问题')}，需要多角色核对事实、规则和处理风险。"
        else:
            route_reason = "这是低风险优惠解释问题，没有触发退款、补发或风险标记，因此采用快速核验。"
        mode_label = "多 Agent 协作" if multi_agent else "快速核验"

    result = {
        "mode": mode,
        "mode_label": mode_label,
        "route_reason": route_reason,
        "agent_count": agent_count,
        "business_steps": steps,
        "applied_skills": [
            str(item.get("code"))
            for item in (filters.get("business_skills") or [])
            if isinstance(item, dict) and item.get("code")
        ],
        "analysis_count": ticket.analysis_count,
        "steps": runs if can_view_technical else [],
        "confidence": recommendation.confidence if recommendation else None,
        "risk_tier": recommendation.risk_tier if recommendation else ticket.risk_tier,
    }
    reflection_enabled = bool(filters.get("reflection_enabled"))
    review_runs = [run for run in runs if run.get("agent_code") == "review_agent"]
    revision_runs = [run for run in runs if run.get("agent_code") == "revision_agent"]
    issue_map: dict[str, dict] = {}
    stop_reason = None
    verdict = None
    for run in review_runs:
        structured = run.get("reflection") or {}
        verdict = structured.get("verdict") or verdict
        stop_reason = structured.get("stop_reason") or stop_reason
        for item in structured.get("issues") or []:
            if item.get("code"):
                issue_map[str(item["code"])] = item
    if reflection_enabled:
        if verdict == "human_review":
            label = "需要人工确认"
        elif revision_runs:
            label = f"已完成质量复核并修正 {len(issue_map)} 项"
        elif review_runs:
            label = "已完成质量复核"
        else:
            label = "等待质量复核"
        result["reflection"] = {
            "enabled": True,
            "label": label,
            "verdict": verdict,
            "issue_count": len(issue_map),
            "revised": bool(revision_runs),
            "rounds": len(review_runs),
            "issues": list(issue_map.values()),
            "stop_reason": stop_reason,
        }
    else:
        result["reflection"] = {
            "enabled": False,
            "label": "未启用自动复核",
            "verdict": None,
            "issue_count": 0,
            "revised": False,
            "rounds": 0,
            "issues": [],
            "stop_reason": None,
        }
    if can_view_technical:
        result["analysis_task_id"] = ticket.last_analysis_task_id
    return result
