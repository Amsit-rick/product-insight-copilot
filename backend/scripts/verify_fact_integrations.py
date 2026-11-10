"""Verify real OMS/payment/logistics/after-sales adapters without leaking identifiers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _default_plan_path() -> Path:
    candidates = (
        BACKEND.parent / "docs" / "integrations" / "fact-probe-plan.example.json",
        BACKEND / "docs" / "integrations" / "fact-probe-plan.example.json",
    )
    return next((path for path in candidates if path.exists()), candidates[0])


def _write_json(path: str | None, payload: dict) -> None:
    if not path:
        return
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON 证据={output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="真实售后事实接口契约验收")
    parser.add_argument(
        "--plan",
        default=str(_default_plan_path()),
        help="不含真实标识的验收场景 JSON；默认使用随项目/镜像发布的计划",
    )
    parser.add_argument("--validate-plan-only", action="store_true")
    parser.add_argument("--allow-real-fact-adapters", action="store_true")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-p95-ms", type=int, default=20_000)
    parser.add_argument("--max-observed-age-seconds", type=int, default=900)
    parser.add_argument("--json-out", help="输出脱敏 JSON 证据")
    args = parser.parse_args(argv)

    from app.services.fact_integration_probe import (
        FactProbePlanError,
        load_fact_probe_plan,
        run_fact_probe,
    )

    try:
        plan = load_fact_probe_plan(args.plan)
    except FactProbePlanError as exc:
        print(f"[FAIL] 验收计划无效：{exc}")
        return 2

    metadata = plan.safe_metadata()
    print(
        f"验收计划有效：version={metadata['plan_version']} "
        f"scenarios={metadata['scenario_count']} sha256={metadata['plan_sha256']}"
    )
    if args.validate_plan_only:
        _write_json(args.json_out, {"status": "valid", **metadata})
        return 0
    if not args.allow_real_fact_adapters:
        print("[FAIL] 联网验收必须显式添加 --allow-real-fact-adapters")
        return 2
    if args.runs < 1 or args.max_p95_ms < 1 or args.max_observed_age_seconds < 1:
        print("[FAIL] runs、max-p95-ms、max-observed-age-seconds 必须为正整数")
        return 2

    from app.adapters.registry import (
        describe_fact_adapter,
        get_fact_adapter,
        reset_adapter_cache,
    )
    from app.config import get_settings

    settings = get_settings()
    described = describe_fact_adapter(settings)
    if settings.ticket_use_mock_adapters or not described.get("configured"):
        print("[FAIL] 当前事实适配器仍为 mock 或四个标准化端点未完整配置")
        return 2

    adapter = get_fact_adapter(settings)
    try:
        result = run_fact_probe(
            adapter,
            plan,
            runs=args.runs,
            max_p95_ms=args.max_p95_ms,
            max_observed_age_seconds=args.max_observed_age_seconds,
        )
    except FactProbePlanError as exc:
        print(f"[FAIL] 联网前检查失败：{exc}")
        return 2
    except Exception as exc:  # Do not print exception text: providers may include URLs or headers.
        print(f"[FAIL] 探测器异常：{type(exc).__name__}（详情已隐藏，避免泄露接口配置）")
        return 1
    finally:
        reset_adapter_cache()

    summary = result["summary"]
    print("=== 真实事实接口验收 ===")
    for source, item in summary["per_source"].items():
        print(
            f"{source:<10} calls={item['calls']} pass_rate={item['pass_rate']:.2%} "
            f"p95={item['p95_latency_ms']}ms"
        )
    print(
        f"checks={summary['checks_passed']}/{summary['checks_total']} "
        f"consistency={summary['scenario_consistency_rate']:.2%} "
        f"overall_p95={summary['p95_latency_ms']}ms"
    )
    print("结果=PASS" if result["passed"] else "结果=FAIL")
    _write_json(args.json_out, result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
