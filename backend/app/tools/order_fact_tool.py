"""订单事实查询工具（只读，需求书 §4.2 的 Adapters 层出口）。

为什么事实查询要包成"工具"而不是让 Agent 直接 import 适配器：

1. **权限与只读在工具层强制**：`PolicyEngine.check_tool_call` 会拒绝任何带写意图的参数，
   并校验 `order_id`；Agent 拿不到绕过路径；
2. **可审计**：每次调用都会记进 `tool_calls` 与 `agent_runs`，事后能查"这条事实是哪次调用来的"；
3. **可替换**：换成真实订单中心接口时只改适配器实现，工具契约不变。
"""

from __future__ import annotations

from app.services.order_fact_service import collect_facts
from app.tools.registry import ReplayPolicy, SideEffectLevel, ToolSpec


def order_facts(*, ctx, order_id: str | None = None, customer_id: str | None = None) -> dict:
    """查询订单/支付/物流/售后四类事实（只读，返回带观测时间与请求号的快照）。"""
    collection = collect_facts(
        order_id=order_id,
        customer_id=customer_id or None,
        request_id=f"tool-order-facts:{ctx.task_id}",
    )
    return {
        "facts": [item.as_fact() for item in collection.results],
        "summary": collection.summary(),
        "missing": collection.missing,
        "has_order_facts": collection.has_order_facts,
    }


SPEC = ToolSpec(
    name="order.facts",
    description="只读查询订单、支付、物流与售后事实（返回来源、观测时间与请求号）",
    handler=order_facts,
    replay_policy=ReplayPolicy.SAFE,
    side_effect_level=SideEffectLevel.READ_ONLY,
    arg_schema={
        "type": "object",
        "properties": {
            "order_id": {"type": "string", "description": "订单号"},
            "customer_id": {"type": "string", "description": "客户 ID（可选，用于售后历史）"},
        },
        "required": ["order_id"],
    },
    # 事实查询不碰数据库会话（走适配器），保持无会话以便未来换成 HTTP 适配器时无需改签名
    requires_session=False,
)

__all__ = ["SPEC", "order_facts"]
