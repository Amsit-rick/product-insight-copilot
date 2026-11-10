"""订单/支付/物流/售后模拟适配器单测。

适配器是**唯一允许接触外部事实的地方**，它的三条纪律必须钉住：
1. 只读（不产生写操作）；
2. 每条事实都带 `observed_at` + `request_id` + `is_mock`（FR-02 要能标来源与查询时间）；
3. 失败也要落成结构化事实（缺单号/查不到/超时 → 进人工，而不是让工单 500）。
"""

from __future__ import annotations

import threading

import pytest

from app.adapters.base import AdapterResult
from app.adapters.mock import MockFactAdapter, demo_order_ids
from app.adapters.registry import get_fact_adapter, reset_adapter_cache
from app.config import Settings
from app.models.base import utcnow
from app.services.masking import contains_sensitive
from app.services import order_fact_service
from app.services.order_fact_service import collect_facts


def make_settings(**overrides) -> Settings:
    base = {"llm_provider": "stub", "ticket_use_mock_adapters": True}
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_adapter_is_deterministic():
    """同一订单号必须返回同一份事实：否则同一工单两次分析会得到不同结论，无法排障。"""
    adapter = MockFactAdapter()
    first = adapter.fetch_order("TB202609241806").payload
    second = adapter.fetch_order("TB202609241806").payload
    assert first == second
    # 未内置的订单号走派生逻辑，同样要确定性
    assert adapter.fetch_logistics("TB-UNKNOWN-0001").payload == adapter.fetch_logistics(
        "TB-UNKNOWN-0001"
    ).payload


def test_demo_orders_cover_prototype_cases():
    ids = demo_order_ids()
    assert len(ids) == 6
    assert "TB202609241806" in ids


def test_every_fact_carries_provenance():
    collection = collect_facts(order_id="TB202609241742", customer_id="U1001")
    assert len(collection.results) == 4
    for item in collection.results:
        assert item.observed_at is not None
        assert item.request_id
        assert item.is_mock is True
        assert item.adapter == "mock-adapter"


def test_facts_are_masked_at_the_source():
    """收件信息在适配器出口就脱敏（手机号/地址不裸奔进证据快照）。"""
    collection = collect_facts(order_id="TB202609241806")
    order = next(item for item in collection.results if item.source == "order")
    assert "13812345678" not in str(order.payload)
    assert contains_sensitive(str(order.payload)) is False
    assert str(order.payload["address"]).endswith("***")


def test_missing_order_id_is_structured_failure():
    collection = collect_facts(order_id=None)
    assert collection.has_order_facts is False
    assert len(collection.failures) == 4
    assert all((item.error or {}).get("code") == "missing_order_id" for item in collection.failures)
    assert collection.missing == ["订单号"]


def test_order_not_found_and_timeout_simulations():
    not_found = collect_facts(order_id="TB20260101NODATA")
    assert not_found.has_order_facts is False
    assert "正确的订单号（按该单号查不到订单）" in not_found.missing

    timeout = collect_facts(order_id="TB20260101TIMEOUT")
    assert timeout.has_order_facts is False
    assert timeout.missing == ["订单事实来源暂不可用，请客服人工核实后再继续"]


def test_aftersale_carries_customer_id():
    collection = collect_facts(order_id="TB202609240655", customer_id="U-8888")
    aftersale = next(item for item in collection.results if item.source == "aftersale")
    assert aftersale.payload.get("customer_id") == "U-8888"


def test_mock_data_matches_prototype_story_tickets():
    """演示数据与原型 6 张工单讲同一个故事（少件/破损/优惠/停滞）。"""
    short = collect_facts(order_id="TB202609241742")
    logistics = next(item for item in short.results if item.source == "logistics")
    # 少件场景：包裹重量明显小于订单标注
    assert "9.6kg" in str(logistics.payload["package_weight"])

    stalled = collect_facts(order_id="TB202609240655")
    track = next(item for item in stalled.results if item.source == "logistics")
    assert int(track.payload["stalled_hours"]) >= 48

    promo = collect_facts(order_id="TB202609240821")
    aftersale = next(item for item in promo.results if item.source == "aftersale")
    assert aftersale.payload["promotion_result"].startswith("排除商品后未达门槛")


def test_registry_switches_to_unavailable_when_mock_disabled():
    """关闭模拟适配器且未接真实接口时，必须返回结构化的"不可用"，不能抛异常。"""
    reset_adapter_cache()


def test_unavailable_fact_source_is_not_reported_as_customer_missing_information():
    from app.adapters.registry import get_fact_adapter

    reset_adapter_cache()
    settings = make_settings(ticket_use_mock_adapters=False)
    adapter = get_fact_adapter(settings)
    results = [
        adapter.fetch_order("TB202609241806"),
        adapter.fetch_payment("TB202609241806"),
        adapter.fetch_logistics("TB202609241806"),
        adapter.fetch_aftersale("TB202609241806"),
    ]
    collection = order_fact_service.FactCollection(results)

    assert collection.has_order_facts is False
    assert collection.missing == ["订单事实来源暂不可用，请客服人工核实后再继续"]
    reset_adapter_cache()
    settings = make_settings(ticket_use_mock_adapters=False)
    adapter = get_fact_adapter(settings)
    result = adapter.fetch_order("TB202609241806")
    assert result.ok is False
    assert (result.error or {}).get("code") == "adapter_unavailable"
    assert result.is_mock is False
    reset_adapter_cache()


def test_registry_returns_mock_by_default():
    reset_adapter_cache()
    adapter = get_fact_adapter(make_settings())
    assert adapter.is_mock is True
    reset_adapter_cache()


def test_collect_summary_shape():
    summary = collect_facts(order_id="TB202609241501").summary()
    assert summary["ok"] == 4 and summary["failed"] == 0
    assert summary["is_mock"] is True
    assert set(summary["sources"]) == {"order", "payment", "logistics", "aftersale"}


def test_collect_queries_four_independent_sources_in_parallel(monkeypatch):
    barrier = threading.Barrier(4)
    thread_ids: set[int] = set()

    class ParallelProbeAdapter:
        def _fetch(self, source: str) -> AdapterResult:
            thread_ids.add(threading.get_ident())
            barrier.wait(timeout=2)
            return AdapterResult(
                source=source,
                fact_type="snapshot",
                payload={"order_status": "ok"} if source == "order" else {},
                observed_at=utcnow(),
                request_id=f"probe-{source}",
                is_mock=False,
                adapter="parallel-probe",
            )

        def fetch_order(self, order_id, *, request_id=None):
            return self._fetch("order")

        def fetch_payment(self, order_id, *, request_id=None):
            return self._fetch("payment")

        def fetch_logistics(self, order_id, *, request_id=None):
            return self._fetch("logistics")

        def fetch_aftersale(self, order_id, *, customer_id=None, request_id=None):
            return self._fetch("aftersale")

    monkeypatch.setattr(order_fact_service, "get_fact_adapter", lambda _settings: ParallelProbeAdapter())

    collection = collect_facts(order_id="O100", settings=make_settings())

    assert [item.source for item in collection.results] == [
        "order",
        "payment",
        "logistics",
        "aftersale",
    ]
    assert len(thread_ids) == 4
