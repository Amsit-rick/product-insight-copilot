"""Repeatability summary and real-provider gate regression tests."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def _runner_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "run_ticket_evaluation.py"
    spec = importlib.util.spec_from_file_location("run_ticket_evaluation", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repeat_summary_reports_variance_consistency_latency_and_cost():
    runner = _runner_module()
    runs = [
        {
            "metrics": {"pass_rate": 0.5, "total_cost_amount": 0.3, "model_calls": 2},
            "details": [
                {"case_id": "a", "passed": True, "latency_ms": 100},
                {"case_id": "b", "passed": False, "latency_ms": 200},
            ],
        },
        {
            "metrics": {"pass_rate": 1.0, "total_cost_amount": 0.5, "model_calls": 3},
            "details": [
                {"case_id": "a", "passed": True, "latency_ms": 300},
                {"case_id": "b", "passed": True, "latency_ms": 400},
            ],
        },
    ]

    summary = runner.summarize_runs(runs)

    assert summary == {
        "run_count": 2,
        "pass_rate_mean": 0.75,
        "pass_rate_min": 0.5,
        "pass_rate_max": 1.0,
        "case_consistency_rate": 0.5,
        "p95_latency_ms": 400,
        "total_cost_amount": 0.8,
        "avg_cost_per_run": 0.4,
        "model_calls": 5,
    }


def test_prepare_env_does_not_override_explicitly_allowed_real_providers(tmp_path, monkeypatch):
    runner = _runner_module()
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai_compatible")
    monkeypatch.setenv("TICKET_USE_MOCK_ADAPTERS", "false")

    runner.prepare_env(
        tmp_path / "eval.db",
        allow_real_models=True,
        allow_real_fact_adapters=True,
    )

    assert os.environ["LLM_PROVIDER"] == "openai_compatible"
    assert os.environ["EMBEDDING_PROVIDER"] == "openai_compatible"
    assert os.environ["TICKET_USE_MOCK_ADAPTERS"] == "false"


def test_prepare_env_forces_safe_defaults(tmp_path, monkeypatch):
    runner = _runner_module()
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("TICKET_USE_MOCK_ADAPTERS", "false")

    runner.prepare_env(tmp_path / "eval.db")

    assert os.environ["LLM_PROVIDER"] == "stub"
    assert os.environ["EMBEDDING_PROVIDER"] == "stub"
    assert os.environ["TICKET_USE_MOCK_ADAPTERS"] == "true"


def _real_runs():
    metrics = {
        "approval_gate_recall": 1.0,
        "expired_policy_leak_rate": 0.0,
        "tenant_isolation_rate": 1.0,
        "duplicate_write_blocked_rate": 1.0,
        "evidence_support_rate": 0.9,
        "unpriced_models": [],
    }
    return [
        {
            "status": "completed",
            "case_count": 200,
            "metrics": dict(metrics),
            "details": [{"case_id": f"case-{index}", "order_id": "ANON-ORDER"}],
        }
        for index in range(3)
    ]


def test_release_acceptance_uses_explicit_thresholds_and_safety_metrics():
    runner = _runner_module()
    result = runner.build_release_acceptance(
        dataset={"kind": "external_holdout", "case_count": 200},
        runtime={"llm_provider": "openai_compatible", "fact_adapter": "http"},
        summary={
            "run_count": 3,
            "model_calls": 600,
            "pass_rate_min": 0.86,
            "case_consistency_rate": 0.95,
            "p95_latency_ms": 8000,
        },
        runs=_real_runs(),
        thresholds={
            "min_pass_rate": 0.85,
            "min_consistency_rate": 0.9,
            "min_evidence_support_rate": 0.8,
            "max_p95_ms": 10000,
        },
    )

    assert result["passed"] is True
    assert result["status"] == "verified"
    assert all(result["checks"].values())


def test_release_acceptance_without_business_thresholds_stays_unverified():
    runner = _runner_module()
    result = runner.build_release_acceptance(
        dataset={"kind": "external_holdout", "case_count": 200},
        runtime={"llm_provider": "openai_compatible", "fact_adapter": "http"},
        summary={
            "run_count": 3,
            "model_calls": 600,
            "pass_rate_min": 1.0,
            "case_consistency_rate": 1.0,
            "p95_latency_ms": 100,
        },
        runs=_real_runs(),
        thresholds=None,
    )

    assert result["passed"] is False
    assert result["status"] == "not_configured"
    assert result["checks"]["thresholds_configured"] is False


def test_external_holdout_evidence_removes_case_level_details():
    runner = _runner_module()
    runs = _real_runs()

    external = runner._evidence_runs(runs, external_holdout=True)
    internal = runner._evidence_runs(runs, external_holdout=False)

    assert all("details" not in run for run in external)
    assert all("details" in run for run in internal)
