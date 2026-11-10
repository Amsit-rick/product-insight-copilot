"""Benchmark the local real ONNX embedding and reranker HTTP service.

This measures model-service latency, throughput, and concurrency degradation.
It does not measure retrieval quality and never turns local CPU evidence into a
production readiness claim.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    BACKEND / ".tmp" / "local-integration" / "semantic" / "local-model-concurrency.json"
)
SCHEMA_VERSION = "local-model-concurrency-benchmark-v1"

EMBEDDING_TEXTS = (
    "退款审核通过后通常多久到账？",
    "物流显示签收但用户没有收到商品应该如何处理？",
    "商品破损申请换货需要提供哪些凭证？",
    "活动优惠没有生效时如何核对订单与支付记录？",
    "超过承诺发货时间后是否可以申请补偿？",
    "退货商品寄回后商家多久需要完成验收？",
    "同一订单重复扣款时应该如何核实支付流水？",
    "用户要求仅退款但商品已经签收应该如何判断？",
)

RERANK_DOCUMENTS = (
    "退款审核通过后，款项将按原支付路径退回。",
    "物流签收异常需要先核对签收底单和配送轨迹。",
    "破损换货需要上传外包装、商品和面单照片。",
    "优惠未生效应核对活动门槛、商品范围和下单时间。",
    "发货超时补偿以订单承诺时效和实际揽收时间为准。",
    "退货验收完成后进入退款处理流程。",
    "重复扣款应比对支付单号、交易时间和金额。",
    "已签收订单的仅退款申请需要结合商品状态和责任判定。",
)


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def positive_int_list(raw: str, *, name: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError(f"{name} must be a comma-separated integer list") from exc
    if not values or any(value < 1 for value in values):
        raise ValueError(f"{name} values must be positive")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} values must be unique")
    return values


def measure(
    call: Callable[[int], None],
    *,
    operation: str,
    requests: int,
    concurrency: int,
    workload_size: int,
) -> dict[str, Any]:
    latencies: list[float] = []
    errors: list[str] = []

    def one(index: int) -> float | None:
        started = time.perf_counter()
        try:
            call(index)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            return None
        return (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for latency in pool.map(one, range(requests)):
            if latency is not None:
                latencies.append(latency)
    elapsed = time.perf_counter() - started
    return {
        "operation": operation,
        "workload_size": workload_size,
        "concurrency": concurrency,
        "requested": requests,
        "completed": len(latencies),
        "error_count": len(errors),
        "errors": errors[:3],
        "p50_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
        "p95_ms": round(percentile(latencies, 0.95), 2),
        "max_ms": round(max(latencies), 2) if latencies else 0.0,
        "qps": round(len(latencies) / elapsed, 3) if elapsed else 0.0,
        "elapsed_seconds": round(elapsed, 3),
    }


def measure_mixed(
    embed_call: Callable[[int], None],
    rerank_call: Callable[[int], None],
    *,
    requests: int,
    concurrency: int,
    rerank_documents: int,
) -> dict[str, Any]:
    samples: dict[str, list[float]] = {"embedding": [], "reranker": []}
    errors: list[str] = []

    def one(index: int) -> tuple[str, float] | None:
        operation = "embedding" if index % 2 == 0 else "reranker"
        started = time.perf_counter()
        try:
            (embed_call if operation == "embedding" else rerank_call)(index)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            return None
        return operation, (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for result in pool.map(one, range(requests)):
            if result is not None:
                operation, latency = result
                samples[operation].append(latency)
    elapsed = time.perf_counter() - started
    completed = sum(len(values) for values in samples.values())
    all_latencies = [value for values in samples.values() for value in values]
    return {
        "operation": "mixed",
        "workload_size": rerank_documents,
        "concurrency": concurrency,
        "requested": requests,
        "completed": completed,
        "error_count": len(errors),
        "errors": errors[:3],
        "p50_ms": round(statistics.median(all_latencies), 2) if all_latencies else 0.0,
        "p95_ms": round(percentile(all_latencies, 0.95), 2),
        "max_ms": round(max(all_latencies), 2) if all_latencies else 0.0,
        "qps": round(completed / elapsed, 3) if elapsed else 0.0,
        "elapsed_seconds": round(elapsed, 3),
        "components": {
            name: {
                "completed": len(values),
                "p50_ms": round(statistics.median(values), 2) if values else 0.0,
                "p95_ms": round(percentile(values, 0.95), 2),
                "max_ms": round(max(values), 2) if values else 0.0,
            }
            for name, values in samples.items()
        },
    }


def build_report(
    *,
    health: dict[str, Any],
    rows: list[dict[str, Any]],
    requests: int,
    concurrencies: list[int],
    embedding_batches: list[int],
    rerank_documents: int,
) -> dict[str, Any]:
    error_count = sum(int(row["error_count"]) for row in rows)
    identity_ok = (
        health.get("status") == "ok"
        and bool(health.get("embedding_model"))
        and bool(health.get("reranker_model"))
    )
    execution_passed = identity_ok and error_count == 0
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "local_real_onnx_model_service",
        "production_ready": False,
        "quality_claim_allowed": False,
        "passed": execution_passed,
        "execution_passed": execution_passed,
        "acceptance_evaluated": False,
        "acceptance_passed": None,
        "models": {
            "embedding": health.get("embedding_model"),
            "reranker": health.get("reranker_model"),
            "device": health.get("device"),
            "model_threads": health.get("model_threads"),
            "inference_policy": health.get("inference_policy"),
        },
        "configuration": {
            "requests_per_case": requests,
            "concurrencies": concurrencies,
            "embedding_batch_sizes": embedding_batches,
            "rerank_document_count": rerank_documents,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "logical_cpu_count": os.cpu_count(),
        },
        "results": rows,
        "error_count": error_count,
        "limitations": [
            "single local FastAPI process on CPU ONNX runtime",
            "no business latency or throughput threshold was supplied",
            "direct model endpoints exclude vector search, database, and agent orchestration",
            "fixed synthetic requests do not measure retrieval quality",
            "target deployment hardware and multi-instance scaling remain unverified",
        ],
    }


def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    base_url = f"http://127.0.0.1:{args.model_port}"
    headers = {"Authorization": f"Bearer {args.api_key}"}
    limits = httpx.Limits(
        max_connections=max(args.concurrencies) + 2,
        max_keepalive_connections=max(args.concurrencies) + 2,
    )
    rows: list[dict[str, Any]] = []
    with httpx.Client(
        base_url=base_url,
        headers=headers,
        timeout=args.timeout_seconds,
        limits=limits,
        trust_env=False,
    ) as client:
        health = client.get("/health").raise_for_status().json()
        if health.get("status") != "ok":
            raise RuntimeError("local model service is not ready")

        def embed_call(batch_size: int) -> Callable[[int], None]:
            def call(index: int) -> None:
                texts = [
                    f"{EMBEDDING_TEXTS[item % len(EMBEDDING_TEXTS)]} case={index}-{item}"
                    for item in range(batch_size)
                ]
                payload = client.post(
                    "/v1/embeddings",
                    json={"model": health["embedding_model"], "input": texts},
                ).raise_for_status().json()
                data = payload.get("data") or []
                if len(data) != batch_size or any(
                    len(item.get("embedding") or []) != args.embedding_dimension for item in data
                ):
                    raise ValueError("embedding response shape mismatch")

            return call

        def rerank_call(index: int) -> None:
            payload = client.post(
                "/rerank",
                json={
                    "query": f"退款多久到账 case={index}",
                    "documents": list(RERANK_DOCUMENTS[: args.rerank_documents]),
                    "top_n": args.rerank_documents,
                },
            ).raise_for_status().json()
            results = payload.get("results") or []
            if len(results) != args.rerank_documents:
                raise ValueError("reranker response shape mismatch")

        # First-call model/session initialization must not pollute P95.
        embed_call(max(args.embedding_batches))(0)
        rerank_call(0)

        for batch_size in args.embedding_batches:
            for concurrency in args.concurrencies:
                rows.append(
                    measure(
                        embed_call(batch_size),
                        operation="embedding",
                        requests=args.requests,
                        concurrency=concurrency,
                        workload_size=batch_size,
                    )
                )
        for concurrency in args.concurrencies:
            rows.append(
                measure(
                    rerank_call,
                    operation="reranker",
                    requests=args.requests,
                    concurrency=concurrency,
                    workload_size=args.rerank_documents,
                )
            )
        mixed_embed = embed_call(1)
        for concurrency in args.concurrencies:
            rows.append(
                measure_mixed(
                    mixed_embed,
                    rerank_call,
                    requests=args.requests,
                    concurrency=concurrency,
                    rerank_documents=args.rerank_documents,
                )
            )

    return build_report(
        health=health,
        rows=rows,
        requests=args.requests,
        concurrencies=args.concurrencies,
        embedding_batches=args.embedding_batches,
        rerank_documents=args.rerank_documents,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-port", type=int, default=8093)
    parser.add_argument("--api-key", default="local-model-key")
    parser.add_argument("--requests", type=int, default=12)
    parser.add_argument("--concurrency", default="1,2,4")
    parser.add_argument("--embedding-batches", default="1,8")
    parser.add_argument("--embedding-dimension", type=int, default=512)
    parser.add_argument("--rerank-documents", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        args.concurrencies = positive_int_list(args.concurrency, name="--concurrency")
        args.embedding_batches = positive_int_list(
            args.embedding_batches,
            name="--embedding-batches",
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.requests < 1 or args.embedding_dimension < 1:
        parser.error("--requests and --embedding-dimension must be positive")
    if not 1 <= args.rerank_documents <= len(RERANK_DOCUMENTS):
        parser.error(f"--rerank-documents must be between 1 and {len(RERANK_DOCUMENTS)}")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")

    try:
        report = benchmark(args)
    except Exception as exc:
        print(f"Benchmark failed: {type(exc).__name__}: {exc}")
        return 2
    output = args.json_out.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for row in report["results"]:
        print(
            f"{row['operation']} size={row['workload_size']} concurrency={row['concurrency']} "
            f"p50={row['p50_ms']:.2f}ms p95={row['p95_ms']:.2f}ms "
            f"qps={row['qps']:.3f} errors={row['error_count']}"
        )
    print(f"evidence={output}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
