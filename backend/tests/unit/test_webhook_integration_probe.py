from __future__ import annotations

import json
from datetime import datetime

import httpx

from app.config import Settings
from app.services.webhook_integration_probe import (
    run_local_webhook_contract_drill,
    run_webhook_integration_probe,
)


def _settings(**overrides) -> Settings:
    values = {
        "llm_provider": "stub",
        "app_env": "prod",
        "alert_webhook_enabled": True,
        "alert_webhook_url": "https://alerts.example.test/hooks/operations",
        "alert_webhook_secret": "probe-signing-secret",
        "alert_webhook_timeout_seconds": 1,
        "alert_webhook_max_attempts": 2,
        "alert_webhook_retry_base_seconds": 0,
        "alert_webhook_lease_seconds": 30,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_local_contract_drill_covers_success_retry_dead_letter_and_replay():
    result = run_local_webhook_contract_drill()

    assert result["executed"] is True
    assert result["passed"] is True
    assert result["transport"] == "loopback_http"
    assert result["external_network_used"] is False
    assert result["checks"] == {
        "hmac_signature": True,
        "success_2xx": True,
        "retry_503_then_2xx": True,
        "dead_letter_400": True,
        "replay_then_2xx": True,
    }


def test_default_probe_never_calls_configured_external_endpoint():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_webhook_integration_probe(
        _settings(),
        allow_real_call=False,
        external_client=client,
    )

    assert calls == 0
    assert result["local_contract"]["passed"] is True
    assert result["external_delivery"] == {
        "executed": False,
        "passed": False,
        "reason": "real_call_not_authorized",
    }
    assert result["production_ready"] is False


def test_real_probe_rejects_insecure_or_unsigned_configuration_before_network():
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = run_webhook_integration_probe(
        _settings(
            alert_webhook_url="http://alerts.example.test/hooks/operations",
            alert_webhook_secret="",
        ),
        allow_real_call=True,
        external_client=client,
    )

    assert calls == 0
    assert result["configuration"]["passed"] is False
    assert result["external_delivery"]["reason"] == "configuration_not_ready"
    assert result["production_ready"] is False


def test_authorized_real_probe_uses_production_sender_without_leaking_configuration():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["X-Alert-Signature"].startswith("sha256=")
        assert request.headers["X-Alert-Timestamp"]
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = _settings()
    result = run_webhook_integration_probe(
        settings,
        allow_real_call=True,
        external_client=client,
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert calls == 1
    assert result["external_delivery"]["executed"] is True
    assert result["external_delivery"]["passed"] is True
    assert result["production_ready"] is True
    assert datetime.fromisoformat(result["generated_at"]).tzinfo is not None
    assert settings.alert_webhook_url not in serialized
    assert settings.alert_webhook_secret not in serialized

