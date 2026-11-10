from types import SimpleNamespace

import pytest

from app.config import Settings
from app.rag.retrieval import RetrievalHit, RetrievalService


def _hit(index: int) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=str(index), document_id="doc", kb_code="policy_kb",
        content=f"document {index}", source_name="policy", page=1,
        document_version="v1", updated_at=None, relevance_score=0.5,
    )


def test_reranker_sends_bearer_key(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.1}]}

    def post(*args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("app.rag.retrieval.httpx.post", post)
    service = RetrievalService()
    service.settings = Settings(
        _env_file=None,
        reranker_endpoint="http://reranker.test/rerank",
        reranker_api_key="secret",
    )
    ranked = service._rerank("refund", [_hit(0), _hit(1)])

    assert captured["headers"] == {"Authorization": "Bearer secret"}
    assert ranked[0].chunk_id == "1"
    assert ranked[0].scores["rerank"] == 0.9


def test_reranker_strict_mode_rejects_silent_heuristic_fallback(monkeypatch):
    def fail(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.rag.retrieval.httpx.post", fail)
    service = RetrievalService()
    service.settings = Settings(
        _env_file=None,
        reranker_endpoint="http://reranker.test/rerank",
        reranker_failure_mode="error",
    )

    with pytest.raises(RuntimeError, match="Reranker 服务不可用"):
        service._rerank("refund", [_hit(0)])


def test_retrieval_strict_embedding_failure_cannot_be_hidden_as_keyword_only():
    service = RetrievalService()
    service.settings = Settings(_env_file=None, embedding_failure_mode="error")
    service.embedder = SimpleNamespace(
        embed_query=lambda _query: (_ for _ in ()).throw(RuntimeError("offline"))
    )

    with pytest.raises(RuntimeError, match="禁止退化为纯关键词检索"):
        service._vector_recall(
            object(),
            "退款规则",
            "tenant",
            ["policy_kb"],
            ["internal"],
            None,
            None,
            None,
        )


def test_reranker_strict_mode_rejects_partial_response(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"index": 0, "score": 0.9}]}

    monkeypatch.setattr("app.rag.retrieval.httpx.post", lambda *_args, **_kwargs: Response())
    service = RetrievalService()
    service.settings = Settings(
        _env_file=None,
        reranker_endpoint="http://reranker.test/rerank",
        reranker_failure_mode="error",
    )

    with pytest.raises(RuntimeError, match="Reranker 服务不可用"):
        service._rerank("refund", [_hit(0), _hit(1)])


def test_reranker_negative_logits_only_reorder_and_preserve_absolute_relevance(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "results": [
                    {"index": 1, "score": -4.0},
                    {"index": 0, "score": -9.0},
                ]
            }

    monkeypatch.setattr("app.rag.retrieval.httpx.post", lambda *_args, **_kwargs: Response())
    service = RetrievalService()
    service.settings = Settings(
        _env_file=None,
        reranker_endpoint="http://reranker.test/rerank",
        reranker_failure_mode="error",
    )
    ranked = service._rerank("refund", [_hit(0), _hit(1)])

    assert [hit.chunk_id for hit in ranked] == ["1", "0"]
    assert [hit.relevance_score for hit in ranked] == [0.5, 0.5]
    assert [hit.scores["rerank"] for hit in ranked] == [-4.0, -9.0]
