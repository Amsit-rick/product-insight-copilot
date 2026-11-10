from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date

from fastapi.testclient import TestClient

from app.adapters.metric_http import HttpMetricSourceAdapter
from sandbox_services.app import create_app


API_KEY = "test-sandbox-key"
WEBHOOK_SECRET = "test-webhook-secret"


def _client() -> TestClient:
    return TestClient(
        create_app(
            api_key=API_KEY,
            webhook_secret=WEBHOOK_SECRET,
            delay_seconds=0,
        )
    )


def _api_headers() -> dict[str, str]:
    return {"X-Api-Key": API_KEY, "X-Request-ID": "test-request-id"}


def _signed_webhook(body: bytes, *, delivery_id: str = "delivery-1") -> dict[str, str]:
    timestamp = "1794172800"
    digest = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Alert-Delivery-Id": delivery_id,
        "X-Alert-Timestamp": timestamp,
        "X-Alert-Signature": f"sha256={digest}",
    }


def test_health_and_fact_endpoints_are_read_only_and_authenticated():
    with _client() as client:
        health = client.get("/health")
        unauthorized = client.get("/orders/SANDBOX-OK-001")
        order = client.get("/orders/SANDBOX-OK-001", headers=_api_headers())
        payment = client.get("/payments/SANDBOX-OK-001", headers=_api_headers())
        logistics = client.get("/logistics/SANDBOX-OK-001", headers=_api_headers())
        aftersale = client.get(
            "/aftersales/SANDBOX-OK-001?customer_id=SANDBOX-CUST-001",
            headers=_api_headers(),
        )

    assert health.json() == {"status": "ok", "service": "reference-integration-sandbox"}
    assert unauthorized.status_code == 401
    for response in (order, payment, logistics, aftersale):
        assert response.status_code == 200
        payload = response.json()
        assert payload["observed_at"].endswith("+00:00")
        assert payload["request_id"] == "test-request-id"
        assert isinstance(payload["data"], dict)


def test_fact_endpoints_expose_not_found_auth_and_bad_json_scenarios():
    with _client() as client:
        not_found = client.get("/orders/SANDBOX-NOTFOUND-001", headers=_api_headers())
        forced_auth = client.get("/orders/SANDBOX-AUTH-001", headers=_api_headers())
        bad_json = client.get("/orders/SANDBOX-BADJSON-001", headers=_api_headers())

    assert not_found.status_code == 404
    assert not_found.json() == {"error": "order_not_found"}
    assert forced_auth.status_code == 401
    assert bad_json.status_code == 200
    assert bad_json.headers["content-type"].startswith("text/plain")


def test_metric_endpoint_matches_the_real_http_adapter_contract():
    with _client() as client:
        adapter = HttpMetricSourceAdapter(
            endpoint="http://testserver/api/v1/metric-snapshots",
            headers={"X-Api-Key": API_KEY},
            client=client,
        )
        batch = adapter.fetch_snapshots(
            tenant_id="tenant-a",
            metric_names=["crash_rate", "next_day_retention"],
            date_from=date(2026, 11, 7),
            date_to=date(2026, 11, 9),
            request_id="metric-request-1",
        )

    assert batch.dataset_version == "sandbox-metrics-20261109"
    assert batch.source_version == "reference-integration-sandbox-v1"
    assert batch.request_id == "metric-request-1"
    assert {row["metric_name"] for row in batch.records} == {
        "crash_rate",
        "next_day_retention",
    }
    assert all(row["stat_date"] in {"2026-11-07", "2026-11-08", "2026-11-09"} for row in batch.records)


def test_metric_endpoint_rejects_unknown_metrics_and_cross_tenant_scenario():
    params = {
        "tenant_id": "tenant-a",
        "metric_names": "unknown_metric",
        "date_from": "2026-11-07",
        "date_to": "2026-11-09",
    }
    with _client() as client:
        unknown = client.get(
            "/api/v1/metric-snapshots",
            params=params,
            headers=_api_headers(),
        )
        cross_tenant = client.get(
            "/api/v1/metric-snapshots",
            params={**params, "metric_names": "crash_rate", "tenant_id": "SANDBOX-CROSS-TENANT"},
            headers=_api_headers(),
        )

    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "metric_not_allowed"
    assert cross_tenant.status_code == 200
    assert cross_tenant.json()["data"][0]["tenant_id"] == "other-tenant"


def test_webhook_receiver_validates_hmac_and_keeps_only_safe_metadata():
    payload = {
        "event": "operational_alert",
        "alert": {"kind": "metric_anomaly", "severity": "warning"},
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with _client() as client:
        accepted = client.post(
            "/webhooks/alerts",
            content=body,
            headers=_signed_webhook(body),
        )
        rejected = client.post(
            "/webhooks/alerts",
            content=body,
            headers={**_signed_webhook(body, delivery_id="delivery-2"), "X-Alert-Signature": "sha256=bad"},
        )
        stats = client.get("/webhooks/alerts/stats", headers=_api_headers())

    assert accepted.status_code == 204
    assert rejected.status_code == 401
    assert stats.status_code == 200
    data = stats.json()
    assert data["accepted_count"] == 1
    assert data["rejected_count"] == 1
    assert data["last_delivery_id"] == "delivery-2"
    assert "alert" not in json.dumps(data)


def test_webhook_retry_and_dead_letter_scenarios_are_deterministic():
    payload = {"event": "operational_alert", "alert": {"kind": "probe"}}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    with _client() as client:
        retry_first = client.post(
            "/webhooks/alerts/retry",
            content=body,
            headers=_signed_webhook(body, delivery_id="retry-1"),
        )
        retry_second = client.post(
            "/webhooks/alerts/retry",
            content=body,
            headers=_signed_webhook(body, delivery_id="retry-1"),
        )
        dead = client.post(
            "/webhooks/alerts/dead",
            content=body,
            headers=_signed_webhook(body, delivery_id="dead-1"),
        )
        replayed = client.post(
            "/webhooks/alerts/dead",
            content=body,
            headers={**_signed_webhook(body, delivery_id="dead-1"), "X-Sandbox-Replay": "true"},
        )

    assert retry_first.status_code == 503
    assert retry_second.status_code == 204
    assert dead.status_code == 400
    assert replayed.status_code == 204
