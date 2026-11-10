"""Real fact integration probe contract, safety, and deterministic metrics."""

from __future__ import annotations

import json
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.adapters.base import AdapterResult
from app.services.fact_integration_probe import (
    FactProbePlanError,
    load_fact_probe_plan,
    run_fact_probe,
)


def _plan() -> dict:
    return {
        "schema_version": "fact-integration-probe-v1",
        "plan_version": "merchant-a-sandbox-2026-09-25",
        "scenarios": [
            {
                "scenario_id": "success",
                "order_id_env": "FACT_PROBE_SUCCESS_ORDER_ID",
                "customer_id_env": "FACT_PROBE_SUCCESS_CUSTOMER_ID",
                "expect": {
                    "order": {
                        "outcome": "ok",
                        "required_fields": ["order_status", "order_amount"],
                    },
                    "payment": {"outcome": "ok", "required_fields": ["paid_amount"]},
                    "logistics": {"outcome": "ok", "required_fields": ["latest_track"]},
                    "aftersale": {
                        "outcome": "ok",
                        "required_fields": ["aftersale_history"],
                    },
                },
            },
            {
                "scenario_id": "not-found",
                "order_id_env": "FACT_PROBE_NOT_FOUND_ORDER_ID",
                "expect": {
                    source: {"outcome": "order_not_found"}
                    for source in ("order", "payment", "logistics", "aftersale")
                },
            },
        ],
    }


def _write(tmp_path, payload: dict):
    path = tmp_path / "probe-plan.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_plan_loader_accepts_env_references_and_returns_safe_metadata(tmp_path):
    plan = load_fact_probe_plan(_write(tmp_path, _plan()))

    assert plan.plan_version == "merchant-a-sandbox-2026-09-25"
    assert plan.scenario_count == 2
    assert len(plan.sha256) == 64
    metadata = plan.safe_metadata()
    assert metadata["scenario_ids"] == ["success", "not-found"]
    assert "source_path" not in metadata
    assert "FACT_PROBE_SUCCESS_ORDER_ID" not in json.dumps(metadata)


def test_plan_loader_rejects_literals_unknown_fields_and_missing_sources(tmp_path):
    literal = _plan()
    literal["scenarios"][0]["order_id"] = "REAL-ORDER-001"
    with pytest.raises(FactProbePlanError, match="order_id"):
        load_fact_probe_plan(_write(tmp_path, literal))

    unknown_field = _plan()
    unknown_field["scenarios"][0]["expect"]["payment"]["required_fields"] = ["card_no"]
    with pytest.raises(FactProbePlanError, match="允许字段"):
        load_fact_probe_plan(_write(tmp_path, unknown_field))

    missing_source = _plan()
    del missing_source["scenarios"][0]["expect"]["logistics"]
    with pytest.raises(FactProbePlanError, match="四类来源"):
        load_fact_probe_plan(_write(tmp_path, missing_source))


class _Adapter:
    name = "test-http-adapter"
    is_mock = False

    def _result(self, source: str, order_id: str, request_id: str | None):
        if order_id == "MISSING-001":
            return AdapterResult(
                source=source,
                fact_type="snapshot",
                observed_at=datetime.now(timezone.utc),
                request_id=request_id,
                is_mock=False,
                adapter=self.name,
                error={"code": "order_not_found", "message": f"secret {order_id}"},
            )
        payloads = {
            "order": {"order_status": "signed", "order_amount": "100.00"},
            "payment": {"paid_amount": "88.00"},
            "logistics": {"latest_track": "signed"},
            "aftersale": {"aftersale_history": "none"},
        }
        return AdapterResult(
            source=source,
            fact_type="snapshot",
            payload=payloads[source],
            observed_at=datetime.now(timezone.utc),
            request_id=request_id,
            is_mock=False,
            adapter=self.name,
        )

    def fetch_order(self, order_id: str, *, request_id=None):
        return self._result("order", order_id, request_id)

    def fetch_payment(self, order_id: str, *, request_id=None):
        return self._result("payment", order_id, request_id)

    def fetch_logistics(self, order_id: str, *, request_id=None):
        return self._result("logistics", order_id, request_id)

    def fetch_aftersale(self, order_id: str, *, customer_id=None, request_id=None):
        assert customer_id == "ANON-001" if order_id == "ORDER-001" else customer_id is None
        return self._result("aftersale", order_id, request_id)


