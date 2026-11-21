from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from sandbox_services.app import create_app
from sandbox_services.providers import (
    ProviderFact,
    ScenarioFactProvider,
    StaticFactProvider,
    build_fact_provider,
)
from sandbox_services.scenarios import (
    REQUIRED_SCENARIO_IDS,
    ScenarioManifestError,
    build_scenario_manifest,
    load_scenario_manifest,
    write_scenario_manifest,
)


API_KEY = "scenario-test-key"


def _orders() -> list[dict[str, object]]:
    return [
        {
            "number": str(number),
            "status": status,
            "total_amount": "268.00",
            "charged_amount": charged,
            "refunded_amount": refunded,
            "has_fulfillment": has_fulfillment,
        }
        for number, status, charged, refunded, has_fulfillment in [
            (1, "UNCONFIRMED", "0.00", "0.00", False),
            (2, "UNFULFILLED", "0.00", "0.00", False),
            (3, "PARTIALLY_FULFILLED", "268.00", "0.00", True),
            (4, "UNFULFILLED", "268.00", "0.00", False),
            (5, "UNFULFILLED", "268.00", "40.00", False),
            (6, "UNCONFIRMED", "0.00", "0.00", False),
            (7, "UNFULFILLED", "0.00", "0.00", False),
            (8, "UNFULFILLED", "268.00", "268.00", False),
            (9, "PARTIALLY_FULFILLED", "268.00", "0.00", True),
            (10, "UNFULFILLED", "268.00", "0.00", False),
            (11, "UNCONFIRMED", "0.00", "0.00", False),
            (12, "FULFILLED", "268.00", "0.00", True),
        ]
    ]


def _manifest() -> dict[str, object]:
    return build_scenario_manifest(_orders())


def test_build_manifest_has_all_required_scenarios_with_unique_orders():
    manifest = _manifest()
    scenarios = manifest["scenarios"]

    assert [item["id"] for item in scenarios] == list(REQUIRED_SCENARIO_IDS)
    assert len(scenarios) == 10
    assert len({item["upstream_order_id"] for item in scenarios}) == 10
    assert manifest["environment"] == "local_high_fidelity_sandbox"
    assert manifest["enterprise_endpoint_verified"] is False
    assert manifest["production_ready"] is False


def test_manifest_selection_is_deterministic_when_input_order_changes():
    original = build_scenario_manifest(_orders())
    shuffled = build_scenario_manifest(list(reversed(_orders())))

    assert shuffled == original


def test_manifest_refuses_insufficient_or_duplicate_upstream_orders():
    with pytest.raises(ScenarioManifestError, match="at least 10 unique"):
        build_scenario_manifest(_orders()[:9])

    duplicated = _orders()[:9] + [deepcopy(_orders()[0])]
    with pytest.raises(ScenarioManifestError, match="at least 10 unique"):
        build_scenario_manifest(duplicated)


def test_manifest_contains_no_credentials_or_customer_pii():
    serialized = json.dumps(_manifest(), ensure_ascii=False).lower()

    for forbidden in (
        "app_token",
        "authorization",
        "password",
        "receiver_phone",
        "customer_id",
        "address",
        "email",
    ):
        assert forbidden not in serialized


def test_manifest_loader_rejects_unknown_overlay_fields_and_sensitive_keys(tmp_path):
    manifest = _manifest()
    manifest["scenarios"][1]["overlays"]["payment"]["transaction_id"] = "secret"
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ScenarioManifestError, match="not allowlisted"):
        load_scenario_manifest(invalid)

    sensitive = _manifest()
    sensitive["scenarios"][0]["overlays"]["order"]["receiver_phone"] = "13800138000"
    invalid.write_text(json.dumps(sensitive, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ScenarioManifestError, match="sensitive"):
        load_scenario_manifest(invalid)


def test_manifest_write_is_idempotent_when_file_already_exists(tmp_path):
    path = tmp_path / "manifest.json"
    first = write_scenario_manifest(path, _orders())
    initial_bytes = path.read_bytes()
    second = write_scenario_manifest(path, list(reversed(_orders())))

    assert first == second
    assert path.read_bytes() == initial_bytes


class _RecordingProvider:
    mode = "saleor"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def fetch(self, source, order_id, *, customer_id=None):  # noqa: ANN001
        self.calls.append((source, order_id, customer_id))
        return ProviderFact(
            data={"order_status": "上游原始状态", "paid_amount": "268.00"},
            observed_at="2026-11-15T10:00:00+00:00",
            source_version="saleor-3.23.49",
        )

    async def health(self):
        return {"fact_provider": "saleor", "fact_provider_status": "saleor_ready"}

    async def aclose(self):
        return None


def test_scenario_provider_resolves_alias_then_merges_only_its_source_overlay():
    base = _RecordingProvider()
    provider = ScenarioFactProvider(base, _manifest())
    scenario = provider.list_scenarios()[0]

    fact = asyncio.run(provider.fetch("order", scenario["id"]))

    assert base.calls == [("order", scenario["upstream_order_id"], None)]
    assert fact.data["order_status"] == "已签收"
    assert fact.data["paid_amount"] == "268.00"
    assert fact.source_version == "saleor-3.23.49+scenario-v1"


def test_scenario_provider_passes_numeric_order_through_without_overlay():
    base = _RecordingProvider()
    provider = ScenarioFactProvider(base, _manifest())

    fact = asyncio.run(provider.fetch("order", "12"))

    assert base.calls == [("order", "12", None)]
    assert fact.data["order_status"] == "上游原始状态"
    assert fact.source_version == "saleor-3.23.49"


def test_provider_factory_wraps_saleor_when_manifest_is_configured(tmp_path):
    path = tmp_path / "manifest.json"
    write_scenario_manifest(path, _orders())
    token = tmp_path / "token"
    token.write_text("runtime-token", encoding="utf-8")

    provider = build_fact_provider(
        {
            "SANDBOX_FACT_PROVIDER": "saleor",
            "SALEOR_GRAPHQL_URL": "http://saleor.test/graphql/",
            "SALEOR_APP_TOKEN_FILE": str(token),
            "SANDBOX_SCENARIO_MANIFEST_FILE": str(path),
        }
    )
    try:
        assert isinstance(provider, ScenarioFactProvider)
    finally:
        asyncio.run(provider.aclose())


def test_scenarios_endpoint_returns_safe_metadata_only():
    provider = ScenarioFactProvider(_RecordingProvider(), _manifest())
    app = create_app(
        api_key=API_KEY,
        webhook_secret="test-secret",
        delay_seconds=0,
        fact_provider=provider,
    )

    with TestClient(app) as client:
        unauthorized = client.get("/scenarios")
        response = client.get("/scenarios", headers={"X-Api-Key": API_KEY})

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    payload = response.json()
    assert payload["environment"] == "local_high_fidelity_sandbox"
    assert len(payload["scenarios"]) == 10
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    assert "upstream_order_id" not in serialized
    assert "overlays" not in serialized
    assert "token" not in serialized
    assert "password" not in serialized
    assert set(payload["scenarios"][0]) == {
        "id",
        "name",
        "purpose",
        "source_type",
        "overlay_applied",
    }


def test_static_provider_does_not_gain_synthetic_scenario_aliases():
    app = create_app(
        api_key=API_KEY,
        webhook_secret="test-secret",
        delay_seconds=0,
        fact_provider=StaticFactProvider(),
    )

    with TestClient(app) as client:
        response = client.get("/scenarios", headers={"X-Api-Key": API_KEY})

    assert response.status_code == 200
    assert response.json() == {
        "environment": "local_static_sandbox",
        "scenarios": [],
    }
