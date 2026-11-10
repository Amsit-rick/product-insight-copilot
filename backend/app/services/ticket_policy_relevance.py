"""售后政策引用的场景相关性闸门。

向量召回只代表文本相近，不等于政策适用于当前售后问题。这里按确定性问题类型
要求命中对应主题词；证据不明确时宁可不展示政策，也不把其他场景规则当作依据。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

POLICY_TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    # Do not match incidental phrases such as "不直接退款" in a compensation policy.
    # A refund reference must identify a refund-specific clause or rule.
    "refund": (
        "退货退款",
        "退款与审批",
        "退款申请",
        "申请退款",
        "退款金额",
        "退款条件",
        "退款资格",
        "退款时效",
        "退款审核",
        "退款规则",
        "退款政策",
        "退款标准",
    ),
    "return": ("退货", "无理由", "退换货", "退回"),
    "exchange": ("换货", "换新", "退换货"),
    "logistics_stalled": ("物流", "快递", "停滞", "签收", "运单", "配送", "承运商"),
    "missing_item": ("少件", "漏发", "缺件", "少发", "出库", "包裹重量"),
    "damaged": ("破损", "损坏", "包装", "商品照片", "质量问题", "碎裂"),
    "wrong_item": ("错发", "发错", "商品不符", "型号不符", "商品型号"),
    "not_shipped": ("未发货", "未出库", "发货时效", "发货"),
    # A logistics policy may mention issuing compensation as a coupon. Require explicit promotion terms.
    "promotion_not_applied": (
        "满减",
        "促销活动",
        "优惠活动",
        "活动优惠",
        "优惠券规则",
        "优惠券使用",
        "折扣规则",
        "活动门槛",
        "优惠规则",
    ),
    "description_mismatch": ("描述不符", "与描述", "商品描述", "商品参数", "规格不符"),
}


def filter_policy_evidence(issue_type: str, evidence: Iterable[Mapping]) -> list[dict]:
    """只保留标题、章节或正文明确包含当前问题主题的政策证据。"""
    terms = POLICY_TOPIC_TERMS.get(str(issue_type or ""), ())
    if not terms:
        return []

    filtered = []
    for item in evidence:
        text = " ".join(
            str(item.get(key) or "")
            for key in ("source_name", "title", "section_title", "content", "snippet")
        )
        if any(term in text for term in terms):
            filtered.append(dict(item))
    return filtered


__all__ = ["POLICY_TOPIC_TERMS", "filter_policy_evidence"]
