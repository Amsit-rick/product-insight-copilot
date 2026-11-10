import os
from types import SimpleNamespace

import pytest

from scripts.benchmark_retrieval import (
    _positive_int_list,
    build_evidence,
    configure_isolated_environment,
)


def _settings():
    return SimpleNamespace(
        embedding_provider="stub",
        vector_backend="fallback",
        cache_backend="memory",
        retrieval_top_k=5,
    )


def _row(*, p95_ms: float = 100.0, errors: int = 0) -> dict:
    return {
        "size": 1000,
        "concurrency": 1,
        "p95_ms": p95_ms,
        "error_count": errors,
    }


def test_positive_int_list_rejects_invalid_or_duplicate_values():
    assert _positive_int_list("1000,4000", name="sizes") == [1000, 4000]
    with pytest.raises(ValueError, match="positive"):
        _positive_int_list("1000,0", name="sizes")
    with pytest.raises(ValueError, match="unique"):
        _positive_int_list("1000,1000", name="sizes")


def test_evidence_never_claims_quality_or_production_readiness():
    result = build_evidence(
        settings=_settings(),
        sizes=[1000],
        concurrencies=[1],
        queries=8,
        dim=512,
        table_rows=[_row()],
        cache_rows=[],
        indexing_rows=[],
        target_p95_ms=None,
    )

    assert result["passed"] is True
    assert result["execution_passed"] is True
    assert result["acceptance_evaluated"] is False
    assert result["acceptance_passed"] is None
    assert result["quality_claim_allowed"] is False
    assert result["production_ready"] is False


def test_evidence_fails_closed_on_errors_or_explicit_latency_target():
    result = build_evidence(
        settings=_settings(),
        sizes=[1000],
        concurrencies=[1],
        queries=8,
        dim=512,
        table_rows=[_row(p95_ms=350.0, errors=1)],
        cache_rows=[],
        indexing_rows=[],
        target_p95_ms=300.0,
    )

    assert result["passed"] is False
    assert result["execution_passed"] is False
    assert result["acceptance_evaluated"] is True
    assert result["acceptance_passed"] is False
    assert result["error_count"] == 1
    assert result["target_failures"] == [
        {"size": 1000, "concurrency": 1, "p95_ms": 350.0}
    ]


def test_isolated_environment_disables_external_retrieval_dependencies(monkeypatch):
    for key in (
        "EMBEDDING_PROVIDER",
        "EMBEDDING_FAILURE_MODE",
        "RERANKER_ENDPOINT",
        "CACHE_BACKEND",
        "VECTOR_BACKEND",
    ):
        monkeypatch.delenv(key, raising=False)

    configure_isolated_environment(512)

    assert os.environ["EMBEDDING_PROVIDER"] == "stub"
    assert os.environ["RERANKER_ENDPOINT"] == ""
    assert os.environ["CACHE_BACKEND"] == "memory"
    assert os.environ["VECTOR_BACKEND"] == "fallback"
    assert os.environ["EMBEDDING_DIM"] == "512"
