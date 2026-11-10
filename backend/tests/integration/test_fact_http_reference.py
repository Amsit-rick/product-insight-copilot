"""真实 HTTP 事实适配器 + 本地参考 API 的契约回归。

不访问外网：测试自己起一个回环 HTTP 服务，验证成功、404、401、超时、非 JSON
五类语义，以及字段白名单和手机号/地址脱敏。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND / "scripts"))

from reference_fact_api import (  # noqa: E402
    AUTH_ORDER,
    BAD_JSON_ORDER,
    NOT_FOUND_ORDER,
    OK_ORDER,
    TIMEOUT_ORDER,
    build_server,
)

from app.adapters.http import HttpFactAdapter  # noqa: E402
from app.services.fact_integration_probe import (  # noqa: E402
    load_fact_probe_plan,
    run_fact_probe,
)


@pytest.fixture()
def reference_api():
    server = build_server(port=0, delay_seconds=0.6)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _adapter(base: str) -> HttpFactAdapter:
    return HttpFactAdapter(
        endpoints={
            "order": f"{base}/orders/{{order_id}}",
            "payment": f"{base}/payments/{{order_id}}",
            "logistics": f"{base}/logistics/{{order_id}}",
            "aftersale": f"{base}/aftersales/{{order_id}}",
        },
        headers={"X-Api-Key": "local-sandbox-key"},
        timeout_seconds=0.25,
        max_retries=0,
        retry_backoff_seconds=0,
    )


def test_success_masks_sensitive_values_and_drops_unknown_fields(reference_api):
    adapter = _adapter(reference_api)
    try:
        result = adapter.fetch_order(OK_ORDER, request_id="trace-ok-1")
    finally:
        adapter.close()

    assert result.ok is True
    assert result.is_mock is False
    assert result.adapter == "http-fact-adapter"
    assert result.request_id == "trace-ok-1"
    assert result.payload["order_status"] == "已签收"
    assert result.payload["receiver_phone"] != "13812345678"
    assert "unknown_field" not in result.payload, "未知字段必须被丢弃"
    assert result.payload["address"] != "上海市浦东新区世纪大道 100 号", "地址必须脱敏"


def test_not_found_does_not_echo_order_id(reference_api):
    adapter = _adapter(reference_api)
    try:
        result = adapter.fetch_order(NOT_FOUND_ORDER, request_id="trace-404")
    finally:
        adapter.close()

    assert result.ok is False
    assert result.error["code"] == "order_not_found"
    assert NOT_FOUND_ORDER not in result.error["message"]


def test_auth_failure_is_structured(reference_api):
    adapter = _adapter(reference_api)
    try:
        result = adapter.fetch_payment(AUTH_ORDER, request_id="trace-401")
    finally:
        adapter.close()

    assert result.error["code"] == "adapter_auth_error"
    assert result.request_id == "trace-401"


def test_timeout_is_structured(reference_api):
    adapter = _adapter(reference_api)
    try:
        result = adapter.fetch_logistics(TIMEOUT_ORDER, request_id="trace-timeout")
    finally:
        adapter.close()

    assert result.error["code"] == "adapter_timeout"


def test_invalid_json_is_structured(reference_api):
    adapter = _adapter(reference_api)
    try:
        result = adapter.fetch_aftersale(BAD_JSON_ORDER, request_id="trace-bad-json")
    finally:
        adapter.close()

    assert result.error["code"] == "adapter_invalid_response"


def test_probe_plan_passes_on_local_reference_api(reference_api):
    plan = load_fact_probe_plan(PROJECT_ROOT / "docs" / "integrations" / "fact-probe-plan.local.json")
    environ = {
        "FACT_PROBE_SUCCESS_ORDER_ID": OK_ORDER,
        "FACT_PROBE_SUCCESS_CUSTOMER_ID": "SANDBOX-CUST-001",
        "FACT_PROBE_NOT_FOUND_ORDER_ID": NOT_FOUND_ORDER,
        "FACT_PROBE_LOGISTICS_TIMEOUT_ORDER_ID": TIMEOUT_ORDER,
        "FACT_PROBE_AUTH_ORDER_ID": AUTH_ORDER,
        "FACT_PROBE_BADJSON_ORDER_ID": BAD_JSON_ORDER,
    }
    adapter = _adapter(reference_api)
    try:
        result = run_fact_probe(
            adapter,
            plan,
            runs=1,
            max_p95_ms=10_000,
            max_observed_age_seconds=300,
            environ=environ,
        )
    finally:
        adapter.close()

    assert result["passed"] is True, result["summary"]
    assert result["summary"]["checks_passed"] == result["summary"]["checks_total"]
    assert result["plan"]["scenario_count"] == 5
