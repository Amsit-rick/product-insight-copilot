"""售后工单领域规则单测：状态机、风险分级、脱敏、详情视图装配。

这四块都是**纯规则**，也是最容易被改错、改错后最难发现的地方：
状态机错一条边 → 用户点按钮报错；风险分级错一档 → 该审批的没审批（合规问题）；
脱敏漏一处 → 手机号进日志；事实行错序 → 详情页把旧事实当新结论。所以逐条钉住。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import Settings
from app.models.base import utcnow
from app.models.enums import ActionType, ApprovalRequirement, RiskTier, TicketStatus
from app.models.ticket import OrderFact, Ticket, TicketMessage
from app.services import ticket_state
from app.services.masking import contains_sensitive, mask_address, mask_text, mask_value
from app.services.ticket_risk import assess, approval_for_tier, auto_draft_allowed
from app.services.ticket_view import (
    build_fact_rows,
    build_handoff_summary,
    build_timeline,
    answerability,
    next_action_for_ticket,
    validate_customer_reply_for_send,
)


def make_settings(**overrides) -> Settings:
    base = {
        "llm_provider": "stub",
        "ticket_l2_amount_limit": 100.0,
        "ticket_l3_amount_limit": 500.0,
        "ticket_l4_amount_limit": 5000.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


# --------------------------------------------------------------------------- 状态机
def test_main_flow_is_reachable():
    """需求书 §2.3 的主线：NEW → ENRICHING → ANALYZING → WAITING_REVIEW → READY_TO_REPLY → RESOLVED。"""
    path = [
        TicketStatus.NEW.value,
        TicketStatus.ENRICHING.value,
        TicketStatus.ANALYZING.value,
        TicketStatus.WAITING_REVIEW.value,
        TicketStatus.READY_TO_REPLY.value,
        TicketStatus.RESOLVED.value,
    ]
    for current, target in zip(path, path[1:]):
        assert ticket_state.can_transition(current, target), f"{current} → {target} 应当允许"


def test_low_risk_can_skip_review():
    """L1 低风险允许直接从分析到草稿（不必绕审批）。"""
    assert ticket_state.can_transition(TicketStatus.ANALYZING.value, TicketStatus.READY_TO_REPLY.value)


def test_illegal_transitions_are_rejected():
    # 不能跳过分析直接解决
    assert not ticket_state.can_transition(TicketStatus.NEW.value, TicketStatus.RESOLVED.value)
    # 已解决是终态
    assert not ticket_state.can_transition(TicketStatus.RESOLVED.value, TicketStatus.ANALYZING.value)
    with pytest.raises(ticket_state.InvalidTransition):
        ticket_state.assert_transition(TicketStatus.NEW.value, TicketStatus.RESOLVED.value)


def test_failed_can_retry_to_enriching():
    """需求书要求失败可重试。"""
    assert ticket_state.can_transition(TicketStatus.FAILED.value, TicketStatus.ENRICHING.value)


def test_need_customer_info_returns_to_enriching():
    assert ticket_state.can_transition(
        TicketStatus.NEED_CUSTOMER_INFO.value, TicketStatus.ENRICHING.value
    )
    assert ticket_state.can_transition(TicketStatus.NEW.value, TicketStatus.NEED_CUSTOMER_INFO.value)


def test_terminal_states_and_describe():
    assert ticket_state.is_terminal(TicketStatus.RESOLVED.value)
    assert ticket_state.is_terminal(TicketStatus.CANCELLED.value)
    assert not ticket_state.is_terminal(TicketStatus.ANALYZING.value)
    snapshot = ticket_state.describe()
    # describe() 是给前端渲染"允许哪些操作"用的，必须覆盖全部状态
    assert set(snapshot) == {item.value for item in TicketStatus}
    assert snapshot[TicketStatus.RESOLVED.value]["terminal"] is True
    assert snapshot[TicketStatus.NEW.value]["next"] == sorted(
        [TicketStatus.ENRICHING.value, TicketStatus.NEED_CUSTOMER_INFO.value,
         TicketStatus.CANCELLED.value, TicketStatus.FAILED.value]
    )


def test_same_state_is_not_a_transition():
    assert not ticket_state.can_transition(TicketStatus.NEW.value, TicketStatus.NEW.value)


# --------------------------------------------------------------------------- 风险分级
def test_l1_for_pure_explanation():
    result = assess(action_type=ActionType.REPLY_ONLY.value, amount=0, settings=make_settings())
    assert result.tier == RiskTier.L1.value
    assert result.approval_requirement == ApprovalRequirement.NONE.value
    assert result.auto_draft_allowed is True


def test_l2_for_write_actions_within_limit():
    result = assess(action_type=ActionType.RESHIP.value, amount=0, settings=make_settings())
    assert result.tier == RiskTier.L2.value
    assert result.approval_requirement == ApprovalRequirement.STAFF_CONFIRM.value


def test_l3_for_refund_even_if_small():
    """需求书把"退款"本身就列为 L3 示例：金额小也要主管审批。"""
    result = assess(action_type=ActionType.REFUND.value, amount=10, settings=make_settings())
    assert result.tier == RiskTier.L3.value
    assert result.approval_requirement == ApprovalRequirement.SUPERVISOR.value
    assert result.auto_draft_allowed is False


def test_l3_for_conflict_and_sensitive_customer():
    assert assess(action_type=ActionType.COUPON.value, conflict=True, settings=make_settings()).tier == "L3"
    assert (
        assess(action_type=ActionType.COUPON.value, sensitive_customer=True, settings=make_settings()).tier
        == "L3"
    )


def test_amount_thresholds_are_configurable():
    settings = make_settings(ticket_l3_amount_limit=200.0)
    assert assess(action_type=ActionType.COMPENSATE.value, amount=150, settings=settings).tier == "L2"
    assert assess(action_type=ActionType.COMPENSATE.value, amount=250, settings=settings).tier == "L3"


def test_l2_amount_limit_is_actually_read():
    """`ticket_l2_amount_limit` 必须真的参与判定（它曾经是个死配置）。

    回归背景：L2 分支原来写的是"amount > 0 → L2"，于是这个配置项**从来没被读过**：
    调大它不会让小额补偿变成自动草稿，调小它也不会让更多工单进客服确认。
    现在口径是：写操作类动作一律 ≥ L2（不看金额），非写操作动作按金额线分档。
    """
    # 非写操作（contact_logistics）+ 金额未达线 → L1（客服发送前仍会看到草稿）
    below = assess(
        action_type=ActionType.CONTACT_LOGISTICS.value,
        amount=30,
        settings=make_settings(ticket_l2_amount_limit=100.0),
    )
    assert below.tier == RiskTier.L1.value, below.reason
    assert below.approval_requirement == ApprovalRequirement.NONE.value

    # 同一条工单，把线调到 20 元 → 变成 L2（配置真的生效）
    above = assess(
        action_type=ActionType.CONTACT_LOGISTICS.value,
        amount=30,
        settings=make_settings(ticket_l2_amount_limit=20.0),
    )
    assert above.tier == RiskTier.L2.value, above.reason
    assert above.approval_requirement == ApprovalRequirement.STAFF_CONFIRM.value
    assert str(int(20)) in above.reason, "原因里要写清是按哪条线判的（审批人要能核对）"

    # 写操作类动作不受金额线影响：0 元也必须是 L2（永不自动执行）
    write = assess(
        action_type=ActionType.RESHIP.value,
        amount=0,
        settings=make_settings(ticket_l2_amount_limit=100.0),
    )
    assert write.tier == RiskTier.L2.value


def test_l4_for_risk_flags_and_huge_amount():
    flagged = assess(
        action_type=ActionType.COMPENSATE.value,
        amount=1,
        flags=["batch_refund"],
        settings=make_settings(),
    )
    assert flagged.tier == RiskTier.L4.value
    assert flagged.approval_requirement == ApprovalRequirement.RISK_CONTROL.value
    assert flagged.auto_draft_allowed is False

    huge = assess(action_type=ActionType.COMPENSATE.value, amount=9999, settings=make_settings())
    assert huge.tier == RiskTier.L4.value


def test_risk_reason_is_human_readable():
    """审批链路上不能出现"不知道为什么"的分级，所以 reason 必须是人话。"""
    result = assess(action_type=ActionType.REFUND.value, amount=88, settings=make_settings())
    assert result.reason and "退款" in result.reason
    assert approval_for_tier(result.tier) == ApprovalRequirement.SUPERVISOR.value
    assert auto_draft_allowed(RiskTier.L1.value) and not auto_draft_allowed(RiskTier.L4.value)


# --------------------------------------------------------------------------- 脱敏
def test_mask_phone_and_id_and_card():
    text = "联系 13812345678，身份证 110101199001011234，卡号 6222021234567890123"
    masked = mask_text(text)
    assert "13812345678" not in masked
    assert "138****5678" in masked
    assert "110101199001011234" not in masked
    assert "6222021234567890123" not in masked
    assert contains_sensitive(masked) is False


def test_mask_text_preserves_uuid_that_contains_phone_shaped_digits():
    identifier = "2deb36ce-57e1-468b-a852-b05929371753"
    masked = mask_text(f"任务 {identifier}，联系电话 13812345678")

    assert identifier in masked
    assert "138****5678" in masked
    assert contains_sensitive(identifier) is False


def test_mask_address_keeps_city_level():
    masked = mask_address("上海市浦东新区张江路 88 弄 3 号楼 501")
    assert masked.startswith("上海市浦东新区")
    assert "501" not in masked


def test_mask_value_by_key_and_by_shape():
    assert mask_value("address", "北京市海淀区中关村大街 1 号") == "北京市海淀区***"
    assert mask_value("phone", "13900001111") == "139****1111"
    # 键名不认识但内容是手机号，也必须脱敏（按内容形态兜底）
    assert mask_value("note", "客户电话 13900001111 打不通") == "客户电话 139****1111 打不通"


def test_mask_value_passes_through_non_sensitive():
    assert mask_value("order_status", "已签收") == "已签收"
    assert mask_value("order_amount", 268.0) == 268.0
    assert mask_value("order_amount", None) is None


# --------------------------------------------------------------------------- 详情视图装配
def _fact(source: str, payload: dict, *, observed_at=None, is_mock: bool = True) -> OrderFact:
    return OrderFact(
        ticket_id="t1",
        source=source,
        fact_type="snapshot",
        payload=payload,
        observed_at=observed_at or utcnow(),
        request_id="req-1",
        is_mock=is_mock,
    )


def test_fact_rows_use_latest_observation_and_are_ordered():
    old = _fact("logistics", {"latest_track": "已揽收"}, observed_at=utcnow() - timedelta(hours=2))
    new = _fact(
        "logistics",
        {"latest_track": "已签收（本人）", "carrier": "顺丰"},
        observed_at=utcnow(),
    )
    order = _fact("order", {"order_status": "已签收", "paid_amount": "268.00"})
    rows = build_fact_rows([old, new, order], ttl_seconds=900)
    by_key = {row["key"]: row for row in rows}
    # 同一键取最新观测
    assert by_key["latest_track"]["value"] == "已签收（本人）"
    # 排序：订单类在前，物流类在后（与原型详情页顺序一致）
    keys = [row["key"] for row in rows]
    assert keys.index("order_status") < keys.index("carrier") < keys.index("latest_track")


def test_fact_rows_mark_stale_and_mask_sensitive():
    stale = _fact(
        "order",
        {"order_status": "已发货", "address": "上海市浦东新区某路 100 号 5 楼"},
        observed_at=utcnow() - timedelta(hours=5),
    )
    rows = build_fact_rows([stale], ttl_seconds=900)
    by_key = {row["key"]: row for row in rows}
    assert by_key["order_status"]["stale"] is True
    assert by_key["order_status"]["source"] == "order"
    assert by_key["order_status"]["is_mock"] is True
    assert "100 号" not in str(by_key["address"]["value"])


def test_fact_rows_carry_provenance_for_ui():
    """FR-02：每个字段要能标出来源与查询时间。"""
    row = build_fact_rows([_fact("payment", {"paid_amount": "156.00"})], ttl_seconds=900)[0]
    assert row["source"] == "payment"
    assert row["observed_at"]
    assert row["request_id"] == "req-1"


def test_fact_rows_translate_sensitive_field_names_for_customer_service():
    fact = _fact("order", {"receiver_phone": "13512345678"})

    row = build_fact_rows([fact], ttl_seconds=900)[0]

    assert row["label"] == "收件电话"
    assert row["value"] == "135****5678"
    assert "receiver_phone" not in row["label"]


def test_fact_rows_do_not_expose_internal_customer_id():
    fact = _fact("order", {"customer_id": "qa-browser-test"})

    rows = build_fact_rows([fact], ttl_seconds=900)

    assert all(row["key"] != "customer_id" for row in rows)


def test_timeline_is_chronological_and_includes_messages():
    ticket = Ticket(
        tenant_id="t1",
        subject="包裹少了一件",
        content="买了两件只收到一件",
        channel="manual",
        created_at=utcnow() - timedelta(minutes=30),
    )
    messages = [
        TicketMessage(
            ticket_id="t1",
            sender_type="customer",
            content="买了两件只收到一件",
            created_at=utcnow() - timedelta(minutes=29),
        ),
        TicketMessage(
            ticket_id="t1",
            sender_type="staff",
            content="已为您申请补发",
            created_at=utcnow() - timedelta(minutes=2),
        ),
    ]
    audits = [
        {
            "action": "ticket_created",
            "detail": {"channel": "manual"},
            "at": (utcnow() - timedelta(minutes=8)).isoformat(),
        },
        {
            "action": "ticket_status_changed",
            "detail": {"from": "analyzing", "to": "ready_to_reply", "reason": "审批通过"},
            "at": (utcnow() - timedelta(minutes=5)).isoformat(),
        }
    ]
    timeline = build_timeline(ticket, messages, audits=audits)
    stamps = [item["at"] for item in timeline]
    assert stamps == sorted(stamps), "时间线必须是正序"
    kinds = [item["kind"] for item in timeline]
    assert kinds[0] == "created"
    assert "message:customer" in kinds and "message:staff" in kinds
    assert any(kind.startswith("audit:") for kind in kinds)
    assert "audit:ticket_created" not in kinds
    status_item = next(item for item in timeline if item["kind"] == "audit:ticket_status_changed")
    assert status_item["title"] == "处理进度：核实处理中 → 等待客服回复"
    assert "analyzing" not in status_item["title"]


def test_timeline_uses_business_channel_label():
    ticket = Ticket(
        tenant_id="t1",
        subject="企业微信工单",
        content="请帮忙核实",
        channel="wecom",
        created_at=utcnow(),
    )

    timeline = build_timeline(ticket, [])

    assert timeline[0]["title"] == "工单创建（来源：企业微信）"
    assert "wecom" not in timeline[0]["title"]


def test_answerability_uses_business_labels_instead_of_confidence_numbers():
    ready = answerability(
        missing_information=[],
        approval_requirement=ApprovalRequirement.NONE.value,
        status="draft",
    )
    assert ready["code"] == "ready_to_reply"
    assert ready["label"] == "可以直接处理"

    missing = answerability(
        missing_information=["订单号"],
        approval_requirement=ApprovalRequirement.NONE.value,
        status="draft",
    )
    assert missing["code"] == "need_information"
    assert "订单号" in missing["reason"]

    review = answerability(
        missing_information=[],
        approval_requirement=ApprovalRequirement.SUPERVISOR.value,
        status="pending_approval",
    )
    assert review["code"] == "human_review"
    assert review["label"] == "需要人工决定"

    unsupported = answerability(
        missing_information=[],
        approval_requirement=ApprovalRequirement.NONE.value,
        status="draft",
        evidence_sufficient=False,
    )
    assert unsupported["code"] == "staff_confirm"
    assert unsupported["label"] == "客服核对后回复"


def test_pending_refund_reply_cannot_make_a_deterministic_promise():
    from app.services.ticket_view import customer_reply_for_display

    original = "您好，已为您发起退款，预计1-3个工作日到账。"
    safe = customer_reply_for_display(
        original,
        approval_requirement=ApprovalRequirement.SUPERVISOR.value,
        status="pending_approval",
    )
    assert "已为您发起退款" not in safe
    assert "尚未执行退款" in safe

    approved = customer_reply_for_display(
        original,
        approval_requirement=ApprovalRequirement.SUPERVISOR.value,
        status="approved",
    )
    assert approved == original


def test_pending_refund_reply_blocks_spaced_settlement_promise():
    from app.services.ticket_view import customer_reply_for_display

    safe = customer_reply_for_display(
        "将为您发起退款，预计 1-3 个工作日到账。",
        approval_requirement=ApprovalRequirement.SUPERVISOR.value,
        status="pending_approval",
    )
    assert safe is not None
    assert "将为您发起退款" not in safe
    assert "预计 1-3 个工作日到账" not in safe
    assert "到账时间以实际处理结果为准" in safe


def test_send_reply_gate_blocks_unapproved_or_deterministic_promises():
    blocked = validate_customer_reply_for_send(
        "我们将为您发起退款，预计 1-3 个工作日到账。",
        ticket_status="waiting_review",
        approval_requirement="supervisor",
        recommendation_status="pending_approval",
    )
    assert len(blocked) == 1
    assert "尚未完成审批" in blocked[0]

    missing = validate_customer_reply_for_send(
        "我们已核实无误，请耐心等待。",
        ticket_status="need_customer_info",
        approval_requirement="none",
        recommendation_status="draft",
    )
    assert "不能承诺已完成核实" in missing[0]


def test_handoff_summary_is_business_oriented():
    ticket = Ticket(
        id="t-handoff",
        tenant_id="tenant-a",
        subject="物流显示签收但未收到",
        content="客户说没有收到包裹",
        status="need_customer_info",
        priority="high",
        missing_fields=["签收凭证"],
    )
    summary = build_handoff_summary(ticket, None, fact_rows=[{"label": "订单状态", "value": "已签收"}])
    assert summary["status_label"] == "等待客户补充"
    assert summary["next_step"] == "先向客户补充：签收凭证"
    assert summary["known_facts"] == ["订单状态：已签收"]


def test_handoff_summary_routes_unavailable_fact_source_to_manual_verification():
    ticket = Ticket(
        id="t-facts-unavailable",
        tenant_id="tenant-a",
        subject="订单事实查询暂不可用",
        content="客户申请退款，请核实订单信息",
        status="need_customer_info",
        priority="high",
        missing_fields=["订单事实来源暂不可用，请客服人工核实后再继续"],
    )

    summary = build_handoff_summary(ticket, None)

    assert summary["next_step"] == "请客服人工核实订单事实来源恢复后再继续处理"
    assert "订单事实来源暂不可用" in summary["missing_information"][0]
    assert next_action_for_ticket(ticket) == "人工核实事实来源"


def test_reply_template_replaces_known_variables_and_reports_missing_values():
    from app.services.ticket_templates import apply_reply_template

    rendered, missing = apply_reply_template(
        "订单{{订单号}}的{{问题类型}}正在核实，金额{{金额}}。",
        {"订单号": "TB-1001", "问题类型": "物流问题", "金额": None},
    )
    assert "TB-1001" in rendered
    assert "物流问题" in rendered
    assert "{{金额}}" in rendered
    assert missing == ["金额"]
