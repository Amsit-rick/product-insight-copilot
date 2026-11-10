"""售后 7 个 Agent 的规则层单测（确定性输出，不依赖模型）。

这三件事错一个都会直接造成业务事故，所以逐条钉住：

1. **分类优先级**：现象优先于诉求（"商品不想要了，申请退款"是退款，不是退货）；
2. **金额只引用**：退款取支付事实的实付金额，补偿取政策写明的标准，绝不"算"金额；
3. **质检与风险**：证据不足不得静默通过；金额超过实付要拦下；L4 标记必须转风控。
"""

from __future__ import annotations

import pytest

from app.agents.after_sales import (
    AFTER_SALES_AGENTS,
    FollowUpAgent,
    PolicyRagAgent,
    ResolutionAgent,
    RevisionAgent,
    ReviewAgent,
    TriageAgent,
    _policy_query,
    _reply_template,
    classify_issue,
    detect_sentiment,
    detect_urgency,
    parse_amount,
    wants_refund_explicitly,
)
from app.agents.prompts import system_prompt
from app.agents.schemas import AgentInput
from app.config import Settings
from app.models.enums import ActionType, IssueType, Sentiment


def make_settings(**overrides) -> Settings:
    base = {
        "llm_provider": "stub",
        "ticket_l2_amount_limit": 100.0,
        "ticket_l3_amount_limit": 500.0,
        "ticket_l4_amount_limit": 5000.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def agent_input(query: str, **filters) -> AgentInput:
    return AgentInput(
        session=None,  # 规则层不需要会话
        task_id="t1",
        tenant_id="demo",
        user_id="u1",
        query=query,
        filters=filters,
        kb_codes=["policy_kb"],
    )


def run_deterministic(agent, ai: AgentInput) -> dict:
    from app.agents.base import Collected

    return agent.deterministic_output(ai, Collected())


# --------------------------------------------------------------------------- 分类
def test_seven_agents_registered():
    assert len(AFTER_SALES_AGENTS) == 7
    assert set(AFTER_SALES_AGENTS) == {
        "triage_agent",
        "order_fact_agent",
        "policy_rag_agent",
        "resolution_agent",
        "response_agent",
        "review_agent",
        "followup_agent",
    }


def test_system_prompt_uses_business_identity_for_after_sales_agents():
    after_sales = system_prompt("triage_agent")
    historical = system_prompt("product_agent")

    assert "电商售后工单智能处理系统" in after_sales
    assert "产品智能诊断平台" not in after_sales
    assert "产品智能诊断平台" in historical


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("商品不想要了，申请退款，请把钱退给我。", IssueType.REFUND.value),
        ("我买了两件，包裹里只收到一件，少了一件。", IssueType.MISSING_ITEM.value),
        ("收到商品外包装破损，担心影响使用。", IssueType.DAMAGED.value),
        ("物流超过 48 小时没有更新了。", IssueType.LOGISTICS_STALLED.value),
        ("活动页显示满 199 减 30，结算没有减。", IssueType.PROMOTION_NOT_APPLIED.value),
        ("发错货了，我要换货。", IssueType.WRONG_ITEM.value),
        ("一直没发货，麻烦看看。", IssueType.NOT_SHIPPED.value),
        ("和图片不一样，与描述不符。", IssueType.DESCRIPTION_MISMATCH.value),
    ],
)
def test_classify_issue_prefers_phenomenon_over_remedy(text, expected):
    assert classify_issue(text) == expected


def test_classify_issue_falls_back_to_other():
    assert classify_issue("你们几点上班？") == IssueType.OTHER.value


@pytest.mark.parametrize(
    "text",
    [
        "订单里有两样东西，包裹里只有一样，漏发了。",
        "收到的商品不全，漏发了赠品。",
        "拆开只有一件，另一个没有，漏发了。",
    ],
)
def test_classify_leaked_item_as_missing_item_not_unshipped(text):
    """“漏发”表示包裹已到但少件，不等于整单未发货。"""
    assert classify_issue(text) == IssueType.MISSING_ITEM.value


