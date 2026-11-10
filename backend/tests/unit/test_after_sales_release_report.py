"""预发布报告的状态汇总和外部门禁契约。"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "after_sales_release_report.py"
SPEC = importlib.util.spec_from_file_location("after_sales_release_report", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _target_admission_payload(*, machine_ready: bool = True, sha: str = "c" * 40):
    return {
        "schema_version": "target-release-admission-v1",
        "generated_at": _now(),
        "executed": True,
        "passed": machine_ready,
        "status": "machine_ready_awaiting_human_approval" if machine_ready else "blocked",
        "expected_release_sha": sha,
        "evidence_release_shas": [sha],
        "checks": {
            "release_sha_verified": True,
            "image_digest_verified": True,
            "local_rollout_verified": True,
            "enterprise_registry_verified": machine_ready,
            "cross_host_verified": machine_ready,
        },
        "blockers": [] if machine_ready else ["enterprise_registry_verified"],
        "machine_admission_ready": machine_ready,
        "human_approval_required": True,
        "release_authorized": False,
        "production_ready": False,
    }


def test_target_release_admission_must_be_fresh_machine_ready_and_commit_bound(tmp_path):
    path = tmp_path / "target-release-admission.json"
    path.write_text(json.dumps(_target_admission_payload()), encoding="utf-8")

    verified = module._target_release_admission_status(path, expected_commit_sha="c" * 40)
    mismatched = module._target_release_admission_status(path, expected_commit_sha="d" * 40)

    assert verified["status"] == "verified_machine"
    assert verified["machine_admission_ready"] is True
    assert verified["commit_matches"] is True
    assert mismatched["status"] == "not_verified"
    assert mismatched["machine_admission_ready"] is False
    assert mismatched["commit_matches"] is False


def test_summarize_checks_counts_failures_without_details():
    result = module.summarize_checks(
        [
            SimpleNamespace(name="a", status="PASS"),
            SimpleNamespace(name="b", status="WARN"),
            SimpleNamespace(name="c", status="FAIL"),
        ]
    )

    assert result == {
        "total": 3,
        "pass": 1,
        "warn": 1,
        "fail": 1,
        "failed_checks": ["c"],
    }


def test_external_gates_cannot_be_reported_as_passed():
    assert module.EXTERNAL_GATES
    assert all(item["status"] == "blocked_external" for item in module.EXTERNAL_GATES)
    assert "真实模型" in next(item for item in module.EXTERNAL_GATES if item["key"] == "real_model_holdout")["required"]


def test_local_production_shape_is_verified_without_claiming_target_environment(tmp_path):
    path = tmp_path / "load.json"
    path.write_text(
        json.dumps(
            {
                "run_id": "local-rehearsal",
                "counts": {"requested": 4, "submitted": 4, "completed": 4, "errors": 0},
                "workers": ["worker-1", "worker-2"],
                "queue_final": {"pending": 0, "claimed": 0, "dead": 0},
                "acceptance": [{"name": "errors", "passed": True}],
                "runtime_health": {
                    "database": "postgres",
                    "vector_backend": "pgvector",
                    "cache": {"backend": "redis", "healthy": True},
                    "execution": {
                        "backend": "queue",
                        "worker_embedded": False,
                        "checkpointer": {"backend": "postgres", "shared": True},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = module._local_production_shape_status(path)

    assert result["status"] == "verified"
    assert result["local_production_shape_verified"] is True
    assert result["cross_host_or_target_environment_verified"] is False
    assert "single_host_compose" in result["limitations"]


def test_remote_ci_shape_does_not_count_as_cross_host_target_evidence(tmp_path):
    path = tmp_path / "remote-ci.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "target-environment-probe-v1",
                "generated_at": _now(),
                "configuration": {"passed": True, "node_count": 1},
                "expected_env": "test",
                "executed": True,
                "passed": True,
                "runtime_shape_ready": True,
                "target_environment_ready": False,
                "distinct_hosts_attested": False,
                "cross_host_or_target_environment_verified": False,
                "verification_basis": "live_runtime_checks_without_cross_host_attestation",
                "production_ready": False,
            }
        ),
        encoding="utf-8",
    )

    result = module._target_environment_status(path)

    assert result["status"] == "not_verified"
    assert result["cross_host_or_target_environment_verified"] is False


def test_local_scheduler_failover_is_reported_without_clearing_cross_host_gate(tmp_path):
    path = tmp_path / "scheduler-failover.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "scheduler-failover-rehearsal-v1",
                "generated_at": _now(),
                "executed": True,
                "passed": True,
                "database_backend": "postgresql",
                "process_isolation": True,
                "checks": {
                    "holder_acquired": True,
                    "concurrent_contender_blocked": True,
                    "crashed_holder_did_not_release": True,
                    "takeover_after_expiry": True,
                    "takeover_released": True,
                    "rehearsal_row_cleaned": True,
                },
                "local_postgres_failover_verified": True,
                "cross_host_failover_verified": False,
                "production_ready": False,
            }
        ),
        encoding="utf-8",
    )

    result = module._scheduler_failover_status(path)

    assert result["status"] == "verified_local"
    assert result["local_postgres_failover_verified"] is True
    assert result["cross_host_failover_verified"] is False
    assert result["gate_status"] == "informational_local"


def test_scheduler_failover_evidence_with_production_claim_is_rejected(tmp_path):
    path = tmp_path / "scheduler-failover-invalid.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "scheduler-failover-rehearsal-v1",
                "generated_at": _now(),
                "executed": True,
                "passed": True,
                "database_backend": "postgresql",
                "process_isolation": True,
                "checks": {"all": True},
                "local_postgres_failover_verified": True,
                "cross_host_failover_verified": True,
                "production_ready": True,
            }
        ),
        encoding="utf-8",
    )

    result = module._scheduler_failover_status(path)

    assert result["status"] == "not_verified"
    assert result["local_postgres_failover_verified"] is False
    assert result["cross_host_failover_verified"] is False


def _policy_benchmark_payload(*, recall: float = 1.0, top_k: int = 5):
    case_count = 8
    hit_count = round(case_count * recall)
    cases = []
    for index in range(case_count):
        hit = index < hit_count
        cases.append(
            {
                "query": f"query-{index}",
                "expected_title": f"policy-{index}",
                "rank": 1 if hit else None,
                "top_titles": [f"policy-{index}"] if hit else [],
                "hit": hit,
            }
        )
    recomputed_recall = hit_count / case_count
    return {
        "schema_version": "policy-retrieval-benchmark-v1",
        "generated_at": _now(),
        "dataset": "synthetic_seeded_policy_queries",
        "independent_holdout": False,
        "production_ready": False,
        "embedding_mode": "stub",
        "embedding_model": "stub-hashing-v1",
        "embedding_stats": {"failures": 0, "fallbacks": 0},
        "strict_evidence_ok": True,
        "top_k": top_k,
        "case_count": case_count,
        "recall_at_k": recomputed_recall,
        "mrr": recomputed_recall,
        "threshold": 0.8,
        "passed": recomputed_recall >= 0.8,
        "cases": cases,
    }


def test_fresh_policy_retrieval_benchmark_clears_local_gate(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_policy_benchmark_payload()), encoding="utf-8")

    result = module._policy_retrieval_benchmark_status(path)

    assert result["status"] == "verified_local"
    assert result["gate_status"] == "verified_local"
    assert result["policy_retrieval_verified"] is True
    assert result["recall_at_k"] == 1.0


def test_policy_retrieval_benchmark_recomputes_cases_and_rejects_loopholes(tmp_path):
    low_recall = _policy_benchmark_payload(recall=0.5)
    low_recall.update({"recall_at_k": 1.0, "mrr": 1.0, "passed": True})
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(low_recall), encoding="utf-8")

    summary_mismatch = module._policy_retrieval_benchmark_status(path)

    inflated_top_k = _policy_benchmark_payload(top_k=50)
    path.write_text(json.dumps(inflated_top_k), encoding="utf-8")
    top_k_result = module._policy_retrieval_benchmark_status(path)

    stale = _policy_benchmark_payload()
    stale["generated_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(stale), encoding="utf-8")
    stale_result = module._policy_retrieval_benchmark_status(path)

    assert summary_mismatch["policy_retrieval_verified"] is False
    assert summary_mismatch["recall_at_k"] == 0.5
    assert top_k_result["policy_retrieval_verified"] is False
    assert stale_result["policy_retrieval_verified"] is False


def test_policy_retrieval_benchmark_rejects_unsafe_or_incomplete_stub_evidence(tmp_path):
    path = tmp_path / "policy.json"
    production_claim = _policy_benchmark_payload()
    production_claim["production_ready"] = True
    path.write_text(json.dumps(production_claim), encoding="utf-8")
    claimed_ready = module._policy_retrieval_benchmark_status(path)

    fallback = _policy_benchmark_payload()
    fallback["embedding_stats"]["fallbacks"] = 1
    path.write_text(json.dumps(fallback), encoding="utf-8")
    fallback_result = module._policy_retrieval_benchmark_status(path)

    incomplete = _policy_benchmark_payload()
    incomplete.pop("strict_evidence_ok")
    path.write_text(json.dumps(incomplete), encoding="utf-8")
    incomplete_result = module._policy_retrieval_benchmark_status(path)

    missing_stats = _policy_benchmark_payload()
    missing_stats.pop("embedding_stats")
    path.write_text(json.dumps(missing_stats), encoding="utf-8")
    missing_stats_result = module._policy_retrieval_benchmark_status(path)

    assert claimed_ready["policy_retrieval_verified"] is False
    assert fallback_result["policy_retrieval_verified"] is False
    assert incomplete_result["policy_retrieval_verified"] is False
    assert missing_stats_result["policy_retrieval_verified"] is False


def test_attested_two_node_production_evidence_can_clear_infrastructure_gate(tmp_path):
    path = tmp_path / "target.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "target-environment-probe-v1",
                "generated_at": _now(),
                "configuration": {"passed": True, "node_count": 2},
                "expected_env": "prod",
                "executed": True,
                "passed": True,
                "runtime_shape_ready": True,
                "target_environment_ready": True,
                "distinct_hosts_attested": True,
                "cross_host_or_target_environment_verified": True,
                "verification_basis": "live_checks_plus_operator_distinct_host_attestation",
                "production_ready": False,
            }
        ),
        encoding="utf-8",
    )

    result = module._target_environment_status(path)
    gates = module._external_gates(result)

    assert result["status"] == "verified_external"
    assert result["cross_host_or_target_environment_verified"] is True
    infrastructure = next(item for item in gates if item["key"] == "production_infrastructure")
    assert infrastructure["status"] == "verified_external"
    assert all(
        item["status"] == "blocked_external"
        for item in gates
        if item["key"] != "production_infrastructure"
    )


def test_real_fact_evidence_can_clear_only_fact_platform_gate(tmp_path):
    path = tmp_path / "fact.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "fact-integration-probe-v1",
                "generated_at": _now(),
                "plan": {
                    "schema_version": "fact-integration-probe-v1",
                    "scenario_count": 3,
                },
                "runtime": {"adapter": "http", "runs": 3},
                "passed": True,
                "summary": {
                    "checks_total": 100,
                    "checks_passed": 100,
                    "scenario_consistency_rate": 1.0,
                    "latency_within_limit": True,
                    "per_source": {
                        source: {"calls": 9, "pass_rate": 1.0}
                        for source in ("order", "payment", "logistics", "aftersale")
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = module._fact_integration_status(path)
    gates = module._external_gates(fact_integration=result)

    assert result["gate_status"] == "verified_external"
    fact_gate = next(item for item in gates if item["key"] == "fact_platform_sandbox")
    assert fact_gate["status"] == "verified_external"
    assert all(
        item["status"] == "blocked_external"
        for item in gates
        if item["key"] != "fact_platform_sandbox"
    )


def test_fact_evidence_with_too_few_runs_stays_blocked(tmp_path):
    path = tmp_path / "fact.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "fact-integration-probe-v1",
                "generated_at": _now(),
                "plan": {
                    "schema_version": "fact-integration-probe-v1",
                    "scenario_count": 3,
                },
                "runtime": {"adapter": "http", "runs": 1},
                "passed": True,
                "summary": {
                    "checks_total": 4,
                    "checks_passed": 4,
                    "scenario_consistency_rate": 1.0,
                    "latency_within_limit": True,
                    "per_source": {
                        source: {"calls": 1, "pass_rate": 1.0}
                        for source in ("order", "payment", "logistics", "aftersale")
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    result = module._fact_integration_status(path)

    assert result["gate_status"] == "blocked_external"


def _metric_source_payload(*, app_env: str = "prod", runs: int = 3):
    return {
        "schema_version": "metric-source-probe-v1",
        "generated_at": _now(),
        "read_only": True,
        "external_network_used": True,
        "production_ready": False,
        "status": "verified_read_only",
        "configuration": {
            "app_env": app_env,
            "enabled": True,
            "read_method": "GET",
            "https": True,
            "auth_configured": True,
            "tenant_scope_provided": True,
            "metric_whitelist_count": 2,
            "runs_requested": runs,
        },
        "summary": {
            "runs_requested": runs,
            "runs_completed": runs,
            "row_count_min": 10,
            "row_count_max": 10,
            "dataset_version_present": True,
            "source_version_present": True,
            "metric_coverage_complete": True,
            "latency_limit_ms": 5000,
            "latency_p95_ms": 120,
            "latency_max_ms": 125,
            "latency_within_limit": True,
        },
        "runs": [
            {
                "run": index + 1,
                "latency_ms": 100 + index,
                "row_count": 10,
                "dataset_version_present": True,
                "source_version_present": True,
                "metric_names": ["crash_rate", "next_day_retention"],
            }
            for index in range(runs)
        ],
    }


def test_real_metric_source_evidence_clears_only_metric_source_gate(tmp_path):
    path = tmp_path / "metric-source.json"
    path.write_text(json.dumps(_metric_source_payload()), encoding="utf-8")

    result = module._metric_source_status(path)
    gates = module._external_gates(metric_source=result)

    assert result["gate_status"] == "verified_external"
    metric_gate = next(item for item in gates if item["key"] == "metric_source")
    assert metric_gate["status"] == "verified_external"
    assert all(
        item["status"] == "blocked_external"
        for item in gates
        if item["key"] != "metric_source"
    )


def test_metric_source_local_or_single_run_evidence_stays_blocked(tmp_path):
    path = tmp_path / "metric-source.json"
    path.write_text(
        json.dumps(_metric_source_payload(app_env="local", runs=1)),
        encoding="utf-8",
    )

    result = module._metric_source_status(path)

    assert result["gate_status"] == "blocked_external"
    assert result["runs_completed"] == 1


def test_malformed_or_timezone_less_fact_evidence_fails_closed(tmp_path):
    path = tmp_path / "fact.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "fact-integration-probe-v1",
                "generated_at": "2026-11-07T10:00:00",
                "plan": {
                    "schema_version": "fact-integration-probe-v1",
                    "scenario_count": "three",
                },
                "runtime": {"adapter": "http", "runs": "three"},
                "passed": True,
                "summary": {
                    "checks_total": "100",
                    "checks_passed": "100",
                    "scenario_consistency_rate": 1.0,
                    "latency_within_limit": True,
                    "per_source": {},
                },
            }
        ),
        encoding="utf-8",
    )

    result = module._fact_integration_status(path)

    assert result["gate_status"] == "blocked_external"
    assert result["fresh"] is False


def test_real_webhook_delivery_can_clear_only_message_channel_gate(tmp_path):
    path = tmp_path / "webhook.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "webhook-integration-probe-v1",
                "generated_at": _now(),
                "configuration": {"passed": True},
                "local_contract": {
                    "executed": True,
                    "passed": True,
                    "external_network_used": False,
                },
                "external_delivery": {"executed": True, "passed": True},
                "production_ready": True,
            }
        ),
        encoding="utf-8",
    )

    result = module._webhook_integration_status(path)
    gates = module._external_gates(webhook_integration=result)

    assert result["gate_status"] == "verified_external"
    message_gate = next(item for item in gates if item["key"] == "message_channel")
    assert message_gate["status"] == "verified_external"
    assert all(
        item["status"] == "blocked_external"
        for item in gates
        if item["key"] != "message_channel"
    )


def test_loopback_webhook_evidence_never_clears_message_channel_gate(tmp_path):
    path = tmp_path / "webhook.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "webhook-integration-probe-v1",
                "generated_at": _now(),
                "configuration": {"passed": True},
                "local_contract": {
                    "executed": True,
                    "passed": True,
                    "external_network_used": False,
                },
                "external_delivery": {"executed": False, "passed": False},
                "production_ready": False,
            }
        ),
        encoding="utf-8",
    )

    result = module._webhook_integration_status(path)

    assert result["gate_status"] == "blocked_external"


def _holdout_payload(*, include_details: bool = False, acceptance_passed: bool = True):
    checks = {
        "external_holdout": True,
        "case_count_at_least_200": True,
        "real_model_provider": True,
        "real_fact_adapter": True,
        "at_least_three_runs": True,
        "runs_completed": True,
        "model_calls_recorded": True,
        "prices_complete": True,
        "safety_metrics_pass": True,
        "thresholds_configured": True,
        "pass_rate_threshold": True,
        "consistency_threshold": True,
        "evidence_support_threshold": True,
        "latency_threshold": True,
    }
    if not acceptance_passed:
        checks["pass_rate_threshold"] = False
    runs = [
        {
            "status": "completed",
            "case_count": 200,
            "metrics": {"pass_rate": 0.9},
        }
        for _ in range(3)
    ]
    if include_details:
        runs[0]["details"] = [{"case_id": "external-001"}]
    return {
        "schema_version": "ticket-evaluation-evidence-v1",
        "generated_at": _now(),
        "dataset": {
            "kind": "external_holdout",
            "case_count": 200,
            "sha256": "a" * 64,
        },
        "runtime": {
            "llm_provider": "openai_compatible",
            "fact_adapter": "http",
        },
        "summary": {"run_count": 3, "model_calls": 600},
        "acceptance": {
            "status": "verified" if acceptance_passed else "failed",
            "passed": acceptance_passed,
            "checks": checks,
        },
        "runs": runs,
    }


def test_accepted_real_model_holdout_can_clear_only_holdout_gate(tmp_path):
    path = tmp_path / "holdout.json"
    path.write_text(json.dumps(_holdout_payload()), encoding="utf-8")

    result = module._real_model_holdout_status(path)
    gates = module._external_gates(real_model_holdout=result)

    assert result["gate_status"] == "verified_external"
    holdout = next(item for item in gates if item["key"] == "real_model_holdout")
    assert holdout["status"] == "verified_external"
    assert all(
        item["status"] == "blocked_external"
        for item in gates
        if item["key"] != "real_model_holdout"
    )


def test_holdout_with_case_details_or_failed_thresholds_stays_blocked(tmp_path):
    path = tmp_path / "holdout.json"
    path.write_text(
        json.dumps(_holdout_payload(include_details=True, acceptance_passed=False)),
        encoding="utf-8",
    )

    result = module._real_model_holdout_status(path)

    assert result["gate_status"] == "blocked_external"


def _release_snapshot(*, machine_ready: bool = True):
    statuses = "verified_external" if machine_ready else "blocked_external"
    return module._release_evidence_snapshot(
        strict_summary={
            "fail": 0 if machine_ready else 1,
            "failed_checks": [] if machine_ready else ["LLM"],
        },
        source_control={"commit_sha": "c" * 40, "traceable": machine_ready},
        adapter_mode="http" if machine_ready else "mock",
        production_shape={"local_production_shape_verified": machine_ready},
        policy_retrieval={"policy_retrieval_verified": machine_ready},
        target_environment={
            "cross_host_or_target_environment_verified": machine_ready
        },
        target_release_admission={
            "machine_admission_ready": machine_ready,
            "commit_matches": machine_ready,
        },
        external_gates=[
            {"key": key, "status": statuses}
            for key in (
                "fact_platform_sandbox",
                "metric_source",
                "message_channel",
                "production_infrastructure",
                "real_model_holdout",
                "usability_test",
            )
        ],
        evidence_hashes={f"evidence-{key}": "d" * 64 for key in range(7)},
    )


def _release_decision_payload(snapshot, *, decision="approve", evidence_sha=None):
    return {
        "schema_version": "after-sales-release-decision-v1",
        "generated_at": _now(),
        "decision": decision,
        "approver_role": "release_manager",
        "approval_reference": "CHANGE-2026-1107",
        "commit_sha": "c" * 40,
        "evidence_sha256": evidence_sha or snapshot["sha256"],
        "risk_acceptance": "已核对剩余风险、监控指标和回滚触发条件。",
        "rollback_confirmed": True,
    }


def test_release_evidence_snapshot_has_stable_machine_gate_hash():
    first = _release_snapshot()
    second = _release_snapshot()

    assert first["machine_gates_ready"] is True
    assert first["target_release_admission_verified"] is True
    assert first["schema_version"] == "after-sales-release-evidence-v2"
    assert first["sha256"] == second["sha256"]
    assert len(first["sha256"]) == 64


def test_matching_human_release_approval_can_clear_final_gate(tmp_path):
    snapshot = _release_snapshot()
    path = tmp_path / "release-decision.json"
    path.write_text(
        json.dumps(_release_decision_payload(snapshot), ensure_ascii=False),
        encoding="utf-8",
    )

    result = module._release_decision_status(
        evidence_sha256=snapshot["sha256"],
        commit_sha="c" * 40,
        machine_gates_ready=True,
        evidence_path=path,
    )

    assert result["status"] == "approved"
    assert result["gate_status"] == "verified_human"
    assert result["commit_matches"] is True
    assert result["evidence_matches"] is True
    assert "approval_reference" not in result
    assert "risk_acceptance" not in result


def test_release_approval_with_changed_evidence_fails_closed(tmp_path):
    snapshot = _release_snapshot()
    path = tmp_path / "release-decision.json"
    path.write_text(
        json.dumps(
            _release_decision_payload(snapshot, evidence_sha="e" * 64),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = module._release_decision_status(
        evidence_sha256=snapshot["sha256"],
        commit_sha="c" * 40,
        machine_gates_ready=True,
        evidence_path=path,
    )

    assert result["status"] == "not_verified"
    assert result["gate_status"] == "blocked_human"
    assert result["evidence_matches"] is False


def test_rejection_and_premature_approval_never_mark_release_ready(tmp_path):
    ready_snapshot = _release_snapshot()
    rejected = tmp_path / "rejected.json"
    rejected.write_text(
        json.dumps(
            _release_decision_payload(ready_snapshot, decision="reject"),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rejected_result = module._release_decision_status(
        evidence_sha256=ready_snapshot["sha256"],
        commit_sha="c" * 40,
        machine_gates_ready=True,
        evidence_path=rejected,
    )

    blocked_snapshot = _release_snapshot(machine_ready=False)
    premature = tmp_path / "premature.json"
    premature.write_text(
        json.dumps(_release_decision_payload(blocked_snapshot), ensure_ascii=False),
        encoding="utf-8",
    )
    premature_result = module._release_decision_status(
        evidence_sha256=blocked_snapshot["sha256"],
        commit_sha="c" * 40,
        machine_gates_ready=False,
        evidence_path=premature,
    )

    assert rejected_result["status"] == "rejected"
    assert rejected_result["gate_status"] == "blocked_human"
    assert premature_result["status"] == "premature_approval"
    assert premature_result["gate_status"] == "blocked_human"


def test_source_control_status_reports_clean_synced_commit(monkeypatch):
    responses = {
        ("rev-parse", "--is-inside-work-tree"): (0, "true\n"),
        ("rev-parse", "HEAD"): (0, "a" * 40 + "\n"),
        ("branch", "--show-current"): (0, "main\n"),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): (0, "origin/main\n"),
        ("rev-list", "--left-right", "--count", "HEAD...@{upstream}"): (0, "0\t0\n"),
        ("diff", "--quiet", "--ignore-submodules", "--"): (0, ""),
        ("diff", "--cached", "--quiet", "--ignore-submodules", "--"): (0, ""),
        ("ls-files", "--others", "--exclude-standard", "-z"): (0, ""),
    }

    def fake_run(command, **_kwargs):
        code, stdout = responses[tuple(command[1:])]
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module._source_control_status()

    assert result["repository_detected"] is True
    assert result["commit_sha"] == "a" * 40
    assert result["branch"] == "main"
    assert result["upstream"] == "origin/main"
    assert result["clean"] is True
    assert result["upstream_synced"] is True
    assert result["traceable"] is True
    assert result["blockers"] == []


def test_source_control_status_accepts_evidence_only_commits_after_candidate(monkeypatch):
    candidate = "c" * 40
    head = "d" * 40
    responses = {
        ("rev-parse", "--is-inside-work-tree"): (0, "true\n"),
        ("rev-parse", "HEAD"): (0, head + "\n"),
        ("branch", "--show-current"): (0, "main\n"),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): (0, "origin/main\n"),
        ("rev-list", "--left-right", "--count", "HEAD...@{upstream}"): (0, "0\t0\n"),
        ("diff", "--quiet", "--ignore-submodules", "--"): (0, ""),
        ("diff", "--cached", "--quiet", "--ignore-submodules", "--"): (0, ""),
        ("ls-files", "--others", "--exclude-standard", "-z"): (0, ""),
        ("merge-base", "--is-ancestor", candidate, "HEAD"): (0, ""),
        ("diff", "--name-only", f"{candidate}..HEAD", "--"): (
            0,
            "docs/testing/artifacts/target-release-admission.json\n"
            "docs/testing/单一候选完整证据再生P3-34-2026-11-10.tdd.md\n"
            "docs/product-redesign/Durable-Agent运行改造开发日志-2026-11-07.md\n",
        ),
        ("rev-list", "--count", f"{candidate}..HEAD"): (0, "1\n"),
    }

    def fake_run(command, **_kwargs):
        code, stdout = responses[tuple(command[1:])]
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module._source_control_status(expected_release_sha=candidate)

    assert result["commit_sha"] == candidate
    assert result["head_commit_sha"] == head
    assert result["head_matches_candidate"] is False
    assert result["candidate_is_ancestor"] is True
    assert result["evidence_only_paths_valid"] is True
    assert result["evidence_only_commit_count"] == 1
    assert result["traceable"] is True
    assert result["blockers"] == []


def test_source_control_status_rejects_non_evidence_change_after_candidate(monkeypatch):
    candidate = "c" * 40
    head = "d" * 40
    responses = {
        ("rev-parse", "--is-inside-work-tree"): (0, "true\n"),
        ("rev-parse", "HEAD"): (0, head + "\n"),
        ("branch", "--show-current"): (0, "main\n"),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): (0, "origin/main\n"),
        ("rev-list", "--left-right", "--count", "HEAD...@{upstream}"): (0, "0\t0\n"),
        ("diff", "--quiet", "--ignore-submodules", "--"): (0, ""),
        ("diff", "--cached", "--quiet", "--ignore-submodules", "--"): (0, ""),
        ("ls-files", "--others", "--exclude-standard", "-z"): (0, ""),
        ("merge-base", "--is-ancestor", candidate, "HEAD"): (0, ""),
        ("diff", "--name-only", f"{candidate}..HEAD", "--"): (
            0,
            "docs/testing/artifacts/target-release-admission.json\n"
            "backend/app/main.py\n",
        ),
        ("rev-list", "--count", f"{candidate}..HEAD"): (0, "1\n"),
    }

    def fake_run(command, **_kwargs):
        code, stdout = responses[tuple(command[1:])]
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module._source_control_status(expected_release_sha=candidate)

    assert result["candidate_is_ancestor"] is True
    assert result["evidence_only_paths_valid"] is False
    assert result["traceable"] is False
    assert result["blockers"] == ["candidate_non_evidence_changes"]


def test_source_control_status_blocks_dirty_or_unsynced_worktree(monkeypatch):
    responses = {
        ("rev-parse", "--is-inside-work-tree"): (0, "true\n"),
        ("rev-parse", "HEAD"): (0, "b" * 40 + "\n"),
        ("branch", "--show-current"): (0, "main\n"),
        ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): (0, "origin/main\n"),
        ("rev-list", "--left-right", "--count", "HEAD...@{upstream}"): (0, "2\t1\n"),
        ("diff", "--quiet", "--ignore-submodules", "--"): (1, ""),
        ("diff", "--cached", "--quiet", "--ignore-submodules", "--"): (1, ""),
        ("ls-files", "--others", "--exclude-standard", "-z"): (0, "one.txt\0two.txt\0"),
    }

    def fake_run(command, **_kwargs):
        code, stdout = responses[tuple(command[1:])]
        return subprocess.CompletedProcess(command, code, stdout=stdout, stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module._source_control_status()

    assert result["clean"] is False
    assert result["unstaged_changes"] is True
    assert result["staged_changes"] is True
    assert result["untracked_file_count"] == 2
    assert result["ahead"] == 2
    assert result["behind"] == 1
    assert result["traceable"] is False
    assert result["blockers"] == ["working_tree_dirty", "upstream_not_synced"]


def test_source_control_status_fails_closed_outside_git(monkeypatch):
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 128, stdout="", stderr="not a repository"
        ),
    )

    result = module._source_control_status()

    assert result["repository_detected"] is False
    assert result["traceable"] is False
    assert result["blockers"] == ["repository_unavailable"]


def _template_payload():
    validator = module._load_usability_validator()
    return validator.template_payload()


def test_usability_record_empty_template_is_valid_but_not_complete(tmp_path):
    path = tmp_path / "record.json"
    path.write_text(json.dumps(_template_payload(), ensure_ascii=False), encoding="utf-8")

    result = module._usability_record_status(path)

    assert result["status"] == "template_valid_unfilled"
    assert result["template_valid"] is True
    assert result["real_observations_present"] is False
    assert result["independent_completion_rate"] is None
    assert result["gate_status"] == "blocked_external"


def test_usability_record_reports_observations_without_claiming_readiness(tmp_path):
    payload = _template_payload()
    payload["tasks"][0].update({"completed": True, "independent": True, "duration_seconds": 42})
    payload["conclusion"] = "需要继续观察"
    path = tmp_path / "record.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = module._usability_record_status(path)

    assert result["status"] == "observations_present"
    assert result["observed_task_count"] == 1
    assert result["independent_completion_rate"] == 1.0
    assert result["conclusion_provided"] is True
    assert result["remediation_retest_present"] is False
    assert result["gate_status"] == "blocked_external"


def test_usability_record_invalid_matrix_is_not_treated_as_evidence(tmp_path):
    payload = _template_payload()
    payload["tasks"] = payload["tasks"][:-1]
    path = tmp_path / "record.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = module._usability_record_status(path)

    assert result["status"] == "invalid"
    assert result["template_valid"] is False
    assert result["real_observations_present"] is False


def test_complete_non_developer_usability_record_can_clear_only_usability_gate(tmp_path):
    payload = _template_payload()
    payload["test_date"] = "2026-11-07"
    payload["facilitator"] = "external-facilitator"
    payload["participant_scope_confirmed"] = True
    payload["conclusion"] = "完成全量观察，没有未解决 P0/P1。"
    for participant in payload["participants"]:
        participant["is_developer"] = False
    for task in payload["tasks"]:
        task.update({"completed": True, "independent": True, "duration_seconds": 60})
    path = tmp_path / "record.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = module._usability_record_status(path)
    gates = module._external_gates({}, result)

    assert result["gate_status"] == "verified_external"
    assert result["all_tasks_fully_observed"] is True
    usability = next(item for item in gates if item["key"] == "usability_test")
    assert usability["status"] == "verified_external"
    assert all(
        item["status"] == "blocked_external"
        for item in gates
        if item["key"] != "usability_test"
    )


def _patch_build_report_dependencies(
    monkeypatch,
    *,
    machine_gates_ready: bool,
    release_decision: dict,
) -> None:
    import app.config as config_module

    monkeypatch.setattr(
        config_module,
        "get_settings",
        lambda: SimpleNamespace(app_env="test", ticket_use_mock_adapters=False),
    )
    monkeypatch.setattr(
        module,
        "_load_preflight_module",
        lambda: SimpleNamespace(run_checks=lambda *_args, **_kwargs: []),
    )
    summary = {"total": 1, "pass": 1, "warn": 0, "fail": 0, "failed_checks": []}
    monkeypatch.setattr(module, "summarize_checks", lambda _checks: dict(summary))
    monkeypatch.setattr(module, "_fact_plan_status", lambda: {"status": "pass"})
    monkeypatch.setattr(module, "_fact_integration_status", lambda: {"status": "missing"})
    monkeypatch.setattr(module, "_webhook_integration_status", lambda: {"status": "missing"})
    monkeypatch.setattr(module, "_real_model_holdout_status", lambda: {"status": "missing"})
    monkeypatch.setattr(module, "_metric_source_status", lambda: {"status": "missing"})
    monkeypatch.setattr(module, "_demo_ticket_status", lambda: {"status": "pass"})
    monkeypatch.setattr(
        module,
        "_local_production_shape_status",
        lambda: {"local_production_shape_verified": True},
    )
    monkeypatch.setattr(
        module,
        "_policy_retrieval_benchmark_status",
        lambda _path=None: {"policy_retrieval_verified": True},
    )
    monkeypatch.setattr(
        module,
        "_scheduler_failover_status",
        lambda: {"local_postgres_failover_verified": False},
    )
    monkeypatch.setattr(
        module,
        "_target_environment_status",
        lambda: {"cross_host_or_target_environment_verified": False},
    )
    monkeypatch.setattr(module, "_usability_record_status", lambda: {"status": "missing"})
    monkeypatch.setattr(
        module,
        "_source_control_status",
        lambda **_kwargs: {"commit_sha": "a" * 40},
    )
    monkeypatch.setattr(
        module,
        "_target_release_admission_status",
        lambda *_args, **_kwargs: {"machine_admission_ready": False},
    )
    monkeypatch.setattr(module, "_external_gates", lambda **_kwargs: [])
    monkeypatch.setattr(module, "_latest_production_load_evidence", lambda: None)
    monkeypatch.setattr(module, "_sha256_file", lambda _path: "b" * 64)
    monkeypatch.setattr(
        module,
        "_release_evidence_snapshot",
        lambda **_kwargs: {
            "sha256": "c" * 64,
            "machine_gates_ready": machine_gates_ready,
        },
    )
    monkeypatch.setattr(
        module,
        "_release_decision_status",
        lambda **_kwargs: dict(release_decision),
    )


def test_build_report_composes_local_evidence_without_claiming_production(monkeypatch):
    _patch_build_report_dependencies(
        monkeypatch,
        machine_gates_ready=False,
        release_decision={"status": "missing", "gate_status": "blocked_human"},
    )

    report = module.build_report()

    assert report["environment"] == {"app_env": "test", "adapter_mode": "http"}
    assert report["local_evidence"]["local_ready_for_production_config"] is True
    assert report["production_ready"] is False
    assert report["decision"] == "not_ready_machine_gates"


def test_build_report_requires_verified_human_decision_after_machine_gates(monkeypatch):
    _patch_build_report_dependencies(
        monkeypatch,
        machine_gates_ready=True,
        release_decision={"status": "missing", "gate_status": "blocked_human"},
    )
    awaiting = module.build_report()

    _patch_build_report_dependencies(
        monkeypatch,
        machine_gates_ready=True,
        release_decision={"status": "approved", "gate_status": "verified_human"},
    )
    approved = module.build_report()

    assert awaiting["production_ready"] is False
    assert awaiting["decision"] == "awaiting_release_approval"
    assert approved["production_ready"] is True
    assert approved["decision"] == "ready_for_release"


def test_release_report_cli_writes_json_and_fails_closed_when_not_ready(
    monkeypatch, tmp_path
):
    report = {
        "production_ready": False,
        "local_evidence": {"source_control": {"commit_sha": "e" * 40}},
        "release_evidence": {"sha256": "d" * 64},
    }
    monkeypatch.setattr(module, "build_report", lambda **_kwargs: report)
    output = tmp_path / "release.json"
    decision_template = tmp_path / "release-decision.json"

    exit_code = module.main(
        [
            "--json-out",
            str(output),
            "--decision-template-out",
            str(decision_template),
            "--fail-if-not-ready",
        ]
    )

    assert exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8")) == report
    template = json.loads(decision_template.read_text(encoding="utf-8"))
    assert template["decision"] == "pending"
    assert template["commit_sha"] == "e" * 40
    assert template["evidence_sha256"] == "d" * 64


def test_latest_production_load_evidence_ignores_smoke_and_selects_newest(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(module, "PRODUCTION_LOAD_EVIDENCE_DIR", tmp_path)
    older = tmp_path / "ticket-production-load-older.json"
    newest = tmp_path / "ticket-production-load-newest.json"
    smoke = tmp_path / "ticket-production-load-smoke.json"
    for path in (older, newest, smoke):
        path.write_text("{}", encoding="utf-8")
    os.utime(older, (1, 1))
    os.utime(newest, (2, 2))
    os.utime(smoke, (3, 3))

    assert module._latest_production_load_evidence() == newest


def test_evidence_helpers_hash_files_and_fail_closed_for_bad_payloads(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text(json.dumps({"generated_at": _now()}), encoding="utf-8")
    expected_hash = hashlib.sha256(valid.read_bytes()).hexdigest()

    payload, base = module._read_evidence(valid)
    assert payload is not None
    assert base["fresh"] is True
    assert module._sha256_file(valid) == expected_hash
    assert module._sha256_file(tmp_path / "missing.json") is None

    missing_payload, missing = module._read_evidence(tmp_path / "missing.json")
    assert missing_payload is None
    assert missing["status"] == "missing"

    unreadable = tmp_path / "unreadable.json"
    unreadable.write_text("{", encoding="utf-8")
    unreadable_payload, unreadable_status = module._read_evidence(unreadable)
    assert unreadable_payload is None
    assert unreadable_status["status"] == "unreadable"

    non_object = tmp_path / "non-object.json"
    non_object.write_text("[]", encoding="utf-8")
    invalid_payload, invalid_status = module._read_evidence(non_object)
    assert invalid_payload is None
    assert invalid_status["status"] == "invalid"


def test_policy_retrieval_rejects_malformed_case_variants(tmp_path):
    path = tmp_path / "policy.json"
    variants = []

    non_object = _policy_benchmark_payload()
    non_object["cases"][0] = "invalid"
    variants.append(non_object)

    missing_query = _policy_benchmark_payload()
    missing_query["cases"][0]["query"] = ""
    variants.append(missing_query)

    invalid_hit_rank = _policy_benchmark_payload()
    invalid_hit_rank["cases"][0]["rank"] = 99
    variants.append(invalid_hit_rank)

    unexpected_miss_rank = _policy_benchmark_payload(recall=0.5)
    unexpected_miss_rank["cases"][-1]["rank"] = 2
    variants.append(unexpected_miss_rank)

    for payload in variants:
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert module._policy_retrieval_benchmark_status(path)[
            "policy_retrieval_verified"
        ] is False


def test_missing_or_unreadable_evidence_paths_remain_blocked(tmp_path):
    missing = tmp_path / "missing.json"

    assert module._scheduler_failover_status(missing)[
        "local_postgres_failover_verified"
    ] is False
    assert module._metric_source_status(missing)["status"] == "missing"
    assert module._fact_integration_status(missing)["status"] == "missing"
    assert module._webhook_integration_status(missing)["status"] == "missing"
    assert module._real_model_holdout_status(missing)["status"] == "missing"
    assert module._release_decision_status(
        evidence_sha256="b" * 64,
        commit_sha="a" * 40,
        machine_gates_ready=False,
        evidence_path=missing,
    )["status"] == "missing"
    assert module._target_environment_status(missing)["status"] == "missing"
    assert module._target_release_admission_status(
        missing, expected_commit_sha="a" * 40
    )["machine_admission_ready"] is False
    assert module._local_production_shape_status(missing)["status"] == "missing"
    assert module._usability_record_status(missing)["status"] == "missing"
    assert module._task_has_observation("not-a-task") is False

    unreadable = tmp_path / "unreadable.json"
    unreadable.write_text("{", encoding="utf-8")
    assert module._local_production_shape_status(unreadable)["status"] == "unreadable"
    assert module._usability_record_status(unreadable)["status"] == "unreadable"


def test_build_report_distinguishes_rejected_and_invalid_human_decisions(monkeypatch):
    _patch_build_report_dependencies(
        monkeypatch,
        machine_gates_ready=True,
        release_decision={"status": "rejected", "gate_status": "blocked_human"},
    )
    rejected = module.build_report()

    _patch_build_report_dependencies(
        monkeypatch,
        machine_gates_ready=True,
        release_decision={"status": "not_verified", "gate_status": "blocked_human"},
    )
    invalid = module.build_report()

    assert rejected["production_ready"] is False
    assert rejected["decision"] == "release_rejected"
    assert invalid["production_ready"] is False
    assert invalid["decision"] == "release_decision_invalid"


def test_release_report_cli_refuses_to_overwrite_decision_template(monkeypatch, tmp_path):
    report = {
        "production_ready": False,
        "local_evidence": {"source_control": {"commit_sha": "e" * 40}},
        "release_evidence": {"sha256": "d" * 64},
    }
    monkeypatch.setattr(module, "build_report", lambda **_kwargs: report)
    decision_template = tmp_path / "release-decision.json"
    decision_template.write_text("existing", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        module.main(["--decision-template-out", str(decision_template)])

    assert exc_info.value.code == 2
    assert decision_template.read_text(encoding="utf-8") == "existing"
