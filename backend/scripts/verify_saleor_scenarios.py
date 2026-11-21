#!/usr/bin/env python
"""Verify all local Saleor scenario aliases without recording fact values."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from sandbox_services.scenarios import REQUIRED_SCENARIO_IDS  # noqa: E402


PUBLIC_SCENARIO_FIELDS = {
    "id",
    "name",
    "purpose",
    "source_type",
    "overlay_applied",
}
SOURCE_PATHS = ("orders", "payments", "logistics", "aftersales")


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction) - 1))
    return round(ordered[index], 2)


def verify(base_url: str, api_key: str, timeout_seconds: float) -> dict[str, Any]:
    headers = {"X-Api-Key": api_key, "X-Request-ID": "saleor-scenario-verifier"}
    checks: list[dict[str, Any]] = []
    latencies: list[float] = []
    with httpx.Client(
        base_url=base_url.rstrip("/"), timeout=timeout_seconds, trust_env=False
    ) as client:
        health = client.get("/health")
        health_payload = health.json() if health.status_code == 200 else {}
        checks.append(
            {
                "name": "health",
                "passed": health.status_code == 200
                and health_payload.get("fact_provider_status") == "saleor_ready"
                and health_payload.get("scenario_manifest_status") == "ready",
                "actual_status": health.status_code,
            }
        )

        unauthorized = client.get("/scenarios")
        checks.append(
            {
                "name": "scenario_catalog_requires_api_key",
                "passed": unauthorized.status_code == 401,
                "actual_status": unauthorized.status_code,
            }
        )

        catalog_response = client.get("/scenarios", headers=headers)
        try:
            catalog = catalog_response.json()
        except ValueError:
            catalog = {}
        scenarios = catalog.get("scenarios") if isinstance(catalog, dict) else None
        safe_catalog = isinstance(scenarios, list) and all(
            isinstance(item, dict) and set(item) == PUBLIC_SCENARIO_FIELDS
            for item in scenarios or []
        )
        scenario_ids = [str(item.get("id")) for item in scenarios or [] if isinstance(item, dict)]
        checks.append(
            {
                "name": "scenario_catalog",
                "passed": catalog_response.status_code == 200
                and catalog.get("environment") == "local_high_fidelity_sandbox"
                and tuple(scenario_ids) == REQUIRED_SCENARIO_IDS
                and safe_catalog,
                "actual_status": catalog_response.status_code,
                "scenario_count": len(scenario_ids),
            }
        )

        for scenario_id in scenario_ids:
            for source_path in SOURCE_PATHS:
                started = time.perf_counter()
                response = client.get(
                    f"/{source_path}/{scenario_id}", headers=headers
                )
                latency_ms = (time.perf_counter() - started) * 1000
                latencies.append(latency_ms)
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                data = payload.get("data") if isinstance(payload, dict) else None
                checks.append(
                    {
                        "name": f"scenario:{scenario_id}:{source_path}",
                        "passed": response.status_code == 200
                        and isinstance(data, dict)
                        and bool(data)
                        and payload.get("source_version")
                        == "saleor-3.23.49+scenario-v1"
                        and payload.get("request_id") == "saleor-scenario-verifier",
                        "actual_status": response.status_code,
                        "field_names": sorted(data) if isinstance(data, dict) else [],
                    }
                )

    passed = sum(1 for item in checks if item["passed"])
    return {
        "schema_version": "saleor-scenario-verification-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "verified_local" if passed == len(checks) else "failed",
        "environment": "local_high_fidelity_sandbox",
        "transport": "real_http",
        "enterprise_endpoint_verified": False,
        "real_payment_verified": False,
        "real_carrier_verified": False,
        "production_ready": False,
        "summary": {
            "passed": passed,
            "failed": len(checks) - passed,
            "total": len(checks),
            "scenario_count": len(scenario_ids),
            "fact_query_count": len(scenario_ids) * len(SOURCE_PATHS),
            "latency_ms_p50": _percentile(latencies, 0.50),
            "latency_ms_p95": _percentile(latencies, 0.95),
        },
        "checks": checks,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8092")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--json-out")
    args = parser.parse_args()
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
        parser.error("--base-url must be an HTTP(S) root without credentials or query data")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    return args


def main() -> int:
    args = _parse_args()
    api_key = os.getenv("SANDBOX_API_KEY", "local-sandbox-key")
    try:
        report = verify(args.base_url, api_key, args.timeout_seconds)
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Scenario verification failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if args.json_out:
        output = Path(args.json_out).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    summary = report["summary"]
    print(
        f"checks={summary['passed']}/{summary['total']} "
        f"scenarios={summary['scenario_count']} "
        f"fact_queries={summary['fact_query_count']} production_ready=false"
    )
    return 0 if report["status"] == "verified_local" else 1


if __name__ == "__main__":
    raise SystemExit(main())
