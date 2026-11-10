"""通过真实 HTTP 验证 Postgres + Redis + 队列 + 多 worker 工单执行形态。

默认只允许 stub LLM/Embedding，避免一次负载验收意外产生模型费用。脚本不会创建或
销毁容器；应由调用方在一次性 Compose project 中启动环境，再把 ``--base-url`` 指向它。
任一生产形态硬门槛或性能门槛失败，进程都会以非零状态退出。
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx


ORDER_IDS = (
    "TB202609241806",
    "TB202609241742",
    "TB202609241501",
    "TB202609240992",
    "TB202609240821",
    "TB202609240655",
)
SUCCESS_TASK_STATUSES = {"completed", "waiting_human"}


def percentile(values: list[float], ratio: float) -> float:
    """返回 nearest-rank 分位数；空列表返回 0，便于生成失败报告。"""
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(ratio * len(ordered)) - 1))
    return ordered[index]


def validate_runtime_health(payload: dict[str, Any], *, allow_real_models: bool = False) -> list[str]:
    """验证环境确实是共享生产形态，而不是悄悄降级的本地形态。"""
    errors: list[str] = []
    if payload.get("database") != "postgres":
        errors.append("database 必须是 postgres")

    cache = payload.get("cache") or {}
    if cache.get("backend") != "redis" or cache.get("healthy") is not True:
        errors.append("cache 必须是健康的 redis，且不能发生降级")
    if cache.get("degraded_reason"):
        errors.append(f"cache 已降级：{cache.get('degraded_reason')}")

    execution = payload.get("execution") or {}
    if execution.get("backend") != "queue":
        errors.append("execution backend 必须是 queue")
    checkpointer = execution.get("checkpointer") or {}
    if checkpointer.get("backend") != "postgres" or checkpointer.get("shared") is not True:
        errors.append("checkpointer 必须是 shared=true 的 postgres")

    if not allow_real_models:
        for name in ("llm", "embedding"):
            provider = str((payload.get(name) or {}).get("provider") or "").lower()
            if provider != "stub":
                errors.append(f"{name} provider 必须是 stub（可用 --allow-real-models 显式覆盖）")
    return errors


def summarize_run(
    submissions: list[dict[str, Any]],
    completions: list[dict[str, Any]],
    *,
    queue_items: list[dict[str, Any]],
    elapsed_seconds: float,
    final_health: dict[str, Any],
) -> dict[str, Any]:
    submit_ok = [item for item in submissions if item.get("ok")]
    completion_ok = [item for item in completions if item.get("ok")]
    failed_ids = {
        str(item.get("task_id") or item.get("ticket_id") or f"submit:{index}")
        for index, item in enumerate(submissions)
        if not item.get("ok")
    }
    failed_ids.update(
        str(item.get("task_id") or item.get("ticket_id") or f"complete:{index}")
        for index, item in enumerate(completions)
        if not item.get("ok")
    )
    requested = len(submissions)
    elapsed = max(float(elapsed_seconds), 0.000001)
    workers = sorted(
        {
            str(item["claimed_by"])
            for item in queue_items
            if item.get("claimed_by")
        }
    )
    queue_final = dict(((final_health.get("execution") or {}).get("queue") or {}))
    return {
        "latency_ms": {
            "submit_p50": percentile([item["latency_ms"] for item in submissions], 0.50),
            "submit_p95": percentile([item["latency_ms"] for item in submissions], 0.95),
            "submit_p99": percentile([item["latency_ms"] for item in submissions], 0.99),
            "end_to_end_p50": percentile([item["latency_ms"] for item in completions], 0.50),
            "end_to_end_p95": percentile([item["latency_ms"] for item in completions], 0.95),
            "end_to_end_p99": percentile([item["latency_ms"] for item in completions], 0.99),
        },
        "counts": {
            "requested": requested,
            "submitted": len(submit_ok),
            "completed": len(completion_ok),
            "errors": len(failed_ids),
        },
        "error_rate": len(failed_ids) / requested if requested else 1.0,
        "throughput_per_second": len(completion_ok) / elapsed,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "workers": workers,
        "queue_final": {
            key: int(queue_final.get(key, 0) or 0)
            for key in ("pending", "claimed", "done", "dead")
        },
        "submission_errors": [item for item in submissions if not item.get("ok")],
        "completion_errors": [item for item in completions if not item.get("ok")],
    }


def evaluate_acceptance(
    report: dict[str, Any],
    *,
    target_submit_p95_ms: float,
    target_end_to_end_p95_ms: float,
    max_error_rate: float,
    expected_workers: int,
) -> list[dict[str, Any]]:
    latency = report.get("latency_ms") or {}
    queue = report.get("queue_final") or {}
    return [
        {
            "name": "提交 P95",
            "passed": float(latency.get("submit_p95", math.inf)) <= target_submit_p95_ms,
            "actual": latency.get("submit_p95"),
            "target": f"<= {target_submit_p95_ms} ms",
        },
        {
            "name": "端到端 P95",
            "passed": float(latency.get("end_to_end_p95", math.inf)) <= target_end_to_end_p95_ms,
            "actual": latency.get("end_to_end_p95"),
            "target": f"<= {target_end_to_end_p95_ms} ms",
        },
        {
            "name": "错误率",
            "passed": float(report.get("error_rate", 1.0)) <= max_error_rate,
            "actual": report.get("error_rate"),
            "target": f"<= {max_error_rate:.2%}",
        },
        {
            "name": "独立 worker 数",
            "passed": len(report.get("workers") or []) >= expected_workers,
            "actual": len(report.get("workers") or []),
            "target": f">= {expected_workers}",
        },
        {
            "name": "队列最终清空",
            "passed": all(int(queue.get(key, 0) or 0) == 0 for key in ("pending", "claimed", "dead")),
            "actual": {key: queue.get(key, 0) for key in ("pending", "claimed", "dead")},
            "target": "pending=0, claimed=0, dead=0",
        },
    ]


def exit_code(checks: list[dict[str, Any]]) -> int:
    return 0 if checks and all(item.get("passed") for item in checks) else 1


def _request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = client.request(method, path, headers=headers, json=json_body)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError(f"{method} {path} 返回的不是 JSON object")
    return payload


def _login(client: httpx.Client, username: str, password: str) -> dict[str, str]:
    payload = _request_json(
        client,
        "POST",
        "/api/v1/auth/login",
        json_body={"username": username, "password": password},
    )
    return {"Authorization": f"Bearer {payload['access_token']}"}


def _submit_one(
    base_url: str,
    headers: dict[str, str],
    *,
    index: int,
    run_id: str,
    timeout: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    ticket_id: str | None = None
    task_id: str | None = None
    try:
        order_id = ORDER_IDS[index % len(ORDER_IDS)]
        external_id = f"LOAD-{run_id}-{index:04d}"
        with httpx.Client(base_url=base_url, timeout=timeout) as client:
            ticket = _request_json(
                client,
                "POST",
                "/api/v1/tickets",
                headers=headers,
                json_body={
                    "subject": f"生产队列负载验证 #{index + 1}",
                    "content": "客户反馈订单售后问题，请核验订单、物流和政策后生成处理建议。",
                    "external_id": external_id,
                    "idempotency_key": external_id,
                    "order_id": order_id,
                    "customer_id": f"LOAD-CUSTOMER-{index % 20:02d}",
                    "channel": "api",
                    "priority": "medium",
                    "data_origin": "test",
                },
            )
            ticket_id = str(ticket["id"])
            analysis = _request_json(
                client,
                "POST",
                f"/api/v1/tickets/{ticket_id}/analyze",
                headers=headers,
            )
            task_id = str(analysis["run_id"])
        return {
            "ok": True,
            "ticket_id": ticket_id,
            "task_id": task_id,
            "started_at_mono": started,
            "latency_ms": (time.perf_counter() - started) * 1000,
        }
    except Exception as exc:
        return {
            "ok": False,
            "ticket_id": ticket_id,
            "task_id": task_id,
            "started_at_mono": started,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _poll_completions(
    client: httpx.Client,
    headers: dict[str, str],
    submissions: list[dict[str, Any]],
    *,
    deadline: float,
    poll_interval: float,
) -> list[dict[str, Any]]:
    pending = {
        str(item["task_id"]): {
            "ticket_id": str(item["ticket_id"]),
            "submitted_at": float(
                item.get("started_at_mono")
                or (time.perf_counter() - float(item["latency_ms"]) / 1000)
            ),
        }
        for item in submissions
        if item.get("ok") and item.get("task_id") and item.get("ticket_id")
    }
    results: list[dict[str, Any]] = []
    while pending and time.monotonic() < deadline:
        for task_id, context in list(pending.items()):
            try:
                task = _request_json(
                    client,
                    "GET",
                    f"/api/v1/tasks/{task_id}",
                    headers=headers,
                )
                task_status = str(task.get("status") or "")
                if task_status in {"failed", "cancelled"}:
                    results.append(
                        {
                            "ok": False,
                            "ticket_id": context["ticket_id"],
                            "task_id": task_id,
                            "latency_ms": (time.perf_counter() - context["submitted_at"]) * 1000,
                            "error": f"task {task_status}: {task.get('error') or {}}",
                        }
                    )
                    pending.pop(task_id)
                    continue
                detail = _request_json(
                    client,
                    "GET",
                    f"/api/v1/tickets/{context['ticket_id']}",
                    headers=headers,
                )
                ticket = detail.get("ticket") or {}
                analysis = detail.get("analysis") or {}
                steps = analysis.get("steps") or []
                step_failed = any(step.get("status") == "failed" for step in steps)
                ticket_status = str(ticket.get("status") or "")
                recommendation = detail.get("recommendation")
                complete = task_status in SUCCESS_TASK_STATUSES or bool(recommendation) or ticket_status in {
                    "ready_to_reply",
                    "waiting_review",
                    "resolved",
                    "closed",
                    "escalated",
                }
                if step_failed:
                    results.append(
                        {
                            "ok": False,
                            "ticket_id": context["ticket_id"],
                            "task_id": task_id,
                            "latency_ms": (time.perf_counter() - context["submitted_at"]) * 1000,
                            "error": "agent step failed",
                        }
                    )
                    pending.pop(task_id)
                elif complete:
                    results.append(
                        {
                            "ok": True,
                            "ticket_id": context["ticket_id"],
                            "task_id": task_id,
                            "latency_ms": (time.perf_counter() - context["submitted_at"]) * 1000,
                            "ticket_status": ticket_status,
                        }
                    )
                    pending.pop(task_id)
            except Exception:
                # API 短暂不可用时保留待轮询项，直到总超时再记失败。
                continue
        if pending:
            time.sleep(poll_interval)

    for task_id, context in pending.items():
        results.append(
            {
                "ok": False,
                "ticket_id": context["ticket_id"],
                "task_id": task_id,
                "latency_ms": (time.perf_counter() - context["submitted_at"]) * 1000,
                "error": "timeout waiting for terminal ticket result",
            }
        )
    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18200")
    parser.add_argument("--count", type=int, default=24)
    parser.add_argument("--submit-concurrency", type=int, default=8)
    parser.add_argument("--target-submit-p95", type=float, default=3_000, metavar="MS")
    parser.add_argument("--target-end-to-end-p95", type=float, default=30_000, metavar="MS")
    parser.add_argument("--max-error-rate", type=float, default=0.0)
    parser.add_argument("--expected-workers", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=180.0, help="整轮完成等待秒数")
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--allow-real-models", action="store_true")
    return parser.parse_args(argv)


def _print_report(report: dict[str, Any], checks: list[dict[str, Any]]) -> None:
    latency = report["latency_ms"]
    print("\n=== 工单生产队列负载报告 ===")
    print(f"请求/提交/完成/错误: {report['counts']}")
    print(f"提交 P95: {latency['submit_p95']:.1f} ms")
    print(f"端到端 P95: {latency['end_to_end_p95']:.1f} ms")
    print(f"吞吐: {report['throughput_per_second']:.2f} 单/秒")
    print(f"错误率: {report['error_rate']:.2%}")
    print(f"实际 worker: {', '.join(report['workers']) or '无'}")
    print(f"最终队列: {report['queue_final']}")
    print("\n硬门槛:")
    for item in checks:
        print(f"  {'PASS' if item['passed'] else 'FAIL'}  {item['name']}: {item['actual']} ({item['target']})")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.count < 1 or args.submit_concurrency < 1 or args.expected_workers < 1:
        raise SystemExit("count / submit-concurrency / expected-workers 必须 >= 1")
    started = time.perf_counter()
    with httpx.Client(base_url=args.base_url, timeout=args.request_timeout) as client:
        health = _request_json(client, "GET", "/health")
        health_errors = validate_runtime_health(health, allow_real_models=args.allow_real_models)
        if health_errors:
            print("运行时硬门槛未通过：", file=sys.stderr)
            for error in health_errors:
                print(f"  - {error}", file=sys.stderr)
            return 2
        ops_headers = _login(client, "ops", "ops123")
        admin_headers = _login(client, "admin", "admin123")

    run_id = f"{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.submit_concurrency) as pool:
        futures = [
            pool.submit(
                _submit_one,
                args.base_url,
                ops_headers,
                index=index,
                run_id=run_id,
                timeout=args.request_timeout,
            )
            for index in range(args.count)
        ]
        submissions = [future.result() for future in futures]

    with httpx.Client(base_url=args.base_url, timeout=args.request_timeout) as client:
        completions = _poll_completions(
            client,
            ops_headers,
            submissions,
            deadline=time.monotonic() + args.timeout,
            poll_interval=args.poll_interval,
        )
        task_ids = {str(item["task_id"]) for item in submissions if item.get("task_id")}
        queue_payload = _request_json(
            client,
            "GET",
            "/api/v1/queue/items?status=done&limit=200",
            headers=admin_headers,
        )
        queue_items = [
            item for item in (queue_payload.get("items") or []) if str(item.get("task_id")) in task_ids
        ]
        final_health = _request_json(client, "GET", "/health")

    report = summarize_run(
        submissions,
        completions,
        queue_items=queue_items,
        elapsed_seconds=time.perf_counter() - started,
        final_health=final_health,
    )
    report["run_id"] = run_id
    report["runtime_health"] = final_health
    checks = evaluate_acceptance(
        report,
        target_submit_p95_ms=args.target_submit_p95,
        target_end_to_end_p95_ms=args.target_end_to_end_p95,
        max_error_rate=args.max_error_rate,
        expected_workers=args.expected_workers,
    )
    report["acceptance"] = checks
    _print_report(report, checks)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 报告: {args.json_out}")
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