def test_probe_validates_success_and_faults_without_leaking_identifiers(tmp_path):
    plan = load_fact_probe_plan(_write(tmp_path, _plan()))
    result = run_fact_probe(
        _Adapter(),
        plan,
        runs=2,
        max_p95_ms=1000,
        max_observed_age_seconds=60,
        environ={
            "FACT_PROBE_SUCCESS_ORDER_ID": "ORDER-001",
            "FACT_PROBE_SUCCESS_CUSTOMER_ID": "ANON-001",
            "FACT_PROBE_NOT_FOUND_ORDER_ID": "MISSING-001",
        },
    )

    assert result["passed"] is True
    assert result["schema_version"] == "fact-integration-probe-v1"
    assert datetime.fromisoformat(result["generated_at"]).tzinfo is not None
    assert result["summary"]["checks_passed"] == result["summary"]["checks_total"]
    assert result["summary"]["scenario_consistency_rate"] == 1.0
    rendered = json.dumps(result, ensure_ascii=False)
    assert "ORDER-001" not in rendered
    assert "MISSING-001" not in rendered
    assert "ANON-001" not in rendered
    assert "secret" not in rendered
    assert "signed" not in rendered


def test_probe_fails_before_network_when_required_environment_value_is_missing(tmp_path):
    plan = load_fact_probe_plan(_write(tmp_path, _plan()))

    with pytest.raises(FactProbePlanError, match="FACT_PROBE_SUCCESS_ORDER_ID"):
        run_fact_probe(_Adapter(), plan, environ={})


def test_probe_reports_contract_failure_without_returning_payload_values(tmp_path):
    payload = _plan()
    payload["scenarios"] = payload["scenarios"][:1]
    plan = load_fact_probe_plan(_write(tmp_path, payload))

    class MissingPaymentFieldAdapter(_Adapter):
        def fetch_payment(self, order_id: str, *, request_id=None):
            result = super().fetch_payment(order_id, request_id=request_id)
            result.payload = {"pay_channel": "secret-provider-channel"}
            return result

    result = run_fact_probe(
        MissingPaymentFieldAdapter(),
        plan,
        runs=1,
        environ={
            "FACT_PROBE_SUCCESS_ORDER_ID": "ORDER-001",
            "FACT_PROBE_SUCCESS_CUSTOMER_ID": "ANON-001",
        },
    )

    payment = result["trials"][0]["sources"]["payment"]
    assert result["passed"] is False
    assert payment["checks"]["required_fields_present"] is False
    assert payment["payload_fields"] == ["pay_channel"]
    assert "secret-provider-channel" not in json.dumps(result)


def test_probe_rejects_mock_adapter(tmp_path):
    payload = _plan()
    payload["scenarios"] = payload["scenarios"][:1]
    plan = load_fact_probe_plan(_write(tmp_path, payload))
    adapter = _Adapter()
    adapter.is_mock = True

    with pytest.raises(FactProbePlanError, match="mock"):
        run_fact_probe(adapter, plan, environ={})


def test_committed_example_plan_and_validate_only_cli_are_usable(capsys):
    root = Path(__file__).resolve().parents[3]
    example = root / "docs" / "integrations" / "fact-probe-plan.example.json"
    plan = load_fact_probe_plan(example)
    assert plan.scenario_count == 3

    script = root / "backend" / "scripts" / "verify_fact_integrations.py"
    spec = importlib.util.spec_from_file_location("verify_fact_integrations", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main(["--plan", str(example), "--validate-plan-only"]) == 0
    assert "验收计划有效" in capsys.readouterr().out

    assert module.main(["--validate-plan-only"]) == 0
    assert "验收计划有效" in capsys.readouterr().out