def test_classify_issue_covers_colloquial_damage_and_refund_wording():
    """口语化说法的关键词缺口（评测集扩到 228 条时暴露，本轮已按最小化原则补词）。

    旧规则只认"外包装 / 破损 / 压坏 / 坏了 / 碎了"，于是下面三句全被判成"未识别"：
    动作退化成"追问客户"，本该直接补发的破损件被拖成二次沟通。
    退款诉求同理："把这笔钱退给我"里没有"退我"这个子串，方案生成看不出用户在要钱。

    这些词在售后语境里没有歧义（都指商品/包装受损、都在明确索要退款），
    所以补进关键词表；这条测试钉住它们，避免以后被"精简关键词"时误删。
    """
    assert classify_issue("外箱瘪了，里面的罐子也压变形了。") == IssueType.DAMAGED.value
    assert classify_issue("拆开发现商品是坏的，用不了。") == IssueType.DAMAGED.value
    assert classify_issue("包装箱被压瘪了一角，商品外壳也有磕碰。") == IssueType.DAMAGED.value
    assert classify_issue("麻烦把这笔钱退给我，我不需要了。") == IssueType.REFUND.value
    assert wants_refund_explicitly("麻烦把这笔钱退给我，我不需要了。") is True


def test_refund_words_detected():
    assert wants_refund_explicitly("帮我退款") is True
    assert wants_refund_explicitly("能不能补发一件") is False


def test_urgency_and_sentiment_rules():
    assert detect_urgency("再不处理我就去投诉曝光了") == "urgent"
    assert detect_urgency("麻烦看下物流") == "medium"
    assert detect_sentiment("太差了，垃圾服务！") == Sentiment.ANGRY.value
    assert detect_sentiment("麻烦帮我看下订单") == Sentiment.CALM.value


def test_triage_reports_missing_fields():
    output = run_deterministic(TriageAgent(), agent_input("收到商品破损了", order_id=None))
    assert output["issue_type"] == IssueType.DAMAGED.value
    assert "订单号" in output["missing_fields"]
    assert "商品照片" in output["missing_fields"]


# --------------------------------------------------------------------------- 金额引用
def test_refund_amount_is_referenced_from_facts():
    output = run_deterministic(
        ResolutionAgent(),
        agent_input(
            "申请退款，请把钱退给我",
            order_id="TB1",
            issue_type=IssueType.REFUND.value,
            fact_digest={"paid_amount": "89.00", "order_status": "已签收"},
        ),
    )
    assert output["action_type"] == ActionType.REFUND.value
    assert output["amount"] == "89.00"
    assert "支付事实" in output["amount_source"]


def test_refund_without_facts_asks_for_missing_amount():
    output = run_deterministic(
        ResolutionAgent(),
        agent_input("申请退款", order_id="TB1", issue_type=IssueType.REFUND.value, fact_digest={}),
    )
    assert output["amount"] is None
    assert "实付金额" in output["missing_information"]


def test_compensation_amount_only_from_policy_text():
    from app.agents.base import Collected

    collected = Collected()
    collected.evidence = [
        {
            "local_ref": "e1",
            "content": "第五章 物流时效与补偿标准：停滞超过 48 小时，按订单金额 5% 补偿，单笔不超过 ¥30。",
        }
    ]
    output = ResolutionAgent().deterministic_output(
        agent_input(
            "物流停滞两天了",
            order_id="TB1",
            issue_type=IssueType.LOGISTICS_STALLED.value,
            fact_digest={"stalled_hours": 56},
        ),
        collected,
    )
    assert output["action_type"] == ActionType.CONTACT_LOGISTICS.value
    # 金额来自政策文本里的数字，而不是模型计算
    assert output["amount"] == "30"
    assert "政策条款" in output["amount_source"]


def test_stalled_but_below_threshold_needs_no_compensation():
    output = run_deterministic(
        ResolutionAgent(),
        agent_input(
            "物流有点慢",
            order_id="TB1",
            issue_type=IssueType.LOGISTICS_STALLED.value,
            fact_digest={"stalled_hours": 12},
        ),
    )
    assert output["action_type"] == ActionType.CONTACT_LOGISTICS.value
    assert output["amount"] == "0"


def test_parse_amount_only_extracts():
    assert parse_amount("补偿标准为 50 元") == "50"
    assert parse_amount("赔付不超过 ¥30") == "30"
    assert parse_amount("没有任何数字描述") is None


