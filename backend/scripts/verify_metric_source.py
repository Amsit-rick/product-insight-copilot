"""只读指标源连通性探针。

默认不会访问网络。只有显式传入 ``--allow-real-source-call`` 才调用已配置的标准化 GET
端点；报告只保存结构和计数，不保存 URL、鉴权头、请求 ID 或指标值。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.adapters.metric_http import (  # noqa: E402
    HttpMetricSourceAdapter,
    MetricSourceError,
    validate_metric_source_settings,
)
from app.config import Settings, get_settings  # noqa: E402
from app.models.base import utcnow  # noqa: E402


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="标准化只读指标源连通性探针")
    parser.add_argument("--tenant-id", required=True, help="隔离测试租户 ID")
    parser.add_argument(
        "--allow-real-source-call",
        action="store_true",
        help="显式允许执行真实只读 GET；缺少该参数时绝不访问网络",
    )
    parser.add_argument("--runs", type=int, default=3, help="连续只读验证次数（1-10）")
    parser.add_argument(
        "--max-latency-ms",
        type=float,
        default=5000,
        help="单次读取延迟上限，默认 5000ms",
    )
    parser.add_argument("--json-output", type=Path, required=True, help="脱敏 JSON 证据输出路径")
    args = parser.parse_args(argv)
    if not 1 <= args.runs <= 10:
        parser.error("--runs 必须在 1 到 10 之间")
    if args.max_latency_ms <= 0:
        parser.error("--max-latency-ms 必须大于 0")
    return args


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _batch_metadata(records: list[dict]) -> dict:
    metric_names = sorted(
        {str(row.get("metric_name")) for row in records if row.get("metric_name")}
    )
    dates = sorted(
        {str(row.get("stat_date")) for row in records if row.get("stat_date")}
    )
    return {
        "row_count": len(records),
        "metric_names": metric_names,
        "date_range": {
            "min": dates[0] if dates else None,
            "max": dates[-1] if dates else None,
        },
    }


def _configuration_metadata(settings: Settings, *, tenant_id: str, runs: int) -> dict:
    endpoint = urlsplit(str(settings.metric_source_http_url or "").strip())
    return {
        "app_env": settings.app_env,
        "enabled": settings.metric_source_http_enabled,
        "read_method": "GET",
        "https": endpoint.scheme == "https",
        "auth_configured": bool(settings.metric_source_http_auth_headers),
        "tenant_scope_provided": bool(tenant_id.strip()),
        "metric_whitelist_count": len(settings.proactive_anomaly_metrics),
        "runs_requested": runs,
    }


def main(
    argv: list[str] | None = None,
    *,
    settings: Settings | None = None,
    adapter_factory: Callable[[Settings], object] | None = None,
) -> int:
    args = _parse_args(argv)
    runtime_settings = settings or get_settings()
    base_report = {
        "schema_version": "metric-source-probe-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "external_network_used": False,
        "production_ready": False,
        "configuration": _configuration_metadata(
            runtime_settings,
            tenant_id=args.tenant_id,
            runs=args.runs,
        ),
    }
    if not args.allow_real_source_call:
        report = {
            **base_report,
            "status": "blocked_not_authorized",
            "reason": "缺少 --allow-real-source-call，未连接指标源",
        }
        _write_report(args.json_output, report)
        return 2

    errors = validate_metric_source_settings(runtime_settings)
    if errors:
        report = {
            **base_report,
            "status": "failed",
            "error_code": "metric_source_config_invalid",
        }
        _write_report(args.json_output, report)
        return 1

    factory = adapter_factory or HttpMetricSourceAdapter.from_settings
    adapter = None
    report = {**base_report, "external_network_used": True}
    try:
        adapter = factory(runtime_settings)
        date_to = utcnow().date()
        date_from = date_to - timedelta(
            days=max(runtime_settings.proactive_anomaly_lookback_days - 1, 0)
        )
        run_results: list[dict] = []
        first_metadata: dict | None = None
        for run_number in range(1, args.runs + 1):
            started = time.perf_counter()
            batch = adapter.fetch_snapshots(
                tenant_id=args.tenant_id,
                metric_names=runtime_settings.proactive_anomaly_metrics,
                date_from=date_from,
                date_to=date_to,
            )
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            metadata = _batch_metadata(batch.records)
            if metadata["row_count"] <= 0:
                raise MetricSourceError("metric_source_empty_dataset", "指标源未返回数据")
            if first_metadata is None:
                first_metadata = metadata
            run_results.append(
                {
                    "run": run_number,
                    "latency_ms": latency_ms,
                    "row_count": metadata["row_count"],
                    "dataset_version_present": bool(batch.dataset_version),
                    "source_version_present": bool(batch.source_version),
                    "metric_names": metadata["metric_names"],
                }
            )
        latencies = sorted(float(item["latency_ms"]) for item in run_results)
        p95_index = max(0, math.ceil(len(latencies) * 0.95) - 1)
        expected_metrics = sorted(set(runtime_settings.proactive_anomaly_metrics))
        summary = {
            "runs_requested": args.runs,
            "runs_completed": len(run_results),
            "row_count_min": min(item["row_count"] for item in run_results),
            "row_count_max": max(item["row_count"] for item in run_results),
            "dataset_version_present": all(
                item["dataset_version_present"] for item in run_results
            ),
            "source_version_present": all(
                item["source_version_present"] for item in run_results
            ),
            "metric_coverage_complete": all(
                sorted(item["metric_names"]) == expected_metrics for item in run_results
            ),
            "latency_limit_ms": args.max_latency_ms,
            "latency_p95_ms": latencies[p95_index],
            "latency_max_ms": max(latencies),
            "latency_within_limit": max(latencies) <= args.max_latency_ms,
        }
        verified = all(
            (
                summary["runs_completed"] == args.runs,
                summary["row_count_min"] > 0,
                summary["dataset_version_present"],
                summary["source_version_present"],
                summary["metric_coverage_complete"],
                summary["latency_within_limit"],
            )
        )
        report.update(
            {
                "status": "verified_read_only" if verified else "failed",
                "error_code": None if verified else "metric_source_acceptance_failed",
                **(first_metadata or {}),
                "summary": summary,
                "runs": run_results,
            }
        )
        exit_code = 0 if verified else 1
    except MetricSourceError as exc:
        report.update({"status": "failed", "error_code": exc.code})
        exit_code = 1
    except Exception as exc:  # 防御性：不把异常正文写入证据
        report.update({"status": "failed", "error_code": type(exc).__name__})
        exit_code = 1
    finally:
        if adapter is not None and hasattr(adapter, "close"):
            adapter.close()

    _write_report(args.json_output, report)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
