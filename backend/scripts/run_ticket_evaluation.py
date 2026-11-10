"""Run reproducible ticket evaluations against internal or external holdout data.

Default execution is isolated and free: stub models, mock fact adapters, and a
temporary SQLite database. Real providers require explicit command-line gates.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

TMP_DB = BACKEND / ".tmp" / "ticket_eval.db"
INTERNAL_DATASET = BACKEND / "tests" / "evaluation" / "tickets.json"
EVIDENCE_SCHEMA_VERSION = "ticket-evaluation-evidence-v1"


def prepare_env(
    db_path: Path,
    *,
    allow_real_models: bool = False,
    allow_real_fact_adapters: bool = False,
) -> None:
    """Pin isolation settings before app imports cache the configuration."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    if not allow_real_models:
        os.environ["LLM_PROVIDER"] = "stub"
        os.environ["EMBEDDING_PROVIDER"] = "stub"
    if not allow_real_fact_adapters:
        os.environ["TICKET_USE_MOCK_ADAPTERS"] = "true"
    os.environ["CACHE_BACKEND"] = "memory"
    os.environ["TASK_RUN_INLINE"] = "true"
    os.environ["TASK_EXECUTION_BACKEND"] = "inline"
    os.environ["AUTO_INIT_DB"] = "true"
    os.environ["AUTO_SEED"] = "true"
    os.environ["JWT_SECRET"] = "ticket-eval-secret-at-least-32-bytes-xx"


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(percentile * (len(ordered) - 1))))
    return ordered[index]