# --------------------------------------------------------------------------- 受控决策（模型不得改写）
def test_decide_action_refund_references_paid_amount():
    """退款：金额只引用支付事实的实付金额。"""
    from app.agents.after_sales import decide_action

    decision = decide_action(
        query="申请退款，请把钱退给我",
        issue_type=IssueType.REFUND.value,
        facts={"paid_amount": "89.00", "order_status": "已签收"},
    )
    assert decision["action_type"] == ActionType.REFUND.value
    assert decision["amount"] == "89.00"
    assert "支付事实" in decision["amount_source"]
    assert decision["issue_type"] == IssueType.REFUND.value


def test_decide_action_is_pure_and_ignores_extra_facts():
    """纯函数：同样输入永远同样输出（模型润色不得影响它）。"""
    from app.agents.after_sales import decide_action

    first = decide_action(query="少件了", issue_type=IssueType.MISSING_ITEM.value, facts={})
    second = decide_action(query="少件了", issue_type=IssueType.MISSING_ITEM.value, facts={})
    assert first == second
    assert first["action_type"] == ActionType.RESHIP.value


def test_decide_action_stalled_uses_policy_amount_only():
    from app.agents.after_sales import decide_action

    without_policy = decide_action(
        query="物流停滞两天",
        issue_type=IssueType.LOGISTICS_STALLED.value,
        facts={"stalled_hours": 56},
    )
    assert without_policy["action_type"] == ActionType.CONTACT_LOGISTICS.value
    assert without_policy["amount"] is None
    assert any("补偿标准" in item for item in without_policy["missing_information"])

    with_policy = decide_action(
        query="物流停滞两天",
        issue_type=IssueType.LOGISTICS_STALLED.value,
        facts={"stalled_hours": 56},
        policy_text="停滞超过 48 小时：按订单实付金额的 5% 补偿，单笔补偿不超过 ¥30。",
    )
    assert with_policy["amount"] == "30"
    assert "政策条款" in with_policy["amount_source"]


@pytest.mark.parametrize(
    "facts",
    [
        {"order_status": "已签收", "latest_track": "已签收"},
        {"order_status": "signed", "latest_track": "signed by customer"},
        {"order_status": "completed", "latest_track": ""},
        {"order_status": "", "latest_track": "", "signed_at": "2026-09-24 09:05"},
        {"order_status": "paid", "latest_track": "delivered at front desk"},
    ],
)
def test_not_shipped_conflicts_with_fulfillment_evidence_and_must_not_reship(facts):
    """中文/英文状态、物流轨迹或签收时间任一证明已履约时，都不得重复补发。"""
    from app.agents.after_sales import decide_action

    facts = {"paid_amount": "156.00", **facts}
    decision = decide_action(
        query="我买的东西到现在还没有发货。",
        issue_type=IssueType.NOT_SHIPPED.value,
        facts=facts,
    )

    assert decision["action_type"] == ActionType.ASK_CUSTOMER.value
    assert decision["conflict"] is True
    assert any("实际履约情况" in item for item in decision["missing_information"])


@pytest.mark.parametrize("status", ["未发货", "not_shipped", "pending_shipment"])
def test_not_shipped_status_without_fulfillment_still_allows_reship(status):
    """明确的未发货状态不能被英文 shipped 子串误判为已履约。"""
    from app.agents.after_sales import decide_action

    decision = decide_action(
        query="一直没有发货。",
        issue_type=IssueType.NOT_SHIPPED.value,
        facts={"order_status": status},
    )

    assert decision["action_type"] == ActionType.RESHIP.value
    assert decision["conflict"] is False


def test_review_output_does_not_replace_existing_facts_with_model_gaps():
    """已存在的金额与物流事实不应再被质检列为缺失。"""
    from app.graph.nodes.after_sales import reconcile_review_warnings

    warnings = reconcile_review_warnings(
        [
            "缺少必要信息：订单实付金额、最后一次有效轨迹时间",
            "金额一致性无法校验：无支付事实与政策金额",
            "证据覆盖为 0：未命中任何政策条款",
        ],
        policy_count=1,
        fact_digest={
            "paid_amount": "156.00",
            "latest_track": "已签收",
            "signed_at": "2026-09-24 09:05",
        },
    )

    assert warnings == []


def test_rule_empty_missing_information_is_authoritative():
    """规则明确给出空列表时，不得回退采用模型臆测的缺失信息。"""
    from app.graph.nodes.after_sales import resolve_missing_information

    assert (
        resolve_missing_information(
            {"missing_information": []},
            {"missing_information": ["订单实付金额", "物流状态"]},
        )
        == []
    )
    assert resolve_missing_information({}, {"missing_information": ["订单号"]}) == ["订单号"]
    assert resolve_missing_information({"missing_information": None}, {"missing_information": ["订单号"]}) == []
    assert resolve_missing_information(
        {"missing_information": ["物流状态"]}, {"missing_information": ["订单号"]}
    ) == ["物流状态"]


