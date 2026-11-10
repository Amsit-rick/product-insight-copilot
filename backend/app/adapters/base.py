"""外部事实适配器接口（订单 / 支付 / 物流 / 售后）。

## 为什么先定接口再写实现

需求书 §4.2 把 `Order/Payment/Logistics Adapters` 画成独立一层，§11 又把
"真实接入使用何种订单/工单接口、接口权限由谁提供"列为**开放问题**。
所以 MVP 必须做到：**换真实接口时只实现这一层，业务代码一行不改**。

## 三条硬约束（来自需求书）

1. **只读**：事实适配器只能查询，绝不能有写操作（§1.2 不自动执行退款/赔付）；
2. **必须带来源与观测时间**：`observed_at` + `request_id`，界面才能标"来源/查询时间/是否实时"（FR-02）；
3. **必须标 `is_mock`**：MVP 用模拟数据，界面与审计里要能一眼看出（§1 数据说明）。

失败也必须是**结构化事实**：接口超时要落成一条带 `error` 的事实，
而不是抛异常把整张工单打挂 —— 需求书 §4.3 要求"接口过期/超时进人工处理"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass
class AdapterResult:
    """一次事实查询的结果（成功或失败都返回它，不抛异常）。"""

    source: str
    fact_type: str
    payload: dict = field(default_factory=dict)
    observed_at: datetime | None = None
    request_id: str | None = None
    is_mock: bool = True
    adapter: str = ""
    error: dict | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_fact(self) -> dict:
        """转成 `TicketService.save_facts` 能吃的结构。"""
        return {
            "source": self.source,
            "fact_type": self.fact_type,
            "payload": self.payload,
            "observed_at": self.observed_at,
            "request_id": self.request_id,
            "is_mock": self.is_mock,
            "adapter": self.adapter,
            "error": self.error,
        }


class FactAdapter(Protocol):
    """事实适配器协议。真实实现（HTTP 对接订单中心/物流网关）只需满足它。"""

    name: str
    is_mock: bool

    def fetch_order(self, order_id: str, *, request_id: str | None = None) -> AdapterResult: ...

    def fetch_payment(self, order_id: str, *, request_id: str | None = None) -> AdapterResult: ...

    def fetch_logistics(self, order_id: str, *, request_id: str | None = None) -> AdapterResult: ...

    def fetch_aftersale(
        self, order_id: str, *, customer_id: str | None = None, request_id: str | None = None
    ) -> AdapterResult: ...


class AdapterUnavailable(Exception):
    """适配器不可用（未配置真实接口时由注册表抛出，调用方决定降级策略）。"""

    code = "adapter_unavailable"
