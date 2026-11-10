"""生产事实 HTTP 适配器的契约与故障注入测试。"""

from __future__ import annotations

import httpx

from app.adapters.http import HttpFactAdapter, validate_endpoint_templates
from app.adapters.registry import describe_fact_adapter, get_fact_adapter, reset_adapter_cache
from app.config import Settings


ENDPOINTS = {
    "order": "https://facts.example.test/orders/{order_id}",
    "payment": "https://facts.example.test/payments/{order_id}",
    "logistics": "https://facts.example.test/logistics/{order_id}",
    "aftersale": "https://facts.example.test/aftersales/{order_id}",
}


def make_adapter(handler, *, retries: int = 2) -> HttpFactAdapter:
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=0.1)
    return HttpFactAdapter(
        endpoints=ENDPOINTS,
        headers={"Authorization": "Bearer server-only-secret"},
        timeout_seconds=0.1,
        max_retries=retries,
        retry_backoff_seconds=0,
        client=client,
    )


def make_settings(**overrides) -> Settings:
    base = {
        "llm_provider": "stub",
        "ticket_use_mock_adapters": False,
        "ticket_fact_order_url": ENDPOINTS["order"],
        "ticket_fact_payment_url": ENDPOINTS["payment"],
        "ticket_fact_logistics_url": ENDPOINTS["logistics"],
        "ticket_fact_aftersale_url": ENDPOINTS["aftersale"],
        "ticket_fact_auth_headers": {"Authorization": "Bearer server-only-secret"},
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_success_uses_get_propagates_request_id_and_sanitizes_payload():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "order_status": "已签收",
                    "order_amount": "268.00",
                    "receiver_phone": "13812345678",
                    "address": "上海市浦东新区张江路 88 号",
                    "payment_token": "must-not-enter-core",
                },
                "observed_at": "2026-09-25T12:00:00+00:00",
                "request_id": "upstream-request-id",
            },
        )

    result = make_adapter(handler).fetch_order("O/100", request_id="trace-100")

    assert result.ok is True
    assert result.is_mock is False
    assert result.adapter == "http-fact-adapter"
    assert result.request_id == "upstream-request-id"
    assert result.observed_at.isoformat() == "2026-09-25T12:00:00+00:00"
    assert result.payload["receiver_phone"] == "138****5678"
    assert result.payload["address"] == "上海市浦东新区***"
    assert "payment_token" not in result.payload
    assert seen[0].method == "GET"
    assert seen[0].url.raw_path.endswith(b"/O%2F100")
    assert seen[0].headers["x-request-id"] == "trace-100"
    assert seen[0].headers["authorization"] == "Bearer server-only-secret"


def test_success_recursively_masks_sensitive_values_inside_allowed_nested_fields():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "order_items": [
                        {
                            "sku": "SKU-100",
                            "receiver_phone": "13812345678",
                            "note": "备用联系电话 13912345678",
                        }
                    ]
                }
            },
        )

    result = make_adapter(handler).fetch_order("O100")

    item = result.payload["order_items"][0]
    assert item["receiver_phone"] == "138****5678"
    assert item["note"] == "备用联系电话 139****5678"


def test_aftersale_passes_customer_id_as_query_parameter():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["customer_id"] == "U-100"
        return httpx.Response(200, json={"data": {"aftersale_history": "无"}})

    result = make_adapter(handler).fetch_aftersale("O100", customer_id="U-100")

    assert result.ok is True
    assert result.payload == {"aftersale_history": "无"}


def test_retryable_503_recovers_on_second_attempt():
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"message": "temporary"})
        return httpx.Response(200, json={"data": {"carrier": "顺丰"}})

    result = make_adapter(handler).fetch_logistics("O100")

    assert result.ok is True
    assert attempts == 2


def test_timeout_exhausts_retries_as_structured_failure():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("slow upstream", request=request)

    result = make_adapter(handler).fetch_payment("O100")

    assert result.ok is False
    assert result.error == {"code": "adapter_timeout", "message": "payment 接口超时"}
    assert attempts == 3


def test_auth_failure_is_not_retried_and_never_leaks_secret():
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(401, json={"message": "Bearer server-only-secret invalid"})

    result = make_adapter(handler).fetch_order("O100")

    assert result.ok is False
    assert (result.error or {})["code"] == "adapter_auth_error"
    assert "server-only-secret" not in str(result.error)
    assert attempts == 1


def test_404_and_invalid_response_are_structured_failures():
    not_found = make_adapter(lambda _request: httpx.Response(404)).fetch_order("O404")
    malformed = make_adapter(
        lambda _request: httpx.Response(200, json={"data": ["not", "an", "object"]})
    ).fetch_payment("O100")

    assert (not_found.error or {})["code"] == "order_not_found"
    assert "O404" not in str(not_found.error)
    assert (malformed.error or {})["code"] == "adapter_invalid_response"


def test_endpoint_templates_reject_embedded_credentials_fragments_and_production_http():
    embedded_credentials = validate_endpoint_templates(
        make_settings(ticket_fact_order_url="https://user:password@facts.test/orders/{order_id}")
    )
    fragment = validate_endpoint_templates(
        make_settings(ticket_fact_payment_url="https://facts.test/payments/{order_id}#debug")
    )
    production_http = validate_endpoint_templates(
        make_settings(
            app_env="prod",
            ticket_fact_logistics_url="http://facts.test/logistics/{order_id}",
        )
    )

    assert any("口令" in item for item in embedded_credentials)
    assert any("片段" in item for item in fragment)
    assert any("https://" in item for item in production_http)


def test_rate_limit_exhaustion_has_specific_error_code():
    result = make_adapter(lambda _request: httpx.Response(429), retries=1).fetch_logistics("O100")

    assert (result.error or {})["code"] == "adapter_rate_limited"


def test_registry_builds_http_adapter_only_when_all_endpoints_are_valid():
    reset_adapter_cache()
    adapter = get_fact_adapter(make_settings())
    missing = get_fact_adapter(make_settings(ticket_fact_payment_url=""))
    invalid = get_fact_adapter(make_settings(ticket_fact_order_url="https://facts.test/orders"))

    assert isinstance(adapter, HttpFactAdapter)
    assert (missing.fetch_order("O100").error or {})["code"] == "adapter_unavailable"
    assert (invalid.fetch_order("O100").error or {})["code"] == "adapter_unavailable"
    reset_adapter_cache()


def test_runtime_description_never_exposes_urls_or_auth_headers():
    settings = make_settings()

    described = describe_fact_adapter(settings)

    assert described["backend"] == "http"
    assert described["configured"] is True
    assert described["sources"] == {
        "order": True,
        "payment": True,
        "logistics": True,
        "aftersale": True,
    }
    rendered = str(described)
    assert "facts.example.test" not in rendered
    assert "server-only-secret" not in rendered
