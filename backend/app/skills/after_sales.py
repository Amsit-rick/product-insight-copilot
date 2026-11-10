"""Deterministic business skills for the after-sales workflow.

Skills add evidence checklists to the responsible agent. They do not grant tools
or authority and cannot change rule-owned actions, amounts, risk, or approvals.
"""

from __future__ import annotations

from dataclasses import dataclass

VERSION = "1.0.0"


@dataclass(frozen=True)
class BusinessSkill:
    code: str
    issue_types: tuple[str, ...]
    required_evidence: tuple[str, ...]
    agent_guidance: dict[str, str]


_SKILLS = {
    "promotion_explanation": BusinessSkill(
        code="promotion_explanation",
        issue_types=("promotion_not_applied",),
        required_evidence=("活动门槛", "订单商品与金额", "优惠适用条件", "政策有效期"),
        agent_guidance={
            "triage_agent": "确认客户询问的是哪项优惠及结算时出现的现象，不推断活动资格。",
            "order_fact_agent": "优先核对订单商品、实付金额及可用的优惠记录；缺少活动记录时明确标记缺口。",
            "policy_rag_agent": "核对活动门槛、适用商品/渠道和有效时间，只引用已检索到的规则。",
            "response_agent": "解释已确认的门槛或缺失信息，不承诺补差价或额外发券。",
            "review_agent": "检查优惠结论是否同时有订单事实和适用规则支持。",
        },
    ),
    "refund_review": BusinessSkill(
        code="refund_review",
        issue_types=("refund", "return"),
        required_evidence=("订单状态", "实付金额", "退款/售后历史", "退款适用条件"),
        agent_guidance={
            "order_fact_agent": "核对订单状态、实付金额和已有售后/退款记录；不得把订单原价当成实付金额。",
            "policy_rag_agent": "查找与订单状态和退款原因相匹配的有效退款条件。",
            "resolution_agent": "仅引用支付事实中的实付金额，不自行推算可退金额，并指出仍需人工审批的事项。",
            "response_agent": "退款未获批准前不得写成已退款或已受理执行。",
            "review_agent": "核对建议金额不超过可用支付事实，检查历史售后、证据和审批说明。",
        },
    ),
    "logistics_exception": BusinessSkill(
        code="logistics_exception",
        issue_types=("logistics_stalled", "not_shipped"),
        required_evidence=("发货状态", "最近物流轨迹", "轨迹时间", "跟进期限"),
        agent_guidance={
            "order_fact_agent": "核对发货状态、承运商、最近有效轨迹和时间；物流接口无数据时不能推断停滞时长。",
            "policy_rag_agent": "核对适用的物流异常阈值、催件方式和跟进期限。",
            "resolution_agent": "结合已核实的轨迹和规则给出下一步；不把联系承运商说成已完成赔付。",
            "followup_agent": "依据已验证的停滞时长和处理方案设置复查时间，缺少时间事实时说明缺口。",
            "review_agent": "检查物流轨迹是否足以支持异常结论及补偿承诺。",
        },
    ),
    "fulfillment_exception": BusinessSkill(
        code="fulfillment_exception",
        issue_types=("missing_item", "wrong_item"),
        required_evidence=("购买商品和数量", "包裹/发货记录", "客户描述", "补发适用规则"),
        agent_guidance={
            "order_fact_agent": "比对订单商品与数量、包裹或发货记录；不要仅凭描述直接确认漏发或错发。",
            "policy_rag_agent": "核对少件、错发的举证、补发和退换条件。",
            "resolution_agent": "只有事实和政策支持时才建议补发/换货；不足时列明待核实信息。",
            "response_agent": "说明核实结果和下一步，不承诺尚未批准的补发。",
            "review_agent": "检查商品、数量、包裹事实与建议动作是否一致。",
        },
    ),
    "damage_evidence": BusinessSkill(
        code="damage_evidence",
        issue_types=("damaged", "description_mismatch"),
        required_evidence=("破损/差异描述", "照片或其他凭证", "签收状态", "退换适用条件"),
        agent_guidance={
            "order_fact_agent": "核对签收时间和已有售后记录；没有附件时明确提示需要补充凭证。",
            "policy_rag_agent": "核对破损/描述不符对应的照片要求、处理时限和退换条件。",
            "resolution_agent": "证据不足时提出补证，不把破损推测写成已核实事实。",
            "response_agent": "清楚说明需要的照片或材料，不提前承诺退款、补发或赔付。",
            "review_agent": "确认是否具备凭证、签收信息和匹配的政策依据。",
        },
    ),
    "quality_guard": BusinessSkill(
        code="quality_guard",
        issue_types=(),
        required_evidence=("事实来源", "政策版本", "金额来源", "回复承诺", "审批要求"),
        agent_guidance={
            "resolution_agent": "业务规则已经确定动作、金额、风险和审批要求；不得更改这些受控字段。",
            "response_agent": "只陈述核实到的事实，不披露内部规则编号，不承诺未批准的写操作。",
            "review_agent": "针对退款建议核对实付依据、历史售后、证据缺口、回复承诺和主管审批要求的一致性。",
        },
    ),
}

_ISSUE_SKILL = {
    issue_type: code
    for code, skill in _SKILLS.items()
    if code != "quality_guard"
    for issue_type in skill.issue_types
}


def select_business_skills(issue_type: str, multi_agent: bool) -> list[dict[str, str]]:
    """Return deterministic, versioned skill metadata for one issue and route."""
    codes: list[str] = []
    scenario_skill = _ISSUE_SKILL.get(issue_type)
    if scenario_skill:
        codes.append(scenario_skill)
    if multi_agent:
        codes.append("quality_guard")
    return [{"code": code, "version": VERSION} for code in codes]


def skill_prompt_for_agent(agent_code: str, skills: list[dict] | None) -> str:
    sections = []
    for item in skills or []:
        skill = _SKILLS.get(str(item.get("code") or ""))
        guidance = skill.agent_guidance.get(agent_code) if skill else None
        if guidance:
            sections.append(f"- {skill.code} v{item.get('version') or VERSION}：{guidance}")
    if not sections:
        return ""
    return (
        "\n\n【场景核验清单】以下内容仅用于提示需要核对的证据和流程，"
        "不授予工具权限，也不得改变规则决定的动作、金额、风险等级或审批要求。\n"
        + "\n".join(sections)
    )


def skills_for_agent(agent_code: str, skills: list[dict] | None) -> list[dict[str, str]]:
    result = []
    for item in skills or []:
        code = str(item.get("code") or "")
        skill = _SKILLS.get(code)
        if skill and agent_code in skill.agent_guidance:
            result.append({"code": code, "version": str(item.get("version") or VERSION)})
    return result


def list_agent_skills() -> list[dict]:
    return [
        {
            "code": skill.code,
            "version": VERSION,
            "issue_types": list(skill.issue_types),
            "required_evidence": list(skill.required_evidence),
            "agent_codes": sorted(skill.agent_guidance),
            "allowed_actions": [],
            "may_change_decision": False,
        }
        for skill in _SKILLS.values()
    ]


__all__ = [
    "VERSION",
    "list_agent_skills",
    "select_business_skills",
    "skill_prompt_for_agent",
    "skills_for_agent",
]
