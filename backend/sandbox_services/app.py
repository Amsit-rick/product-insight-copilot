"""Independent local sandbox for facts, metrics and alert Webhooks.

The service exercises real HTTP adapters without pretending to be an enterprise
production system. It is read-only except for in-memory Webhook receipt counters.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from sandbox_services.providers import (
    FactProvider,
    ProviderError,
    build_fact_provider,
)


OK_ORDER = "SANDBOX-OK-001"
NOT_FOUND_ORDER = "SANDBOX-NOTFOUND-001"
TIMEOUT_ORDER = "SANDBOX-TIMEOUT-001"
AUTH_ORDER = "SANDBOX-AUTH-001"
BAD_JSON_ORDER = "SANDBOX-BADJSON-001"

FACT_FAULT_SCENARIOS: dict[str, dict[str, str]] = {
    "SCN-FAULT-LOGISTICS-TIMEOUT": {
        "name": "物流查询超时",
        "target_source": "logistics",
        "fault": "timeout",
        "expected_error": "adapter_timeout",
    },
    "SCN-FAULT-PAYMENT-FIELD-MISSING": {
        "name": "支付关键字段缺失",
        "target_source": "payment",
        "fault": "missing_required_field",
        "expected_error": "adapter_invalid_response",
    },
    "SCN-FAULT-LOGISTICS-BAD-TIME": {
        "name": "物流观测时间非法",
        "target_source": "logistics",
        "fault": "invalid_time",
        "expected_error": "adapter_invalid_response",
    },
    "SCN-FAULT-PAYMENT-RATE-LIMIT": {
        "name": "支付接口限流",
        "target_source": "payment",
        "fault": "rate_limit",
        "expected_error": "adapter_rate_limited",
    },
    "SCN-FAULT-ORDER-UPSTREAM-ERROR": {
        "name": "Saleor 上游服务异常",
        "target_source": "order",
        "fault": "upstream_error",
        "expected_error": "adapter_upstream_error",
    },
    "SCN-FAULT-AFTERSALE-BAD-JSON": {
        "name": "售后接口非法 JSON",
        "target_source": "aftersale",
        "fault": "bad_json",
        "expected_error": "adapter_invalid_response",
    },
}
FAULT_BASE_SCENARIO = "SCN-NORMAL-DELIVERED"

ALLOWED_METRICS = {
    "crash_rate",
    "next_day_retention",
    "conversion_rate",
}
METRIC_VALUES = {
    "crash_rate": 0.018,
    "next_day_retention": 0.42,
    "conversion_rate": 0.087,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class WebhookReceiptState:
    accepted_count: int = 0
    rejected_count: int = 0
    last_delivery_id: str | None = None
    retry_attempts: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, delivery_id: str, *, accepted: bool) -> None:
        with self.lock:
            self.last_delivery_id = delivery_id
            if accepted:
                self.accepted_count += 1
            else:
                self.rejected_count += 1

    def retry_attempt(self, delivery_id: str) -> int:
        with self.lock:
            attempt = self.retry_attempts.get(delivery_id, 0) + 1
            self.retry_attempts[delivery_id] = attempt
            return attempt

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "accepted_count": self.accepted_count,
                "rejected_count": self.rejected_count,
                "last_delivery_id": self.last_delivery_id,
                "retry_delivery_count": len(self.retry_attempts),
            }


def create_app(
    *,
    api_key: str | None = None,
    webhook_secret: str | None = None,
    delay_seconds: float | None = None,
    fact_provider: FactProvider | None = None,
) -> FastAPI:
    sandbox_api_key = api_key or os.getenv("SANDBOX_API_KEY", "local-sandbox-key")
    signing_secret = webhook_secret or os.getenv(
        "SANDBOX_WEBHOOK_SECRET",
        "local-webhook-secret",
    )
    timeout_delay = (
        float(delay_seconds)
        if delay_seconds is not None
        else float(os.getenv("SANDBOX_DELAY_SECONDS", "0.6"))
    )
    state = WebhookReceiptState()
    provider = fact_provider or build_fact_provider()

    app = FastAPI(
        title="Reference Integration Sandbox",
        version="1.0.0",
        description=(
            "Local-only read-only upstream sandbox for fact, metric and Webhook "
            "integration development. It is not production evidence."
        ),
    )
    app.state.webhook_receipts = state
    app.state.fact_provider = provider

    @app.on_event("shutdown")
    async def close_fact_provider() -> None:
        await provider.aclose()

    def require_api_key(
        x_api_key: Annotated[str | None, Header(alias="X-Api-Key")] = None,
    ) -> None:
        if not x_api_key or not hmac.compare_digest(x_api_key, sandbox_api_key):
            raise_api_error(401, "unauthorized", "Missing or invalid API key")

    def raise_api_error(status_code: int, code: str, message: str) -> None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status_code,
            detail={"error": {"code": code, "message": message}},
        )

    @app.exception_handler(401)
    async def unauthorized_handler(_request: Request, exc) -> JSONResponse:  # noqa: ANN001
        detail = exc.detail if isinstance(exc.detail, dict) else {
            "error": {"code": "unauthorized", "message": "Unauthorized"}
        }
        return JSONResponse(status_code=401, content=detail)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        provider_health = await provider.health()
        return {
            "status": "ok",
            "service": "reference-integration-sandbox",
            **provider_health,
        }

    @app.get("/scenarios", dependencies=[Depends(require_api_key)])
    def scenarios() -> dict[str, Any]:
        catalog = getattr(provider, "public_scenarios", None)
        if not callable(catalog):
            return {
                "environment": "local_static_sandbox",
                "scenarios": [],
            }
        return catalog()

    @app.get("/fault-scenarios", dependencies=[Depends(require_api_key)])
    def fault_scenarios() -> dict[str, Any]:
        return {
            "environment": "local_high_fidelity_sandbox",
            "scenarios": [
                {
                    "id": scenario_id,
                    "name": item["name"],
                    "target_source": item["target_source"],
                    "expected_error": item["expected_error"],
                }
                for scenario_id, item in FACT_FAULT_SCENARIOS.items()
            ],
        }

    async def fact_response(
        source: str,
        order_id: str,
        request_id: str | None,
        customer_id: str | None = None,
    ) -> Response:
        fault = FACT_FAULT_SCENARIOS.get(order_id)
        resolved_order_id = FAULT_BASE_SCENARIO if fault else order_id
        targeted_fault = fault if fault and fault["target_source"] == source else None
        if targeted_fault and targeted_fault["fault"] == "rate_limit":
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limited"},
                headers={"Retry-After": "1"},
            )
        if targeted_fault and targeted_fault["fault"] == "upstream_error":
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "adapter_upstream_error",
                        "message": "Saleor upstream query failed",
                    }
                },
            )
        if targeted_fault and targeted_fault["fault"] == "bad_json":
            return PlainTextResponse("this is not json")
        if order_id == AUTH_ORDER:
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        if order_id == NOT_FOUND_ORDER:
            return JSONResponse(status_code=404, content={"error": "order_not_found"})
        if order_id == BAD_JSON_ORDER:
            return PlainTextResponse("this is not json")
        if (
            (order_id == TIMEOUT_ORDER and source == "logistics")
            or (targeted_fault and targeted_fault["fault"] == "timeout")
        ) and timeout_delay > 0:
            await asyncio.sleep(timeout_delay)
        trace_id = request_id or f"sandbox-{uuid.uuid4().hex[:12]}"
        try:
            fact = await provider.fetch(
                source,
                resolved_order_id,
                customer_id=customer_id,
            )
        except ProviderError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "code": exc.code,
                        "message": exc.safe_message,
                    },
                    "request_id": trace_id,
                },
                headers={"X-Request-ID": trace_id},
            )
        data = dict(fact.data)
        observed_at = fact.observed_at
        if targeted_fault and targeted_fault["fault"] == "missing_required_field":
            if source == "payment":
                data.pop("paid_amount", None)
        if targeted_fault and targeted_fault["fault"] == "invalid_time":
            observed_at = "not-a-valid-time"
        return JSONResponse(
            content={
                "data": data,
                "observed_at": observed_at,
                "request_id": trace_id,
                "source_version": fact.source_version,
            },
            headers={"X-Request-ID": trace_id},
        )

    @app.get("/orders/{order_id}", dependencies=[Depends(require_api_key)])
    async def order_fact(
        order_id: str,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> Response:
        return await fact_response("order", order_id, x_request_id)

    @app.get("/payments/{order_id}", dependencies=[Depends(require_api_key)])
    async def payment_fact(
        order_id: str,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> Response:
        return await fact_response("payment", order_id, x_request_id)

    @app.get("/logistics/{order_id}", dependencies=[Depends(require_api_key)])
    async def logistics_fact(
        order_id: str,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> Response:
        return await fact_response("logistics", order_id, x_request_id)

    @app.get("/aftersales/{order_id}", dependencies=[Depends(require_api_key)])
    async def aftersale_fact(
        order_id: str,
        customer_id: str | None = None,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> Response:
        return await fact_response("aftersale", order_id, x_request_id, customer_id)

    @app.get(
        "/api/v1/metric-snapshots",
        dependencies=[Depends(require_api_key)],
    )
    def metric_snapshots(
        tenant_id: Annotated[str, Query(min_length=1, max_length=64)],
        metric_names: Annotated[str, Query(min_length=1, max_length=500)],
        date_from: date,
        date_to: date,
        x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
    ) -> Response:
        requested = list(
            dict.fromkeys(name.strip() for name in metric_names.split(",") if name.strip())
        )
        unknown = sorted(set(requested) - ALLOWED_METRICS)
        if unknown:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "metric_not_allowed",
                        "message": "Requested metric is not allowlisted",
                    }
                },
            )
        if date_from > date_to or (date_to - date_from).days > 90:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "date_range_invalid",
                        "message": "Date range must be ordered and no longer than 90 days",
                    }
                },
            )
        if tenant_id == "SANDBOX-METRIC-ERROR":
            return JSONResponse(status_code=503, content={"error": "temporary_unavailable"})
        if tenant_id == "SANDBOX-METRIC-RATE-LIMIT":
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limited"},
                headers={"Retry-After": "1"},
            )

        row_tenant = "other-tenant" if tenant_id == "SANDBOX-CROSS-TENANT" else tenant_id
        records: list[dict[str, Any]] = []
        cursor = date_from
        while cursor <= date_to:
            for metric_name in requested:
                records.append(
                    {
                        "tenant_id": row_tenant,
                        "metric_name": metric_name,
                        "platform": "android",
                        "app_version": "3.2.0",
                        "channel": "channel_a",
                        "device": "high_end",
                        "stat_date": cursor.isoformat(),
                        "value": METRIC_VALUES[metric_name],
                        "sample_size": 1200,
                        "source_version": "reference-integration-sandbox-v1",
                    }
                )
            cursor += timedelta(days=1)
        trace_id = x_request_id or f"sandbox-metric-{uuid.uuid4().hex[:12]}"
        return JSONResponse(
            content={
                "dataset_version": f"sandbox-metrics-{date_to:%Y%m%d}",
                "source_version": "reference-integration-sandbox-v1",
                "request_id": trace_id,
                "data": records,
            },
            headers={"X-Request-ID": trace_id},
        )

    async def handle_webhook(request: Request, scenario: str) -> Response:
        body = await request.body()
        timestamp = request.headers.get("X-Alert-Timestamp", "")
        delivery_id = request.headers.get("X-Alert-Delivery-Id", "")
        expected = hmac.new(
            signing_secret.encode("utf-8"),
            timestamp.encode("utf-8") + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        signature_valid = bool(timestamp and delivery_id) and hmac.compare_digest(
            request.headers.get("X-Alert-Signature", ""),
            f"sha256={expected}",
        )
        if not signature_valid:
            state.record(delivery_id, accepted=False)
            return JSONResponse(status_code=401, content={"error": "invalid_signature"})
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        payload_valid = (
            isinstance(payload, dict)
            and payload.get("event") == "operational_alert"
            and isinstance(payload.get("alert"), dict)
        )
        if not payload_valid:
            state.record(delivery_id, accepted=False)
            return JSONResponse(status_code=422, content={"error": "invalid_payload"})

        if scenario == "retry" and state.retry_attempt(delivery_id) == 1:
            return Response(status_code=503, headers={"Retry-After": "1"})
        if scenario == "dead" and request.headers.get("X-Sandbox-Replay") != "true":
            return Response(status_code=400)
        state.record(delivery_id, accepted=True)
        return Response(status_code=204)

    @app.post("/webhooks/alerts", status_code=204)
    async def webhook_success(request: Request) -> Response:
        return await handle_webhook(request, "success")

    @app.post("/webhooks/alerts/retry")
    async def webhook_retry(request: Request) -> Response:
        return await handle_webhook(request, "retry")

    @app.post("/webhooks/alerts/dead")
    async def webhook_dead(request: Request) -> Response:
        return await handle_webhook(request, "dead")

    @app.get(
        "/webhooks/alerts/stats",
        dependencies=[Depends(require_api_key)],
    )
    def webhook_stats() -> dict[str, Any]:
        return state.snapshot()

    return app


app = create_app()


__all__ = ["FACT_FAULT_SCENARIOS", "app", "create_app"]
