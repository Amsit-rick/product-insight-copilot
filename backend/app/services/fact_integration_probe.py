"""Safe, repeatable conformance probes for real after-sales fact adapters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from app.adapters.base import AdapterResult, FactAdapter
from app.adapters.http import ALLOWED_FIELDS, SOURCES
from app.services.masking import contains_sensitive, mask_structure

SCHEMA_VERSION = "fact-integration-probe-v1"
KNOWN_OUTCOMES = {
    "ok",
    "order_not_found",
    "adapter_auth_error",
    "adapter_timeout",
    "adapter_rate_limited",
    "adapter_upstream_error",
    "adapter_unavailable",
    "adapter_request_rejected",
    "adapter_invalid_response",
}
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")


class FactProbePlanError(ValueError):
    """The probe plan is unsafe, incomplete, or cannot be resolved."""


@dataclass(frozen=True)
class FactProbeExpectation:
    outcome: str
    required_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class FactProbeScenario:
    scenario_id: str
    order_id_env: str
    customer_id_env: str | None
    expect: dict[str, FactProbeExpectation]


@dataclass(frozen=True)
class FactProbePlan:
    plan_version: str
    scenarios: tuple[FactProbeScenario, ...]
    sha256: str

    @property
    def scenario_count(self) -> int:
        return len(self.scenarios)

    def safe_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "plan_version": self.plan_version,
            "plan_sha256": self.sha256,
            "scenario_count": self.scenario_count,
            "scenario_ids": [scenario.scenario_id for scenario in self.scenarios],
        }


def _read_payload(path: Path) -> tuple[dict, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FactProbePlanError(f"无法读取验收计划: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactProbePlanError(f"验收计划不是有效 UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FactProbePlanError("验收计划顶层必须是 JSON 对象")
    return payload, raw


def _env_reference(value: object, *, field: str, scenario_id: str) -> str:
    if not isinstance(value, str) or not ENV_NAME_RE.fullmatch(value):
        raise FactProbePlanError(f"{scenario_id}.{field} 必须是大写环境变量名")
    return value


def load_fact_probe_plan(path: str | Path) -> FactProbePlan:
    """Load a plan that references identifiers by environment variable only."""
    payload, raw = _read_payload(Path(path))
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise FactProbePlanError(f"schema_version 必须是 {SCHEMA_VERSION}")
    plan_version = payload.get("plan_version")
    if not isinstance(plan_version, str) or not plan_version.strip():
        raise FactProbePlanError("plan_version 不能为空")
    if len(plan_version) > 128:
        raise FactProbePlanError("plan_version 不能超过 128 个字符")
    raw_scenarios = payload.get("scenarios")
    if not isinstance(raw_scenarios, list) or not raw_scenarios:
        raise FactProbePlanError("scenarios 必须是非空数组")

    scenarios: list[FactProbeScenario] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_scenarios):
        if not isinstance(item, dict):
            raise FactProbePlanError(f"scenarios[{index}] 必须是对象")
        if "order_id" in item or "customer_id" in item:
            raise FactProbePlanError("order_id/customer_id 不得写入计划，请只引用环境变量")
        scenario_id = str(item.get("scenario_id") or "").strip()
        if not scenario_id or scenario_id in seen_ids:
            raise FactProbePlanError(f"scenario_id 为空或重复: {scenario_id or index}")
        seen_ids.add(scenario_id)
        order_id_env = _env_reference(
            item.get("order_id_env"), field="order_id_env", scenario_id=scenario_id
        )
        customer_id_env = item.get("customer_id_env")
        if customer_id_env is not None:
            customer_id_env = _env_reference(
                customer_id_env, field="customer_id_env", scenario_id=scenario_id
            )

        raw_expect = item.get("expect")
        if not isinstance(raw_expect, dict) or set(raw_expect) != set(SOURCES):
            raise FactProbePlanError(f"{scenario_id}.expect 必须完整覆盖四类来源: {list(SOURCES)}")
        expectations: dict[str, FactProbeExpectation] = {}
        for source in SOURCES:
            expected = raw_expect[source]
            if not isinstance(expected, dict):
                raise FactProbePlanError(f"{scenario_id}.expect.{source} 必须是对象")
            outcome = expected.get("outcome")
            if outcome not in KNOWN_OUTCOMES:
                raise FactProbePlanError(
                    f"{scenario_id}.expect.{source}.outcome 不受支持: {outcome}"
                )
            required_fields = expected.get("required_fields") or []
            if not isinstance(required_fields, list) or not all(
                isinstance(field, str) for field in required_fields
            ):
                raise FactProbePlanError(
                    f"{scenario_id}.expect.{source}.required_fields 必须是字符串数组"
                )
            invalid_fields = set(required_fields) - set(ALLOWED_FIELDS[source])
            if invalid_fields:
                raise FactProbePlanError(
                    f"{scenario_id}.expect.{source} 包含非允许字段: {sorted(invalid_fields)}"
                )
            if outcome != "ok" and required_fields:
                raise FactProbePlanError(
                    f"{scenario_id}.expect.{source} 为故障场景时不能要求成功字段"
                )
            expectations[source] = FactProbeExpectation(
                outcome=str(outcome), required_fields=tuple(required_fields)
            )
        scenarios.append(
            FactProbeScenario(
                scenario_id=scenario_id,
                order_id_env=order_id_env,
                customer_id_env=customer_id_env,
                expect=expectations,
            )
        )

    return FactProbePlan(
        plan_version=plan_version.strip(),
        scenarios=tuple(scenarios),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(percentile * (len(ordered) - 1))))
    return ordered[index]


def _fresh(observed_at: datetime | None, *, max_age_seconds: int) -> bool:
    if observed_at is None:
        return False
    value = observed_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - value.astimezone(timezone.utc)).total_seconds()
    return -5 <= age <= max_age_seconds


def _call(
    adapter: FactAdapter,
    source: str,
    order_id: str,
    customer_id: str | None,
    trace_id: str,
) -> tuple[AdapterResult, int]:
    started = time.perf_counter()
    if source == "order":
        result = adapter.fetch_order(order_id, request_id=trace_id)
    elif source == "payment":
        result = adapter.fetch_payment(order_id, request_id=trace_id)
    elif source == "logistics":
        result = adapter.fetch_logistics(order_id, request_id=trace_id)
    else:
        result = adapter.fetch_aftersale(
            order_id, customer_id=customer_id, request_id=trace_id
        )
    return result, int((time.perf_counter() - started) * 1000)


def _safe_outcome(result: AdapterResult) -> str:
    if result.ok:
        return "ok"
    code = str((result.error or {}).get("code") or "")
    return code if code in KNOWN_OUTCOMES else "unexpected_error"


def _validate_result(
    source: str,
    result: AdapterResult,
    latency_ms: int,
    expectation: FactProbeExpectation,
    *,
    max_observed_age_seconds: int,
) -> dict[str, Any]:
    payload_fields = sorted(str(key) for key in (result.payload or {}))
    outcome = _safe_outcome(result)
    checks = {
        "expected_outcome": outcome == expectation.outcome,
        "source_matches": result.source == source,
        "real_adapter": not result.is_mock,
        "adapter_identified": bool(result.adapter),
        "request_id_present": bool(result.request_id),
        "observed_at_fresh": _fresh(
            result.observed_at, max_age_seconds=max_observed_age_seconds
        ),
        "allowed_fields_only": set(payload_fields) <= set(ALLOWED_FIELDS[source]),
        "required_fields_present": set(expectation.required_fields) <= set(payload_fields),
        "sensitive_values_absent": (
            not contains_sensitive(
                json.dumps(result.payload or {}, ensure_ascii=False, default=str)
            )
            and mask_structure(result.payload or {}) == (result.payload or {})
        ),
    }
    return {
        "passed": all(checks.values()),
        "outcome": outcome,
        "latency_ms": latency_ms,
        "payload_fields": payload_fields,
        "checks": checks,
    }


def run_fact_probe(
    adapter: FactAdapter,
    plan: FactProbePlan,
    *,
    runs: int = 3,
    max_p95_ms: int = 20_000,
    max_observed_age_seconds: int = 900,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Execute all scenarios and return an identifier-free conformance artifact."""
    if runs < 1:
        raise FactProbePlanError("runs 必须大于等于 1")
    if getattr(adapter, "is_mock", True):
        raise FactProbePlanError("真实接口验收拒绝使用 mock adapter")
    values = environ if environ is not None else os.environ
    resolved: dict[str, tuple[str, str | None]] = {}
    for scenario in plan.scenarios:
        order_id = str(values.get(scenario.order_id_env) or "").strip()
        if not order_id:
            raise FactProbePlanError(f"缺少环境变量 {scenario.order_id_env}")
        customer_id = None
        if scenario.customer_id_env:
            customer_id = str(values.get(scenario.customer_id_env) or "").strip()
            if not customer_id:
                raise FactProbePlanError(f"缺少环境变量 {scenario.customer_id_env}")
        resolved[scenario.scenario_id] = (order_id, customer_id)

    trials: list[dict[str, Any]] = []
    latencies: list[int] = []
    source_latencies: dict[str, list[int]] = defaultdict(list)
    source_passed: dict[str, int] = defaultdict(int)
    checks_total = 0
    checks_passed = 0
    signatures: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for run_index in range(runs):
        for scenario in plan.scenarios:
            order_id, customer_id = resolved[scenario.scenario_id]
            trace_id = f"fact-probe-{uuid.uuid4().hex}"
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="fact-probe") as executor:
                futures = {
                    source: executor.submit(
                        _call, adapter, source, order_id, customer_id, trace_id
                    )
                    for source in SOURCES
                }
                source_results = {
                    source: _validate_result(
                        source,
                        *future.result(),
                        scenario.expect[source],
                        max_observed_age_seconds=max_observed_age_seconds,
                    )
                    for source, future in futures.items()
                }
            total_latency = int((time.perf_counter() - started) * 1000)
            latencies.append(total_latency)
            signature = tuple(source_results[source]["outcome"] for source in SOURCES)
            signatures[scenario.scenario_id].append(signature)
            for source, item in source_results.items():
                source_latencies[source].append(item["latency_ms"])
                source_passed[source] += int(item["passed"])
                checks_total += len(item["checks"])
                checks_passed += sum(1 for passed in item["checks"].values() if passed)
            trials.append(
                {
                    "scenario_id": scenario.scenario_id,
                    "run": run_index + 1,
                    "passed": all(item["passed"] for item in source_results.values()),
                    "latency_ms": total_latency,
                    "sources": source_results,
                }
            )

    p95 = _percentile(latencies, 0.95)
    latency_passed = p95 <= max_p95_ms
    checks_total += 1
    checks_passed += int(latency_passed)
    consistent = sum(1 for items in signatures.values() if len(set(items)) == 1)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan": plan.safe_metadata(),
        "runtime": {
            "adapter": str(getattr(adapter, "name", "unknown")),
            "runs": runs,
            "max_p95_ms": max_p95_ms,
            "max_observed_age_seconds": max_observed_age_seconds,
        },
        "passed": all(trial["passed"] for trial in trials) and latency_passed,
        "summary": {
            "checks_total": checks_total,
            "checks_passed": checks_passed,
            "scenario_consistency_rate": round(consistent / len(signatures), 4),
            "p95_latency_ms": p95,
            "latency_within_limit": latency_passed,
            "per_source": {
                source: {
                    "calls": len(source_latencies[source]),
                    "pass_rate": round(
                        source_passed[source] / len(source_latencies[source]), 4
                    )
                    if source_latencies[source]
                    else 0.0,
                    "p95_latency_ms": _percentile(source_latencies[source], 0.95),
                }
                for source in SOURCES
            },
        },
        "trials": trials,
    }


__all__ = [
    "FactProbePlan",
    "FactProbePlanError",
    "load_fact_probe_plan",
    "run_fact_probe",
]
