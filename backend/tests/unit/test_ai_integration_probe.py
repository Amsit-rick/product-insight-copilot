"""AI integration probe safety, protocol validation, and CLI opt-in behavior."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from app.config import Settings
from app.services.ai_integration_probe import (
    describe_ai_integrations,
    run_ai_integration_probe,
)


def _settings(**overrides) -> Settings:
    values = {
        "llm_provider": "openai_compatible",
        "llm_base_url": "https://llm.example.test/v1",
        "llm_api_key": "llm-secret",
        "llm_model": "small-model",
        "embedding_provider": "openai_compatible",
        "embedding_base_url": "https://embedding.example.test/v1",
        "embedding_api_key": "embedding-secret",
        "embedding_model": "bge-m3",
        "embedding_dim": 4,
        "reranker_endpoint": "https://reranker.example.test/rerank",
        "reranker_api_key": "reranker-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


class _Response:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("provider error with secret body")

    def json(self) -> dict:
        return self.payload


def _success_post(url: str, **kwargs):
    assert kwargs.get("timeout")
    if url.endswith("/chat/completions"):
        assert kwargs["headers"]["Authorization"] == "Bearer llm-secret"
        return _Response(
            {
                "choices": [{"message": {"content": "OK PRIVATE ANSWER"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 1},
            }
        )
    if url.endswith("/embeddings"):
        assert kwargs["headers"]["Authorization"] == "Bearer embedding-secret"
        return _Response({"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3, 0.4]}]})
    assert url == "https://reranker.example.test/rerank"
    assert kwargs["headers"]["Authorization"] == "Bearer reranker-secret"
    return _Response({"results": [{"index": 0, "score": 0.9}, {"index": 1, "score": 0.1}]})


def test_configuration_description_is_safe_and_complete():
    described = describe_ai_integrations(_settings())

    assert described["ready"] is True
    rendered = json.dumps(described)
    assert "example.test" not in rendered
    assert "secret" not in rendered


def test_probe_requires_explicit_real_call_authorization():
    result = run_ai_integration_probe(_settings(), allow_real_calls=False, post=_success_post)

    assert result["executed"] is False
    assert result["reason"] == "real_calls_not_allowed"


def test_probe_stops_before_network_when_configuration_is_incomplete():
    called = False

    def should_not_call(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be called")

    result = run_ai_integration_probe(
        _settings(embedding_provider="stub"), allow_real_calls=True, post=should_not_call
    )

    assert result["executed"] is False
    assert result["reason"] == "configuration_incomplete"
    assert called is False


def test_probe_validates_three_protocols_without_persisting_payload_content():
    result = run_ai_integration_probe(_settings(), allow_real_calls=True, post=_success_post)

    assert result["executed"] is True
    assert result["passed"] is True
    assert [item["component"] for item in result["results"]] == [
        "llm",
        "embedding",
        "reranker",
    ]
    rendered = json.dumps(result)
    assert "PRIVATE ANSWER" not in rendered
    assert "0.1" not in rendered
    assert "售后退款规则" not in rendered
    assert "example.test" not in rendered
    assert "secret" not in rendered


def test_embedding_dimension_mismatch_blocks_probe():
    def mismatch_post(url: str, **kwargs):
        if url.endswith("/embeddings"):
            return _Response({"data": [{"embedding": [0.1, 0.2]}]})
        return _success_post(url, **kwargs)

    result = run_ai_integration_probe(_settings(), allow_real_calls=True, post=mismatch_post)
    embedding = next(item for item in result["results"] if item["component"] == "embedding")

    assert result["passed"] is False
    assert embedding["checks"]["dimension_matches"] is False
    assert embedding["metadata"]["dimension"] == 2


def test_provider_exception_type_is_reported_without_exception_text():
    def failing_post(url: str, **kwargs):
        raise RuntimeError(f"secret token at {url}")

    result = run_ai_integration_probe(_settings(), allow_real_calls=True, post=failing_post)

    assert result["passed"] is False
    assert all(item["error_type"] == "RuntimeError" for item in result["results"])
    rendered = json.dumps(result)
    assert "token" not in rendered
    assert "example.test" not in rendered


def test_cli_defaults_to_config_only_and_makes_no_network_call(monkeypatch, capsys):
    root = Path(__file__).resolve().parents[3]
    script = root / "backend" / "scripts" / "verify_ai_integrations.py"
    spec = importlib.util.spec_from_file_location("verify_ai_integrations", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr("app.config.get_settings", lambda: _settings())

    assert module.main([]) == 0
    output = capsys.readouterr().out
    assert "未发出网络请求" in output
    assert "example.test" not in output
    assert "secret" not in output
