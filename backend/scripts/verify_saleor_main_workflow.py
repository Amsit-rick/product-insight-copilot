#!/usr/bin/env python
"""Run the main after-sales workflow against the local Saleor sandbox.

The report intentionally stores only safe verification metadata. It never
persists fact payloads, upstream order mappings, credentials, GraphQL bodies,
customer identifiers or model token usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx


BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

SOURCE_VERSION = "saleor-3.23.49+scenario-v1"
SOURCES = ("order", "payment", "logistics", "aftersale")

BUSINESS_CASES: tuple[tuple[str, str, str], ...] = (
    ("SCN-NORMAL-DELIVERED", "商品已经签收，我想了解一下售后处理条件。", "ask_customer"),
    ("SCN-UNPAID-CANCELED", "订单已取消，请确认是否还有退款需要处理。", "ask_customer"),
    ("SCN-PAID-UNSHIPPED-REFUND", "订单已付款但还没发货，我要退款。", "refund"),
    ("SCN-PARTIAL-FULFILLMENT", "订单只发了一部分，请核实剩余商品。", "ask_customer"),
    ("SCN-LOGISTICS-STALLED", "物流很久没有更新，请帮我核实并处理。", "contact_logistics"),
    ("SCN-DELIVERED-RETURN", "商品已签收，我要申请退货退款。", "refund"),
    ("SCN-PARTIAL-REFUND", "之前只退了一部分，现在申请退还剩余可退金额。", "refund"),
    ("SCN-DUPLICATE-REFUND", "我还要再次退款，请直接退款。", "ask_customer"),
    ("SCN-MISSING-ITEM", "收到包裹但少了一件商品，请补发或处理。", "reship"),
    ("SCN-CROSS-SOURCE-CONFLICT", "订单显示已支付，我要退款。", "ask_customer"),
)

FORBIDDEN_EVIDENCE_KEYS = {
    "authorization",
    "customer_id",
    "facts",
    "graphql",
    "order_id",
    "password",
    "payload",
    "query_body",
    "request_headers",
    "secret",
    "token",
}
EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _source_map(rows: list[dict]) -> dict[str, dict]:
    return {
        str(row.get("source")): row
        for row in rows
        if isinstance(row, dict) and row.get("source")
    }


def _check(name: str, passed: bool) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed)}


def _percentile(values: list[int], percentile: float) -> int:
    """Return the repository-standard nearest-index percentile."""
    if not values:
        return 0
    ordered = sorted(int(value) for value in values)
    index = min(len(ordered) - 1, int(round(percentile * (len(ordered) - 1))))
    return ordered[index]


def _rate(passed: int, total: int) -> float:
    return round(passed / total, 4) if total else 0.0


def _handoff_reasons(
    *, scenario_id: str, recommendation: dict | None, source_unavailable: bool = False
) -> list[str]:
    reasons: list[str] = []
    if source_unavailable:
        reasons.append("source_unavailable")
    if scenario_id == "SCN-CROSS-SOURCE-CONFLICT":
        reasons.append("cross_source_conflict")
    if scenario_id == "SCN-DUPLICATE-REFUND":
        reasons.append("duplicate_refund_guard")
    if (recommendation or {}).get("missing_information"):
        reasons.append("missing_information")
    if (recommendation or {}).get("approval_requirement") in {
        "staff_confirm",
        "supervisor",
        "risk_control",
    }:
        reasons.append("supervisor_approval")
    return reasons


def _fact_query_latency(trace: dict[str, Any]) -> int | None:
    latencies = [
        int(run["latency_ms"])
        for run in trace.get("agent_runs") or []
        if run.get("agent_code") == "order_fact_agent"
        and run.get("latency_ms") is not None
    ]
    return sum(latencies) if latencies else None


def summarize_results(
    business_results: list[dict[str, Any]], fault_results: list[dict[str, Any]]
) -> dict[str, Any]:
    passed_business = sum(1 for item in business_results if item.get("passed"))
    passed_faults = sum(1 for item in fault_results if item.get("passed"))
    all_results = [*business_results, *fault_results]
    workflow_latencies = [
        int(item["latency_ms"])
        for item in all_results
        if item.get("latency_ms") is not None
    ]
    fact_query_latencies = [
        int(item["fact_query_latency_ms"])
        for item in all_results
        if item.get("fact_query_latency_ms") is not None
    ]
    failure_types: Counter[str] = Counter()
    for item in business_results:
        if item.get("failure_type"):
            failure_types[str(item["failure_type"])] += 1
    for item in fault_results:
        if item.get("target_error_code"):
            failure_types[str(item["target_error_code"])] += 1
        if item.get("failure_type"):
            failure_types[str(item["failure_type"])] += 1
    handoff_reasons = Counter(
        str(reason)
        for item in all_results
        for reason in item.get("handoff_reasons") or []
    )
    total = len(all_results)
    passed_total = passed_business + passed_faults
    return {
        "business_passed": passed_business,
        "business_total": len(business_results),
        "fault_passed": passed_faults,
        "fault_total": len(fault_results),
        "success_rate": _rate(passed_total, total),
        "business_success_rate": _rate(passed_business, len(business_results)),
        "fault_success_rate": _rate(passed_faults, len(fault_results)),
        "workflow_latency_ms_p50": _percentile(workflow_latencies, 0.50),
        "workflow_latency_ms_p95": _percentile(workflow_latencies, 0.95),
        "fact_query_latency_ms_p50": _percentile(fact_query_latencies, 0.50),
        "fact_query_latency_ms_p95": _percentile(fact_query_latencies, 0.95),
        "observed_failure_types": dict(sorted(failure_types.items())),
        "human_handoff_reasons": dict(sorted(handoff_reasons.items())),
    }


def evaluate_business_case(
    *,
    scenario_id: str,
    analyze_status: int,
    task_status: str | None,
    facts: list[dict],
    trace_fact_sources: list[dict],
    recommendation: dict | None,
    reflection: dict,
    refundable_amount: Any = None,
    expected_action: str | None = None,
    latency_ms: int | None = None,
    fact_query_latency_ms: int | None = None,
) -> dict[str, Any]:
    source_map = _source_map(facts)
    trace_map = _source_map(trace_fact_sources)
    versions = sorted(
        {
            str(row.get("source_version"))
            for row in source_map.values()
            if row.get("source_version")
        }
    )
    max_rounds = int(reflection.get("max_rounds") or 1)
    rounds = int(reflection.get("rounds") or 0)
    revision_rounds = int(reflection.get("revision_rounds", rounds) or 0)
    checks = [
        _check("analysis_accepted", analyze_status == 202),
        _check("workflow_completed", task_status == "completed"),
        _check("four_sources_persisted", set(source_map) == set(SOURCES)),
        _check(
            "sources_are_real_http",
            all(
                not bool(row.get("is_mock"))
                and str(row.get("adapter") or "").startswith("http")
                for row in source_map.values()
            ),
        ),
        _check(
            "source_version_preserved",
            bool(source_map)
            and all(row.get("source_version") == SOURCE_VERSION for row in source_map.values()),
        ),
        _check(
            "trace_sources_complete",
            set(trace_map) == set(SOURCES)
            and all(row.get("source_version") == SOURCE_VERSION for row in trace_map.values()),
        ),
        _check("recommendation_generated", isinstance(recommendation, dict)),
        _check("reflection_executed", bool(reflection.get("enabled"))),
        _check("reflection_is_bounded", revision_rounds <= max_rounds),
    ]

    action = str((recommendation or {}).get("action_type") or "")
    amount = _decimal((recommendation or {}).get("amount"))
    amount_source = str((recommendation or {}).get("amount_source") or "")
    refund_balance_matches: bool | None = None
    if expected_action:
        checks.append(_check("scenario_action_matches", action == expected_action))

    if scenario_id == "SCN-PARTIAL-REFUND":
        expected = _decimal(refundable_amount)
        action_is_refund = action == "refund"
        expected_balance_present = expected is not None
        amount_matches_balance = expected_balance_present and amount == expected
        remaining_balance_source = "剩余可退金额" in amount_source
        refund_balance_matches = (
            action_is_refund
            and expected_balance_present
            and amount_matches_balance
            and remaining_balance_source
        )
        checks.extend(
            [
                _check("partial_refund_action", action_is_refund),
                _check("partial_refund_balance_present", expected_balance_present),
                _check("partial_refund_amount_matches_balance", amount_matches_balance),
                _check("partial_refund_uses_remaining_balance_source", remaining_balance_source),
            ]
        )

    if scenario_id in {"SCN-DUPLICATE-REFUND", "SCN-CROSS-SOURCE-CONFLICT"}:
        checks.append(
            _check("unsafe_refund_blocked", action != "refund" and amount is None)
        )

    if action == "refund":
        checks.extend(
            [
                _check(
                    "refund_requires_supervisor",
                    (recommendation or {}).get("approval_requirement") == "supervisor",
                ),
                _check(
                    "refund_waits_for_approval",
                    (recommendation or {}).get("status") == "pending_approval",
                ),
            ]
        )

    failed_checks = [item["name"] for item in checks if not item["passed"]]
    return {
        "scenario_id": scenario_id,
        "passed": all(item["passed"] for item in checks),
        "failure_type": failed_checks[0] if failed_checks else None,
        "analyze_status": analyze_status,
        "task_status": task_status,
        "latency_ms": latency_ms,
        "fact_query_latency_ms": fact_query_latency_ms,
        "source_count": len(source_map),
        "source_versions": versions,
        "action_type": action or None,
        "expected_action": expected_action,
        "refund_balance_matches": refund_balance_matches,
        "reflection_enabled": bool(reflection.get("enabled")),
        "reflection_rounds": rounds,
        "revision_rounds": revision_rounds,
        "reflection_bounded": revision_rounds <= max_rounds,
        "handoff_reasons": _handoff_reasons(
            scenario_id=scenario_id, recommendation=recommendation
        ),
        "checks": checks,
    }


def evaluate_fault_case(
    *,
    scenario_id: str,
    target_source: str,
    expected_error: str,
    analyze_status: int,
    facts: list[dict],
    recommendation: dict | None,
    task_status: str | None = None,
    latency_ms: int | None = None,
    fact_query_latency_ms: int | None = None,
) -> dict[str, Any]:
    source_map = _source_map(facts)
    target = source_map.get(target_source) or {}
    healthy = [
        row
        for source, row in source_map.items()
        if source != target_source
        and not row.get("error_code")
        and row.get("source_version") == SOURCE_VERSION
        and not bool(row.get("is_mock"))
    ]
    action = str((recommendation or {}).get("action_type") or "")
    amount = _decimal((recommendation or {}).get("amount"))
    unsafe_refund = action == "refund" or amount is not None
    accepted_or_explicitly_stopped = analyze_status == 202 or (
        target_source == "order" and analyze_status == 409
    )
    workflow_finished = analyze_status == 409 or task_status == "completed"
    checks = [
        _check("analysis_has_expected_outcome", accepted_or_explicitly_stopped),
        _check("workflow_finished_or_stopped", workflow_finished),
        _check("four_source_results_persisted", set(source_map) == set(SOURCES)),
        _check("target_error_recorded", target.get("error_code") == expected_error),
        _check("other_sources_preserved", len(healthy) == len(SOURCES) - 1),
        _check("unsafe_refund_not_generated", not unsafe_refund),
    ]
    failed_checks = [item["name"] for item in checks if not item["passed"]]
    return {
        "scenario_id": scenario_id,
        "target_source": target_source,
        "expected_error": expected_error,
        "target_error_code": target.get("error_code"),
        "passed": all(item["passed"] for item in checks),
        "failure_type": failed_checks[0] if failed_checks else None,
        "analyze_status": analyze_status,
        "task_status": task_status,
        "latency_ms": latency_ms,
        "fact_query_latency_ms": fact_query_latency_ms,
        "source_count": len(source_map),
        "healthy_source_count": len(healthy),
        "action_type": action or None,
        "unsafe_refund_generated": unsafe_refund,
        "handoff_reasons": _handoff_reasons(
            scenario_id=scenario_id,
            recommendation=recommendation,
            source_unavailable=True,
        ),
        "checks": checks,
    }


def assert_safe_evidence(value: Any, path: str = "report") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in FORBIDDEN_EVIDENCE_KEYS or any(
                lowered.endswith(f"_{item}") for item in FORBIDDEN_EVIDENCE_KEYS
            ):
                raise ValueError(f"unsafe evidence field at {path}.{key}")
            assert_safe_evidence(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_safe_evidence(child, f"{path}[{index}]")
    elif isinstance(value, str):
        if EMAIL_RE.search(value) or PHONE_RE.search(value):
            raise ValueError(f"unsafe evidence value at {path}")
        lowered = value.lower()
        if "query {" in lowered or "mutation {" in lowered:
            raise ValueError(f"unsafe evidence GraphQL body at {path}")


def _runtime_environment(state_dir: Path, sandbox_port: int) -> dict[str, str]:
    from scripts.local_integration import integration_environment

    env = integration_environment(state_dir, sandbox_port)
    env.update(
        {
            "TASK_RUN_INLINE": "true",
            "TASK_EXECUTION_BACKEND": "inline",
            "WORKER_EMBEDDED": "false",
            "LLM_PROVIDER": "stub",
            "LLM_API_KEY": "",
            "AGENT_MODEL_OVERRIDES": "{}",
            "EMBEDDING_PROVIDER": "stub",
            "JWT_SECRET": "saleor-workflow-verifier-secret-at-least-32-bytes",
        }
    )
    return env


def _latest_fact_metadata(ticket_id: str) -> list[dict[str, Any]]:
    from app.db.session import SessionLocal
    from app.models.ticket import OrderFact

    with SessionLocal() as session:
        rows = (
            session.query(OrderFact)
            .filter(OrderFact.ticket_id == ticket_id)
            .order_by(OrderFact.created_at.asc())
            .all()
        )
        latest = {row.source: row for row in rows}
        return [
            {
                "source": source,
                "adapter": row.adapter,
                "source_version": row.source_version,
                "is_mock": bool(row.is_mock),
                "error_code": (row.error or {}).get("code"),
            }
            for source, row in sorted(latest.items())
        ]


def _login(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": f"{username}123"},
    )
    response.raise_for_status()
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def _create_ticket(client, headers: dict[str, str], scenario_id: str, content: str) -> dict:
    suffix = uuid.uuid4().hex[:12]
    response = client.post(
        "/api/v1/tickets",
        headers=headers,
        json={
            "subject": f"[Saleor Slice 4] {scenario_id}",
            "content": content,
            "order_id": scenario_id,
            "external_id": f"SALEOR-SLICE4-{suffix}",
            "data_origin": "test",
        },
    )
    response.raise_for_status()
    return response.json()


def _fault_catalog(base_url: str) -> list[dict[str, str]]:
    with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as client:
        health = client.get("/health")
        health.raise_for_status()
        payload = health.json()
        if payload.get("fact_provider_status") != "saleor_ready":
            raise RuntimeError("Saleor sandbox is not ready")
        response = client.get(
            "/fault-scenarios", headers={"X-Api-Key": "local-sandbox-key"}
        )
        response.raise_for_status()
        scenarios = response.json().get("scenarios") or []
    if len(scenarios) < 6:
        raise RuntimeError("at least six fault scenarios are required")
    return scenarios


def verify(sandbox_port: int) -> dict[str, Any]:
    sandbox_base = f"http://127.0.0.1:{sandbox_port}"
    fault_cases = _fault_catalog(sandbox_base)

    with tempfile.TemporaryDirectory(prefix="saleor-main-workflow-") as directory:
        state_dir = Path(directory)
        os.environ.update(_runtime_environment(state_dir, sandbox_port))

        import app.config as config_module

        config_module.get_settings.cache_clear()
        config_module.settings = config_module.get_settings()

        from app.db.seed import init_db, seed_all
        from app.db.session import SessionLocal
        from app.main import app
        from fastapi.testclient import TestClient

        init_db(drop=True)
        with SessionLocal() as session:
            seed_all(session)

        business_results: list[dict[str, Any]] = []
        fault_results: list[dict[str, Any]] = []
        with TestClient(app) as client:
            ops = _login(client, "ops")
            admin = _login(client, "admin")
            health = client.get("/api/v1/health").json()
            if (
                health.get("fact_adapter", {}).get("backend") != "http"
                or health.get("fact_adapter", {}).get("is_mock") is not False
                or health.get("llm", {}).get("provider") != "stub"
                or health.get("execution", {}).get("backend") != "inline"
            ):
                raise RuntimeError("isolated verifier runtime is not using HTTP facts, stub LLM and inline execution")

            for scenario_id, content, expected_action in BUSINESS_CASES:
                ticket = _create_ticket(client, ops, scenario_id, content)
                started = time.perf_counter()
                analysis = client.post(
                    f"/api/v1/tickets/{ticket['id']}/analyze", headers=ops
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                task_id = analysis.json().get("run_id") if analysis.status_code == 202 else None
                detail = client.get(
                    f"/api/v1/tickets/{ticket['id']}", headers=admin
                ).json()
                task_status = None
                trace_sources: list[dict] = []
                trace: dict[str, Any] = {}
                if task_id:
                    task_status = client.get(
                        f"/api/v1/tasks/{task_id}", headers=admin
                    ).json().get("status")
                    trace = client.get(
                        f"/api/v1/tasks/{task_id}/trace", headers=admin
                    ).json()
                    trace_sources = trace.get("fact_sources") or []
                payment_payload = next(
                    (
                        item.get("payload") or {}
                        for item in detail.get("facts") or []
                        if item.get("source") == "payment"
                    ),
                    {},
                )
                reflection = dict((detail.get("analysis") or {}).get("reflection") or {})
                reflection["max_rounds"] = int(
                    config_module.get_settings().max_reflection_rounds
                )
                reflection["revision_rounds"] = sum(
                    1
                    for run in (trace.get("agent_runs") or [])
                    if run.get("agent_code") == "revision_agent"
                ) if task_id else 0
                result = evaluate_business_case(
                    scenario_id=scenario_id,
                    analyze_status=analysis.status_code,
                    task_status=task_status,
                    facts=_latest_fact_metadata(ticket["id"]),
                    trace_fact_sources=trace_sources,
                    recommendation=detail.get("recommendation"),
                    reflection=reflection,
                    refundable_amount=payment_payload.get("refundable_amount"),
                    expected_action=expected_action,
                    latency_ms=latency_ms,
                    fact_query_latency_ms=_fact_query_latency(trace),
                )
                business_results.append(result)
                failed = ",".join(
                    item["name"] for item in result["checks"] if not item["passed"]
                )
                print(
                    f"{'PASS' if result['passed'] else 'FAIL'} business {scenario_id}"
                    + (f" failed={failed}" if failed else ""),
                    flush=True,
                )

            for fault in fault_cases:
                scenario_id = str(fault["id"])
                ticket = _create_ticket(
                    client,
                    ops,
                    scenario_id,
                    "订单存在售后问题，我要退款，请先核验全部事实。",
                )
                started = time.perf_counter()
                analysis = client.post(
                    f"/api/v1/tickets/{ticket['id']}/analyze", headers=ops
                )
                latency_ms = int((time.perf_counter() - started) * 1000)
                task_id = analysis.json().get("run_id") if analysis.status_code == 202 else None
                detail = client.get(
                    f"/api/v1/tickets/{ticket['id']}", headers=admin
                ).json()
                task_status = None
                trace: dict[str, Any] = {}
                if task_id:
                    task_status = client.get(
                        f"/api/v1/tasks/{task_id}", headers=admin
                    ).json().get("status")
                    trace = client.get(
                        f"/api/v1/tasks/{task_id}/trace", headers=admin
                    ).json()
                result = evaluate_fault_case(
                    scenario_id=scenario_id,
                    target_source=str(fault["target_source"]),
                    expected_error=str(fault["expected_error"]),
                    analyze_status=analysis.status_code,
                    task_status=task_status,
                    facts=_latest_fact_metadata(ticket["id"]),
                    recommendation=detail.get("recommendation"),
                    latency_ms=latency_ms,
                    fact_query_latency_ms=_fact_query_latency(trace),
                )
                fault_results.append(result)
                failed = ",".join(
                    item["name"] for item in result["checks"] if not item["passed"]
                )
                print(
                    f"{'PASS' if result['passed'] else 'FAIL'} fault {scenario_id}"
                    + (f" failed={failed}" if failed else ""),
                    flush=True,
                )

        summary = summarize_results(business_results, fault_results)
        passed_business = summary["business_passed"]
        passed_faults = summary["fault_passed"]
        report = {
            "schema_version": "saleor-main-workflow-slice5-v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": (
                "verified_local"
                if passed_business == len(BUSINESS_CASES)
                and passed_faults == len(fault_results)
                and len(fault_results) >= 6
                else "failed"
            ),
            "environment": "local_high_fidelity_sandbox",
            "transport": "main_api_to_sandbox_real_http",
            "llm_provider": "stub",
            "fact_adapter": "http-fact-adapter",
            "task_execution": "inline",
            "enterprise_endpoint_verified": False,
            "real_payment_verified": False,
            "real_carrier_verified": False,
            "production_ready": False,
            "summary": summary,
            "business_cases": business_results,
            "fault_cases": fault_results,
        }
        assert_safe_evidence(report)
        from app.db import session as db_session

        db_session.engine.dispose()
        return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sandbox-port", type=int, default=8092)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()
    if not 1 <= args.sandbox_port <= 65535:
        parser.error("--sandbox-port must be between 1 and 65535")
    return args


def main() -> int:
    args = _parse_args()
    try:
        report = verify(args.sandbox_port)
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        print(f"Saleor main workflow verification failed: {exc}", file=sys.stderr)
        return 1
    if args.json_out:
        destination = args.json_out.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    summary = report["summary"]
    print(
        f"business={summary['business_passed']}/{summary['business_total']} "
        f"faults={summary['fault_passed']}/{summary['fault_total']} "
        "production_ready=false"
    )
    return 0 if report["status"] == "verified_local" else 1


if __name__ == "__main__":
    raise SystemExit(main())
