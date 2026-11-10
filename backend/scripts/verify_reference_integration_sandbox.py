#!/usr/bin/env python
"""Verify the reference integration sandbox over a real HTTP socket.

The report deliberately excludes endpoint URLs, credentials, request bodies, fact
values and metric values. Passing this verifier proves only the local/reference
contract; it is never production evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

import httpx
import uvicorn

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.adapters.metric_http import (  # noqa: E402
    HttpMetricSourceAdapter,
    MetricSourceError,
)
from sandbox_services.app import create_app  # noqa: E402


class CheckRecorder:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(
        self,
        name: str,
        passed: bool,
        *,
        expected_status: int | None = None,
        actual_status: int | None = None,
        error_code: str | None = None,
        error_type: str | None = None,
    ) -> None:
        item: dict[str, Any] = {"name": name, "passed": bool(passed)}
        if expected_status is not None:
            item["expected_status"] = expected_status
        if actual_status is not None:
            item["actual_status"] = actual_status
        if error_code:
            item["error_code"] = error_code
        if error_type:
            item["error_type"] = error_type
        self.items.append(item)

    def status(self, name: str, response: httpx.Response, expected: int) -> None:
        self.add(
            name,
            response.status_code == expected,
            expected_status=expected,
            actual_status=response.status_code,
        )


@contextmanager
def _local_server(api_key: str, webhook_secret: str) -> Iterator[str]:
    config = uvicorn.Config(
        create_app(
            api_key=api_key,
            webhook_secret=webhook_secret,
            delay_seconds=0.25,
        ),
        host="127.0.0.1",
        port=0,
        log_level="error",
        access_log=False,
    )
    listening_socket = config.bind_socket()
    port = int(listening_socket.getsockname()[1])
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listening_socket]},
        daemon=True,
        name="reference-integration-sandbox",
    )
    thread.start()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            try:
                with httpx.Client(timeout=0.2, trust_env=False) as probe:
                    if probe.get(f"{base_url}/health").status_code == 200:
                        break
            except httpx.RequestError:
                time.sleep(0.02)
        else:
            raise RuntimeError("sandbox_start_timeout")
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listening_socket.close()


def _sign_webhook(secret: str, body: bytes, delivery_id: str) -> dict[str, str]:
    timestamp = "1794172800"
    digest = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("utf-8") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Alert-Delivery-Id": delivery_id,
        "X-Alert-Timestamp": timestamp,
        "X-Alert-Signature": f"sha256={digest}",
    }


def _metric_error_code(call) -> str | None:  # noqa: ANN001
    try:
        call()
    except MetricSourceError as exc:
        return exc.code
    return None


def _run_checks(base_url: str, api_key: str, webhook_secret: str) -> CheckRecorder:
    checks = CheckRecorder()
    headers = {"X-Api-Key": api_key, "X-Request-ID": "sandbox-verifier"}
    with httpx.Client(base_url=base_url, timeout=2.0, trust_env=False) as client:
        try:
            health = client.get("/health")
            checks.status("health", health, 200)

            unauthorized = client.get("/orders/SANDBOX-OK-001")
            checks.status("fact_missing_api_key", unauthorized, 401)

            for source in ("orders", "payments", "logistics", "aftersales"):
                response = client.get(f"/{source}/SANDBOX-OK-001", headers=headers)
                valid_shape = False
                if response.status_code == 200:
                    try:
                        payload = response.json()
                        valid_shape = (
                            isinstance(payload, dict)
                            and isinstance(payload.get("data"), dict)
                            and bool(payload.get("observed_at"))
                            and payload.get("request_id") == "sandbox-verifier"
                        )
                    except ValueError:
                        valid_shape = False
                checks.add(
                    f"fact_{source}_success",
                    response.status_code == 200 and valid_shape,
                    expected_status=200,
                    actual_status=response.status_code,
                )

            checks.status(
                "fact_not_found",
                client.get("/orders/SANDBOX-NOTFOUND-001", headers=headers),
                404,
            )
            checks.status(
                "fact_forced_auth_failure",
                client.get("/orders/SANDBOX-AUTH-001", headers=headers),
                401,
            )
            bad_json = client.get("/orders/SANDBOX-BADJSON-001", headers=headers)
            invalid_json = False
            try:
                bad_json.json()
            except ValueError:
                invalid_json = True
            checks.add(
                "fact_invalid_json",
                bad_json.status_code == 200 and invalid_json,
                expected_status=200,
                actual_status=bad_json.status_code,
            )
            try:
                client.get(
                    "/logistics/SANDBOX-TIMEOUT-001",
                    headers=headers,
                    timeout=0.05,
                )
            except httpx.TimeoutException:
                checks.add("fact_timeout", True, error_code="timeout_observed")
            except httpx.RequestError as exc:
                checks.add("fact_timeout", False, error_type=type(exc).__name__)
            else:
                checks.add("fact_timeout", False, error_code="timeout_not_observed")

            metric_endpoint = f"{base_url.rstrip('/')}/api/v1/metric-snapshots"
            adapter = HttpMetricSourceAdapter(
                endpoint=metric_endpoint,
                headers={"X-Api-Key": api_key},
                timeout_seconds=2,
                max_retries=0,
                client=client,
            )
            metric_batch = adapter.fetch_snapshots(
                tenant_id="sandbox-tenant",
                metric_names=["crash_rate", "next_day_retention"],
                date_from=date(2026, 11, 8),
                date_to=date(2026, 11, 9),
                request_id="sandbox-metric-verifier",
            )
            checks.add(
                "metric_adapter_success",
                len(metric_batch.records) == 4
                and metric_batch.request_id == "sandbox-metric-verifier",
            )

            metric_params = {
                "metric_names": "crash_rate",
                "date_from": "2026-11-08",
                "date_to": "2026-11-09",
            }
            unknown = client.get(
                "/api/v1/metric-snapshots",
                headers=headers,
                params={**metric_params, "tenant_id": "sandbox-tenant", "metric_names": "unknown"},
            )
            checks.status("metric_unknown_rejected", unknown, 422)

            cross_tenant_code = _metric_error_code(
                lambda: adapter.fetch_snapshots(
                    tenant_id="SANDBOX-CROSS-TENANT",
                    metric_names=["crash_rate"],
                    date_from=date(2026, 11, 8),
                    date_to=date(2026, 11, 9),
                )
            )
            checks.add(
                "metric_cross_tenant_rejected",
                cross_tenant_code == "metric_source_tenant_mismatch",
                error_code=cross_tenant_code,
            )
            rate_limit_code = _metric_error_code(
                lambda: adapter.fetch_snapshots(
                    tenant_id="SANDBOX-METRIC-RATE-LIMIT",
                    metric_names=["crash_rate"],
                    date_from=date(2026, 11, 8),
                    date_to=date(2026, 11, 9),
                )
            )
            checks.add(
                "metric_rate_limit",
                rate_limit_code == "metric_source_rate_limited",
                error_code=rate_limit_code,
            )
            unavailable_code = _metric_error_code(
                lambda: adapter.fetch_snapshots(
                    tenant_id="SANDBOX-METRIC-ERROR",
                    metric_names=["crash_rate"],
                    date_from=date(2026, 11, 8),
                    date_to=date(2026, 11, 9),
                )
            )
            checks.add(
                "metric_temporary_unavailable",
                unavailable_code == "metric_source_upstream_error",
                error_code=unavailable_code,
            )

            webhook_body = json.dumps(
                {"event": "operational_alert", "alert": {"kind": "sandbox_probe"}},
                separators=(",", ":"),
            ).encode("utf-8")
            valid_headers = _sign_webhook(webhook_secret, webhook_body, "delivery-ok")
            accepted = client.post(
                "/webhooks/alerts", content=webhook_body, headers=valid_headers
            )
            checks.status("webhook_hmac_success", accepted, 204)
            rejected = client.post(
                "/webhooks/alerts",
                content=webhook_body,
                headers={**valid_headers, "X-Alert-Signature": "sha256=invalid"},
            )
            checks.status("webhook_invalid_hmac", rejected, 401)

            retry_headers = _sign_webhook(webhook_secret, webhook_body, "delivery-retry")
            retry_first = client.post(
                "/webhooks/alerts/retry", content=webhook_body, headers=retry_headers
            )
            retry_second = client.post(
                "/webhooks/alerts/retry", content=webhook_body, headers=retry_headers
            )
            checks.add(
                "webhook_retry_then_success",
                retry_first.status_code == 503 and retry_second.status_code == 204,
                expected_status=204,
                actual_status=retry_second.status_code,
            )

            dead_headers = _sign_webhook(webhook_secret, webhook_body, "delivery-dead")
            dead = client.post(
                "/webhooks/alerts/dead", content=webhook_body, headers=dead_headers
            )
            replayed = client.post(
                "/webhooks/alerts/dead",
                content=webhook_body,
                headers={**dead_headers, "X-Sandbox-Replay": "true"},
            )
            checks.add(
                "webhook_dead_letter_replay",
                dead.status_code == 400 and replayed.status_code == 204,
                expected_status=204,
                actual_status=replayed.status_code,
            )

            stats = client.get("/webhooks/alerts/stats", headers=headers)
            stats_shape = False
            if stats.status_code == 200:
                try:
                    stats_payload = stats.json()
                    stats_shape = set(stats_payload) == {
                        "accepted_count",
                        "rejected_count",
                        "last_delivery_id",
                        "retry_delivery_count",
                    }
                except ValueError:
                    stats_shape = False
            checks.add(
                "webhook_stats_metadata_only",
                stats.status_code == 200 and stats_shape,
                expected_status=200,
                actual_status=stats.status_code,
            )
        except Exception as exc:  # Never expose exception text: it can contain endpoint data.
            checks.add("verifier_unexpected_error", False, error_type=type(exc).__name__)
    return checks


def _write_json(path: str | None, report: dict[str, Any]) -> None:
    if not path:
        return
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="通过真实 HTTP 验证本地/隔离集成 Sandbox 契约",
    )
    parser.add_argument(
        "--base-url",
        help="已启动 Sandbox 的地址；不传时自动在随机本地端口启动临时服务",
    )
    parser.add_argument("--json-out", help="输出脱敏 JSON 证据")
    args = parser.parse_args(argv)
    if args.base_url:
        parsed = urlsplit(args.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            parser.error("--base-url 必须是不含凭据、查询参数、片段和子路径的 HTTP(S) 根地址")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    self_hosted = not bool(args.base_url)
    api_key = (
        secrets.token_urlsafe(24)
        if self_hosted
        else os.getenv("SANDBOX_API_KEY", "local-sandbox-key")
    )
    webhook_secret = (
        secrets.token_urlsafe(32)
        if self_hosted
        else os.getenv("SANDBOX_WEBHOOK_SECRET", "local-webhook-secret")
    )

    if self_hosted:
        with _local_server(api_key, webhook_secret) as base_url:
            checks = _run_checks(base_url, api_key, webhook_secret)
    else:
        checks = _run_checks(str(args.base_url).rstrip("/"), api_key, webhook_secret)

    passed = sum(1 for item in checks.items if item["passed"])
    failed = len(checks.items) - passed
    report = {
        "schema_version": "reference-integration-sandbox-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "verified_local"
            if self_hosted and failed == 0
            else "verified_endpoint_contract"
            if failed == 0
            else "failed"
        ),
        "transport": "real_http",
        "self_hosted": self_hosted,
        "production_ready": False,
        "evidence_scope": "reference_sandbox_contract_only",
        "summary": {"passed": passed, "failed": failed, "total": len(checks.items)},
        "checks": checks.items,
    }
    for item in checks.items:
        print(f"{'PASS' if item['passed'] else 'FAIL':<4} {item['name']}")
    print(f"checks={passed}/{len(checks.items)} production_ready=false")
    _write_json(args.json_out, report)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
