"""模拟订单/支付/物流/售后适配器（MVP 数据源，需求书 §1 数据说明）。

## 设计要点

1. **确定性**：同一个 `order_id` 每次都返回同一份事实。用 `hashlib` 从订单号派生，
   而不是用随机数 —— 否则重跑分析会得到不同事实，"同一工单两次分析结论不同"就无从排查。
2. **演示数据可复现**：内置 6 单与原型里的 6 张工单一一对应（物流签收未收到、少件、退款、
   破损、优惠未生效、物流停滞），其余订单号走派生逻辑，保证任何工单都能补到事实。
3. **故意留了两种"接口异常"**：`order_id` 以 `TIMEOUT` 结尾时返回超时事实，
   以 `NODATA` 结尾时返回空数据 —— 需求书 §9 要求验收"物流接口超时"这一场景。
4. **脱敏**：收件信息在适配器出口就按需保留城市级（展示层还会再脱一次），
   避免敏感数据一路裸奔到证据快照里。
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

from app.adapters.base import AdapterResult
from app.models.base import utcnow
from app.services.masking import mask_address, mask_phone

# 与原型 6 张演示工单对应的订单事实（保持同一套故事，演示时前后一致）
_DEMO_ORDERS: dict[str, dict] = {
    "TB202609241806": {
        "order": {
            "order_status": "已签收",
            "order_amount": "268.00",
            "order_items": "猫粮 5kg × 1",
            "order_channel": "手淘",
            "address": "上海市浦东新区张江路 88 弄 3 号楼 501 室",
            "receiver_phone": "13812345678",
        },
        "payment": {"paid_amount": "268.00", "refundable_amount": "268.00", "pay_channel": "支付宝"},
        "logistics": {
            "carrier": "顺丰速运",
            "tracking_no": "SF1234567890",
            "latest_track": "已签收（本人签收）",
            "signed_at": "2026-09-23 14:22",
            "stalled_hours": "0",
            "eta": "2026-09-23",
        },
        "aftersale": {"aftersale_history": "无历史售后", "last_case_result": "—"},
    },
    "TB202609241742": {
        "order": {
            "order_status": "已签收",
            "order_amount": "156.00",
            "order_items": "猫砂 10L × 2",
            "order_channel": "手淘",
            "address": "杭州市西湖区文一西路 100 号 2 幢 801",
            "receiver_phone": "13900001111",
        },
        "payment": {"paid_amount": "156.00", "refundable_amount": "156.00", "pay_channel": "微信支付"},
        "logistics": {
            "carrier": "中通快递",
            "tracking_no": "ZT9988776655",
            "latest_track": "已签收",
            "signed_at": "2026-09-24 09:05",
            "stalled_hours": "0",
            "package_weight": "9.6kg（订单标注 19.2kg）",
            "eta": "2026-09-24",
        },
        "aftersale": {"aftersale_history": "无历史售后", "last_case_result": "—"},
    },
    "TB202609241501": {
        "order": {
            "order_status": "已签收",
            "order_amount": "89.00",
            "order_items": "逗猫棒套装 × 1",
            "order_channel": "天猫",
            "address": "北京市海淀区中关村大街 1 号 A 座 1201",
            "receiver_phone": "13700002222",
        },
        "payment": {"paid_amount": "89.00", "refundable_amount": "89.00", "pay_channel": "支付宝"},
        "logistics": {
            "carrier": "圆通速递",
            "tracking_no": "YT1122334455",
            "latest_track": "已签收",
            "signed_at": "2026-09-22 17:40",
            "stalled_hours": "0",
            "eta": "2026-09-22",
        },
        "aftersale": {
            "aftersale_history": "2026-09-10 同类商品退货 1 次（已完结）",
            "last_case_result": "退货退款已结案",
        },
    },
    "TB202609240992": {
        "order": {
            "order_status": "已签收",
            "order_amount": "329.00",
            "order_items": "猫爬架 × 1",
            "order_channel": "抖音商城",
            "address": "广州市天河区体育西路 200 号 3 栋 502",
            "receiver_phone": "13600003333",
            "category": "家具/宠物用品",
            "special_category": "否",
        },
        "payment": {"paid_amount": "329.00", "refundable_amount": "329.00", "pay_channel": "抖音支付"},
        "logistics": {
            "carrier": "德邦快递",
            "tracking_no": "DB5566778899",
            "latest_track": "已签收",
            "signed_at": "2026-09-24 11:18",
            "stalled_hours": "0",
            "eta": "2026-09-24",
        },
        "aftersale": {"aftersale_history": "无历史售后", "last_case_result": "—"},
    },
    "TB202609240821": {
        "order": {
            "order_status": "已完成",
            "order_amount": "219.00",
            "order_items": "冻干零食 × 3",
            "order_channel": "手淘",
            "address": "成都市武侯区天府大道 500 号 1 栋 903",
            "receiver_phone": "13500004444",
        },
        "payment": {"paid_amount": "189.00", "refundable_amount": "189.00", "pay_channel": "支付宝"},
        "logistics": {
            "carrier": "韵达快递",
            "tracking_no": "YD2233445566",
            "latest_track": "已签收",
            "signed_at": "2026-09-21 15:02",
            "stalled_hours": "0",
            "eta": "2026-09-21",
        },
        "aftersale": {
            "aftersale_history": "2026-09-20 咨询满减规则 1 次",
            "last_case_result": "已解释活动规则",
            "gross_amount": "219.00",
            "eligible_amount": "179.00",
            "excluded_items": "冻干零食（不参与满减）",
            "promotion_threshold": "满 199 减 30",
            "promotion_result": "排除商品后未达门槛，未减 30 元",
        },
    },
    "TB202609240655": {
        "order": {
            "order_status": "已发货",
            "order_amount": "428.00",
            "order_items": "猫窝 × 1",
            "order_channel": "手淘",
            "address": "南京市鼓楼区中山北路 300 号 2 栋 601",
            "receiver_phone": "13400005555",
        },
        "payment": {"paid_amount": "428.00", "refundable_amount": "428.00", "pay_channel": "支付宝"},
        "logistics": {
            "carrier": "邮政 EMS",
            "tracking_no": "EMS6677889900",
            "latest_track": "离开【杭州转运中心】，发往【南京】",
            "signed_at": "",
            "stalled_hours": "56",
            "eta": "2026-09-22（已超时 2 天）",
        },
        "aftersale": {"aftersale_history": "无历史售后", "last_case_result": "—"},
    },
}


def _seed(order_id: str) -> int:
    return int(hashlib.sha256(order_id.encode("utf-8")).hexdigest()[:8], 16)


class MockFactAdapter:
    """确定性的模拟事实源。真实接入时实现 `FactAdapter` 协议即可替换掉它。"""

    name = "mock-adapter"
    is_mock = True

    def __init__(self, *, failing_order_suffix: str = "TIMEOUT", empty_suffix: str = "NODATA") -> None:
        self.failing_order_suffix = failing_order_suffix
        self.empty_suffix = empty_suffix

    # ---------------------------------------------------------------- 内部
    def _fixture(self, order_id: str) -> dict:
        if order_id in _DEMO_ORDERS:
            return _DEMO_ORDERS[order_id]
        # 派生一套自洽的事实：金额/重量/轨迹都由订单号决定，保证可复现
        seed = _seed(order_id)
        amount = f"{(seed % 900) + 39}.00"
        stalled = seed % 5
        return {
            "order": {
                "order_status": "已签收" if seed % 3 else "已发货",
                "order_amount": amount,
                "order_items": f"商品 SKU-{seed % 9000:04d} × {seed % 3 + 1}",
                "order_channel": ["手淘", "天猫", "抖音商城", "京东"][seed % 4],
                "address": f"{['上海市', '杭州市', '北京市', '成都市', '南京市'][seed % 5]}某区某路 {seed % 200} 号",
                "receiver_phone": f"13{seed % 900000000 + 100000000}"[:11],
            },
            "payment": {
                "paid_amount": amount,
                "refundable_amount": amount,
                "pay_channel": ["支付宝", "微信支付", "抖音支付"][seed % 3],
            },
            "logistics": {
                "carrier": ["顺丰速运", "中通快递", "圆通速递", "韵达快递"][seed % 4],
                "tracking_no": f"SF{seed % 10**10:010d}",
                "latest_track": "已签收" if stalled == 0 else "运输中：包裹停滞",
                "signed_at": "" if stalled else "2026-09-24 10:00",
                "stalled_hours": str(stalled * 24),
                "eta": "2026-09-24",
            },
            "aftersale": {"aftersale_history": "无历史售后", "last_case_result": "—"},
        }

    def _result(
        self, *, source: str, order_id: str, payload: dict, request_id: str | None
    ) -> AdapterResult:
        now = utcnow()
        # 事实出口脱敏：收件信息保留城市级、手机号打码
        safe = dict(payload)
        if "address" in safe:
            safe["address"] = mask_address(str(safe["address"]))
        if "receiver_phone" in safe:
            safe["receiver_phone"] = mask_phone(str(safe["receiver_phone"]))
        return AdapterResult(
            source=source,
            fact_type="snapshot",
            payload=safe,
            observed_at=now,
            request_id=request_id or f"mock-{source}-{order_id}-{int(now.timestamp())}",
            is_mock=True,
            adapter=self.name,
        )

    def _guard(self, *, source: str, order_id: str, request_id: str | None) -> AdapterResult | None:
        """模拟两类接口异常：超时、查无数据（需求书 §9 验收场景）。"""
        now = utcnow()
        if not order_id:
            return AdapterResult(
                source=source,
                fact_type="snapshot",
                payload={},
                observed_at=now,
                request_id=request_id,
                is_mock=True,
                adapter=self.name,
                error={"code": "missing_order_id", "message": "缺少订单号，无法查询事实"},
            )
        if order_id.upper().endswith(self.failing_order_suffix):
            return AdapterResult(
                source=source,
                fact_type="snapshot",
                payload={},
                observed_at=now,
                request_id=request_id,
                is_mock=True,
                adapter=self.name,
                error={"code": "adapter_timeout", "message": f"{source} 接口超时（模拟）"},
            )
        if order_id.upper().endswith(self.empty_suffix):
            return AdapterResult(
                source=source,
                fact_type="snapshot",
                payload={},
                observed_at=now,
                request_id=request_id,
                is_mock=True,
                adapter=self.name,
                error={"code": "order_not_found", "message": f"订单不存在：{order_id}"},
            )
        return None

    # ---------------------------------------------------------------- 协议实现
    def fetch_order(self, order_id: str, *, request_id: str | None = None) -> AdapterResult:
        guard = self._guard(source="order", order_id=order_id, request_id=request_id)
        if guard:
            return guard
        return self._result(
            source="order",
            order_id=order_id,
            payload=self._fixture(order_id)["order"],
            request_id=request_id,
        )

    def fetch_payment(self, order_id: str, *, request_id: str | None = None) -> AdapterResult:
        guard = self._guard(source="payment", order_id=order_id, request_id=request_id)
        if guard:
            return guard
        return self._result(
            source="payment",
            order_id=order_id,
            payload=self._fixture(order_id)["payment"],
            request_id=request_id,
        )

    def fetch_logistics(self, order_id: str, *, request_id: str | None = None) -> AdapterResult:
        guard = self._guard(source="logistics", order_id=order_id, request_id=request_id)
        if guard:
            return guard
        return self._result(
            source="logistics",
            order_id=order_id,
            payload=self._fixture(order_id)["logistics"],
            request_id=request_id,
        )

    def fetch_aftersale(
        self, order_id: str, *, customer_id: str | None = None, request_id: str | None = None
    ) -> AdapterResult:
        guard = self._guard(source="aftersale", order_id=order_id, request_id=request_id)
        if guard:
            return guard
        payload = dict(self._fixture(order_id)["aftersale"])
        if customer_id:
            payload["customer_id"] = customer_id
        return self._result(
            source="aftersale", order_id=order_id, payload=payload, request_id=request_id
        )


def demo_order_ids() -> list[str]:
    """演示订单号（灌演示数据与文档时复用同一份清单）。"""
    return sorted(_DEMO_ORDERS)


def fact_freshness(observed_at, *, ttl_seconds: int) -> bool:
    """事实是否新鲜（供界面标注"实时/可能过期"）。"""
    if observed_at is None:
        return False
    return (utcnow() - observed_at) <= timedelta(seconds=ttl_seconds)
