"""风险分级与审批要求（需求书 §3.3 / §4.3）。

需求书只给了"条件示例"（L3 = 退款、较高金额赔付、责任争议、敏感用户），
没给金额阈值。所以这里的做法是：**规则写成可配置阈值**，默认值明确标注为 MVP 假设，
并提供 `explain()` 让人能问"为什么这条是 L3" —— 审批链路上不能出现"不知道为什么"的分级。

两条硬纪律（需求书 §1.2 / §4.3）：

- **写操作永不自动执行**：L3/L4 必须有人拍板，L4 连人工都不在客服侧（转风控）；
- **金额只能来自规则与适配器**：本模块只做"按金额分级"，不计算金额本身。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.models.enums import ActionType, ApprovalRequirement, RiskTier

# 写操作类动作：会动钱或动物流，MVP 一律不允许 Agent 自动执行
WRITE_ACTIONS: set[str] = {
    ActionType.REFUND.value,
    ActionType.RETURN_GOODS.value,
    ActionType.EXCHANGE.value,
    ActionType.RESHIP.value,
    ActionType.COMPENSATE.value,
    ActionType.COUPON.value,
}

# 命中这些标记直接 L4（需求书：批量退款、账户限制、疑似欺诈）
L4_FLAGS: set[str] = {"batch_refund", "account_restriction", "suspected_fraud"}

# 风险等级 → 谁能拍板
TIER_TO_APPROVAL: dict[str, str] = {
    RiskTier.L1.value: ApprovalRequirement.NONE.value,
    RiskTier.L2.value: ApprovalRequirement.STAFF_CONFIRM.value,
    RiskTier.L3.value: ApprovalRequirement.SUPERVISOR.value,
    RiskTier.L4.value: ApprovalRequirement.RISK_CONTROL.value,
}

# 自动化边界：L1/L2 可以自动出草稿（L2 需客服确认），L3/L4 不允许自动推进
AUTO_DRAFT_TIERS: set[str] = {RiskTier.L1.value, RiskTier.L2.value}


@dataclass
class RiskAssessment:
    tier: str
    reason: str
    approval_requirement: str

    @property
    def auto_draft_allowed(self) -> bool:
        return self.tier in AUTO_DRAFT_TIERS

    def as_dict(self) -> dict:
        return {
            "risk_tier": self.tier,
            "risk_reason": self.reason,
            "approval_requirement": self.approval_requirement,
            "auto_draft_allowed": self.auto_draft_allowed,
        }


def assess(
    *,
    action_type: str,
    amount: float | None = None,
    flags: list[str] | None = None,
    conflict: bool = False,
    sensitive_customer: bool = False,
    settings: Settings | None = None,
) -> RiskAssessment:
    """判定风险等级。返回的 `reason` 直接展示给客服与审核人，所以必须是人话。"""
    settings = settings or get_settings()
    flags = set(flags or [])
    amount_value = float(amount or 0)

    # ① L4：命中风控标记，先于一切金额规则（批量退款再小额也是 L4）
    hit = sorted(flags & L4_FLAGS)
    if hit:
        return _make(RiskTier.L4.value, f"命中风控标记：{', '.join(hit)}（不允许自动执行）")

    # ② 金额超过风控线：即使动作本身是"低金额赔付"也要转风控
    if amount_value >= settings.ticket_l4_amount_limit:
        return _make(
            RiskTier.L4.value,
            f"赔付金额 {amount_value:.2f} 元达到风控线 {settings.ticket_l4_amount_limit:.0f} 元",
        )

    # ③ L3：退款、责任争议、敏感用户、较高金额
    if action_type == ActionType.REFUND.value:
        return _make(RiskTier.L3.value, "涉及退款，需主管审批后执行")
    if conflict:
        return _make(RiskTier.L3.value, "存在政策或事实冲突，需人工裁定")
    if sensitive_customer:
        return _make(RiskTier.L3.value, "敏感用户（投诉/纠纷历史），需主管审批")
    if amount_value >= settings.ticket_l3_amount_limit:
        return _make(
            RiskTier.L3.value,
            f"赔付金额 {amount_value:.2f} 元超过 {settings.ticket_l3_amount_limit:.0f} 元，需主管审批",
        )

    # ④ L2：会动钱/物流的动作，或达到"客服确认线"的赔付
    #
    # 为什么 L2 也要看金额线（踩过的坑）：原来这条分支写的是"amount > 0 就是 L2"，
    # 于是配置项 `ticket_l2_amount_limit`（默认 100 元）**从来没有被读过** ——
    # 一个"可配置阈值"实际是死配置：没人能把 3 元的小额补偿调成自动草稿，
    # 也没人能把线调高来减少客服确认量。现在按配置判定：
    #   写操作类动作 → 一律 L2（永不自动执行，这是 §1.2 的硬纪律）；
    #   非写操作动作 + 金额达到确认线 → L2；
    #   非写操作动作 + 金额未达确认线（含 0）→ L1（只是解释/追问，客服发送前仍会看到草稿）。
    if action_type in WRITE_ACTIONS:
        return _make(RiskTier.L2.value, f"动作 {action_type} 会改变订单或金额，需客服确认后执行")
    if amount_value >= settings.ticket_l2_amount_limit:
        return _make(
            RiskTier.L2.value,
            f"涉及 {amount_value:.2f} 元赔付，达到客服确认线 {settings.ticket_l2_amount_limit:.0f} 元",
        )

    # ⑤ L1：只是解释/追问，且未达客服确认线
    if amount_value > 0:
        return _make(
            RiskTier.L1.value,
            f"仅查询政策并提供说明：涉及 {amount_value:.2f} 元补偿标准，"
            f"未达客服确认线 {settings.ticket_l2_amount_limit:.0f} 元，可自动生成草稿",
        )
    return _make(RiskTier.L1.value, "仅查询政策并提供说明，金额为 0，可自动生成草稿")


def _make(tier: str, reason: str) -> RiskAssessment:
    return RiskAssessment(tier=tier, reason=reason, approval_requirement=TIER_TO_APPROVAL[tier])


def approval_for_tier(tier: str) -> str:
    return TIER_TO_APPROVAL.get(tier, ApprovalRequirement.SUPERVISOR.value)


def auto_draft_allowed(tier: str) -> bool:
    return tier in AUTO_DRAFT_TIERS


def requires_human_approval(tier: str) -> bool:
    """L3/L4 需要"审批链"上的动作（L2 只是客服确认，不走审批单）。"""
    return tier in {RiskTier.L3.value, RiskTier.L4.value}
