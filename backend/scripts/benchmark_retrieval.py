"""检索压力评估（设计文档 §阶段六：按真实检索压力评估 OpenSearch / Qdrant / Milvus）。

这个脚本回答一个具体问题：**现在的检索实现能扛多大、什么时候必须换向量库？**

做法（全部在 `backend/.tmp/` 下的临时库里跑，**绝不碰开发库**）：

1. 造固定随机种子的合成切片，分别在多个规模下模拟知识库增长；
2. 每种规模下重复查询，测 **p50 / p95 延迟** 与 **并发 QPS**（并发 1/4/8）；
3. 同时测"缓存命中"路径的延迟，用来量化缓存到底省了多少；
4. 打印结论表，并给出按延迟目标外推的容量与选型建议。

诚实边界（结论里也会写）：

- 测的是**存储与检索**路径，Embedding 与 rerank 模型延迟不计入（真实部署里它们通常占大头）；
- 本机是开发机 + SQLite，绝对数字只在"同机对比、看趋势"时有意义；
- FallbackVectorStore 的复杂度是 O(n)：每次查询把候选切片整体读进 Python 再算余弦，
  所以延迟随规模近似线性增长——这正是它适合"演示/万级以内"的原因。

用法::

    python scripts/benchmark_retrieval.py                 # 默认 200/1000/4000
    python scripts/benchmark_retrieval.py --sizes 500,2000
    python scripts/benchmark_retrieval.py --queries 10 --concurrency 1,4
    python scripts/benchmark_retrieval.py --sizes 1000,4000,10000 --json-out .tmp/retrieval.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

TENANT = "bench-tenant"
KB = "bench_kb"
SCHEMA_VERSION = "retrieval-capacity-benchmark-v2"
RANDOM_SEED = 20260913
DEFAULT_JSON_OUT = BACKEND / ".tmp" / "retrieval-capacity-benchmark.json"
QUERIES = [
    "Android 3.2.0 次日留存下降 与哪些版本变更相关",
    "崩溃率上升 是否集中在某个渠道或机型",
    "订单转化率下降 的运营活动重叠情况",
    "用户反馈里提到闪退与卡顿的比例",
    "版本回滚 与 灰度放量 的决策记录",
]


def configure_isolated_environment(embedding_dim: int | None = None) -> None:
    """压测固定为离线 stub + 内存缓存，禁止继承真实模型或外部向量库。"""
    os.environ.update(
        {
            "EMBEDDING_PROVIDER": "stub",
            "EMBEDDING_FAILURE_MODE": "error",
            "RERANKER_ENDPOINT": "",
            "CACHE_BACKEND": "memory",
            "VECTOR_BACKEND": "fallback",
        }
    )
    if embedding_dim is not None:
        os.environ["EMBEDDING_DIM"] = str(embedding_dim)


def build_corpus(session: Any, size: int, dim: int) -> None:
    """插入 size 条合成切片（含真实维度的向量）。"""
    import numpy as np

    from app.config import get_settings
    from app.models.knowledge import DocumentChunk

    rng = np.random.default_rng(RANDOM_SEED)
    document_id = "bench-doc"
    batch: list[dict] = []
    for index in range(size):
        vector = rng.standard_normal(dim).astype("float32")
        norm = float(np.linalg.norm(vector)) or 1.0
        batch.append(
            {
                "tenant_id": TENANT,
                "document_id": document_id,
                "kb_id": "bench-kb-id",
                "kb_code": KB,
                "document_version": "v1",
                "chunk_index": index,
                "content": f"合成切片 {index}：Android 3.2.0 版本在部分机型上出现闪退与留存下降。",
                "token_count": 40,
                "chunk_type": "semantic",
                "security_level": "internal",
                "embedding": (vector / norm).tolist(),
                "embedding_version": get_settings().embedding_version,
            }
        )
        if len(batch) >= 200:
            session.bulk_insert_mappings(DocumentChunk, batch)
            session.commit()
            batch.clear()
    if batch:
        session.bulk_insert_mappings(DocumentChunk, batch)
        session.commit()


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def measure(service: Any, session_factory: Any, *, queries: int, concurrency: int) -> dict:
    """并发跑 queries 次检索，返回延迟与吞吐。

    调用方需保证传入的 service 走**未命中缓存**的路径（TTL=0），否则测到的是缓存速度。
    """
    latencies: list[float] = []
    errors: list[str] = []

    def one(index: int) -> float:
        session = session_factory()
        try:
            query = QUERIES[index % len(QUERIES)]
            started = time.perf_counter()
            service.search(session, query=query, tenant_id=TENANT, kb_codes=[KB], max_security_rank=3)
            return (time.perf_counter() - started) * 1000
        finally:
            session.close()

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for value in pool.map(_safe(one, errors), range(queries)):
            if value is not None:
                latencies.append(value)
    elapsed = time.perf_counter() - started

    return {
        "concurrency": concurrency,
        "requested_queries": queries,
        "queries": len(latencies),
        "p50_ms": round(statistics.median(latencies), 2) if latencies else 0.0,
        "p95_ms": round(percentile(latencies, 0.95), 2),
        "max_ms": round(max(latencies), 2) if latencies else 0.0,
        "qps": round(len(latencies) / elapsed, 2) if elapsed > 0 else 0.0,
        "elapsed_seconds": round(elapsed, 3),
        "error_count": len(errors),
        "errors": errors[:3],
    }


def warmup(service: Any, session_factory: Any, rounds: int = 2) -> None:
    """预热：把首次调用的初始化开销（缓存后端探活、SQLite 页缓存、连接池）排除在统计之外。

    不预热会把"第一次请求"算成 p95，得到"200 条切片要 2.3 秒"这种误导性结论——
    那个数字其实大半是 Redis 探活与建连，而不是检索本身。
    """
    for index in range(rounds):
        session = session_factory()
        try:
            service.search(
                session,
                query=f"预热查询 {index}",
                tenant_id=TENANT,
                kb_codes=[KB],
                max_security_rank=3,
            )
        except Exception as exc:  # 预热失败不影响后续测量
            print(f"  预热失败（忽略）: {type(exc).__name__}: {exc}")
        finally:
            session.close()


def measure_cache(service: Any, session_factory: Any, *, size: int) -> dict:
    """测缓存效果：用一个全新的 query 制造未命中，再用同一 query 命中。

    先跑一轮丢弃（预热缓存后端）：否则第一次调用会把"缓存后端探活/建连"（例如 Redis 连不上要等
    超时）算进"未命中"，得出 200 条切片要 2.2 秒这种假数字。
    """
    warmup_query = f"缓存预热 size={size} 时间={time.time()}"
    session = session_factory()
    try:
        service.search(
            session, query=warmup_query, tenant_id=TENANT, kb_codes=[KB], max_security_rank=3
        )
        service.search(
            session, query=warmup_query, tenant_id=TENANT, kb_codes=[KB], max_security_rank=3
        )
    finally:
        session.close()

    query = f"缓存对照查询 size={size} 时间={time.time()}"
    session = session_factory()
    try:
        started = time.perf_counter()
        service.search(session, query=query, tenant_id=TENANT, kb_codes=[KB], max_security_rank=3)
        cold_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        service.search(session, query=query, tenant_id=TENANT, kb_codes=[KB], max_security_rank=3)
        warm_ms = (time.perf_counter() - started) * 1000
    finally:
        session.close()
    return {
        "size": size,
        "cold_ms": round(cold_ms, 2),
        "warm_ms": round(warm_ms, 3),
        "speedup": round(cold_ms / max(warm_ms, 0.001), 1),
    }


def _safe(fn, errors: list[str]):
    def wrapped(value):
        try:
            return fn(value)
        except Exception as exc:  # 基准脚本不应该因为一次失败整体崩掉
            errors.append(f"{type(exc).__name__}: {exc}")
            return None

    return wrapped


def _positive_int_list(raw: str, *, name: str) -> list[int]:
    try:
        values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    except ValueError as exc:
        raise ValueError(f"{name} must be a comma-separated integer list") from exc
    if not values or any(value < 1 for value in values):
        raise ValueError(f"{name} values must be positive")
    if len(set(values)) != len(values):
        raise ValueError(f"{name} values must be unique")
    return values


def build_evidence(
    *,
    settings: Any,
    sizes: list[int],
    concurrencies: list[int],
    queries: int,
    dim: int,
    table_rows: list[dict[str, Any]],
    cache_rows: list[dict[str, Any]],
    indexing_rows: list[dict[str, Any]],
    target_p95_ms: float | None,
) -> dict[str, Any]:
    error_count = sum(int(row["error_count"]) for row in table_rows)
    target_failures = [
        {
            "size": row["size"],
            "concurrency": row["concurrency"],
            "p95_ms": row["p95_ms"],
        }
        for row in table_rows
        if target_p95_ms is not None and row["p95_ms"] > target_p95_ms
    ]
    execution_passed = error_count == 0
    acceptance_evaluated = target_p95_ms is not None
    acceptance_passed = not target_failures if acceptance_evaluated else None
    passed = execution_passed and acceptance_passed is not False
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_type": "synthetic_capacity",
        "production_ready": False,
        "quality_claim_allowed": False,
        "passed": passed,
        "execution_passed": execution_passed,
        "acceptance_evaluated": acceptance_evaluated,
        "acceptance_passed": acceptance_passed,
        "configuration": {
            "sizes": sizes,
            "queries_per_concurrency": queries,
            "concurrencies": concurrencies,
            "embedding_dimension": dim,
            "embedding_provider": settings.embedding_provider,
            "vector_backend": settings.vector_backend,
            "cache_backend": settings.cache_backend,
            "top_k": settings.retrieval_top_k,
            "random_seed": RANDOM_SEED,
            "target_p95_ms": target_p95_ms,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "logical_cpu_count": os.cpu_count(),
            "sqlite": sqlite3.sqlite_version,
        },
        "corpus_load": indexing_rows,
        "retrieval": table_rows,
        "cache": cache_rows,
        "error_count": error_count,
        "target_failures": target_failures,
        "limitations": [
            "synthetic text and deterministic random vectors do not measure semantic quality",
            "stub query embedding excludes real model and network latency",
            "SQLite fallback performs exact O(n) scanning in one local process",
            "results cannot be extrapolated to pgvector, Qdrant, Milvus, or target production hardware",
            "production readiness still requires target vector store, concurrent traffic, and real holdout validation",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检索压力评估")
    parser.add_argument("--sizes", default="200,1000,4000", help="合成切片规模（逗号分隔）")
    parser.add_argument("--queries", type=int, default=24, help="每种规模下的查询次数")
    parser.add_argument("--concurrency", default="1,4,8", help="并发度（逗号分隔）")
    parser.add_argument(
        "--dim",
        type=int,
        default=512,
        help="合成向量与隔离 stub 查询向量维度（默认 512，与当前 BGE 模型一致）",
    )
    parser.add_argument(
        "--target-p95-ms",
        type=float,
        help="可选工程阈值；任一规模/并发超过该 P95 时退出 1",
    )
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    args = parser.parse_args(argv)

    if args.queries < 1:
        parser.error("--queries must be positive")
    if args.target_p95_ms is not None and args.target_p95_ms <= 0:
        parser.error("--target-p95-ms must be positive")
    try:
        sizes = _positive_int_list(args.sizes, name="--sizes")
        concurrencies = _positive_int_list(args.concurrency, name="--concurrency")
    except ValueError as exc:
        parser.error(str(exc))

    if args.dim < 1:
        parser.error("--dim must be positive")
    configure_isolated_environment(args.dim)
    import app.config as config_module
    from app.db.session import build_engine, build_session_factory
    from app.models import Base
    from app.rag.retrieval import RetrievalService
    from app.rag.vectorstore import reset_vector_store
    from app.services import model_gateway

    config_module.get_settings.cache_clear()
    config_module.settings = config_module.get_settings()
    settings = config_module.settings
    model_gateway._embedder = None
    reset_vector_store()
    dim = args.dim
    if dim != settings.embedding_dim:
        parser.error(
            f"--dim={dim} does not match isolated query embedding dimension "
            f"{settings.embedding_dim}"
        )

    print("=== 环境 ===")
    print(f"  向量后端={settings.vector_backend}  维度={dim}  top_k={settings.retrieval_top_k}")
    print("  向量库实现=FallbackVectorStore（SQLite + Python 精确余弦，O(n)）")
    print(f"  缓存 TTL={settings.retrieval_cache_ttl_seconds}s  缓存后端={settings.cache_backend}")
    print("  数据口径=合成文本 + 固定随机向量；不用于语义质量或生产就绪结论")

    tmp_dir = BACKEND / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    service = RetrievalService()
    # 关掉缓存的副本：压力评估要测"每次都要真的检索"的容量
    uncached = RetrievalService()
    uncached.settings = settings.model_copy(update={"retrieval_cache_ttl_seconds": 0})
    table_rows: list[dict] = []
    cache_rows: list[dict] = []
    indexing_rows: list[dict] = []

    for size in sizes:
        db_path = tmp_dir / f"bench_retrieval_{size}.db"
        if db_path.exists():
            db_path.unlink()
        engine = build_engine(
            settings.model_copy(update={"database_url": f"sqlite:///{db_path.as_posix()}"})
        )
        session_factory = build_session_factory(engine)
        try:
            Base.metadata.create_all(engine)
            session = session_factory()
            try:
                started = time.perf_counter()
                build_corpus(session, size, dim)
                insert_seconds = time.perf_counter() - started
            finally:
                session.close()

            storage_size_bytes = sum(
                candidate.stat().st_size
                for suffix in ("", "-wal", "-shm")
                if (candidate := Path(f"{db_path}{suffix}")).exists()
            )
            indexing = {
                "size": size,
                "insert_seconds": round(insert_seconds, 3),
                "chunks_per_second": round(size / max(insert_seconds, 0.001), 2),
                "storage_size_bytes": storage_size_bytes,
                "includes_embedding_generation": False,
            }
            indexing_rows.append(indexing)
            print(f"\n=== 规模 {size} 条切片（写入 {insert_seconds:.1f}s）===")
            warmup(uncached, session_factory)
            for concurrency in concurrencies:
                row = measure(
                    uncached,
                    session_factory,
                    queries=args.queries,
                    concurrency=concurrency,
                )
                row["size"] = size
                table_rows.append(row)
                print(
                    f"  并发{concurrency:>2}  p50={row['p50_ms']:>8.2f}ms  "
                    f"p95={row['p95_ms']:>8.2f}ms  max={row['max_ms']:>8.2f}ms  "
                    f"QPS={row['qps']:>6.2f}  errors={row['error_count']}"
                )

            cache_row = measure_cache(service, session_factory, size=size)
            cache_rows.append(cache_row)
            print(
                f"  缓存：未命中 {cache_row['cold_ms']:.2f}ms → "
                f"命中 {cache_row['warm_ms']:.2f}ms（{cache_row['speedup']:.0f}x）"
            )
        finally:
            engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                candidate = Path(f"{db_path}{suffix}")
                if candidate.exists():
                    candidate.unlink()

    evidence = build_evidence(
        settings=settings,
        sizes=sizes,
        concurrencies=concurrencies,
        queries=args.queries,
        dim=dim,
        table_rows=table_rows,
        cache_rows=cache_rows,
        indexing_rows=indexing_rows,
        target_p95_ms=args.target_p95_ms,
    )
    output = args.json_out.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("\n=== 结论 ===")
    single = [row for row in table_rows if row["concurrency"] == 1]
    worst = max(single, key=lambda r: r["p95_ms"], default=None)
    if worst:
        print(f"  单并发最差 p95 = {worst['p95_ms']}ms @ {worst['size']} 条切片")
    for row in cache_rows:
        print(f"  缓存把重复检索从 {row['cold_ms']}ms 降到 {row['warm_ms']}ms（{row['speedup']:.0f}x）")
    print(
        "  该结果只证明本机 SQLite fallback 的合成容量趋势；不含真实 Embedding/Reranker，\n"
        "  不能替代 pgvector/Qdrant、目标硬件并发或独立人工 Holdout。"
    )
    print(f"  evidence={output}")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
