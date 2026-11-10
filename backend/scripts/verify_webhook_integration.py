"""Verify Webhook delivery locally and optionally send one real probe event."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _write_json(path: str | None, payload: dict) -> None:
    if not path:
        return
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON 证据={output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行告警 Webhook 安全验收")
    parser.add_argument(
        "--allow-real-webhook-call",
        action="store_true",
        help="显式授权向当前 ALERT_WEBHOOK_URL 发送一条不含生产数据的探针事件",
    )
    parser.add_argument(
        "--require-production-config",
        action="store_true",
        help="要求当前配置已启用 HTTPS 和 HMAC；不代表已发生真实送达",
    )
    parser.add_argument("--json-out", help="输出不含 URL、密钥和请求正文的 JSON 证据")
    args = parser.parse_args(argv)

    from app.config import get_settings
    from app.services.webhook_integration_probe import run_webhook_integration_probe

    result = run_webhook_integration_probe(
        get_settings(), allow_real_call=args.allow_real_webhook_call
    )
    print("=== Webhook 本地闭环 ===")
    for name, passed in result["local_contract"]["checks"].items():
        print(f"{'PASS' if passed else 'FAIL':<4} {name}")
    print(
        "生产配置="
        + ("READY" if result["configuration"]["passed"] else "NOT_READY")
        + "（地址与密钥不回显）"
    )
    external = result["external_delivery"]
    if external["executed"]:
        print("真实最小投递=" + ("PASS" if external["passed"] else "FAIL"))
    else:
        print(f"真实最小投递=SKIP ({external['reason']})")
    print("production_ready=" + str(result["production_ready"]).lower())
    _write_json(args.json_out, result)

    if not result["local_contract"]["passed"]:
        return 1
    if args.require_production_config and not result["configuration"]["passed"]:
        return 2
    if args.allow_real_webhook_call and not result["production_ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
