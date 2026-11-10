"""单机 Compose 部署证据必须版本绑定、脱敏且失败关闭。"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "verify_compose_deployment.py"
SPEC = importlib.util.spec_from_file_location("verify_compose_deployment", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def _container(service: str, *, image_id: str = "sha256:current") -> dict:
    app_service = service in {"api", "worker"}
    environment = [
        "APP_ENV=test",
        "APP_RELEASE_SHA=abc123",
        "DEBUG=false",
        "STORAGE_BACKEND=minio",
        "TASK_EXECUTION_BACKEND=queue",
        "VECTOR_BACKEND=pgvector",
        "AUTO_SEED=false",
        "CACHE_BACKEND=redis",
        "CHECKPOINTER_BACKEND=postgres",
        "WORKER_EMBEDDED=false",
        "JWT_SECRET=must-not-appear-in-evidence",
    ]
    return {
        "Image": image_id,
        "Config": {
            "Image": "pic-api:local" if app_service else f"{service}:pinned@sha256:digest",
            "User": "appuser" if app_service else "999",
            "Env": environment,
            "Labels": {"com.docker.compose.service": service},
        },
        "State": {
            "Running": True,
            "Status": "running",
            "ExitCode": 0,
            "StartedAt": "2026-11-09T00:00:00Z",
            "Health": {"Status": "healthy"},
        },
        "HostConfig": {
            "ReadonlyRootfs": app_service,
            "CapDrop": ["ALL"] if app_service else [],
            "SecurityOpt": ["no-new-privileges:true"] if app_service else [],
            "Memory": 2_147_483_648,
            "NanoCpus": 2_000_000_000,
            "PidsLimit": 256,
        },
    }


def _runner(*, stale_worker: bool = False):
    names = ["api-1", "worker-1", "worker-2", "postgres-1", "redis-1"]
    containers = [
        _container("api"),
        _container("worker"),
        _container("worker", image_id="sha256:stale" if stale_worker else "sha256:current"),
        _container("postgres"),
        _container("redis"),
    ]

    def run(command: list[str]) -> str:
        if command[:2] == ["docker", "ps"]:
            return "\n".join(json.dumps({"Names": name}) for name in names)
        if command[:2] == ["docker", "inspect"]:
            return json.dumps(containers)
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps([{"Id": "sha256:current"}])
        raise AssertionError(command)

    return run


def _ready(_url: str, _timeout: float) -> dict:
    return {
        "status": "ready",
        "env": "test",
        "checks": {
            "database": {"ok": True},
            "cache": {"ok": True, "backend": "redis"},
            "execution": {
                "ok": True,
                "backend": "queue",
                "checkpointer_shared": True,
            },
            "storage": {"ok": True, "shared": True},
        },
    }


def test_single_host_compose_can_pass_runtime_and_hardening_without_claiming_production():
    result = module.collect_compose_evidence(
        project="pic-target-verify",
        base_url="http://127.0.0.1:18210",
        expected_env="test",
        expected_workers=2,
        expected_release_sha="abc123",
        expected_image="pic-api:local",
        timeout_seconds=3,
        command_runner=_runner(),
        http_loader=_ready,
        now=datetime(2026, 11, 9, 1, 0, tzinfo=UTC),
    )

    assert result["runtime_shape_ready"] is True
    assert result["application_runtime_hardening_ready"] is True
    assert result["deployment_hardening_ready"] is False
    assert result["checks"]["infrastructure_resource_limits"] is True
    assert result["checks"]["application_image_immutable"] is False
    assert result["passed"] is True
    assert result["single_host"] is True
    assert result["target_environment_ready"] is False
    assert result["production_ready"] is False
    serialized = json.dumps(result, ensure_ascii=False)
    assert "127.0.0.1:18210" not in serialized
    assert "must-not-appear-in-evidence" not in serialized


def test_stale_worker_image_fails_version_bound_deployment_gate():
    result = module.collect_compose_evidence(
        project="pic-target-verify",
        base_url="http://127.0.0.1:18210",
        expected_env="test",
        expected_workers=2,
        expected_release_sha="abc123",
        expected_image="pic-api:local",
        timeout_seconds=3,
        command_runner=_runner(stale_worker=True),
        http_loader=_ready,
        now=datetime(2026, 11, 9, 1, 0, tzinfo=UTC),
    )

    assert result["runtime_shape_ready"] is True
    assert result["checks"]["running_image_matches_expected"] is False
    assert result["application_runtime_hardening_ready"] is False
    assert result["deployment_hardening_ready"] is False
    assert result["passed"] is False
    assert result["production_ready"] is False
