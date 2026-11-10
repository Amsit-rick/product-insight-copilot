"""Safe, minimal conformance probes for real LLM, Embedding, and Reranker services."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings

SCHEMA_VERSION = "ai-integration-probe-v1"
PostCallable = Callable[..., Any]


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not (
        parsed.username or parsed.password
    )


def describe_ai_integrations(settings: Settings) -> dict[str, Any]:
    """Return configuration readiness without exposing endpoints or credentials."""

    embedding_base = settings.embedding_base_url or settings.llm_base_url
    components = {
        "llm": {
            "configured": settings.llm_provider == "openai_compatible"
            and _valid_http_url(settings.llm_base_url)
            and bool(settings.llm_api_key),
            "provider": settings.llm_provider,
            "model": settings.llm_model,
        },
        "embedding": {
            "configured": settings.embedding_provider == "openai_compatible"
            and _valid_http_url(embedding_base)
            and bool(settings.embedding_api_key or settings.llm_api_key),
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
            "expected_dimension": settings.embedding_dim,
        },
        "reranker": {
            "configured": _valid_http_url(settings.reranker_endpoint),
            "provider": "http",
            "authenticated": bool(settings.reranker_api_key),
        },
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "ready": all(item["configured"] for item in components.values()),
        "components": components,
    }


def _failure(component: str, started: float, error_type: str, **metadata: Any) -> dict[str, Any]:
    return {
        "component": component,
        "passed": False,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "error_type": error_type,
        "metadata": metadata,
    }


def _success(component: str, started: float, checks: dict[str, bool], **metadata: Any) -> dict[str, Any]:
    return {
        "component": component,
        "passed": all(checks.values()),
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "checks": checks,
        "metadata": metadata,
    }


def _probe_llm(settings: Settings, post: PostCallable) -> dict[str, Any]:
    started = time.perf_counter()
    if not describe_ai_integrations(settings)["components"]["llm"]["configured"]:
        return _failure("llm", started, "not_configured", model=settings.llm_model)
    try:
        response = post(
            f"{settings.llm_base_url.rstrip('/')}/chat/completions",
            json={
                "model": settings.llm_model,
                "messages": [
                    {"role": "system", "content": "Reply with exactly OK."},
                    {"role": "user", "content": "health check"},
                ],
                "temperature": 0,
                "max_tokens": 8,
            },
            headers={"Authorization": f"Bearer {settings.llm_api_key}"},
            timeout=min(settings.llm_timeout_seconds, 30),
        )
        response.raise_for_status()
        payload = response.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        content = ""
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "") if isinstance(message, dict) else ""
        usage = payload.get("usage") or {} if isinstance(payload, dict) else {}
        return _success(
            "llm",
            started,
            {"choices_present": bool(choices), "content_present": bool(content.strip())},
            model=settings.llm_model,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
    except Exception as exc:
        return _failure("llm", started, type(exc).__name__, model=settings.llm_model)


def _probe_embedding(settings: Settings, post: PostCallable) -> dict[str, Any]:
    started = time.perf_counter()
    described = describe_ai_integrations(settings)["components"]["embedding"]
    if not described["configured"]:
        return _failure(
            "embedding",
            started,
            "not_configured",
            model=settings.embedding_model,
            expected_dimension=settings.embedding_dim,
        )
    base_url = settings.embedding_base_url or settings.llm_base_url
    api_key = settings.embedding_api_key or settings.llm_api_key
    try:
        response = post(
            f"{base_url.rstrip('/')}/embeddings",
            json={"model": settings.embedding_model, "input": ["售后退款规则"]},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=min(settings.llm_timeout_seconds, 30),
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        vector = None
        if isinstance(data, list) and data and isinstance(data[0], dict):
            vector = data[0].get("embedding")
        numeric = isinstance(vector, list) and bool(vector) and all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in vector
        )
        dimension = len(vector) if isinstance(vector, list) else 0
        return _success(
            "embedding",
            started,
            {
                "vector_present": numeric,
                "dimension_matches": numeric and dimension == settings.embedding_dim,
            },
            model=settings.embedding_model,
            dimension=dimension,
            expected_dimension=settings.embedding_dim,
        )
    except Exception as exc:
        return _failure(
            "embedding",
            started,
            type(exc).__name__,
            model=settings.embedding_model,
            expected_dimension=settings.embedding_dim,
        )


def _probe_reranker(settings: Settings, post: PostCallable) -> dict[str, Any]:
    started = time.perf_counter()
    if not describe_ai_integrations(settings)["components"]["reranker"]["configured"]:
        return _failure("reranker", started, "not_configured")
    try:
        response = post(
            settings.reranker_endpoint,
            json={
                "query": "退款多久到账",
                "documents": ["退款审核通过后原路退回。", "物流签收后可以评价。"],
                "top_n": 2,
            },
            headers=(
                {"Authorization": f"Bearer {settings.reranker_api_key}"}
                if settings.reranker_api_key
                else None
            ),
            timeout=min(settings.reranker_timeout_seconds, 30),
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results") if isinstance(payload, dict) else None
        valid_results = isinstance(results, list) and bool(results)
        valid_indexes = valid_results and all(
            isinstance(item, dict)
            and isinstance(item.get("index"), int)
            and 0 <= item["index"] < 2
            for item in results
        )
        numeric_scores = valid_results and all(
            isinstance(item.get("score"), (int, float)) and not isinstance(item.get("score"), bool)
            for item in results
        )
        return _success(
            "reranker",
            started,
            {
                "results_present": valid_results,
                "indexes_valid": bool(valid_indexes),
                "scores_numeric": bool(numeric_scores),
            },
            result_count=len(results) if isinstance(results, list) else 0,
        )
    except Exception as exc:
        return _failure("reranker", started, type(exc).__name__)


def run_ai_integration_probe(
    settings: Settings,
    *,
    allow_real_calls: bool,
    post: PostCallable | None = None,
) -> dict[str, Any]:
    """Run three minimal network calls only after explicit authorization."""

    config = describe_ai_integrations(settings)
    if not allow_real_calls:
        return {**config, "executed": False, "passed": False, "reason": "real_calls_not_allowed"}
    if not config["ready"]:
        return {**config, "executed": False, "passed": False, "reason": "configuration_incomplete"}

    caller = post or httpx.post
    results = [
        _probe_llm(settings, caller),
        _probe_embedding(settings, caller),
        _probe_reranker(settings, caller),
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "executed": True,
        "passed": all(result["passed"] for result in results),
        "results": results,
    }
