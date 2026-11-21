from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "verify_saleor_main_workflow.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "verify_saleor_main_workflow_under_test", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _facts(*, payment_error: str | None = None) -> list[dict]:
    rows = []
    for source in ("order", "payment", "logistics", "aftersale"):
        rows.append(
            {
                "source": source,
                "adapter": "http",
                "source_version": "saleor-3.23.49+scenario-v1",
                "is_mock": False,
                "error_code": payment_error if source == "payment" else None,
            }
        )
    return rows


def test_business_case_requires_four_versioned_non_mock_sources_and_trace():
    module = _load_module()
    result = module.evaluate_business_case(
        scenario_id="SCN-NORMAL-DELIVERED",
        analyze_status=202,
        task_status="completed",
        facts=_facts(),
        trace_fact_sources=_facts(),
        recommendation={"action_type": "ask_customer", "amount": None},
        reflection={"enabled": True, "rounds": 1, "max_rounds": 1},
        expected_action="ask_customer",
    )

    assert result["passed"] is True
    assert result["source_count"] == 4
    assert result["source_versions"] == ["saleor-3.23.49+scenario-v1"]
    assert result["reflection_bounded"] is True


def test_business_case_fails_when_action_does_not_match_scenario_expectation():
    module = _load_module()
    result = module.evaluate_business_case(
        scenario_id="SCN-NORMAL-DELIVERED",
        analyze_status=202,
        task_status="completed",
        facts=_facts(),
        trace_fact_sources=_facts(),
        recommendation={"action_type": "contact_logistics", "amount": None},
        reflection={"enabled": True, "rounds": 1, "max_rounds": 1},
        expected_action="ask_customer",
    )

    assert result["passed"] is False
    assert any(
        item["name"] == "scenario_action_matches" and not item["passed"]
        for item in result["checks"]
    )


@pytest.mark.parametrize(
    ("scenario_id", "recommendation", "passed"),
    [
        (
            "SCN-PARTIAL-REFUND",
            {
                "action_type": "refund",
                "amount": "40.00",
                "amount_source": "支付事实（剩余可退金额）",
                "risk_tier": "L3",
                "approval_requirement": "supervisor",
                "status": "pending_approval",
            },
            True,
        ),
        (
            "SCN-PARTIAL-REFUND",
            {
                "action_type": "refund",
                "amount": "80.00",
                "amount_source": "支付事实（实付金额）",
                "risk_tier": "L3",
                "approval_requirement": "supervisor",
                "status": "pending_approval",
            },
            False,
        ),
        (
            "SCN-DUPLICATE-REFUND",
            {"action_type": "ask_customer", "amount": None},
            True,
        ),
        (
            "SCN-CROSS-SOURCE-CONFLICT",
            {"action_type": "ask_customer", "amount": None},
            True,
        ),
    ],
)
def test_business_specific_refund_guards(scenario_id, recommendation, passed):
    module = _load_module()
    result = module.evaluate_business_case(
        scenario_id=scenario_id,
        analyze_status=202,
        task_status="completed",
        facts=_facts(),
        trace_fact_sources=_facts(),
        recommendation=recommendation,
        reflection={"enabled": True, "rounds": 0, "max_rounds": 1},
        refundable_amount="40.00" if scenario_id == "SCN-PARTIAL-REFUND" else None,
    )

    assert result["passed"] is passed


def test_fault_case_preserves_other_sources_and_blocks_refund_on_payment_failure():
    module = _load_module()
    result = module.evaluate_fault_case(
        scenario_id="SCN-FAULT-PAYMENT-RATE-LIMIT",
        target_source="payment",
        expected_error="adapter_rate_limited",
        analyze_status=202,
        task_status="completed",
        facts=_facts(payment_error="adapter_rate_limited"),
        recommendation={"action_type": "ask_customer", "amount": None},
    )

    assert result["passed"] is True
    assert result["healthy_source_count"] == 3
    assert result["target_error_code"] == "adapter_rate_limited"
    assert result["unsafe_refund_generated"] is False


def test_evidence_rejects_sensitive_or_raw_fact_material():
    module = _load_module()
    safe = {
        "scenario_id": "SCN-NORMAL-DELIVERED",
        "source_versions": ["saleor-3.23.49+scenario-v1"],
        "passed": True,
    }
    module.assert_safe_evidence(safe)

    for unsafe in (
        {"token": "secret"},
        {"graphql": {"query": "query { orders { edges { node { id } } } }"}},
        {"facts": [{"payload": {"paid_amount": "88.00"}}]},
        {"customer_id": "customer-1"},
        {"order_id": "100001"},
    ):
        with pytest.raises(ValueError, match="unsafe evidence"):
            module.assert_safe_evidence(unsafe)


def test_percentile_uses_the_repository_nearest_index_convention():
    module = _load_module()

    assert module._percentile([], 0.50) == 0
    assert module._percentile([40, 10, 30, 20], 0.50) == 30
    assert module._percentile([40, 10, 30, 20], 0.95) == 40


def test_summary_reports_rates_latencies_failures_and_handoff_reasons():
    module = _load_module()
    business = [
        {
            "passed": True,
            "latency_ms": 100,
            "fact_query_latency_ms": 40,
            "handoff_reasons": [],
        },
        {
            "passed": False,
            "latency_ms": 300,
            "fact_query_latency_ms": 80,
            "failure_type": "scenario_action_mismatch",
            "handoff_reasons": ["missing_information"],
        },
    ]
    faults = [
        {
            "passed": True,
            "latency_ms": 200,
            "fact_query_latency_ms": 60,
            "target_error_code": "adapter_timeout",
            "handoff_reasons": ["source_unavailable"],
        },
        {
            "passed": True,
            "latency_ms": 400,
            "fact_query_latency_ms": None,
            "target_error_code": "adapter_upstream_error",
            "handoff_reasons": ["source_unavailable", "supervisor_approval"],
        },
    ]

    summary = module.summarize_results(business, faults)

    assert summary == {
        "business_passed": 1,
        "business_total": 2,
        "fault_passed": 2,
        "fault_total": 2,
        "success_rate": 0.75,
        "business_success_rate": 0.5,
        "fault_success_rate": 1.0,
        "workflow_latency_ms_p50": 300,
        "workflow_latency_ms_p95": 400,
        "fact_query_latency_ms_p50": 60,
        "fact_query_latency_ms_p95": 80,
        "observed_failure_types": {
            "adapter_timeout": 1,
            "adapter_upstream_error": 1,
            "scenario_action_mismatch": 1,
        },
        "human_handoff_reasons": {
            "missing_information": 1,
            "source_unavailable": 2,
            "supervisor_approval": 1,
        },
    }


def test_case_evaluation_exposes_safe_handoff_reason_enums():
    module = _load_module()
    result = module.evaluate_business_case(
        scenario_id="SCN-PARTIAL-REFUND",
        analyze_status=202,
        task_status="completed",
        facts=_facts(),
        trace_fact_sources=_facts(),
        recommendation={
            "action_type": "refund",
            "amount": "40.00",
            "amount_source": "支付事实（剩余可退金额）",
            "risk_tier": "L3",
            "approval_requirement": "supervisor",
            "status": "pending_approval",
        },
        reflection={"enabled": True, "rounds": 0, "max_rounds": 1},
        refundable_amount="40.00",
        latency_ms=123,
        fact_query_latency_ms=45,
    )

    assert result["latency_ms"] == 123
    assert result["fact_query_latency_ms"] == 45
    assert result["handoff_reasons"] == ["supervisor_approval"]
