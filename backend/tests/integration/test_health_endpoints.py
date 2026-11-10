"""进程探活、依赖就绪与基础安全响应头契约。"""

from __future__ import annotations

from app.api import routes_system


def test_live_and_ready_are_separate_public_endpoints(client):
    live = client.get("/health/live")
    ready = client.get("/health/ready")

    assert live.status_code == 200
    assert live.json()["status"] == "alive"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["release_sha"] is None
    assert ready.json()["checks"]["database"]["ok"] is True
    assert ready.json()["checks"]["storage"]["ok"] is True

    assert client.get("/api/v1/health/live").status_code == 200
    assert client.get("/api/v1/health/ready").status_code == 200


def test_ready_exposes_deployed_release_sha(client, monkeypatch):
    settings = routes_system.get_settings().model_copy(update={"app_release_sha": "a" * 40})
    monkeypatch.setattr(routes_system, "get_settings", lambda: settings)

    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["release_sha"] == "a" * 40


def test_ready_returns_503_when_database_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(
        routes_system,
        "_database_check",
        lambda: {"ok": False, "required": True, "error": "OperationalError"},
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["checks"]["database"]["error"] == "OperationalError"


def test_prod_ready_rejects_degraded_cache_and_non_shared_execution(client, monkeypatch):
    prod = routes_system.get_settings().model_copy(update={"app_env": "prod"})
    monkeypatch.setattr(routes_system, "get_settings", lambda: prod)
    monkeypatch.setattr(
        routes_system,
        "_database_check",
        lambda: {"ok": True, "required": True},
    )
    monkeypatch.setattr(
        routes_system,
        "_cache_readiness_check",
        lambda _settings: {
            "ok": False,
            "required": True,
            "backend": "sqlite",
            "configured": "redis",
            "error": "cache_degraded",
        },
    )
    monkeypatch.setattr(
        routes_system,
        "_execution_readiness_check",
        lambda _settings: {
            "ok": False,
            "required": True,
            "backend": "threadpool",
            "queue_readable": False,
            "checkpointer_shared": False,
            "error": "production_execution_not_shared",
        },
    )
    monkeypatch.setattr(
        routes_system,
        "storage_readiness_check",
        lambda _settings: {
            "ok": False,
            "required": True,
            "backend": "local",
            "shared": False,
            "error": "production_storage_not_shared",
        },
    )

    response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["checks"]["cache"]["ok"] is False
    assert body["checks"]["execution"]["checkpointer_shared"] is False
    assert body["checks"]["storage"]["shared"] is False


def test_security_headers_are_added_to_success_and_error_responses(client):
    for path in ("/health/live", "/api/v1/not-a-real-route"):
        response = client.get(path)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_health_exposes_alert_delivery_state_without_leaking_endpoint_or_secret(client):
    response = client.get("/health")

    assert response.status_code == 200
    alerting = response.json()["alerting"]
    assert alerting["webhook"]["enabled"] is False
    assert alerting["webhook"]["configured"] is False
    assert alerting["delivery_queue"] == {
        "pending": 0,
        "claimed": 0,
        "sent": 0,
        "dead": 0,
    }
    assert "url" not in alerting["webhook"]
    assert "secret" not in str(alerting).lower()


def test_delivery_readiness_snapshot_is_admin_only_and_read_only(client, auth, monkeypatch):
    monkeypatch.setattr(
        routes_system,
        "build_delivery_readiness_snapshot",
        lambda *_args, **_kwargs: {
            "schema_version": "delivery-readiness-snapshot-v1",
            "status": "stale",
            "evidence_valid": True,
            "evidence_fresh": False,
            "release_sha_verified": False,
            "trusted_production_ready": False,
            "reasons": ["evidence_expired"],
            "external_gates": [],
            "release_checks": [],
        },
        raising=False,
    )

    assert client.get("/api/v1/delivery-readiness", headers=auth["pm"]).status_code == 403
    response = client.get("/api/v1/delivery-readiness", headers=auth["admin"])

    assert response.status_code == 200
    assert response.json()["status"] == "stale"
    assert response.json()["trusted_production_ready"] is False


def test_health_exposes_model_runtime_metrics_without_prompt_or_output(client):
    response = client.get("/health")

    assert response.status_code == 200
    metrics = response.json()["llm"]["runtime_metrics"]
    assert set(metrics) == {
        "chat_calls",
        "chat_json_calls",
        "json_parse_failures",
        "json_retries",
        "fallbacks",
    }
    assert all(isinstance(value, int) and value >= 0 for value in metrics.values())
    assert "prompt" not in str(metrics).lower()
    assert "content" not in str(metrics).lower()
