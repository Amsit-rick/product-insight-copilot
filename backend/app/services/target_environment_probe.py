"""Redacted live probe for remote and cross-host deployment evidence."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

SCHEMA_VERSION = "target-environment-probe-v1"
NODE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
Fetcher = Callable[[str, float], tuple[int, Any]]


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _normalize_nodes(
    nodes: list[tuple[str, str]], *, allow_insecure_http: bool
) -> tuple[list[dict[str, str]], list[str]]:
    normalized: list[dict[str, str]] = []
    errors: list[str] = []
    labels: set[str] = set()
    if not 1 <= len(nodes) <= 8:
        errors.append("node_count_out_of_range")
    for index, (raw_label, raw_url) in enumerate(nodes):
        label = str(raw_label or "").strip()
        value = str(raw_url or "").strip().rstrip("/")
        if not NODE_LABEL_RE.fullmatch(label):
            errors.append(f"node_{index}_label_invalid")
        if label in labels:
            errors.append(f"node_{index}_label_duplicate")
        labels.add(label)
        try:
            parsed = urlparse(value)
            _ = parsed.port
        except ValueError:
            parsed = urlparse("")
        valid_url = bool(parsed.scheme in {"http", "https"} and parsed.hostname)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            valid_url = False
        if parsed.path not in {"", "/"}:
            valid_url = False
        if parsed.scheme == "http" and not allow_insecure_http:
            errors.append(f"node_{index}_https_required")
        if not valid_url:
            errors.append(f"node_{index}_url_invalid")
        if valid_url:
            normalized.append(
                {
                    "label": label,
                    "url": value,
                    "endpoint_fingerprint": _fingerprint(value),
                }
            )
    return normalized, errors


def _default_fetcher(url: str, timeout: float) -> tuple[int, Any]:
    with httpx.Client(timeout=timeout, follow_redirects=False) as client:
        response = client.get(url)
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        return response.status_code, payload


def _validate_readiness(
    status_code: int,
    payload: Any,
    expected_env: str,
    expected_release_sha: str,
) -> dict[str, bool]:
    data = payload if isinstance(payload, dict) else {}
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    database = checks.get("database") if isinstance(checks.get("database"), dict) else {}
    cache = checks.get("cache") if isinstance(checks.get("cache"), dict) else {}
    execution = checks.get("execution") if isinstance(checks.get("execution"), dict) else {}
    storage = checks.get("storage") if isinstance(checks.get("storage"), dict) else {}
    return {
        "http_200": status_code == 200,
        "status_ready": data.get("status") == "ready",
        "environment_matches": data.get("env") == expected_env,
        "release_sha_matches": (
            not expected_release_sha or data.get("release_sha") == expected_release_sha
        ),
        "database_ok": database.get("ok") is True,
        "redis_cache_ok": cache.get("ok") is True and cache.get("backend") == "redis",
        "queue_execution_ok": (
            execution.get("ok") is True
            and execution.get("backend") == "queue"
            and execution.get("queue_readable") is True
        ),
        "shared_checkpointer_ok": execution.get("checkpointer_shared") is True,
        "shared_storage_ok": storage.get("ok") is True and storage.get("shared") is True,
    }


def run_target_environment_probe(
    nodes: list[tuple[str, str]],
    *,
    expected_env: str = "prod",
    allow_real_calls: bool = False,
    allow_insecure_http: bool = False,
    confirm_distinct_hosts: bool = False,
    expected_release_sha: str = "",
    timeout_seconds: float = 10.0,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    """Check redacted readiness across target API nodes.

    ``confirm_distinct_hosts`` is an explicit operator attestation. URL shape alone
    cannot prove that two endpoints run on different physical or virtual hosts.
    """

    normalized, errors = _normalize_nodes(nodes, allow_insecure_http=allow_insecure_http)
    expected_env_valid = expected_env in {"dev", "test", "prod"}
    release_sha_valid = not expected_release_sha or bool(SHA_RE.fullmatch(expected_release_sha))
    timeout_valid = 0 < timeout_seconds <= 60
    distinct_shape_valid = not confirm_distinct_hosts or len(nodes) >= 2
    configuration = {
        "passed": (
            not errors
            and expected_env_valid
            and release_sha_valid
            and timeout_valid
            and distinct_shape_valid
        ),
        "checks": {
            "nodes_configured": bool(nodes),
            "node_definitions_valid": not errors,
            "expected_environment_valid": expected_env_valid,
            "expected_release_sha_valid": release_sha_valid,
            "timeout_valid": timeout_valid,
            "distinct_host_attestation_shape_valid": distinct_shape_valid,
        },
        "node_count": len(nodes),
        "nodes": [
            {
                "label": item["label"],
                "endpoint_fingerprint": item["endpoint_fingerprint"],
            }
            for item in normalized
        ],
    }
    base = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "configuration": configuration,
        "expected_env": expected_env,
        "expected_release_sha": expected_release_sha or None,
        "distinct_hosts_attested": bool(confirm_distinct_hosts),
    }
    if not configuration["passed"]:
        return {
            **base,
            "executed": False,
            "passed": False,
            "runtime_shape_ready": False,
            "target_environment_ready": False,
            "cross_host_or_target_environment_verified": False,
            "production_ready": False,
            "reason": "target_configuration_invalid",
        }
    if not allow_real_calls:
        return {
            **base,
            "executed": False,
            "passed": False,
            "runtime_shape_ready": False,
            "target_environment_ready": False,
            "cross_host_or_target_environment_verified": False,
            "production_ready": False,
            "reason": "real_target_calls_not_authorized",
        }

    call = fetcher or _default_fetcher
    node_results: list[dict[str, Any]] = []
    for item in normalized:
        started = time.perf_counter()
        try:
            status_code, payload = call(f"{item['url']}/health/ready", timeout_seconds)
            checks = _validate_readiness(
                status_code,
                payload,
                expected_env,
                expected_release_sha,
            )
            node_results.append(
                {
                    "label": item["label"],
                    "endpoint_fingerprint": item["endpoint_fingerprint"],
                    "passed": all(checks.values()),
                    "checks": checks,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                }
            )
        except Exception as exc:
            node_results.append(
                {
                    "label": item["label"],
                    "endpoint_fingerprint": item["endpoint_fingerprint"],
                    "passed": False,
                    "failure_type": type(exc).__name__,
                    "latency_ms": int((time.perf_counter() - started) * 1000),
                }
            )

    passed = bool(node_results) and all(item["passed"] for item in node_results)
    target_ready = passed and expected_env == "prod"
    cross_host_verified = target_ready and confirm_distinct_hosts and len(node_results) >= 2
    if cross_host_verified:
        basis = "live_checks_plus_operator_distinct_host_attestation"
    elif passed:
        basis = "live_runtime_checks_without_cross_host_attestation"
    else:
        basis = "live_runtime_checks_failed"
    return {
        **base,
        "executed": True,
        "passed": passed,
        "runtime_shape_ready": passed,
        "target_environment_ready": target_ready,
        "cross_host_or_target_environment_verified": cross_host_verified,
        "verification_basis": basis,
        "node_results": node_results,
        # 该探针不覆盖真实模型、事实接口、外部消息、客服测试等其它生产门禁。
        "production_ready": False,
    }


__all__ = ["SCHEMA_VERSION", "run_target_environment_probe"]
