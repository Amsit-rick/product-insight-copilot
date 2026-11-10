"""Validate and benchmark an independently annotated real-document holdout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_STATE = BACKEND / ".tmp" / "document-holdout" / "evaluation"
HUMAN_METHODS = {
    "double_human_review",
    "expert_review",
    "adjudicated_human_review",
}


def validate_holdout(
    manifest: dict[str, Any],
    dataset: dict[str, Any],
    *,
    min_cases: int,
) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema_version") != "document-corpus-manifest-v1":
        errors.append("invalid manifest schema")
    if dataset.get("schema_version") != "document-retrieval-holdout-v1":
        errors.append("invalid dataset schema")
    if dataset.get("corpus_fingerprint") != manifest.get("corpus_fingerprint"):
        errors.append("corpus fingerprint mismatch")

    provenance = dataset.get("provenance") or {}
    if provenance.get("annotation_method") not in HUMAN_METHODS:
        errors.append("annotation method must be independent human review")
    if provenance.get("independent_from_system") is not True:
        errors.append("dataset must be independent from the system")
    if provenance.get("deidentified") is not True:
        errors.append("deidentification must be confirmed")
    if int(provenance.get("reviewer_count") or 0) < 2:
        errors.append("at least two reviewers are required")

    cases = dataset.get("cases")
    if not isinstance(cases, list):
        errors.append("cases must be a list")
        return errors
    if len(cases) < min_cases:
        errors.append(f"at least {min_cases} cases are required")

    unique_files = {
        item["source_name"]: item["sha256"]
        for item in manifest.get("files", [])
        if not item.get("duplicate_of") and item.get("parse_status") == "parsed"
    }
    ids: set[str] = set()
    questions: set[str] = set()
    missing_or_duplicate_ids = 0
    missing_questions = 0
    duplicate_questions = 0
    unknown_documents = 0
    unreviewed_cases = 0
    missing_evidence_locators = 0
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            errors.append(f"case {index} must be an object")
            continue
        case_id = str(case.get("case_id") or "").strip()
        question = str(case.get("question") or "").strip()
        source_name = str(case.get("expected_source_name") or "").strip()
        digest = str(case.get("expected_document_sha256") or "").strip()
        status = str(case.get("annotation_status") or "").strip()
        section = str(case.get("expected_section_title") or "").strip()
        excerpt = str(case.get("expected_evidence_excerpt") or "").strip()
        if not case_id or case_id in ids:
            missing_or_duplicate_ids += 1
        ids.add(case_id)
        if len(question) < 4:
            missing_questions += 1
        elif question in questions:
            duplicate_questions += 1
        if question:
            questions.add(question)
        if unique_files.get(source_name) != digest:
            unknown_documents += 1
        if status not in {"human_reviewed", "adjudicated"}:
            unreviewed_cases += 1
        if not section and len(excerpt) < 12:
            missing_evidence_locators += 1
    if missing_or_duplicate_ids:
        errors.append(f"{missing_or_duplicate_ids} cases have missing or duplicate case_id")
    if missing_questions:
        errors.append(f"{missing_questions} cases have missing questions")
    if duplicate_questions:
        errors.append(f"{duplicate_questions} cases have duplicate questions")
    if unknown_documents:
        errors.append(f"{unknown_documents} cases reference unknown or drifted documents")
    if unreviewed_cases:
        errors.append(f"{unreviewed_cases} cases are not human reviewed")
    if missing_evidence_locators:
        errors.append(
            f"{missing_evidence_locators} cases lack a section or evidence excerpt locator"
        )
    return errors


def validate_source_files(manifest: dict[str, Any], source_dir: Path) -> list[str]:
    errors: list[str] = []
    root = source_dir.resolve()
    if not root.is_dir():
        return ["source directory does not exist"]
    for item in manifest.get("files", []):
        source_name = str(item.get("source_name") or "")
        expected = str(item.get("sha256") or "")
        path = (root / source_name).resolve()
        if not path.is_relative_to(root):
            errors.append(f"source path escapes source directory: {source_name}")
            continue
        if not path.is_file():
            errors.append(f"source document is missing: {source_name}")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            errors.append(f"source document drifted: {source_name}")
    return errors


def _percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(ratio * (len(ordered) - 1))))
    return round(ordered[index], 2)


def _matches_expected_hit(hit: Any, case: dict[str, Any]) -> bool:
    if hit.source_name != case["expected_source_name"]:
        return False
    expected_section = str(case.get("expected_section_title") or "").strip()
    if expected_section and (hit.section_title or "").strip() != expected_section:
        return False
    expected_excerpt = str(case.get("expected_evidence_excerpt") or "").strip()
    if expected_excerpt and expected_excerpt not in (hit.content or ""):
        return False
    return True


def _prepare_env(args: argparse.Namespace) -> Path:
    state = args.state_dir.resolve()
    state.mkdir(parents=True, exist_ok=True)
    db_path = state / "document-holdout.db"
    if db_path.exists():
        db_path.unlink()
    model_base = f"http://127.0.0.1:{args.model_port}"
    os.environ.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path.as_posix()}",
            "AUTO_INIT_DB": "false",
            "AUTO_SEED": "false",
            "CACHE_BACKEND": "memory",
            "RETRIEVAL_CACHE_TTL_SECONDS": "0",
            "STORAGE_BACKEND": "local",
            "STORAGE_LOCAL_DIR": str(state / "objects"),
            "VECTOR_BACKEND": "fallback",
            "LLM_PROVIDER": "stub",
            "EMBEDDING_PROVIDER": "openai_compatible",
            "EMBEDDING_BASE_URL": model_base + "/v1",
            "EMBEDDING_API_KEY": args.api_key,
            "EMBEDDING_MODEL": args.embedding_model,
            "EMBEDDING_DIM": str(args.embedding_dimension),
            "EMBEDDING_VERSION": "document-holdout-bge-small-zh-v1.5-v1",
            "EMBEDDING_FAILURE_MODE": "error",
            "RERANKER_ENDPOINT": model_base + "/rerank",
            "RERANKER_API_KEY": args.api_key,
            "RERANKER_FAILURE_MODE": "error",
        }
    )
    return db_path


def evaluate(
    args: argparse.Namespace,
    manifest: dict[str, Any],
    dataset: dict[str, Any],
) -> dict[str, Any]:
    _prepare_env(args)
    sys.path.insert(0, str(BACKEND))

    import app.config as config_module

    config_module.get_settings.cache_clear()
    config_module.settings = config_module.get_settings()

    from app.db.seed import init_db, seed_all
    from app.db.session import SessionLocal, engine
    from app.models.identity import Tenant, User
    from app.rag.retrieval import RetrievalService
    from app.rag.vectorstore import reset_vector_store
    from app.services import model_gateway
    from app.services.document_service import DocumentService

    model_gateway._embedder = None
    reset_vector_store()
    init_db(drop=True)
    seed_all(with_demo=False, with_eval_cases=False, with_metrics=False)
    session = SessionLocal()
    service = RetrievalService()
    source_dir = args.source_dir.resolve()
    ingest_started = time.perf_counter()
    try:
        tenant = session.query(Tenant).filter(Tenant.code == "demo").one()
        user = session.query(User).filter(User.tenant_id == tenant.id).first()
        document_service = DocumentService(session)
        document_service.ensure_kb(
            tenant.id,
            "external_holdout_kb",
            name="真实文档独立 Holdout",
            description="仅用于隔离检索验收，不进入日常知识库",
        )
        for item in manifest.get("files", []):
            if item.get("duplicate_of") or item.get("parse_status") != "parsed":
                continue
            path = source_dir / item["source_name"]
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError(f"source document drifted: {item['source_name']}")
            result = document_service.ingest(
                tenant_id=tenant.id,
                user=user,
                kb_code="external_holdout_kb",
                filename=path.name,
                source_name=item["source_name"],
                data=data,
                source_type="external_holdout",
                meta={"source_sha256": item["sha256"]},
            )
            if result["index_status"] != "indexed":
                raise RuntimeError(f"failed to ingest {item['source_name']}")
        session.commit()
        indexing_ms = round((time.perf_counter() - ingest_started) * 1000, 2)

        rows = []
        latencies = []
        for case in dataset["cases"]:
            started = time.perf_counter()
            hits = service.search(
                session,
                query=case["question"],
                tenant_id=tenant.id,
                kb_codes=["external_holdout_kb"],
                max_security_rank=3,
                top_k=args.top_k,
            )
            latency = round((time.perf_counter() - started) * 1000, 2)
            latencies.append(latency)
            rank = next(
                (
                    index + 1
                    for index, hit in enumerate(hits)
                    if _matches_expected_hit(hit, case)
                ),
                None,
            )
            rows.append(
                {
                    "case_id": case["case_id"],
                    "rank": rank,
                    "hit": rank is not None,
                    "reranked": bool(hits)
                    and all("rerank" in hit.scores for hit in hits),
                    "latency_ms": latency,
                }
            )
    finally:
        session.close()
        engine.dispose()

    total = len(rows)
    recall = sum(1 for row in rows if row["hit"]) / total if total else 0.0
    mrr = sum(1 / row["rank"] for row in rows if row["rank"]) / total if total else 0.0
    stats = service.embedder.cache_info()
    passed = (
        recall >= args.min_recall
        and mrr >= args.min_mrr
        and all(row["reranked"] for row in rows)
        and stats["failures"] == 0
        and stats["fallbacks"] == 0
    )
    return {
        "schema_version": "document-retrieval-holdout-evidence-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "production_ready": False,
        "corpus_fingerprint": manifest["corpus_fingerprint"],
        "dataset_version": dataset["dataset_version"],
        "case_count": total,
        "top_k": args.top_k,
        "recall_at_k": round(recall, 4),
        "mrr": round(mrr, 4),
        "min_recall": args.min_recall,
        "min_mrr": args.min_mrr,
        "reranked_case_count": sum(1 for row in rows if row["reranked"]),
        "embedding_stats": stats,
        "indexing_latency_ms": indexing_ms,
        "query_latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 2) if latencies else 0.0,
        },
        "passed": passed,
        "cases": rows,
        "limitations": [
            "local CPU ONNX runtime",
            "SQLite fallback vector store",
            "production readiness requires target infrastructure and business approval",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--min-cases", type=int, default=30)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--min-recall", type=float, required=False)
    parser.add_argument("--min-mrr", type=float, required=False)
    parser.add_argument("--model-port", type=int, default=8093)
    parser.add_argument("--api-key", default="local-model-key")
    parser.add_argument("--embedding-model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--embedding-dimension", type=int, default=512)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    errors = [
        *validate_source_files(manifest, args.source_dir),
        *validate_holdout(manifest, dataset, min_cases=args.min_cases),
    ]
    if errors:
        print("=== Holdout validation: FAIL ===")
        for error in errors:
            print(f"- {error}")
        return 2
    print(f"Holdout validation: PASS ({len(dataset['cases'])} cases)")
    if args.validate_only:
        return 0
    if args.min_recall is None or args.min_mrr is None:
        parser.error("evaluation requires business-owned --min-recall and --min-mrr")

    result = evaluate(args, manifest, dataset)
    output = args.json_out or (args.state_dir / "document-holdout-evidence.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Recall@{args.top_k}={result['recall_at_k']:.2%} "
        f"MRR={result['mrr']:.4f} "
        f"reranked={result['reranked_case_count']}/{result['case_count']} "
        f"result={'PASS' if result['passed'] else 'FAIL'}"
    )
    print(f"evidence={output.resolve()}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
