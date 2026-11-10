from __future__ import annotations

import json
from datetime import datetime

from app.services.target_environment_probe import run_target_environment_probe


READY_PAYLOAD = {
    "status": "ready",
    "env": "prod",
    "release_sha": "a" * 40,
    "checks": {
        "database": {"ok": True, "required": True},
        "cache": {"ok": True, "required": True, "backend": "redis"},
        "execution": {
            "ok": True,
            "required": True,
            "backend": "queue",
            "queue_readable": True,
            "checkpointer_shared": True,
        },
        "storage": {
            "ok": True,
            "required": True,
            "backend": "minio",
            "shared": True,
        },
    },
}


def test_probe_does_not_call_target_without_explicit_authorization():
    def forbidden(*args, **kwargs):
        raise AssertionError("target must not be called")

    result = run_target_environment_probe(
        [("api-a", "https://api-a.example.com")],
        allow_real_calls=False,
        fetcher=forbidden,
    )

    assert result["executed"] is False
    assert result["reason"] == "real_target_calls_not_authorized"
    assert result["production_ready"] is False
    assert datetime.fromisoformat(result["generated_at"]).tzinfo is not None


def test_two_ready_production_nodes_can_form_operator_attested_cross_host_evidence():
    def ready_fetcher(url: str, timeout: float):
        assert timeout > 0
        assert url.endswith("/health/ready")
        return 200, READY_PAYLOAD

    nodes = [
        ("api-a", "https://api-a.example.com"),
        ("api-b", "https://api-b.example.com"),
    ]
    result = run_target_environment_probe(
        nodes,
        allow_real_calls=True,
        confirm_distinct_hosts=True,
        expected_release_sha="a" * 40,
        fetcher=ready_fetcher,
    )

    assert result["runtime_shape_ready"] is True
    assert result["target_environment_ready"] is True
    assert result["cross_host_or_target_environment_verified"] is True
    assert result["expected_release_sha"] == "a" * 40
    assert all(
        item["checks"]["release_sha_matches"] is True
        for item in result["node_results"]
    )
    assert result["verification_basis"] == "live_checks_plus_operator_distinct_host_attestation"
    assert result["production_ready"] is False
    encoded = json.dumps(result)
    assert "api-a.example.com" not in encoded
    assert "api-b.example.com" not in encoded


def test_single_test_node_can_prove_remote_ci_shape_but_not_target_or_cross_host():
    test_payload = {**READY_PAYLOAD, "env": "test", "release_sha": "b" * 40}

    result = run_target_environment_probe(
        [("github-runner", "http://127.0.0.1:18210")],
        expected_env="test",
        expected_release_sha="b" * 40,
        allow_insecure_http=True,
        allow_real_calls=True,
        fetcher=lambda *_: (200, test_payload),
    )

    assert result["runtime_shape_ready"] is True
    assert result["target_environment_ready"] is False
    assert result["cross_host_or_target_environment_verified"] is False
    assert result["production_ready"] is False


def test_release_sha_mismatch_fails_target_probe():
    result = run_target_environment_probe(
        [("api-a", "https://api-a.example.com")],
        allow_real_calls=True,
        expected_release_sha="b" * 40,
        fetcher=lambda *_: (200, READY_PAYLOAD),
    )

    assert result["passed"] is False
    assert result["node_results"][0]["checks"]["release_sha_matches"] is False


def test_unready_response_fails_closed_without_leaking_response_content():
    result = run_target_environment_probe(
        [("api-a", "https://api-a.example.com")],
        allow_real_calls=True,
        fetcher=lambda *_: (503, {"detail": "secret internal endpoint"}),
    )

    assert result["passed"] is False
    assert result["runtime_shape_ready"] is False
    assert "secret internal endpoint" not in json.dumps(result)


def test_invalid_or_credential_bearing_urls_are_rejected_before_network():
    result = run_target_environment_probe(
        [
            ("api-a", "https://user:password@example.com"),
            ("api-a", "https://example.com?token=value"),
        ],
        allow_real_calls=True,
        fetcher=lambda *_: (_ for _ in ()).throw(AssertionError("must not call")),
    )

    assert result["executed"] is False
    assert result["reason"] == "target_configuration_invalid"
    assert result["production_ready"] is False
