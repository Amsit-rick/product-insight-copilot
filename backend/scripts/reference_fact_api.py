#!/usr/bin/env python
"""本地参考事实 API：用回环 HTTP 演练真实 OMS/支付/物流/售后契约。

用途：
- 在没有厂商 sandbox 的情况下，验证 `HttpFactAdapter` 的 URL 替换、鉴权头、
  X-Request-ID、超时/404/401/非 JSON 语义和出口脱敏；
- 给平台方一个可运行的"标准响应长什么样"的参照，不替代厂商 sandbox。

四个端点（GET）：
    /orders/{order_id}
    /payments/{order_id}
    /logistics/{order_id}
    /aftersales/{order_id}?customer_id=...

约定订单号（仅本地）：
    SANDBOX-OK-001          四类来源都成功
    SANDBOX-NOTFOUND-001    四类来源都 404
    SANDBOX-TIMEOUT-001     物流故意延迟（超过适配器 timeout）
    SANDBOX-AUTH-001        四类来源都 401
    SANDBOX-BADJSON-001     四类来源都返回非 JSON

运行：
    python scripts/reference_fact_api.py --port 8091
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

SOURCE_BY_PATH = {
    "orders": "order",
    "payments": "payment",
    "logistics": "logistics",
    "aftersales": "aftersale",
}

OK_ORDER = "SANDBOX-OK-001"
NOT_FOUND_ORDER = "SANDBOX-NOTFOUND-001"
TIMEOUT_ORDER = "SANDBOX-TIMEOUT-001"
AUTH_ORDER = "SANDBOX-AUTH-001"
BAD_JSON_ORDER = "SANDBOX-BADJSON-001"


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()


def _payload(source: str, order_id: str, customer_id: str | None) -> dict:
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


class ReferenceFactServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, api_key: str, delay_seconds: float, verbose: bool = False):
        super().__init__(address, ReferenceFactHandler)
        self.api_key = api_key
        self.delay_seconds = delay_seconds
        self.verbose = verbose


class ReferenceFactHandler(BaseHTTPRequestHandler):
    server_version = "ReferenceFactAPI/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Request-ID", self.headers.get("X-Request-ID") or "")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _text(self, status: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"status": "ok", "service": "reference-fact-api"})
            return

        parts = [unquote(part) for part in parsed.path.strip("/").split("/") if part]
        if len(parts) != 2 or parts[0] not in SOURCE_BY_PATH:
            self._json(404, {"error": "not_found"})
            return
        source = SOURCE_BY_PATH[parts[0]]
        order_id = parts[1]

        if order_id == AUTH_ORDER or self.headers.get("X-Api-Key") != self.server.api_key:
            self._json(401, {"error": "unauthorized"})
            return
        if order_id == NOT_FOUND_ORDER:
            self._json(404, {"error": "order_not_found"})
            return
        if order_id == BAD_JSON_ORDER:
            self._text(200, "this is not json")
            return
        if order_id == TIMEOUT_ORDER and source == "logistics":
            time.sleep(self.server.delay_seconds)

        customer_id = (parse_qs(parsed.query).get("customer_id") or [None])[0]
        self._json(
            200,
            {
                "data": _payload(source, order_id, customer_id),
                "observed_at": _now_iso(),
                "request_id": self.headers.get("X-Request-ID") or f"ref-{uuid.uuid4().hex[:12]}",
            },
        )


def build_server(
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    api_key: str = "local-sandbox-key",
    delay_seconds: float = 0.6,
    verbose: bool = False,
) -> ReferenceFactServer:
    return ReferenceFactServer(
        (host, port),
        api_key=api_key,
        delay_seconds=delay_seconds,
        verbose=verbose,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="本地参考事实 API（只读）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--api-key", default="local-sandbox-key")
    parser.add_argument("--delay-seconds", type=float, default=0.6)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    server = build_server(
        args.host,
        args.port,
        api_key=args.api_key,
        delay_seconds=args.delay_seconds,
        verbose=args.verbose,
    )
    host, port = server.server_address[:2]
    print(f"reference fact API: http://{host}:{port}")
    print(f"api key header: X-Api-Key={args.api_key}")
    print(f"orders: {OK_ORDER} / {NOT_FOUND_ORDER} / {TIMEOUT_ORDER} / {AUTH_ORDER} / {BAD_JSON_ORDER}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
