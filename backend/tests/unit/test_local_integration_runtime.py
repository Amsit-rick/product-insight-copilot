from pathlib import Path

import pytest

from app.config import Settings
from scripts.local_integration import integration_environment


def test_local_runtime_isolates_data_and_preserves_model_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-env-secret")
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("TASK_RUN_INLINE", "true")
    monkeypatch.setenv("WORKER_EMBEDDED", "true")
    original = Settings(_env_file=None, database_url="sqlite:///original.db")
    env = integration_environment(tmp_path, 8091, original)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    effective = Settings(_env_file=None)
    assert env["LLM_API_KEY"] == "test-env-secret"
    assert effective.llm_provider == "openai_compatible"
    assert effective.database_url != original.database_url
    assert effective.database_url.endswith("integration.db")
    assert effective.task_execution_backend == "queue"
    assert not effective.task_run_inline and not effective.worker_embedded
    assert effective.checkpointer_backend == "sqlite"
    assert Path(effective.checkpointer_sqlite_path).parent == tmp_path.resolve()
    assert effective.reflection_enabled and effective.reflection_rollout_auto_guard
    assert not effective.ticket_use_mock_adapters
    assert effective.ticket_fact_order_url == "http://127.0.0.1:8091/orders/{order_id}"
    assert effective.metric_source_http_enabled
    assert effective.proactive_anomaly_metrics == ["crash_rate", "next_day_retention"]
    assert not effective.proactive_anomaly_enabled
    assert effective.alert_webhook_enabled


def test_local_runtime_rejects_production(tmp_path):
    with pytest.raises(ValueError, match="production"):
        integration_environment(tmp_path, 8091, Settings(_env_file=None, app_env="prod"))


def test_local_runtime_refuses_business_database_collision(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 'integration.db'}")
    with pytest.raises(ValueError, match="business DB"):
        integration_environment(tmp_path, 8091, settings)


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_local_runtime_rejects_invalid_sandbox_port(tmp_path, port):
    with pytest.raises(ValueError, match="port"):
        integration_environment(tmp_path, port, Settings(_env_file=None))


def test_local_runtime_real_models_are_strict_and_isolated(tmp_path):
    env = integration_environment(
        tmp_path,
        8091,
        Settings(_env_file=None),
        with_local_models=True,
        model_port=8093,
    )

    assert env["DATABASE_URL"].endswith("integration.db")
    assert env["EMBEDDING_PROVIDER"] == "openai_compatible"
    assert env["EMBEDDING_MODEL"] == "BAAI/bge-small-zh-v1.5"
    assert env["EMBEDDING_DIM"] == "512"
    assert env["EMBEDDING_BASE_URL"] == "http://127.0.0.1:8093/v1"
    assert env["EMBEDDING_FAILURE_MODE"] == "error"
    assert env["RERANKER_ENDPOINT"] == "http://127.0.0.1:8093/rerank"
    assert env["RERANKER_FAILURE_MODE"] == "error"
    assert env["LOCAL_RERANKER_MODEL"] == "BAAI/bge-reranker-v2-m3-int8"


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_local_runtime_rejects_invalid_model_port(tmp_path, port):
    with pytest.raises(ValueError, match="model port"):
        integration_environment(
            tmp_path,
            8091,
            Settings(_env_file=None),
            with_local_models=True,
            model_port=port,
        )