def test_review_reads_generated_reply_and_blocks_unapproved_completed_refund():
    """启用反思后，质检必须读取最终草稿，而不是与回复生成并行。"""
    output = run_deterministic(
        ReviewAgent(settings=make_settings(reflection_enabled=True)),
        agent_input(
            "申请退款",
            reflection_enabled=True,
            draft_reply="您好，退款已完成，89 元会在 1-3 个工作日到账。",
            approval_status="not_required",
            decision={
                "issue_type": "refund",
                "action_type": "refund",
                "amount": "89.00",
                "amount_source": "支付事实（实付金额）",
                "risk_tier": "L3",
                "approval_requirement": "supervisor",
                "policy_refs": [{"title": "退款规则", "version": "v2"}],
            },
            resolution={"action_type": "refund", "amount": "89.00"},
            fact_digest={"paid_amount": "89.00", "order_status": "已支付"},
            policy_count=1,
        ),
    )

    assert output["verdict"] == "human_review"
    assert any(item["code"] == "unapproved_completed_action" for item in output["issues"])


def test_revision_agent_only_rewrites_reply_and_adds_approval_condition():
    output = run_deterministic(
        RevisionAgent(settings=make_settings(reflection_enabled=True)),
        agent_input(
            "申请退款",
            reflection_enabled=True,
            draft_reply="您好，我们将为您发起退款，预计 1-3 个工作日到账。",
            reflection_issues=[
                {
                    "code": "approval_condition_missing",
                    "severity": "medium",
                    "category": "reply",
                    "target": "ticket_reply",
                    "source": "code",
                    "rule_code": "approval_condition_missing",
                    "message": "回复没有明确说明动作需审批通过后才能执行",
                    "evidence_refs": [],
                    "auto_fixable": True,
                }
            ],
            decision={
                "action_type": "refund",
                "amount": "89.00",
                "approval_requirement": "supervisor",
            },
        ),
    )

    assert output["revised_text"]
    assert "审批通过后" in output["revised_text"]
    assert "退款已完成" not in output["revised_text"]
    assert output["addressed_issue_codes"] == ["approval_condition_missing"]


def test_reflection_enabled_routes_response_before_review():
    from app.graph.nodes.after_sales import route_after_resolution

    assert route_after_resolution({"filters": {"multi_agent": True, "reflection_enabled": True}}) == [
        "response_agent"
    ]
    assert route_after_resolution({"filters": {"multi_agent": False, "reflection_enabled": True}}) == [
        "response_agent"
    ]


def test_review_warning_keeps_still_missing_part_of_compound_warning():
    """复合警告只能核销已有事实，不能连仍缺失的字段一起吞掉。"""
    from app.graph.nodes.after_sales import reconcile_review_warnings

    warnings = reconcile_review_warnings(
        ["缺少必要信息：订单实付金额、最后一次有效轨迹时间"],
        policy_count=0,
        fact_digest={"paid_amount": "156.00"},
    )

    assert warnings == ["缺少必要信息：最后一次有效轨迹时间"]


def test_order_status_alone_does_not_prove_logistics_status_exists():
    """只有订单状态时，不能过滤掉真实的物流状态缺口。"""
    from app.graph.nodes.after_sales import reconcile_review_warnings

    warnings = reconcile_review_warnings(
        ["缺少必要信息：物流状态"],
        policy_count=0,
        fact_digest={"order_status": "已支付"},
    )

    assert warnings == ["缺少必要信息：物流状态"]