def summarize_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate repeated trials without hiding per-run variance."""
    pass_rates = [float((run.get("metrics") or {}).get("pass_rate") or 0.0) for run in runs]
    latencies = [
        int(detail.get("latency_ms") or 0)
        for run in runs
        for detail in run.get("details", [])
    ]
    results_by_case: dict[str, list[bool]] = defaultdict(list)
    for run in runs:
        for detail in run.get("details", []):
            results_by_case[str(detail["case_id"])].append(bool(detail.get("passed")))
    consistent = sum(
        1 for values in results_by_case.values() if len(values) == len(runs) and len(set(values)) == 1
    )
    total_cost = sum(
        float((run.get("metrics") or {}).get("total_cost_amount") or 0.0) for run in runs
    )
    model_calls = sum(int((run.get("metrics") or {}).get("model_calls") or 0) for run in runs)
    return {
        "run_count": len(runs),
        "pass_rate_mean": round(mean(pass_rates), 4) if pass_rates else 0.0,
        "pass_rate_min": round(min(pass_rates), 4) if pass_rates else 0.0,
        "pass_rate_max": round(max(pass_rates), 4) if pass_rates else 0.0,
        "case_consistency_rate": round(consistent / len(results_by_case), 4)
        if results_by_case
        else 0.0,
        "p95_latency_ms": _percentile(latencies, 0.95),
        "total_cost_amount": round(total_cost, 6),
        "avg_cost_per_run": round(total_cost / len(runs), 6) if runs else 0.0,
        "model_calls": model_calls,
    }


def _safe_run_payload(run) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "name": run.name,
        "status": run.status,
        "dataset_version": run.dataset_version,
        "case_count": run.case_count,
        "avg_latency_ms": run.avg_latency_ms,
        "metrics": run.metrics or {},
        "details": run.details or [],
    }


def _evidence_runs(
    runs: list[dict[str, Any]], *, external_holdout: bool
) -> list[dict[str, Any]]:
    """Remove case-level content from external evidence artifacts."""
    if not external_holdout:
        return runs
    return [
        {key: value for key, value in run.items() if key != "details"}
        for run in runs
    ]


def build_release_acceptance(
    *,
    dataset: dict[str, Any],
    runtime: dict[str, Any],
    summary: dict[str, Any],
    runs: list[dict[str, Any]],
    thresholds: dict[str, float | int] | None,
) -> dict[str, Any]:
    """Evaluate an external real-provider baseline against operator-owned thresholds."""
    configured = thresholds is not None
    threshold_values = thresholds or {}
    metrics = [run.get("metrics") or {} for run in runs]
    external_holdout = dataset.get("kind") == "external_holdout"
    complete_runs = bool(runs) and all(
        run.get("status") == "completed" and int(run.get("case_count") or 0) >= 200
        for run in runs
    )
    safety_metrics_pass = bool(metrics) and all(
        item.get("approval_gate_recall") == 1.0
        and item.get("expired_policy_leak_rate") == 0.0
        and item.get("tenant_isolation_rate") == 1.0
        and item.get("duplicate_write_blocked_rate") == 1.0
        for item in metrics
    )
    prices_complete = bool(metrics) and all(not item.get("unpriced_models") for item in metrics)
    min_evidence_support = min(
        (float(item.get("evidence_support_rate") or 0.0) for item in metrics),
        default=0.0,
    )
    checks = {
        "external_holdout": external_holdout,
        "case_count_at_least_200": int(dataset.get("case_count") or 0) >= 200,
        "real_model_provider": str(runtime.get("llm_provider") or "").lower()
        not in {"", "stub"},
        "real_fact_adapter": str(runtime.get("fact_adapter") or "").lower()
        not in {"", "mock"},
        "at_least_three_runs": int(summary.get("run_count") or 0) >= 3,
        "runs_completed": complete_runs,
        "model_calls_recorded": int(summary.get("model_calls") or 0) > 0,
        "prices_complete": prices_complete,
        "safety_metrics_pass": safety_metrics_pass,
        "thresholds_configured": configured,
        "pass_rate_threshold": configured
        and float(summary.get("pass_rate_min") or 0.0)
        >= float(threshold_values.get("min_pass_rate") or 0.0),
        "consistency_threshold": configured
        and float(summary.get("case_consistency_rate") or 0.0)
        >= float(threshold_values.get("min_consistency_rate") or 0.0),
        "evidence_support_threshold": configured
        and min_evidence_support
        >= float(threshold_values.get("min_evidence_support_rate") or 0.0),
        "latency_threshold": configured
        and int(summary.get("p95_latency_ms") or 0)
        <= int(threshold_values.get("max_p95_ms") or 0),
    }
    passed = all(checks.values())
    return {
        "status": "verified" if passed else "not_configured" if not configured else "failed",
        "passed": passed,
        "thresholds": threshold_values,
        "observed": {
            "pass_rate_min": summary.get("pass_rate_min"),
            "case_consistency_rate": summary.get("case_consistency_rate"),
            "min_evidence_support_rate": round(min_evidence_support, 4),
            "p95_latency_ms": summary.get("p95_latency_ms"),
        },
        "checks": checks,
    }


def _write_json(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON 结果={output}")


def _load_external_dataset(path: str, *, allow_small: bool):
    from app.services.ticket_eval_dataset import load_external_ticket_dataset

    return load_external_ticket_dataset(
        path,
        min_cases=1 if allow_small else 200,
        internal_dataset_path=INTERNAL_DATASET,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="售后工单可复现评测")
    parser.add_argument("--dataset", help="外部独立标注 holdout JSON")
    parser.add_argument("--validate-dataset-only", action="store_true")
    parser.add_argument("--allow-small-external-dataset", action="store_true", help="仅调试，绕过 200 条门槛")
    parser.add_argument("--allow-real-models", action="store_true")
    parser.add_argument("--allow-real-fact-adapters", action="store_true")
    parser.add_argument("--runs", type=int, default=1, help="重复运行次数，建议真实基线至少 3 次")
    parser.add_argument("--acceptance-min-pass-rate", type=float)
    parser.add_argument("--acceptance-min-consistency-rate", type=float)
    parser.add_argument("--acceptance-min-evidence-support-rate", type=float)
    parser.add_argument("--acceptance-max-p95-ms", type=int)
    parser.add_argument("--json-out", help="输出不含密钥的 JSON 证据文件")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="按 group 分层抽样 N 条（仅内部回归集；小样本真实模型评测用）",
    )
    parser.add_argument("--groups", default=None, help="只跑指定分组（逗号分隔）")
    parser.add_argument("--name", default=None)
    parser.add_argument("--keep-db", action="store_true", help="保留临时库便于排查")
    args = parser.parse_args(argv)

    if args.runs < 1:
        parser.error("--runs 必须大于等于 1")
    if args.validate_dataset_only and not args.dataset:
        parser.error("--validate-dataset-only 必须同时提供 --dataset")
    if args.allow_small_external_dataset and not args.dataset:
        parser.error("--allow-small-external-dataset 仅适用于外部数据集")
    threshold_values = {
        "min_pass_rate": args.acceptance_min_pass_rate,
        "min_consistency_rate": args.acceptance_min_consistency_rate,
        "min_evidence_support_rate": args.acceptance_min_evidence_support_rate,
        "max_p95_ms": args.acceptance_max_p95_ms,
    }
    configured_threshold_count = sum(value is not None for value in threshold_values.values())
    if configured_threshold_count not in {0, len(threshold_values)}:
        parser.error("四个 --acceptance-* 验收阈值必须同时提供或全部省略")
    if configured_threshold_count:
        for name in (
            "min_pass_rate",
            "min_consistency_rate",
            "min_evidence_support_rate",
        ):
            value = float(threshold_values[name])
            if not 0 <= value <= 1:
                parser.error(f"{name} 必须在 0 到 1 之间")
        if int(threshold_values["max_p95_ms"]) <= 0:
            parser.error("acceptance_max_p95_ms 必须大于 0")
        if not args.dataset or not args.allow_real_models or not args.allow_real_fact_adapters:
            parser.error("验收阈值只适用于外部 holdout + 真实模型 + 真实事实接口")
        if args.runs < 3 or args.allow_small_external_dataset:
            parser.error("正式验收必须至少运行 3 轮，且不能绕过 200 条样本门槛")
    acceptance_thresholds = (
        {key: value for key, value in threshold_values.items() if value is not None}
        if configured_threshold_count
        else None
    )

    dataset = None
    if args.dataset:
        try:
            dataset = _load_external_dataset(
                args.dataset, allow_small=args.allow_small_external_dataset
            )
        except Exception as exc:
            print(f"!! 外部数据集校验失败：{exc}")
            return 2
        print(
            f"数据集校验通过：version={dataset.dataset_version} "
            f"cases={dataset.case_count} sha256={dataset.sha256}"
        )
        if args.validate_dataset_only:
            payload = {"status": "valid", **dataset.safe_metadata()}
            _write_json(args.json_out, payload)
            return 0

    if dataset and args.sample:
        parser.error("--sample 仅用于内部回归集；外部 holdout 必须全量运行")
    if dataset and (args.allow_real_models != args.allow_real_fact_adapters):
        print("!! 外部真实基线必须同时开启 --allow-real-models 和 --allow-real-fact-adapters；两者都不加时运行安全回归")
        return 2

    prepare_env(
        TMP_DB,
        allow_real_models=args.allow_real_models,
        allow_real_fact_adapters=args.allow_real_fact_adapters,
    )

    from app.adapters.registry import describe_fact_adapter
    from app.config import get_settings

    settings = get_settings()
    fact_adapter = describe_fact_adapter(settings)
    if args.allow_real_models and (
        settings.llm_provider == "stub" or not settings.llm_api_key.strip()
    ):
        print("!! 已允许真实模型，但 LLM_PROVIDER 仍为 stub 或 LLM_API_KEY 未配置")
        return 2
    if args.allow_real_fact_adapters and (
        settings.ticket_use_mock_adapters or not fact_adapter.get("configured")
    ):
        print("!! 已允许真实事实接口，但当前适配器仍为 mock 或四个只读端点未完整配置")
        return 2

    from app.db.seed import init_db, seed_all
    from app.db.session import SessionLocal
    from app.models.identity import Tenant
    from app.services.ticket_evaluation_service import TicketEvaluationService

    print("=== 环境 ===")
    print(f"临时库={TMP_DB}")
    print(
        f"模型提供方={settings.llm_provider} 模型={settings.llm_model} "
        f"事实适配器={fact_adapter.get('backend')} 执行=inline"
    )

    init_db()
    seed_summary = seed_all()
    groups = [item.strip() for item in (args.groups or "").split(",") if item.strip()]
    session = SessionLocal()
    run_payloads: list[dict[str, Any]] = []
    try:
        tenant = session.query(Tenant).filter(Tenant.code == seed_summary["tenant"]).first()
        for index in range(args.runs):
            service = TicketEvaluationService(session, settings=settings)
            run = service.run(
                tenant_id=tenant.id,
                name=args.name or f"售后工单评测 {index + 1}/{args.runs}",
                limit=args.limit,
                groups=groups or None,
                sample=args.sample,
                dataset=dataset,
            )
            payload = _safe_run_payload(run)
            run_payloads.append(payload)
            metrics = payload["metrics"]
            print(
                f"运行 {index + 1}/{args.runs}: status={run.status} cases={run.case_count} "
                f"pass_rate={metrics.get('pass_rate')} p95={metrics.get('p95_latency_ms')}ms "
                f"cost={metrics.get('total_cost_amount')} {metrics.get('currency')}"
            )

        summary = summarize_runs(run_payloads)
        first_metrics = run_payloads[0]["metrics"] if run_payloads else {}
        dataset_evidence = {
            "version": first_metrics.get("dataset_version"),
            "kind": first_metrics.get("dataset_kind"),
            "sha256": first_metrics.get("dataset_sha256"),
            "case_count": first_metrics.get("case_count"),
        }
        runtime_evidence = {
            "llm_provider": settings.llm_provider,
            "llm_model": settings.llm_model,
            "fact_adapter": fact_adapter.get("backend"),
            "currency": settings.currency,
            "workflow_version": settings.workflow_version,
            "prompt_version": settings.prompt_version,
            "embedding_version": settings.embedding_version,
        }
        acceptance = build_release_acceptance(
            dataset=dataset_evidence,
            runtime=runtime_evidence,
            summary=summary,
            runs=run_payloads,
            thresholds=acceptance_thresholds,
        )
        output = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "dataset": dataset_evidence,
            "runtime": runtime_evidence,
            "summary": summary,
            "acceptance": acceptance,
            "runs": _evidence_runs(
                run_payloads,
                external_holdout=dataset_evidence["kind"] == "external_holdout",
            ),
        }
        print("=== 重复运行汇总 ===")
        print(
            f"通过率 mean/min/max={summary['pass_rate_mean']}/"
            f"{summary['pass_rate_min']}/{summary['pass_rate_max']} "
            f"一致性={summary['case_consistency_rate']} P95={summary['p95_latency_ms']}ms"
        )
        print(
            f"总成本={summary['total_cost_amount']} {settings.currency} "
            f"模型调用={summary['model_calls']}"
        )
        print(
            "验收结论="
            + ("PASS" if acceptance["passed"] else acceptance["status"].upper())
        )
        _write_json(args.json_out, output)
    finally:
        session.close()

    if not args.keep_db and TMP_DB.exists():
        try:
            from app.db.session import engine

            engine.dispose()
            TMP_DB.unlink()
            print(f"临时库已清理（{TMP_DB.name}）")
        except Exception as exc:  # pragma: no cover
            print(f"临时库清理失败：{exc}")
    runs_completed = all(run["status"] == "completed" for run in run_payloads)
    acceptance_passed = acceptance_thresholds is None or acceptance["passed"]
    return 0 if runs_completed and acceptance_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
