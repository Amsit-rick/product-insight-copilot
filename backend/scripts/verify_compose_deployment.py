"""Verify a live single-host Docker Compose deployment without leaking secrets.

This probe is intentionally narrower than target-environment verification. It can
prove that one Compose project is running the expected commit with basic runtime
hardening, but it can never claim cross-host or production readiness.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
CommandRunner = Callable[[list[str]], str]
HttpLoader = Callable[[str, float], dict[str, Any]]


def _run_command(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"命令执行失败 ({' '.join(command[:2])}): {detail[:300]}")
    return completed.stdout


def _load_http_json(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "compose-probe/1"})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 - explicit operator URL
        body = response.read(512 * 1024)
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("健康检查响应必须是 JSON 对象")
    return payload


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _json_lines(raw: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _labels(value: dict[str, Any]) -> dict[str, str]:
    labels = value.get("Config", {}).get("Labels") or {}
    return labels if isinstance(labels, dict) else {}


def _selected_environment(value: dict[str, Any]) -> dict[str, str]:
    allowed = {
        "APP_ENV",
        "APP_RELEASE_SHA",
        "DEBUG",
        "STORAGE_BACKEND",
        "TASK_EXECUTION_BACKEND",
        "VECTOR_BACKEND",
        "AUTO_SEED",
        "CACHE_BACKEND",
        "CHECKPOINTER_BACKEND",
        "WORKER_EMBEDDED",
    }
    selected: dict[str, str] = {}
    for item in value.get("Config", {}).get("Env") or []:
        key, separator, content = str(item).partition("=")
        if separator and key in allowed:
            selected[key] = content
    return selected


def _is_non_root(user: str | None) -> bool:
    normalized = (user or "").strip().lower().split(":", 1)[0]
    return normalized not in {"", "0", "root"}


def _container_summary(value: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    labels = _labels(value)
    state = value.get("State") or {}
    host = value.get("HostConfig") or {}
    health = state.get("Health") or {}
    started_at = state.get("StartedAt")
    uptime_seconds: int | None = None
    if started_at and state.get("Running"):
        try:
            parsed = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            uptime_seconds = max(0, int((now - parsed.astimezone(UTC)).total_seconds()))
        except ValueError:
            uptime_seconds = None

    service = labels.get("com.docker.compose.service", "unknown")
    security_opt = [str(item).lower() for item in (host.get("SecurityOpt") or [])]
    cap_drop = [str(item).upper() for item in (host.get("CapDrop") or [])]
    environment = _selected_environment(value)
    health_status = health.get("Status") or "not_configured"
    if not state.get("Running") and state.get("ExitCode") == 0:
        health_status = "completed"
    return {
        "service": service,
        "state": "running" if state.get("Running") else str(state.get("Status") or "unknown"),
        "exit_code": state.get("ExitCode"),
        "health": health_status,
        "uptime_seconds": uptime_seconds,
        "image_reference": str(value.get("Config", {}).get("Image") or ""),
        "non_root": _is_non_root(value.get("Config", {}).get("User")),
        "read_only_rootfs": bool(host.get("ReadonlyRootfs")),
        "cap_drop_all": "ALL" in cap_drop,
        "no_new_privileges": any("no-new-privileges" in item for item in security_opt),
        "resource_limits": {
            "memory_bytes": int(host.get("Memory") or 0),
            "nano_cpus": int(host.get("NanoCpus") or 0),
            "pids_limit": host.get("PidsLimit"),
        },
        "release_sha": environment.get("APP_RELEASE_SHA", ""),
        "runtime_profile": {
            key.lower(): environment.get(key, "")
            for key in (
                "APP_ENV",
                "DEBUG",
                "STORAGE_BACKEND",
                "TASK_EXECUTION_BACKEND",
                "VECTOR_BACKEND",
                "AUTO_SEED",
                "CACHE_BACKEND",
                "CHECKPOINTER_BACKEND",
                "WORKER_EMBEDDED",
            )
        },
        "_image_id": str(value.get("Image") or ""),
    }


def _resource_limits_present(summary: dict[str, Any]) -> bool:
    limits = summary["resource_limits"]
    return bool(
        limits["memory_bytes"] > 0
        and limits["nano_cpus"] > 0
        and isinstance(limits["pids_limit"], int)
        and limits["pids_limit"] > 0
    )


def _health_ready_checks(payload: dict[str, Any], expected_env: str) -> dict[str, bool]:
    checks = payload.get("checks") or {}
    execution = checks.get("execution") or {}
    storage = checks.get("storage") or {}
    cache = checks.get("cache") or {}
    database = checks.get("database") or {}
    return {
        "status_ready": payload.get("status") == "ready",
        "environment_matches": payload.get("env") == expected_env,
        "database_ok": database.get("ok") is True,
        "redis_cache_ok": cache.get("ok") is True and cache.get("backend") == "redis",
        "queue_execution_ok": execution.get("ok") is True and execution.get("backend") == "queue",
        "shared_checkpointer_ok": execution.get("checkpointer_shared") is True,
        "shared_storage_ok": storage.get("ok") is True and storage.get("shared") is True,
    }


def collect_compose_evidence(
    *,
    project: str,
    base_url: str,
    expected_env: str,
    expected_workers: int,
    expected_release_sha: str,
    expected_image: str,
    timeout_seconds: float,
    command_runner: CommandRunner = _run_command,
    http_loader: HttpLoader = _load_http_json,
    now: datetime | None = None,
) -> dict[str, Any]:
    current_time = now or datetime.now(UTC)
    ps_output = command_runner(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            "{{json .}}",
        ]
    )
    ps_rows = _json_lines(ps_output)
    names = [str(row.get("Names") or "") for row in ps_rows if row.get("Names")]
    inspected = json.loads(command_runner(["docker", "inspect", *names])) if names else []
    summaries = [_container_summary(item, now=current_time) for item in inspected]
    by_service: dict[str, list[dict[str, Any]]] = {}
    for item in summaries:
        by_service.setdefault(item["service"], []).append(item)

    api_items = by_service.get("api", [])
    worker_items = by_service.get("worker", [])
    readiness_error = ""
    readiness_payload: dict[str, Any] = {}
    try:
        readiness_payload = http_loader(base_url.rstrip("/") + "/health/ready", timeout_seconds)
    except Exception as exc:  # noqa: BLE001 - evidence must record a closed failure
        readiness_error = type(exc).__name__
    readiness_checks = _health_ready_checks(readiness_payload, expected_env)

    expected_image_id = ""
    try:
        image_rows = json.loads(command_runner(["docker", "image", "inspect", expected_image]))
        if image_rows:
            expected_image_id = str(image_rows[0].get("Id") or "")
    except Exception:  # noqa: BLE001 - a missing local image is a failed check below
        expected_image_id = ""

    app_items = api_items + worker_items
    infrastructure_items = [
        item
        for service in ("postgres", "redis", "minio")
        for item in by_service.get(service, [])
        if item["state"] == "running"
    ]
    checks = {
        "project_found": bool(summaries),
        "api_singleton": len(api_items) == 1,
        "worker_count_met": len(worker_items) >= expected_workers,
        "postgres_running": any(item["state"] == "running" for item in by_service.get("postgres", [])),
        "redis_running": any(item["state"] == "running" for item in by_service.get("redis", [])),
        "api_healthy": bool(api_items) and api_items[0]["health"] == "healthy",
        "workers_healthy": bool(worker_items)
        and all(item["health"] == "healthy" for item in worker_items),
        "app_non_root": bool(app_items) and all(item["non_root"] for item in app_items),
        "app_read_only_rootfs": bool(app_items)
        and all(item["read_only_rootfs"] for item in app_items),
        "app_capabilities_dropped": bool(app_items)
        and all(item["cap_drop_all"] for item in app_items),
        "app_no_new_privileges": bool(app_items)
        and all(item["no_new_privileges"] for item in app_items),
        "app_resource_limits": bool(app_items)
        and all(_resource_limits_present(item) for item in app_items),
        "infrastructure_resource_limits": bool(infrastructure_items)
        and all(_resource_limits_present(item) for item in infrastructure_items),
        "application_image_immutable": bool(app_items)
        and all("@sha256:" in item["image_reference"] for item in app_items),
        "running_image_matches_expected": bool(expected_image_id)
        and bool(app_items)
        and all(item["_image_id"] == expected_image_id for item in app_items),
        "release_sha_bound": bool(expected_release_sha)
        and bool(app_items)
        and all(item["release_sha"] == expected_release_sha for item in app_items),
        **readiness_checks,
    }

    runtime_keys = {
        "project_found",
        "api_singleton",
        "worker_count_met",
        "postgres_running",
        "redis_running",
        "api_healthy",
        "workers_healthy",
        "status_ready",
        "environment_matches",
        "database_ok",
        "redis_cache_ok",
        "queue_execution_ok",
        "shared_checkpointer_ok",
        "shared_storage_ok",
    }
    application_hardening_keys = {
        "app_non_root",
        "app_read_only_rootfs",
        "app_capabilities_dropped",
        "app_no_new_privileges",
        "app_resource_limits",
        "running_image_matches_expected",
        "release_sha_bound",
    }
    runtime_shape_ready = all(checks[key] for key in runtime_keys)
    application_runtime_hardening_ready = all(
        checks[key] for key in application_hardening_keys
    )
    deployment_hardening_ready = bool(
        application_runtime_hardening_ready
        and checks["infrastructure_resource_limits"]
        and checks["application_image_immutable"]
    )

    public_containers: list[dict[str, Any]] = []
    for item in summaries:
        public_containers.append({key: value for key, value in item.items() if not key.startswith("_")})

    return {
        "schema_version": "compose-deployment-probe-v1",
        "generated_at": current_time.isoformat(),
        "commit_sha": expected_release_sha,
        "scope": "single_host_compose",
        "single_host": True,
        "project": project,
        "configuration": {
            "base_url_fingerprint": _fingerprint(base_url),
            "expected_env": expected_env,
            "expected_workers": expected_workers,
            "expected_image": expected_image,
            "timeout_seconds": timeout_seconds,
        },
        "checks": checks,
        "runtime_shape_ready": runtime_shape_ready,
        "application_runtime_hardening_ready": application_runtime_hardening_ready,
        "deployment_hardening_ready": deployment_hardening_ready,
        "passed": runtime_shape_ready and application_runtime_hardening_ready,
        "target_environment_ready": False,
        "cross_host_or_target_environment_verified": False,
        "production_ready": False,
        "verification_basis": "single_host_compose_live_checks",
        "readiness_error": readiness_error or None,
        "service_counts": {key: len(value) for key, value in sorted(by_service.items())},
        "containers": public_containers,
        "limitations": [
            "single_host_compose",
            "no_cross_host_attestation",
            "no_real_model_quality_claim",
            "no_real_fact_platform_claim",
            "no_release_approval",
        ],
    }


def _git_head(command_runner: CommandRunner = _run_command) -> str:
    return command_runner(["git", "rev-parse", "HEAD"]).strip()


def _write_json(path: str | None, payload: dict[str, Any]) -> None:
    if not path:
        return
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"JSON 证据={output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单机 Docker Compose 部署与加固验收")
    parser.add_argument("--project", required=True, help="Compose project 名称")
    parser.add_argument("--base-url", required=True, help="API 基础地址；证据只保存哈希")
    parser.add_argument("--expected-env", default="test", choices=("dev", "test", "prod"))
    parser.add_argument("--expected-workers", type=int, default=2)
    parser.add_argument("--expected-release-sha", default="")
    parser.add_argument("--expected-image", default="pic-api:local")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--allow-insecure-http", action="store_true", help="仅限本地/CI")
    parser.add_argument("--json-out")
    args = parser.parse_args(argv)

    parsed = urlparse(args.base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        parser.error("--base-url 必须是完整 HTTP(S) 地址")
    if parsed.scheme == "http" and not args.allow_insecure_http:
        parser.error("HTTP 仅允许在显式传入 --allow-insecure-http 的本地/CI 验证中使用")
    if args.expected_workers < 1:
        parser.error("--expected-workers 必须大于 0")

    release_sha = args.expected_release_sha.strip() or _git_head()
    try:
        result = collect_compose_evidence(
            project=args.project,
            base_url=args.base_url,
            expected_env=args.expected_env,
            expected_workers=args.expected_workers,
            expected_release_sha=release_sha,
            expected_image=args.expected_image,
            timeout_seconds=args.timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - CLI must fail closed with a concise error
        print(f"部署验收失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print("=== 单机 Compose 部署验收 ===")
    print("运行形态=" + ("PASS" if result["runtime_shape_ready"] else "FAIL"))
    print(
        "应用容器加固="
        + ("PASS" if result["application_runtime_hardening_ready"] else "FAIL")
    )
    print("整体部署加固=" + ("READY" if result["deployment_hardening_ready"] else "NOT_READY"))
    print("目标生产环境=NOT_PROVEN")
    print("production_ready=false")
    _write_json(args.json_out, result)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
