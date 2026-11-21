from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from sandbox_services.providers import (
    SALEOR_ORDER_QUERY,
    ProviderInvalidResponse,
    ProviderNotFound,
    ProviderUnavailable,
    SaleorFactProvider,
    StaticFactProvider,
    build_fact_provider,
)


def _saleor_body() -> dict:
    return {
        "data": {
            "orders": {
                "edges": [
                    {
                        "node": {
                            "number": "20",
                            "status": "PARTIALLY_FULFILLED",
                            "created": "2026-11-15T09:27:16+00:00",
                            "channel": {"slug": "channel-pln"},
                            "total": {
                                "gross": {"amount": 262.93, "currency": "PLN"}
                            },
                            "totalCharged": {"amount": 262.93, "currency": "PLN"},
                            "totalRefunded": {"amount": 40.0, "currency": "PLN"},
                            "lines": [
                                {
                                    "quantity": 1,
                                    "productName": "Gift card 50",
                                    "variantName": "variant-1",
                                }
                            ],
                            "transactions": [
                                {
                                    "authorizedAmount": {
                                        "amount": 0.0,
                                        "currency": "PLN",
                                    },
                                    "chargedAmount": {
                                        "amount": 262.93,
                                        "currency": "PLN",
                                    },
                                    "refundedAmount": {
                                        "amount": 40.0,
                                        "currency": "PLN",
                                    },
                                    "events": [
                                        {
                                            "type": "REFUND_SUCCESS",
                                            "amount": {
                                                "amount": 40.0,
                                                "currency": "PLN",
                                            },
                                        }
                                    ],
                                }
                            ],
                            "fulfillments": [
                                {
                                    "status": "FULFILLED",
                                    "trackingNumber": "TRACK-001",
                                    "lines": [
                                        {
                                            "quantity": 1,
                                            "orderLine": {
                                                "quantity": 1,
                                                "productName": "Gift card 50",
                                                "variantName": "variant-1",
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                ]
            }
        }
    }


def _provider(handler) -> SaleorFactProvider:  # noqa: ANN001
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SaleorFactProvider(
        graphql_url="http://saleor.test/graphql/",
        app_token="runtime-token",
        client=client,
        timeout_seconds=0.1,
        max_retries=0,
    )


def test_saleor_query_is_fixed_read_only_and_does_not_request_customer_pii():
    lowered = SALEOR_ORDER_QUERY.lower()

    assert "query sandboxorderfacts" in lowered
    assert "mutation" not in lowered
    assert "__schema" not in lowered
    assert "__type" not in lowered
    assert "address" not in lowered
    assert "email" not in lowered
    assert "phone" not in lowered


def test_saleor_provider_sends_only_fixed_query_and_maps_all_four_sources():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_saleor_body())

    provider = _provider(handler)
    try:
        order = asyncio.run(provider.fetch("order", "20"))
        payment = asyncio.run(provider.fetch("payment", "20"))
        logistics = asyncio.run(provider.fetch("logistics", "20"))
        aftersale = asyncio.run(
            provider.fetch("aftersale", "20", customer_id="CUST-20")
        )
    finally:
        asyncio.run(provider.aclose())

    assert order.data == {
        "order_status": "部分发货",
        "order_amount": "262.93",
        "order_items": "Gift card 50 x 1",
        "order_channel": "channel-pln",
    }
    assert payment.data == {
        "paid_amount": "262.93",
        "refundable_amount": "222.93",
    }
    assert logistics.data == {
        "tracking_no": "TRACK-001",
        "latest_track": "已履约",
    }
    assert aftersale.data == {
        "aftersale_history": "已退款 40.00 PLN",
        "last_case_result": "存在成功退款记录",
        "gross_amount": "262.93",
        "eligible_amount": "222.93",
        "customer_id": "CUST-20",
    }
    assert all(item.source_version == "saleor-3.23.49" for item in (order, payment, logistics, aftersale))
    assert len(requests) == 4
    for request in requests:
        payload = json.loads(request.content)
        assert payload == {"query": SALEOR_ORDER_QUERY, "variables": {"numbers": ["20"]}}
        assert request.headers["authorization"] == "Bearer runtime-token"


def test_saleor_provider_has_structured_not_found_and_invalid_response_errors():
    not_found = _provider(
        lambda _request: httpx.Response(
            200, json={"data": {"orders": {"edges": []}}}
        )
    )
    try:
        with pytest.raises(ProviderNotFound):
            asyncio.run(not_found.fetch("order", "missing"))
    finally:
        asyncio.run(not_found.aclose())

    invalid = _provider(lambda _request: httpx.Response(200, json={"data": {}}))
    try:
        with pytest.raises(ProviderInvalidResponse):
            asyncio.run(invalid.fetch("order", "20"))
    finally:
        asyncio.run(invalid.aclose())


def test_saleor_provider_does_not_fall_back_when_upstream_is_unavailable():
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    provider = _provider(timeout)
    try:
        with pytest.raises(ProviderUnavailable) as exc:
            asyncio.run(provider.fetch("order", "20"))
    finally:
        asyncio.run(provider.aclose())

    assert exc.value.code == "adapter_timeout"


def test_provider_factory_defaults_to_static_and_requires_explicit_saleor_config(tmp_path):
    static = build_fact_provider({})
    assert isinstance(static, StaticFactProvider)

    with pytest.raises(ValueError, match="SALEOR_GRAPHQL_URL"):
        build_fact_provider({"SANDBOX_FACT_PROVIDER": "saleor"})

    token_file = tmp_path / "token"
    token_file.write_text("file-token", encoding="utf-8")
    saleor = build_fact_provider(
        {
            "SANDBOX_FACT_PROVIDER": "saleor",
            "SALEOR_GRAPHQL_URL": "http://saleor.test/graphql/",
            "SALEOR_APP_TOKEN_FILE": str(token_file),
        }
    )
    assert isinstance(saleor, SaleorFactProvider)
    asyncio.run(saleor.aclose())
