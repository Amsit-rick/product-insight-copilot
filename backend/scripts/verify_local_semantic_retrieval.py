"""Verify the local real embedding/reranker service and retrieval benchmark."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_STATE = BACKEND / ".tmp" / "local-integration" / "semantic"


def _check(checks: list[dict], name: str, passed: bool, **evidence) -> None:
    checks.append({"name": name, "passed": bool(passed), **evidence})
    print(f"{'PASS' if passed else 'FAIL'} {name}", flush=True)


def verify(args: argparse.Namespace) -> dict:
    checks: list[dict] = []
    report = {
        "schema_version": "local-semantic-retrieval-verification-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "local_real_models_synthetic_policy_dataset",
        "production_ready": False,
        "checks": checks,
    }
    base_url = f"http://127.0.0.1:{args.model_port}"
    headers = {"Authorization": f"Bearer {args.api_key}"}
    with httpx.Client(timeout=120, trust_env=False) as client:
        started = time.perf_counter()
        health = client.get(base_url + "/health").raise_for_status().json()
        report["models"] = {
            "embedding": health.get("embedding_model"),
            "reranker": health.get("reranker_model"),
            "device": health.get("device"),
        }
        _check(
            checks,
            "model_identity",
            health.get("status") == "ok"
            and health.get("embedding_model") == args.embedding_model
            and health.get("reranker_model") == args.reranker_model,
            device=health.get("device"),
        )
        embedding = client.post(
            base_url + "/v1/embeddings",
            headers=headers,
            json={"model": args.embedding_model, "input": ["退款多久到账"]},
        ).raise_for_status().json()
        vector = embedding["data"][0]["embedding"]
        _check(
            checks,
            "embedding_dimension",
            len(vector) == args.embedding_dimension,
            dimension=len(vector),
        )
        rerank = client.post(
            base_url + "/rerank",
            headers=headers,
            json={
                "query": "退款多久到账",
                "documents": ["物流签收后可以评价", "退款审核通过后原路退回"],
                "top_n": 2,
            },
        ).raise_for_status().json()
        results = rerank.get("results") or []
        _check(
            checks,
            "reranker_orders_relevant_document_first",
            bool(results) and results[0].get("index") == 1,
        )
        report["probe_latency_ms"] = round((time.perf_counter() - started) * 1000, 2)

    args.state_dir.mkdir(parents=True, exist_ok=True)
    benchmark_path = args.state_dir / "semantic-retrieval-benchmark.json"
    db_path = args.state_dir / "policy-benchmark.db"
    env = dict(os.environ)
    env.update(
        {
            "EMBEDDING_PROVIDER": "openai_compatible",
            "EMBEDDING_BASE_URL": base_url + "/v1",
            "EMBEDDING_API_KEY": args.api_key,
            "EMBEDDING_MODEL": args.embedding_model,
            "EMBEDDING_DIM": str(args.embedding_dimension),
            "EMBEDDING_VERSION": "fastembed-bge-small-zh-v1.5-v1",
            "EMBEDDING_FAILURE_MODE": "error",
            "RERANKER_ENDPOINT": base_url + "/rerank",
            "RERANKER_API_KEY": args.api_key,
            "RERANKER_FAILURE_MODE": "error",
        }
    )
    command = [
        sys.executable,
        str(BACKEND / "scripts" / "benchmark_policy_retrieval.py"),
        "--embedding-mode",
        "configured",
        "--db-path",
        str(db_path),
        "--json-out",
        str(benchmark_path),
    ]
    completed = subprocess.run(command, cwd=BACKEND, env=env, check=False)
    _check(
        checks,
        "configured_policy_benchmark_exit",
        completed.returncode == 0,
        returncode=completed.returncode,
    )
    benchmark = (
        json.loads(benchmark_path.read_text(encoding="utf-8"))
        if benchmark_path.exists()
        else {}
    )
    report["benchmark"] = benchmark
    _check(
        checks,
        "benchmark_has_zero_fallbacks",
        benchmark.get("embedding_stats", {}).get("fallbacks") == 0
        and benchmark.get("embedding_stats", {}).get("failures") == 0,
    )
    _check(
        checks,
        "all_cases_used_reranker",
        bool(benchmark.get("case_count"))
        and benchmark.get("reranked_case_count") == benchmark.get("case_count"),
        reranked=benchmark.get("reranked_case_count"),
        cases=benchmark.get("case_count"),
    )
    _check(
        checks,
        "recall_gate",
        bool(benchmark.get("passed")),
        recall_at_k=benchmark.get("recall_at_k"),
        mrr=benchmark.get("mrr"),
    )
    report["passed"] = all(item["passed"] for item in checks)
    report["passed_count"] = sum(1 for item in checks if item["passed"])
    report["check_count"] = len(checks)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-port", type=int, default=8093)
    parser.add_argument("--api-key", default="local-model-key")
    parser.add_argument("--embedding-model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--reranker-model", default="BAAI/bge-reranker-v2-m3-int8")
    parser.add_argument("--embedding-dimension", type=int, default=512)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    report = verify(args)
    destination = args.state_dir / "semantic-retrieval-verification.json"
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Result: {report['passed_count']}/{report['check_count']}; report {destination}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
