"""事实补全服务：订单/支付/物流/售后一次取齐并落库。

需求书 §2.2 把"查询订单/物流事实"放在分类之后、检索政策之前 —— 顺序有业务含义：
**先有事实才谈得上政策适用**（同一句"我要退款"，事实不同结论完全不同：
未发货可以直接退，已签收要判断签收异常）。

设计要点：

1. **并行不必要，但顺序固定**：四个查询互不依赖，但量小（4 次调用），顺序执行更好排障；
   真实接口慢时再改成并发（协议不变）。
2. **部分失败不阻断**：任何一个来源失败 → 落一条带 `error` 的事实，
   并把工单需要的缺失信息写进 `missing_fields`（例如订单号缺失 → NEED_CUSTOMER_INFO）。
3. **事实是快照**：每次补全都新增一批事实（保留历史观测），
   由展示层取"最新一条"，这样"3 小时前查到的轨迹"和"现在的轨迹"都查得到。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from app.adapters.base import AdapterResult
from app.adapters.registry import get_fact_adapter
from app.config import Settings, get_settings
from app.models.ticket import Ticket
from app.services.ticket_service import TicketService


class FactCollection:
    def __init__(self, results: list[AdapterResult]) -> None:
        self.results = results

    @property
    def ok_results(self) -> list[AdapterResult]:
        return [item for item in self.results if item.ok]

    @property
    def failures(self) -> list[AdapterResult]:
        return [item for item in self.results if not item.ok]

    @property
    def facts(self) -> list[dict]:
        return [item.as_fact() for item in self.results]

    @property
    def has_order_facts(self) -> bool:
        """是否拿到了可用的订单事实（没有订单号/订单不存在时全为 False）。"""
        return any(item.ok and item.source == "order" for item in self.results)

    @property
    def missing(self) -> list[str]:
        """需要客户补充的信息（喂给 NEED_CUSTOMER_INFO 状态与人话提示）。"""
        missing: list[str] = []
        source_unavailable = False
        for item in self.failures:
            code = (item.error or {}).get("code")
            if code == "missing_order_id":
                missing.append("订单号")
            elif code == "order_not_found":
                missing.append("正确的订单号（按该单号查不到订单）")
            elif code in {
                "adapter_timeout",
                "adapter_unavailable",
                "adapter_auth_error",
                "adapter_rate_limited",
                "adapter_upstream_error",
                "adapter_invalid_response",
                "adapter_request_rejected",
            }:
                source_unavailable = True
            else:
                missing.append(f"{item.source} 事实缺失")
        if source_unavailable:
            missing.append("订单事实来源暂不可用，请客服人工核实后再继续")
        return sorted(set(missing))

    def summary(self) -> dict:
        return {
            "sources": sorted({item.source for item in self.results}),
            "ok": len(self.ok_results),
            "failed": len(self.failures),
            "missing": self.missing,
            "is_mock": all(item.is_mock for item in self.results) if self.results else True,
        }


def collect_facts(
    *,
    order_id: str | None,
    customer_id: str | None = None,
    request_id: str | None = None,
    settings: Settings | None = None,
) -> FactCollection:
    """按订单号取齐四类事实。返回结构化结果（含失败项），不抛异常。"""
    settings = settings or get_settings()
    adapter = get_fact_adapter(settings)
    resolved_order_id = order_id or ""
    # 四个来源互不依赖。真实 HTTP 端点必须并行，否则 3 秒超时 × 2 次重试 × 4 个来源
    # 会把单次补全的最坏耗时放大到 36 秒，直接越过需求书的 20 秒 P95 目标。
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="ticket-fact") as executor:
        futures = [
            executor.submit(adapter.fetch_order, resolved_order_id, request_id=request_id),
            executor.submit(adapter.fetch_payment, resolved_order_id, request_id=request_id),
            executor.submit(adapter.fetch_logistics, resolved_order_id, request_id=request_id),
            executor.submit(
                adapter.fetch_aftersale,
                resolved_order_id,
                customer_id=customer_id,
                request_id=request_id,
            ),
        ]
        # 按提交顺序取结果，保持订单/支付/物流/售后的稳定展示顺序。
        results = [future.result() for future in futures]
    return FactCollection(results)


def enrich_ticket(
    service: TicketService,
    ticket: Ticket,
    *,
    request_id: str | None = None,
    settings: Settings | None = None,
) -> FactCollection:
    """给工单补事实并落库，同时把"缺什么"写到工单上。"""
    settings = settings or get_settings()
    collection = collect_facts(
        order_id=ticket.order_id,
        customer_id=ticket.customer_id,
        request_id=request_id,
        settings=settings,
    )
    service.save_facts(ticket.id, collection.facts)

    ticket.missing_fields = collection.missing
    # 事实查询失败（缺单号/查不到/超时）→ 工单要显式告诉人，而不是假装有事实
    if not collection.has_order_facts:
        from app.models.enums import TicketStatus
        from app.services.ticket_state import can_transition

        if can_transition(ticket.status, TicketStatus.NEED_CUSTOMER_INFO.value):
            service.transition(
                ticket,
                TicketStatus.NEED_CUSTOMER_INFO.value,
                actor_id=None,
                reason="事实查询未取得订单信息：" + "、".join(collection.missing or ["未知原因"]),
            )
        else:
            # A retry may already be in NEED_CUSTOMER_INFO. Persist refreshed failure details
            # even when the state machine correctly declines a no-op transition.
            service.session.commit()
    else:
        service.session.commit()
    return collection
