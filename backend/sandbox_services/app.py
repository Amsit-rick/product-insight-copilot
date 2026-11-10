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


OK_ORDER = "SANDBOX-OK-001"
NOT_FOUND_ORDER = "SANDBOX-NOTFOUND-001"
TIMEOUT_ORDER = "SANDBOX-TIMEOUT-001"
AUTH_ORDER = "SANDBOX-AUTH-001"
BAD_JSON_ORDER = "SANDBOX-BADJSON-001"

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


def _fact_payload(source: str, customer_id: str | None) -> dict[str, Any]:
    if source == "order":
        return {
            "order_status": "已签收",
            "order_amount": "268.00",
            "order_items": "SKU-100 x 1",
            "order_channel": "自营商城",
            "address": "上海市浦东新区世纪大道 100 号",
            "receiver_phone": "13812345678",
            "category": "猫粮",
            "unknown_field": "must-be-dropped",
        }
    if source == "payment":
        return {
            "paid_amount": "268.00",
            "refundable_amount": "268.00",
            "pay_channel": "支付宝",
            "transaction_id": "must-be-dropped",
        }
    if source == "logistics":
        return {
            "carrier": "顺丰速运",
            "tracking_no": "SF1234567890",
            "latest_track": "已签收（本人签收）",
            "signed_at": "2026-09-25T12:00:00+08:00",
            "stalled_hours": 0,
            "eta": "2026-09-25",
            "package_weight": "1.2kg",
        }
    return {
        "aftersale_history": "2026-09-10 同类商品退货 1 次（已完结）",
        "last_case_result": "退货退款已结案",
        "gross_amount": "268.00",
        "eligible_amount": "268.00",
        "excluded_items": "无",
        "promotion_threshold": "满 199 减 30",
        "promotion_result": "已抵扣",
        "customer_id": customer_id or "SANDBOX-CUST-001",
    }


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

    app = FastAPI(
        title="Reference Integration Sandbox",
        version="1.0.0",
        description=(
            "Local-only read-only upstream sandbox for fact, metric and Webhook "
            "integration development. It is not production evidence."
        ),
    )
    app.state.webhook_receipts = state

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
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "reference-integration-sandbox"}

    async def fact_response(
        source: str,
        order_id: str,
        request_id: str | None,
        customer_id: str | None = None,
    ) -> Response:
        if order_id == AUTH_ORDER:
            return JSONResponse(status_code=401, content={"error": "unauthorized"})
        if order_id == NOT_FOUND_ORDER:
            return JSONResponse(status_code=404, content={"error": "order_not_found"})
        if order_id == BAD_JSON_ORDER:
            return PlainTextResponse("this is not json")
        if order_id == TIMEOUT_ORDER and source == "logistics" and timeout_delay > 0:
            await asyncio.sleep(timeout_delay)
        trace_id = request_id or f"sandbox-{uuid.uuid4().hex[:12]}"
        return JSONResponse(
            content={
                "data": _fact_payload(source, customer_id),
                "observed_at": _now_iso(),
                "request_id": trace_id,
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


__all__ = ["app", "create_app"]
