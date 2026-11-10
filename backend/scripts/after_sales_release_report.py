"""Generate an honest, identifier-free after-sales release readiness report.

The report distinguishes local evidence from external gates. It never calls a
real vendor endpoint and it never treats mock/stub results as production proof.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

REQUIRED_TICKETS = (
    "TK-260924-0181",
    "TK-260924-0184",
    "TK-260924-0186",
)

USABILITY_RECORD_PATH = ROOT / "docs" / "product-redesign" / "客服可用性测试记录.json"
PRODUCTION_LOAD_EVIDENCE_DIR = ROOT / "docs" / "testing" / "artifacts"
TARGET_ENVIRONMENT_EVIDENCE_PATH = (
    PRODUCTION_LOAD_EVIDENCE_DIR / "target-environment-result.json"
)
TARGET_RELEASE_ADMISSION_EVIDENCE_PATH = (
    PRODUCTION_LOAD_EVIDENCE_DIR / "target-release-admission.json"
)
SCHEDULER_FAILOVER_EVIDENCE_PATH = (
    PRODUCTION_LOAD_EVIDENCE_DIR / "scheduler-failover-local-check.json"
)
FACT_INTEGRATION_EVIDENCE_PATH = PRODUCTION_LOAD_EVIDENCE_DIR / "fact-integration-result.json"
WEBHOOK_INTEGRATION_EVIDENCE_PATH = (
    PRODUCTION_LOAD_EVIDENCE_DIR / "webhook-integration-result.json"
)
REAL_MODEL_HOLDOUT_EVIDENCE_PATH = (
    PRODUCTION_LOAD_EVIDENCE_DIR / "real-model-holdout-result.json"
)
METRIC_SOURCE_EVIDENCE_PATH = PRODUCTION_LOAD_EVIDENCE_DIR / "metric-source-result.json"
RELEASE_DECISION_EVIDENCE_PATH = PRODUCTION_LOAD_EVIDENCE_DIR / "release-decision.json"
POLICY_RETRIEVAL_BENCHMARK_EVIDENCE_PATH = (
    PRODUCTION_LOAD_EVIDENCE_DIR / "policy-retrieval-benchmark.json"
)
LIVE_EVIDENCE_MAX_AGE_HOURS = 24
HOLDOUT_EVIDENCE_MAX_AGE_HOURS = 168
RELEASE_DECISION_MAX_AGE_HOURS = 24
POLICY_RETRIEVAL_MIN_CASES = 8
POLICY_RETRIEVAL_TOP_K = 5
POLICY_RETRIEVAL_MIN_RECALL = 0.8
RELEASE_APPROVER_ROLES = {
    "business_owner",
    "technical_owner",
    "operations_owner",
    "release_manager",
}


def _latest_production_load_evidence() -> Path | None:
    candidates = [
        path
        for path in PRODUCTION_LOAD_EVIDENCE_DIR.glob("ticket-production-load-*.json")
        if "smoke" not in path.stem.lower()
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)

EXTERNAL_GATES = (
    {
        "key": "fact_platform_sandbox",
        "status": "blocked_external",
        "required": "OMS/支付/物流/售后 sandbox、匿名测试订单、只读授权和真实端点",
    },
    {
        "key": "metric_source",
        "status": "blocked_external",
        "required": "生产 HTTPS 指标 API、服务端鉴权、隔离测试租户和至少 3 次连续只读 SLA 验证",
    },
    {
        "key": "message_channel",
        "status": "blocked_external",
        "required": "企业微信/飞书/短信或实际客户消息发送渠道与回滚开关",
    },
    {
        "key": "production_infrastructure",
        "status": "blocked_external",
        "required": "跨主机或目标部署环境中的多副本 API/Worker、托管依赖、故障切换和容量验收；本机 Docker 形态单独记录，不冒充目标环境验收",
    },
    {
        "key": "real_model_holdout",
        "status": "blocked_external",
        "required": "真实模型密钥/价格和至少 200 条独立人工标注脱敏 holdout",
    },
    {
        "key": "usability_test",
        "status": "blocked_external",
        "required": "至少 5 名非研发客服/主管的无指导测试记录和 P0/P1 整改回测",
    },
)


def _external_gates(
    target_environment: dict[str, Any] | None = None,
    usability: dict[str, Any] | None = None,
    fact_integration: dict[str, Any] | None = None,
    webhook_integration: dict[str, Any] | None = None,
    real_model_holdout: dict[str, Any] | None = None,
    metric_source: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    gates = [dict(item) for item in EXTERNAL_GATES]
    evidence_by_gate = {
        "fact_platform_sandbox": fact_integration,
        "message_channel": webhook_integration,
        "real_model_holdout": real_model_holdout,
        "metric_source": metric_source,
    }
    for key, evidence in evidence_by_gate.items():
        if (evidence or {}).get("gate_status") != "verified_external":
            continue
        for item in gates:
            if item["key"] == key:
                item["status"] = "verified_external"
                item["evidence"] = (evidence or {}).get("evidence_path")
                break
    if (target_environment or {}).get("cross_host_or_target_environment_verified") is True:
        for item in gates:
            if item["key"] == "production_infrastructure":
                item["status"] = "verified_external"
                item["evidence"] = (target_environment or {}).get("evidence_path")
                break
    if (usability or {}).get("gate_status") == "verified_external":
        for item in gates:
            if item["key"] == "usability_test":
                item["status"] = "verified_external"
                item["evidence"] = (usability or {}).get("record_path")
                break
    return gates


def _evidence_path(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else path.name


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _sha256_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scheduler_failover_status(path: Path | None = None) -> dict[str, Any]:
    evidence_path = path or SCHEDULER_FAILOVER_EVIDENCE_PATH
    payload, base = _read_evidence(evidence_path)
    result = {
        **base,
        "gate_status": "informational_local",
        "local_postgres_failover_verified": False,
        "cross_host_failover_verified": False,
    }
    if payload is None:
        return result

    required_checks = {
        "holder_acquired",
        "concurrent_contender_blocked",
        "crashed_holder_did_not_release",
        "takeover_after_expiry",
        "takeover_released",
        "rehearsal_row_cleaned",
    }
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    verified = all(
        (
            base["fresh"] is True,
            payload.get("schema_version") == "scheduler-failover-rehearsal-v1",
            payload.get("executed") is True,
            payload.get("passed") is True,
            payload.get("scope") in {None, "local_single_host"},
            payload.get("database_backend") == "postgresql",
            payload.get("process_isolation") is True,
            required_checks.issubset(checks),
            all(checks.get(name) is True for name in required_checks),
            payload.get("local_postgres_failover_verified") is True,
            payload.get("cross_host_failover_verified") is False,
            payload.get("production_ready") is False,
        )
    )
    return {
        **result,
        "status": "verified_local" if verified else "not_verified",
        "local_postgres_failover_verified": verified,
        "cross_host_failover_verified": False,
        "lease_seconds": _safe_nonnegative_int(payload.get("lease_seconds")),
        "takeover_wait_seconds": payload.get("takeover_wait_seconds"),
    }


def _safe_nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _read_evidence(
    path: Path, *, max_age_hours: int = LIVE_EVIDENCE_MAX_AGE_HOURS
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    base = {
        "status": "missing",
        "gate_status": "blocked_external",
        "evidence_path": _evidence_path(path),
        "fresh": False,
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None, base
    except (OSError, json.JSONDecodeError) as exc:
        return None, {**base, "status": "unreadable", "error_type": type(exc).__name__}
    if not isinstance(payload, dict):
        return None, {**base, "status": "invalid"}
    try:
        generated = datetime.fromisoformat(
            str(payload.get("generated_at") or "").replace("Z", "+00:00")
        )
        if generated.tzinfo is None:
            raise ValueError("generated_at must include timezone")
        generated = generated.astimezone(timezone.utc)
        age = datetime.now(timezone.utc) - generated
        fresh = timedelta(0) <= age <= timedelta(hours=max_age_hours)
    except (TypeError, ValueError):
        fresh = False
    return payload, {**base, "status": "not_verified", "fresh": fresh}


def _policy_retrieval_benchmark_status(
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    """Validate the local Recall@5 gate without trusting summary fields alone."""

    path = evidence_path or POLICY_RETRIEVAL_BENCHMARK_EVIDENCE_PATH
    payload, base = _read_evidence(path)
    result = {
        **base,
        "gate_status": "blocked_local",
        "policy_retrieval_verified": False,
    }
    if payload is None:
        return result

    top_k = _safe_nonnegative_int(payload.get("top_k"))
    case_count = _safe_nonnegative_int(payload.get("case_count"))
    cases = payload.get("cases") if isinstance(payload.get("cases"), list) else []
    structurally_valid_cases = bool(cases) and len(cases) == case_count
    hits = 0
    reciprocal_rank = 0.0
    if structurally_valid_cases:
        for case in cases:
            if not isinstance(case, dict):
                structurally_valid_cases = False
                break
            query = str(case.get("query") or "").strip()
            expected_title = str(case.get("expected_title") or "").strip()
            hit = case.get("hit")
            rank = case.get("rank")
            if not query or not expected_title or not isinstance(hit, bool):
                structurally_valid_cases = False
                break
            if hit:
                if (
                    not isinstance(rank, int)
                    or isinstance(rank, bool)
                    or not 1 <= rank <= top_k
                ):
                    structurally_valid_cases = False
                    break
                hits += 1
                reciprocal_rank += 1.0 / rank
            elif rank is not None:
                structurally_valid_cases = False
                break

    recomputed_recall = hits / case_count if case_count else 0.0
    recomputed_mrr = reciprocal_rank / case_count if case_count else 0.0
    reported_recall = payload.get("recall_at_k")
    reported_mrr = payload.get("mrr")
    threshold = payload.get("threshold")
    embedding_stats_payload = payload.get("embedding_stats")
    embedding_stats = (
        embedding_stats_payload if isinstance(embedding_stats_payload, dict) else {}
    )
    embedding_stats_complete = isinstance(embedding_stats_payload, dict) and all(
        isinstance(embedding_stats.get(key), int)
        and not isinstance(embedding_stats.get(key), bool)
        and embedding_stats[key] >= 0
        for key in ("failures", "fallbacks")
    )
    numeric_summary = all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in (reported_recall, reported_mrr, threshold)
    )
    threshold_value = float(threshold) if numeric_summary else 0.0
    summary_matches_cases = bool(
        numeric_summary
        and 0.0 <= float(reported_recall) <= 1.0
        and 0.0 <= float(reported_mrr) <= 1.0
        and 0.0 <= threshold_value <= 1.0
        and abs(float(reported_recall) - recomputed_recall) <= 0.0001
        and abs(float(reported_mrr) - recomputed_mrr) <= 0.0001
    )
    verified = all(
        (
            base["fresh"] is True,
            payload.get("schema_version") == "policy-retrieval-benchmark-v1",
            payload.get("embedding_mode") == "stub",
            str(payload.get("embedding_model") or "").startswith("stub-"),
            payload.get("independent_holdout") is False,
            payload.get("production_ready") is False,
            payload.get("strict_evidence_ok") is True,
            embedding_stats_complete,
            _safe_nonnegative_int(embedding_stats.get("failures")) == 0,
            _safe_nonnegative_int(embedding_stats.get("fallbacks")) == 0,
            top_k == POLICY_RETRIEVAL_TOP_K,
            case_count >= POLICY_RETRIEVAL_MIN_CASES,
            structurally_valid_cases,
            summary_matches_cases,
            threshold_value >= POLICY_RETRIEVAL_MIN_RECALL,
            recomputed_recall >= threshold_value,
            payload.get("passed") is True,
        )
    )
    return {
        **result,
        "status": "verified_local" if verified else "not_verified",
        "gate_status": "verified_local" if verified else "blocked_local",
        "policy_retrieval_verified": verified,
        "embedding_mode": payload.get("embedding_mode"),
        "embedding_model": payload.get("embedding_model"),
        "top_k": top_k,
        "case_count": case_count,
        "recall_at_k": round(recomputed_recall, 4),
        "mrr": round(recomputed_mrr, 4),
        "minimum_recall": POLICY_RETRIEVAL_MIN_RECALL,
    }


def _metric_source_status(evidence_path: Path | None = None) -> dict[str, Any]:
    path = evidence_path or METRIC_SOURCE_EVIDENCE_PATH
    payload, base = _read_evidence(path)
    if payload is None:
        return base

    configuration = (
        payload.get("configuration")
        if isinstance(payload.get("configuration"), dict)
        else {}
    )
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    runs = payload.get("runs") if isinstance(payload.get("runs"), list) else []
    runs_requested = _safe_nonnegative_int(configuration.get("runs_requested"))
    runs_completed = _safe_nonnegative_int(summary.get("runs_completed"))
    latency_limit = summary.get("latency_limit_ms")
    latency_max = summary.get("latency_max_ms")
    latency_values_valid = all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0
        for value in (latency_limit, latency_max)
    )
    run_details_valid = len(runs) == runs_completed and all(
        isinstance(item, dict)
        and _safe_nonnegative_int(item.get("row_count")) >= 1
        and item.get("dataset_version_present") is True
        and item.get("source_version_present") is True
        and isinstance(item.get("metric_names"), list)
        and bool(item.get("metric_names"))
        for item in runs
    )
    verified = all(
        (
            payload.get("schema_version") == "metric-source-probe-v1",
            base["fresh"] is True,
            payload.get("read_only") is True,
            payload.get("external_network_used") is True,
            payload.get("production_ready") is False,
            payload.get("status") == "verified_read_only",
            configuration.get("app_env") == "prod",
            configuration.get("enabled") is True,
            configuration.get("read_method") == "GET",
            configuration.get("https") is True,
            configuration.get("auth_configured") is True,
            configuration.get("tenant_scope_provided") is True,
            _safe_nonnegative_int(configuration.get("metric_whitelist_count")) >= 1,
            runs_requested >= 3,
            runs_completed == runs_requested,
            _safe_nonnegative_int(summary.get("row_count_min")) >= 1,
            summary.get("dataset_version_present") is True,
            summary.get("source_version_present") is True,
            summary.get("metric_coverage_complete") is True,
            summary.get("latency_within_limit") is True,
            latency_values_valid,
            float(latency_max) <= float(latency_limit) if latency_values_valid else False,
            run_details_valid,
        )
    )
    return {
        **base,
        "status": "verified_external" if verified else "not_verified",
        "gate_status": "verified_external" if verified else "blocked_external",
        "read_only_verified": payload.get("read_only") is True,
        "runs_requested": runs_requested,
        "runs_completed": runs_completed,
        "row_count_min": _safe_nonnegative_int(summary.get("row_count_min")),
        "latency_p95_ms": summary.get("latency_p95_ms"),
        "latency_within_limit": summary.get("latency_within_limit") is True,
    }


def _fact_integration_status(evidence_path: Path | None = None) -> dict[str, Any]:
    path = evidence_path or FACT_INTEGRATION_EVIDENCE_PATH
    payload, base = _read_evidence(path)
    if payload is None:
        return base
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    per_source = summary.get("per_source") if isinstance(summary.get("per_source"), dict) else {}
    required_sources = {"order", "payment", "logistics", "aftersale"}
    sources_verified = set(per_source) == required_sources and all(
        isinstance(item, dict)
        and _safe_nonnegative_int(item.get("calls")) > 0
        and isinstance(item.get("pass_rate"), (int, float))
        and not isinstance(item.get("pass_rate"), bool)
        and float(item["pass_rate"]) == 1.0
        for item in per_source.values()
    )
    scenario_count = _safe_nonnegative_int(plan.get("scenario_count"))
    runs = _safe_nonnegative_int(runtime.get("runs"))
    checks_total = _safe_nonnegative_int(summary.get("checks_total"))
    checks_passed = _safe_nonnegative_int(summary.get("checks_passed"))
    verified = all(
        (
            payload.get("schema_version") == "fact-integration-probe-v1",
            base["fresh"] is True,
            payload.get("passed") is True,
            plan.get("schema_version") == "fact-integration-probe-v1",
            scenario_count >= 1,
            runs >= 3,
            str(runtime.get("adapter") or "").lower() not in {"", "mock"},
            checks_total > 0 and checks_total == checks_passed,
            summary.get("scenario_consistency_rate") == 1.0,
            summary.get("latency_within_limit") is True,
            sources_verified,
        )
    )
    return {
        **base,
        "status": "verified_external" if verified else "not_verified",
        "gate_status": "verified_external" if verified else "blocked_external",
        "scenario_count": scenario_count,
        "runs": runs,
        "checks_passed": checks_passed,
        "checks_total": checks_total,
    }


def _webhook_integration_status(evidence_path: Path | None = None) -> dict[str, Any]:
    path = evidence_path or WEBHOOK_INTEGRATION_EVIDENCE_PATH
    payload, base = _read_evidence(path)
    if payload is None:
        return base
    configuration = (
        payload.get("configuration") if isinstance(payload.get("configuration"), dict) else {}
    )
    local_contract = (
        payload.get("local_contract") if isinstance(payload.get("local_contract"), dict) else {}
    )
    external_delivery = (
        payload.get("external_delivery")
        if isinstance(payload.get("external_delivery"), dict)
        else {}
    )
    verified = all(
        (
            payload.get("schema_version") == "webhook-integration-probe-v1",
            base["fresh"] is True,
            configuration.get("passed") is True,
            local_contract.get("executed") is True,
            local_contract.get("passed") is True,
            local_contract.get("external_network_used") is False,
            external_delivery.get("executed") is True,
            external_delivery.get("passed") is True,
            payload.get("production_ready") is True,
        )
    )
    return {
        **base,
        "status": "verified_external" if verified else "not_verified",
        "gate_status": "verified_external" if verified else "blocked_external",
        "external_delivery_executed": external_delivery.get("executed") is True,
        "external_delivery_passed": external_delivery.get("passed") is True,
    }


def _real_model_holdout_status(evidence_path: Path | None = None) -> dict[str, Any]:
    path = evidence_path or REAL_MODEL_HOLDOUT_EVIDENCE_PATH
    payload, base = _read_evidence(path, max_age_hours=HOLDOUT_EVIDENCE_MAX_AGE_HOURS)
    if payload is None:
        return base
    dataset = payload.get("dataset") if isinstance(payload.get("dataset"), dict) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    acceptance = (
        payload.get("acceptance") if isinstance(payload.get("acceptance"), dict) else {}
    )
    checks = acceptance.get("checks") if isinstance(acceptance.get("checks"), dict) else {}
    verified = all(
        (
            payload.get("schema_version") == "ticket-evaluation-evidence-v1",
            base["fresh"] is True,
            dataset.get("kind") == "external_holdout",
            _safe_nonnegative_int(dataset.get("case_count")) >= 200,
            isinstance(dataset.get("sha256"), str) and len(dataset["sha256"]) == 64,
            str(runtime.get("llm_provider") or "").lower() not in {"", "stub"},
            str(runtime.get("fact_adapter") or "").lower() not in {"", "mock"},
            _safe_nonnegative_int(summary.get("run_count")) >= 3,
            _safe_nonnegative_int(summary.get("model_calls")) > 0,
            acceptance.get("status") == "verified",
            acceptance.get("passed") is True,
            bool(checks) and all(value is True for value in checks.values()),
            isinstance(payload.get("runs"), list) and len(payload["runs"]) >= 3,
            all(
                isinstance(run, dict)
                and run.get("status") == "completed"
                and _safe_nonnegative_int(run.get("case_count")) >= 200
                and "details" not in run
                for run in payload.get("runs", [])
            ),
        )
    )
    return {
        **base,
        "status": "verified_external" if verified else "not_verified",
        "gate_status": "verified_external" if verified else "blocked_external",
        "dataset_kind": dataset.get("kind"),
        "case_count": _safe_nonnegative_int(dataset.get("case_count")),
        "run_count": _safe_nonnegative_int(summary.get("run_count")),
        "acceptance_status": acceptance.get("status"),
    }


def _release_evidence_snapshot(
    *,
    strict_summary: dict[str, Any],
    source_control: dict[str, Any],
    adapter_mode: str,
    production_shape: dict[str, Any],
    policy_retrieval: dict[str, Any],
    target_environment: dict[str, Any],
    target_release_admission: dict[str, Any],
    external_gates: list[dict[str, Any]],
    evidence_hashes: dict[str, str | None],
) -> dict[str, Any]:
    gate_statuses = {
        str(item.get("key")): str(item.get("status"))
        for item in external_gates
        if item.get("key")
    }
    machine_gates_ready = all(
        (
            strict_summary.get("fail") == 0,
            source_control.get("traceable") is True,
            adapter_mode == "http",
            production_shape.get("local_production_shape_verified") is True,
            policy_retrieval.get("policy_retrieval_verified") is True,
            target_environment.get("cross_host_or_target_environment_verified") is True,
            target_release_admission.get("machine_admission_ready") is True,
            target_release_admission.get("commit_matches") is True,
            bool(gate_statuses),
            all(status == "verified_external" for status in gate_statuses.values()),
            bool(evidence_hashes),
            all(value is not None for value in evidence_hashes.values()),
        )
    )
    snapshot = {
        "schema_version": "after-sales-release-evidence-v2",
        "commit_sha": source_control.get("commit_sha"),
        "strict_preflight": {
            "fail": strict_summary.get("fail"),
            "failed_checks": list(strict_summary.get("failed_checks") or []),
        },
        "source_control_traceable": source_control.get("traceable") is True,
        "adapter_mode": adapter_mode,
        "local_production_shape_verified": production_shape.get(
            "local_production_shape_verified"
        )
        is True,
        "policy_retrieval_verified": policy_retrieval.get(
            "policy_retrieval_verified"
        )
        is True,
        "cross_host_or_target_environment_verified": target_environment.get(
            "cross_host_or_target_environment_verified"
        )
        is True,
        "target_release_admission_verified": all(
            (
                target_release_admission.get("machine_admission_ready") is True,
                target_release_admission.get("commit_matches") is True,
            )
        ),
        "external_gate_statuses": gate_statuses,
        "evidence_file_sha256": evidence_hashes,
        "machine_gates_ready": machine_gates_ready,
    }
    return {**snapshot, "sha256": _sha256_json(snapshot)}


def _release_decision_status(
    *,
    evidence_sha256: str,
    commit_sha: str | None,
    machine_gates_ready: bool,
    evidence_path: Path | None = None,
) -> dict[str, Any]:
    path = evidence_path or RELEASE_DECISION_EVIDENCE_PATH
    payload, base = _read_evidence(path, max_age_hours=RELEASE_DECISION_MAX_AGE_HOURS)
    if payload is None:
        return {**base, "status": "missing", "gate_status": "blocked_human"}

    decision = str(payload.get("decision") or "").strip().lower()
    approver_role = str(payload.get("approver_role") or "").strip()
    approval_reference = str(payload.get("approval_reference") or "").strip()
    risk_acceptance = str(payload.get("risk_acceptance") or "").strip()
    payload_commit = str(payload.get("commit_sha") or "").strip().lower()
    payload_evidence = str(payload.get("evidence_sha256") or "").strip().lower()
    commit_normalized = str(commit_sha or "").strip().lower()
    structure_valid = all(
        (
            payload.get("schema_version") == "after-sales-release-decision-v1",
            base["fresh"] is True,
            decision in {"approve", "reject"},
            approver_role in RELEASE_APPROVER_ROLES,
            1 <= len(approval_reference) <= 128,
            10 <= len(risk_acceptance) <= 2000,
            payload.get("rollback_confirmed") is True,
            len(payload_commit) == 40
            and all(character in "0123456789abcdef" for character in payload_commit),
            len(payload_evidence) == 64
            and all(character in "0123456789abcdef" for character in payload_evidence),
        )
    )
    commit_matches = bool(commit_normalized) and payload_commit == commit_normalized
    evidence_matches = payload_evidence == evidence_sha256.lower()
    approved = all(
        (
            structure_valid,
            decision == "approve",
            commit_matches,
            evidence_matches,
            machine_gates_ready,
        )
    )
    if approved:
        status = "approved"
        gate_status = "verified_human"
    elif structure_valid and decision == "reject" and commit_matches and evidence_matches:
        status = "rejected"
        gate_status = "blocked_human"
    elif structure_valid and decision == "approve" and not machine_gates_ready:
        status = "premature_approval"
        gate_status = "blocked_human"
    else:
        status = "not_verified"
        gate_status = "blocked_human"
    return {
        **base,
        "status": status,
        "gate_status": gate_status,
        "decision": decision or None,
        "approver_role": approver_role or None,
        "approval_reference_sha256": (
            hashlib.sha256(approval_reference.encode("utf-8")).hexdigest()
            if approval_reference
            else None
        ),
        "risk_acceptance_present": bool(risk_acceptance),
        "rollback_confirmed": payload.get("rollback_confirmed") is True,
        "commit_matches": commit_matches,
        "evidence_matches": evidence_matches,
        "machine_gates_ready": machine_gates_ready,
    }


def _release_decision_template(report: dict[str, Any]) -> dict[str, Any]:
    source_control = (report.get("local_evidence") or {}).get("source_control") or {}
    release_evidence = report.get("release_evidence") or {}
    return {
        "schema_version": "after-sales-release-decision-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision": "pending",
        "approver_role": "",
        "approval_reference": "",
        "commit_sha": source_control.get("commit_sha"),
        "evidence_sha256": release_evidence.get("sha256"),
        "risk_acceptance": "",
        "rollback_confirmed": False,
    }


def _run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
        check=False,
    )


def _is_delivery_evidence_path(path: str) -> bool:
    normalized = path.replace("\\", "/").strip("/")
    if normalized.startswith("docs/testing/artifacts/"):
        return True
    if normalized.startswith("docs/testing/") and normalized.endswith(".tdd.md"):
        return True
    return normalized == (
        "docs/product-redesign/"
        "Durable-Agent运行改造开发日志-2026-11-07.md"
    )


def _source_control_status(
    *, expected_release_sha: str | None = None
) -> dict[str, Any]:
    """Collect identifier-only Git provenance and fail closed when it is unavailable."""

    base = {
        "repository_detected": False,
        "commit_sha": None,
        "head_commit_sha": None,
        "head_matches_candidate": None,
        "candidate_is_ancestor": None,
        "evidence_only_paths_valid": None,
        "evidence_only_commit_count": None,
        "non_evidence_path_count": None,
        "branch": None,
        "upstream": None,
        "clean": False,
        "unstaged_changes": None,
        "staged_changes": None,
        "untracked_file_count": None,
        "ahead": None,
        "behind": None,
        "upstream_synced": False,
        "traceable": False,
        "blockers": ["repository_unavailable"],
    }
    try:
        detected = _run_git("rev-parse", "--is-inside-work-tree")
        if detected.returncode != 0 or detected.stdout.strip() != "true":
            return base

        commit = _run_git("rev-parse", "HEAD")
        branch = _run_git("branch", "--show-current")
        upstream = _run_git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        divergence = _run_git("rev-list", "--left-right", "--count", "HEAD...@{upstream}")
        unstaged = _run_git("diff", "--quiet", "--ignore-submodules", "--")
        staged = _run_git("diff", "--cached", "--quiet", "--ignore-submodules", "--")
        untracked = _run_git("ls-files", "--others", "--exclude-standard", "-z")
    except (OSError, subprocess.SubprocessError):
        return base

    commit_sha = commit.stdout.strip() if commit.returncode == 0 else None
    branch_name = branch.stdout.strip() if branch.returncode == 0 else None
    upstream_name = upstream.stdout.strip() if upstream.returncode == 0 else None
    unstaged_changes = unstaged.returncode != 0
    staged_changes = staged.returncode != 0
    untracked_count = len([item for item in untracked.stdout.split("\0") if item]) if untracked.returncode == 0 else None
    clean = (
        unstaged.returncode == 0
        and staged.returncode == 0
        and untracked_count == 0
    )

    ahead = behind = None
    if divergence.returncode == 0:
        parts = divergence.stdout.replace("\t", " ").split()
        if len(parts) == 2 and all(part.isdigit() for part in parts):
            ahead, behind = (int(part) for part in parts)
    upstream_synced = bool(upstream_name) and ahead == 0 and behind == 0

    blockers: list[str] = []
    if not commit_sha:
        blockers.append("commit_unavailable")
    if not clean:
        blockers.append("working_tree_dirty")
    if not upstream_name:
        blockers.append("upstream_missing")
    elif not upstream_synced:
        blockers.append("upstream_not_synced")

    requested_sha = str(expected_release_sha or "").strip().lower()
    candidate_sha = commit_sha
    head_matches_candidate = bool(commit_sha)
    candidate_is_ancestor = bool(commit_sha)
    evidence_only_paths_valid = bool(commit_sha)
    evidence_only_commit_count = 0 if commit_sha else None
    non_evidence_path_count = 0 if commit_sha else None
    if expected_release_sha is not None:
        valid_requested_sha = len(requested_sha) == 40 and all(
            character in "0123456789abcdef" for character in requested_sha
        )
        if not valid_requested_sha:
            blockers.append("candidate_commit_invalid")
            candidate_sha = None
            head_matches_candidate = False
            candidate_is_ancestor = False
            evidence_only_paths_valid = False
            evidence_only_commit_count = None
            non_evidence_path_count = None
        else:
            candidate_sha = requested_sha
            head_matches_candidate = candidate_sha == commit_sha
            if head_matches_candidate:
                candidate_is_ancestor = True
                evidence_only_paths_valid = True
                evidence_only_commit_count = 0
                non_evidence_path_count = 0
            elif commit_sha:
                try:
                    ancestor = _run_git(
                        "merge-base", "--is-ancestor", candidate_sha, "HEAD"
                    )
                    changed = _run_git(
                        "diff", "--name-only", f"{candidate_sha}..HEAD", "--"
                    )
                    commit_count = _run_git(
                        "rev-list", "--count", f"{candidate_sha}..HEAD"
                    )
                except (OSError, subprocess.SubprocessError):
                    ancestor = changed = commit_count = None
                if ancestor is None or changed is None or commit_count is None:
                    candidate_is_ancestor = False
                    evidence_only_paths_valid = False
                    evidence_only_commit_count = None
                    non_evidence_path_count = None
                    blockers.append("candidate_history_unavailable")
                else:
                    candidate_is_ancestor = ancestor.returncode == 0
                    if not candidate_is_ancestor:
                        blockers.append("candidate_not_ancestor")
                    if changed.returncode == 0:
                        changed_paths = [
                            item.strip()
                            for item in changed.stdout.splitlines()
                            if item.strip()
                        ]
                        non_evidence_paths = [
                            item
                            for item in changed_paths
                            if not _is_delivery_evidence_path(item)
                        ]
                        evidence_only_paths_valid = not non_evidence_paths
                        non_evidence_path_count = len(non_evidence_paths)
                        if non_evidence_paths:
                            blockers.append("candidate_non_evidence_changes")
                    else:
                        evidence_only_paths_valid = False
                        non_evidence_path_count = None
                        blockers.append("candidate_history_unavailable")
                    count_text = commit_count.stdout.strip()
                    evidence_only_commit_count = (
                        int(count_text)
                        if commit_count.returncode == 0 and count_text.isdigit()
                        else None
                    )
                    if evidence_only_commit_count is None:
                        blockers.append("candidate_history_unavailable")

    blockers = list(dict.fromkeys(blockers))

    return {
        **base,
        "repository_detected": True,
        "commit_sha": candidate_sha,
        "head_commit_sha": commit_sha,
        "head_matches_candidate": head_matches_candidate,
        "candidate_is_ancestor": candidate_is_ancestor,
        "evidence_only_paths_valid": evidence_only_paths_valid,
        "evidence_only_commit_count": evidence_only_commit_count,
        "non_evidence_path_count": non_evidence_path_count,
        "branch": branch_name or None,
        "upstream": upstream_name or None,
        "clean": clean,
        "unstaged_changes": unstaged_changes,
        "staged_changes": staged_changes,
        "untracked_file_count": untracked_count,
        "ahead": ahead,
        "behind": behind,
        "upstream_synced": upstream_synced,
        "traceable": not blockers,
        "blockers": blockers,
    }


def _load_preflight_module():
    path = BACKEND / "scripts" / "preflight_check.py"
    spec = importlib.util.spec_from_file_location("after_sales_preflight", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 preflight_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def summarize_checks(results: Iterable[Any]) -> dict[str, Any]:
    items = list(results)
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for item in items:
        status = str(getattr(item, "status", "UNKNOWN"))
        counts[status] = counts.get(status, 0) + 1
    return {
        "total": len(items),
        "pass": counts.get("PASS", 0),
        "warn": counts.get("WARN", 0),
        "fail": counts.get("FAIL", 0),
        "failed_checks": [
            str(getattr(item, "name", "unknown"))
            for item in items
            if getattr(item, "status", None) == "FAIL"
        ],
    }


def _fact_plan_status() -> dict[str, Any]:
    try:
        from app.services.fact_integration_probe import load_fact_probe_plan

        plan = load_fact_probe_plan(ROOT / "docs" / "integrations" / "fact-probe-plan.example.json")
        return {"status": "pass", **plan.safe_metadata()}
    except Exception as exc:  # Do not include paths or raw payloads in the report.
        return {"status": "fail", "error_type": type(exc).__name__}


def _demo_ticket_status() -> dict[str, Any]:
    try:
        from app.db.seed import init_db
        from app.db.session import SessionLocal
        from app.models.identity import Tenant
        from app.models.ticket import Ticket

        init_db()
        session = SessionLocal()
        try:
            tenant = session.query(Tenant).filter(Tenant.code == "demo").first()
            if tenant is None:
                return {"status": "fail", "missing": list(REQUIRED_TICKETS)}
            rows = (
                session.query(Ticket.external_id)
                .filter(Ticket.tenant_id == tenant.id, Ticket.external_id.in_(REQUIRED_TICKETS))
                .all()
            )
            present = sorted(str(row[0]) for row in rows if row[0])
            missing = sorted(set(REQUIRED_TICKETS) - set(present))
            return {
                "status": "pass" if not missing else "fail",
                "required_count": len(REQUIRED_TICKETS),
                "present_count": len(present),
                "missing": missing,
            }
        finally:
            session.close()
    except Exception as exc:
        return {"status": "fail", "error_type": type(exc).__name__}


def _local_production_shape_status(evidence_path: Path | None = None) -> dict[str, Any]:
    """Validate the checked-in local Docker rehearsal evidence.

    This proves only a single-host production-shaped rehearsal.  It explicitly
    does not prove cross-host failover, managed services, real models, real fact
    adapters, object storage or the target deployment environment.
    """

    path = evidence_path or _latest_production_load_evidence()
    if path is None:
        return {
            "status": "missing",
            "local_production_shape_verified": False,
            "cross_host_or_target_environment_verified": False,
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {
            "status": "missing",
            "local_production_shape_verified": False,
            "cross_host_or_target_environment_verified": False,
        }
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unreadable",
            "error_type": type(exc).__name__,
            "local_production_shape_verified": False,
            "cross_host_or_target_environment_verified": False,
        }

    health = payload.get("runtime_health") or {}
    execution = health.get("execution") or {}
    checkpointer = execution.get("checkpointer") or {}
    cache = health.get("cache") or {}
    counts = payload.get("counts") or {}
    queue_final = payload.get("queue_final") or {}
    acceptance = payload.get("acceptance") or []
    workers = payload.get("workers") or []
    checks = {
        "postgres_database": health.get("database") == "postgres",
        "redis_cache_healthy": cache.get("backend") == "redis" and cache.get("healthy") is True,
        "pgvector_backend": health.get("vector_backend") == "pgvector",
        "queue_backend": execution.get("backend") == "queue",
        "independent_workers": execution.get("worker_embedded") is False and len(set(workers)) >= 2,
        "shared_postgres_checkpointer": checkpointer.get("backend") == "postgres" and checkpointer.get("shared") is True,
        "load_completed_without_errors": (
            isinstance(counts.get("requested"), int)
            and counts.get("requested", 0) > 0
            and counts.get("requested") == counts.get("submitted") == counts.get("completed")
            and counts.get("errors") == 0
        ),
        "queue_drained": all(queue_final.get(key) == 0 for key in ("pending", "claimed", "dead")),
        "acceptance_passed": bool(acceptance)
        and all(
            isinstance(item, dict) and item.get("passed") is True
            for item in acceptance
        ),
    }
    verified = all(checks.values())
    relative_path = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else path.name
    return {
        "status": "verified" if verified else "failed",
        "evidence_path": relative_path,
        "run_id": payload.get("run_id"),
        "checks": checks,
        "local_production_shape_verified": verified,
        "cross_host_or_target_environment_verified": False,
        "limitations": [
            "single_host_compose",
            "stub_model",
            "mock_fact_adapter",
            "local_object_storage",
        ],
    }


def _target_environment_status(evidence_path: Path | None = None) -> dict[str, Any]:
    """Validate redacted target evidence without treating remote CI as cross-host proof."""

    path = evidence_path or TARGET_ENVIRONMENT_EVIDENCE_PATH
    payload, base = _read_evidence(path)
    base = {
        **base,
        "target_environment_ready": False,
        "cross_host_or_target_environment_verified": False,
    }
    if payload is None:
        return base

    configuration = payload.get("configuration") if isinstance(payload, dict) else {}
    if not isinstance(configuration, dict):
        configuration = {}
    node_count = configuration.get("node_count")
    verified = all(
        (
            payload.get("schema_version") == "target-environment-probe-v1",
            base["fresh"] is True,
            configuration.get("passed") is True,
            isinstance(node_count, int) and node_count >= 2,
            payload.get("expected_env") == "prod",
            payload.get("executed") is True,
            payload.get("passed") is True,
            payload.get("runtime_shape_ready") is True,
            payload.get("target_environment_ready") is True,
            payload.get("distinct_hosts_attested") is True,
            payload.get("cross_host_or_target_environment_verified") is True,
            payload.get("verification_basis")
            == "live_checks_plus_operator_distinct_host_attestation",
        )
    )
    return {
        **base,
        "status": "verified_external" if verified else "not_verified",
        "node_count": node_count if isinstance(node_count, int) else 0,
        "verification_basis": payload.get("verification_basis"),
        "target_environment_ready": verified,
        "cross_host_or_target_environment_verified": verified,
        "note": "不同主机由操作者显式确认，运行时依赖由实时 readiness 检查验证。",
    }


def _target_release_admission_status(
    evidence_path: Path | None = None,
    *,
    expected_commit_sha: str | None,
) -> dict[str, Any]:
    """Validate the unified candidate admission without granting release approval."""

    path = evidence_path or TARGET_RELEASE_ADMISSION_EVIDENCE_PATH
    payload, base = _read_evidence(path)
    result = {
        **base,
        "machine_admission_ready": False,
        "commit_matches": False,
        "blockers": ["target_release_admission_missing"],
    }
    if payload is None:
        return result

    expected_sha = str(expected_commit_sha or "").strip().lower()
    evidence_shas = payload.get("evidence_release_shas")
    checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    commit_matches = bool(
        len(expected_sha) == 40
        and payload.get("expected_release_sha") == expected_sha
        and evidence_shas == [expected_sha]
        and checks.get("release_sha_verified") is True
    )
    verified = all(
        (
            payload.get("schema_version") == "target-release-admission-v1",
            base["fresh"] is True,
            payload.get("executed") is True,
            payload.get("passed") is True,
            payload.get("status") == "machine_ready_awaiting_human_approval",
            payload.get("machine_admission_ready") is True,
            payload.get("human_approval_required") is True,
            payload.get("release_authorized") is False,
            payload.get("production_ready") is False,
            not blockers,
            commit_matches,
            checks.get("image_digest_verified") is True,
            checks.get("local_rollout_verified") is True,
            checks.get("enterprise_registry_verified") is True,
            checks.get("cross_host_verified") is True,
        )
    )
    return {
        **result,
        "status": "verified_machine" if verified else "not_verified",
        "gate_status": "verified_machine" if verified else "blocked_machine",
        "machine_admission_ready": verified,
        "commit_matches": commit_matches,
        "admission_bundle_id": payload.get("admission_bundle_id"),
        "blockers": [str(item) for item in blockers],
    }


def _load_usability_validator():
    path = BACKEND / "scripts" / "validate_usability_test_record.py"
    spec = importlib.util.spec_from_file_location("after_sales_usability_validator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 validate_usability_test_record.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _task_has_observation(task: Any) -> bool:
    """Return whether a task contains an explicit observation beyond template defaults."""
    if not isinstance(task, dict):
        return False
    return any(
        (
            task.get("completed") is not None,
            task.get("independent") is not None,
            task.get("duration_seconds") is not None,
            isinstance(task.get("mistakes"), int) and task.get("mistakes", 0) > 0,
            bool(str(task.get("blocked_at") or "").strip()),
            bool(str(task.get("notes") or "").strip()),
        )
    )


def _usability_record_status(record_path: Path | None = None) -> dict[str, Any]:
    """Summarize the usability record without exposing participant notes or PII."""
    path = record_path or USABILITY_RECORD_PATH
    base = {
        "record_path": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else path.name,
        "template_valid": False,
        "real_observations_present": False,
        "independent_completion_rate": None,
        "issue_counts": {"P0": 0, "P1": 0, "P2": 0},
        "conclusion_provided": False,
        "remediation_retest_present": False,
        "gate_status": "blocked_external",
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {**base, "status": "missing"}
    except (OSError, json.JSONDecodeError) as exc:
        return {**base, "status": "unreadable", "error_type": type(exc).__name__}

    validator = _load_usability_validator()
    summary, errors = validator.validate_record(payload)
    if errors or summary is None:
        return {**base, "status": "invalid", "error_count": len(errors)}

    tasks = payload.get("tasks", [])
    observed_task_count = sum(_task_has_observation(task) for task in tasks)
    real_observations_present = observed_task_count > 0 or bool(payload.get("issues"))
    retest = payload.get("remediation_retest") or payload.get("retest")
    issue_counts = dict(summary["issue_counts"])
    unresolved = dict(summary["unresolved_issue_counts"])
    high_severity_seen = issue_counts["P0"] + issue_counts["P1"] > 0
    retest_requirement_met = not high_severity_seen or bool(retest)
    gate_verified = all(
        (
            summary["participant_scope_confirmed"] is True,
            summary["non_developer_confirmed_count"] >= 5,
            summary["all_tasks_fully_observed"] is True,
            summary["test_metadata_complete"] is True,
            summary["conclusion_provided"] is True,
            unresolved["P0"] == 0,
            unresolved["P1"] == 0,
            retest_requirement_met,
        )
    )
    return {
        **base,
        "status": (
            "verified_external"
            if gate_verified
            else "observations_present"
            if real_observations_present
            else "template_valid_unfilled"
        ),
        "template_valid": True,
        "real_observations_present": real_observations_present,
        "observed_task_count": observed_task_count,
        "participant_count": summary["participant_count"],
        "task_count": summary["task_count"],
        "independent_completion_rate": summary["independent_completion_rate"],
        "issue_counts": issue_counts,
        "unresolved_issue_counts": unresolved,
        "all_tasks_fully_observed": summary["all_tasks_fully_observed"],
        "non_developer_confirmed_count": summary[
            "non_developer_confirmed_count"
        ],
        "participant_scope_confirmed": summary["participant_scope_confirmed"],
        "test_metadata_complete": summary["test_metadata_complete"],
        "conclusion_provided": summary["conclusion_provided"],
        "remediation_retest_present": bool(retest),
        "retest_requirement_met": retest_requirement_met,
        "gate_status": "verified_external" if gate_verified else "blocked_external",
    }


def build_report(
    *,
    policy_benchmark_path: Path | None = None,
    expected_release_sha: str | None = None,
    target_release_admission_path: Path | None = None,
) -> dict[str, Any]:
    from app.config import get_settings

    settings = get_settings()
    preflight = _load_preflight_module()
    normal = preflight.run_checks(settings, strict=False, with_eval=False)
    strict = preflight.run_checks(settings, strict=True, with_eval=False)
    fact_plan = _fact_plan_status()
    fact_integration = _fact_integration_status()
    webhook_integration = _webhook_integration_status()
    real_model_holdout = _real_model_holdout_status()
    metric_source = _metric_source_status()
    demo_tickets = _demo_ticket_status()
    production_shape = _local_production_shape_status()
    policy_retrieval = _policy_retrieval_benchmark_status(policy_benchmark_path)
    scheduler_failover = _scheduler_failover_status()
    target_environment = _target_environment_status()
    usability = _usability_record_status()
    source_control = _source_control_status(
        expected_release_sha=expected_release_sha
    )
    target_release_admission = _target_release_admission_status(
        target_release_admission_path,
        expected_commit_sha=source_control.get("commit_sha")
    )
    strict_summary = summarize_checks(strict)
    adapter_mode = "mock" if settings.ticket_use_mock_adapters else "http"
    external_gates = _external_gates(
        target_environment=target_environment,
        usability=usability,
        fact_integration=fact_integration,
        webhook_integration=webhook_integration,
        real_model_holdout=real_model_holdout,
        metric_source=metric_source,
    )
    production_load_path = _latest_production_load_evidence()
    release_evidence = _release_evidence_snapshot(
        strict_summary=strict_summary,
        source_control=source_control,
        adapter_mode=adapter_mode,
        production_shape=production_shape,
        policy_retrieval=policy_retrieval,
        target_environment=target_environment,
        target_release_admission=target_release_admission,
        external_gates=external_gates,
        evidence_hashes={
            "fact_platform_sandbox": _sha256_file(FACT_INTEGRATION_EVIDENCE_PATH),
            "message_channel": _sha256_file(WEBHOOK_INTEGRATION_EVIDENCE_PATH),
            "production_infrastructure": _sha256_file(TARGET_ENVIRONMENT_EVIDENCE_PATH),
            "real_model_holdout": _sha256_file(REAL_MODEL_HOLDOUT_EVIDENCE_PATH),
            "metric_source": _sha256_file(METRIC_SOURCE_EVIDENCE_PATH),
            "usability_test": _sha256_file(USABILITY_RECORD_PATH),
            "local_production_shape": (
                _sha256_file(production_load_path) if production_load_path else None
            ),
            "policy_retrieval_benchmark": _sha256_file(
                policy_benchmark_path or POLICY_RETRIEVAL_BENCHMARK_EVIDENCE_PATH
            ),
            "target_release_admission": _sha256_file(
                target_release_admission_path
                or TARGET_RELEASE_ADMISSION_EVIDENCE_PATH
            ),
        },
    )
    release_decision = _release_decision_status(
        evidence_sha256=release_evidence["sha256"],
        commit_sha=source_control.get("commit_sha"),
        machine_gates_ready=release_evidence["machine_gates_ready"],
    )
    production_ready = (
        release_evidence["machine_gates_ready"] is True
        and release_decision["gate_status"] == "verified_human"
    )
    if production_ready:
        decision = "ready_for_release"
    elif release_evidence["machine_gates_ready"] is not True:
        decision = "not_ready_machine_gates"
    elif release_decision["status"] == "rejected":
        decision = "release_rejected"
    elif release_decision["status"] == "missing":
        decision = "awaiting_release_approval"
    else:
        decision = "release_decision_invalid"
    local_ready = (
        strict_summary["fail"] == 0
        and fact_plan["status"] == "pass"
        and demo_tickets["status"] == "pass"
        and production_shape["local_production_shape_verified"] is True
        and policy_retrieval["policy_retrieval_verified"] is True
    )
    return {
        "schema_version": "after-sales-release-readiness-v5",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"app_env": settings.app_env, "adapter_mode": adapter_mode},
        "local_evidence": {
            "preflight": {
                "normal": summarize_checks(normal),
                "strict": strict_summary,
            },
            "fact_probe_plan": fact_plan,
            "demo_tickets": demo_tickets,
            "production_shape": production_shape,
            "policy_retrieval_benchmark": policy_retrieval,
            "scheduler_failover": scheduler_failover,
            "usability_test": usability,
            "source_control": source_control,
            "target_release_admission": target_release_admission,
            "local_ready_for_production_config": local_ready,
        },
        "infrastructure_verification": {
            "local_production_shape_verified": production_shape[
                "local_production_shape_verified"
            ],
            "local_scheduler_failover_verified": scheduler_failover[
                "local_postgres_failover_verified"
            ],
            "scheduler_failover": scheduler_failover,
            "cross_host_or_target_environment_verified": target_environment[
                "cross_host_or_target_environment_verified"
            ],
            "target_environment": target_environment,
        },
        "external_integration_verification": {
            "fact_platform_sandbox": fact_integration,
            "metric_source": metric_source,
            "message_channel": webhook_integration,
            "real_model_holdout": real_model_holdout,
        },
        "external_gates": external_gates,
        "release_evidence": release_evidence,
        "release_decision": release_decision,
        "production_ready": production_ready,
        "decision": decision,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成售后系统诚实的预发布就绪报告")
    parser.add_argument("--json-out", help="写入脱敏 JSON 报告")
    parser.add_argument(
        "--policy-benchmark-evidence",
        help="指定本轮刚生成的政策 Recall@5 证据，避免读取过期快照",
    )
    parser.add_argument(
        "--expected-release-sha",
        help="显式绑定运行候选 SHA；候选之后只允许交付证据类提交",
    )
    parser.add_argument(
        "--target-release-admission-evidence",
        help="指定本轮统一候选准入证据，避免在固化前读取旧 canonical 文件",
    )
    parser.add_argument(
        "--decision-template-out",
        help="生成绑定当前提交与机器证据摘要的待审批模板；不会自动批准",
    )
    parser.add_argument("--fail-if-not-ready", action="store_true", help="未满足生产门禁时返回 1")
    args = parser.parse_args(argv)
    report = build_report(
        policy_benchmark_path=(
            Path(args.policy_benchmark_evidence).resolve()
            if args.policy_benchmark_evidence
            else None
        ),
        expected_release_sha=args.expected_release_sha,
        target_release_admission_path=(
            Path(args.target_release_admission_evidence).resolve()
            if args.target_release_admission_evidence
            else None
        ),
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    print(encoded)
    if args.json_out:
        output = Path(args.json_out).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
        print(f"JSON 报告={output}")
    if args.decision_template_out:
        template_output = Path(args.decision_template_out).resolve()
        if template_output.exists():
            parser.error(f"拒绝覆盖已有审批模板：{template_output}")
        template_output.parent.mkdir(parents=True, exist_ok=True)
        template_output.write_text(
            json.dumps(_release_decision_template(report), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        print(f"待审批模板={template_output}")
    return 1 if args.fail_if_not_ready and not report["production_ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
