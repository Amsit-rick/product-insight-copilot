"""Local OpenAI-compatible embeddings and authenticated reranker service.

This is an optional development/evaluation process. It runs real ONNX models
through FastEmbed and is never imported by the main API process.
"""

from __future__ import annotations

import hmac
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator


class EmbeddingRequest(BaseModel):
    model: str
    input: str | list[str]

    @field_validator("input")
    @classmethod
    def validate_input(cls, value):  # noqa: ANN001
        items = [value] if isinstance(value, str) else value
        if not items or len(items) > 128:
            raise ValueError("input batch must contain 1-128 texts")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 12000 for item in items):
            raise ValueError("each input must be non-empty and at most 12000 characters")
        return value


class RerankRequest(BaseModel):
    query: str = Field(min_length=1, max_length=12000)
    documents: list[str] = Field(min_length=1, max_length=128)
    top_n: int | None = Field(default=None, ge=1, le=128)

    @field_validator("documents")
    @classmethod
    def validate_documents(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 12000 for item in value):
            raise ValueError("each document must be non-empty and at most 12000 characters")
        return value


class LocalModelRuntime:
    def __init__(
        self,
        *,
        embedding_model: str | None = None,
        reranker_model: str | None = None,
        cache_dir: str | Path | None = None,
        threads: int | None = None,
    ) -> None:
        self.embedding_model = embedding_model or os.getenv(
            "LOCAL_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"
        )
        self.reranker_model = reranker_model or os.getenv(
            "LOCAL_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3-int8"
        )
        self.cache_dir = str(cache_dir or os.getenv("LOCAL_MODEL_CACHE_DIR", ".tmp/fastembed-cache"))
        self.threads = threads or int(os.getenv("LOCAL_MODEL_THREADS", "4"))
        self._embedding = None
        self._reranker = None
        self._load_lock = threading.Lock()
        # FastEmbed 暴露生成器接口，本地服务选择单通道推理，避免同一进程内
        # ONNX 线程池互相争抢。容量边界由 benchmark_local_models.py 实测。
        self._inference_lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._embedding is not None and self._reranker is not None

    def load(self) -> None:
        if self.loaded:
            return
        with self._load_lock:
            if self.loaded:
                return
            from fastembed import TextEmbedding
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            Path(self.cache_dir).mkdir(parents=True, exist_ok=True)
            # The optional package installs CPU ONNX Runtime by default. CUDA is
            # disabled explicitly so hosts without onnxruntime-gpu behave identically.
            self._embedding = TextEmbedding(
                self.embedding_model,
                cache_dir=self.cache_dir,
                threads=self.threads,
                cuda=False,
            )
            self._reranker = TextCrossEncoder(
                self.reranker_model,
                cache_dir=self.cache_dir,
                threads=self.threads,
                cuda=False,
            )

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.load()
        with self._inference_lock:
            return [[float(value) for value in vector] for vector in self._embedding.embed(texts)]

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.load()
        with self._inference_lock:
            return [float(value) for value in self._reranker.rerank(query, documents)]

    def description(self) -> dict[str, Any]:
        return {
            "loaded": self.loaded,
            "embedding_model": self.embedding_model,
            "reranker_model": self.reranker_model,
            "device": "cpu-onnx",
            "model_threads": self.threads,
            "inference_policy": "shared_serial",
        }


def create_app(
    runtime: LocalModelRuntime | Any | None = None,
    *,
    api_key: str | None = None,
    eager_load: bool = True,
) -> FastAPI:
    model_runtime = runtime or LocalModelRuntime()
    expected_key = api_key or os.getenv("LOCAL_MODEL_API_KEY", "local-model-key")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if eager_load:
            model_runtime.load()
        yield

    app = FastAPI(
        title="Local Semantic Retrieval Models",
        version="1.0.0",
        lifespan=lifespan,
        description="Local-only real ONNX embedding and reranker service.",
    )
    app.state.model_runtime = model_runtime

    def authorize(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> None:
        prefix = "Bearer "
        supplied = authorization[len(prefix) :] if authorization and authorization.startswith(prefix) else ""
        if not supplied or not hmac.compare_digest(supplied, expected_key):
            raise HTTPException(status_code=401, detail={"code": "unauthorized"})

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, Any]:
        return {"status": "ok" if model_runtime.loaded else "loading", **model_runtime.description()}

    @app.post("/v1/embeddings", dependencies=[Depends(authorize)])
    def embeddings(payload: EmbeddingRequest) -> dict[str, Any]:
        if payload.model != model_runtime.embedding_model:
            raise HTTPException(status_code=400, detail={"code": "unsupported_model"})
        texts = [payload.input] if isinstance(payload.input, str) else payload.input
        vectors = model_runtime.embed(texts)
        return {
            "object": "list",
            "model": model_runtime.embedding_model,
            "data": [
                {"object": "embedding", "index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ],
            "usage": {"input_count": len(texts)},
        }

    @app.post("/rerank", dependencies=[Depends(authorize)])
    def rerank(payload: RerankRequest) -> dict[str, Any]:
        scores = model_runtime.rerank(payload.query, payload.documents)
        if len(scores) != len(payload.documents):
            raise HTTPException(status_code=502, detail={"code": "score_count_mismatch"})
        ranked = sorted(enumerate(scores), key=lambda item: item[1], reverse=True)
        top_n = min(payload.top_n or len(ranked), len(ranked))
        return {
            "model": model_runtime.reranker_model,
            "results": [
                {"index": index, "score": score}
                for index, score in ranked[:top_n]
            ],
        }

    return app


app = create_app()

__all__ = ["LocalModelRuntime", "app", "create_app"]
