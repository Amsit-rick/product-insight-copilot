"""Validate or minimally probe real LLM, Embedding, and Reranker integrations."""

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
    parser = argparse.ArgumentParser(description="真实 AI 服务最小接入验收")
    parser.add_argument(
        "--allow-real-ai-calls",
        action="store_true",
        help="显式允许各调用一次 LLM、Embedding 和 Reranker",
    )
    parser.add_argument("--json-out", help="输出不含端点、密钥、回答、向量和原文的 JSON 证据")
    args = parser.parse_args(argv)

    from app.config import get_settings
    from app.services.ai_integration_probe import (
        describe_ai_integrations,
        run_ai_integration_probe,
    )

    settings = get_settings()
    config = describe_ai_integrations(settings)
    print("=== AI 接入配置（不回显端点或密钥）===")
    for name, item in config["components"].items():
        print(
            f"  [{'PASS' if item['configured'] else 'FAIL'}] {name:<10} "
            f"provider={item.get('provider')} model={item.get('model', '-')}"
        )

    if not args.allow_real_ai_calls:
        print("\n[config-only] 未发出网络请求；需要真实验收时显式添加 --allow-real-ai-calls。")
        _write_json(args.json_out, {**config, "executed": False})
        return 0 if config["ready"] else 1

    if not config["ready"]:
        print("\n[FAIL] 配置不完整，已在联网前停止。")
        _write_json(args.json_out, {**config, "executed": False})
        return 2

    result = run_ai_integration_probe(settings, allow_real_calls=True)
    print("\n=== 最小真实调用结果 ===")
    for item in result["results"]:
        print(
            f"  [{'PASS' if item['passed'] else 'FAIL'}] {item['component']:<10} "
            f"latency={item['latency_ms']}ms error={item.get('error_type', '-')}"
        )
    print("结果=PASS" if result["passed"] else "结果=FAIL")
    _write_json(args.json_out, result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