def test_review_warning_variants_cannot_contradict_policy_facts_or_controlled_action():
    """真实模型的同义改写也必须经过确定性一致性裁决。"""
    from app.graph.nodes.after_sales import reconcile_review_warnings

    warnings = reconcile_review_warnings(
        [
            "证据覆盖为0：无任何政策或事实证据支撑结论，所有判断均为待验证假设。",
            "证据为空：政策命中数0，任何结论均无证据支撑。",
            "金额一致性无法校验：缺少支付事实与政策条款中的金额数字。",
            "政策/事实冲突无法判定：无政策条款与订单事实可比对。",
            "工单未闭环：仍在等待外部处理（动作 reship）。",
            "存在未闭环的外部处理动作（reship），且已触发 48 小时升级阈值。",
            "工单处于等待外部处理（reship）状态，48小时后需升级。",
            "补充信息中的 followup 字段属于工单元数据，不构成订单事实证据。",
            "模型分类为 物流停滞，规则分类为 not_shipped，以规则为准",
            "模型判定 L4，规则判定 L3，以规则为准",
            "禁止自行计算或编造金额。",
        ],
        policy_count=1,
        fact_digest={
            "fact_count": 4,
            "paid_amount": "156.00",
            "latest_track": "已签收",
            "signed_at": "2026-09-24 09:05",
        },
        action_type="ask_customer",
    )

    assert warnings == [
        "系统识别结果存在分歧，已按订单事实和业务规则处理，请主管复核。",
        "禁止自行计算或编造金额。",
    ]


def test_missing_item_prefers_reship():
    output = run_deterministic(
        ResolutionAgent(),
        agent_input("只收到一件", order_id="TB1", issue_type=IssueType.MISSING_ITEM.value),
    )
    assert output["action_type"] == ActionType.RESHIP.value


# --------------------------------------------------------------------------- 质检与风险
def test_review_escalates_when_evidence_insufficient():
    output = run_deterministic(
        ReviewAgent(),
        agent_input(
            "申请退款",
            resolution={"action_type": ActionType.REFUND.value, "amount": "89.00"},
            policy_count=0,
            fact_digest={"paid_amount": "89.00"},
        ),
    )
    assert output["evidence_sufficient"] is False
    assert output["warnings"]
    # 证据不足不得停在 L1/L2 的"可自动"档位
    assert output["risk_tier"] in ("L3", "L4", "L2")
    assert output["approval_requirement"] != "none"


def test_review_flags_amount_over_paid():
    output = run_deterministic(
        ReviewAgent(),
        agent_input(
            "退我 500",
            resolution={"action_type": ActionType.REFUND.value, "amount": "500.00"},
            policy_count=1,
            fact_digest={"paid_amount": "89.00", "order_status": "已签收"},
        ),
    )
    assert output["amount_consistent"] is False
    assert any("超过实付" in w for w in output["warnings"])


def test_review_l4_for_batch_flag():
    output = run_deterministic(
        ReviewAgent(),
        agent_input(
            "批量退款",
            resolution={"action_type": ActionType.REFUND.value, "amount": "10"},
            policy_count=1,
            risk_flags=["batch_refund"],
            fact_digest={"paid_amount": "1000", "order_status": "已签收"},
        ),
    )
    assert output["risk_tier"] == "L4"
    assert output["escalate"] is True


def test_refund_is_l3_by_rule():
    output = run_deterministic(
        ReviewAgent(),
        agent_input(
            "申请退款",
            resolution={"action_type": ActionType.REFUND.value, "amount": "89"},
            policy_count=2,
            fact_digest={"paid_amount": "89", "order_status": "已签收"},
        ),
    )
    assert output["risk_tier"] == "L3"
    assert output["approval_requirement"] == "supervisor"
    assert output["risk_reason"]


def test_promotion_reply_explains_verified_threshold_and_excluded_item():
    reply = _reply_template(
        ActionType.REPLY_ONLY.value,
        "TB202609240821",
        {
            "eligible_amount": "179.00",
            "excluded_items": "冻干零食（不参与满减）",
            "promotion_threshold": "满 199 减 30",
        },
        {"issue_type": IssueType.PROMOTION_NOT_APPLIED.value},
    )

    assert "冻干零食" in reply
    assert "179" in reply
    assert "满 199 减 30" in reply
    assert "未达到" in reply
    assert "正在核实" not in reply


def test_promotion_reply_does_not_claim_a_cause_without_order_facts():
    reply = _reply_template(
        ActionType.REPLY_ONLY.value,
        "您的订单",
        {},
        {"issue_type": IssueType.PROMOTION_NOT_APPLIED.value},
    )

    assert "无法确认" in reply
    assert "截图" in reply
    assert "未达到活动门槛" not in reply


