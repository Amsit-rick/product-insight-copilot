#!/usr/bin/env python
r"""本地回环演练：真实 HTTP 事实适配器 + 参考事实 API + 脱敏验收。

不访问外网、不需要厂商凭据；用于在拿到 sandbox 之前验证：
成功、404、物流超时、401、非 JSON 五类语义，以及出口字段白名单/手机号/地址脱敏。

运行：
    cd backend
    ..\.venv\Scripts\python.exe scripts\verify_fact_local_rehearsal.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND.parent
PLAN = PROJECT_ROOT / "docs" / "integrations" / "fact-probe-plan.local.json"
OUTPUT = PROJECT_ROOT / "docs" / "testing" / "artifacts" / "fact-integration-local-check.json"

sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))

from reference_fact_api import build_server  # noqa: E402


def main() -> int:
    server = build_server(port=0, delay_seconds=0.6)
    host, port = server.server_address[:2]
    base = f"http://{host}:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    os.environ.update(
        {
            "APP_ENV": "local",
            "TICKET_USE_MOCK_ADAPTERS": "false",
            "TICKET_FACT_ORDER_URL": f"{base}/orders/{{order_id}}",
            "TICKET_FACT_PAYMENT_URL": f"{base}/payments/{{order_id}}",
            "TICKET_FACT_LOGISTICS_URL": f"{base}/logistics/{{order_id}}",
            "TICKET_FACT_AFTERSALE_URL": f"{base}/aftersales/{{order_id}}",
            "TICKET_FACT_AUTH_HEADERS": '{"X-Api-Key":"local-sandbox-key"}',
            "TICKET_FACT_TIMEOUT_SECONDS": "0.25",
            "TICKET_FACT_MAX_RETRIES": "0",
            "TICKET_FACT_RETRY_BACKOFF_SECONDS": "0",
            "FACT_PROBE_SUCCESS_ORDER_ID": "SANDBOX-OK-001",
            "FACT_PROBE_SUCCESS_CUSTOMER_ID": "SANDBOX-CUST-001",
            "FACT_PROBE_NOT_FOUND_ORDER_ID": "SANDBOX-NOTFOUND-001",
            "FACT_PROBE_LOGISTICS_TIMEOUT_ORDER_ID": "SANDBOX-TIMEOUT-001",
            "FACT_PROBE_AUTH_ORDER_ID": "SANDBOX-AUTH-001",
            "FACT_PROBE_BADJSON_ORDER_ID": "SANDBOX-BADJSON-001",
        }
    )

    try:
        from app.adapters.registry import get_fact_adapter, reset_adapter_cache
        from app.config import get_settings
        from app.services.fact_integration_probe import load_fact_probe_plan, run_fact_probe

        get_settings.cache_clear()
        reset_adapter_cache()
        settings = get_settings()
        plan = load_fact_probe_plan(PLAN)
        adapter = get_fact_adapter(settings)
        result = run_fact_probe(
            adapter,
            plan,
            runs=1,
            max_p95_ms=10_000,
            max_observed_age_seconds=300,
        )
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        summary = result["summary"]
        print("=== 本地回环事实接口演练 ===")
        for source, item in summary["per_source"].items():
            print(
                f"{source:<10} calls={item['calls']} pass_rate={item['pass_rate']:.2%} "
                f"p95={item['p95_latency_ms']}ms"
            )
        print(
            f"checks={summary['checks_passed']}/{summary['checks_total']} "
            f"overall_p95={summary['p95_latency_ms']}ms"
        )
        print("结果=" + ("PASS" if result["passed"] else "FAIL"))
        print(f"JSON 证据={OUTPUT}")
        return 0 if result["passed"] else 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        os.environ.pop("TICKET_USE_MOCK_ADAPTERS", None)
        try:
            from app.config import get_settings

            get_settings.cache_clear()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
