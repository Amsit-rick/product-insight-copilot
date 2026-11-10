#!/usr/bin/env python
r"""政策检索 Recall@k / MRR 基准。

默认 ``stub`` 模式保持 CI 零成本；``configured`` 模式使用当前环境配置的
真实 Embedding 与 Reranker，并对静默回退、模型维度和重排覆盖率失败关闭。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND.parent
DB_PATH = BACKEND / ".tmp" / "policy_bench.db"
OUTPUT = PROJECT_ROOT / "docs" / "testing" / "artifacts" / "policy-retrieval-benchmark.json"

GOLDEN_CASES = [
    ("物流停滞超过 48 小时的补偿标准", "物流时效与补偿标准"),
    ("快递三天没有更新怎么办", "物流时效与补偿标准"),
    ("包裹破损了怎么补发", "物流时效与补偿标准"),
    ("少件漏发怎么处理", "物流时效与补偿标准"),
    ("退款需要谁审批", "物流时效与补偿标准"),
    ("满 199 减 30 没生效", "促销优惠与满减规则"),
    ("优惠券为什么不能叠加", "促销优惠与满减规则"),
    ("活动门槛是什么", "促销优惠与满减规则"),
]

SEMANTIC_CASES = [
    ("签收后发现包裹里的商品坏了该怎么处理", "物流时效与补偿标准"),
    ("订单里少发了一件可以申请什么处理", "物流时效与补偿标准"),
    ("已提交退款需要哪个角色通过", "物流时效与补偿标准"),
    ("运费补偿最高能补多少", "物流时效与补偿标准"),
    ("凑单满199为什么还没减30", "促销优惠与满减规则"),
    ("同一订单可以一起用两张优惠券吗", "促销优惠与满减规则"),
    ("活动商品退款后优惠怎么处理", "促销优惠与满减规则"),
    ("哪些商品不计算满减门槛", "促销优惠与满减规则"),
]


def _prepare_env(mode: str, db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["LLM_PROVIDER"] = "stub"
    if mode == "stub":
        os.environ["EMBEDDING_PROVIDER"] = "stub"
        os.environ["RERANKER_ENDPOINT"] = ""
        os.environ["RERANKER_API_KEY"] = ""
        os.environ["EMBEDDING_FAILURE_MODE"] = "fallback"
        os.environ["RERANKER_FAILURE_MODE"] = "fallback"
    os.environ["CACHE_BACKEND"] = "memory"
    os.environ["RETRIEVAL_CACHE_TTL_SECONDS"] = "0"
    os.environ["AUTO_SEED"] = "false"
    os.environ["TASK_RUN_INLINE"] = "true"


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return round(ordered[index], 2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="政策检索 Recall@k 基准")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=0.8, help="Recall@k 下限")
    parser.add_argument("--embedding-mode", choices=("stub", "configured"), default="stub")
    parser.add_argument("--db-path", type=Path, default=DB_PATH)
    parser.add_argument("--json-out", default=str(OUTPUT))
    args = parser.parse_args(argv)

    db_path = args.db_path.resolve()
    _prepare_env(args.embedding_mode, db_path)
    sys.path.insert(0, str(BACKEND))

    import app.config as config_module

    config_module.get_settings.cache_clear()
    config_module.settings = config_module.get_settings()
    settings = config_module.get_settings()
    if args.embedding_mode == "configured":
        problems = []
        if settings.embedding_provider != "openai_compatible":
            problems.append("EMBEDDING_PROVIDER must be openai_compatible")
        if not settings.embedding_base_url or not settings.embedding_api_key:
            problems.append("embedding endpoint and key are required")
        if settings.embedding_failure_mode != "error":
            problems.append("EMBEDDING_FAILURE_MODE must be error")
        if not settings.reranker_endpoint or not settings.reranker_api_key:
            problems.append("reranker endpoint and key are required")
        if settings.reranker_failure_mode != "error":
            problems.append("RERANKER_FAILURE_MODE must be error")
        if problems:
            parser.error("; ".join(problems))

    from app.db.seed import init_db, seed_all
    from app.db.session import SessionLocal, engine
    from app.models.identity import Tenant
    from app.rag.retrieval import RetrievalService
    from app.rag.vectorstore import reset_vector_store
    from app.services import model_gateway

    model_gateway._embedder = None
    reset_vector_store()
    started = time.perf_counter()
    init_db(drop=True)
    seed_all()
    indexing_ms = round((time.perf_counter() - started) * 1000, 2)
    session = SessionLocal()
    service = RetrievalService()
    cases = GOLDEN_CASES + (SEMANTIC_CASES if args.embedding_mode == "configured" else [])
    rows: list[dict] = []
    latencies: list[float] = []
    try:
        tenant = session.query(Tenant).filter(Tenant.code == "demo").one()
        for query, expected_title in cases:
            query_started = time.perf_counter()
            hits = service.search(
                session,
                query=query,
                tenant_id=tenant.id,
                kb_codes=["policy_kb"],
                max_security_rank=3,
                top_k=args.top_k,
            )
            elapsed_ms = round((time.perf_counter() - query_started) * 1000, 2)
            latencies.append(elapsed_ms)
            titles = [str(hit.source_name or "") for hit in hits]
            rank = next(
                (index + 1 for index, title in enumerate(titles) if expected_title in title),
                None,
            )
            reranked = bool(hits) and all("rerank" in hit.scores for hit in hits)
            rows.append(
                {
                    "query": query,
                    "expected_title": expected_title,
                    "rank": rank,
                    "top_titles": titles,
                    "hit": rank is not None,
                    "reranked": reranked,
                    "latency_ms": elapsed_ms,
                }
            )
    finally:
        session.close()
        engine.dispose()

    total = len(rows)
    recall = sum(1 for row in rows if row["hit"]) / total if total else 0.0
    mrr = sum(1.0 / row["rank"] for row in rows if row["rank"]) / total if total else 0.0
    reranked_case_count = sum(1 for row in rows if row["reranked"])
    embedding_stats = service.embedder.cache_info()
    strict_evidence_ok = (
        args.embedding_mode == "stub"
        or (
            embedding_stats["fallbacks"] == 0
            and embedding_stats["failures"] == 0
            and reranked_case_count == total
        )
    )
    passed = recall >= args.threshold and strict_evidence_ok
    payload = {
        # 默认 CI/发布门禁继续产出 v1，避免真实模型扩展改变既有零成本证据契约。
        "schema_version": (
            "policy-retrieval-benchmark-v2"
            if args.embedding_mode == "configured"
            else "policy-retrieval-benchmark-v1"
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": "synthetic_seeded_policy_queries",
        "independent_holdout": False,
        "production_ready": False,
        "embedding_mode": args.embedding_mode,
        "embedding_model": service.embedder.model_name,
        "embedding_dimension": service.embedder.dim,
        "embedding_stats": embedding_stats,
        "reranker_configured": bool(settings.reranker_endpoint),
        "reranked_case_count": reranked_case_count,
        "top_k": args.top_k,
        "case_count": total,
        "recall_at_k": round(recall, 4),
        "mrr": round(mrr, 4),
        "threshold": args.threshold,
        "indexing_latency_ms": indexing_ms,
        "query_latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 2) if latencies else 0.0,
        },
        "strict_evidence_ok": strict_evidence_ok,
        "passed": passed,
        "cases": rows,
    }
    output = Path(args.json_out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("=== 政策检索基准 ===")
    print(
        f"mode={args.embedding_mode} model={service.embedder.model_name} "
        f"dim={service.embedder.dim} reranked={reranked_case_count}/{total}"
    )
    for row in rows:
        mark = "PASS" if row["hit"] else "FAIL"
        rank = row["rank"] if row["rank"] is not None else "-"
        print(
            f"[{mark}] rank={rank} rerank={row['reranked']} "
            f"{row['latency_ms']}ms {row['query']} -> {row['expected_title']}"
        )
        if not row["hit"]:
            print(f"       top{args.top_k}: {row['top_titles']}")
    print(
        f"Recall@{args.top_k}={recall:.2%} MRR={mrr:.4f} "
        f"p95={payload['query_latency_ms']['p95']}ms "
        f"fallbacks={embedding_stats['fallbacks']} 结果={'PASS' if passed else 'FAIL'}"
    )
    print(f"JSON 证据={output}")

    if db_path.exists():
        try:
            db_path.unlink()
        except OSError:
            pass
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