# --------------------------------------------------------------------------- 跟进
def test_followup_required_for_external_dependency():
    output = run_deterministic(
        FollowUpAgent(),
        agent_input("催物流", resolution={"action_type": ActionType.CONTACT_LOGISTICS.value}, risk={"risk_tier": "L2"}),
    )
    assert output["required"] is True
    assert output["next_check_hours"] >= 1
    assert output["reminder"]


def test_followup_not_required_for_pure_explanation():
    output = run_deterministic(
        FollowUpAgent(),
        agent_input("解释优惠", resolution={"action_type": ActionType.REPLY_ONLY.value}),
    )
    assert output["required"] is False
    assert output["reminder"] == ""


def test_review_agent_normalizes_model_issue_aliases():
    """真实模型会把 issues 写成 issue_type/description/evidence，不能整段丢弃。"""

    output, errors = ReviewAgent()._validate_output(
        {
            "summary": "模型质检",
            "verdict": "pass",
            "issues": [
                {
                    "issue_type": "evidence",
                    "description": "缺少政策依据",
                    "evidence": ["E1"],
                    "severity": "medium",
                }
            ],
            "required_changes": [],
            "confidence": 0.7,
        }
    )

    assert errors == []
    issue = output["issues"][0]
    assert issue["code"] == "evidence"
    assert issue["category"] == "evidence"
    assert issue["message"] == "缺少政策依据"
    assert issue["target"] == "ticket_reply"
    assert issue["source"] == "model"
    assert issue["evidence_refs"] == ["E1"]
    assert output["verdict"] == "pass"


def test_policy_query_includes_chinese_topic_terms():
    """英文类型名单独检索会被通用词带偏，必须拼上中文主题词。"""

    damaged_query = _policy_query("damaged", "处理规则 补偿标准")
    assert "破损" in damaged_query
    assert "包装" in damaged_query

    promotion_query = _policy_query("promotion_not_applied", "处理规则")
    assert "满减" in promotion_query


def test_policy_agent_normalizes_string_data_gaps():
    """模型把 data_gaps 写成字符串时，不能丢掉整段 policies/findings。"""

    output, errors = PolicyRagAgent()._validate_output(
        {
            "summary": "未检索到政策",
            "findings": [],
            "data_gaps": "未检索到与当前问题类型匹配的售后政策条款",
            "policies": [],
            "matched": 0,
        }
    )

    assert errors == []
    assert output["data_gaps"] == ["未检索到与当前问题类型匹配的售后政策条款"]


def test_review_agent_escalates_pass_with_high_severity_issue():
    output, errors = ReviewAgent()._validate_output(
        {
            "summary": "模型质检",
            "verdict": "pass",
            "issues": [
                {
                    "code": "policy_conflict",
                    "severity": "high",
                    "category": "policy",
                    "target": "ticket_reply",
                    "source": "model",
                    "message": "政策与事实冲突",
                }
            ],
            "confidence": 0.7,
        }
    )

    assert errors == []
    assert output["verdict"] == "human_review", "high 问题不能被 pass 放行"


def test_review_agent_normalizes_contradictory_pass_stop_reason():
    output, errors = ReviewAgent()._validate_output(
        {
            "summary": "需要人工复核",
            "verdict": "human_review",
            "issues": [
                {
                    "code": "missing_evidence",
                    "severity": "medium",
                    "category": "evidence",
                    "target": "ticket_reply",
                    "source": "model",
                    "message": "证据不足",
                }
            ],
            "confidence": 0.7,
            "stop_reason": "passed",
        }
    )

    assert errors == []
    assert output["verdict"] == "human_review"
    assert output["stop_reason"] is None


def test_reflection_marks_required_approval_as_pending():
    from contextlib import contextmanager

    from app.graph.nodes.after_sales import prepare_ticket_reflection

    class Context:
        def emit(self, *_args, **_kwargs):
            return None

    class Runtime:
        @contextmanager
        def node_session(self, *_args, **_kwargs):
            yield Context()

    output = prepare_ticket_reflection(
        {
            "approval_status": "not_required",
            "filters": {
                "decision": {"action_type": "refund", "amount": 268.0},
                "max_reflection_rounds": 1,
            },
            "agent_results": {
                "response_agent": {"blocks": {"customer_reply": "正在处理退款"}},
                "policy_rag_agent": {"blocks": {"policies": []}},
            },
        },
        runtime=Runtime(),
    )

    assert output["filters"]["decision"]["approval_requirement"] == "supervisor"
    assert output["filters"]["approval_status"] == "pending"
